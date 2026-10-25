"""会话只在真实调用和原保存交付后关闭剩余基准清单。"""

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.background_flow import BackgroundFlow
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.baseline import PendingBaseline
from camctl.capture.baseline_models import (
    BaselineFixSave, BaselineReadIntent, BaselineReadReturn, BaselineRef, BaselineState,
)
from camctl.capture.baseline_reads import PendingBaselineRead
from camctl.capture.result_scans import PendingResultScan
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead, DirectoryScan
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.session.outcome import SessionOutcome

pytestmark = pytest.mark.asyncio


def _baseline(events, label):
    pending = PendingBaseline(1, DeviceBinding("cam-1", "dji-action6"), ("/DCIM",))
    scan = create_autospec(DirectoryScan, instance=True, spec_set=True)
    scan.close.side_effect = lambda: events.append(label)
    pending.directory_scan = scan
    return pending, scan


def _world(mocker):
    events = []
    deps = lifecycle.RuntimeDeps(CommandMode.SUBMIT, load_config({}, ConfigDefaults()),
        Path("/state.db"), Path("/session.lock"), Path("/admission.lock"), None)
    deps.log_runtime = object()
    pending, scan = _baseline(events, "baseline")
    deps.capture_baselines[1] = pending
    mocker.patch.object(lifecycle, "_failure_log_wiring", autospec=True, return_value=(None, None))
    close_log = mocker.patch.object(lifecycle, "_close_log_runtime", autospec=True,
                                   side_effect=lambda *args, **kwargs: events.append("logs"))
    mocker.patch.object(lifecycle, "_emit_session_error", autospec=True)
    session = mocker.patch.object(lifecycle, "run_session", autospec=True,
                                 return_value=SessionOutcome(succeeded=True))
    return deps, pending, scan, session, close_log, events


async def test_baseline_closes_after_actual_owner_and_before_shared_log(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(context):
        pending.in_call = True
        entered.set()
        await release.wait()
        pending.in_call = False
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
        assert events == ["actual-end", "baseline", "logs"]
        assert deps.capture_baselines[1] is pending
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)
        await owner.settle()


async def test_baseline_close_failure_keeps_owner_and_closes_other_resources(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    failure = OSError("baseline close failed")
    scan.close.side_effect = failure
    second, second_scan = _baseline(events, "second-baseline")
    deps.capture_baselines[2] = second
    result_scan = create_autospec(PendingResultScan, instance=True, spec_set=True)
    result_scan.in_call = False
    result_scan.close.side_effect = lambda: events.append("result-scan")
    deps.capture_result_scans[1] = result_scan

    with pytest.raises(OSError) as caught:
        await lifecycle.execute_command(deps, None, flows={})

    assert caught.value is failure
    assert deps.capture_baselines[1] is pending and pending.directory_scan is scan
    second_scan.close.assert_called_once()
    result_scan.close.assert_called_once()
    close_log.assert_awaited_once()
    assert events == ["second-baseline", "result-scan", "logs"]


async def test_baseline_close_failure_preserves_original_session_error(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    primary = RuntimeError("session failed")
    session.side_effect = primary
    scan.close.side_effect = OSError("baseline close failed")

    with pytest.raises(RuntimeError) as caught:
        await lifecycle.execute_command(deps, None, flows={})

    assert caught.value is primary
    assert any("baseline close failed" in note for note in primary.__notes__)
    assert deps.capture_baselines[1] is pending and pending.directory_scan is scan
    close_log.assert_awaited_once()


async def test_baseline_is_not_closed_while_directory_call_is_active(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    pending.in_call = True

    with pytest.raises(RuntimeError):
        await lifecycle.execute_command(deps, None, flows={})

    scan.close.assert_not_called()
    assert deps.capture_baselines[1] is pending
    close_log.assert_awaited_once()


async def test_baseline_is_not_closed_while_actual_read_task_is_active(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    owner = create_autospec(PendingBaselineRead, instance=True, spec_set=True)
    owner.task = task
    pending.read_owner = owner
    try:
        with pytest.raises(ConsistencyError):
            await lifecycle.execute_command(deps, None, flows={})
        scan.close.assert_not_called()
        assert deps.capture_baselines[1] is pending
        close_log.assert_awaited_once()
    finally:
        release.set()
        await task


async def test_baseline_is_not_closed_while_background_owner_remains(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
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


@pytest.mark.parametrize("read_save", [False, True])
async def test_baseline_close_keeps_unknown_original_save_and_returned_page(mocker, read_save):
    deps, pending, scan, session, close_log, events = _world(mocker)
    occurred_at = 1_700_000_000_000_000
    if read_save:
        intent = BaselineReadIntent(1, 1, pending.binding, pending.directories, None,
            Decimal("2"), BaselineRef(1, BaselineState.COLLECTING, None, None, 0, 0),
            1, new_operation_key(), new_operation_key(), occurred_at)
        owner = PendingBaselineRead(intent)
        returned = DirectoryRead(None, None, Page((), None))
        owner.return_request = BaselineReadReturn(1, occurred_at, None, returned)
        original = owner.saves.begin(owner.return_request, key=intent.result_save_key)
        pending.read_owner = owner
        saves = owner.saves
    else:
        saves = pending.saves
        original = saves.begin(BaselineFixSave(1, 0, 0, occurred_at))
    capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
    save = capture.save_baseline_read_return if read_save else capture.fix_baseline
    save.return_value = DbOutcome(DbOutcomeKind.UNKNOWN)
    assert saves.save(capture, object()).kind is DbOutcomeKind.UNKNOWN

    assert (await lifecycle.execute_command(deps, None, flows={})).succeeded

    scan.close.assert_called_once()
    assert deps.capture_baselines[1] is pending
    assert saves.pending is original
    if read_save:
        assert pending.read_owner is owner
        assert owner.return_request is original.request
        assert owner.return_request.returned == returned
    close_log.assert_awaited_once()


async def test_baseline_close_failure_preserves_original_cancellation(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    primary = asyncio.CancelledError("session canceled")
    session.side_effect = primary
    scan.close.side_effect = OSError("baseline close failed")

    with pytest.raises(asyncio.CancelledError) as caught:
        await lifecycle.execute_command(deps, None, flows={})

    assert caught.value is primary
    assert any("baseline close failed" in note for note in primary.__notes__)
    close_log.assert_awaited_once()


async def test_failure_diagnostic_cancellation_still_closes_baseline_and_log(mocker):
    deps, pending, scan, session, close_log, events = _world(mocker)
    session.return_value = SessionOutcome(succeeded=False, reason="state_db_error")
    entered = asyncio.Event()

    async def diagnostic(*args, **kwargs):
        events.append("diagnostic")
        entered.set()
        await asyncio.Future()

    lifecycle._emit_session_error.side_effect = diagnostic
    command = asyncio.create_task(lifecycle.execute_command(deps, None, flows={}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    command.cancel("diagnostic wait canceled")

    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(command, timeout=1)

    assert caught.value.args == ("diagnostic wait canceled",)
    scan.close.assert_called_once()
    close_log.assert_awaited_once()
    assert events == ["diagnostic", "baseline", "logs"]
