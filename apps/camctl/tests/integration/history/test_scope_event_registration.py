"""真实包内事件登记允许范围责任沿普通尝试、重试及收尾保存。"""

from decimal import Decimal

import pytest

from camctl.contracts.enums import enum_for
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
