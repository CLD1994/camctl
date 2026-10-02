"""真实事件登记与取回成员守卫共同约束拒绝和显式核实。"""

from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import (
    CommandPlan, commit_operation, event_envelope, row_facts, saved_transaction_events, update_change,
)

from .test_read_associations import read_targets
from .test_qualification import _NOW
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture
def member_context(read_targets):
    _complete_source(read_targets.connection)
    register_outputs_guards()
    return EventContext(TransactionRange(2, 2, 2),
                        {("obtain_items", 101): ("action", 31)}, _state(read_targets.connection))


def _complete_source(connection):
    connection.execute("UPDATE actions SET status=3 WHERE id=11")
    connection.commit()


def _state(connection):
    identities = {
        "plans": (1,), "actions": (11, 31), "action_dependencies": (31,),
        "obtain_source_selections": (31,), "obtain_items": (101,), "outputs": (701,),
    }
    return {table: {identity: row_facts(connection, table, identity)
                     for identity in ids} for table, ids in identities.items()}


def _event(context, reason):
    item = context.state_rows["obtain_items"][101]
    if reason == 4:
        item.update(status=1, basis=5, requested_output_id=701, output_id=None)
        after = {"status": 2, "output_id": 701}
    else:
        after = {"status": 4, "error_code": 4, "error_details_json": {"output_id": 701}}
    row = update_change("obtain_items", 101, {key: item[key] for key in after}, after)
    return event_envelope(2, 2, 21, reason, (row,), _NOW)


@pytest.mark.parametrize("reason", [2, 4])
def test_fixed_member_can_advance_through_registered_branch(member_context, reason):
    event = _event(member_context, reason)
    assert validate_event(event, member_context).branch_name == ("REJECT" if reason == 2 else "RESOLVE")


@pytest.mark.parametrize("reason", [2, 4])
def test_canceled_capture_with_output_remains_a_valid_source(member_context, reason):
    event = _event(member_context, reason)
    member_context.state_rows["actions"][11].update(status=6, cancel_requested=1)
    validate_event(event, member_context)


@pytest.mark.parametrize("reason", [2, 4])
@pytest.mark.parametrize("table,identity,column,value", [
    ("obtain_source_selections", 31, "status", 1),
    ("obtain_source_selections", 31, "error_code", 1),
    ("obtain_source_selections", 31, "error_details_json", {}),
    ("obtain_source_selections", 31, "dependency_id", 999),
    ("obtain_items", 101, "selection_id", 999),
    ("action_dependencies", 31, "action_id", 999),
    ("action_dependencies", 31, "depends_on_action_id", 999),
    ("actions", 31, "status", 1),
    ("actions", 31, "cancel_requested", 1),
    ("actions", 31, "type", 7),
    ("actions", 31, "source_resolution_state", 1),
    ("actions", 31, "resolved_source_plan_id", 99),
    ("actions", 11, "type", 7),
    ("actions", 11, "status", 1),
    ("actions", 11, "status", 2),
    ("outputs", 701, "source_action_id", 12),
])
def test_member_guard_rejects_ineligible_or_misbound_facts(member_context, reason, table, identity, column, value):
    event = _event(member_context, reason)
    member_context.state_rows[table][identity][column] = value
    with pytest.raises(EventValidationError) as failure:
        validate_event(event, member_context)
    assert "具名校验未接入" not in str(failure.value)


@pytest.mark.parametrize("reason", [2, 4])
@pytest.mark.parametrize("table,identity", [
    ("obtain_items", 101), ("obtain_source_selections", 31),
    ("action_dependencies", 31), ("actions", 31), ("actions", 11), ("outputs", 701),
])
def test_future_transaction_facts_cannot_supply_current_member(member_context, reason, table, identity):
    event = _event(member_context, reason)
    proposed = deepcopy(member_context.state_rows)
    del member_context.state_rows[table][identity]
    context = replace(member_context, transaction_rows=proposed)
    with pytest.raises(EventValidationError) as failure:
        validate_event(event, context)
    assert "具名校验未接入" not in str(failure.value)


