"""分段保存的真实回执与主协程原申请交接。"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import segment_completion
from camctl.contracts.values import new_operation_key
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.capture.segment_finish import FinishSegmentRecording, SegmentFinishResult
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.history_values import HistoryBoundary

pytestmark = pytest.mark.asyncio


def runtime():
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata('camctl', 'a'*32, 5, '/staging', '/ready', '/processing'))
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    return SimpleNamespace(database=database, pending_segment_writes={}), owned, repository


async def test_segment_save_applies_result_before_original_cancel_is_delivered():
    state, owned, repository = runtime()
    entered, release = asyncio.Event(), asyncio.Event()
    request = object()
    async def write(callback, *, key, **kwargs):
        assert state.pending_segment_writes[7].key == key
        entered.set()
        await release.wait()
        return callback(owned)
    state.database.write.side_effect = write
    repository.finish_segment_recording.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'saved')
    task = asyncio.create_task(segment_completion._save(
        state, 7, repository, 'finish_segment_recording', request))
    await entered.wait()
    task.cancel('save waiter')
    await asyncio.sleep(0)
    assert not task.done() and 7 in state.pending_segment_writes
    release.set()
    with pytest.raises(asyncio.CancelledError, match='save waiter'):
        await task
    assert not state.pending_segment_writes
    repository.finish_segment_recording.assert_called_once()


async def test_unknown_segment_save_reuses_original_key_and_request_on_new_owned():
    state, old, repository = runtime()
    fresh = OwnedConnection(create_autospec(sqlite3.Connection, instance=True), old.metadata)
    request = object()
    key = new_operation_key()
    pending = segment_completion.PendingSegmentWrite(repository, 'finish_segment_recording', (request,), key)
    state.pending_segment_writes[7] = pending
    async def write(callback, *, key, **kwargs):
        return callback(fresh)
    state.database.write.side_effect = write
    repository.finish_segment_recording.return_value = DbOutcome(DbOutcomeKind.COMPLETED, 'saved')
    await segment_completion.resume_segment_writes(state.database, state.pending_segment_writes, 7)
    repository.finish_segment_recording.assert_called_once_with(request, key, fresh)
    old.connection.rollback.assert_not_called()
    assert not state.pending_segment_writes


async def test_member_batches_are_materialized_before_leaving_database_callback():
    from camctl.contracts.pages import Page
    from camctl.persistence.repositories.segments import SegmentRepository
    state, owned, _ = runtime()
    repository = create_autospec(SegmentRepository, instance=True, spec_set=True)
    ref = object()
    repository.read_members.side_effect = [Page(tuple(range(128)), 'next'), Page((128,), None)]
    async def read(callback, **kwargs):
        return callback(owned)
    state.database.read.side_effect = read
    seen = [item async for item in segment_completion._members(repository, ref, state)]
    assert seen == list(range(129))
    assert [call.args[2] for call in repository.read_members.call_args_list] == [128, 128]


async def test_completed_control_error_clears_segment_pending_before_propagation():
    from support.business_database import inline_business_database
    state, owned, repository = runtime()
    state.database = inline_business_database(owned)
    class Control(BaseException):
        pass
    error = Control('committed control')
    repository.finish_segment_recording.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, 'saved', control=error)
    with pytest.raises(Control, match='committed control'):
        await segment_completion._save(state, 7, repository, 'finish_segment_recording', object())
    assert not state.pending_segment_writes


@pytest.mark.parametrize("control_error", [False, True])
async def test_segment_completion_logs_once_after_reliable_commit_including_notification_error(control_error):
    from support.business_database import inline_business_database

    state, owned, repository = runtime()
    state.database = inline_business_database(owned)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    ref = SegmentResultRef(7, 3, 1)
    request = FinishSegmentRecording(ref, 71, 9, 1000)
    result = SegmentFinishResult(ref, 3, 2, HistoryBoundary(1, 9), 2, 1, 1, 1)
    failure = RuntimeError("已提交后的回执通知错误") if control_error else None
    repository.finish_segment_recording.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, result, control=failure)
    with event_logging(channel):
        if control_error:
            with pytest.raises(RuntimeError) as caught:
                await segment_completion._save(state, 7, repository, "finish_segment_recording", request)
            assert caught.value is failure
        else:
            assert await segment_completion._save(state, 7, repository,
                "finish_segment_recording", request) is result
        await segment_completion.resume_segment_writes(state.database, state.pending_segment_writes, 7)
    info = [json.loads(call.args[0].message) for call in channel.slog.call_args_list
        if json.loads(call.args[0].message)["level"] == "INFO"]
    payload, = info
    assert (payload["stage"], payload["event"], payload["action_id"], payload["activity_id"],
        payload["run_id"], payload["action_status"], payload["output_count"]) == (
        "capture.completion", "settled", 7, 71, 9, 3, 2)
    assert not state.pending_segment_writes


async def test_terminal_inspection_cancel_waits_for_save_and_applies_pending(monkeypatch):
    from camctl.capture import action6_recovery, segment_inspections
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRef
    state, owned, _ = runtime()
    state.media = None
    state.wall_us = lambda: 5
    state.pending_segment_inspection_results = {}
    state.pending_read_business = {}
    repository = create_autospec(segment_inspections.SegmentInspectionRepository,
                                 instance=True, spec_set=True)
    repository.read.return_value = SegmentInspectionRef(
        3, 7, 2, 4, 5, 1, {'metric': 'frames'}, {})
    repository.save_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    monkeypatch.setattr(segment_inspections, 'SegmentInspectionRepository',
        create_autospec(segment_inspections.SegmentInspectionRepository, return_value=repository))
    owned.connection.execute.return_value.fetchall.return_value = [(3,)]
    async def read(callback, **kwargs):
        return callback(owned)
    state.database.read.side_effect = read
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(callback, *, key, **kwargs):
        held = state.pending_segment_inspection_results[3]
        assert held[1] == key
        entered.set()
        await release.wait()
        return callback(owned)
    state.database.write.side_effect = write
    task = asyncio.create_task(action6_recovery.finalize_pending_action6_inspections(
        state, 7, reason='source_terminal'))
    await entered.wait()
    task.cancel('inspection cancel')
    await asyncio.sleep(0)
    assert not task.done() and 3 in state.pending_segment_inspection_results
    release.set()
    with pytest.raises(asyncio.CancelledError, match='inspection cancel'):
        await task
    assert not state.pending_segment_inspection_results


async def test_unknown_terminal_inspection_retains_original_request_and_key(monkeypatch):
    from camctl.capture import action6_recovery, segment_inspections
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRef
    state, owned, _ = runtime()
    state.media = None
    state.wall_us = lambda: 5
    state.pending_segment_inspection_results = {}
    state.pending_read_business = {}
    repository = create_autospec(segment_inspections.SegmentInspectionRepository,
                                 instance=True, spec_set=True)
    repository.read.return_value = SegmentInspectionRef(
        3, 7, 2, 4, 5, 1, {'metric': 'frames'}, {})
    repository.save_result.return_value = DbOutcome(DbOutcomeKind.UNKNOWN)
    monkeypatch.setattr(segment_inspections, 'SegmentInspectionRepository',
        create_autospec(segment_inspections.SegmentInspectionRepository, return_value=repository))
    owned.connection.execute.return_value.fetchall.side_effect = [[(3,)], [(7,)]]
    async def read(callback, **kwargs):
        return callback(owned)
    async def write(callback, **kwargs):
        return callback(owned)
    state.database.read.side_effect = read
    state.database.write.side_effect = write
    assert not await action6_recovery.finalize_pending_action6_inspections(
        state, 7, reason='source_terminal')
    original = state.pending_segment_inspection_results[3]
    assert not await action6_recovery.finalize_pending_action6_inspections(
        state, 7, reason='source_terminal')
    assert state.pending_segment_inspection_results[3] == original
    assert state.pending_segment_inspection_results[3][0] is original[0]
    calls = repository.save_result.call_args_list
    assert calls[0].args == calls[1].args == (*original, owned)
