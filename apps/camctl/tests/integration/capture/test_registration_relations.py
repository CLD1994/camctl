"""完成事务保存同批原片及派生关系，关系写入失败时整组回滚。"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.catalog import FileReference, OutputDraft, OutputKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.outputs import load_output_family

from .test_recording_finish import _environment, _finish, _original_draft, _seed_device_file
from ..operations.test_result_reuse import _FaultConnection


def _derived_environment(tmp_path):
    owned = _environment(tmp_path)
    connection = owned.connection
    connection.execute("UPDATE device_files SET original_device_file_id=11, pairing_evidence_json='{}' WHERE id=12")
    connection.execute(
        "INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,retention_state,"
        " cleanup_state,size_bytes,sha256,created_event_id,last_event_id,change_count)"
        " VALUES (11,1,4,'derived/11.mp4',1,1,512,?,1,1,1)",
        ("c" * 64,))
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (1, 1, NULL, 3, 3, '{}', '{}', 5, '{}', 11, NULL, 1, NULL)"
    )
    connection.commit()
    return owned


def _draft(kind):
    return OutputDraft(
        kind=kind, file=FileReference(device_file_id=12) if kind is OutputKind.PREVIEW
        else FileReference(intermediate_file_id=11), file_complete=True, original_batch_file_id=11)


@pytest.mark.parametrize("kind", [OutputKind.PREVIEW, OutputKind.REPAIRED])
@pytest.mark.parametrize("derived_first", [False, True])
def test_finish_preserves_explicit_batch_pairing(tmp_path, kind, derived_first):
    owned = _derived_environment(tmp_path)
    try:
        drafts = (_draft(kind), _original_draft(11)) if derived_first else (_original_draft(11), _draft(kind))
        result = CaptureRepository().finish_capture(_finish(drafts=drafts), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        derived_id, original_id = result.value.output_ids if derived_first else reversed(result.value.output_ids)
        assert owned.connection.execute("SELECT output_id,original_output_id FROM output_origins").fetchall() == [(derived_id, original_id)]
        family = load_output_family(owned.connection, original_id)
        assert family.original.output_id == original_id
        derivative = family.preview if kind is OutputKind.PREVIEW else family.repaired
        assert derivative.output_id == derived_id
        assert result.value.action_status == result.value.plan_status == 3
    finally:
        owned.connection.close()


def test_failed_origin_insert_rolls_back_outputs_and_terminal_state(tmp_path):
    owned = _derived_environment(tmp_path)
    try:
        before = tuple(owned.connection.iterdump())
        faulty = replace(owned, connection=_FaultConnection(owned.connection, "INSERT INTO output_origins"))
        result = CaptureRepository().finish_capture(
            _finish(drafts=(_original_draft(11), _draft(OutputKind.PREVIEW))), new_operation_key(), faulty)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, sqlite3.OperationalError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_finish_registers_both_derivative_kinds_of_one_original(tmp_path):
    owned = _derived_environment(tmp_path)
    try:
        result = CaptureRepository().finish_capture(
            _finish(drafts=(_draft(OutputKind.REPAIRED), _draft(OutputKind.PREVIEW), _original_draft(11))),
            new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        repaired_id, preview_id, original_id = result.value.output_ids
        family = load_output_family(owned.connection, original_id)
        assert (family.original.output_id, family.preview.output_id, family.repaired.output_id) == (
            original_id, preview_id, repaired_id)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["unpaired", "wrong_pair", "missing_existing", "existing_ref_to_new_id", "duplicate_preview"])
def test_invalid_relation_rolls_back_entire_finish(tmp_path, case):
    owned = _derived_environment(tmp_path)
    connection = owned.connection
    try:
        drafts = [_original_draft(11), _draft(OutputKind.PREVIEW)]
        if case == "unpaired":
            connection.execute("UPDATE device_files SET original_device_file_id=NULL, pairing_evidence_json=NULL WHERE id=12")
        elif case == "wrong_pair":
            _seed_device_file(connection, 13)
            connection.execute("UPDATE device_files SET original_device_file_id=13 WHERE id=12")
        elif case in {"missing_existing", "existing_ref_to_new_id"}:
            drafts[1] = replace(drafts[1], original_batch_file_id=None,
                                original_output_id=999 if case == "missing_existing" else 1)
        else:
            _seed_device_file(connection, 13, role=3)
            connection.execute("UPDATE device_files SET original_device_file_id=11, pairing_evidence_json='{}' WHERE id=13")
            drafts.append(replace(drafts[1], file=FileReference(device_file_id=13)))
        connection.commit()
        before = tuple(connection.iterdump())
        result = CaptureRepository().finish_capture(_finish(drafts=tuple(drafts)), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(connection.iterdump()) == before
    finally:
        connection.close()
