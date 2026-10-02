"""新键申请复用完整准备责任，坏关联不能由取消或后续状态掩盖。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.qualification import OperationConfig, QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command
from .test_local_read import local_read
from .test_qualification import _seed_output, _seed_selection_and_item
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture(params=["device", "host", "internal"])
def prepared_read(request):
    if request.param == "host":
        owned, command = request.getfixturevalue("local_read")
    else:
        owned = request.getfixturevalue("read_targets")
        command = _command(internal=request.param == "internal")
    if request.param == "internal":
        owned.connection.execute("DELETE FROM obtain_items")
        owned.connection.execute("DELETE FROM outputs")
    else:
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    return owned, command, result.value


def _apply_state(owned, original, state):
    connection = owned.connection
    if state == "released_slot":
        connection.execute("UPDATE file_copies SET slot_device_id=NULL WHERE id=?", (original.copy_id,))
    elif state == "failed_run":
        connection.execute(
            "UPDATE operation_runs SET status=4, attempts_used=1, error_json='{}' WHERE id=?", (original.run_id,))
        connection.execute("UPDATE file_copies SET slot_device_id=NULL WHERE id=?", (original.copy_id,))
    elif state == "progress":
        connection.execute("UPDATE file_copies SET committed_bytes=1024 WHERE id=?", (original.copy_id,))
        connection.execute("UPDATE operation_runs SET status=2, attempts_used=1 WHERE id=?", (original.run_id,))
    elif state == "source_missing":
        connection.execute("UPDATE device_files SET presence_state=3 WHERE id=501")
        connection.execute("UPDATE outputs SET availability=4, error_json='{}' WHERE id=701")
    elif state == "source_unknown":
        connection.execute("UPDATE device_files SET presence_state=1 WHERE id=501")
        connection.execute("UPDATE outputs SET availability=5, error_json='{}' WHERE id=701")
    connection.commit()


@pytest.mark.parametrize("state", ["initial", "released_slot", "failed_run", "progress", "source_missing", "source_unknown"])
def test_new_key_returns_all_original_identities_without_writes(prepared_read, state):
    owned, command, original = prepared_read
    _apply_state(owned, original, state)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    assert (result.value.copy_id, result.value.run_id, result.value.delivery_id, result.value.target_file_id) == (
        original.copy_id, original.run_id, original.delivery_id, original.target_file_id)
    assert tuple(owned.connection.iterdump()) == before


def test_new_key_does_not_replace_adopted_config_or_names(prepared_read):
    owned, command, original = prepared_read
    later = replace(command, target_extension="bin", occurred_at=command.occurred_at + 1,
                    config=OperationConfig(5, Decimal("20"), Decimal("3")) if command.config else None,
                    delivery_extension="mov" if command.item_id else None,
                    delivery_display_name="新名称" if command.item_id else None)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(later, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.target_file_id == original.target_file_id
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("ending", ["canceled", "terminal"])
def test_ineligible_action_keeps_complete_responsibility_without_grant(prepared_read, ending):
    owned, command, _ = prepared_read
    statement = "cancel_requested=1" if ending == "canceled" else "status=3"
    owned.connection.execute(f"UPDATE actions SET {statement} WHERE id=?", (command.action_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert tuple(owned.connection.iterdump()) == before


def _break_responsibility(owned, command, original, fault):
    connection = owned.connection
    if fault == "missing_run":
        connection.execute("DELETE FROM operation_runs WHERE id=?", (original.run_id,))
    elif fault == "missing_copy":
        connection.execute("DELETE FROM operation_runs WHERE id=?", (original.run_id,))
        connection.execute("DELETE FROM file_copies WHERE id=?", (original.copy_id,))
    elif fault == "run_owner":
        connection.execute("UPDATE operation_runs SET action_id=12 WHERE id=?", (original.run_id,))
    elif fault == "run_key":
        connection.execute("UPDATE operation_runs SET responsibility_key='read/999' WHERE id=?", (original.run_id,))
    elif fault == "source":
        connection.execute("UPDATE file_copies SET source_device_file_id=502, source_intermediate_file_id=NULL WHERE id=?",
                           (original.copy_id,))
    elif fault == "length":
        connection.execute("UPDATE file_copies SET source_size=4097 WHERE id=?", (original.copy_id,))
    elif fault == "target_owner":
        connection.execute("UPDATE intermediate_files SET owner_action_id=12, owner_delivery_id=NULL, purpose=2 WHERE id=?",
                           (original.target_file_id,))
    elif fault == "target_path":
        connection.execute("UPDATE intermediate_files SET relative_path='recording-inputs/999.part' WHERE id=?",
                           (original.target_file_id,))
    elif fault == "duplicate_target":
        connection.execute(
            "INSERT INTO intermediate_files SELECT 999, owner_action_id, owner_delivery_id, purpose,"
            " 'recording-inputs/999.part', retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
            " created_event_id, last_event_id, change_count FROM intermediate_files WHERE id=?", (original.target_file_id,))
    connection.commit()


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("fault", ["missing_run", "missing_copy", "run_owner", "run_key", "source", "length",
                                   "target_owner", "target_path", "duplicate_target"])
def test_incomplete_or_mismatched_responsibility_is_an_error_before_status(prepared_read, fault, canceled):
    owned, command, original = prepared_read
    _break_responsibility(owned, command, original, fault)
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("state", ["dependency_released", "cleanup_started", "delivery_failed"])
def test_delivery_recovery_preserves_source_dependency(local_read, state):
    owned, command = local_read
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    if state == "dependency_released":
        owned.connection.execute("UPDATE obtain_items SET source_dependency=0 WHERE id=101")
        owned.connection.execute("UPDATE deliveries SET status=3 WHERE id=?", (first.value.delivery_id,))
        owned.connection.execute(
            "UPDATE file_copies SET committed_bytes=source_size, verification_state=3,"
            " target_sha256=source_sha256, slot_device_id=NULL WHERE id=?", (first.value.copy_id,))
        owned.connection.execute("UPDATE operation_runs SET status=3 WHERE id=?", (first.value.run_id,))
        owned.connection.execute("UPDATE intermediate_files SET size_bytes=4096, sha256=? WHERE id=?",
                                 ("a" * 64, first.value.target_file_id))
    elif state == "cleanup_started":
        from .test_read_rejections import _cleanup, _availability
        _cleanup(owned.connection, 2, 2)
        _availability(owned.connection, command, 2, 2)
    else:
        owned.connection.execute("UPDATE deliveries SET status=6, error_json='{}' WHERE id=?", (first.value.delivery_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    later = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert later.kind is DbOutcomeKind.COMPLETED, later.error
    assert later.value.target_file_id == first.value.target_file_id
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("fault", ["item_link", "delivery_owner", "delivery_output", "delivery_name", "run_delivery"])
def test_delivery_recovery_requires_original_owner_and_item_link(local_read, fault):
    owned, command = local_read
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    if fault == "item_link":
        owned.connection.execute("UPDATE obtain_items SET status=2, delivery_id=NULL, source_dependency=0 WHERE id=101")
    elif fault == "delivery_owner":
        owned.connection.execute("UPDATE deliveries SET action_id=32 WHERE id=?", (first.value.delivery_id,))
    elif fault == "delivery_output":
        owned.connection.execute("UPDATE deliveries SET output_id=702 WHERE id=?", (first.value.delivery_id,))
    elif fault == "delivery_name":
        owned.connection.execute("UPDATE deliveries SET file_name='999.mp4' WHERE id=?", (first.value.delivery_id,))
    else:
        owned.connection.execute("UPDATE operation_runs SET delivery_id=NULL WHERE id=?", (first.value.run_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("prefix", ["SELECT id FROM file_copies", "SELECT id FROM operation_runs"])
def test_recovery_query_failure_never_becomes_missing_responsibility(prepared_read, prefix):
    owned, command, _ = prepared_read
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(),
        replace(owned, connection=_FaultConnection(owned.connection, prefix)))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("copy_checksum,source_checksum", [
    (None, None), (None, "b" * 64), ("a" * 64, None), ("a" * 64, "a" * 64), ("a" * 64, "b" * 64),
])
def test_recovery_checks_known_checksums_without_inventing_unknown_values(prepared_read, canceled, copy_checksum, source_checksum):
    owned, command, original = prepared_read
    owned.connection.execute("UPDATE file_copies SET source_sha256=? WHERE id=?", (copy_checksum, original.copy_id))
    table = "device_files" if command.source_device_file_id else "intermediate_files"
    source_id = command.source_device_file_id or command.source_intermediate_file_id
    if command.source_device_file_id is not None:
        owned.connection.execute("UPDATE device_files SET checksum_support=2 WHERE id=?", (source_id,))
    owned.connection.execute(f"UPDATE {table} SET sha256=? WHERE id=?", (source_checksum, source_id))
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    if copy_checksum is not None and source_checksum is not None and copy_checksum != source_checksum:
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, ConsistencyError), result.error
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.outcome is (QualificationOutcome.REJECTED if canceled else QualificationOutcome.GRANTED)
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("wrong_delivery", [False, True])
def test_internal_original_flow_cannot_be_hidden_by_wrong_copy_ownership(read_targets, canceled, wrong_delivery):
    owned = read_targets
    command = _command(internal=True)
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.commit()
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    owned.connection.execute("UPDATE file_copies SET processing_id=6, slot_device_id=NULL WHERE id=?", (first.value.copy_id,))
    owned.connection.execute("UPDATE intermediate_files SET owner_action_id=12 WHERE id=?", (first.value.target_file_id,))
    owned.connection.execute("UPDATE operation_runs SET status=3 WHERE id=?", (first.value.run_id,))
    if wrong_delivery:
        # 另一份真实交付作为错误外键目标；错误不依赖缺失外键或非法行内组合。
        _seed_output(owned.connection, 702, 12, 502)
        owned.connection.execute("DELETE FROM obtain_source_selections WHERE id=32")
        owned.connection.execute("DELETE FROM action_dependencies WHERE id=32")
        _seed_selection_and_item(owned.connection, dependency_id=32, selection_id=32, item_id=102,
                                  owner_action_id=32, source_action_id=12, output_id=702)
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
        owned.connection.commit()
        other = OutputsRepository().grant_file(replace(_command(), action_id=32, item_id=102,
                                                      output_id=702, source_device_file_id=502), new_operation_key(), owned)
        assert other.kind is DbOutcomeKind.COMPLETED, other.error
        assert other.value.outcome is QualificationOutcome.GRANTED
        owned.connection.execute("UPDATE operation_runs SET delivery_id=? WHERE id=?",
                                 (other.value.delivery_id, first.value.run_id))
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=11")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before
