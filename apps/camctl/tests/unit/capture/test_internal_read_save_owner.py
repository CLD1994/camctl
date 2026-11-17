"""内部读取结果与读取机会的保存、回调和消费共同协调。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture import media_flow
from camctl.capture.media_flow import _save_internal_read_and_settle
from camctl.contracts.enums import enum_for
from camctl.contracts.values import OperationKey
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptFinish, FinishAttemptResult, FinishDisposition, RunFinish, RunOutcome, RunStatus,
)
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.read_attempts import hold_read_result, retry_read_business, save_read_business
from camctl.outputs.slots import SlotDecision, SlotOutcome, SlotRequest
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.segment_inspections import (
    InspectionResultSave, SegmentInspectionRef, SegmentInspectionRepository,
)

pytestmark = pytest.mark.asyncio


def _flow():
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    operations = create_autospec(OperationRepository, instance=True, spec_set=True)
    flow = SimpleNamespace(database=database, operations=operations,
        pending_read_results={}, pending_read_business={}, pending_read_ends={}, continuing_read_tickets={},
        pending_segment_inspection_results={})
    ticket = AttemptTicket(1, "read", "9", "read/9", 28)
    evidence = EvidenceRegistry((EvidenceContract("read_returned", 1, "read", frozenset()),))
    actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    pending = hold_read_result(flow, AttemptFinish(ticket,
        validate_outcome(ticket, actual, evidence), 123, run_finish=RunFinish(RunOutcome.SUCCEEDED)))
    return flow, pending


def _result_receipt(status=RunStatus.SUCCEEDED):
    return DbOutcome(DbOutcomeKind.COMPLETED,
                     FinishAttemptResult(FinishDisposition.SAVED, AttemptStatus.SUCCEEDED, status))


def _failed_read(flow, original):
    pending = replace(original, finish=replace(original.finish,
        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("source_file_unconfirmed", "source_read"))))
    flow.pending_read_results[(pending.finish.ticket.run_id, pending.finish.ticket.attempt_id)] = pending
    return pending


def _held_inspection(segment_inspections):
    request = InspectionResultSave(42, 3, {
        "metric": {"metric": "frames", "value": None, "reason": "source_file_unconfirmed"},
        "probe": None, "tool": {"ran": False, "error": None},
        "failure": {"code": "read_failed", "reason": "source_file_unconfirmed"},
    }, 123)
    return segment_inspections.PendingInspectionResult(request, OperationKey("b" * 32))


async def _wait_entered(task, entered):
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("保存任务在抵达等待点之前结束")
        assert waiter in done, "保存任务未抵达等待点"
    finally:
        waiter.cancel()


@pytest.mark.parametrize("blocked", ["result", "slot"])
async def test_internal_read_recovery_shares_result_and_slot_consumption_before_cancel(blocked, monkeypatch):
    flow, pending = _flow()
    flow.database.read.return_value = (None, None)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    owned = object()
    repository = create_autospec(OutputsRepository, instance=True, spec_set=True)
    repository.release_read_slot.return_value = DbOutcome(DbOutcomeKind.COMPLETED, SlotDecision(SlotOutcome.RELEASED))
    flow.operations.finish_attempt.return_value = _result_receipt()
    monkeypatch.setattr(media_flow, "OutputsRepository", lambda: repository)

    async def write(callback, *, key, **kwargs):
        stage = "result" if key == pending.key else "slot"
        writes.append(stage)
        if stage == blocked:
            entered.set()
            await release.wait()
        return callback(owned)

    flow.database.write.side_effect = write
    first = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await _wait_entered(first, entered)
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
    flow.operations.finish_attempt.assert_called_once_with(pending.finish, pending.key, owned)
    request, slot_key, actual_owned = repository.release_read_slot.call_args.args
    assert request == SlotRequest(9, pending.finish.occurred_at)
    assert slot_key != pending.key and actual_owned is owned
    repository.release_read_slot.assert_called_once()
    assert not flow.pending_read_results and not flow.pending_read_business


async def test_internal_read_committed_notification_error_applies_required_slot_before_propagation():
    flow, pending = _flow()
    flow.database.read.return_value = (None, None)
    failure = RuntimeError("原读取已提交后的通知失败")
    _attach_db_outcome(failure, _result_receipt())
    flow.database.write.side_effect = [failure, DbOutcome(DbOutcomeKind.COMPLETED, SlotDecision(SlotOutcome.RELEASED))]

    with pytest.raises(RuntimeError) as raised:
        await _save_internal_read_and_settle(flow, pending)

    assert raised.value is failure
    assert not flow.pending_read_results and not flow.pending_read_business
    assert flow.database.write.await_count == 2


async def test_read_business_recovery_shares_original_receipt_callback_and_consumption():
    flow, _pending = _flow()
    repository = create_autospec(OutputsRepository, instance=True, spec_set=True)
    request = SlotRequest(9, 123)
    receipt = SlotDecision(SlotOutcome.RELEASED)
    applied = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, receipt)

    flow.database.write.side_effect = write
    first = asyncio.create_task(save_read_business(flow, 9, "release_read_slot", request,
        repository.release_read_slot, on_completed=applied.append))
    await _wait_entered(first, entered)
    second = asyncio.create_task(retry_read_business(flow))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert not any(isinstance(value, BaseException) for value in results)
    assert applied == [receipt] and not flow.pending_read_business
    assert flow.database.write.await_count == 1


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_unconfirmed_inspection_keeps_original_read_until_explicit_recovery(
        monkeypatch, cancel_waiter):
    from camctl.capture import segment_inspections

    flow, original = _flow()
    pending = _failed_read(flow, original)
    identity = (pending.finish.ticket.run_id, pending.finish.ticket.attempt_id)
    inspection = SegmentInspectionRef(42, 4, 3, 8, 30, 1,
        {"metric": "frames", "value": 10},
        {"max_attempts": 1, "timeout_s": 5, "retry_interval_s": 0})
    error = '{"code":"source_file_unconfirmed","stage":"source_read"}'
    actual_result = '{"settlement":{"basis":"observed","evidence":{"type":"read_returned"}}}'
    flow.database.read.side_effect = [
        (None, 42), (4,),
        [(42, 10, 10, None, "a" * 64, int(enum_for("file_copies.verification_state").FAILED),
          '{"code":"source_file_unconfirmed"}', 28, 4, error, 0, 1)],
        None, inspection, (1, int(enum_for("operation_attempts.status").SUCCEEDED), actual_result),
        [(9,)],
    ]
    flow.occurred_at = lambda: 200
    repository = create_autospec(SegmentInspectionRepository, instance=True, spec_set=True)
    monkeypatch.setattr(segment_inspections, "SegmentInspectionRepository", lambda: repository)
    flow.operations.finish_attempt.return_value = _result_receipt(RunStatus.FAILED)
    def save_inspection(request, key, owned):
        if repository.save_result.call_count == 1:
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("回执未知"))
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         replace(inspection, state=request.state, result_json=request.result_json))
    repository.save_result.side_effect = save_inspection
    entered, release = asyncio.Event(), asyncio.Event()

    owned = object()
    async def write(callback, *, key, **kwargs):
        if key != pending.key and repository.save_result.call_count == 0:
            entered.set()
            await release.wait()
        return callback(owned)

    flow.database.write.side_effect = write
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    first = asyncio.create_task(_save_internal_read_and_settle(flow, pending))
    await _wait_entered(first, entered)
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
    assert identity not in flow.pending_read_results
    assert pending.key == original.key and pending.finish is not original.finish
    held = flow.pending_segment_inspection_results[42]
    assert held.request.inspection_id == 42 and held.request.occurred_at == 123
    assert released.await_count == 0
    flow.operations.finish_attempt.assert_called_once_with(pending.finish, pending.key, owned)
    repository.save_result.assert_called_once_with(held.request, held.key, owned)

    # 迟到读取观察者不能重新保存；显式恢复沿原检查申请与键核实。
    await _save_internal_read_and_settle(flow, pending)
    assert repository.save_result.call_count == 1 and flow.database.write.await_count == 2
    await segment_inspections.resume_pending_inspection_results(flow)
    assert not flow.pending_segment_inspection_results
    assert repository.save_result.call_count == 2
    assert repository.save_result.call_args.args == (held.request, held.key, owned)
    released.assert_awaited_once_with(flow, 9, 200)
    assert flow.database.write.await_count == 3
    await _save_internal_read_and_settle(flow, pending, retry=True)
    assert repository.save_result.call_count == 2 and released.await_count == 1


async def test_consumed_unconfirmed_read_does_not_restart_from_old_snapshot(monkeypatch):
    from camctl.capture import segment_inspections

    flow, pending = _flow()
    pending = _failed_read(flow, pending)
    flow.database.read.side_effect = [(None, 42), (4,)]
    flow.database.write.return_value = _result_receipt(RunStatus.FAILED)
    held = _held_inspection(segment_inspections)
    flow.pending_segment_inspection_results[42] = held
    inspected = AsyncMock(return_value=False)
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_inspection_read_failure", inspected)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    await _save_internal_read_and_settle(flow, pending)
    assert not flow.pending_read_results
    later = hold_read_result(flow, replace(pending.finish,
        ticket=replace(pending.finish.ticket, attempt_id=2)))
    await _save_internal_read_and_settle(flow, pending, retry=True)
    assert flow.pending_read_results[(later.finish.ticket.run_id, later.finish.ticket.attempt_id)] is later
    assert flow.pending_segment_inspection_results[42] is held
    assert inspected.await_count == 1 and released.await_count == 0
    assert flow.database.write.await_count == 1


@pytest.mark.parametrize("control", [False, True])
async def test_unconfirmed_inspection_preserves_committed_read_notification_error(monkeypatch, control):
    from camctl.capture import segment_inspections

    flow, pending = _flow()
    pending = _failed_read(flow, pending)
    failure = (asyncio.CancelledError("original read control") if control
               else RuntimeError("original read notification"))
    _attach_db_outcome(failure, _result_receipt(RunStatus.FAILED))
    flow.database.write.side_effect = failure
    flow.database.read.side_effect = [(None, 42), (4,)]
    held = _held_inspection(segment_inspections)
    flow.pending_segment_inspection_results[42] = held
    inspected = AsyncMock(return_value=False)
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(segment_inspections, "settle_inspection_read_failure", inspected)
    monkeypatch.setattr(segment_inspections, "settle_terminal_inspection_read_slot", released)
    with pytest.raises(type(failure)) as raised:
        await _save_internal_read_and_settle(flow, pending)
    assert raised.value is failure
    assert not flow.pending_read_results
    assert flow.pending_segment_inspection_results[42] is held
    assert inspected.await_count == 1 and released.await_count == 0
