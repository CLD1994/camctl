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


@pytest.mark.parametrize('failed_stage', ['check', 'repair_decision', 'repair_result', 'slot'])
@pytest.mark.parametrize('control_kind', ['none', 'error', 'cancel'])
async def test_exhausted_internal_read_recovers_original_business_stages(world, monkeypatch, failed_stage, control_kind):
    from camctl.capture import processing
    from camctl.operations.models import ErrorValue

    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'read', {'reason': 'device read failed'}),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    pending = hold_read_result(world.runtime, AttemptFinish(ticket,
        validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE), 123,
        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue('read_attempts_exhausted', 'source_read'))))
    world.runtime.occurred_at = lambda: 123
    status = SimpleNamespace(check_decision=3, check_state=1,
        repair_state=1 if failed_stage == 'repair_decision' else 3 if failed_stage == 'repair_result' else 2,
        target_duration_ms=1000)
    world.database.read.return_value = status
    slot_held = [failed_stage == 'slot']
    async def read_one(database, sql, parameters):
        if sql.startswith('SELECT processing_id,inspection_id'):
            return (10, None)
        if sql.startswith('SELECT c.processing_id,r.attempts_used'):
            return (10, 1, 1, 4)
        if sql.startswith('SELECT 1 FROM operation_attempts'):
            return None
        if sql.startswith('SELECT error_json'):
            return ('{"code":"read_attempts_exhausted","stage":"source_read","details":{}}',)
        if sql.startswith('SELECT slot_device_id'):
            return ('cam-1' if slot_held[0] else None,)
        raise AssertionError(sql)
    monkeypatch.setattr(media_flow, '_read_one', read_one)
    calls = []
    failed = [False]
    control = (None if control_kind == 'none' else RuntimeError('committed check')
               if control_kind == 'error' else asyncio.CancelledError('committed check'))
    def save(stage, request, key):
        calls.append((stage, request, key))
        if stage == failed_stage and not failed[0]:
            failed[0] = True
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        if stage == 'check':
            status.check_state = 3
            if control is not None:
                _attach_db_outcome(control, DbOutcome(DbOutcomeKind.COMPLETED))
                raise control
        elif stage.startswith('repair'):
            status.repair_state = 5
        elif stage == 'slot':
            slot_held[0] = False
        return DbOutcome(DbOutcomeKind.COMPLETED)
    def save_check_result(request, key, owned):
        assert isinstance(request, processing.CheckResultSave)
        return save('check', request, key)
    def save_repair_decision(request, key, owned):
        return save('repair_decision', request, key)
    def save_repair_result(request, key, owned):
        return save('repair_result', request, key)
    def release_read_slot(request, key, owned):
        return save('slot', request, key)
    monkeypatch.setattr(media_flow, 'CaptureRepository', lambda: SimpleNamespace(
        save_check_result=save_check_result, save_repair_decision=save_repair_decision,
        save_repair_result=save_repair_result))
    world.outputs.release_read_slot.side_effect = release_read_slot
    world.release.set()
    with pytest.raises(ConsistencyError):
        await media_flow._save_internal_read_and_settle(world.runtime, pending)
    original = next(item for item in calls if item[0] == failed_stage)
    async def recover():
        await media_flow.resume_prepared_internal_reads(world.database,
            pending_read_results=world.runtime.pending_read_results,
            pending_read_business=world.runtime.pending_read_business,
            pending_read_ends=world.runtime.pending_read_ends,
            continuing_read_tickets=world.runtime.continuing_read_tickets)
    if control is None:
        await recover()
    else:
        with pytest.raises(type(control)) as raised:
            await recover()
        assert raised.value is control
    await recover()
    retried = [item for item in calls if item[0] == failed_stage]
    assert len(retried) == 2 and retried[0][1] is retried[1][1] and retried[1][2] == original[2]
    assert [stage for stage, _, _ in calls].count('check') == (2 if failed_stage == 'check' else 1)
    assert not world.runtime.pending_read_business and not world.runtime.pending_read_results
    world.operations.finish_attempt.assert_called_once()


async def test_public_obtain_recovery_reuses_completed_release_before_result_settlement(world):
    from camctl.outputs import obtain_flow
    from camctl.operations.attempts import RetryWaitGate

    class RoundReached(Exception):
        pass
    async def begin_round(database, now):
        raise RoundReached
    world.runtime.outputs = world.outputs
    world.runtime.occurred_at = lambda: 123
    world.runtime.retry_gate = RetryWaitGate()
    world.runtime.discovery = SimpleNamespace(begin_round=begin_round)
    facts = {'id': 2, 'delivery_status': 1, 'source_device_file_id': 7}
    world.database.read.return_value = facts
    world.outputs.release_read_slot.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN), DbOutcome(DbOutcomeKind.COMPLETED)]
    pending = _pending_result(world)
    world.release.set()
    with pytest.raises(ConsistencyError):
        await obtain_flow._save_read_result_and_settle(world.runtime, pending, facts)
    original = world.runtime.pending_read_business[(2, 'release_read_slot')]
    with pytest.raises(RoundReached):
        await obtain_flow.advance_obtain(world.runtime)
    assert world.outputs.release_read_slot.call_count == 2
    assert all(call.args[:2] == (original.request, original.key)
        for call in world.outputs.release_read_slot.call_args_list)
    assert not world.runtime.pending_read_results and not world.runtime.pending_read_business
    world.operations.finish_attempt.assert_called_once()


