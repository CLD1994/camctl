"""准备占用只登记责任，到计划时间才保存动作开始。"""
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.models import ActivityCapabilities, activity_capabilities
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories import scheduling
from camctl.persistence.transaction import TransactionError, TransactionScope
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.repositories.capture_facts import unstarted_events
from camctl.contracts.workflow_errors import validate_error_details
from camctl.history.changes import event_report_targets
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES
from camctl.devices.drivers.hasselblad.identity import CameraIdentity


def _identity(action):
    return CameraIdentity("CAMERA-1", 10) if action["driver_id"] == "hasselblad_cfv100c" else None


@pytest.fixture
def reserve(mocker):
    action = dict(id=7, type=2, status=1, execution_started=0, cancel_requested=0,
        scheduled_at=20_000_000, max_delay_ms=3000, plan_id=3, input_index=0,
        device_id="cam", driver_id="driver", execution_spec_json={"ownership_mode": 2,
            "output_scope": {"directories": ["/media"]}})
    action.update(error_code=None, error_details_json=None)
    connection = create_autospec(sqlite3.Connection, instance=True)
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        cursor.__iter__.return_value = iter([])
        return cursor
    connection.execute.side_effect = execute
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" and identity == 7 else None)
    mocker.patch.object(scheduling, "saved_transaction_events", return_value=None)
    mocker.patch.object(scheduling, "_next_id", return_value=11)
    mocker.patch.object(scheduling, "activity_capabilities", return_value=
        ActivityCapabilities(0, 1, 1, 2, 2, 2, {"directories": ["/media"]}))
    return action, TransactionScope(connection, 4, 10)


@pytest.fixture
def photo_reserve(reserve, mocker):
    action, scope = reserve
    action.update(type=1, driver_id="hasselblad_cfv100c",
        execution_spec_json={"output_storage": "host", "start_call_timeout_s": Decimal("60")})
    mocker.patch.object(scheduling, "activity_capabilities", side_effect=activity_capabilities)
    return action, scope


def test_photo_reservation_saves_only_original_photo_capabilities(photo_reserve):
    action, scope = photo_reserve
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_001, Decimal("10"), _identity(action)),
        new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.RESERVED
    assert [(event.event_type, event.reason) for event in plan.events] == [(13, 1)]
    assert action["status"] == 1 and action["execution_started"] == 0
    row, = plan.events[0].rows
    assert row.table == "device_activities"
    assert row.after.values["stop_supported"] == row.after.values["safe_repeat_stop"] == 0
    assert row.after.values["start_return_meaning"] == 3
    assert row.after.values["ownership_mode"] == 1 and row.after.values["output_scope_json"] == {}
    assert row.after.values["dispatch_state"] == 1 and row.after.values["occupancy_state"] == 1
    assert row.after.values["started_at"] is None and row.after.values["capture_json"] is None
    assert row.after.values["baseline_state"] == 1


def test_photo_reservation_reuses_original_activity(photo_reserve, mocker):
    action, scope = photo_reserve
    capabilities = activity_capabilities("camera_take_photo", action["execution_spec_json"])
    activity = dict(scheduling._new_activity_values(action, capabilities, _identity(action)), id=11)
    mocker.patch.object(scheduling.StartActionCommand, "_existing_activity_id", return_value=11)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else activity)
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10"), _identity(action)),
        new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.ALREADY
    assert plan.result.activity_id == 11 and not plan.events


def test_photo_reservation_cannot_pass_an_original_start(photo_reserve):
    _, scope = photo_reserve
    original_execute = scope.connection.execute.side_effect
    def execute(sql, parameters=()):
        cursor = original_execute(sql, parameters)
        if "FROM operation_runs WHERE action_id" in sql:
            cursor.fetchone.return_value = (1,)
        return cursor
    scope.connection.execute.side_effect = execute
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10")),
        new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.REJECTED
    assert not plan.events


def test_preparation_candidate_query_includes_an_earlier_photo(photo_reserve):
    _, scope = photo_reserve
    original_execute = scope.connection.execute.side_effect
    def execute(sql, parameters=()):
        cursor = original_execute(sql, parameters)
        if sql.startswith("SELECT scheduled_at,plan_id,input_index,max_delay_ms FROM actions"):
            assert 1 in parameters[1:]
            cursor.__iter__.return_value = iter([(19_000_000, 3, 0, 3000)])
        return cursor
    scope.connection.execute.side_effect = execute
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10")),
        new_operation_key()).plan(scope)
    assert plan.result.reason == "not_first_candidate" and not plan.events


@pytest.mark.parametrize("changed_request", [False, True])
def test_photo_original_reservation_is_verified_after_terminal(photo_reserve, mocker, changed_request):
    action, scope = photo_reserve
    request = scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10"))
    command = scheduling.ReservePreparationCommand(request, new_operation_key())
    saved = [{"type": 13, "reason": 1, "occurred_at": request.occurred_at,
        "body": {"evidence": {"preparation_request": command._evidence()}, "rows": [{
            "table": "device_activities", "id": 11, "after": {"values": {"action_id": 7}}}]}}]
    mocker.patch.object(scheduling, "saved_transaction_events", return_value=saved)
    action.update(status=6, cancel_requested=1)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else dict(id=11, action_id=7))
    if changed_request:
        command.request = scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("9"))
        with pytest.raises(TransactionError):
            command.plan(scope)
    else:
        plan = command.plan(scope)
        assert plan.result.outcome is scheduling.ReservePreparationOutcome.ALREADY
        assert plan.result.activity_id == 11 and not plan.events


