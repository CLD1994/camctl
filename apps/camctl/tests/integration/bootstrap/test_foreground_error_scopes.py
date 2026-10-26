"""真实会话模块与包内资源组合后的流程诊断责任。"""

from types import SimpleNamespace

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics, record_error
from camctl.logging_runtime.service import AdmissionResult
from camctl.session.service import _drive_flows


class ObservingChannel:
    def __init__(self, ready):
        self.ready = ready
        self.records = []

    async def alog(self, record):
        assert self.ready(), "日志接纳提前阻止了必要业务收场"
        self.records.append(record)
        return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)


@pytest.mark.asyncio
async def test_foreground_flow_logs_after_its_business_result():
    saved = False
    channel = ObservingChannel(lambda: saved)

    async def flow(_context):
        nonlocal saved
        record_error({"code": "notification_failed", "stage": "motor"}, stage="motor")
        saved = True

    context = SimpleNamespace(flows={"motor": flow}, local_work=None)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        assert await _drive_flows(context) is None
        assert len(channel.records) == 1


@pytest.mark.asyncio
async def test_fatal_foreground_flow_delivers_outcome_before_logs():
    stopped = False
    channel = ObservingChannel(lambda: stopped)

    async def flow(_context):
        record_error(RuntimeError("实际设备错误"), stage="capture")
        raise RuntimeError("结果保存失败")

    context = SimpleNamespace(flows={"scheduling": flow}, local_work=None)
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        result = await _drive_flows(context)
        assert result.reason == "state_db_error"
        assert not channel.records
        stopped = True
        await flush_diagnostics()
        assert len(channel.records) == 1