@pytest.mark.parametrize('stage', ['release', 'read_attempts_exhausted', 'checksum_mismatch', 'source_file_unconfirmed', 'binding'])
@pytest.mark.parametrize('initial_kind', [DbOutcomeKind.COMPLETED, DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED])
@pytest.mark.parametrize('control_kind', ['error', 'cancel'])
async def test_obtain_complete_business_applies_tail_before_original_control(
        world, stage, initial_kind, control_kind):
    from camctl.outputs import obtain_flow
    from camctl.operations.attempts import RetryWaitGate
    from camctl.operations.models import ErrorValue
    from camctl.contracts.values import new_operation_key
    from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding
    from camctl.persistence.repositories.outputs import FailReadBinding

    class RoundReached(Exception):
        pass
    async def begin_round(database, now):
        raise RoundReached
    world.runtime.outputs = world.outputs
    world.runtime.occurred_at = lambda: 123
    world.runtime.retry_gate = RetryWaitGate()
    world.runtime.retry_gate.established('read/2', 1)
    world.runtime.discovery = SimpleNamespace(begin_round=begin_round)
    facts = {'id': 2, 'delivery_id': 3, 'delivery_status': 1, 'source_device_file_id': 7}
    world.database.read.return_value = facts
    control = RuntimeError('committed business') if control_kind == 'error' else asyncio.CancelledError('committed business')
    actual = DbOutcome(DbOutcomeKind.COMPLETED)
    _attach_db_outcome(control, actual)
    method = (world.outputs.release_read_slot if stage == 'release' else
              world.outputs.fail_read_binding if stage == 'binding' else world.outputs.fail_read_delivery)
    method.side_effect = ([control] if initial_kind is DbOutcomeKind.COMPLETED
                          else [DbOutcome(initial_kind), control])
    ticket = AttemptTicket(1, 'read', '2', 'read/2', 5)
    failed = stage not in ('release', 'binding')
    outcome = CallOutcome(status=AttemptStatus.FAILED if failed else AttemptStatus.SUCCEEDED,
        effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'read', {'reason': 'device read failed'}) if failed else None,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('read_returned', 1, {})))
    finish = AttemptFinish(ticket, validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE), 123,
        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue(stage, 'source_read'))
        if failed else RunFinish(RunOutcome.SUCCEEDED))
    business = None
    if stage == 'binding':
        request = FailReadBinding(2, 7, BindingResult(BindingStatus.DEVICE_MISSING,
            DeviceBinding('cam-1', 'camera-driver'), None), 123)
        business = PendingReadBusiness(method, request, new_operation_key(),
            lambda _: world.runtime.retry_gate.cleared('read/2'))
    pending = hold_read_result(world.runtime, finish, business=business)
    world.release.set()
    if initial_kind is DbOutcomeKind.COMPLETED:
        with pytest.raises(type(control)) as raised:
            await obtain_flow._save_read_result_and_settle(world.runtime, pending, facts)
    else:
        with pytest.raises(ConsistencyError):
            await obtain_flow._save_read_result_and_settle(world.runtime, pending, facts)
        with pytest.raises(type(control)) as raised:
            await obtain_flow.advance_obtain(world.runtime)
    assert raised.value is control
    assert not world.runtime.pending_read_business and not world.runtime.pending_read_results
    assert 'read/2' not in world.runtime.retry_gate.anchors
    with pytest.raises(RoundReached):
        await obtain_flow.advance_obtain(world.runtime)
    assert method.call_count == (1 if initial_kind is DbOutcomeKind.COMPLETED else 2)
    if method.call_count == 2:
        assert method.call_args_list[0].args[:2] == method.call_args_list[1].args[:2]
    world.operations.finish_attempt.assert_called_once()


async def test_obtain_binding_producer_keeps_gate_application_with_original_business(world, monkeypatch):
    from camctl.outputs import obtain_flow, read_attempts
    from camctl.outputs.read_discovery import ReadCandidate
    from camctl.contracts.pages import Page
    from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding
    from camctl.operations.attempts import RetryWaitGate

    pending = _pending_result(world)
    world.runtime.pending_read_results.clear()
    world.runtime.outputs = world.outputs
    world.runtime.retry_gate = RetryWaitGate()
    world.runtime.retry_gate.established('read/2', 1)
    world.runtime.binding_check = lambda binding: BindingResult(BindingStatus.DEVICE_MISSING, binding, None)
    candidate = ReadCandidate(2, 3, 1, 4, 0, 'cam-1', 'camera-driver', 7, True)
    async def reads(database, scope, now):
        return Page((candidate,), None)
    world.runtime.discovery = SimpleNamespace(reads=reads)
    async def held_binding(runtime, copy_id, binding_result, business):
        assert binding_result.binding == DeviceBinding(candidate.device_id, candidate.driver_id)
        return hold_read_result(runtime, pending.finish, business=business)
    monkeypatch.setattr(read_attempts, 'held_binding_read_result', held_binding)
    control = RuntimeError('committed binding result')
    _attach_db_outcome(control, DbOutcome(DbOutcomeKind.COMPLETED))
    world.outputs.fail_read_binding.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN), control]
    world.release.set()
    with pytest.raises(ConsistencyError):
        await obtain_flow._advance_reads(world.runtime, 123)
    assert not world.runtime.pending_read_results and world.runtime.pending_read_business
    with pytest.raises(RuntimeError) as raised:
        await obtain_flow.advance_obtain(world.runtime)
    assert raised.value is control
    assert not world.runtime.pending_read_business
    assert 'read/2' not in world.runtime.retry_gate.anchors
