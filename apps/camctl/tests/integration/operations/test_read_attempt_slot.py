"""新读取意图消费当前相机机会及轮次，原意图恢复保留首次结果。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history import validators
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import BeginAttemptCommand, OperationRepository
from camctl.persistence.transaction import TransactionError, TransactionScope, commit_operation

from ..outputs.test_grant_reuse import granted, read_request, read_targets, local_read
from ..outputs.test_selection_reuse_boundaries import _ReadProbe


@pytest.fixture
def ready_read(granted):
    """提供已持有机会的当前事实，不替代机会授予事务的验收。"""
    owned, command, _, prepared = granted
    if command.source_device_file_id is not None:
        owned.connection.execute("UPDATE file_copies SET slot_device_id='cam-1' WHERE id=?",
                                 (prepared.copy_id,))
        owned.connection.commit()
    return granted


def _intent(command, prepared):
    config = AttemptConfig(3, "10", "0") if command.config is not None else AttemptConfig(1)
    return AttemptIntent("read", command.action_id, OperationKind.READ_FILE,
        AttemptTarget(copy_id=prepared.copy_id), None, config, command.occurred_at + 1, copy_round=1)


@pytest.mark.parametrize("slot", ["held", "absent", "foreign"])
def test_new_read_attempt_requires_its_own_device_slot(ready_read, slot):
    owned, command, _, prepared = ready_read
    if slot != "held":
        owned.connection.execute("UPDATE file_copies SET slot_device_id=? WHERE id=?",
                                 (None if slot == "absent" else "another-camera", prepared.copy_id))
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    if slot == "foreign":
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result
        assert isinstance(result.error, ConsistencyError), result.error
    elif slot == "absent" and command.source_device_file_id is not None:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is BeginDisposition.REJECTED
        assert result.value.reason == "read_slot_not_held"
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is BeginDisposition.GRANTED
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
                                        (prepared.run_id,)).fetchone() == (1,)
        return
    assert tuple(owned.connection.iterdump()) == before


def test_read_attempt_cannot_use_a_different_copy_round(ready_read):
    owned, command, _, prepared = ready_read
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(replace(_intent(command, prepared), copy_round=2),
                                                 new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, TransactionError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_read_attempt_requires_the_record_of_its_source(ready_read):
    owned, command, _, prepared = ready_read
    table, identity = (("device_files", command.source_device_file_id) if command.source_device_file_id is not None
                       else ("intermediate_files", command.source_intermediate_file_id))
    owned.connection.execute(f"DELETE FROM {table} WHERE id=?", (identity,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("binding", ["missing_observer", "different_device", "different_driver"])
def test_read_attempt_requires_consistent_original_device_binding(ready_read, binding):
    owned, command, _, prepared = ready_read
    owned.connection.execute("UPDATE device_files SET observer_action_id=? WHERE id=?",
                             (999 if binding == "missing_observer" else 12, command.source_device_file_id))
    if binding != "missing_observer":
        column = "device_id" if binding == "different_device" else "driver_id"
        owned.connection.execute(f"UPDATE actions SET {column}=? WHERE id=12", ("different",))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("state", ["released", "next_round"])
def test_original_read_intent_does_not_reapply_current_slot_or_round(ready_read, state):
    owned, command, _, prepared = ready_read
    intent, key = _intent(command, prepared), new_operation_key()
    repository = OperationRepository()
    first = repository.begin_attempt(intent, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.disposition is BeginDisposition.GRANTED
    if state == "released":
        owned.connection.execute("UPDATE file_copies SET slot_device_id=NULL WHERE id=?", (prepared.copy_id,))
    else:
        owned.connection.execute("UPDATE file_copies SET round=2, recopies_used=1 WHERE id=?", (prepared.copy_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    repeated = repository.begin_attempt(intent, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == first.value
    assert tuple(owned.connection.iterdump()) == before


def _proposal(owned, command, prepared):
    owned.connection.execute("BEGIN")
    try:
        plan = BeginAttemptCommand(_intent(command, prepared), new_operation_key()).plan(
            TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    event, = plan.events
    return event, EventContext(TransactionRange(event.transaction_id, event.event_id, event.event_id),
                               plan.owners, deepcopy(plan.state_rows))


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("change", ["slot", "round", "source", "binding"])
def test_formal_read_intent_cannot_use_future_slot_facts(ready_read, change):
    owned, command, _, prepared = ready_read
    event, context = _proposal(owned, command, prepared)
    validate_event(event, context)
    future = deepcopy(context.state_rows)
    if change == "slot":
        context.state_rows["file_copies"][prepared.copy_id]["slot_device_id"] = None
    elif change == "round":
        context.state_rows["file_copies"][prepared.copy_id].update(round=2, recopies_used=1)
    elif change == "source":
        context.state_rows.setdefault("device_files", {}).pop(command.source_device_file_id, None)
    else:
        context.state_rows.setdefault("actions", {}).pop(11, None)
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


class _IncompleteRead:
    def __init__(self, intent, key, mutation):
        self.intent, self.key, self.mutation = intent, key, mutation

    def plan(self, scope):
        plan = BeginAttemptCommand(self.intent, self.key).plan(scope)
        state = deepcopy(plan.state_rows)
        copy = state["file_copies"][self.intent.target.copy_id]
        if self.mutation == "slot":
            copy["slot_device_id"] = "another-camera"
        elif self.mutation == "round":
            copy.update(round=2, recopies_used=1)
        else:
            table, identity = (("device_files", copy["source_device_file_id"])
                               if copy["source_device_file_id"] is not None
                               else ("intermediate_files", copy["source_intermediate_file_id"]))
            state[table].pop(identity)
        return replace(plan, state_rows=state)


@pytest.mark.parametrize("mutation", ["slot", "round", "source"])
def test_real_intent_transaction_requires_complete_current_read_facts(ready_read, mutation, monkeypatch):
    owned, command, _, prepared = ready_read
    original = validators.NAMED_GUARDS["attempt_intent"]
    reached = []

    def inspect(event, context):
        reached.append((event.event_type, event.reason))
        original(event, context)

    monkeypatch.setitem(validators.NAMED_GUARDS, "attempt_intent", inspect)
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(_IncompleteRead(_intent(command, prepared), key, mutation), key, owned)
    assert reached == [(11, 1)], receipt.error
    assert receipt.kind == "rolled_back", receipt
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("failure", ["execute", "fetch"])
def test_read_source_query_failure_is_preserved_without_attempt(ready_read, failure):
    owned, command, _, prepared = ready_read
    table = "device_files" if command.source_device_file_id is not None else "intermediate_files"
    probe = _ReadProbe(owned.connection, f"SELECT * FROM {table}", failure)
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(),
                                                 replace(owned, connection=probe))
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert result.error is probe.error
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(owned.connection.iterdump()) == before
