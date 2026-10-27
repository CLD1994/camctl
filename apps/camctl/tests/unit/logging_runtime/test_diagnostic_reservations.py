"""副本责任接管原诊断，不改变原记录与实际接纳事实。"""

import asyncio

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.diagnostics import (
    DiagnosticStatus, adopt_diagnostic, diagnostic_record_for, diagnostic_scope,
    error_diagnostics_context, flush_diagnostics, record_error,
    release_diagnostic, reserve_diagnostic,
)
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult

pytestmark = pytest.mark.asyncio


class MemoryChannel:
    def __init__(self):
        self.records = []

    def submit(self, record, *, receipt=None):
        receipt = receipt if receipt is not None else LogReceipt()
        self.records.append(record)
        result = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)
        receipt.complete_admission(result)
        receipt.complete_write(AdmissionResult(result.decision, 0, delivered=True))
        return receipt


async def test_reservation_removes_parent_responsibility_until_child_adopts_original_state():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        async with diagnostic_scope():
            state = record_error(ValueError("original-report-error"), stage="report_generate")
            original_record = diagnostic_record_for(state)
            reservation = reserve_diagnostic(state)
            assert reservation is not None
        await asyncio.sleep(0)
        assert not channel.records
        async with diagnostic_scope():
            assert adopt_diagnostic(reservation) is state
            assert adopt_diagnostic(reservation) is state
            release_diagnostic(reservation)
        await flush_diagnostics()
    assert channel.records == [original_record]
    assert state.status is DiagnosticStatus.ADMITTED


@pytest.mark.parametrize("explicit_release", [False, True])
async def test_unstarted_operation_returns_reservation_to_command_once(explicit_release):
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        async with diagnostic_scope():
            state = record_error(ValueError("unstarted"), stage="report")
            reservation = reserve_diagnostic(state)
        if explicit_release:
            release_diagnostic(reservation)
            release_diagnostic(reservation)
        await flush_diagnostics()
    assert len(channel.records) == 1
    assert state.status is DiagnosticStatus.ADMITTED


async def test_adopt_requires_child_scope_and_keeps_unadopted_record_for_final_flush():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        state = record_error(ValueError("unstarted"), stage="report")
        reservation = reserve_diagnostic(state)
        with pytest.raises(RuntimeError):
            adopt_diagnostic(reservation)
        await flush_diagnostics()
    assert len(channel.records) == 1


async def test_child_cannot_adopt_into_inherited_parent_scope():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        async with diagnostic_scope():
            state = record_error(ValueError("parent-scope"), stage="report")
            reservation = reserve_diagnostic(state)

            async def child():
                with pytest.raises(RuntimeError):
                    adopt_diagnostic(reservation)

            await asyncio.create_task(child())
        await flush_diagnostics()
    assert len(channel.records) == 1


async def test_already_admitting_state_cannot_be_reserved():
    entered = asyncio.Event()

    class BlockedChannel(MemoryChannel):
        def __init__(self):
            super().__init__()
            self.pending = []

        def submit(self, record, *, receipt=None):
            receipt = receipt if receipt is not None else LogReceipt()
            self.records.append(record)
            self.pending.append((record, receipt))
            entered.set()
            return receipt

        def release_admission(self):
            for record, receipt in self.pending:
                result = AdmissionResult(
                    AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)
                receipt.complete_admission(result)
                receipt.complete_write(AdmissionResult(result.decision, 0, delivered=True))
            self.pending.clear()

    channel = BlockedChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        state = record_error(ValueError("already-admitting"), stage="report")
        await entered.wait()
        assert reserve_diagnostic(state) is None
        channel.release_admission()
        await flush_diagnostics()
        assert reserve_diagnostic(state) is None
    assert len(channel.records) == 1


async def test_reservation_cannot_move_another_commands_record():
    channel = MemoryChannel()
    with error_diagnostics_context(channel, lambda text: pytest.fail(text)):
        state = record_error(ValueError("original-command"), stage="report")
        with error_diagnostics_context(MemoryChannel(), lambda text: pytest.fail(text)):
            assert reserve_diagnostic(state) is None
        await flush_diagnostics()
    assert len(channel.records) == 1
