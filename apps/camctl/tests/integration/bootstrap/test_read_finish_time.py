"""原真实 End 与完整 READ 结束申请分别形成，重送保持原申请时刻。"""

import asyncio

import pytest

from camctl.capture.media_flow import RecordingInputCopies, run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_output_binding_changes import _CONTENT, _NOW, environment
from .test_output_binding_transactions import pending_read
from .test_read_execution_runtime import (
    ReadRequests, _MEDIA_CONTENT, _apply_read_business_cancel, _configured_factory, _real_recording_media_flow,
)

pytestmark = pytest.mark.asyncio


async def _assert_complete_finish_time(owned, runtime, reader, target, advance, monkeypatch):
    raw_time, cancel_time, finish_time, retry_time = (_NOW + value for value in (10, 20, 30, 40))
    clock = [raw_time]
    runtime.occurred_at = lambda: clock[0]
    completion_entered, allowed = asyncio.Event(), asyncio.Event()
    complete = RecordingInputCopies.complete

    async def after_actual_end(copies, copy_id, digest):
        completion_entered.set()
        await allowed.wait()
        return await complete(copies, copy_id, digest)

    monkeypatch.setattr(RecordingInputCopies, "complete", after_actual_end)
    saves = []
    original = OperationRepository.finish_attempt

    def uncertain_once(repository, request, key, connection):
        saves.append((request, key))
        if len(saves) == 1:
            faulty = _fault_owned(connection, "COMMIT", after_commit=True)
            result = original(repository, request, key, faulty)
            assert faulty.connection.failed and result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, connection)

    monkeypatch.setattr(OperationRepository, "finish_attempt", uncertain_once)
    task = asyncio.create_task(advance())
    try:
        await asyncio.wait_for(completion_entered.wait(), timeout=5)
        ticket = reader.requests[0][0]
        held = runtime.pending_read_ends[int(ticket.target_id)]
        assert held.occurred_at == raw_time and held.end.stopped and held.end.error is None
        assert owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
        clock[0] = cancel_time
        _apply_read_business_cancel(owned, target, occurred_at=cancel_time)
        assert owned.connection.execute(
            "SELECT occurred_at FROM history_events WHERE event_type=25 AND json_extract(body_json,'$.evidence.cancel_request.mode')='with_stop' ORDER BY id DESC LIMIT 1"
        ).fetchone() == (cancel_time,)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
        clock[0] = finish_time
        allowed.set()
        with pytest.raises(ConsistencyError):
            await task
        assert len(saves) == 1
        clock[0] = retry_time
        await advance()
        assert len(saves) == 2 and saves[1] == saves[0]
        assert saves[0][0].occurred_at == finish_time
        assert owned.connection.execute(
            "SELECT event_type,occurred_at FROM history_events WHERE transaction_id=(SELECT id FROM history_transactions WHERE operation_key=?) ORDER BY id",
            (str(saves[0][1]),)).fetchall() == [(12, finish_time), (10, finish_time)]
        assert owned.connection.execute(
            "SELECT status,error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (2, None)
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (
                int(enum_for("operation_runs.status").CANCELED), ticket.attempt_id)
        assert len(reader.requests) == 1
    finally:
        allowed.set()
        if not task.done():
            await task


async def test_obtain_complete_read_finish_uses_first_complete_input_time_and_retains_it(pending_read, monkeypatch):
    cfg, owned, command = pending_read
    reader = ReadRequests(owned, _CONTENT)
    runtime = _configured_factory(cfg, reader, maximum=7)(owned)
    target = owned.connection.execute("SELECT action_id FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone()[0]
    await _assert_complete_finish_time(owned, runtime, reader, target, lambda: advance_obtain(runtime), monkeypatch)


async def test_internal_complete_read_finish_uses_first_complete_input_time_and_retains_it(tmp_path, monkeypatch):
    flow, source_id = await _real_recording_media_flow(tmp_path)
    reader = ReadRequests(flow.owned, _MEDIA_CONTENT)
    flow.sessions = flow.sessions.__class__(flow.owned, reader, None)
    try:
        await _assert_complete_finish_time(flow.owned, flow, reader, 1,
            lambda: run_recording_media(flow, 1, 1, source_id), monkeypatch)
    finally:
        flow.owned.connection.close()
