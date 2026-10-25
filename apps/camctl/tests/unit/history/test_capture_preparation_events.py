"""准备失败使用独立历史分支，不产生动作开始事实。"""

from dataclasses import replace

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventValidationError, validate_event_structure


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
