"""结束观察必须指向原活动及其先前可靠操作结果。"""

import pytest

from camctl.capture.activity_completion_rules import (
    result_page_confirms_completion, start_result_confirms_completion,
    stop_result_confirms_completion,
)
from camctl.contracts.enums import enum_for


@pytest.mark.parametrize("change", [
    {}, {"result_page_event_id": 20}, {"result_page_event_id": True},
    {"completion_evidence": {"type": "done", "version": 1, "data": {"activity_id": "8"}}},
    {"extra": True},
])
def test_page_completion_uses_prior_page_for_same_activity(change):
    observation = {"result_page_event_id": 18,
        "completion_evidence": {"type": "done", "version": 1, "data": {"activity_id": "7"}}}
    assert result_page_confirms_completion(observation | change, 7, 20) is (not change)


def _operation(kind):
    run = {"id": 9, "kind": int(getattr(enum_for("operation_runs.kind"), kind)),
           "action_id": 3, "activity_id": 7}
    attempt = {"run_id": 9, "result_event_id": 18,
        "status": int(enum_for("operation_attempts.status").SUCCEEDED),
        "effect_state": int(enum_for("operation_attempts.effect_state").CONFIRMED), "error_json": None}
    return run, attempt


@pytest.mark.parametrize("change", [None, "other_activity", "other_result", "error", "not_confirmed"])
def test_completed_start_requires_original_confirmed_result(change):
    run, attempt = _operation("START")
    if change == "other_activity": run["activity_id"] = 8
    if change == "other_result": attempt["result_event_id"] = 19
    if change == "error": attempt["error_json"] = {"code": "failed"}
    if change == "not_confirmed": attempt["effect_state"] = 1
    assert start_result_confirms_completion({"start_result_event_id": 18},
        {"action_id": 3, "start_return_meaning": 3}, 7, 20, [run], [attempt]) is (change is None)


@pytest.mark.parametrize("change", [None, "other_activity", "other_result", "running", "not_confirmed", "duration"])
def test_stop_requires_original_confirmed_result_and_matching_duration(change):
    run, attempt = _operation("STOP")
    after = {"control_elapsed_ns": 10}
    if change == "other_activity": run["activity_id"] = 8
    if change == "other_result": attempt["result_event_id"] = 19
    if change == "running": attempt["status"] = int(enum_for("operation_attempts.status").RUNNING)
    if change == "not_confirmed": attempt["effect_state"] = 1
    if change == "duration": after["control_elapsed_ns"] = 11
    assert stop_result_confirms_completion({"stop_result_event_id": 18, "control_elapsed_ns": 10},
        {"action_id": 3}, {"type": 3, "execution_spec_json": {"end_control": 2}},
        7, 20, after, [run], [attempt]) is (change is None)
