"""停止观察须关联原 STOP 实际结果，不能借其他已结束流程证明。"""

from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.capture import _activity_guard, _RUN_KIND, _ATTEMPT_STATUS, _ATTEMPT_RESULT_EVENT
from camctl.contracts.enums import enum_for

_CONFIRMED = int(enum_for("operation_attempts.effect_state").CONFIRMED)


class OriginalStopReads:
    def __init__(self, event):
        self.event = event

    def transaction_event(self, identity, transaction):
        assert identity == self.event.event_id
        assert transaction.first_event_id <= identity <= transaction.last_event_id
        return self.event


def world(elapsed=10):
    before, after = {"activity_state": 2}, {"activity_state": 3}
    if elapsed is not None:
        before["control_elapsed_ns"], after["control_elapsed_ns"] = None, elapsed
    event = EventEnvelope(20, 1, 13, 1, 0, 1, None, 2,
        {"observation": {"stop_result_event_id": 18, "control_elapsed_ns": elapsed}},
        (RowChange("device_activities", 7, RowImage(True, before), RowImage(True, after)),))
    original = EventEnvelope(18, 1, _ATTEMPT_RESULT_EVENT, 1, 0, 1, None, 1, {},
        (RowChange("operation_attempts", 11, RowImage(True, {"status": 1}),
            RowImage(True, {"status": int(_ATTEMPT_STATUS.SUCCEEDED)})),))
    context = EventContext(TransactionRange(1, 18, 20), {}, {
        "device_activities": {7: {"action_id": 3, "capture_json": None, "last_error_json": None}},
        "actions": {3: {"type": 3, "execution_spec_json": {"end_control": 2}}},
        "operation_runs": {9: {"id": 9, "status": 3, "kind": int(_RUN_KIND.STOP),
            "action_id": 3, "activity_id": 7}},
        "operation_attempts": {11: {"run_id": 9, "result_event_id": 18,
            "status": int(_ATTEMPT_STATUS.SUCCEEDED), "effect_state": _CONFIRMED}},
    }, current_reads=OriginalStopReads(original))
    return event, context


@pytest.mark.parametrize("elapsed", [None, 0, 10])
def test_actual_host_stop_observation_is_valid(elapsed):
    event, context = world(elapsed)
    _activity_guard(event, context)


@pytest.mark.parametrize("source", ["missing", "different_time", "different_type", "different_attempt"])
def test_elapsed_requires_actual_original_stop_in_same_group(source):
    event, context = world()
    original = context.current_reads.event
    if source == "missing":
        reads = None
    else:
        changes = {"different_time": {"occurred_at": 1},
            "different_type": {"event_type": event.event_type},
            "different_attempt": {"rows": (replace(original.rows[0], row_id=12),)}}
        reads = OriginalStopReads(replace(original, **changes[source]))
    with pytest.raises(EventValidationError):
        _activity_guard(event, replace(context, current_reads=reads))


@pytest.mark.parametrize("change", [
    {"stop_result_event_id": 17}, {"stop_result_event_id": True},
    {"stop_result_event_id": 21}, {"control_elapsed_ns": 11},
    {"control_elapsed_ns": None}, {"control_elapsed_ns": True}, {"extra": 1},
])
def test_host_stop_observation_rejects_changed_source_or_duration(change):
    event, context = world()
    observation = {**event.evidence["observation"], **change}
    with pytest.raises(EventValidationError):
        _activity_guard(replace(event, evidence={"observation": observation}), context)


@pytest.mark.parametrize("table,change", [
    ("operation_runs", {"kind": int(_RUN_KIND.START)}),
    ("operation_runs", {"activity_id": 8}),
    ("operation_runs", {"action_id": 4}),
    ("operation_attempts", {"effect_state": 1}),
    ("operation_attempts", {"status": int(_ATTEMPT_STATUS.RUNNING)}),
    ("actions", {"execution_spec_json": {"end_control": 1}}),
])
def test_host_stop_observation_rejects_missing_original_qualification(table, change):
    event, context = world()
    identity, original = next(iter(context.state_rows[table].items()))
    states = {**context.state_rows, table: {identity: {**original, **change}}}
    with pytest.raises(EventValidationError):
        _activity_guard(event, replace(context, state_rows=states))
