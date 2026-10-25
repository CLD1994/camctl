"""原事务独立核对设置准备调用，不从固定基准推断调用结束。"""
from .capture_facts import preparation_calls_resolved


class PreparationReads:
    def __init__(self, connection, validated=()):
        self.connection = connection
        self.events = tuple(accepted.envelope for accepted in validated)

    def can_prepare(self, action, activity):
        return preparation_action_eligible(self.connection, action, activity)

    def verify_create(self, event, context, row):
        from contextlib import closing
        from decimal import Decimal
        from camctl.contracts.values import ConsistencyError
        from camctl.contracts.json_values import json_equal
        from camctl.capture.models import activity_capabilities
        from .scheduling import ReservePreparationRequest, _CAMERA_TYPE_NAMES
        raw = event.evidence.get("preparation_request")
        if not isinstance(raw, dict) or set(raw) != {"action_id", "trusted_wall_now", "prepare_lead_s", "occurred_at"}:
            raise ConsistencyError("pending活动创建缺少完整准备申请")
        request = ReservePreparationRequest(**raw)
        action = context.state_rows.get("actions", {}).get(request.action_id)
        if (action is None or (action.get("status"), action.get("execution_started")) != (1, 0)
                or action.get("cancel_requested") != 0 or action.get("type") not in (2, 3)
                or row.after.values.get("action_id") != request.action_id
                or event.occurred_at != request.occurred_at):
            raise ConsistencyError("准备活动不属于原pending拍摄动作")
        if not (Decimal(action["scheduled_at"]) - request.prepare_lead_s * 1_000_000
                <= request.trusted_wall_now <= action["scheduled_at"] + action["max_delay_ms"] * 1000):
            raise ConsistencyError("准备活动没有原提前区间的时间资格")
        capabilities = activity_capabilities(_CAMERA_TYPE_NAMES[action["type"]], action["execution_spec_json"])
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
                " WHERE device_id=? AND type IN (2,3) AND status=1 AND execution_started=0"
                " AND cancel_requested=0 ORDER BY scheduled_at,plan_id,input_index", (action["device_id"],))) as cursor:
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

    def calls_resolved(self, activity_id):
        return preparation_calls_resolved(self.connection, activity_id, result_events=self.events)


def preparation_action_eligible(connection, action, activity):
    if action.get("cancel_requested"):
        return False
    if (action.get("status"), action.get("execution_started")) == (1, 0):
        return True
    if (action.get("status"), action.get("execution_started")) != (2, 1):
        return False
    from .capture_facts import load_start_facts
    facts = load_start_facts(connection, action)
    return (facts.activity is not None and facts.activity["id"] == activity["id"]
            and facts.attempts_used > 0 and facts.not_started)


def verify_failure_source(connection, action, facts, reason):
    """准备失败只能引用真实失败；未返回的责任继续等待。"""
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    if facts.activity is None or not facts.not_started or not facts.preparation_resolved:
        raise ConsistencyError("准备失败缺少已收场且无启动效果的原活动")
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
        sql = ("SELECT 1 FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
               " WHERE r.activity_id=? AND r.kind=? AND r.status IN (4,6)"
               " AND t.status IN (3,4) AND t.error_json IS NOT NULL AND t.result_event_id IS NOT NULL LIMIT 1")
        from camctl.contracts.enums import enum_for
        parameters = (activity_id, int(enum_for("operation_runs.kind").PREPARE_CAPTURE))
    else:
        sql = ("SELECT 1 FROM baseline_read_calls WHERE activity_id=? AND state=2"
               " AND error_json IS NOT NULL AND result_event_id IS NOT NULL LIMIT 1")
        parameters = (activity_id,)
    with closing(connection.execute(sql, parameters)) as cursor:
        if cursor.fetchone() is None:
            raise ConsistencyError("准备失败没有对应已保存的真实失败结果")
