"""根据已取得的应急尝试与目标活动事实判断停止补记。"""

from collections.abc import Mapping

from camctl.contracts.json_values import is_json_integer, json_equal

_CONFIG_COLUMNS = ("max_attempts_used", "timeout_s_json", "retry_interval_s_json")


def emergency_identity_error(run):
    session_key = run.get("session_key")
    if not isinstance(session_key, str) or len(session_key) != 32:
        return "应急流程行必须携带本会话身份"
    if run.get("responsibility_key") != f"emergency/{session_key}/{run.get('activity_id')}":
        return "应急责任键与会话及活动不符"
    if run.get("kind") != 9 or run.get("retry_wait_required") != 0:
        return "应急流程必须是补记终态"
    return None


def emergency_run_status_error(run, attempts):
    if attempts and any(run.get(column) is None for column in _CONFIG_COLUMNS):
        return "已有应急尝试必须保存完整的固定配置"
    status = run.get("status")
    if status not in (3, 4, 6):
        return "应急补记不保存进行中状态"
    if status == 3 and run.get("error_json") is not None:
        return "应急成功不携带流程错误"
    if status in (4, 6) and run.get("error_json") is None:
        return "应急失败或未确认必须携带原因"
    return None


def emergency_failure_error(run):
    """公共错误结构已核验后，检查结果分区及实际目标身份。"""
    error = run["error_json"]
    expected = "emergency_not_attempted" if run.get("status") == 4 else "emergency_stop_unconfirmed"
    if error["code"] != expected:
        return "应急错误与最终结果分区不符"
    if error["details"]["activity_id"] != str(run.get("activity_id")):
        return "应急错误必须指向本流程的实际目标活动"
    return None


def _structured_observation(observation):
    return (isinstance(observation, Mapping)
        and isinstance(observation.get("type"), str) and bool(observation.get("type"))
        and is_json_integer(observation.get("version")) and observation["version"] > 0
        and isinstance(observation.get("data"), Mapping))


def _attempt_confirms_stop(attempt, activity_id):
    result = attempt.get("result_json")
    if not isinstance(result, Mapping):
        return False
    observations = result.get("observations")
    if not isinstance(observations, list):
        return False
    return any(_structured_observation(item) and item["data"].get("activity_id") == str(activity_id)
               for item in observations)


def emergency_attempt_presence_error(run, attempts):
    status = run.get("status")
    if status == 6 and not attempts:
        return "停止未确认的补记必须有实际尝试"
    if status == 4 and attempts:
        return "未能尝试的补记不创建尝试行"
    return None


def emergency_observation_error(run, attempts, observation):
    status = run.get("status")
    if observation is not None and status != 3:
        return "停止依据只伴随停止成功的补记"
    if observation is not None or (status == 3 and not attempts):
        if not _structured_observation(observation):
            return "停止的补记必须携带结构化停止依据"
        if observation["data"].get("activity_id") != str(run.get("activity_id")):
            return "停止依据必须指向本补记的目标活动"
    if status == 3 and attempts and observation is None:
        if not any(_attempt_confirms_stop(attempt, run.get("activity_id")) for attempt in attempts):
            return "停止成功的补记必须在尝试结果中保存指向目标活动的停止观察"
    return None


def emergency_target_error(run, activity):
    if "last_error_json" not in activity:
        return "应急补记缺少可靠的目标活动错误"
    if not json_equal(activity["last_error_json"], run.get("error_json")):
        return "应急流程与目标活动的完整错误不一致"
    return None


def emergency_attempt_count_error(run, attempts):
    if not all(attempt.get("attempt_no") == number for number, attempt in enumerate(attempts, 1)):
        return "应急尝试必须从 1 连续编号"
    if run.get("attempts_used") != len(attempts):
        return "累计次数与尝试行数不符"
    maximum = run.get("max_attempts_used")
    if maximum is not None and len(attempts) > maximum:
        return "尝试次数超过本会话固定限额"
    return None


def emergency_attempt_error(run, run_id, event_id, attempt):
    if any(not json_equal(attempt.get(column), run.get(column)) for column in _CONFIG_COLUMNS):
        return "每条应急尝试必须采用本会话的相同固定配置"
    if attempt.get("run_id") != run_id:
        return "应急尝试必须归属本次流程"
    if attempt.get("intent_event_id") is not None:
        return "应急尝试不携带普通意图引用"
    if attempt.get("result_event_id") != event_id:
        return "应急尝试结果必须指向本事件"
    if attempt.get("status") == 1:
        return "应急补记不保存运行中尝试"
    if attempt.get("copy_round") is not None:
        return "应急尝试没有拷贝轮次"
    return None
