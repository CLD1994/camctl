"""应急补记按实际尝试、错误及停止观察决定最终结果。"""

from camctl.capture.emergency_rules import (
    emergency_attempt_count_error, emergency_attempt_error, emergency_attempt_presence_error,
    emergency_identity_error,
    emergency_observation_error, emergency_run_status_error, emergency_target_error,
)


def _run(status=3):
    return {"session_key": "a" * 32, "activity_id": 7,
        "responsibility_key": f"emergency/{'a' * 32}/7", "kind": 9,
        "retry_wait_required": 0, "status": status, "error_json": None,
        "attempts_used": 1, "max_attempts_used": 3,
        "timeout_s_json": 10, "retry_interval_s_json": 0}


def _attempt():
    return {"attempt_no": 1, "run_id": 9, "result_event_id": 20, "status": 2,
        "intent_event_id": None, "copy_round": None,
        "max_attempts_used": 3, "timeout_s_json": 10, "retry_interval_s_json": 0,
        "result_json": {"observations": [{"type": "stop_confirmed", "version": 1,
                                          "data": {"activity_id": "7"}}]}}


def test_stopped_result_uses_original_attempt_and_target_error():
    run, attempt = _run(), _attempt()
    assert emergency_identity_error(run) is None
    assert emergency_run_status_error(run, [attempt]) is None
    assert emergency_observation_error(run, [attempt], None) is None
    assert emergency_target_error(run, {"last_error_json": None}) is None
    assert emergency_attempt_count_error(run, [attempt]) is None
    assert emergency_attempt_error(run, 9, 20, attempt) is None


def test_other_activity_stop_observation_cannot_confirm_this_target():
    run, attempt = _run(), _attempt()
    attempt["result_json"]["observations"][0]["data"]["activity_id"] = "8"
    assert emergency_observation_error(run, [attempt], None)
    assert emergency_observation_error(run, [], {"type": "stop_confirmed", "version": 1,
                                                 "data": {"activity_id": "8"}})


def test_attempt_configuration_and_identity_remain_bound_to_original_run():
    run, attempt = _run(), _attempt()
    assert emergency_attempt_count_error(run, [attempt | {"attempt_no": 2}])
    assert emergency_attempt_error(run, 9, 20, attempt | {"timeout_s_json": 11})
    assert emergency_attempt_error(run, 9, 20, attempt | {"run_id": 8})
    assert emergency_attempt_error(run, 9, 20, attempt | {"result_event_id": 19})
    assert emergency_attempt_error(run, 9, 20, attempt | {"status": 1})


def test_unconfirmed_and_not_attempted_outcomes_keep_distinct_obligations():
    run = _run(6) | {"error_json": {"code": "emergency_stop_unconfirmed"}}
    assert emergency_attempt_presence_error(run, [])
    run = _run(4) | {"error_json": {"code": "emergency_not_attempted"}}
    assert emergency_attempt_presence_error(run, [_attempt()])
    assert emergency_run_status_error(_run(3) | {"error_json": {"code": "failure"}}, [])
