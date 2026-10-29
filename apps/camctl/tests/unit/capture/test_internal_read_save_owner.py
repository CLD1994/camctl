"""内部读取结果与读取机会的保存、回调和消费共同协调。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture.media_flow import _save_internal_read_and_settle
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.read_attempts import hold_read_result, retry_read_business, save_read_business
from camctl.outputs.slots import SlotRequest
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

pytestmark = pytest.mark.asyncio


def _flow():
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    operations = create_autospec(OperationRepository, instance=True, spec_set=True)
    flow = SimpleNamespace(database=database, operations=operations,
        pending_read_results={}, pending_read_business={}, pending_read_ends={}, continuing_read_tickets={})
    ticket = AttemptTicket(1, "read", "9", "read/9", 28)
    evidence = EvidenceRegistry((EvidenceContract("read_returned", 1, "read", frozenset()),))
    actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    pending = hold_read_result(flow, AttemptFinish(ticket,
        validate_outcome(ticket, actual, evidence), 123, run_finish=RunFinish(RunOutcome.SUCCEEDED)))
    return flow, pending


@pytest.mark.parametrize("blocked", ["result", "slot"])
async def test_internal_read_recovery_shares_result_and_slot_consumption_before_cancel(blocked):
    flow, pending = _flow()
    flow.database.read.return_value = (None, None)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def write(callback, *, key, **kwargs):
        stage = "result" if key == pending.key else "slot"
        writes.append(stage)
        if stage == blocked:
            entered.set()
            await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED)

    flow.database.write.side_effect = write
    first = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await entered.wait()
    second = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel("internal read waiter")
    await asyncio.sleep(0)
    assert not first.done()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
    assert writes == ["result", "slot"]
    assert not flow.pending_read_results and not flow.pending_read_business


async def test_internal_read_committed_notification_error_applies_required_slot_before_propagation():
    flow, pending = _flow()
    flow.database.read.return_value = (None, None)
    failure = RuntimeError("原读取已提交后的通知失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED))
    flow.database.write.side_effect = [failure, DbOutcome(DbOutcomeKind.COMPLETED)]

    with pytest.raises(RuntimeError) as raised:
        await _save_internal_read_and_settle(flow, pending)

    assert raised.value is failure
    assert not flow.pending_read_results and not flow.pending_read_business
    assert flow.database.write.await_count == 2


async def test_read_business_recovery_shares_original_receipt_callback_and_consumption():
    flow, _pending = _flow()
    repository = create_autospec(OutputsRepository, instance=True, spec_set=True)
    request = SlotRequest(9, 123)
    applied = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, "released")

    flow.database.write.side_effect = write
    first = asyncio.create_task(save_read_business(flow, 9, "release_read_slot", request,
        repository.release_read_slot, on_completed=applied.append))
    await entered.wait()
    second = asyncio.create_task(retry_read_business(flow))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert not any(isinstance(value, BaseException) for value in results)
    assert applied == ["released"] and not flow.pending_read_business
    assert flow.database.write.await_count == 1


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_unconfirmed_inspection_keeps_original_read_until_explicit_recovery(
        monkeypatch, cancel_waiter):
    from camctl.capture import segment_inspections

    flow, original = _flow()
    pending = replace(original, finish=replace(original.finish,
        run_finish=RunFinish(RunOutcome.FAILED,
            ErrorValue("source_file_unconfirmed", "source_read"))))
    identity = (pending.finish.ticket.run_id, pending.finish.ticket.attempt_id)
    flow.pending_read_results[identity] = pending
    flow.database.read.side_effect = [(None, 42), (4,), (None, 42), (4,)]
    flow.database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    entered, release = asyncio.Event(), asyncio.Event()

    async def inspect(*args):
        if inspected.await_count == 1:
            entered.set()
            await release.wait()
            return False
        return True

    inspected = AsyncMock(side_effect=inspect)
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_inspection_read_failure", inspected)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    first = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await entered.wait()
    second = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await asyncio.sleep(0)
    if cancel_waiter:
        first.cancel("waiter while inspection remains unconfirmed")
        await asyncio.sleep(0)
        assert not first.done()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if cancel_waiter:
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results[0] is None
    assert results[1] is None
    assert flow.pending_read_results[identity] is pending
    assert pending.key == original.key and pending.finish is not original.finish
    assert inspected.await_count == 1 and released.await_count == 0

    # 迟到观察者取得原未完成结果；只有明确恢复才核实后续阶段。
    await _save_internal_read_and_settle(flow, pending)
    assert inspected.await_count == 1 and flow.database.write.await_count == 1
    await _save_internal_read_and_settle(flow, pending, retry=True)
    assert not flow.pending_read_results
    assert inspected.await_count == 2 and released.await_count == 1
    assert flow.database.write.await_count == 1
    assert flow.database.write.await_args.kwargs["key"] == original.key
    await _save_internal_read_and_settle(flow, pending, retry=True)
    assert inspected.await_count == 2 and released.await_count == 1


async def test_consumed_unconfirmed_read_does_not_restart_from_old_snapshot(monkeypatch):
    from camctl.capture import segment_inspections

    flow, pending = _flow()
    flow.database.read.side_effect = [(None, 42), (4,)]
    flow.database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    inspected = AsyncMock(return_value=False)
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_inspection_read_failure", inspected)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    await _save_internal_read_and_settle(flow, pending)
    del flow.pending_read_results[(pending.finish.ticket.run_id, pending.finish.ticket.attempt_id)]
    later = hold_read_result(flow, replace(pending.finish,
        ticket=replace(pending.finish.ticket, attempt_id=2)))
    await _save_internal_read_and_settle(flow, pending, retry=True)
    assert flow.pending_read_results[(later.finish.ticket.run_id, later.finish.ticket.attempt_id)] is later
    assert inspected.await_count == 1 and released.await_count == 0
    assert flow.database.write.await_count == 1


@pytest.mark.parametrize("control", [False, True])
async def test_unconfirmed_inspection_preserves_committed_read_notification_error(monkeypatch, control):
    from camctl.capture import segment_inspections

    flow, pending = _flow()
    failure = (asyncio.CancelledError("original read control") if control
               else RuntimeError("original read notification"))
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED))
    flow.database.write.side_effect = failure
    flow.database.read.side_effect = [(None, 42), (4,)]
    inspected = AsyncMock(return_value=False)
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_inspection_read_failure", inspected)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    with pytest.raises(type(failure)) as raised:
        await _save_internal_read_and_settle(flow, pending)
    assert raised.value is failure
    assert flow.pending_read_results[(pending.finish.ticket.run_id, pending.finish.ticket.attempt_id)] is pending
    assert inspected.await_count == 1 and released.await_count == 0
