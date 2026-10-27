"""START 不能与尚未收场的同设备读取同时执行。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError
from camctl.operations.attempts import (AttemptConfig, AttemptIntent, AttemptTarget,
                                        BeginDisposition, OperationKind)
from camctl.persistence.repositories import scheduling
from camctl.persistence.repositories.operations import _attempt_intent_guard
from camctl.persistence.repositories import operations
from camctl.persistence.repositories.preparation import PreparationReads
from camctl.persistence.transaction import TransactionScope, event_envelope, row_change
from camctl.scheduling.resources import device_read_in_flight, device_start_in_flight
from camctl.scheduling.rules import LaunchWindow


def _start_intent():
    run = dict(action_id=7, activity_id=11, kind=int(enum_for("operation_runs.kind").START),
               attempts_used=1)
    attempt = dict(run_id=3, attempt_no=1, intent_event_id=21, status=1,
                   effect_state=int(enum_for("operation_attempts.effect_state").UNKNOWN),
                   result_event_id=None, result_json=None, error_json=None, copy_round=None)
    event = event_envelope(21, 8, 11, 1,
        (row_change("operation_attempts", 5, attempt), row_change("operation_runs", 3, run)), 10)
    state = {"actions": {7: dict(id=7, device_id="cam", driver_id="other")},
             "device_activities": {11: dict(id=11, action_id=7, ownership_mode=1)}}
    return event, EventContext(TransactionRange(8, 21, 21), {}, state)


def test_start_intent_guard_rechecks_active_read_from_transaction_connection(monkeypatch):
    event, context = _start_intent()
    reader = PreparationReads(create_autospec(sqlite3.Connection, instance=True))
    monkeypatch.setattr(reader, "device_read_in_flight", lambda device_id: True)
    with pytest.raises(EventValidationError, match="读取"):
        _attempt_intent_guard(event, replace(context, preparation_reads=reader))


def test_start_intent_guard_allows_no_active_read(monkeypatch):
    event, context = _start_intent()
    reader = PreparationReads(create_autospec(sqlite3.Connection, instance=True))
    monkeypatch.setattr(reader, "device_read_in_flight", lambda device_id: False)
    _attempt_intent_guard(event, replace(context, preparation_reads=reader))


def test_start_intent_guard_requires_transaction_reader():
    event, context = _start_intent()
    with pytest.raises(EventValidationError, match="读取"):
        _attempt_intent_guard(event, context)


@pytest.mark.parametrize("row,expected", [(None, False), ((1,), True)])
def test_read_execution_query_uses_same_device_and_running_attempt(row, expected):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = row
    assert device_read_in_flight(connection, "cam") is expected
    sql, parameters = connection.execute.call_args.args
    assert "c.slot_device_id=?" in sql
    assert f"t.status={int(enum_for('operation_attempts.status').RUNNING)}" in sql
    assert parameters == (int(enum_for("operation_runs.kind").READ_FILE), "cam")


@pytest.mark.parametrize("row,expected", [(None, False), ((1,), True)])
def test_start_execution_query_uses_same_device_and_running_attempt(row, expected):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = row
    assert device_start_in_flight(connection, "cam") is expected
    sql, parameters = connection.execute.call_args.args
    assert "a.device_id=?" in sql
    assert f"t.status={int(enum_for('operation_attempts.status').RUNNING)}" in sql
    assert parameters == (int(enum_for("operation_runs.kind").START), "cam")


def test_start_grant_waits_for_in_flight_read(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    monkeypatch.setattr(scheduling, "saved_transaction_events", lambda connection, key: None)
    monkeypatch.setattr(scheduling.GrantStartCommand, "_load_action", lambda self, connection, action_id:
        dict(id=7, type=1, status=2, cancel_requested=0, device_id="cam"))
    monkeypatch.setattr(scheduling, "current_start_holder", lambda connection, device_id: None)
    monkeypatch.setattr(scheduling, "current_preparation_holder", lambda connection, device_id: None)
    monkeypatch.setattr(scheduling, "device_read_in_flight", lambda connection, device_id: True)
    request = scheduling.GrantRequest("cam", 7, LaunchWindow(10, 20), 15,
        AttemptConfig(max_attempts=2, timeout_s=Decimal("5"), retry_interval_s=Decimal("1")), 15)
    plan = scheduling.GrantStartCommand(request, new_operation_key()).plan(TransactionScope(connection, 4, 10))
    assert plan.result.outcome is scheduling.GrantOutcome.REJECTED
    assert plan.result.reason == "device_busy" and not plan.events


@pytest.mark.parametrize("slot,busy,allowed", [("cam", True, False), ("cam", False, True),
                                              (None, True, True)])
def test_read_intent_respects_in_flight_start_but_host_read_is_independent(
        monkeypatch, slot, busy, allowed):
    event, context = _start_intent()
    run = event.rows[1]
    attempt = event.rows[0]
    event = replace(event, rows=(replace(attempt, after=replace(attempt.after,
        values={**attempt.after.values, "copy_round": 1})),
        replace(run, after=replace(run.after, values={**run.after.values,
            "kind": int(enum_for("operation_runs.kind").READ_FILE), "copy_id": 9}))))
    context = replace(context, state_rows={"file_copies": {9: {"slot_device_id": slot}}})
    reader = PreparationReads(create_autospec(sqlite3.Connection, instance=True))
    monkeypatch.setattr(reader, "device_start_in_flight", lambda device_id: busy)
    monkeypatch.setattr(operations, "_read_attempt_has_slot", lambda state, copy_id, round: True)
    context = replace(context, preparation_reads=reader)
    if allowed:
        _attempt_intent_guard(event, context)
    else:
        with pytest.raises(EventValidationError, match="启动"):
            _attempt_intent_guard(event, context)


def test_read_attempt_waits_for_in_flight_start(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    action = {"id": 7}
    run = {"id": 3, "status": 1, "attempts_used": 0}
    monkeypatch.setattr(operations, "_saved_transaction_events", lambda connection, key: None)
    monkeypatch.setattr(operations, "_load_row", lambda connection, table, identity:
        action if table == "actions" else run)
    def load_context(connection, kind, action_id, target, purpose, state):
        state["file_copies"] = {9: {"slot_device_id": "cam"}}
    monkeypatch.setattr(operations, "_load_flow_context", load_context)
    monkeypatch.setattr(operations, "intent_recovery_locked", lambda connection, intent: False)
    monkeypatch.setattr(operations, "_verify_obtain_config", lambda run, state: None)
    monkeypatch.setattr(operations, "_find_responsibility", lambda connection, intent: 3)
    monkeypatch.setattr(operations.BeginAttemptCommand, "_verify_existing_run", lambda *args: None)
    monkeypatch.setattr(operations, "_load_read_attempt_context", lambda *args: None)
    monkeypatch.setattr(operations, "_read_attempt_has_slot", lambda *args: True)
    monkeypatch.setattr(operations, "device_start_in_flight", lambda connection, device_id: True)
    intent = AttemptIntent("read", 7, OperationKind.READ_FILE, AttemptTarget(copy_id=9), None,
        AttemptConfig(2, Decimal("5"), Decimal("1")), 15, copy_round=1)
    plan = operations.BeginAttemptCommand(intent, new_operation_key()).plan(TransactionScope(connection, 4, 10))
    assert plan.result.disposition is BeginDisposition.REJECTED
    assert plan.result.reason == "device_busy" and not plan.events
