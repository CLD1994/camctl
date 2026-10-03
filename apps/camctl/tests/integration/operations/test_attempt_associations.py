"""普通流程沿固定对象关系核对身份，联合创建保持事务完整。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import CommandPlan, commit_operation, event_envelope, row_change, row_facts
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind, QueryPurpose

from ..outputs.test_qualification import (
    _NOW, _seed_action, _seed_device_file, _seed_environment, _seed_output, _seed_plan,
    _seed_processing,
)
from .test_attempts import _delete_intent, _seed_environment as _cleanup_environment
from .test_queries import _environment as _query_environment, _intent as _query_intent
from .test_result_reuse import _finish, _FaultConnection


@pytest.fixture
def read_environment(tmp_path):
    _, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_processing(connection, 801, 11, 501)
    # 内部输入建档要求检查构成独立输入需求；未决决定按需求规则不授予。
    connection.execute(
        "UPDATE recording_processing SET check_decision=3, check_basis_json='{}' WHERE id=801")
    connection.execute(
        "INSERT INTO deliveries (id, action_id, output_id, file_name, display_name, status,"
        " publication_intent_event_id, published_event_id, withdrawal_state, withdrawal_error_json,"
        " error_json, created_event_id, last_event_id, change_count)"
        " VALUES (901, 31, 701, 'video.mp4', '录像', 1, NULL, NULL, 1, NULL, NULL, 1, 1, 1)"
    )
    for file_id, action_id, delivery_id, purpose in ((601, None, 901, 1), (602, 11, None, 2)):
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id, purpose,"
            " relative_path, retention_state, cleanup_state, size_bytes, sha256, last_error_json,"
            " created_event_id, last_event_id, change_count)"
            " VALUES (?, ?, ?, ?, ?, 1, 1, NULL, NULL, NULL, 1, 1, 1)",
            (file_id, action_id, delivery_id, purpose, f"copy/{file_id}.part"),
        )
    connection.commit()
    register_operation_guards()
    register_outputs_guards()
    try:
        yield owned
    finally:
        owned.connection.close()


def _read_rows(internal=False):
    delivery_id, processing_id = (None, 801) if internal else (901, None)
    return (
        row_change("operation_runs", 41, {
            "action_id": 11 if internal else 31, "delivery_id": delivery_id, "kind": 3,
            "query_purpose": None, "responsibility_key": "read/51", "activity_id": None,
            "copy_id": 51, "cleanup_item_id": None, "session_key": None,
            "status": 1, "attempts_used": 0, "max_attempts_used": 3,
            "timeout_s_json": Decimal("5"), "retry_interval_s_json": Decimal("1"),
            "retry_wait_required": 0, "error_json": None,
        }),
        row_change("file_copies", 51, {
            "delivery_id": delivery_id, "processing_id": processing_id,
            "source_device_file_id": 501, "source_intermediate_file_id": None,
            "target_file_id": 602 if internal else 601, "round": 1, "recopies_used": 0,
            "max_recopies_used": 0, "source_size": 4096, "source_sha256": None,
            "committed_bytes": 0, "reset_state": 1, "slot_device_id": None,
            "verification_state": 1, "target_sha256": None, "verification_error_json": None,
        }),
    )


class CreateReadAndCopy:
    """仅组合建档事务；真实业务守卫与内核保持启用。"""

    def __init__(self, internal=False, copy_first=False, rows=None):
        self.internal = internal
        run, copy = rows if rows is not None else _read_rows(internal)
        self.rows = (copy, run) if copy_first else (run, copy)

    def plan(self, scope):
        allocation = scope.allocate(len(self.rows))
        owner = ("action", 11) if self.internal else ("delivery", 901)
        state = {"file_copies": {}, "outputs": {}}
        for table, identities in (("actions", (11, 31)), ("deliveries", (901,)),
                                  ("recording_processing", (801,)), ("intermediate_files", (601, 602))):
            state[table] = {identity: row_facts(scope.connection, table, identity) for identity in identities}
        return CommandPlan(
            events=tuple(event_envelope(allocation.first_event_id + index, allocation.txn_id,
                                       10 if row.table == "operation_runs" else 22, 1, (row,), _NOW)
                         for index, row in enumerate(self.rows)),
            owners={(row.table, row.row_id): owner for row in self.rows}, state_rows=state,
        )


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("copy_first", [False, True])
def test_read_flow_and_copy_can_be_jointly_created_in_either_order(read_environment, internal, copy_first):
    owned = read_environment
    result = commit_operation(CreateReadAndCopy(internal, copy_first), new_operation_key(), owned)
    assert result.kind == "completed", result.error
    assert owned.connection.execute("SELECT action_id, delivery_id, copy_id FROM operation_runs WHERE id=41").fetchone() == (
        11 if internal else 31, None if internal else 901, 51)
    assert owned.connection.execute("SELECT delivery_id, processing_id FROM file_copies WHERE id=51").fetchone() == (
        None if internal else 901, 801 if internal else None)
    assert owned.connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("fault", ["foreign_action", "wrong_delivery", "missing_parent"])
def test_joint_read_creation_rejects_inconsistent_parent_before_commit(read_environment, internal, fault):
    owned = read_environment
    run, copy = _read_rows(internal)
    if fault == "foreign_action":
        run = replace(run, after=replace(run.after, values=dict(run.after.values, action_id=31 if internal else 11)))
    elif fault == "wrong_delivery":
        run = replace(run, after=replace(run.after, values=dict(run.after.values, delivery_id=901 if internal else None)))
    else:
        copy = replace(copy, after=replace(copy.after, values=dict(copy.after.values,
            **({"processing_id": 999} if internal else {"delivery_id": 999}))))
    before = tuple(owned.connection.iterdump())
    result = commit_operation(CreateReadAndCopy(internal, rows=(run, copy)), new_operation_key(), owned)
    assert result.kind == "rolled_back", result.error
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("missing", ["copy", "flow"])
def test_joint_read_creation_requires_both_records(read_environment, internal, missing):
    owned = read_environment
    command = CreateReadAndCopy(internal)
    command.rows = tuple(row for row in command.rows if row.table != ("file_copies" if missing == "copy" else "operation_runs"))
    before = tuple(owned.connection.iterdump())
    result = commit_operation(command, new_operation_key(), owned)
    assert result.kind == "rolled_back", result.error
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("internal", [False, True])
def test_joint_read_creation_rolls_back_when_copy_projection_write_fails(read_environment, internal):
    owned = read_environment
    before = tuple(owned.connection.iterdump())
    fault = _FaultConnection(owned.connection, "INSERT INTO file_copies")
    result = commit_operation(CreateReadAndCopy(internal), new_operation_key(), replace(owned, connection=fault))
    assert result.kind == "rolled_back", result.error
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.in_transaction is False


def _read_intent(internal=False):
    return AttemptIntent(operation="read", action_id=11 if internal else 31,
                         kind=OperationKind.READ_FILE, target=AttemptTarget(copy_id=51),
                         query_purpose=None, config=AttemptConfig(3, "5", "1"),
                         copy_round=1, occurred_at=_NOW)


@pytest.mark.parametrize("internal", [False, True])
def test_existing_copy_does_not_allow_replacement_read_flow(read_environment, internal):
    owned = read_environment
    result = commit_operation(CreateReadAndCopy(internal), new_operation_key(), owned)
    assert result.kind == "completed", result.error
    owned.connection.execute("DELETE FROM operation_runs WHERE id=41")
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_read_intent(internal), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("internal", [False, True])
def test_legal_read_uses_consumer_parent_and_original_flow(read_environment, internal):
    owned = read_environment
    created = commit_operation(CreateReadAndCopy(internal), new_operation_key(), owned)
    assert created.kind == "completed", created.error
    # 消费端以已持有机会为前提；共同建档本身仍保存空机会。
    owned.connection.execute("UPDATE file_copies SET slot_device_id='cam-1' WHERE id=51")
    owned.connection.commit()
    repository = OperationRepository()
    key = new_operation_key()
    intent = _read_intent(internal)
    begun = repository.begin_attempt(intent, key, owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    assert begun.value.disposition is BeginDisposition.GRANTED
    assert (begun.value.ticket.run_id, begun.value.ticket.target_id) == (41, "51")
    owned.connection.execute("UPDATE operation_runs SET status=3, timeout_s_json='9', retry_interval_s_json='2' WHERE id=41")
    before = tuple(owned.connection.iterdump())
    again = repository.begin_attempt(intent, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == begun.value
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("stage", ["old", "new", "ended", "exhausted"])
def test_cleanup_owner_is_checked_before_existing_run_disposition(tmp_path, stage):
    owned = _cleanup_environment(tmp_path)
    try:
        repository = OperationRepository()
        key = new_operation_key()
        intent = _delete_intent()
        begun = repository.begin_attempt(intent, key, owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        _seed_action(owned.connection, 9, 1, action_type=5)
        owned.connection.execute("UPDATE cleanup_items SET action_id=9 WHERE id=1")
        if stage == "ended":
            owned.connection.execute("UPDATE operation_runs SET status=3 WHERE id=?", (begun.value.ticket.run_id,))
        if stage == "exhausted":
            intent = replace(intent, config=AttemptConfig(1, "4", "2"))
        before = tuple(owned.connection.iterdump())
        result = repository.begin_attempt(intent, key if stage == "old" else new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert isinstance(result.error, ConsistencyError), result.error
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def _add_foreign_activity(connection):
    from camctl.persistence.transaction import encode_json_value

    _seed_action(connection, 9, 1, action_type=2)
    facts = row_facts(connection, "device_activities", 1)
    facts.update(id=4, action_id=9, task_key="9" * 32)
    columns = tuple(facts)
    values = tuple(encode_json_value(facts[name]) if name.endswith("_json") and facts[name] is not None
                   else facts[name] for name in columns)
    connection.execute(f"INSERT INTO device_activities ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)


@pytest.mark.parametrize("kind,run_id,operation", [(OperationKind.START, 1, "control"),
                                                  (OperationKind.STOP, 2, "stop")])
def test_wrong_saved_key_does_not_create_second_actual_responsibility(tmp_path, kind, run_id, operation):
    owned = _query_environment(tmp_path)
    try:
        owned.connection.execute("UPDATE operation_runs SET responsibility_key=? WHERE id=?", (f"damaged/{run_id}", run_id))
        intent = AttemptIntent(operation, 1, kind, AttemptTarget(activity_id=1), None,
                               AttemptConfig(3, "5", "1"), _NOW)
        before = tuple(owned.connection.iterdump())
        result = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert isinstance(result.error, ConsistencyError), result.error
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("purpose,original_id", [(QueryPurpose.START_CONFIRMATION, 1), (QueryPurpose.STOP_CONFIRMATION, 2)])
@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong_key"])
def test_query_confirmation_rejects_missing_or_inconsistent_original(tmp_path, purpose, original_id, fault):
    owned = _query_environment(tmp_path)
    try:
        connection = owned.connection
        if fault == "missing":
            connection.execute("DELETE FROM operation_attempts WHERE run_id=?", (original_id,))
            connection.execute("DELETE FROM operation_runs WHERE id=?", (original_id,))
        elif fault == "wrong_key":
            connection.execute("UPDATE operation_runs SET responsibility_key=? WHERE id=?", ("wrong/original", original_id))
        else:
            connection.execute(
                "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose, responsibility_key,"
                " activity_id, copy_id, cleanup_item_id, session_key, status, attempts_used, max_attempts_used,"
                " timeout_s_json, retry_interval_s_json, retry_wait_required, error_json)"
                " SELECT 3, action_id, delivery_id, kind, query_purpose, 'wrong/original', activity_id, copy_id,"
                " cleanup_item_id, session_key, 1, 0, max_attempts_used, timeout_s_json, retry_interval_s_json,"
                " 0, error_json FROM operation_runs WHERE id=?", (original_id,))
        before = tuple(connection.iterdump())
        result = OperationRepository().begin_attempt(_query_intent(purpose), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert isinstance(result.error, ConsistencyError), result.error
        assert tuple(connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_residual_trigger_and_confirmation_keep_historical_activity_owner(tmp_path):
    owned = _query_environment(tmp_path)
    try:
        _seed_action(owned.connection, 9, 1, action_type=2)
        repository = OperationRepository()
        for kind, purpose, operation in ((OperationKind.STOP_RESIDUAL, None, "stop"),
                                        (OperationKind.QUERY_ACTIVITY, QueryPurpose.RESIDUAL_STOP_CONFIRMATION, "query")):
            intent = AttemptIntent(operation, 9, kind, AttemptTarget(activity_id=1), purpose,
                                   AttemptConfig(3, "5", "1"), _NOW)
            key = new_operation_key()
            begun = repository.begin_attempt(intent, key, owned)
            assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
            assert begun.value.disposition is BeginDisposition.GRANTED
            before = tuple(owned.connection.iterdump())
            repeated = repository.begin_attempt(intent, key, owned)
            assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
            assert repeated.value == begun.value
            assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute("SELECT action_id FROM device_activities WHERE id=1").fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("stage", ["old", "new", "ended", "exhausted"])
def test_stop_input_cannot_replace_original_activity(tmp_path, stage):
    owned = _query_environment(tmp_path)
    try:
        connection = owned.connection
        connection.execute("DELETE FROM operation_attempts WHERE run_id=2")
        connection.execute("DELETE FROM operation_runs WHERE id=2")
        _add_foreign_activity(connection)
        repository = OperationRepository()
        key = new_operation_key()
        intent = AttemptIntent("stop", 1, OperationKind.STOP, AttemptTarget(activity_id=1), None,
                               AttemptConfig(3, "5", "1"), _NOW)
        begun = repository.begin_attempt(intent, key, owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        changed = replace(intent, target=AttemptTarget(activity_id=4))
        if stage == "ended":
            connection.execute("UPDATE operation_runs SET status=3 WHERE id=?", (begun.value.ticket.run_id,))
        if stage == "exhausted":
            changed = replace(changed, config=AttemptConfig(1, "5", "1"))
        before = tuple(connection.iterdump())
        result = repository.begin_attempt(changed, key if stage == "old" else new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert isinstance(result.error, ConsistencyError), result.error
        assert tuple(connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("stage", ["new", "late", "reuse"])
def test_result_entry_rechecks_original_cleanup_owner(tmp_path, stage):
    owned = _cleanup_environment(tmp_path)
    try:
        repository = OperationRepository()
        begun = repository.begin_attempt(_delete_intent(), new_operation_key(), owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        finish = _finish(begun.value.ticket)
        key = new_operation_key()
        if stage != "new":
            result = repository.finish_attempt(finish, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        _seed_action(owned.connection, 9, 1, action_type=5)
        owned.connection.execute("UPDATE cleanup_items SET action_id=9 WHERE id=1")
        before = tuple(owned.connection.iterdump())
        result = repository.finish_attempt(finish, key if stage == "reuse" else new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert isinstance(result.error, ConsistencyError), result.error
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()
