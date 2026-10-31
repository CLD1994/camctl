"""Action6 分段录像的原正常停止事实。"""

from collections.abc import Mapping
from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID
from camctl.devices.tasks import CaptureResultPolicy, StartReturn
from camctl.persistence.fact_reads import read_event
from camctl.persistence.transaction import row_facts


def verify_normal_recording_stop(connection, event, attempt, run, action, *, check_start=True):
    """核对原结果的固定身份与冻结目标，不重演会话单调钟。"""
    source = event.evidence.get("normal_recording_stop")
    if not isinstance(source, Mapping) or set(source) != {
        "start_result_event_id", "elapsed_before_stop_ns"
    }:
        raise ConsistencyError("正常录像停止来源格式无效")
    start_id, elapsed = source["start_result_event_id"], source["elapsed_before_stop_ns"]
    if (not is_json_integer(start_id) or not 1 <= start_id <= MAX_OBJECT_ID
            or not is_json_integer(elapsed) or not 0 <= elapsed <= MAX_OBJECT_ID):
        raise ConsistencyError("正常录像停止来源身份或计时无效")
    spec = action.get("execution_spec_json")
    if (not isinstance(spec, Mapping) or action.get("type") != int(enum_for("actions.type").CAMERA_RECORD)
            or spec.get("result_policy") != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
            or run.get("kind") != int(enum_for("operation_runs.kind").STOP) or run.get("action_id") != action.get("id")
            or run.get("responsibility_key") != f"stop/{action['id']}"
            or attempt.get("run_id") != run.get("id") or attempt.get("attempt_no") != 1
            or attempt.get("status") != int(enum_for("operation_attempts.status").SUCCEEDED)
            or attempt.get("effect_state") != int(enum_for("operation_attempts.effect_state").CONFIRMED)
            or attempt.get("error_json") is not None
            or attempt.get("result_event_id") != event.event_id
            or not is_json_integer(attempt.get("intent_event_id"))
            or not start_id < attempt["intent_event_id"] < event.event_id):
        raise ConsistencyError("正常录像停止不属于原成功 STOP")
    duration, margin = spec.get("target_duration_ms"), spec.get("startup_margin_ms", 0)
    if (not is_json_integer(duration) or duration <= 0
            or not is_json_integer(margin) or margin < 0
            or elapsed < (duration + margin) * 1_000_000):
        raise ConsistencyError("正常录像停止尚未达到冻结停止时点")
    activity = row_facts(connection, "device_activities", run.get("activity_id"))
    if activity is None or activity.get("action_id") != action["id"]:
        raise ConsistencyError("正常录像停止缺少原活动")
    stop_result = attempt.get("result_json")
    stop_observations = stop_result.get("observations") if isinstance(stop_result, Mapping) else None
    if (not isinstance(stop_observations, list)
            or not any(isinstance(item, Mapping) and item.get("type") == "stop_confirmed"
                       and item.get("version") == 1 and isinstance(item.get("data"), Mapping)
                       and item["data"].get("activity_id") == str(activity["id"])
                       for item in stop_observations)):
        raise ConsistencyError("正常录像停止缺少原活动的实际停止观察")
    if not check_start:
        return
    start_event = read_event(connection, start_id).event
    changes = [row for row in start_event.rows if row.table == "operation_attempts"]
    if start_event.event_type != 12 or len(changes) != 1:
        raise ConsistencyError("正常录像停止缺少原 START 结果")
    original = changes[0]
    start = row_facts(connection, "operation_attempts", original.row_id)
    start_run = None if start is None else row_facts(connection, "operation_runs", start.get("run_id"))
    if (start is None or start_run is None or start.get("result_event_id") != start_id
            or start.get("status") != int(enum_for("operation_attempts.status").SUCCEEDED)
            or start.get("effect_state") != int(enum_for("operation_attempts.effect_state").CONFIRMED)
            or start.get("error_json") is not None or start_run.get("kind") != int(enum_for("operation_runs.kind").START)
            or start_run.get("action_id") != action["id"]
            or start_run.get("activity_id") != activity["id"]
            or start_run.get("responsibility_key") != f"start/{action['id']}"
            or any(not json_equal(original.after.values.get(name), start.get(name))
                   for name in ("status", "effect_state", "result_json", "error_json"))):
        raise ConsistencyError("正常录像停止引用的 START 结果不一致")
    meaning = spec.get("start_return_meaning", int(StartReturn.STARTED))
    expected = {int(StartReturn.SENT): ("recording_sent", "sent_at"),
                int(StartReturn.STARTED): ("start_confirmed", "started_at")}.get(meaning)
    result = start.get("result_json")
    observations = result.get("observations") if isinstance(result, Mapping) else None
    if (expected is None or not isinstance(observations, list)
            or activity.get(expected[1]) != start_event.occurred_at
            or not any(isinstance(item, Mapping) and item.get("type") == expected[0]
                       and item.get("version") == 1 and isinstance(item.get("data"), Mapping)
                       and item["data"].get("activity_id") == str(activity["id"])
                       for item in observations)):
        raise ConsistencyError("原 START 不符合冻结返回语义")


