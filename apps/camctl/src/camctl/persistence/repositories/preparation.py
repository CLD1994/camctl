"""原事务独立核对设置准备调用，不从固定基准推断调用结束。"""
from camctl.contracts.enums import decode_member, enum_for
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES

from .capture_facts import preparation_calls_resolved


def require_action_preparation_format(owned, action_id):
    """准备写入口按原动作类型核对其所需的数据库格式。"""
    from camctl.persistence.runtime import require_capture_preparation_format
    from camctl.persistence.transaction import TransactionError, row_facts
    action = row_facts(owned.connection, "actions", action_id)
    if action is None:
        raise TransactionError("拍摄准备缺少原动作")
    require_capture_preparation_format(owned.metadata.format_version, action_type=action["type"])


class PreparationReads:
    def __init__(self, connection, validated=()):
        self.connection = connection
        self.events = tuple(accepted.envelope for accepted in validated)

    def can_prepare(self, action, activity, occurred_at):
        from contextlib import closing
        from camctl.scheduling.resources import capture_scope_blocked
        if (not preparation_action_eligible(self.connection, action, activity)
                or occurred_at > action["scheduled_at"] + action["max_delay_ms"] * 1000
                or capture_scope_blocked(self.connection, action["device_id"], action["id"],
                                         activity["ownership_mode"], activity["output_scope_json"])):
            return False
        with closing(self.connection.execute("SELECT 1 FROM file_copies WHERE slot_device_id=? LIMIT 1",
                                            (action["device_id"],))) as cursor:
            if cursor.fetchone() is not None:
                return False
        with closing(self.connection.execute(
                "SELECT 1 FROM device_activities d JOIN actions a ON a.id=d.action_id"
                " WHERE a.device_id=? AND d.action_id<>? AND d.occupancy_state=1"
                " AND d.activity_state<>3 AND d.dispatch_state NOT IN (1,4) LIMIT 1",
                (action["device_id"], action["id"]))) as cursor:
            return cursor.fetchone() is None

    def verify_create(self, event, context, row):
        from contextlib import closing
        from decimal import Decimal
        from camctl.contracts.values import ConsistencyError
        from camctl.contracts.json_values import json_equal
        from camctl.capture.models import activity_capabilities
        from .scheduling import ReservePreparationRequest
        raw = event.evidence.get("preparation_request")
        from camctl.history.validators import parse_preparation_request
        request = ReservePreparationRequest(**parse_preparation_request(raw))
        action = context.state_rows.get("actions", {}).get(request.action_id)
        if (action is None or (action.get("status"), action.get("execution_started")) != (1, 0)
                or action.get("cancel_requested") != 0 or action.get("type") not in CAPTURE_ACTION_TYPES
                or row.after.values.get("action_id") != request.action_id
                or event.occurred_at != request.occurred_at):
            raise ConsistencyError("准备活动不属于原pending拍摄动作")
        if photo_start_recorded(self.connection, action):
            raise ConsistencyError("已有照片START责任不能建立准备活动")
        if not (Decimal(action["scheduled_at"]) - request.prepare_lead_s * 1_000_000
                <= request.trusted_wall_now <= action["scheduled_at"] + action["max_delay_ms"] * 1000):
            raise ConsistencyError("准备活动没有原提前区间的时间资格")
        capabilities = activity_capabilities(decode_member("actions.type", action["type"]).name.lower(),
                                            action["execution_spec_json"])
        for column in ("state_query_supported", "stop_supported", "safe_repeat_stop", "start_return_meaning",
                       "completion_mode", "ownership_mode", "output_scope_json"):
            if not json_equal(row.after.values.get(column), getattr(capabilities, column)):
                raise ConsistencyError("准备活动固定能力与原执行定义不符")
        with closing(self.connection.execute(
                "SELECT 1 FROM device_activities d JOIN actions a ON a.id=d.action_id"
                " WHERE a.device_id=? AND d.occupancy_state=1 LIMIT 1", (action["device_id"],))) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("准备活动越过了同设备原占用")
        with closing(self.connection.execute("SELECT 1 FROM file_copies WHERE slot_device_id=? LIMIT 1",
                                            (action["device_id"],))) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("准备活动越过了同设备原读取调用")
        mine = (action["scheduled_at"], action["plan_id"], action["input_index"])
        with closing(self.connection.execute(
                "SELECT scheduled_at,plan_id,input_index,max_delay_ms FROM actions"
                f" WHERE device_id=? AND type IN ({','.join('?' for _ in CAPTURE_ACTION_TYPES)})"
                " AND status=1 AND execution_started=0"
                " AND cancel_requested=0 ORDER BY scheduled_at,plan_id,input_index",
                (action["device_id"], *CAPTURE_ACTION_TYPES))) as cursor:
            for scheduled, plan_id, index, delay in cursor:
                if (scheduled, plan_id, index) >= mine:
                    break
                if (Decimal(scheduled) - request.prepare_lead_s * 1_000_000
                        <= request.trusted_wall_now <= scheduled + delay * 1000):
                    raise ConsistencyError("准备活动越过了原候选顺序")

    def verify_failure(self, action, details):
        from .capture_facts import load_start_facts
        from camctl.contracts.values import ConsistencyError
        facts = load_start_facts(self.connection, action)
        if (facts.activity is None or details.get("activity_id") != str(facts.activity["id"])
                or details.get("reason") not in ("device_failed", "baseline_read_failed")):
            raise ConsistencyError("准备失败详情不属于原活动")
        verify_failure_source(self.connection, action, facts, details["reason"])

    def cancel_ready(self, action):
        from .capture_facts import load_start_facts
        from .cancellation import _preparation_cancel_pending
        return not _preparation_cancel_pending(load_start_facts(self.connection, action))

    def calls_resolved(self, activity_id):
        return preparation_calls_resolved(self.connection, activity_id, result_events=self.events)


