"""单张主机拍摄成功登记必须沿预存责任形成唯一配对。"""

from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.capture.media import RecordingFailure
from camctl.outputs.catalog import FileReference, OutputDraft, OutputKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCanceledCapture,
)
from camctl.persistence.transaction import row_facts

from .test_host_photo_storage import _complete, _drafts, _photo_environment, _prepare
from .test_recording_finish import _NOW, _finish


def _snapshot(owned):
    connection = owned.connection
    return {
        "actions": connection.execute("SELECT id, status FROM actions ORDER BY id").fetchall(),
        "plans": connection.execute("SELECT id, status FROM plans ORDER BY id").fetchall(),
        "outputs": connection.execute("SELECT * FROM outputs ORDER BY id").fetchall(),
        "origins": connection.execute("SELECT * FROM output_origins ORDER BY id").fetchall(),
        "files": connection.execute(
            "SELECT id, purpose, relative_path, retention_state, cleanup_state, size_bytes, sha256"
            " FROM intermediate_files ORDER BY id"
        ).fetchall(),
        "events": connection.execute("SELECT COUNT(*) FROM history_events").fetchone(),
        "transactions": connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone(),
    }


def test_host_spec_with_no_source_responsibilities_cannot_succeed_empty(tmp_path):
    from camctl.acceptance.definitions import read_action_spec

    owned = _photo_environment(tmp_path)
    try:
        action = row_facts(owned.connection, "actions", 1)
        spec = read_action_spec(action)
        assert spec["output_storage"] == "host"
        before = _snapshot(owned)

        result = CaptureRepository().finish_capture(
            _finish(drafts=()), new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_host_spec_with_unreadable_saved_spec_is_not_treated_as_legacy(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        owned.connection.execute(
            "UPDATE actions SET execution_spec_json=? WHERE id=1",
            ('{"unexpected":true}',),
        )
        owned.connection.commit()
        before = _snapshot(owned)

        result = CaptureRepository().finish_capture(
            _finish(drafts=()), new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_action_finish_event_guard_rejects_host_spec_without_source_responsibilities(
    tmp_path,
):
    from types import SimpleNamespace

    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.capture import _action_finish_guard

    owned = _photo_environment(tmp_path)
    try:
        action = row_facts(owned.connection, "actions", 1)

        class Context:
            state_rows = {"actions": {1: action}}
            transaction_rows = {"outputs": {}, "output_origins": {}}

            def complete_rows(self, table, column, identity):
                assert (table, column, identity) == (
                    "intermediate_files", "owner_action_id", 1)
                return {}

        row = SimpleNamespace(
            table="actions",
            row_id=1,
            before=SimpleNamespace(exists=True, values={"cancel_requested": 0}),
            after=SimpleNamespace(values={"status": 3}),
        )

        with pytest.raises(EventValidationError):
            _action_finish_guard(SimpleNamespace(rows=(row,)), Context())
    finally:
        owned.connection.close()


@pytest.mark.parametrize(
    "draft_shape",
    ["empty", "original", "preview", "wrong_hash", "swapped_roles",
     "wrong_preview_link", "duplicate_original", "extra_device"],
)
def test_success_rejects_nonmatching_host_photo_drafts_without_changes(
    tmp_path, draft_shape
):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        preview, original = _drafts(files)
        if draft_shape == "empty":
            incomplete = ()
        elif draft_shape == "original":
            incomplete = (original,)
        elif draft_shape == "preview":
            incomplete = (preview,)
        elif draft_shape == "wrong_hash":
            incomplete = (preview, replace(original, sha256="c" * 64))
        elif draft_shape == "swapped_roles":
            incomplete = (
                OutputDraft(OutputKind.ORIGINAL,
                    FileReference(intermediate_file_id=files.preview.file_id), True,
                    "b" * 64, original_name="tool-name.jpg", media_type="image/jpeg"),
                OutputDraft(OutputKind.PREVIEW,
                    FileReference(intermediate_file_id=files.original.file_id), True,
                    "a" * 64, original_batch_intermediate_file_id=files.preview.file_id,
                    original_name="tool-name.3fr", media_type="image/x-hasselblad-3fr"),
            )
        elif draft_shape == "wrong_preview_link":
            incomplete = (
                replace(preview, original_batch_intermediate_file_id=files.preview.file_id),
                original,
            )
        elif draft_shape == "duplicate_original":
            incomplete = (preview, original, original)
        else:
            incomplete = (preview, original, OutputDraft(
                OutputKind.ORIGINAL,
                # 同一个数字也不能把设备文件身份折叠成主机中间文件身份。
                FileReference(device_file_id=files.original.file_id), True))
        before = _snapshot(owned)

        result = CaptureRepository().finish_capture(
            _finish(drafts=incomplete), new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_success_rejects_host_pair_without_two_complete_source_facts(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        before = _snapshot(owned)
        result = CaptureRepository().finish_capture(
            _finish(drafts=_drafts(files)), new_operation_key(), owned
        )
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_legacy_photo_with_host_roles_still_requires_complete_pair(tmp_path):
    owned = _photo_environment(tmp_path, host_spec=False)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        before = _snapshot(owned)

        result = CaptureRepository().finish_capture(
            _finish(drafts=()), new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_success_rejects_another_actions_host_original_even_when_roles_match(tmp_path):
    from camctl.persistence.repositories.host_capture import (
        HostCaptureRepository,
        HostPhotoComplete,
    )

    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        cursor = owned.connection.execute("SELECT * FROM actions WHERE id=1")
        columns = tuple(item[0] for item in cursor.description)
        other_action = dict(zip(columns, cursor.fetchone()))
        other_action.update(id=2, input_index=1, name="另一动作")
        owned.connection.execute(
            f"INSERT INTO actions ({','.join(columns)})"
            f" VALUES ({','.join('?' for _ in columns)})",
            tuple(other_action[column] for column in columns),
        )
        owned.connection.commit()
        other = HostCaptureRepository().prepare_host_photo(
            2, _NOW, new_operation_key(), owned)
        assert other.kind is DbOutcomeKind.COMPLETED, other.error
        other_files = other.value
        complete = HostCaptureRepository().save_host_photo_complete(
            HostPhotoComplete(
                2, other_files.original.file_id, 4096, "c" * 64,
                other_files.preview.file_id, 128, "d" * 64, _NOW,
            ),
            new_operation_key(), owned,
        )
        assert complete.kind is DbOutcomeKind.COMPLETED, complete.error
        _preview, original = _drafts(files)
        foreign_original = OutputDraft(
            OutputKind.ORIGINAL,
            FileReference(intermediate_file_id=other_files.original.file_id),
            True,
            "c" * 64,
            original_name="other-action.3fr",
            media_type="image/x-hasselblad-3fr",
        )
        before = _snapshot(owned)

        rejected = CaptureRepository().finish_capture(
            _finish(drafts=(original, _drafts(files)[0], foreign_original)),
            new_operation_key(),
            owned,
        )

        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


def test_device_photo_with_only_original_is_not_subject_to_host_pair_rule(tmp_path):
    owned = _photo_environment(tmp_path, host_spec=False)
    try:
        result = CaptureRepository().finish_capture(
            _finish(drafts=(
                OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=11), True),
            )),
            new_operation_key(),
            owned,
        )
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT kind, device_file_id FROM outputs").fetchall() == [(1, 11)]
    finally:
        owned.connection.close()


def test_failed_host_photo_can_finish_without_complete_pair_or_outputs(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        failed = replace(
            _finish(drafts=()),
            failure=RecordingFailure("capture_failed", {
                "activity_id": "1", "reason": "no_outputs",
            }),
        )
        result = CaptureRepository().finish_capture(failed, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 4
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT retention_state, cleanup_state, size_bytes, sha256"
            " FROM intermediate_files WHERE id IN (?, ?) ORDER BY id",
            (files.original.file_id, files.preview.file_id),
        ).fetchall() == [(1, 1, None, None), (1, 1, None, None)]
    finally:
        owned.connection.close()


def test_failed_host_photo_may_retain_only_reliable_original(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        _preview, original = _drafts(files)
        failed = replace(
            _finish(drafts=(original,)),
            failure=RecordingFailure("capture_failed", {
                "activity_id": "1", "reason": "invalid_outputs",
            }),
        )

        result = CaptureRepository().finish_capture(
            failed, new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 4
        assert owned.connection.execute(
            "SELECT kind, intermediate_file_id FROM outputs ORDER BY id"
        ).fetchall() == [(1, files.original.file_id)]
        assert owned.connection.execute(
            "SELECT id, retention_state FROM intermediate_files ORDER BY id"
        ).fetchall() == [(files.original.file_id, 3), (files.preview.file_id, 1)]
    finally:
        owned.connection.close()


def test_canceled_host_photo_without_complete_pair_keeps_file_responsibilities(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
        owned.connection.commit()

        result = CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(action_id=1, occurred_at=_NOW),
            new_operation_key(), owned,
        )

        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 6
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT id, retention_state, cleanup_state, size_bytes, sha256"
            " FROM intermediate_files ORDER BY id"
        ).fetchall() == [
            (files.original.file_id, 1, 1, None, None),
            (files.preview.file_id, 1, 1, None, None),
        ]
    finally:
        owned.connection.close()


def test_canceled_host_photo_may_promote_only_reliable_original(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        _preview, original = _drafts(files)
        catalog_facts = _finish(drafts=(original,)).catalog_facts
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
        owned.connection.commit()

        result = CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(
                action_id=1, occurred_at=_NOW, drafts=(original,),
                catalog_facts=catalog_facts,
            ),
            new_operation_key(), owned,
        )

        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 6
        assert owned.connection.execute(
            "SELECT kind, intermediate_file_id FROM outputs ORDER BY id"
        ).fetchall() == [(1, files.original.file_id)]
        assert owned.connection.execute(
            "SELECT id, retention_state FROM intermediate_files ORDER BY id"
        ).fetchall() == [(files.original.file_id, 3), (files.preview.file_id, 1)]
    finally:
        owned.connection.close()


def test_success_accepts_reversed_complete_host_pair_and_preserves_identity(tmp_path):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        preview, original = _drafts(files)
        result = CaptureRepository().finish_capture(
            _finish(drafts=(original, preview)), new_operation_key(), owned
        )

        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        original_id, preview_id = result.value.output_ids
        assert owned.connection.execute(
            "SELECT kind, intermediate_file_id FROM outputs WHERE id=?", (original_id,)
        ).fetchone() == (1, files.original.file_id)
        assert owned.connection.execute(
            "SELECT kind, intermediate_file_id FROM outputs WHERE id=?", (preview_id,)
        ).fetchone() == (3, files.preview.file_id)
        assert owned.connection.execute(
            "SELECT output_id, original_output_id FROM output_origins"
        ).fetchall() == [(preview_id, original_id)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "new-key"])
def test_terminal_success_with_mispaird_saved_outputs_is_rejected_without_repair(
    tmp_path, same_key,
):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        request = _finish(drafts=_drafts(files))
        key = new_operation_key()
        saved = CaptureRepository().finish_capture(request, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

        original_id, = owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=?",
            (int(enum_for("outputs.kind").ORIGINAL),),
        ).fetchone()
        owned.connection.execute(
            "UPDATE outputs SET kind=? WHERE id=?",
            (int(enum_for("outputs.kind").PREVIEW), original_id),
        )
        owned.connection.commit()
        before = _snapshot(owned)

        recovered = CaptureRepository().finish_capture(
            request, key if same_key else new_operation_key(), owned
        )

        assert recovered.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "new-key"])
@pytest.mark.parametrize("missing_roles", [True, False], ids=["zero-host-roles", "one-host-role"])
def test_terminal_host_spec_missing_pair_is_not_recovered_or_repaired(
    tmp_path, same_key, missing_roles,
):
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        request = _finish(drafts=_drafts(files))
        key = new_operation_key()
        saved = CaptureRepository().finish_capture(request, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

        other_action_purpose = int(
            enum_for("intermediate_files.purpose").RECORDING_INPUT)
        owned.connection.execute(
            "UPDATE intermediate_files SET purpose=?, relative_path=? WHERE id=?",
            (other_action_purpose,
             f"recording-inputs/{files.original.file_id}", files.original.file_id),
        )
        if missing_roles:
            owned.connection.execute(
                "UPDATE intermediate_files SET purpose=?, relative_path=? WHERE id=?",
                (other_action_purpose,
                 f"recording-inputs/{files.preview.file_id}", files.preview.file_id),
            )
        owned.connection.commit()
        before = _snapshot(owned)

        recovered = CaptureRepository().finish_capture(
            request, key if same_key else new_operation_key(), owned
        )

        assert recovered.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("clean_kind", ["original", "preview"])
async def test_same_and_new_key_recovery_preserve_explicit_source_cleanup(
    tmp_path, clean_kind
):
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.outputs.cleanup_flow import delete_source_file
    from camctl.persistence.repositories.acceptance import register_acceptance_guards
    from camctl.persistence.repositories.scheduling import register_window_guard
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup

    register_acceptance_guards()
    register_window_guard()
    owned = _photo_environment(tmp_path)
    try:
        files = _prepare(owned)
        _complete(owned, files)
        request = _finish(drafts=_drafts(files))
        key = new_operation_key()
        first = CaptureRepository().finish_capture(request, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        preview_id, original_id = first.value.output_ids
        source_id = original_id if clean_kind == "original" else preview_id
        source_path = tmp_path / (
            files.original.relative_path if clean_kind == "original"
            else files.preview.relative_path
        )
        source_path.parent.mkdir()
        source_path.write_bytes(b"x" * (4096 if clean_kind == "original" else 128))
        _, item_id = _start_cleanup(owned, source_id, "23")
        runtime = session_cleanup_assembly(
            devices={}, drivers=None, staging=tmp_path, max_delete_attempts=1,
            max_query_attempts=1, occurred_at=lambda: 1_800_000_000_000_000,
            monotonic_ns=lambda: 0,
        )(owned)
        cleanup = await delete_source_file(runtime.for_item(item_id), item_id)
        assert cleanup.phase == "succeeded", cleanup
        assert not source_path.exists()
        before = _snapshot(owned)

        same_key = CaptureRepository().finish_capture(request, key, owned)
        new_key = CaptureRepository().finish_capture(request, new_operation_key(), owned)

        assert same_key.kind is DbOutcomeKind.COMPLETED, same_key.error
        assert new_key.kind is DbOutcomeKind.COMPLETED, new_key.error
        assert same_key.value.output_ids == first.value.output_ids
        assert new_key.value.output_ids == first.value.output_ids
        assert _snapshot(owned) == before
        assert owned.connection.execute(
            "SELECT output_id, original_output_id FROM output_origins"
        ).fetchall() == [(preview_id, original_id)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "new-key"])
@pytest.mark.parametrize("output_count", [0, 1], ids=["no-outputs", "one-device-output"])
def test_terminal_host_spec_without_source_pair_is_consistency_error_on_recovery(
    tmp_path, monkeypatch, same_key, output_count,
):
    import camctl.persistence.repositories.capture as capture_module
    from camctl.contracts.values import ConsistencyError

    owned = _photo_environment(tmp_path)
    try:
        drafts = () if output_count == 0 else (
            OutputDraft(
                OutputKind.ORIGINAL,
                FileReference(device_file_id=11),
                True,
            ),
        )
        request = _finish(drafts=drafts)
        key = new_operation_key()

        # 仅构造一个缺少主机源责任的已终态回执，随后恢复路径必须拒绝确认它。
        monkeypatch.setattr(
            capture_module, "_host_photo_source_pair",
            lambda *_args, **_kwargs: None,
        )
        saved = CaptureRepository().finish_capture(request, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        monkeypatch.undo()
        before = _snapshot(owned)

        recovered = CaptureRepository().finish_capture(
            request, key if same_key else new_operation_key(), owned
        )

        assert recovered.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(recovered.error, ConsistencyError)
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()
