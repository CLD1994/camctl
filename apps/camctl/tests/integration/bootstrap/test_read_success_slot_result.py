"""同一拍摄工厂在数据库重开后接回原读取结束及其机会释放。"""

import json
import sqlite3
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories import read_end
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_read_execution_runtime import _real_recording_media_flow

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("after_commit", [False, True])
async def test_internal_read_end_unknown_reuses_original_request_in_same_factory(tmp_path, monkeypatch, after_commit, capture_resources):
    from camctl.host_files.media import ProbeRequest
    from .test_media_assembly import _tool

    probe = _tool(tmp_path, "slot-probe", "print('{\"streams\": [{\"codec_type\": \"video\"}], \"format\": {\"duration\": \"6\"}}')\n")
    recording, source_id = await _real_recording_media_flow(tmp_path,
        probe_request=ProbeRequest(ffprobe=probe))
    reader = recording.reader
    options = {key: value for key, value in recording.runtime_options.items()
               if key not in {"driver", "evidence"}}
    factory = session_capture_assembly(**options)
    saves = []
    original = read_end.save_read_end

    def uncertain_once(request, key, owned):
        if not saves:
            faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
            result = original(request, key, faulty)
            saves.append((request, key, faulty.connection, result))
            return result
        saves.append((request, key, None, None))
        return original(request, key, owned)

    monkeypatch.setattr(read_end, "save_read_end", uncertain_once)
    try:
        runtime = await factory(recording.database, "cam-1")
        with pytest.raises(ConsistencyError, match="原读取业务结果未可靠保存") as caught:
            await run_recording_media(runtime.media, 1, 1, source_id)
        assert len(reader.requests) == 1 and len(saves) == 1
        ticket = reader.requests[0][0]
        request, key, fault, result = saves[0]
        assert fault.failed and result.kind is DbOutcomeKind.UNKNOWN
        assert isinstance(result.error, sqlite3.OperationalError)
        assert str(result.error) == "受控事务失败"
        assert db_outcome_for(caught.value) is result
        assert caught.value.__cause__ is result.error
        assert request.ticket == ticket
        assert request.source_size == 10 and request.end.stopped
        assert request.end.bytes_read == 10 and request.end.error is None
        assert (ticket, request.end) in reader.ends
        assert recording.owned.connection.execute(
            "SELECT status,error_json,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None, None)
        path = recording.owned.connection.execute("PRAGMA database_list").fetchone()[2]
        recording.owned.connection.close()
        recording.database = await capture_resources.restart_database(path)
        recording.owned = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
        committed, size, slot, end_event = recording.owned.connection.execute(
            "SELECT committed_bytes,source_size,slot_device_id,read_end_event_id"
            " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()
        assert (committed, size, slot) == (10, 10, None if after_commit else "cam-1")
        assert (end_event is not None) is after_commit
        assert recording.owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)
        ).fetchone() == (int(after_commit),)
        assert recording.owned.connection.execute(
            "SELECT status,error_json,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None, None)
        relative, = recording.owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?", (request.target_file_id,)
        ).fetchone()
        assert (recording.roots.staging / relative).read_bytes() == reader.content
        next_runtime = await factory(recording.database, "cam-1")
        await run_recording_media(next_runtime.media, 1, 1, source_id)
        assert len(saves) == 2 and saves[1][:2] == (request, key)
        assert saves[1][0].occurred_at == request.occurred_at
        assert len(reader.requests) == 1
        status, result_event, result_json = recording.owned.connection.execute(
            "SELECT status,result_event_id,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert status == 2 and result_event is not None
        assert json.loads(result_json)["settlement"] == {
            "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
        assert recording.owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (ticket.attempt_id,)
        slot, final_end = recording.owned.connection.execute(
            "SELECT slot_device_id,read_end_event_id FROM file_copies WHERE id=?",
            (int(ticket.target_id),)).fetchone()
        assert slot is None and final_end is not None
        if after_commit:
            assert final_end == end_event
        body, occurred_at = recording.owned.connection.execute(
            "SELECT body_json,occurred_at FROM history_events WHERE id=?", (final_end,)
        ).fetchone()
        assert json.loads(body)["evidence"]["read_end"] == request.document()
        assert occurred_at == request.occurred_at
        assert recording.owned.connection.execute(
            "SELECT COUNT(*) FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id"
            " WHERE e.id=? AND t.operation_key=?", (final_end, str(key))
        ).fetchone() == (1,)
        assert recording.owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
            " WHERE json_extract(body_json,'$.evidence.read_end.ticket.run_id')=?"
            " AND json_extract(body_json,'$.evidence.read_end.ticket.attempt_no')=?",
            (ticket.run_id, ticket.attempt_id)
        ).fetchone() == (1,)
        assert recording.owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        assert (recording.roots.staging / relative).read_bytes() == reader.content
        assert recording.owned.connection.execute(
            "SELECT check_state FROM recording_processing WHERE id=1").fetchone() == (3,)
    finally:
        recording.owned.connection.close()