def test_resolve_cannot_replace_requested_output(member_context):
    event = _event(member_context, 4)
    member_context.state_rows["obtain_items"][101]["requested_output_id"] = 702
    with pytest.raises(EventValidationError) as failure:
        validate_event(event, member_context)
    assert "具名校验未接入" not in str(failure.value)


def test_explicit_selected_member_keeps_requested_identity(member_context):
    event = _event(member_context, 2)
    member_context.state_rows["obtain_items"][101].update(basis=5, requested_output_id=702)
    with pytest.raises(EventValidationError) as failure:
        validate_event(event, member_context)
    assert "具名校验未接入" not in str(failure.value)


def test_unresolved_explicit_member_can_end_without_output_association(member_context):
    member_context.state_rows["obtain_items"][101].update(
        status=1, basis=5, requested_output_id=999, output_id=None,
    )
    event = _event(member_context, 2)
    after = {"status": 4, "error_code": 1, "error_details_json": {"requested_output_id": 999}}
    item = member_context.state_rows["obtain_items"][101]
    event = replace(event, rows=(update_change("obtain_items", 101,
        {key: item[key] for key in after}, after),))
    validate_event(event, member_context)


@pytest.mark.parametrize("reason", [2, 4])
@pytest.mark.parametrize("changes", [
    {"status": 3, "delivery_id": 201}, {"status": 4}, {"status": 5},
    {"delivery_id": 201}, {"source_dependency": 1},
])
def test_member_guard_does_not_reopen_delivery_or_final_item(member_context, reason, changes):
    event = _event(member_context, reason)
    member_context.state_rows["obtain_items"][101].update(changes)
    with pytest.raises(EventValidationError):
        validate_event(event, member_context)


@pytest.mark.parametrize("reason", [2, 4])
def test_future_eligibility_cannot_override_cancellation(member_context, reason):
    event = _event(member_context, reason)
    proposed = deepcopy(member_context.state_rows)
    member_context.state_rows["actions"][31]["cancel_requested"] = 1
    with pytest.raises(EventValidationError):
        validate_event(event, replace(member_context, transaction_rows=proposed))


class _AdvanceMember:
    """由真实事务消费事件，验证登记、守卫与投影写入的接缝。"""

    def __init__(self, reason):
        self.reason = reason

    def plan(self, scope):
        state = _state(scope.connection)
        item = state["obtain_items"][101]
        after = ({"status": 2, "output_id": 701} if self.reason == 4 else {
            "status": 4, "error_code": 1,
            "error_details_json": {"requested_output_id": 999},
        })
        allocation = scope.allocate(1)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 21,
            self.reason, (update_change("obtain_items", 101,
                {key: item[key] for key in after}, after),), _NOW)
        return CommandPlan((event,), {("obtain_items", 101): ("action", 31)}, state)


@pytest.mark.parametrize("reason", [2, 4])
@pytest.mark.parametrize("write_failure", [False, True])
def test_member_event_and_projection_commit_or_rollback_together(read_targets, reason, write_failure):
    owned = read_targets
    _complete_source(owned.connection)
    requested = 701 if reason == 4 else 999
    owned.connection.execute(
        "UPDATE obtain_items SET status=1, basis=5, requested_output_id=?, output_id=NULL WHERE id=101",
        (requested,),
    )
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    register_outputs_guards()
    target = replace(owned, connection=_FaultConnection(owned.connection, "UPDATE obtain_items SET")) if write_failure else owned
    key = new_operation_key()
    receipt = commit_operation(_AdvanceMember(reason), key, target)
    if write_failure:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, sqlite3.OperationalError)
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error
        item = row_facts(owned.connection, "obtain_items", 101)
        assert item["status"] == (2 if reason == 4 else 4)
        assert item["requested_output_id"] == requested
        assert item["output_id"] == (701 if reason == 4 else None)
        events = saved_transaction_events(owned.connection, key)
        assert len(events) == 1
        assert (events[0]["type"], events[0]["reason"]) == (21, reason)
