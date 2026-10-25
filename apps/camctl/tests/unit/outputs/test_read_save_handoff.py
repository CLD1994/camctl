"""读取结果的原保存与业务续接共享完整责任，不启动真实协作者。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import media_flow
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
from camctl.outputs.read_attempts import (
    PendingReadBusiness, ReadContinuation, forget_read_result, hold_read_result,
    retry_read_business, save_prepared_read_business, save_read_business, save_read_result,
    stopped_read_result,
)
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import FinishObtain, OutputsRepository
from camctl.persistence.runtime import OwnedConnection

pytestmark = pytest.mark.asyncio


@pytest.fixture
def world(monkeypatch):
    owned = create_autospec(OwnedConnection, instance=True, spec_set=True)
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    operations = create_autospec(OperationRepository, instance=True, spec_set=True)
    outputs = create_autospec(OutputsRepository, instance=True, spec_set=True)
    operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    outputs.release_read_slot.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    outputs.finish_obtain.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'saved')
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, *, key, description='业务写入'):
        entered.set()
        await release.wait()
        return callback(owned)

    database.write.side_effect = write
    database.read.return_value = (None, None)
    monkeypatch.setattr(media_flow, 'OperationRepository', lambda: operations)
    monkeypatch.setattr(media_flow, 'OutputsRepository', lambda: outputs)
    runtime = SimpleNamespace(
        database=database, operations=operations, pending_read_results={},
        pending_read_ends={}, pending_read_business={}, continuing_read_tickets={},
    )
    return SimpleNamespace(runtime=runtime, database=database, operations=operations,
        outputs=outputs, entered=entered, release=release)


def _pending_result(world):
    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    finish = AttemptFinish(ticket, validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE),
        123, run_finish=RunFinish(RunOutcome.SUCCEEDED))
    world.runtime.continuing_read_tickets[2] = ReadContinuation(ticket, False)
    return hold_read_result(world.runtime, finish)


async def _join_after_release(world, *tasks):
    await asyncio.sleep(0)
    world.release.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


async def test_original_business_and_retry_share_write_application_and_deletion(world):
    request = FinishObtain(3, 9)
    applied = []
    original = asyncio.create_task(save_read_business(world.runtime, 2, 'finish', request,
        world.outputs.finish_obtain, on_completed=applied.append))
    await world.entered.wait()
    pending = world.runtime.pending_read_business[(2, 'finish')]
    resumed = asyncio.create_task(retry_read_business(world.runtime))
    receipts = await _join_after_release(world, original, resumed)
    assert receipts == [DbOutcome(DbOutcomeKind.COMPLETED, 'saved'), None]
    assert world.database.write.await_count == 1
    assert applied == ['saved'] and not world.runtime.pending_read_business
    world.outputs.finish_obtain.assert_called_once()
    assert world.outputs.finish_obtain.call_args.args[:2] == (request, pending.key)


async def test_prepared_business_late_success_keeps_original_receipt_without_reinsertion(world):
    from camctl.contracts.values import new_operation_key

    pending = PendingReadBusiness(world.outputs.finish_obtain, FinishObtain(3, 9), new_operation_key())
    world.release.set()
    first = await save_prepared_read_business(world.runtime, 2, 'finish', pending)
    late = await save_prepared_read_business(world.runtime, 2, 'finish', pending)
    assert late is first
    assert world.database.write.await_count == 1
    assert not world.runtime.pending_read_business


async def test_retry_snapshot_cannot_recreate_already_completed_business(world):
    from camctl.contracts.values import new_operation_key

    pending = [PendingReadBusiness(world.outputs.finish_obtain, FinishObtain(index, 9), new_operation_key())
        for index in (3, 4)]
    originals = [asyncio.create_task(save_prepared_read_business(world.runtime, index, 'finish', item))
        for index, item in enumerate(pending, 2)]
    await world.entered.wait()
    await asyncio.sleep(0)
    resumed = asyncio.create_task(retry_read_business(world.runtime))
    receipts = await _join_after_release(world, *originals, resumed)
    assert not any(isinstance(value, BaseException) for value in receipts)
    assert world.database.write.await_count == 2
    assert not world.runtime.pending_read_business


@pytest.mark.parametrize('kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_business_failure_is_sticky_until_explicit_retry(world, kind):
    world.release.set()
    request = FinishObtain(3, 9)
    world.outputs.finish_obtain.return_value = DbOutcome(kind)
    with pytest.raises(ConsistencyError) as first:
        await save_read_business(world.runtime, 2, 'finish', request, world.outputs.finish_obtain)
    pending = world.runtime.pending_read_business[(2, 'finish')]
    with pytest.raises(ConsistencyError) as late:
        await save_read_business(world.runtime, 2, 'finish', request, world.outputs.finish_obtain)
    assert late.value is first.value
    assert world.database.write.await_count == 1
    world.outputs.finish_obtain.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'verified')
    await retry_read_business(world.runtime)
    assert not world.runtime.pending_read_business
    assert world.database.write.await_count == 2
    assert all(call.args[:2] == (request, pending.key)
        for call in world.outputs.finish_obtain.call_args_list)


async def test_cancelled_business_waiter_still_joins_single_application(world):
    applied = []
    original = asyncio.create_task(save_read_business(world.runtime, 2, 'finish', FinishObtain(3, 9),
        world.outputs.finish_obtain, on_completed=applied.append))
    await world.entered.wait()
    resumed = asyncio.create_task(retry_read_business(world.runtime))
    original.cancel()
    await asyncio.sleep(0)
    original.cancel()
    receipts = await _join_after_release(world, original, resumed)
    assert isinstance(receipts[0], asyncio.CancelledError) and receipts[1] is None
    assert world.database.write.await_count == 1
    assert applied == ['saved'] and not world.runtime.pending_read_business


@pytest.mark.parametrize('kind', [DbOutcomeKind.COMPLETED, DbOutcomeKind.UNKNOWN])
async def test_actual_receipt_error_is_shared_and_applies_completed_business_once(world, kind):
    from camctl.contracts.values import new_operation_key

    error = RuntimeError('result notification')
    actual = DbOutcome(kind, 'actual', observer_error=error)
    world.outputs.finish_obtain.return_value = actual
    applied = []
    pending = PendingReadBusiness(world.outputs.finish_obtain, FinishObtain(3, 9),
        new_operation_key(), applied.append)
    write = world.database.write.side_effect

    async def write_error(callback, **kwargs):
        await write(callback, **kwargs)
        _attach_db_outcome(error, actual)
        raise error

    world.database.write.side_effect = write_error
    original = asyncio.create_task(save_prepared_read_business(world.runtime, 2, 'finish', pending))
    await world.entered.wait()
    resumed = asyncio.create_task(retry_read_business(world.runtime))
    receipts = await _join_after_release(world, original, resumed)
    assert receipts == [error, error]
    assert world.database.write.await_count == 1
    assert applied == (['actual'] if kind is DbOutcomeKind.COMPLETED else [])
    with pytest.raises(RuntimeError) as late:
        await save_prepared_read_business(world.runtime, 2, 'finish', pending)
    assert late.value is error and world.database.write.await_count == 1
    if kind is DbOutcomeKind.COMPLETED:
        assert not world.runtime.pending_read_business
    else:
        assert world.runtime.pending_read_business[(2, 'finish')] is pending
        world.database.write.side_effect = write
        world.outputs.finish_obtain.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'saved')
        await retry_read_business(world.runtime)
        assert applied == ['saved'] and not world.runtime.pending_read_business
        assert world.outputs.finish_obtain.call_args.args[:2] == (pending.request, pending.key)


async def test_internal_result_and_main_resume_share_complete_business_phase(world):
    pending = _pending_result(world)
    original = asyncio.create_task(media_flow._save_internal_read_and_settle(world.runtime, pending))
    await world.entered.wait()
    resumed = asyncio.create_task(media_flow.resume_prepared_internal_reads(world.database,
        pending_read_results=world.runtime.pending_read_results,
        pending_read_business=world.runtime.pending_read_business,
        pending_read_ends=world.runtime.pending_read_ends,
        continuing_read_tickets=world.runtime.continuing_read_tickets))
    receipts = await _join_after_release(world, original, resumed)
    assert receipts == [None, None]
    world.operations.finish_attempt.assert_called_once()
    world.outputs.release_read_slot.assert_called_once()
    assert world.database.write.await_count == 2
    assert not world.runtime.pending_read_results
    assert not world.runtime.pending_read_business and not world.runtime.continuing_read_tickets
    # 原拥有者晚到时只观察已完成阶段，不重交结果或另建释放申请。
    await media_flow._save_internal_read_and_settle(world.runtime, pending)
    assert world.database.write.await_count == 2


async def test_internal_complete_phase_applies_before_repeated_waiter_cancellation(world):
    pending = _pending_result(world)
    original = asyncio.create_task(media_flow._save_internal_read_and_settle(world.runtime, pending))
    await world.entered.wait()
    resumed = asyncio.create_task(media_flow._save_internal_read_and_settle(world.runtime, pending))
    original.cancel()
    await asyncio.sleep(0)
    original.cancel()
    receipts = await _join_after_release(world, original, resumed)
    assert isinstance(receipts[0], asyncio.CancelledError) and receipts[1] is None
    world.operations.finish_attempt.assert_called_once()
    world.outputs.release_read_slot.assert_called_once()
    assert not world.runtime.pending_read_results and not world.runtime.pending_read_business
    assert not world.runtime.continuing_read_tickets


@pytest.mark.parametrize('canceled', [False, True])
async def test_stopped_observation_and_main_resume_share_original_preparation(world, canceled):
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.devices.read_session import ReadEnd

    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=2,
        read_end=ReadEnd(True, 10, 'stopped'), stop_requested=True)
    pending = stopped_read_result(world.runtime, ticket, step, _LOCAL_READ_EVIDENCE, 123)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []

    async def read(callback, **kwargs):
        reads.append(callback)
        if len(reads) == 1:
            entered.set()
            await release.wait()
            return int(canceled), 20
        return None, None

    world.database.read.side_effect = read
    original = asyncio.create_task(save_read_result(world.runtime, pending))
    await entered.wait()
    resumed = asyncio.create_task(media_flow.resume_prepared_internal_reads(world.database,
        pending_read_results=world.runtime.pending_read_results,
        pending_read_business=world.runtime.pending_read_business,
        pending_read_ends=world.runtime.pending_read_ends,
        continuing_read_tickets=world.runtime.continuing_read_tickets))
    await asyncio.sleep(0)
    release.set()
    receipts = await _join_after_release(world, original, resumed)
    assert receipts[1] is None and not world.runtime.pending_read_results
    if canceled:
        assert receipts[0].key == pending.key and receipts[0].finish.occurred_at == 123
        assert world.operations.finish_attempt.call_count == 1
        assert len(reads) == 2 and not world.runtime.continuing_read_tickets
    else:
        assert receipts[0] is None and world.operations.finish_attempt.call_count == 0
        assert len(reads) == 1 and world.runtime.continuing_read_tickets[2].result_key == pending.key
    late = await save_read_result(world.runtime, pending)
    assert late is receipts[0]


@pytest.mark.parametrize('cancel', [False, True])
async def test_result_save_only_shares_original_callback_and_handle(world, cancel):
    pending = _pending_result(world)
    original = asyncio.create_task(save_read_result(world.runtime, pending))
    await world.entered.wait()
    resumed = asyncio.create_task(save_read_result(world.runtime, pending))
    if cancel:
        original.cancel()
    receipts = await _join_after_release(world, original, resumed)
    if cancel:
        assert isinstance(receipts[0], asyncio.CancelledError)
    else:
        assert receipts[0] is pending
    assert receipts[1] is pending
    assert world.operations.finish_attempt.call_count == 1
    assert world.database.write.await_count == 1
    assert world.runtime.pending_read_results[(5, 1)] is pending


@pytest.mark.parametrize('kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_internal_recovery_reuses_original_result_key_after_failure(world, kind):
    pending = _pending_result(world)
    world.release.set()
    world.operations.finish_attempt.return_value = DbOutcome(kind)
    with pytest.raises(ConsistencyError):
        await media_flow._save_internal_read_and_settle(world.runtime, pending)
    with pytest.raises(ConsistencyError):
        await media_flow._save_internal_read_and_settle(world.runtime, pending)
    assert world.operations.finish_attempt.call_count == 1
    world.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    await media_flow.resume_prepared_internal_reads(world.database,
        pending_read_results=world.runtime.pending_read_results,
        pending_read_business=world.runtime.pending_read_business,
        pending_read_ends=world.runtime.pending_read_ends,
        continuing_read_tickets=world.runtime.continuing_read_tickets)
    assert all(call.args[:2] == (pending.finish, pending.key)
        for call in world.operations.finish_attempt.call_args_list)
    assert world.operations.finish_attempt.call_count == 2
    world.outputs.release_read_slot.assert_called_once()
    assert not world.runtime.pending_read_results and not world.runtime.pending_read_business


@pytest.mark.parametrize('kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_stopped_result_failure_keeps_prepared_phase_and_original_input(world, kind):
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.devices.read_session import ReadEnd

    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    pending = stopped_read_result(world.runtime, ticket,
        InputStep(InputPhase.SEGMENT_FAILED, copy_id=2,
            read_end=ReadEnd(True, 10, 'stopped'), stop_requested=True),
        _LOCAL_READ_EVIDENCE, 123)
    world.database.read.side_effect = [(1, 20), (None, None)]
    world.operations.finish_attempt.return_value = DbOutcome(kind)
    world.release.set()
    with pytest.raises(ConsistencyError):
        await media_flow._save_internal_read_and_settle(world.runtime, pending)
    prepared = world.runtime.pending_read_results[(5, 1)]
    world.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    await media_flow.resume_prepared_internal_reads(world.database,
        pending_read_results=world.runtime.pending_read_results,
        pending_read_business=world.runtime.pending_read_business,
        pending_read_ends=world.runtime.pending_read_ends,
        continuing_read_tickets=world.runtime.continuing_read_tickets)
    assert world.database.read.await_count == 2
    assert all(call.args[:2] == (prepared.finish, pending.key)
        for call in world.operations.finish_attempt.call_args_list)
    assert not world.runtime.pending_read_results


@pytest.mark.parametrize('kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_internal_business_failure_retries_only_original_release_phase(world, kind):
    pending = _pending_result(world)
    world.outputs.release_read_slot.return_value = DbOutcome(kind)
    world.release.set()
    with pytest.raises(ConsistencyError):
        await media_flow._save_internal_read_and_settle(world.runtime, pending)
    release = world.runtime.pending_read_business[(2, 'release_read_slot')]
    assert not world.runtime.pending_read_results
    world.outputs.release_read_slot.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    await media_flow.resume_prepared_internal_reads(world.database,
        pending_read_results=world.runtime.pending_read_results,
        pending_read_business=world.runtime.pending_read_business,
        pending_read_ends=world.runtime.pending_read_ends,
        continuing_read_tickets=world.runtime.continuing_read_tickets)
    world.operations.finish_attempt.assert_called_once()
    assert world.outputs.release_read_slot.call_count == 2
    assert all(call.args[:2] == (release.request, release.key)
        for call in world.outputs.release_read_slot.call_args_list)
    assert not world.runtime.pending_read_business


async def test_old_result_forget_cannot_remove_new_owner_or_continuation(world):
    original = _pending_result(world)
    forget_read_result(world.runtime, original)
    replacement = _pending_result(world)
    forget_read_result(world.runtime, original)
    assert world.runtime.pending_read_results[(5, 1)] is replacement
    assert world.runtime.continuing_read_tickets[2].ticket == replacement.finish.ticket
