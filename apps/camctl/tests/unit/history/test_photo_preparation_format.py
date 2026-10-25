"""照片准备失败使用原动作事实验证权威事件分支。"""

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import validators
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError


@pytest.mark.parametrize("action_type", [1, 2, 3, 8])
def test_preparation_failure_branch_uses_original_capture_type(monkeypatch, action_type):
    event = EventEnvelope(
        event_id=101, transaction_id=7, event_type=8, event_version=1,
        occurred_at=1, clock_status=2, change_seq=9, reason=5,
        evidence={"preparation_resolved": True, "preparation_failure_request": {
            "action_id": 8, "reason": "device_failed", "occurred_at": 1,
        }},
        rows=(RowChange(table="actions", row_id=8,
            before=RowImage(exists=True, values={"status": 1, "error_code": None, "error_details_json": None}),
            after=RowImage(exists=True, values={"status": 4, "error_code": 13,
                "error_details_json": {"activity_id": "11", "reason": "device_failed"}})),),
    )
    _, _, branch = validators.validate_event_structure(event)
    # 本单元核对完整context的分支条件，具名业务守卫由其各自单元验证。
    monkeypatch.setattr(validators, "NAMED_GUARDS", {
        name: lambda event, context: None for name in branch["guards"]
    })
    context = EventContext(TransactionRange(7, 101, 101), {("actions", 8): ("action", 8)},
        {"actions": {8: {"type": action_type, "status": 1,
                         "execution_started": 0, "cancel_requested": 0}}})
    if action_type in (1, 2, 3):
        validated = validators.validate_event(event, context)
        assert validated.branch_name == "PREPARATION_FAILED"
    else:
        with pytest.raises(EventValidationError):
            validators.validate_event(event, context)
