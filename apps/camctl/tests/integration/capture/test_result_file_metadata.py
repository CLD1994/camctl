"""正式仓储读取原 RESULTS 元数据，不以独立文件完成事实覆盖它。"""

import json
from pathlib import Path

import pytest

from camctl.capture.files import FileCompletionSave, FileObservationSave, FilePresenceSave, OwnershipSave
from camctl.capture.results import FileKind
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..support.result_cases import begin_result_attempt
from .test_result_consumer_saves import _actual, _EVIDENCE
from .result_consumer_fixtures import consumer_world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("latest_without_observation", [False, True])
async def test_saved_listing_preserves_v1_metadata_when_file_evidence_has_no_kind(tmp_path, latest_without_observation):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    actual = _actual(action_id, with_files=True, complete=True)
    actual.observations[0].data["entries"][0].update(
        original_name="original.mp4", media_type="video/mp4")
    actual.observations[0].data["entries"].append({
        "identity": "preview", "locator": {"path": "/DCIM/preview.jpg"},
        "kind": "photo", "complete": True, "size_bytes": 7,
        "original_name": "preview.jpg", "media_type": "image/jpeg", "paired_identity": "original",
    })
    repository = CaptureRepository()
    expected_file_ids = {}
    occurred_at = runtime.wall_us()
    ticket = begin_result_attempt(owned, action_id, runtime.check_config, occurred_at)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        original_id = None
        for identity, locator, size, role in (
            ("original", {"path": "/DCIM/original.mp4"}, 41, 2),
            ("preview", {"path": "/DCIM/preview.jpg"}, 7, 3),
        ):
            discovered = repository.save_file_observation(FileObservationSave(
                action_id, identity, locator, occurred_at), new_operation_key(), owned)
            assert discovered.kind is DbOutcomeKind.COMPLETED, discovered.error
            file_id = discovered.value.file_id
            expected_file_ids[identity] = file_id
            if original_id is None:
                original_id = file_id
            owner = OwnershipSave(file_id, action_id, 1, role, {"listing": identity}, occurred_at,
                **({"paired_device_file_id": original_id, "pairing_observation": {"paired_identity": "original"}}
                   if role == 3 else {}))
            for save, command in (
                (repository.save_file_presence, FilePresenceSave(file_id, 2, occurred_at)),
                (repository.save_file_ownership, owner),
                (repository.save_file_completion, FileCompletionSave(file_id, 3, occurred_at,
                    basis=1, observation={"size_verified": True}, size_bytes=size)),
            ):
                receipt = save(command, new_operation_key(), owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        finish = AttemptFinish(ticket, validate_outcome(ticket, actual, _EVIDENCE), occurred_at,
            retry_wait=latest_without_observation,
            run_finish=None if latest_without_observation else RunFinish(RunOutcome.SUCCEEDED))
        saved = OperationRepository().finish_attempt(finish, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if latest_without_observation:
            ticket = begin_result_attempt(owned, action_id, runtime.check_config, occurred_at)
            missing = _actual(action_id, with_files=False)
            saved = OperationRepository().finish_attempt(AttemptFinish(ticket,
                validate_outcome(ticket, missing, _EVIDENCE), occurred_at,
                run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())

        current = repository.read_result_sources(ticket, None, 32, owned)
        assert current.next_cursor is None
        entries = {entry.identity: entry for entry, _, _ in current.items}
        files = {entry.identity: file for entry, file, _ in current.items}
        file_ids = {entry.identity: identity for entry, _, identity in current.items}
        assert set(entries) == {"original", "preview"}
        assert file_ids == expected_file_ids
        assert entries["original"].kind is FileKind.VIDEO
        assert (entries["original"].original_name, entries["original"].media_type,
                entries["original"].size_bytes) == ("original.mp4", "video/mp4", 41)
        assert entries["preview"].kind is FileKind.PHOTO
        assert (entries["preview"].original_name, entries["preview"].media_type,
                entries["preview"].paired_identity, entries["preview"].size_bytes) == (
                    "preview.jpg", "image/jpeg", "original", 7)
        assert all(entry.complete for entry in entries.values())
        assert files["original"].kind is FileKind.VIDEO
        assert files["preview"].kind is FileKind.PHOTO
        latest, raw_error = owned.connection.execute(
            "SELECT result_json,error_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (ticket.run_id,)).fetchone()
        assert json.loads(raw_error) == {"code": "transport_timeout", "stage": "transport",
                                         "details": {"received_bytes": 23}}
        observations = json.loads(latest)["observations"]
        assert observations == ([] if latest_without_observation else [{
            "type": value.type, "version": value.version, "data": dict(value.data)}
            for value in actual.observations])
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone() == (
                2 if latest_without_observation else 1,)
    finally:
        owned.connection.close()