def preparation_action_eligible(connection, action, activity):
    if (action.get("type") not in CAPTURE_ACTION_TYPES or action.get("cancel_requested")
            or photo_start_recorded(connection, action)):
        return False
    if (action.get("status"), action.get("execution_started")) == (1, 0):
        return True
    if (action.get("status"), action.get("execution_started")) != (2, 1):
        return False
    from .capture_facts import load_start_facts
    facts = load_start_facts(connection, action)
    return (facts.activity is not None and facts.activity["id"] == activity["id"]
            and facts.not_started)


def photo_start_recorded(connection, action):
    """照片已有START责任时只沿原单次拍摄收场，不重新准备。"""
    from contextlib import closing
    if action.get("type") != int(enum_for("actions.type").CAMERA_TAKE_PHOTO):
        return False
    with closing(connection.execute(
            "SELECT 1 FROM operation_runs WHERE action_id=? AND kind=? LIMIT 1",
            (action["id"], int(enum_for("operation_runs.kind").START)))) as cursor:
        return cursor.fetchone() is not None


def verify_failure_source(connection, action, facts, reason):
    """准备失败只能引用真实失败；未返回的责任继续等待。"""
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    if facts.activity is None or not facts.not_started or not facts.preparation_resolved:
        raise ConsistencyError("准备失败缺少已收场且无启动效果的原活动")
    if photo_start_recorded(connection, action):
        raise ConsistencyError("已有照片START责任不能保存准备失败")
    activity_id = facts.activity["id"]
    with closing(connection.execute(
            "SELECT 1 FROM baseline_read_calls WHERE activity_id=? AND state=1 LIMIT 1",
            (activity_id,))) as cursor:
        if cursor.fetchone() is not None:
            raise ConsistencyError("原基准调用尚未收场")
    with closing(connection.execute("SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1",
                                    (action["id"],))) as cursor:
        if cursor.fetchone() is not None:
            raise ConsistencyError("未启动的准备失败不能已有正式产物")
    if reason == "device_failed":
        with closing(connection.execute(
                "SELECT r.status,t.status,t.error_json,t.result_event_id FROM operation_runs r"
                " LEFT JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=1"
                " WHERE r.activity_id=? AND r.kind=? ORDER BY r.id DESC LIMIT 1",
                (activity_id, int(enum_for("operation_runs.kind").PREPARE_CAPTURE)))) as cursor:
            latest = cursor.fetchone()
        if (latest is None or latest[0] not in (4, 6) or latest[1] not in (3, 4)
                or latest[2] is None or latest[3] is None):
            raise ConsistencyError("最近一次设置准备没有已保存的真实失败结果")
    else:
        with closing(connection.execute(
                "SELECT state,error_json,result_event_id FROM baseline_read_calls"
                " WHERE activity_id=? ORDER BY read_no DESC LIMIT 1", (activity_id,))) as cursor:
            latest = cursor.fetchone()
        if (latest is None or latest[0] not in (2, 3) or latest[2] is None
                or (latest[0] == 2 and latest[1] is None)):
            raise ConsistencyError("最近一次目录基准没有已保存的失败或未知结果")
