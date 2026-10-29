"""真实内部读取成功后的 slot 释放按原申请和原键恢复。"""

from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_read_execution_runtime import _real_recording_media_flow

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("after_commit", [False, True])
async def test_internal_success_slot_unknown_reopens_factory_with_original_request_and_key(tmp_path, monkeypatch, after_commit, capture_resources):
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
    original = OutputsRepository.release_read_slot

    def uncertain_once(repository, request, key, owned):
        saves.append((request, key))
        if len(saves) == 1:
            assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
                (request.copy_id,)).fetchone() == ("cam-1",)
            faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
            result = original(repository, request, key, faulty)
            assert faulty.connection.failed and result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, owned)

    monkeypatch.setattr(OutputsRepository, "release_read_slot", uncertain_once)
    try:
        runtime = await factory(recording.database, "cam-1")
        with pytest.raises(ConsistencyError):
            await run_recording_media(runtime.media, 1, 1, source_id)
        assert len(reader.requests) == 1 and len(saves) == 1
        ticket = reader.requests[0][0]
        assert recording.owned.connection.execute(
            "SELECT status,error_json,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()[0:2] == (2, None)
        result_event = recording.owned.connection.execute(
            "SELECT result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()[0]
        assert result_event is not None
        path = recording.owned.connection.execute("PRAGMA database_list").fetchone()[2]
        recording.owned.connection.close()
        recording.database = await capture_resources.restart_database(path)
        recording.owned = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
        assert recording.owned.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == (
                None if after_commit else "cam-1",)
        next_runtime = await factory(recording.database, "cam-1")
        await run_recording_media(next_runtime.media, 1, 1, source_id)
        assert len(saves) == 2 and saves[1] == saves[0]
        assert len(reader.requests) == 1
        assert recording.owned.connection.execute(
            "SELECT status,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (2, result_event)
        assert recording.owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (ticket.attempt_id,)
        assert recording.owned.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == (None,)
        assert recording.owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(saves[0][1]),)).fetchone() == (1,)
    finally:
        recording.owned.connection.close()
