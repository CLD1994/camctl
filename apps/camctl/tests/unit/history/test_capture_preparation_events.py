"""准备失败使用独立历史分支，不产生动作开始事实。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history import validators
from camctl.history.validators import EventValidationError, validate_event_structure
from camctl.contracts.history_values import TransactionRange
from camctl.history.validators import EventContext
from camctl.capture.models import activity_capabilities
from camctl.persistence.repositories import capture, scheduling
from camctl.persistence.repositories.preparation import PreparationReads
from camctl.persistence.transaction import event_envelope, row_change, update_change


def _failure(*, reason="device_failed", before_status=1, error_code=13):
    return EventEnvelope(
        event_id=101, transaction_id=7, event_type=8, event_version=1,
        occurred_at=1, clock_status=2, change_seq=9, reason=5,
        evidence={
            "preparation_resolved": True,
            "preparation_failure_request": {"action_id": 8, "reason": reason, "occurred_at": 1},
        },
        rows=(RowChange(
            table="actions", row_id=8,
            before=RowImage(exists=True, values={
                "status": before_status, "error_code": None, "error_details_json": None,
            }),
            after=RowImage(exists=True, values={
                "status": 4, "error_code": error_code, "error_details_json": {"activity_id": "11", "reason": reason},
            }),
        ),),
    )


@pytest.mark.parametrize("reason", ["device_failed", "baseline_read_failed"])
def test_preparation_failure_has_a_branch_without_execution_marker_change(reason):
    event, branch, _ = validate_event_structure(_failure(reason=reason))
    assert (event, branch) == ("ACTION_FINISHED", "PREPARATION_FAILED")


@pytest.mark.parametrize("before_status,error_code", [(2, 13), (1, 14)])
def test_preparation_failure_rejects_other_state_or_error(before_status, error_code):
    with pytest.raises(EventValidationError):
        validate_event_structure(_failure(before_status=before_status, error_code=error_code))


def test_preparation_failure_cannot_claim_execution_started():
    event = _failure()
    row = event.rows[0]
    row = replace(row,
        before=RowImage(exists=True, values={**row.before.values, "execution_started": 0}),
        after=RowImage(exists=True, values={**row.after.values, "execution_started": 1}),
    )
    with pytest.raises(EventValidationError):
        validate_event_structure(replace(event, rows=(row,)))


def _photo_action():
    return dict(id=8, type=1, status=1, execution_started=0, cancel_requested=0,
        device_id="cam", scheduled_at=20_000_000, max_delay_ms=3000, plan_id=3, input_index=0,
        execution_spec_json={"output_storage": "host", "start_call_timeout_s": Decimal("60")})


def _database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = None
    cursor.__iter__.return_value = iter(())
    connection.execute.return_value = cursor
    return connection


def test_photo_pending_activity_has_the_original_photo_contract():
    action = _photo_action()
    values = scheduling._new_activity_values(action,
        activity_capabilities("camera_take_photo", action["execution_spec_json"]))
    event = event_envelope(101, 7, 13, 1, (row_change("device_activities", 11, values),),
        10_000_000, evidence={"preparation_request": {"action_id": 8,
            "trusted_wall_now": 10_000_000, "occurred_at": 10_000_000, "prepare_lead_s": Decimal("10")}})
    context = EventContext(TransactionRange(7, 101, 101), {}, {"actions": {8: action}})
    PreparationReads(_database()).verify_create(event, context, event.rows[0])


def test_photo_preparation_failure_uses_the_original_connection_reader(mocker):
    reader = PreparationReads(_database())
    verify = mocker.patch.object(PreparationReads, "verify_failure", autospec=True)
    event = _failure()
    context = EventContext(TransactionRange(7, 101, 101), {}, {"actions": {8: _photo_action()}},
        preparation_reads=reader)
    capture._action_finish_guard(event, context)
    verify.assert_called_once_with(reader, context.state_rows["actions"][8],
        {"activity_id": "11", "reason": "device_failed"})


@pytest.mark.parametrize("action_type", [1, 8])
def test_preparation_failure_branch_checks_original_capture_type(monkeypatch, action_type):
    event = _failure()
    _, _, branch = validate_event_structure(event)
    # 业务守卫是校验器的具名扩展端口；本单元核对其前置分支条件。
    monkeypatch.setattr(validators, "NAMED_GUARDS", {
        name: lambda event, context: None for name in branch["guards"]})
    context = EventContext(TransactionRange(7, 101, 101), {("actions", 8): ("action", 8)},
        {"actions": {8: {**_photo_action(), "type": action_type}}})
    if action_type == 1:
        validated = validators.validate_event(event, context)
        assert validated.branch_name == "PREPARATION_FAILED"
    else:
        with pytest.raises(EventValidationError):
            validators.validate_event(event, context)


@pytest.mark.parametrize("resolved", [False, True])
def test_photo_preparation_release_waits_for_actual_calls(mocker, resolved):
    action = {**_photo_action(), "status": 6}
    activity = dict(id=11, **scheduling._new_activity_values(action,
        activity_capabilities("camera_take_photo", action["execution_spec_json"])))
    reader = PreparationReads(_database())
    mocker.patch.object(PreparationReads, "calls_resolved", autospec=True, return_value=resolved)
    event = event_envelope(101, 7, 13, 3, (update_change("device_activities", 11,
        {"occupancy_state": 1}, {"occupancy_state": 2}),), 1,
        evidence={"preparation_resolved": True})
    context = EventContext(TransactionRange(7, 101, 101), {},
        {"actions": {8: action}, "device_activities": {11: activity}}, preparation_reads=reader)
    if resolved:
        capture._release_guard(event, context)
    else:
        with pytest.raises(EventValidationError):
            capture._release_guard(event, context)
    assert activity["activity_state"] == activity["dispatch_state"] == 1
    assert activity["started_at"] is None and activity["capture_json"] is None
