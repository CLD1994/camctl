"""准备占用只登记责任，到计划时间才保存动作开始。"""
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.models import ActivityCapabilities
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories import scheduling
from camctl.persistence.transaction import TransactionScope
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.repositories.capture_facts import unstarted_events
from camctl.contracts.workflow_errors import validate_error_details


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


def test_preparation_failure_preserves_public_error_identity(reserve, mocker):
    action, scope = reserve
    activity = dict(id=11, action_id=7, occupancy_state=1, dispatch_state=1,
                    activity_state=1, ownership_mode=2, baseline_state=3)
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


def test_start_action_reuses_prepared_activity_and_original_holder(reserve, mocker):
    from camctl.scheduling.resources import PreparationHolder
    action, scope = reserve
    capabilities = scheduling.activity_capabilities("camera_record", {})
    activity = dict(scheduling._new_activity_values(action, capabilities), id=11, baseline_state=3)
    mocker.patch.object(scheduling.StartActionCommand, "_existing_activity_id", return_value=11)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else activity if table == "device_activities" else dict(id=3, status=1))
    mocker.patch.object(scheduling, "current_preparation_holder", return_value=PreparationHolder(7, 11))
    mocker.patch("camctl.persistence.repositories.capture_facts.preparation_calls_resolved", return_value=True)
    plan = scheduling.StartActionCommand(
        scheduling.StartActionRequest(7, 20_000_000, 20_000_000), new_operation_key()).plan(scope)
    assert plan.result.activity_id == 11
    assert [(event.event_type, event.reason) for event in plan.events] == [(5, 1), (9, 1)]
    assert all(row.table != "device_activities" for event in plan.events for row in event.rows)
    assert plan.events[0].rows[0].after.values == {"status": 2, "execution_started": 1}


def test_original_reservation_does_not_compete_again(reserve, mocker):
    action, scope = reserve
    capabilities = scheduling.activity_capabilities("camera_record", {})
    activity = dict(scheduling._new_activity_values(action, capabilities), id=11)
    mocker.patch.object(scheduling.StartActionCommand, "_existing_activity_id", return_value=11)
    mocker.patch.object(scheduling, "row_facts", side_effect=lambda db, table, identity:
        action if table == "actions" else activity)
    plan = scheduling.ReservePreparationCommand(
        scheduling.ReservePreparationRequest(7, 10_000_000, 10_000_000, Decimal("10")),
        new_operation_key()).plan(scope)
    assert plan.result.outcome is scheduling.ReservePreparationOutcome.ALREADY
    assert plan.result.activity_id == 11 and not plan.events
