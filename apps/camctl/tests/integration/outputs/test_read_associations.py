"""读取资格在等待、取消或建档前核对持久化对象的固定关联。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.json_values import parse_exact_json
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository

from ..operations.test_result_reuse import _FaultConnection
from .test_qualification import (
    _NOW, _seed_action, _seed_device_file, _seed_environment, _seed_output,
    _seed_plan, _seed_processing, _seed_selection_and_item,
)


@pytest.fixture
def read_targets(tmp_path):
    _, owned = _seed_environment(tmp_path)
    try:
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        for action_id in (11, 12):
            _seed_action(connection, action_id, 1, action_type=2)
            _seed_device_file(connection, action_id + 490, action_id)
            _seed_output(connection, action_id + 690, action_id, action_id + 490)
        for action_id in (31, 32):
            _seed_action(connection, action_id, 1, action_type=4)
            _seed_selection_and_item(
                connection, dependency_id=action_id, selection_id=action_id,
                item_id=action_id + 70, owner_action_id=action_id,
                source_action_id=action_id - 20, output_id=action_id + 670,
            )
        _seed_processing(connection, 5, 11, 501)
        _seed_processing(connection, 6, 12, 502)
        connection.execute(
            "UPDATE recording_processing SET check_decision=3, check_basis_json='{}'"
        )
        connection.commit()
        register_operation_guards()
        yield owned
    finally:
        owned.connection.close()


def _command(*, internal=False):
    return FileCandidate(
        action_id=11 if internal else 31, item_id=None if internal else 101,
        processing_id=5 if internal else None, output_id=None if internal else 701,
        source_device_file_id=501,
        target_relative_path="recording-inputs/1.part" if internal else "deliveries/1.part",
        delivery_file_name="1.mp4", delivery_display_name="录像",
        config=OperationConfig(3, Decimal("10"), Decimal("0")), occurred_at=_NOW,
    )


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("fault", [
    "item", "output", "source", "selection_owner", "source_owner", "unfixed_source", "unfixed_selection",
])
def test_delivery_rejects_mixed_fixed_associations_before_status(read_targets, canceled, fault):
    owned = read_targets
    command = _command()
    if fault == "item":
        command = replace(command, item_id=102)
    elif fault == "output":
        command = replace(command, output_id=702, source_device_file_id=502)
    elif fault == "source":
        command = replace(command, source_device_file_id=502)
    elif fault == "selection_owner":
        owned.connection.execute("UPDATE action_dependencies SET action_id=32 WHERE id=31")
    elif fault == "source_owner":
        owned.connection.execute("UPDATE outputs SET source_action_id=12 WHERE id=701")
    elif fault == "unfixed_source":
        owned.connection.execute("UPDATE actions SET source_resolution_state=1, resolved_source_plan_id=NULL WHERE id=31")
    else:
        owned.connection.execute("UPDATE obtain_source_selections SET status=1 WHERE id=31")
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("fault", ["processing", "file", "observer_device", "observer_driver"])
def test_internal_read_rejects_mixed_fixed_associations_before_status(read_targets, canceled, fault):
    owned = read_targets
    command = _command(internal=True)
    if fault == "processing":
        command = replace(command, processing_id=6)
    elif fault == "file":
        command = replace(command, source_device_file_id=502)
    else:
        owned.connection.execute("UPDATE device_files SET observer_action_id=12 WHERE id=501")
        if fault == "observer_device":
            owned.connection.execute("UPDATE actions SET device_id='cam-2' WHERE id=12")
        else:
            owned.connection.execute("UPDATE actions SET driver_id='other-driver' WHERE id=12")
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=11")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_internal_read_does_not_require_formal_output(read_targets):
    owned = read_targets
    # 此录像还在内部检查阶段；取回动作在未来，不参与当前资格。
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE type=4", (_NOW + 60_000_000,))
    owned.connection.commit()
    result = OutputsRepository().grant_file(_command(internal=True), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    with closing(owned.connection.execute("SELECT count(*) FROM deliveries")) as cursor:
        assert cursor.fetchone() == (0,)


def test_delivery_creation_saves_complete_owner_chain(read_targets):
    owned = read_targets
    command = replace(_command(), config=OperationConfig(3, Decimal("1.234567890123456789"), Decimal("0")))
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED
    with closing(owned.connection.execute(
        "SELECT action_id, output_id, withdrawal_state, withdrawal_error_json FROM deliveries"
    )) as cursor:
        assert cursor.fetchall() == [(31, 701, 1, None)]
    with closing(owned.connection.execute(
        "SELECT delivery_id, source_dependency FROM obtain_items WHERE id=101"
    )) as cursor:
        assert cursor.fetchone() == (result.value.delivery_id, 1)
    with closing(owned.connection.execute(
        "SELECT timeout_s_json, retry_interval_s_json, attempts_used FROM operation_runs"
    )) as cursor:
        timeout, interval, attempts = cursor.fetchone()
    assert parse_exact_json(timeout) == Decimal("1.234567890123456789")
    assert parse_exact_json(interval) == 0
    assert attempts == 0


@pytest.mark.parametrize("prefix", ["INSERT INTO deliveries", "UPDATE obtain_items SET"])
def test_delivery_creation_write_failure_preserves_original_selection(read_targets, prefix):
    owned = read_targets
    before = tuple(owned.connection.iterdump())
    failing = replace(owned, connection=_FaultConnection(owned.connection, prefix))
    key = new_operation_key()
    result = OutputsRepository().grant_file(_command(), key, failing)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.in_transaction is False
    retry = OutputsRepository().grant_file(_command(), key, owned)
    assert retry.kind is DbOutcomeKind.COMPLETED, retry.error


@pytest.mark.parametrize("internal,field", [
    (False, "action_id"), (False, "item_id"), (False, "output_id"),
    (True, "processing_id"), (True, "source_device_file_id"),
])
def test_read_rejects_missing_fixed_record_without_writes(read_targets, internal, field):
    owned = read_targets
    command = replace(_command(internal=internal), **{field: 999})
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("role", [1, 3])
def test_original_read_rejects_undetermined_or_preview_source(read_targets, internal, canceled, role):
    owned = read_targets
    command = _command(internal=internal)
    owned.connection.execute("UPDATE device_files SET role=? WHERE id=501", (role,))
    if canceled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_delivery_can_read_explicit_preview_with_confirmed_role(read_targets):
    owned = read_targets
    connection = owned.connection
    _seed_device_file(connection, 503, 11)
    _seed_output(connection, 703, 11, 503)
    connection.execute(
        "UPDATE device_files SET role=3, original_device_file_id=503,"
        " pairing_evidence_json='{\"method\":1,\"observation\":{}}' WHERE id=501"
    )
    connection.execute("UPDATE outputs SET kind=3 WHERE id=701")
    connection.execute("INSERT INTO output_origins (output_id, original_output_id) VALUES (701, 703)")
    connection.execute("UPDATE obtain_items SET basis=5, requested_output_id=701 WHERE id=101")
    connection.execute("UPDATE actions SET execution_spec_json='{\"selection_mode\":3}' WHERE id=31")
    connection.commit()
    result = OutputsRepository().grant_file(_command(), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED


def test_device_file_can_have_another_observer_with_same_binding(read_targets):
    owned = read_targets
    owned.connection.execute("UPDATE device_files SET observer_action_id=12 WHERE id=501")
    owned.connection.commit()
    result = OutputsRepository().grant_file(_command(), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("invalid", [None, {}, object()])
def test_repository_rejects_untyped_candidate_before_key_reuse(read_targets, saved, invalid):
    owned = read_targets
    repository = OutputsRepository()
    key = new_operation_key()
    if saved:
        first = repository.grant_file(_command(), key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    result = repository.grant_file(invalid, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, TypeError), result.error
    assert tuple(owned.connection.iterdump()) == before
