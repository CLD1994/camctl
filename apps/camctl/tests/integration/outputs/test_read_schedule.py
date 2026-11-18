"""首次读取建档遵守发起动作时间，恢复原责任不重新竞争资格。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from ..support.output_planning import capture_operation, validate_static_event as validate_event
from camctl.outputs.qualification import QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_grant_reuse import read_request, _save_grant
from .test_local_read import local_read, prepare_local_read
from .test_read_associations import read_targets, open_read_targets, _command
from ..support.output_planning import event_case
from .test_read_rejections import _availability


@pytest.mark.parametrize("offset,outcome", [
    (-1, QualificationOutcome.REJECTED), (0, QualificationOutcome.GRANTED),
    (1, QualificationOutcome.GRANTED),
])
def test_first_read_uses_owner_schedule_at_microsecond_boundary(read_request, offset, outcome):
    owned, command = read_request
    command = replace(command, occurred_at=command.occurred_at + offset)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is outcome
    if outcome is QualificationOutcome.REJECTED:
        assert result.value.reason == "not_due"
        assert tuple(owned.connection.iterdump()) == before


def test_future_read_does_not_save_source_failure(read_request):
    owned, command = read_request
    if command.item_id is None:
        owned.connection.execute("UPDATE device_files SET presence_state=3 WHERE id=501")
    else:
        _availability(owned.connection, command, 4)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(
        replace(command, occurred_at=command.occurred_at - 1), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "not_due"
    assert tuple(owned.connection.iterdump()) == before


def test_future_read_still_checks_fixed_associations(read_request):
    owned, command = read_request
    command = replace(command, action_id=32, occurred_at=command.occurred_at - 1)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_waiting_for_schedule_does_not_consume_operation_key(read_request):
    owned, command = read_request
    key = new_operation_key()
    repository = OutputsRepository()
    early = repository.grant_file(replace(command, occurred_at=command.occurred_at - 1), key, owned)
    assert early.kind is DbOutcomeKind.COMPLETED, early.error
    assert early.value.outcome is QualificationOutcome.REJECTED
    due = repository.grant_file(command, key, owned)
    assert due.kind is DbOutcomeKind.COMPLETED, due.error
    assert due.value.outcome is QualificationOutcome.GRANTED
    before = tuple(owned.connection.iterdump())
    again = repository.grant_file(command, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == due.value
    assert tuple(owned.connection.iterdump()) == before


def test_existing_preparation_is_readable_before_schedule(read_request):
    owned, command, _, first = _save_grant(*read_request)
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(
        replace(command, occurred_at=command.occurred_at - 1), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value == replace(first, reason="already_granted")
    assert tuple(owned.connection.iterdump()) == before


@pytest.fixture(scope="module")
def _scheduled_event_sources(tmp_path_factory):
    # 每种实际输入只通过真实仓储生成一次；关闭连接后才复用静态事实。
    sources = {}
    for kind in ("device", "host", "internal"):
        with open_read_targets(tmp_path_factory.mktemp(f"schedule-{kind}")) as owned:
            if kind == "host":
                command = prepare_local_read(owned)
            else:
                command = _command(internal=kind == "internal")
            if kind == "internal":
                owned.connection.execute("DELETE FROM obtain_items")
                owned.connection.execute("DELETE FROM outputs")
            else:
                owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
            owned.connection.commit()
            plan = capture_operation(owned, lambda target: OutputsRepository().grant_file(
                command, new_operation_key(), target))
            for event_type in ((22,) if kind == "internal" else (22, 21)):
                event, proposal = event_case(plan, event_type)
                sources[kind, event_type] = event, proposal, command.action_id
    return sources


def _scheduled_event(sources, kind, event_type):
    event, proposal, action_id = sources[kind, event_type]
    # 事件和值字典可能被用例修改；读取范围由不可变映射和 frozenset 构成。
    event, events, owners, state, future = deepcopy((event, proposal.events, proposal.owners,
                                                     proposal.state_rows, proposal.transaction_rows))
    return event, replace(proposal, events=events, owners=owners, state_rows=state,
                          transaction_rows=future), action_id


@pytest.fixture(params=[("device", 22), ("host", 22), ("internal", 22),
                       ("device", 21), ("host", 21)])
def scheduled_event(request, _scheduled_event_sources):
    return _scheduled_event(_scheduled_event_sources, *request.param)


@pytest.fixture(params=["device", "host"])
def grant_event(request, _scheduled_event_sources):
    return _scheduled_event(_scheduled_event_sources, request.param, 21)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_registered_read_event_checks_time_boundary(scheduled_event, offset):
    event, context, _ = scheduled_event
    event = replace(event, occurred_at=event.occurred_at + offset)
    if offset < 0:
        with pytest.raises(EventValidationError):
            validate_event(event, context)
    else:
        validate_event(event, context)


@pytest.mark.parametrize("scheduled", [None, True, "1750000000000000", Decimal("1.5")])
def test_registered_read_event_requires_interpretable_schedule(scheduled_event, scheduled):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id]["scheduled_at"] = scheduled
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_future_proposal_cannot_supply_current_action(scheduled_event):
    event, context, action_id = scheduled_event
    del context.state_rows["actions"][action_id]
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_future_proposal_cannot_make_current_action_due(scheduled_event):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id]["scheduled_at"] = event.occurred_at + 1_000_000
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_current_schedule_accepts_exact_json_integer(scheduled_event):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id]["scheduled_at"] = Decimal(str(event.occurred_at) + ".0")
    validate_event(event, context)


@pytest.mark.parametrize("status,canceled", [(1, 0), (2, 1), (3, 0), (4, 0), (5, 0), (6, 1)])
def test_first_creation_requires_current_owner_eligibility(scheduled_event, status, canceled):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id].update(status=status, cancel_requested=canceled)
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("column,value", [
    ("status", None), ("status", "2"), ("cancel_requested", None),
    ("cancel_requested", False), ("cancel_requested", "0"), ("cancel_requested", 2),
])
def test_first_creation_requires_interpretable_current_owner(scheduled_event, column, value):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id][column] = value
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_future_ineligible_owner_does_not_replace_current_eligibility(scheduled_event):
    event, context, action_id = scheduled_event
    context.transaction_rows["actions"][action_id].update(status=6, cancel_requested=1)
    validate_event(event, context)


def test_current_owner_accepts_exact_json_integers(scheduled_event):
    event, context, action_id = scheduled_event
    context.state_rows["actions"][action_id].update(status=Decimal("2.0"), cancel_requested=Decimal("0.0"))
    validate_event(event, context)


@pytest.mark.parametrize("owner_future,delivery_future", [(True, False), (False, True), (False, False)])
def test_grant_rejects_delivery_of_another_action(grant_event, owner_future, delivery_future):
    event, context, action_id = grant_event
    context.state_rows["actions"][action_id]["scheduled_at"] = event.occurred_at + (1_000_000 if owner_future else 0)
    context.state_rows["actions"][32]["scheduled_at"] = event.occurred_at + (1_000_000 if delivery_future else 0)
    delivery_id = event.rows[0].after.values["delivery_id"]
    context.state_rows["deliveries"][delivery_id]["action_id"] = 32
    context.transaction_rows["deliveries"][delivery_id]["action_id"] = 32
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("table,identity", [("obtain_items", 101), ("obtain_source_selections", 31),
                                          ("action_dependencies", 31)])
def test_grant_requires_current_item_owner_chain(grant_event, table, identity):
    event, context, _ = grant_event
    del context.state_rows[table][identity]
    with pytest.raises(EventValidationError):
        validate_event(event, context)


def test_grant_rejects_delivery_of_another_output(grant_event):
    event, context, _ = grant_event
    delivery_id = event.rows[0].after.values["delivery_id"]
    context.state_rows["deliveries"][delivery_id]["output_id"] = 702
    context.transaction_rows["deliveries"][delivery_id]["output_id"] = 702
    with pytest.raises(EventValidationError):
        validate_event(event, context)
