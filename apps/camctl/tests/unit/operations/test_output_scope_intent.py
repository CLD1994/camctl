"""范围收尾的意图只消费已结束且仍持有原固定基准的活动。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.operations import _attempt_intent_guard, _query_configuration_guard
from camctl.persistence.transaction import event_envelope, row_change


def _intent(**activity_changes):
    run = dict(action_id=7, activity_id=11,
        kind=int(enum_for("operation_runs.kind").RESOLVE_OUTPUT_SCOPE), attempts_used=1,
        max_attempts_used=2, timeout_s_json=Decimal("10"), retry_interval_s_json=Decimal("1"))
    attempt = dict(run_id=3, attempt_no=1, intent_event_id=21, status=1,
        effect_state=int(enum_for("operation_attempts.effect_state").UNKNOWN),
        result_event_id=None, result_json=None, error_json=None, copy_round=None)
    event = event_envelope(21, 8, 11, 1,
        (row_change("operation_attempts", 5, attempt), row_change("operation_runs", 3, run)), 10)
    state = {"actions": {7: dict(id=7, type=2, status=4)},
        "device_activities": {11: dict(id=11, action_id=7, activity_state=3,
            occupancy_state=1, baseline_state=3, output_set_finalized_event_id=None,
            **activity_changes)}}
    return event, EventContext(TransactionRange(8, 21, 21), {}, state)


def test_scope_intent_preserves_original_terminal_action():
    event, context = _intent()
    _attempt_intent_guard(event, context)
    _query_configuration_guard(event, context)
    assert context.state_rows["actions"][7]["status"] == 4


@pytest.mark.parametrize("changes", [
    {"activity_state": 2}, {"occupancy_state": 2}, {"baseline_state": 2},
    {"output_set_finalized_event_id": 9},
])
def test_scope_intent_rejects_unqualified_original_activity(changes):
    event, context = _intent()
    context.state_rows["device_activities"][11].update(changes)
    with pytest.raises(EventValidationError):
        _attempt_intent_guard(event, context)


@pytest.mark.parametrize("column", ["timeout_s_json", "retry_interval_s_json"])
def test_scope_intent_requires_full_result_check_configuration(column):
    event, context = _intent()
    run = event.rows[1]
    event = replace(event, rows=(event.rows[0], replace(run,
        after=replace(run.after, values={**run.after.values, column: None}))))
    with pytest.raises(EventValidationError):
        _query_configuration_guard(event, context)
