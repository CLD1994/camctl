"""原诊断与副本持有同一实际写入事实，协作者全部使用内存。"""

import asyncio
from unittest.mock import patch

import pytest

from camctl.logging_runtime import diagnostics
from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult, LogRecord
from camctl.operations.models import CallOutcome

pytestmark = pytest.mark.asyncio


class MemoryWriter:
    def __init__(self, *, admit=True):
        self.records = []
        self.receipts = []
        self.admit_on_submit = admit
        self.entered = asyncio.Event()

    def submit(self, record, *, receipt=None):
        receipt = receipt if receipt is not None else LogReceipt()
        self.records.append(record)
        self.receipts.append(receipt)
        self.entered.set()
        if self.admit_on_submit:
            self.admit()
        return receipt

    def admit(self):
        record, receipt = self.records[-1], self.receipts[-1]
        receipt.complete_admission(AdmissionResult(
            AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0))

    def write(self):
        record, receipt = self.records[-1], self.receipts[-1]
        receipt.complete_write(AdmissionResult(
            AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0, delivered=True))


async def test_old_admission_then_new_submit_returns_original_pending_write_fact():
    channel = MemoryWriter()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(ValueError("first"), stage="read")
        await diagnostics.admit_diagnostic(state)
        assert state.status is diagnostics.DiagnosticStatus.ADMITTED
        receipt = diagnostics.submit_diagnostic(state)
        assert receipt is channel.receipts[0]
        assert receipt.write_result is None
        assert diagnostics.submit_diagnostic(state) is receipt
        channel.write()
        assert (await receipt).delivered is True
        await diagnostics.flush_diagnostics()
    assert len(channel.records) == 1


async def test_submit_during_original_admission_only_binds_original_receipt():
    channel = MemoryWriter(admit=False)
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        state = diagnostics.record_error(ValueError("pending"), stage="read")
        await channel.entered.wait()
        receipt = diagnostics.submit_diagnostic(state)
        assert receipt is channel.receipts[0]
        assert state.status is diagnostics.DiagnosticStatus.HELD
        assert len(channel.records) == 1
        channel.admit()
        await diagnostics.flush_diagnostics()
        assert state.status is diagnostics.DiagnosticStatus.ADMITTED
        assert receipt.write_result is None
        channel.write()
        assert (await receipt).delivered is True


@pytest.mark.parametrize("copy_written", [False, True])
async def test_reserved_and_adopted_copy_keep_original_receipt_until_actual_handoff(copy_written):
    channel = MemoryWriter()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        async with diagnostics.diagnostic_scope():
            state = diagnostics.record_error(ValueError("copy"), stage="report")
            reservation = diagnostics.reserve_diagnostic(state)
            assert reservation is not None
            receipt = diagnostics.submit_diagnostic(state)
            assert not channel.records
        async with diagnostics.diagnostic_scope():
            diagnostics.adopt_diagnostic(reservation)
            assert diagnostics.submit_diagnostic(state) is receipt
            assert not channel.records
            if copy_written:
                diagnostics.acknowledge_diagnostic_record(diagnostics.diagnostic_record_for(state))
                assert (await receipt).delivered is True
                # 副本后续失败也不抹去已经确认的普通日志追加事实。
                with pytest.raises(OSError):
                    raise OSError("later copy failed")
        if not copy_written:
            assert receipt is channel.receipts[0]
            assert receipt.write_result is None
            channel.write()
        assert (await receipt).delivered is True
        await diagnostics.flush_diagnostics()
    assert len(channel.records) == (0 if copy_written else 1)


async def test_unstarted_copy_release_submits_same_receipt_once():
    channel = MemoryWriter()
    with diagnostics.error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = diagnostics.record_error(ValueError("not-started"), stage="report")
        reservation = diagnostics.reserve_diagnostic(state)
        receipt = diagnostics.submit_diagnostic(state)
        diagnostics.release_diagnostic(reservation)
        assert diagnostics.submit_diagnostic(state) is receipt
        assert channel.receipts == [receipt]
        channel.write()
        assert (await receipt).delivered is True
        await diagnostics.flush_diagnostics()


async def test_cancelled_write_waiter_does_not_cancel_other_waiter_or_original_record():
    channel = MemoryWriter()
    state = diagnostics.prepare_log_diagnostic(LogRecord(LogLevel.ERROR, "shared"), channel=channel)
    receipt = diagnostics.submit_diagnostic(state)

    async def wait():
        return await receipt

    first, second = asyncio.create_task(wait()), asyncio.create_task(wait())
    await asyncio.sleep(0)
    first.cancel("one observer")
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    assert receipt.write_result is None
    channel.write()
    assert (await second).delivered is True
    assert len(channel.records) == 1


@pytest.mark.parametrize("fallback_result", [None, False])
async def test_fallback_receipt_separates_file_write_from_stderr_fact(fallback_result):
    messages = []

    def fallback(message):
        messages.append(message)
        return fallback_result

    state = diagnostics.prepare_log_diagnostic(LogRecord(LogLevel.ERROR, "fallback"), fallback=fallback)
    with patch("asyncio.create_task", side_effect=AssertionError("不能新建逐条任务")):
        receipt = diagnostics.submit_diagnostic(state)
        result = await receipt
    assert result.delivered is False
    assert result.fallback_written is (fallback_result is not False)
    assert len(messages) == 1
    assert diagnostics.submit_diagnostic(state) is receipt


async def test_late_scope_control_settles_every_shared_receipt_before_propagation():
    original = asyncio.CancelledError("first stderr control")
    entered, release = asyncio.Event(), asyncio.Event()
    receipts, states, messages = [], [], []

    def fallback(message):
        messages.append(message)
        if len(messages) == 1:
            raise original

    async def late_scope():
        async with diagnostics.diagnostic_scope():
            for text in ("first", "second"):
                state = diagnostics.prepare_log_diagnostic(LogRecord(LogLevel.ERROR, text))
                states.append(state)
                reservation = diagnostics.reserve_diagnostic(state)
                receipts.append(diagnostics.submit_diagnostic(state))
                diagnostics.adopt_diagnostic(reservation)
            entered.set()
            await release.wait()

    with diagnostics.error_diagnostics_context(None, fallback, background=True):
        task = asyncio.create_task(late_scope())
        await entered.wait()
        await diagnostics.flush_diagnostics()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
    assert raised.value is original
    assert all(state.status is diagnostics.DiagnosticStatus.FALLBACK for state in states)
    results = [await receipt for receipt in receipts]
    assert [result.delivered for result in results] == [False, False]
    assert [result.fallback_written for result in results] == [False, True]
    assert messages == ["first", "second"]


@pytest.mark.parametrize("call_outcome", [False, True])
async def test_late_original_error_binds_receipt_before_fallback_control(call_outcome):
    original, control = ValueError("original"), asyncio.CancelledError("stderr control")
    messages = []

    def fallback(message):
        messages.append(message)
        raise control

    with diagnostics.error_diagnostics_context(None, fallback, background=True):
        await diagnostics.flush_diagnostics()
        with pytest.raises(asyncio.CancelledError) as raised:
            if call_outcome:
                diagnostics.record_call_outcome(CallOutcome(), run_error=original)
            else:
                diagnostics.record_error(original, stage="late")
        assert raised.value is control
        state = diagnostics.diagnostic_state_for(original)
        assert state is not None
        receipt = diagnostics.submit_diagnostic(state)
        assert diagnostics.record_error(original, stage="outer") is state
        assert (await receipt).delivered is False
        assert (await receipt).fallback_written is False
    assert len(messages) == 1
