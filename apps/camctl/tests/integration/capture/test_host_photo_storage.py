"""主机拍摄源的准备、可靠字节、登记、提升与重送组合。"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.events import branch_of, load_event_registry
from camctl.history.host_photo_request import HostPhotoRequestError
from camctl.outputs.catalog import FileReference, OutputDraft, OutputKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.transaction import row_facts
from camctl.persistence.runtime import CURRENT_FORMAT_VERSION
from camctl.persistence.repositories.outputs import load_output_family, register_outputs_guards

from .test_recording_finish import _environment, _finish, _NOW

register_capture_guards()
register_outputs_guards()


def _photo_environment(tmp_path, *, legacy_v1=False, host_spec=True):
    owned = _environment(
        tmp_path,
        format_version=1 if legacy_v1 else CURRENT_FORMAT_VERSION,
        legacy_v1_schema=legacy_v1,
    )
    parameter_type = "hasselblad_photo" if host_spec else "legacy_photo"
    execution_spec = (
        {"output_storage": "host", "start_call_timeout_s": 180}
        if host_spec else {}
    )
    owned.connection.execute("UPDATE actions SET type=1 WHERE id=1")
    owned.connection.execute(
        "UPDATE actions SET input_fields_json=?, effective_params_json=?,"
        " driver_id=?, max_delay_ms=1000, execution_spec_json=? WHERE id=1",
        (
            json.dumps({
                "params": {"type": parameter_type},
                "policy": {"max_delay_ms": 1000},
            }),
            json.dumps({"type": parameter_type}),
            "hasselblad_cfv100c" if host_spec else "legacy_camera",
            json.dumps(execution_spec),
        ),
    )
    owned.connection.commit()
    action = row_facts(owned.connection, "actions", 1)
    from camctl.acceptance.definitions import read_action_spec

    checked_spec = read_action_spec(action)
    assert checked_spec is not None
    assert checked_spec.get("output_storage") == ("host" if host_spec else None)
    return owned


def _prepare(owned, key=None):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository
    result = HostCaptureRepository().prepare_host_photo(1, _NOW, key or new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value


def _database_snapshot(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    snapshot = []
    for (table_name,) in tables:
        quoted_name = table_name.replace('"', '""')
        rows = connection.execute(f'SELECT * FROM "{quoted_name}"').fetchall()
        snapshot.append((table_name, tuple(rows)))
    return tuple(snapshot)


@pytest.mark.parametrize("action_id", [Decimal("1.0"), Decimal("7.0")])
def test_prepare_rejects_python_decimal_action_identity_without_database_changes(tmp_path, action_id):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    owned = _photo_environment(tmp_path)
    try:
        repository = HostCaptureRepository()
        before = _database_snapshot(owned.connection)
        error = None
        try:
            repository.prepare_host_photo(action_id, _NOW, new_operation_key(), owned)
        except HostPhotoRequestError as caught:
            error = caught
        after = _database_snapshot(owned.connection)

        if after != before:
            pytest.fail("prepare_host_photo changed database snapshot before rejecting Decimal")
        assert isinstance(error, HostPhotoRequestError)
    finally:
        owned.connection.close()


def test_prepare_rejects_python_decimal_timestamp_without_database_changes(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        from camctl.persistence.repositories.host_capture import HostCaptureRepository
        before = _database_snapshot(owned.connection)
        error = None
        try:
            HostCaptureRepository().prepare_host_photo(1, Decimal("0e0"), new_operation_key(), owned)
        except HostPhotoRequestError as caught:
            error = caught
        after = _database_snapshot(owned.connection)

        if after != before:
            pytest.fail("prepare_host_photo changed database snapshot before rejecting Decimal")
        assert isinstance(error, HostPhotoRequestError)
    finally:
        owned.connection.close()


def _complete(owned, files):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository, HostPhotoComplete
    request = HostPhotoComplete(1, files.original.file_id, 4096, "a" * 64,
                                files.preview.file_id, 128, "b" * 64, _NOW)
    result = HostCaptureRepository().save_host_photo_complete(request, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return request


def _drafts(files):
    return (
        OutputDraft(OutputKind.PREVIEW, FileReference(intermediate_file_id=files.preview.file_id),
                    True, "b" * 64, original_batch_intermediate_file_id=files.original.file_id,
                    original_name="tool-name.jpg", media_type="image/jpeg"),
        OutputDraft(OutputKind.ORIGINAL, FileReference(intermediate_file_id=files.original.file_id),
                    True, "a" * 64, original_name="tool-name.3fr", media_type="image/x-hasselblad-3fr"),
    )


def test_prepare_reuses_action_files_across_operation_keys_without_physical_files(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        key = new_operation_key()
        first = _prepare(owned, key)
        assert _prepare(owned, key) == first
        assert _prepare(owned) == first
        assert first.original.relative_path == f"capture-originals/{first.original.file_id}.3fr"
        assert first.preview.relative_path == f"capture-previews/{first.preview.file_id}.jpg"
        assert not (tmp_path / first.original.relative_path).exists()
        assert owned.connection.execute("SELECT COUNT(*) FROM intermediate_files").fetchone() == (2,)
    finally:
        owned.connection.close()


def test_legacy_v1_rejects_host_photo_writes_without_history_or_files(tmp_path):
    from camctl.persistence.repositories.host_capture import (
        HostCaptureRepository,
        HostPhotoComplete,
    )
    from camctl.persistence.runtime import DatabaseUnsupportedFormatError

    owned = _photo_environment(tmp_path, legacy_v1=True)
    repository = HostCaptureRepository()
    try:
        before = (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone(),
            owned.connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone(),
            owned.connection.execute("SELECT COUNT(*) FROM intermediate_files").fetchone(),
        )
        prepared = repository.prepare_host_photo(1, _NOW, new_operation_key(), owned)
        assert prepared.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(prepared.error, DatabaseUnsupportedFormatError)

        complete = HostPhotoComplete(
            1, 201, 4096, "a" * 64, 202, 128, "b" * 64, _NOW
        )
        saved = repository.save_host_photo_complete(complete, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(saved.error, DatabaseUnsupportedFormatError)
        assert (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone(),
            owned.connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone(),
            owned.connection.execute("SELECT COUNT(*) FROM intermediate_files").fetchone(),
        ) == before
    finally:
        owned.connection.close()


def test_legacy_v1_recording_finish_remains_usable(tmp_path):
    owned = _environment(tmp_path, format_version=1, legacy_v1_schema=True)
    try:
        result = CaptureRepository().finish_capture(
            _finish(), new_operation_key(), owned
        )
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 3
    finally:
        owned.connection.close()


def test_host_original_preview_register_promote_and_recover_without_new_outputs(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        event_type = load_event_registry()["events"]["INTERMEDIATE_FILE_CHANGED"]["id"]
        branches = [
            branch_of(event_type, json.loads(body_json)["reason"])[0]
            for (body_json,) in owned.connection.execute(
                "SELECT body_json FROM history_events WHERE event_type = ? ORDER BY id",
                (event_type,),
            )
        ]
        assert branches == ["CREATE", "HOST_PHOTO_COMPLETE"]
        key = new_operation_key()
        request = _finish(drafts=_drafts(files))
        repository = CaptureRepository()
        result = repository.finish_capture(request, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        preview_id, original_id = result.value.output_ids
        assert owned.connection.execute("SELECT output_id, original_output_id FROM output_origins").fetchall() == [(preview_id, original_id)]
        assert owned.connection.execute("SELECT retention_state FROM intermediate_files ORDER BY id").fetchall() == [(3,), (3,)]
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute("SELECT original_name, media_type FROM outputs WHERE id=?", (original_id,)).fetchone() == ("tool-name.3fr", "image/x-hasselblad-3fr")
        family = load_output_family(owned.connection, preview_id)
        assert family.original.output_id == original_id
        for recovery_key in (key, new_operation_key()):
            recovered = repository.finish_capture(request, recovery_key, owned)
            assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
            assert recovered.value.output_ids == result.value.output_ids
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
    finally:
        owned.connection.close()


def test_host_byte_fact_conflict_rolls_back_without_rewriting_original(tmp_path):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        request = _complete(owned, files)
        result = HostCaptureRepository().save_host_photo_complete(
            replace(request, original_sha256="c" * 64), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT sha256 FROM intermediate_files WHERE id=?", (files.original.file_id,)).fetchone() == ("a" * 64,)
    finally:
        owned.connection.close()


def test_host_capture_file_cannot_register_as_repaired_without_repair_success(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        draft = OutputDraft(OutputKind.REPAIRED, FileReference(intermediate_file_id=files.preview.file_id),
                            True, "b" * 64, original_batch_file_id=11)
        device_original = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=11), True)
        result = CaptureRepository().finish_capture(_finish(drafts=(device_original, draft)), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        owned.connection.close()


def test_original_complete_request_can_be_verified_after_promotion_with_new_connection(tmp_path):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository, PrepareHostPhoto
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    owned = _photo_environment(tmp_path)
    verification = None
    try:
        repository = HostCaptureRepository()
        prepare_key = new_operation_key()
        files = _prepare(owned, prepare_key)
        from camctl.persistence.repositories.host_capture import HostPhotoComplete
        request = HostPhotoComplete(1, files.original.file_id, 4096, "a" * 64,
                                    files.preview.file_id, 128, "b" * 64, _NOW)
        complete_key = new_operation_key()
        result = repository.save_host_photo_complete(request, complete_key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        finished = CaptureRepository().finish_capture(_finish(drafts=_drafts(files)), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        verification = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        for original_request, key in ((PrepareHostPhoto(1, _NOW), prepare_key), (request, complete_key)):
            result = repository.verify_operation(original_request, key, verification)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            assert result.value == files
        changed = repository.verify_operation(replace(request, preview_size_bytes=129), complete_key, verification)
        assert changed.kind is DbOutcomeKind.UNKNOWN
        missing = repository.verify_operation(request, new_operation_key(), verification)
        assert missing.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
    finally:
        if verification is not None:
            verification.connection.close()
        owned.connection.close()


@pytest.mark.parametrize("same_key", [False, True])
def test_finish_recovery_rejects_host_metadata_change(tmp_path, same_key):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        drafts = _drafts(files)
        key = new_operation_key()
        result = CaptureRepository().finish_capture(_finish(drafts=drafts), key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        changed = replace(drafts[0], original_name="other-preview.jpg")
        rejected = CaptureRepository().finish_capture(_finish(drafts=(changed, drafts[1])),
            key if same_key else new_operation_key(), owned)
        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_original_requests_remain_verifiable_after_explicit_source_cleanup(tmp_path):
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.outputs.cleanup_flow import delete_source_file
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    from camctl.persistence.repositories.host_capture import HostCaptureRepository, HostPhotoComplete, PrepareHostPhoto
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.persistence.repositories.acceptance import register_acceptance_guards
    from camctl.persistence.repositories.scheduling import register_window_guard
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
    register_acceptance_guards()
    register_window_guard()
    owned = _photo_environment(tmp_path)
    verification = None
    try:
        repository = HostCaptureRepository()
        prepare_key, complete_key = new_operation_key(), new_operation_key()
        files = _prepare(owned, prepare_key)
        complete = HostPhotoComplete(1, files.original.file_id, 4096, "a" * 64,
                                     files.preview.file_id, 128, "b" * 64, _NOW)
        saved = repository.save_host_photo_complete(complete, complete_key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        finished = CaptureRepository().finish_capture(_finish(drafts=_drafts(files)), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        preview_id, original_id = finished.value.output_ids
        original_path = tmp_path / files.original.relative_path
        preview_path = tmp_path / files.preview.relative_path
        original_path.parent.mkdir()
        preview_path.parent.mkdir()
        original_path.write_bytes(b"o" * 4096)
        preview_path.write_bytes(b"p" * 128)
        _, item_id = _start_cleanup(owned, original_id, "23")
        work_files = WorkFileRuntime(
            staging=tmp_path,
            limits=WorkFileLimits(batch_size=100, limit_per_run=100),
        )
        runtime = session_cleanup_assembly(devices={}, drivers=None, staging=tmp_path,
            file_executor=work_files.executor,
            max_delete_attempts=1, max_query_attempts=1,
            occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)(owned)
        result = await delete_source_file(runtime.for_item(item_id), item_id)
        assert result.phase == "succeeded", result
        assert not original_path.exists()
        assert preview_path.exists()
        assert owned.connection.execute("SELECT retention_state, cleanup_state FROM intermediate_files WHERE id=?", (files.original.file_id,)).fetchone() == (3, 4)
        assert owned.connection.execute("SELECT availability FROM outputs WHERE id=?", (original_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT availability FROM outputs WHERE id=?", (preview_id,)).fetchone() == (1,)
        family = load_output_family(owned.connection, preview_id)
        assert family.original.availability == 3
        assert family.preview.output_id == preview_id
        assert family.preview.availability == 1
        verification = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        for request, key in ((PrepareHostPhoto(1, _NOW), prepare_key), (complete, complete_key)):
            verified = repository.verify_operation(request, key, verification)
            assert verified.kind is DbOutcomeKind.COMPLETED, verified.error
            assert verified.value == files
    finally:
        if verification is not None:
            verification.connection.close()
        owned.connection.close()
