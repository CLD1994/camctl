"""业务结果在所属流程收场后交接，致命错误先交付必要停止。"""

from types import SimpleNamespace

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow
from camctl.logging_runtime.diagnostics import (
    error_diagnostics_context, flush_diagnostics, record_error,
)
from support.logging import recording_log_channel


def observing_channel(ready):
    records = []

    def before_record(_record):
        assert ready(), "日志接纳提前阻止了必要业务收场"

    return recording_log_channel(records, before_record=before_record), records


@pytest.mark.asyncio
async def test_background_result_logs_after_body_releases_resources():
    released = False
    channel, records = observing_channel(lambda: released)

    async def flow(_context):
        nonlocal released
        try:
            record_error(RuntimeError("读取失败"), stage="read", context={"copy_id": 41})
        finally:
            released = True

    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        owner = BackgroundFlow(flow)
        await owner(SimpleNamespace())
        await owner.settle()
        assert len(records) == 1
        await flush_diagnostics()
        assert len(records) == 1


@pytest.mark.asyncio
async def test_fatal_background_result_precedes_diagnostic_admission():
    stopped = False
    channel, records = observing_channel(lambda: stopped)
    failure = RuntimeError("状态库失效前已经取得读取失败")

    async def flow(_context):
        record_error(failure, stage="read")
        raise failure

    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        owner = BackgroundFlow(flow)
        with pytest.raises(RuntimeError) as caught:
            await owner(SimpleNamespace())
            await owner.settle()
        assert caught.value is failure
        assert not records
        stopped = True
        await flush_diagnostics()
        assert len(records) == 1
