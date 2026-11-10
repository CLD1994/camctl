"""根据已取得的原活动、操作结果和观察判断采集结束依据。"""

from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal


def result_page_confirms_completion(observation, activity_id, event_id):
    actual = observation.get("completion_evidence")
    return (
        set(observation) == {"result_page_event_id", "completion_evidence"}
        and is_json_integer(observation["result_page_event_id"])
        and 0 < observation["result_page_event_id"] < event_id
        and isinstance(actual, Mapping)
        and set(actual) == {"type", "version", "data"}
        and isinstance(actual["type"], str) and bool(actual["type"])
        and is_json_integer(actual["version"]) and actual["version"] > 0
        and isinstance(actual["data"], Mapping)
        and actual["data"].get("activity_id") == str(activity_id)
    )


def start_result_confirms_completion(observation, activity, activity_id, event_id, runs, attempts):
    result_id = observation["start_result_event_id"]
    return (set(observation) == {"start_result_event_id"}
        and is_json_integer(result_id) and 0 < result_id < event_id
        and activity.get("start_return_meaning") == 3
        and any(attempt.get("result_event_id") == result_id
            and attempt.get("status") == int(enum_for("operation_attempts.status").SUCCEEDED)
            and attempt.get("effect_state") == int(enum_for("operation_attempts.effect_state").CONFIRMED)
            and attempt.get("error_json") is None
            and any(run.get("kind") == int(enum_for("operation_runs.kind").START)
                and run.get("action_id") == activity.get("action_id")
                and run.get("activity_id") == activity_id
                and run.get("id") == attempt.get("run_id") for run in runs)
            for attempt in attempts))


def stop_result_confirms_completion(observation, activity, action, activity_id, event_id,
                                    after, runs, attempts):
    result_id, elapsed = observation["stop_result_event_id"], observation.get("control_elapsed_ns")
    end_control = ((2 if "control_elapsed_ns" in observation else 1) if action.get("type") == 2
                   else action.get("execution_spec_json", {}).get("end_control"))
    shape = (set(observation) == {"stop_result_event_id"} if end_control == 1
        else set(observation) == {"stop_result_event_id", "control_elapsed_ns"} if end_control == 2
        else False)
    return (shape
        and is_json_integer(result_id) and 0 < result_id < event_id
        and action.get("type") in (2, 3)
        and json_equal(after.get("control_elapsed_ns"), elapsed)
        and any(attempt.get("result_event_id") == result_id
            and attempt.get("status") != int(enum_for("operation_attempts.status").RUNNING)
            and attempt.get("effect_state") == int(enum_for("operation_attempts.effect_state").CONFIRMED)
            and any(run.get("kind") == int(enum_for("operation_runs.kind").STOP)
                and run.get("action_id") == activity.get("action_id")
                and run.get("activity_id") == activity_id
                and run.get("id") == attempt.get("run_id") for run in runs)
            for attempt in attempts))