def verify_normal_stop_group(event, context, attempt, run):
    """原事务内独立核验停止结果、流程结束和活动结束的完整组。"""
    reads = context.current_reads
    if reads is None or not hasattr(reads, "connection"):
        raise ConsistencyError("正常录像停止缺少原事务读取")
    transaction = context.transaction
    if event.event_id != transaction.first_event_id or transaction.last_event_id != event.event_id + 2:
        raise ConsistencyError("正常录像停止缺少完整结果事务")
    action = row_facts(reads.connection, "actions", run.get("action_id"))
    if action is None:
        raise ConsistencyError("正常录像停止缺少原动作")
    verify_normal_recording_stop(reads.connection, event, attempt, run, action)
    finish = reads.transaction_event(event.event_id + 1, transaction)
    end = reads.transaction_event(event.event_id + 2, transaction)
    if ((finish.event_type, finish.reason) != (10, 3)
            or finish.occurred_at != event.occurred_at or len(finish.rows) != 1
            or finish.rows[0].table != "operation_runs" or finish.rows[0].row_id != run["id"]
            or finish.rows[0].after.values.get("status") != int(enum_for("operation_runs.status").SUCCEEDED)):
        raise ConsistencyError("正常录像停止缺少原流程成功收场")
    observation = end.evidence.get("observation")
    if ((end.event_type, end.reason) != (13, 2) or end.occurred_at != event.occurred_at
            or len(end.rows) != 1 or end.rows[0].table != "device_activities"
            or end.rows[0].row_id != run["activity_id"]
            or end.rows[0].after.values.get("activity_state") != int(enum_for("device_activities.activity_state").ENDED)
            or not isinstance(observation, Mapping)
            or set(observation) != {"stop_result_event_id", "control_elapsed_ns"}
            or observation["stop_result_event_id"] != event.event_id
            or not is_json_integer(observation["control_elapsed_ns"])
            or observation["control_elapsed_ns"] < event.evidence["normal_recording_stop"]["elapsed_before_stop_ns"]):
        raise ConsistencyError("正常录像停止缺少同组原活动结束事实")


def read_normal_recording_stop(connection, action, activity_id):
    """按原活动的 STOP 结果读取已形成的正常来源。"""
    with closing(connection.execute(
        "SELECT a.id,a.result_event_id FROM operation_runs r "
        "JOIN operation_attempts a ON a.run_id=r.id "
        "WHERE r.responsibility_key=? AND r.activity_id=? AND a.effect_state=? "
        "AND a.result_event_id IS NOT NULL ORDER BY a.attempt_no DESC LIMIT 1",
        (f"stop/{action['id']}", activity_id, int(enum_for("operation_attempts.effect_state").CONFIRMED)),
    )) as cursor:
        found = cursor.fetchone()
    if found is None:
        return False
    attempt = row_facts(connection, "operation_attempts", found[0])
    if attempt is None or attempt.get("result_event_id") != found[1]:
        raise ConsistencyError("原 STOP 结果与尝试不一致")
    run = row_facts(connection, "operation_runs", attempt["run_id"])
    if run is None or run.get("action_id") != action["id"] or run.get("activity_id") != activity_id:
        raise ConsistencyError("原 STOP 结果与活动不一致")
    event = read_event(connection, found[1]).event
    rows = [row for row in event.rows if row.table == "operation_attempts"]
    if (event.event_type != 12 or len(rows) != 1 or rows[0].row_id != attempt["id"]
            or any(not json_equal(rows[0].after.values.get(name), attempt.get(name))
                   for name in ("status", "effect_state", "result_json", "error_json"))):
        raise ConsistencyError("原 STOP 结果事件与尝试不一致")
    if "normal_recording_stop" not in event.evidence:
        return False
    verify_normal_recording_stop(connection, event, attempt, run, action, check_start=False)
    return True
