"""读取建档原键核实完整原组及原输入，并恢复首次响应。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind
from camctl.outputs.qualification import OperationConfig, QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionError

from .test_read_associations import read_targets, _command
from .test_local_read import local_read
from .test_read_rejections import target, _availability
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture(params=["device", "host", "internal"])
def granted(request):
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
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.outcome is QualificationOutcome.GRANTED
    return owned, command, key, first.value


def _reuse(owned, command, key):
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, key, owned)
    assert tuple(owned.connection.iterdump()) == before
    return result


def test_original_key_returns_exact_first_grant(granted):
    owned, command, key, first = granted
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first


def _later_attempt(owned, command, first):
    intent = AttemptIntent(operation="read", action_id=command.action_id, kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=first.copy_id), query_purpose=None,
        config=AttemptConfig(5, Decimal("23.000000000000000001"), Decimal("2.1")) if command.config else AttemptConfig(1),
        occurred_at=command.occurred_at + 1, copy_round=1)
    key = new_operation_key()
    result = OperationRepository().begin_attempt(intent, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.disposition is BeginDisposition.GRANTED
    return key


def test_grant_response_survives_real_later_read_intent_and_adopted_config(granted):
    owned, command, key, first = granted
    _later_attempt(owned, command, first)
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first


def test_read_attempt_key_cannot_be_used_as_file_grant_key(granted):
    owned, command, _, first = granted
    attempt_key = _later_attempt(owned, command, first)
    result = _reuse(owned, command, attempt_key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


@pytest.mark.parametrize("state", ["canceled", "terminal", "source_unknown", "released"])
def test_original_grant_does_not_redecide_current_eligibility(granted, state):
    owned, command, key, first = granted
    if state == "canceled":
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    elif state == "terminal":
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=?", (command.action_id,))
    elif state == "source_unknown":
        if command.source_device_file_id:
            owned.connection.execute("UPDATE device_files SET presence_state=1 WHERE id=?", (command.source_device_file_id,))
        if command.output_id:
            owned.connection.execute("UPDATE outputs SET availability=5, error_json='{}' WHERE id=?", (command.output_id,))
    else:
        owned.connection.execute("UPDATE file_copies SET slot_device_id=NULL WHERE id=?", (first.copy_id,))
        if command.item_id:
            owned.connection.execute("UPDATE obtain_items SET source_dependency=0 WHERE id=?", (command.item_id,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first


@pytest.mark.parametrize("field", ["action_id", "source", "occurred_at", "target_extension"])
def test_grant_key_rejects_changed_shared_input(granted, field):
    owned, command, key, _ = granted
    if field == "source":
        changes = ({"source_device_file_id": 502} if command.source_device_file_id
                   else {"source_intermediate_file_id": 999})
    else:
        changes = {field: {"action_id": 12, "occurred_at": command.occurred_at + 1, "target_extension": "bin"}[field]}
    result = _reuse(owned, replace(command, **changes), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


@pytest.mark.parametrize("field", ["max_attempts", "timeout_s", "retry_interval_s"])
def test_grant_key_rejects_changed_adopted_device_config(read_targets, field):
    owned, command = read_targets, _command()
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    changes = {field: {"max_attempts": 4, "timeout_s": Decimal("10.000000000000000001"),
                       "retry_interval_s": Decimal("0.000000000000000001")}[field]}
    result = _reuse(owned, replace(command, config=replace(command.config, **changes)), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


@pytest.mark.parametrize("field,value", [("item_id", 102), ("output_id", 702),
    ("delivery_extension", "mov"), ("delivery_display_name", "另一名称")])
def test_grant_key_rejects_changed_delivery_input(local_read, field, value):
    owned, command = local_read
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    result = _reuse(owned, replace(command, **{field: value}), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


def test_internal_grant_key_rejects_other_processing(read_targets):
    owned, command = read_targets, _command(internal=True)
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.commit()
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    result = _reuse(owned, replace(command, processing_id=6), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


def _change_event(owned, key, table, change):
    with closing(owned.connection.execute(
        "SELECT e.id, e.body_json FROM history_events e JOIN history_transactions t"
        " ON e.transaction_id=t.id WHERE t.operation_key=? ORDER BY e.id", (str(key),),
    )) as cursor:
        events = cursor.fetchall()
    for event_id, raw in events:
        body = json.loads(raw)
        if body["rows"][0]["table"] == table:
            change(body)
            owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?", (json.dumps(body), event_id))
            owned.connection.commit()
            return
    raise AssertionError(table)


@pytest.mark.parametrize("fault", ["extra_target", "wrong_copy_id", "run_target", "source_size", "initial_attempts", "copy_update"])
def test_grant_reuse_rejects_incomplete_or_inconsistent_original_group(granted, fault):
    owned, command, key, first = granted
    def change(body):
        row = body["rows"][0]
        if fault == "extra_target":
            body["rows"].append({**row, "id": 999})
        elif fault == "wrong_copy_id":
            row["id"] = 999
        elif fault == "copy_update":
            body["reason"] = 7
            row["before"] = {"exists": True, "values": {"max_recopies_used": 0}}
            row["after"] = {"exists": True, "values": {"max_recopies_used": 1}}
        else:
            name, value = {"run_target": ("copy_id", 999), "source_size": ("source_size", 4097),
                           "initial_attempts": ("attempts_used", 1)}[fault]
            row["after"]["values"][name] = value
    table = ("intermediate_files" if fault == "extra_target" else
             "operation_runs" if fault in ("run_target", "initial_attempts") else "file_copies")
    _change_event(owned, key, table, change)
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, (ConsistencyError, TransactionError)), result.error


@pytest.mark.parametrize("fault", ["missing_run", "target_path", "run_owner", "created_event"])
def test_original_grant_still_requires_fixed_current_relations(granted, fault):
    owned, command, key, first = granted
    if fault == "missing_run":
        owned.connection.execute("DELETE FROM operation_runs WHERE id=?", (first.run_id,))
    elif fault == "target_path":
        directory = "recording-inputs" if command.processing_id else "deliveries"
        owned.connection.execute("UPDATE intermediate_files SET relative_path=? WHERE id=?",
                                 (f"{directory}/{first.target_file_id}.bin", first.target_file_id))
    elif fault == "run_owner":
        owned.connection.execute("UPDATE operation_runs SET action_id=12 WHERE id=?", (first.run_id,))
    else:
        owned.connection.execute("UPDATE intermediate_files SET created_event_id=1 WHERE id=?", (first.target_file_id,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


def test_original_group_read_error_does_not_create_new_grant(granted):
    owned, command, key, _ = granted
    before = tuple(owned.connection.iterdump())
    fault = replace(owned, connection=_FaultConnection(owned.connection, "SELECT id, transaction_id, event_type"))
    result = OutputsRepository().grant_file(command, key, fault)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_original_snapshot_is_not_replaced_by_later_known_checksum(granted):
    owned, command, key, first = granted
    checksum = "a" * 64 if command.source_intermediate_file_id else "b" * 64
    if command.source_device_file_id:
        owned.connection.execute("UPDATE device_files SET checksum_support=2, sha256=? WHERE id=?",
                                 (checksum, command.source_device_file_id))
    owned.connection.execute("UPDATE file_copies SET source_sha256=? WHERE id=?", (checksum, first.copy_id))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first


def test_original_known_checksum_cannot_be_cleared(local_read):
    owned, command = local_read
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    owned.connection.execute("UPDATE file_copies SET source_sha256=NULL WHERE id=?", (first.value.copy_id,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.fixture
def rejected(target):
    owned, command = target
    _availability(owned.connection, command, 4)
    owned.connection.commit()
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.outcome is QualificationOutcome.REJECTED_FINAL
    return owned, command, key, first.value


def test_rejection_key_returns_original_error_after_source_changes(rejected):
    owned, command, key, first = rejected
    _availability(owned.connection, command, 1)
    owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == first


@pytest.mark.parametrize("field,value", [("item_id", 102), ("output_id", 702), ("occurred_at", 1)])
def test_rejection_key_rejects_changed_used_target_or_time(rejected, field, value):
    owned, command, key, _ = rejected
    result = _reuse(owned, replace(command, **{field: value}), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, (ConsistencyError, TransactionError)), result.error
