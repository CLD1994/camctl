"""设置准备的意图和成功结果具有独立的可靠依据。"""
from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.operations import _attempt_intent_guard, _attempt_result_guard
from camctl.persistence.repositories.preparation import PreparationReads
from camctl.persistence.transaction import event_envelope, row_change, update_change


def _database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = None
    cursor.__iter__.return_value = iter(())
    connection.execute.return_value = cursor
    return connection


def _intent():
    kind = int(enum_for("operation_runs.kind").PREPARE_CAPTURE)
    run = dict(action_id=7, activity_id=11, kind=kind, attempts_used=1,
               max_attempts_used=1, timeout_s_json=Decimal("10"), retry_interval_s_json=None)
    attempt = dict(run_id=3, attempt_no=1, intent_event_id=21, status=1,
        effect_state=int(enum_for("operation_attempts.effect_state").UNKNOWN),
        result_event_id=None, result_json=None, error_json=None, copy_round=None)
    event = event_envelope(21, 8, 11, 1,
        (row_change("operation_attempts", 5, attempt), row_change("operation_runs", 3, run)), 10)
    state = {"actions": {7: dict(id=7, status=1, execution_started=0, cancel_requested=0,
        device_id="cam", scheduled_at=20, max_delay_ms=3)},
        "device_activities": {11: dict(id=11, action_id=7, occupancy_state=1, dispatch_state=1,
            activity_state=1, ownership_mode=1, output_scope_json={})}}
    return event, EventContext(TransactionRange(8, 21, 21), {}, state)


def test_preparation_intent_needs_the_original_connection_reader():
    event, context = _intent()
    with pytest.raises(EventValidationError):
        _attempt_intent_guard(event, context)
    _attempt_intent_guard(event, replace(context, preparation_reads=PreparationReads(_database())))


@pytest.mark.parametrize("observations", [[], [dict(type="setting_applied", version=1, data={})],
    [dict(type="capture_preparation_completed", version=1, data={"activity_id": "12"})]])
def test_success_needs_the_whole_original_preparation_completion(observations):
    effect = int(enum_for("operation_attempts.effect_state").CONFIRMED)
    result = dict(format_version=1,
        settlement=dict(basis="observed", evidence=dict(type="returned", version=1, data={})),
        observations=observations)
    event = event_envelope(22, 8, 12, 1, (update_change("operation_attempts", 5,
        {"status": 1, "result_event_id": None, "effect_state": int(enum_for("operation_attempts.effect_state").UNKNOWN),
         "error_json": None, "result_json": None},
        {"status": 2, "effect_state": effect, "result_event_id": 22,
         "error_json": None, "result_json": result}),), 11)
    state = {"operation_attempts": {5: dict(run_id=3)}, "operation_runs": {3: dict(
        kind=int(enum_for("operation_runs.kind").PREPARE_CAPTURE), activity_id=11)}}
    context = EventContext(TransactionRange(8, 22, 22), {}, state)
    with pytest.raises(EventValidationError):
        _attempt_result_guard(event, context)
    completed = dict(type="capture_preparation_completed", version=1, data={"activity_id": "11"})
    row = event.rows[0]
    from camctl.history.events import RowImage
    event = replace(event, rows=(replace(row, after=RowImage(True,
        {**row.after.values, "result_json": {**result, "observations": [completed]}})),))
    _attempt_result_guard(event, context)
