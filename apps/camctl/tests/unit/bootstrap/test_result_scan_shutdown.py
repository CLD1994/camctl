"""结果读取缓存只在实际拥有者交付后关闭，错误继续沿会话收场交付。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.background_flow import BackgroundFlow
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.result_scans import PendingResultScan
from camctl.session.outcome import SessionOutcome
from camctl.logging_runtime.service import LogChannel

pytestmark = pytest.mark.asyncio


class _ClosableScan(PendingResultScan):
    """结果读取适配层交接的同步 close 端口。"""

    def close(self): ...


def _world(mocker):
    events = []
    deps = lifecycle.RuntimeDeps(CommandMode.SUBMIT, load_config({}, ConfigDefaults()),
        Path("/state.db"), Path("/session.lock"), Path("/admission.lock"), None)
    deps.log_runtime = SimpleNamespace(channel=create_autospec(LogChannel, instance=True, spec_set=True))
    mocker.patch("camctl.logging_runtime.standard_logging.install_standard_logging", autospec=True)
    scan = create_autospec(_ClosableScan, instance=True, spec_set=True)
    scan.in_call = False
    scan.close.side_effect = lambda: events.append("scan")
    deps.capture_result_scans[1] = scan
    mocker.patch.object(lifecycle, "_failure_log_wiring", autospec=True, return_value=(None, None))
    close_log = mocker.patch.object(lifecycle, "_close_log_runtime", autospec=True,
                                   side_effect=lambda *args, **kwargs: events.append("logs"))
    session = mocker.patch.object(lifecycle, "run_session", autospec=True,
                                 return_value=SessionOutcome(succeeded=True))
    return deps, scan, session, close_log, events


async def test_result_cache_closes_after_actual_owner_and_before_shared_log(mocker):
    deps, scan, session, close_log, events = _world(mocker)
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(context):
        scan.in_call = True
        entered.set()
        await release.wait()
        scan.in_call = False
        events.append("actual-end")

    owner = BackgroundFlow(read)
    deps.background_flows = (owner,)

    async def run(context, source):
        await owner(context)
        await context.local_work.settle()
        return SessionOutcome(succeeded=True)

    session.side_effect = run
    command = asyncio.create_task(lifecycle.execute_command(deps, None, flows={}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        scan.close.assert_not_called()
        close_log.assert_not_awaited()
        release.set()
        assert (await asyncio.wait_for(command, timeout=1)).succeeded
        assert events == ["actual-end", "scan", "logs"]
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)
        await owner.settle()


async def test_result_cache_close_failure_preserves_error_and_closes_other_resources(mocker):
    deps, scan, session, close_log, events = _world(mocker)
    failure = OSError("directory cache close failed")
    scan.close.side_effect = failure
    second = create_autospec(_ClosableScan, instance=True, spec_set=True)
    second.in_call = False
    second.close.side_effect = lambda: events.append("second-scan")
    deps.capture_result_scans[2] = second
    with pytest.raises(OSError) as caught:
        await lifecycle.execute_command(deps, None, flows={})
    assert caught.value is failure
    second.close.assert_called_once()
    close_log.assert_awaited_once()
    assert events == ["second-scan", "logs"]


async def test_result_cache_is_not_closed_while_scan_call_is_active(mocker):
    deps, scan, session, close_log, events = _world(mocker)
    scan.in_call = True
    with pytest.raises(RuntimeError):
        await lifecycle.execute_command(deps, None, flows={})
    scan.close.assert_not_called()
    close_log.assert_awaited_once()


async def test_result_cache_is_not_closed_while_background_owner_remains(mocker):
    deps, scan, session, close_log, events = _world(mocker)
    release = asyncio.Event()

    async def read(context):
        await release.wait()

    owner = BackgroundFlow(read)
    deps.background_flows = (owner,)
    await owner(None)
    try:
        with pytest.raises(RuntimeError):
            await lifecycle.execute_command(deps, None, flows={})
        scan.close.assert_not_called()
        close_log.assert_awaited_once()
    finally:
        release.set()
        await owner.settle()


async def test_failure_diagnostic_wait_cancellation_still_closes_cache_and_shared_log(mocker):
    deps, scan, session, close_log, events = _world(mocker)
    session.return_value = SessionOutcome(succeeded=False, reason="state_db_error")
    entered = asyncio.Event()

    async def diagnostic(*args, **kwargs):
        events.append("diagnostic")
        entered.set()
        await asyncio.Future()

    mocker.patch.object(lifecycle, "_emit_session_error", autospec=True, side_effect=diagnostic)
    command = asyncio.create_task(lifecycle.execute_command(deps, None, flows={}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    command.cancel("diagnostic wait canceled")
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(command, timeout=1)
    assert caught.value.args == ("diagnostic wait canceled",)
    scan.close.assert_called_once()
    close_log.assert_awaited_once()
    assert events == ["scan", "diagnostic", "logs"]
