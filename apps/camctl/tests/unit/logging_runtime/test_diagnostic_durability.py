"""副本拥有者交付原日志的追加与持久事实，不重投已追加的记录。"""

import pytest

from camctl.logging_runtime import diagnostics
from camctl.logging_runtime.models import LogWriteResult

pytestmark = pytest.mark.asyncio


class NoOrdinarySubmission:
    def __init__(self):
        self.records = []

    def submit(self, record, *, receipt=None):
        self.records.append(record)
        pytest.fail("副本已确认原日志追加，不应把同一条记录交回普通写入")


@pytest.mark.parametrize("by_record", [False, True])
@pytest.mark.parametrize("durable", [False, True])
async def test_copy_completion_preserves_original_append_and_durability_without_resubmission(
        by_record, durable):
    channel = NoOrdinarySubmission()
    original = ValueError("original report error")
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(original, stage="report")
        reservation = diagnostics.reserve_diagnostic(state)
        assert reservation is not None
        receipt = diagnostics.submit_diagnostic(state)
        record = diagnostics.diagnostic_record_for(state)
        async with diagnostics.diagnostic_scope():
            diagnostics.adopt_diagnostic(reservation)
            completion = LogWriteResult(appended=True, durable=durable)
            if by_record:
                diagnostics.acknowledge_diagnostic_record(record, completion)
            else:
                diagnostics.acknowledge_diagnostic(state, completion)
            result = await receipt
            assert result.appended is True
            assert result.durable is durable
            assert result.delivered is durable
            assert result.fallback_written is False
            assert diagnostics.record_error(original, stage="outer") is state
            assert diagnostics.submit_diagnostic(state) is receipt
            # 同一条记录的后续消费者不能改写首次实际完成事实。
            diagnostics.acknowledge_diagnostic(state, LogWriteResult(True, durable=True))
            assert await receipt is result
        await diagnostics.flush_diagnostics()
    assert not channel.records


@pytest.mark.parametrize("by_record", [False, True])
async def test_original_durable_success_survives_later_copy_failure(by_record):
    channel = NoOrdinarySubmission()
    copy_error = OSError("copy failed after original sync")
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(ValueError("original report error"), stage="report")
        reservation = diagnostics.reserve_diagnostic(state)
        assert reservation is not None
        receipt = diagnostics.submit_diagnostic(state)
        record = diagnostics.diagnostic_record_for(state)
        with pytest.raises(OSError) as raised:
            async with diagnostics.diagnostic_scope():
                diagnostics.adopt_diagnostic(reservation)
                completion = LogWriteResult(appended=True, durable=True)
                if by_record:
                    diagnostics.acknowledge_diagnostic_record(record, completion)
                else:
                    diagnostics.acknowledge_diagnostic(state, completion)
                assert (await receipt).delivered is True
                raise copy_error
        assert raised.value is copy_error
        await diagnostics.flush_diagnostics()
        result = await receipt
        assert result.appended is result.durable is result.delivered is True
        assert diagnostics.submit_diagnostic(state) is receipt
    assert not channel.records