def test_reserve_creates_only_pending_activity(reserve):
    action, scope = reserve
    request = scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_001, Decimal("10"))
    plan = scheduling.ReservePreparationCommand(request, new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.RESERVED
    assert [(event.event_type, event.reason) for event in plan.events] == [(13, 1)]
    assert action["status"] == 1 and action["execution_started"] == 0
    row = plan.events[0].rows[0]
    assert row.table == "device_activities" and row.row_id == 11
    assert row.after.values["dispatch_state"] == 1
    assert row.after.values["occupancy_state"] == 1


@pytest.mark.parametrize("action_type", CAPTURE_ACTION_TYPES)
def test_preparation_reservation_supplies_before_facts_for_report_target(
    request, action_type,
):
    fixture_name = "photo_reserve" if action_type == 1 else "reserve"
    action, scope = request.getfixturevalue(fixture_name)
    action["type"] = action_type
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_001, Decimal("10"), _identity(action)),
        new_operation_key()).plan(scope)

    assert event_report_targets(plan.events[0], plan.state_rows) == ((1, 7),)
    assert plan.state_rows["device_activities"] == {}
    assert plan.events[0].rows[0].row_id not in plan.state_rows["device_activities"]


@pytest.mark.parametrize("now,lead,reason", [
    (9_999_999, Decimal("10"), "too_early"),
    (19_999_999, Decimal("0"), "too_early"),
    (23_000_001, Decimal("10"), "window_ended"),
])
def test_reserve_obeys_precise_prepare_interval(reserve, now, lead, reason):
    _, scope = reserve
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, now, now, lead), new_operation_key()).plan(scope)
    assert plan.result.reason == reason
    assert not plan.events


@pytest.mark.parametrize("lead", [True, Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), "10"])
def test_reserve_rejects_invalid_lead(lead):
    with pytest.raises((TypeError, ValueError)):
        scheduling.ReservePreparationRequest(7, 1, 1, lead)


@pytest.mark.parametrize("fixture_name", ["reserve", "photo_reserve"])
def test_preparation_failure_preserves_public_error_identity(request, fixture_name, mocker):
    action, scope = request.getfixturevalue(fixture_name)
    literal = "camera_take_photo" if action["type"] == 1 else "camera_record"
    activity = dict(scheduling._new_activity_values(action,
        scheduling.activity_capabilities(literal, action["execution_spec_json"]), _identity(action)), id=11)
    mocker.patch.object(scheduling, "load_start_facts", return_value=
        StartFacts(activity, None, 0, True, 1, preparation_resolved=True))
    mocker.patch("camctl.persistence.repositories.capture_facts.preparation_calls_resolved", return_value=True)
    mocker.patch("camctl.persistence.repositories.preparation.verify_failure_source", autospec=True)
    mocker.patch.object(scheduling, "_sibling_actions", return_value={7: action})
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else dict(id=3, status=1) if table == "plans" else None)
    plan = scheduling.FailPreparationCommand(
        scheduling.FailPreparationRequest(7, 12_000_000, "device_failed"), new_operation_key()).plan(scope)
    details = plan.events[0].rows[0].after.values["error_details_json"]
    validate_error_details("capture_failed", details)
    assert details == {"activity_id": "11", "reason": "device_failed"}
    assert plan.events[0].rows[0].after.values["status"] == 4
    assert "execution_started" not in plan.events[0].rows[0].after.values


def test_fixed_baseline_does_not_release_unfinished_settings():
    activity = dict(id=11, action_id=7, occupancy_state=1, dispatch_state=1,
                    activity_state=1, ownership_mode=2, baseline_state=3)
    facts = StartFacts(activity, None, 0, True, 1, preparation_resolved=False)
    assert unstarted_events(facts, 20, run_status=None, preparation_resolved=True) == []


@pytest.mark.parametrize("fixture_name", ["reserve", "photo_reserve"])
def test_start_action_reuses_prepared_activity_and_original_holder(request, fixture_name, mocker):
    from camctl.scheduling.resources import PreparationHolder
    action, scope = request.getfixturevalue(fixture_name)
    literal = "camera_take_photo" if action["type"] == 1 else "camera_record"
    capabilities = scheduling.activity_capabilities(literal, action["execution_spec_json"])
    activity = dict(scheduling._new_activity_values(action, capabilities, _identity(action)), id=11)
    if action["type"] == 2:
        activity["baseline_state"] = 3
    mocker.patch.object(scheduling.StartActionCommand, "_existing_activity_id", return_value=11)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else activity if table == "device_activities" else dict(id=3, status=1))
    mocker.patch.object(scheduling, "current_preparation_holder", return_value=PreparationHolder(7, 11))
    mocker.patch("camctl.persistence.repositories.capture_facts.preparation_calls_resolved", return_value=True)
    plan = scheduling.StartActionCommand(
        scheduling.StartActionRequest(7, 20_000_000, 20_000_000, _identity(action)), new_operation_key()).plan(scope)
    assert plan.result.activity_id == 11
    assert [(event.event_type, event.reason) for event in plan.events] == [(5, 1), (9, 1)]
    assert all(row.table != "device_activities" for event in plan.events for row in event.rows)
    assert plan.events[0].rows[0].after.values == {"status": 2, "execution_started": 1}


def test_original_reservation_does_not_compete_again(reserve, mocker):
    action, scope = reserve
    capabilities = scheduling.activity_capabilities("camera_record", {})
    activity = dict(scheduling._new_activity_values(action, capabilities, _identity(action)), id=11)
    mocker.patch.object(scheduling.StartActionCommand, "_existing_activity_id", return_value=11)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else activity)
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10")),
        new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.ALREADY
    assert plan.result.activity_id == 11 and not plan.events
