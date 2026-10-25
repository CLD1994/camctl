"""原保存与恢复共同等待，可靠结果及内存交接只应用一次。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import baseline, result_pages, segment_completion, result_scans
from camctl.capture.baseline_models import BaselineChunkResult, BaselineChunkSave
from camctl.capture.baseline_models import BaselineReadCallView, BaselineReadReturn, BaselineReadState
from camctl.capture.baseline_reads import PendingBaselineRead, resume_baseline_read_save
from camctl.capture.result_inputs import page_from_outcome
from camctl.capture.models import ActivityReleaseSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.pages import Page
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead
from camctl.devices.file_identity import FileIdentity
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.capture import ActivityReleaseResult, CaptureRepository, ReleaseOutcome
from .test_result_page_saves import _request
from .test_baseline_read_models import _intent

pytestmark = pytest.mark.asyncio


def _gate(value):
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, value)

    database.write.side_effect = write
    return database, repository, entered, release


async def _join(first, entered, second_factory, release):
    await entered.wait()
    second = asyncio.create_task(second_factory())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    return await asyncio.gather(first, second, return_exceptions=True)


def _baseline_world():
    binding = DeviceBinding('cam-1', 'dji-action6')
    pending = baseline.PendingBaseline(1, binding, ('/DCIM',))
    pending.saves.begin(BaselineChunkSave(1, 1, (
        FileIdentity(binding, '/DCIM/a.mp4'),), 1))
    database, repository, entered, release = _gate(BaselineChunkResult(1, 1, 8))
    runtime = SimpleNamespace(database=database, capture=repository, pending_baselines={7: pending})
    return runtime, pending, entered, release


async def test_baseline_background_and_resume_apply_chunk_once():
    runtime, pending, entered, release = _baseline_world()
    first = asyncio.create_task(baseline.resume_baseline_save(7, runtime=runtime))
    results = await _join(first, entered,
        lambda: baseline.resume_baseline_save(7, runtime=runtime), release)
    assert results == [None, None]
    assert (pending.chunks, pending.entries) == (1, 1)
    assert pending.saves.pending is None
    assert runtime.database.write.await_count == 1


async def test_result_page_background_and_resume_share_response():
    request = _request()
    response = result_pages.ResultPageRef(request.ticket, 1, 10,
        page_from_outcome(request.ticket, request.outcome.outcome).next_cursor)
    database, repository, entered, release = _gate(response)
    owner = result_pages.ResultPageSaveOwner()
    owner.begin(request)
    first = asyncio.create_task(owner.save(repository, database))
    results = await _join(first, entered, lambda: owner.save(repository, database), release)
    assert all(result.value == response for result in results)
    assert database.write.await_count == 1
    assert (await owner.save(repository, database)).value == response
    assert database.write.await_count == 1
    assert owner.take() == response


async def test_segment_background_and_resume_delete_pending_once():
    database, repository, entered, release = _gate('original')
    runtime = SimpleNamespace(database=database, pending_segment_writes={})
    request = object()
    first = asyncio.create_task(segment_completion._save(
        runtime, 7, repository, 'finish_segment_recording', request))
    results = await _join(first, entered, lambda: segment_completion.resume_segment_writes(
        database, runtime.pending_segment_writes, 7), release)
    assert results == ['original', None]
    assert not runtime.pending_segment_writes
    assert database.write.await_count == 1


async def test_baseline_release_is_one_shared_phase():
    runtime, pending, entered, release = _baseline_world()
    pending.saves.pending = None
    pending.release = (ActivityReleaseSave(7, 1), new_operation_key())
    runtime.database.write.side_effect = None
    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))
    runtime.database.write.side_effect = write
    first = asyncio.create_task(baseline.resume_baseline_save(7, runtime=runtime))
    results = await _join(first, entered,
        lambda: baseline.resume_baseline_save(7, runtime=runtime), release)
    assert [result.phase for result in results] == [baseline.PreparationPhase.READY] * 2
    assert not runtime.pending_baselines
    assert runtime.database.write.await_count == 1


def _owner_world(kind):
    if kind == 'baseline':
        runtime, pending, entered, release = _baseline_world()
        return pending.saves, runtime.database, runtime.capture, BaselineChunkResult(1, 1, 8), entered, release
    request = _request()
    response = result_pages.ResultPageRef(request.ticket, 1, 10,
        page_from_outcome(request.ticket, request.outcome.outcome).next_cursor)
    database, repository, entered, release = _gate(response)
    owner = result_pages.ResultPageSaveOwner()
    owner.begin(request)
    return owner, database, repository, response, entered, release


@pytest.mark.parametrize('kind', ['baseline', 'page'])
@pytest.mark.parametrize('outcome_kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_incomplete_owner_save_is_sticky_until_explicit_original_key_recovery(kind, outcome_kind):
    owner, database, repository, response, _, _ = _owner_world(kind)
    original = owner.pending
    database.write.side_effect = [DbOutcome(outcome_kind), DbOutcome(DbOutcomeKind.COMPLETED, response)]
    assert (await owner.save(repository, database)).kind is outcome_kind
    assert (await owner.save(repository, database)).kind is outcome_kind
    assert database.write.await_count == 1 and owner.pending is original
    assert (await owner.resume_save(repository, database)).value == response
    assert owner.take() == response
    assert [call.kwargs['key'] for call in database.write.await_args_list] == [original.key] * 2


@pytest.mark.parametrize('kind', ['baseline', 'page'])
async def test_save_failure_is_shared_and_explicit_recovery_preserves_request(kind):
    owner, database, repository, response, entered, release = _owner_world(kind)
    original = owner.pending
    failure = RuntimeError('save failed')
    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        raise failure
    database.write.side_effect = write
    first = asyncio.create_task(owner.save(repository, database))
    results = await _join(first, entered, lambda: owner.save(repository, database), release)
    assert results == [failure, failure] and owner.pending is original
    with pytest.raises(RuntimeError) as raised:
        await owner.save(repository, database)
    assert raised.value is failure and database.write.await_count == 1
    database.write.side_effect = None
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    assert (await owner.resume_save(repository, database)).value == response
    assert owner.pending.request is original.request
    assert [call.kwargs['key'] for call in database.write.await_args_list] == [original.key] * 2


@pytest.mark.parametrize('kind', ['baseline', 'page'])
@pytest.mark.parametrize('cancel_count', [1, 2])
async def test_cancel_waiter_waits_for_response_and_other_waiter(kind, cancel_count):
    owner, database, repository, response, entered, release = _owner_world(kind)
    first = asyncio.create_task(owner.save(repository, database))
    await entered.wait()
    second = asyncio.create_task(owner.save(repository, database))
    for _ in range(cancel_count):
        first.cancel('original waiter')
        await asyncio.sleep(0)
    assert not first.done() and owner.pending.response is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).value == response and owner.pending.response == response
    assert database.write.await_count == 1


@pytest.mark.parametrize('kind', ['baseline', 'page'])
async def test_next_request_has_its_own_save_phase(kind):
    owner, database, repository, response, _, release = _owner_world(kind)
    release.set()
    first = owner.pending
    await owner.save(repository, database)
    owner.take()
    request = replace(first.request, **({'chunk_no': 2} if kind == 'baseline' else {'page_no': 2}))
    later = owner.begin(request)
    next_response = replace(response, **({'chunk_no': 2} if kind == 'baseline' else {'page_no': 2}))
    database.write.side_effect = None
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, next_response)
    assert (await owner.save(repository, database)).value == next_response
    assert owner.take() == next_response and first.key != later.key
    assert database.write.await_count == 2


async def test_segment_unknown_shared_attempt_recovers_original_request_once():
    database, repository, entered, release = _gate(None)
    runtime = SimpleNamespace(database=database, pending_segment_writes={})
    failure = RuntimeError('unknown commit')
    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.UNKNOWN, error=failure)
    database.write.side_effect = write
    request = object()
    first = asyncio.create_task(segment_completion._save(runtime, 7, repository, 'finish_segment_recording', request))
    results = await _join(first, entered, lambda: segment_completion.resume_segment_writes(
        database, runtime.pending_segment_writes), release)
    assert all(isinstance(result, ConsistencyError) for result in results)
    assert results[0] is results[1] and database.write.await_count == 1
    original = runtime.pending_segment_writes[7]
    database.write.side_effect = None
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'original')
    await segment_completion.resume_segment_writes(database, runtime.pending_segment_writes)
    await segment_completion.resume_segment_writes(database, runtime.pending_segment_writes)
    assert not runtime.pending_segment_writes
    assert original.args == (request,)
    assert [call.kwargs['key'] for call in database.write.await_args_list] == [original.key] * 2


async def test_segment_late_previous_waiter_cannot_delete_next_phase():
    database, repository, entered, release = _gate('first')
    runtime = SimpleNamespace(database=database, pending_segment_writes={})
    next_entered, next_release = asyncio.Event(), asyncio.Event()
    keys = []
    async def write(callback, *, key, description):
        keys.append(key)
        if len(keys) == 1:
            entered.set()
            await release.wait()
            return DbOutcome(DbOutcomeKind.COMPLETED, 'first')
        next_entered.set()
        await next_release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, 'second')
    database.write.side_effect = write
    async def producer():
        first = await segment_completion._save(runtime, 7, repository, 'finish_segment_recording', 'one')
        second = await segment_completion._save(runtime, 7, repository, 'finish_segment_recording', 'two')
        return first, second
    active = asyncio.create_task(producer())
    await entered.wait()
    recovered = asyncio.create_task(segment_completion.resume_segment_writes(
        database, runtime.pending_segment_writes))
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await next_entered.wait()
    await recovered
    assert runtime.pending_segment_writes[7].args == ('two',)
    next_release.set()
    assert await active == ('first', 'second')
    assert len(set(keys)) == 2 and not runtime.pending_segment_writes


@pytest.mark.parametrize('cancel_count', [0, 1, 2])
async def test_baseline_complete_application_survives_waiter_cancel(cancel_count):
    runtime, pending, entered, release = _baseline_world()
    first = asyncio.create_task(baseline.resume_baseline_save(7, runtime=runtime))
    await entered.wait()
    second = asyncio.create_task(baseline.resume_baseline_save(7, runtime=runtime))
    for _ in range(cancel_count):
        first.cancel('baseline owner')
        await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert results[1] is None
    if cancel_count:
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results[0] is None
    assert (pending.chunks, pending.entries) == (1, 1)
    assert pending.saves.pending is None and runtime.database.write.await_count == 1


async def test_baseline_read_intent_and_return_have_distinct_complete_application_phases():
    intent = _intent()
    response = BaselineReadCallView(7, 11, BaselineReadState.INTENDED, intent,
        None, None, None, None, None, HistoryBoundary(1, 11))
    database, repository, entered, release = _gate(response)
    owner = PendingBaselineRead(intent)
    owner.saves.begin(intent)
    first = asyncio.create_task(resume_baseline_read_save(owner, capture=repository, database=database))
    results = await _join(first, entered, lambda: resume_baseline_read_save(
        owner, capture=repository, database=database), release)
    assert results == [None, None]
    assert owner.intent_view is response and owner.saves.pending is None
    assert database.write.await_count == 1

    returned = BaselineReadReturn(7, 3, None, DirectoryRead(None, None, Page((), None)))
    owner.saves.begin(returned, key=intent.result_save_key)
    completed = replace(response, state=BaselineReadState.RETURNED, result_event_id=12,
                        returned_at=3, boundary=HistoryBoundary(2, 12))
    database.write.side_effect = None
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, completed)
    assert await resume_baseline_read_save(owner, capture=repository, database=database) is completed
    assert await resume_baseline_read_save(owner, capture=repository, database=database) is completed
    assert owner.result is completed and owner.saves.pending is None
    assert [call.kwargs['key'] for call in database.write.await_args_list] == [intent.call_key, intent.result_save_key]


@pytest.mark.parametrize('cancel_count', [0, 1, 2])
async def test_page_application_and_resume_do_not_repeat_qualification_or_take(cancel_count):
    owner, database, repository, response, entered, release = _owner_world('page')
    pending = result_scans.PendingResultScan(response.ticket, None, Decimal('3'), saves=owner)
    runtime = SimpleNamespace(database=database, capture=repository)
    database.read.return_value = None
    qualified, finish_qualification = asyncio.Event(), asyncio.Event()
    checked = []
    async def page_qualified(saved):
        checked.append(saved.ref)
        qualified.set()
        await finish_qualification.wait()
        return True
    first = asyncio.create_task(result_scans._accept_saved_page(pending, runtime, page_qualified))
    await entered.wait()
    release.set()
    await qualified.wait()
    second = asyncio.create_task(result_scans._accept_saved_page(pending, runtime, page_qualified))
    await result_scans.resume_result_page_saves(database, pending_scans={7: pending}, repository=repository)
    for _ in range(cancel_count):
        first.cancel('page owner')
        await asyncio.sleep(0)
    finish_qualification.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if cancel_count:
        assert isinstance(results[0], asyncio.CancelledError)
    else:
        assert results[0] is False
    assert results[1] is False
    assert checked == [response] and database.write.await_count == 1
    assert owner.pending is None and pending.last_ref is response and pending.page_no == 2


@pytest.mark.parametrize('outcome_kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_cancel_of_incomplete_page_save_propagates_after_retaining_original(outcome_kind):
    owner, database, repository, _, entered, release = _owner_world('page')
    original = owner.pending
    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        return DbOutcome(outcome_kind)
    database.write.side_effect = write
    task = asyncio.create_task(owner.save(repository, database))
    await entered.wait()
    task.cancel('incomplete owner')
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.pending is original and database.write.await_count == 1


@pytest.mark.parametrize('release_phase', [False, True])
async def test_baseline_committed_notification_error_is_shared_after_complete_application(release_phase):
    runtime, pending, entered, release = _baseline_world()
    response = BaselineChunkResult(1, 1, 8)
    if release_phase:
        pending.saves.pending = None
        pending.release = (ActivityReleaseSave(7, 1), new_operation_key())
        response = ActivityReleaseResult(ReleaseOutcome.RELEASED)
    failure = RuntimeError('committed notification')
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, response))
    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        raise failure
    runtime.database.write.side_effect = write
    first = asyncio.create_task(baseline.resume_baseline_save(7, runtime=runtime))
    results = await _join(first, entered,
        lambda: baseline.resume_baseline_save(7, runtime=runtime), release)
    assert results == [failure, failure] and runtime.database.write.await_count == 1
    assert pending.saves.pending is None
    if release_phase:
        assert not runtime.pending_baselines
    else:
        assert (pending.chunks, pending.entries) == (1, 1)


@pytest.mark.parametrize('cancel_count', [1, 2])
async def test_segment_cancel_and_recovery_deliver_one_applied_save(cancel_count):
    database, repository, entered, release = _gate('saved')
    runtime = SimpleNamespace(database=database, pending_segment_writes={})
    first = asyncio.create_task(segment_completion._save(runtime, 7, repository, 'finish_segment_recording', object()))
    await entered.wait()
    second = asyncio.create_task(segment_completion.resume_segment_writes(database, runtime.pending_segment_writes))
    for _ in range(cancel_count):
        first.cancel('segment owner')
        await asyncio.sleep(0)
    assert not first.done() and runtime.pending_segment_writes
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert not runtime.pending_segment_writes and database.write.await_count == 1


@pytest.mark.parametrize('outcome_kind', [DbOutcomeKind.UNKNOWN, DbOutcomeKind.NOT_EXECUTED])
async def test_baseline_release_incomplete_result_keeps_same_request_and_key(outcome_kind):
    runtime, pending, _, _ = _baseline_world()
    pending.saves.pending = None
    original = pending.release = (ActivityReleaseSave(7, 1), new_operation_key())
    runtime.database.write.side_effect = [DbOutcome(outcome_kind), DbOutcome(
        DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))]
    assert (await baseline.resume_baseline_save(7, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    assert pending.release is original and not pending.completed
    assert (await baseline.resume_baseline_save(7, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert not runtime.pending_baselines
    assert [call.kwargs['key'] for call in runtime.database.write.await_args_list] == [original[1]] * 2


async def test_actual_cancel_with_committed_baseline_receipt_applies_before_propagation():
    runtime, pending, _, _ = _baseline_world()
    control = asyncio.CancelledError('committed actual control')
    _attach_db_outcome(control, DbOutcome(DbOutcomeKind.COMPLETED, BaselineChunkResult(1, 1, 8)))
    runtime.database.write.side_effect = control
    with pytest.raises(asyncio.CancelledError):
        await baseline.resume_baseline_save(7, runtime=runtime)
    assert pending.saves.pending is None
    assert (pending.chunks, pending.entries) == (1, 1)


@pytest.mark.parametrize('change', ['add', 'remove'])
async def test_result_page_recovery_snapshots_scans_across_original_save(change):
    request = _request()
    response = result_pages.ResultPageRef(request.ticket, 1, 10,
        page_from_outcome(request.ticket, request.outcome.outcome).next_cursor)
    database, repository, entered, release = _gate(response)
    original = result_scans.PendingResultScan(request.ticket, None, Decimal('3'))
    held = original.saves.begin(request)
    scans = {(request.ticket.run_id, request.ticket.attempt_id): original}
    recovery = asyncio.create_task(result_scans.resume_result_page_saves(
        database, pending_scans=scans, repository=repository))
    await entered.wait()
    next_ticket = replace(request.ticket, run_id=request.ticket.run_id + 1)
    later = result_scans.PendingResultScan(next_ticket, None, Decimal('3'))
    later.saves.begin(replace(request, ticket=next_ticket,
        outcome=replace(request.outcome, ticket=next_ticket)))
    if change == 'add':
        scans[(next_ticket.run_id, next_ticket.attempt_id)] = later
    else:
        scans.pop((request.ticket.run_id, request.ticket.attempt_id))
    release.set()
    await recovery
    assert database.write.await_count == 1
    assert original.saves.pending.response == response
    assert original.saves.pending.save is held.save
    assert (await original.saves.save(repository, database)).value == response
    assert database.write.await_count == 1
    assert later.saves.pending.response is None
