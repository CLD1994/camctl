"""真实会话模块与包内资源组合后的流程诊断责任。"""

from types import SimpleNamespace

import pytest

from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics, record_error
from support.logging import recording_log_channel
from camctl.session.service import run_session
from camctl.persistence.initialization import initialize_state, InitOutcome
from ..support.session import session_config, session_context


def observing_channel(ready):
    records = []

    def before_record(_record):
        assert ready(), "日志接纳提前阻止了必要业务收场"

    return recording_log_channel(records, before_record=before_record), records


def _session(tmp_path, name, flow):
    config = session_config(tmp_path)
    assert initialize_state(config, tmp_path / "state.db").outcome is InitOutcome.CREATED
    return session_context(config.paths.state_db,
        clock=SimpleNamespace(utc_micros=lambda: 1_750_000_000_000_000,
                              monotonic_ns=lambda: 0), flows={name: flow})


@pytest.mark.asyncio
async def test_foreground_flow_logs_after_its_business_result(tmp_path):
    saved = False
    channel, records = observing_channel(lambda: saved)

    async def flow(_context):
        nonlocal saved
        record_error({"code": "notification_failed", "stage": "motor"}, stage="motor")
        saved = True

    context = _session(tmp_path, "motor", flow)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        assert (await run_session(context, None)).succeeded
        assert len(records) == 1


@pytest.mark.asyncio
async def test_fatal_foreground_flow_delivers_outcome_before_logs(tmp_path):
    stopped = False
    channel, records = observing_channel(lambda: stopped)

    async def flow(_context):
        record_error(RuntimeError("实际设备错误"), stage="capture")
        raise RuntimeError("结果保存失败")

    context = _session(tmp_path, "scheduling", flow)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        result = await run_session(context, None)
        assert result.reason == "state_db_error"
        assert not records
        stopped = True
        await flush_diagnostics()
        assert len(records) == 1
