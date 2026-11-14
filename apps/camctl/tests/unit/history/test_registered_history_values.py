"""随版本固定的历史登记、状态转换与恢复种子的局部契约。"""

import json
from decimal import Decimal

import pytest

from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.values import ConsistencyError, to_utc_micros
from camctl.history.decoding import decode_event_row
from camctl.history.replay import EntityImage, ReplayError, RestoreSeed, restore
from camctl.history.events import EventEnvelope, RowChange, RowImage, business_columns, load_event_registry
from camctl.history.validators import validate_event_structure


@pytest.mark.parametrize("event_name,branch_name,row_index,side", [
    ("OPERATION_CONFIGURED", "CREATE", 0, "after"),
    ("OPERATION_CONFIGURED", "CONFIGURE", 0, "before"),
    ("OPERATION_CONFIGURED", "FINISH", 0, "before"),
    ("ATTEMPT_STARTED", "NEW", 1, "before"),
    ("ATTEMPT_STARTED", "NEW", 3, "after"),
    ("RETRY_WAIT_CHANGED", "REQUIRE", 0, "before"),
    ("RETRY_WAIT_CHANGED", "COMPLETE", 0, "before"),
    ("RETRY_WAIT_CHANGED", "ABANDON", 0, "before"),
])
def test_scope_kind_is_allowed_throughout_ordinary_operation_events(event_name, branch_name, row_index, side):
    row = load_event_registry()["events"][event_name]["branches"][branch_name]["rows"][row_index]
    assert row["table"] == "operation_runs"
    assert int(enum_for("operation_runs.kind").RESOLVE_OUTPUT_SCOPE) in row[side]["kind"]


def test_real_scope_intent_passes_the_normal_event_structure_validator():
    values = dict.fromkeys(business_columns("operation_runs"))
    values.update(action_id=43, kind=int(enum_for("operation_runs.kind").RESOLVE_OUTPUT_SCOPE),
        responsibility_key="scope/20", activity_id=20, status=2, attempts_used=1,
        max_attempts_used=3, timeout_s_json=Decimal("10"), retry_interval_s_json=Decimal("3"),
        retry_wait_required=0)
    event = EventEnvelope(101, 7, 11, 1, 1, 1, None, 1, {},
        (RowChange("operation_runs", 88, RowImage(False, {}), RowImage(True, values)),))
    assert validate_event_structure(event)[:2] == ("ATTEMPT_STARTED", "NEW")


def test_registered_observation_rejects_later_illegal_state_transition():
    body = {"reason": 2, "evidence": {"observation": {}}, "rows": [{
        "table": "device_activities", "id": 1,
        "before": {"exists": True, "values": {"dispatch_state": 1, "activity_state": 3}},
        "after": {"exists": True, "values": {"dispatch_state": 2, "activity_state": 2}},
    }]}
    with pytest.raises(ConsistencyError):
        decode_event_row((1, 1, 13, 1, 0, 2, None, json.dumps(body)))


_INITIAL_ROW = {
    "id": 1,
    "acknowledged_wm": 0,
    "acknowledged_report_id": None,
    "trusted_time_lower_bound": None,
    "trusted_time_event_id": None,
    "cleanup_cursor_file_id": None,
}
NOW = to_utc_micros("2026-10-08 09:00:00")

@pytest.mark.parametrize("fault", ["missing_initial_column", "wrong_initial_value", "ordinary_object"])
def test_pure_restore_rejects_unreliable_existing_zero_count_seed(fault):
    """存在的零计数种子必须属于初始化单例且表达其完整初始事实。"""
    registry = load_registry()["history_objects"]
    values = {name: value for name, value in _INITIAL_ROW.items() if name != "id"}
    if fault == "missing_initial_column":
        values.pop("acknowledged_wm")
        image = EntityImage(registry["runtime_state"]["id"], 1, True,
                            {("runtime_state", 1): values}, 0, 0)
    elif fault == "wrong_initial_value":
        values["trusted_time_lower_bound"] = NOW
        values["trusted_time_event_id"] = 1
        image = EntityImage(registry["runtime_state"]["id"], 1, True,
                            {("runtime_state", 1): values}, 0, 0)
    else:
        image = EntityImage(registry["plan"]["id"], 1, True,
                            {("plans", 1): {"status": 1}}, 0, 0)

    with pytest.raises(ReplayError):
        restore(RestoreSeed(image, INITIAL_BOUNDARY), (), INITIAL_BOUNDARY)
