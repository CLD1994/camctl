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

    def device_read_in_flight(self, device_id):
        from camctl.scheduling.resources import device_read_in_flight
        if not self.events:
            return device_read_in_flight(self.connection, device_id)
        return self._device_in_flight(device_id, "READ_FILE")

    def device_start_in_flight(self, device_id):
        from camctl.scheduling.resources import device_start_in_flight
        if not self.events:
            return device_start_in_flight(self.connection, device_id)
        return self._device_in_flight(device_id, "START")

    def availability_in_flight(self, device_id, identity=None):
        """按逻辑设备和固定机身核对已提交或已验证前序的实际检查尝试。"""
        from contextlib import closing
        from camctl.contracts.values import ConsistencyError
        from camctl.devices.drivers.hasselblad.identity import CameraIdentity
        from camctl.persistence.transaction import row_facts

        if not isinstance(device_id, str) or not device_id:
            raise ConsistencyError("设备检查缺少逻辑设备身份")
        if identity is not None and not isinstance(identity, CameraIdentity):
            raise ConsistencyError("设备检查缺少有效机身身份")
        key = None if identity is None else identity.resource_key
        kind = int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
        running = int(enum_for("operation_attempts.status").RUNNING)
        projected = {}
        for event in self.events:
            for row in event.rows:
                if row.table not in ("operation_attempts", "operation_runs", "device_availability_checks"):
                    continue
                ref = (row.table, row.row_id)
                if ref not in projected:
                    original = row_facts(self.connection, *ref)
                    if row.before.exists and original is None:
                        raise ConsistencyError("前序设备检查事实缺少原记录")
                    projected[ref] = {} if original is None else dict(original)
                if not row.after.exists:
                    raise ConsistencyError("前序设备检查事实不能删除")
                projected[ref].update(row.after.values)
                projected[ref]["id"] = row.row_id

        def required(table, row_id):
            facts = projected.get((table, row_id))
            if facts is None and row_id is not None:
                facts = row_facts(self.connection, table, row_id)
            if facts is None:
                raise ConsistencyError(f"设备检查缺少关联记录: {table}#{row_id}")
            return facts

        def matches(attempt):
            if attempt.get("status") != running:
                return False
            run = required("operation_runs", attempt.get("run_id"))
            if run.get("kind") != kind:
                return False
            check = required("device_availability_checks", run.get("availability_check_id"))
            try:
                fixed = CameraIdentity(**check["expected_identity_json"])
            except (TypeError, ValueError, KeyError) as error:
                raise ConsistencyError("设备检查缺少可靠固定机身身份") from error
            if (check.get("resource_key") != fixed.resource_key or check.get("action_id") != run.get("action_id")
                    or not isinstance(check.get("device_id"), str) or not check["device_id"]):
                raise ConsistencyError("设备检查资源键或原责任不一致")
            return check["device_id"] == device_id or (key is not None and check["resource_key"] == key)

        attempts = (facts for (table, _), facts in projected.items() if table == "operation_attempts")
        if any(matches(attempt) for attempt in attempts):
            return True
        sql = ("SELECT t.id FROM device_availability_checks c"
               " JOIN operation_runs r ON r.availability_check_id=c.id AND r.kind=?"
               " JOIN operation_attempts t ON t.run_id=r.id AND t.status=?"
               " WHERE c.device_id=? OR (? IS NOT NULL AND c.resource_key=?)")
        with closing(self.connection.execute(sql, (kind, running, device_id, key, key))) as cursor:
            return any(matches(required("operation_attempts", attempt_id)) for attempt_id, in cursor)

    def _device_in_flight(self, device_id, kind_name):
        from contextlib import closing
        from camctl.contracts.values import ConsistencyError
        from camctl.persistence.transaction import row_facts

        projected = {}
        changed_bindings = {"actions": set(), "file_copies": set()}
        tables = {"operation_attempts", "operation_runs", "actions", "file_copies"}
        for event in self.events:
            for row in event.rows:
                if row.table not in tables:
                    continue
                key = (row.table, row.row_id)
                if key not in projected:
                    original = row_facts(self.connection, *key)
                    if row.before.exists and original is None:
                        raise ConsistencyError("前序设备调用事实缺少原记录")
                    projected[key] = {} if original is None else dict(original)
                if not row.after.exists:
                    raise ConsistencyError("前序设备调用事实不能删除")
                projected[key].update(row.after.values)
                projected[key]["id"] = row.row_id
                if ((row.table == "actions" and "device_id" in row.after.values)
                        or (row.table == "file_copies" and "slot_device_id" in row.after.values)):
                    changed_bindings[row.table].add(row.row_id)

        def required(table, identity):
            facts = projected.get((table, identity))
            if facts is None and identity is not None:
                facts = row_facts(self.connection, table, identity)
            if facts is None:
                raise ConsistencyError(f"设备调用缺少关联记录: {table}#{identity}")
            return facts

        kind = int(getattr(enum_for("operation_runs.kind"), kind_name))
        running = int(enum_for("operation_attempts.status").RUNNING)
        attempts = {identity: facts for (table, identity), facts in projected.items()
                    if table == "operation_attempts"}
        def matches(attempt):
            if attempt.get("status") != running:
                return False
            run = required("operation_runs", attempt.get("run_id"))
            if run.get("kind") != kind:
                return False
            if kind_name == "START":
                owner = required("actions", run.get("action_id"))
                target = owner.get("device_id")
                if target is None:
                    raise ConsistencyError("启动调用缺少设备绑定")
            else:
                owner = required("file_copies", run.get("copy_id"))
                if "slot_device_id" not in owner:
                    raise ConsistencyError("读取调用缺少设备机会事实")
                target = owner["slot_device_id"]
            return target == device_id

        if any(matches(attempt) for attempt in attempts.values()):
            return True

        if kind_name == "START":
            sql = ("SELECT t.id FROM operation_attempts t"
                   " JOIN operation_runs r ON r.id=t.run_id AND r.kind=?"
                   " JOIN actions a ON a.id=r.action_id"
                   f" WHERE t.status={running} AND a.device_id=?")
        else:
            sql = ("SELECT t.id FROM file_copies c"
                   " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
                   f" JOIN operation_attempts t ON t.run_id=r.id AND t.status={running}"
                   " WHERE c.slot_device_id=?")
        def query_matches(sql, identity):
            with closing(self.connection.execute(sql, (kind, identity))) as cursor:
                return any(matches(required("operation_attempts", attempt_id))
                           for attempt_id, in cursor)

        if query_matches(sql, device_id):
            return True
        if kind_name == "START":
            table, column, reference = "actions", "device_id", "action_id"
        else:
            table, column, reference = "file_copies", "slot_device_id", "copy_id"
        target_sql = ("SELECT t.id FROM operation_runs r"
                      " JOIN operation_attempts t ON t.run_id=r.id"
                      f" AND t.status={running} WHERE r.kind=? AND r.{reference}=?")
        return any(query_matches(target_sql, identity)
                   for identity in changed_bindings[table]
                   if projected[(table, identity)].get(column) == device_id)

    def can_prepare(self, action, activity, occurred_at):
        from contextlib import closing
        from camctl.scheduling.resources import capture_scope_blocked
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        identity = None
        if action["driver_id"] == DRIVER_ID:
            from camctl.devices.drivers.hasselblad.identity import CameraIdentity
            from .physical_alias import physical_alias_conflict
            try:
                identity = CameraIdentity(**activity["expected_identity_json"])
            except (TypeError, ValueError, KeyError) as error:
                raise ConsistencyError("原活动缺少可靠固定机身身份") from error
            if activity["resource_key"] != identity.resource_key:
                raise ConsistencyError("原活动资源键与固定机身身份不符")
            if physical_alias_conflict(self.connection, action["device_id"], identity):
                return False
        if self.availability_in_flight(action["device_id"], identity):
            return False
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
        from .scheduling import ReservePreparationRequest, _fixed_identity
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
        expected, resource_key = _fixed_identity(action, request.expected_identity)
        if (not json_equal(row.after.values.get("expected_identity_json"), expected)
                or row.after.values.get("resource_key") != resource_key):
            raise ConsistencyError("准备活动的机身身份与原申请不符")
        if self.availability_in_flight(action["device_id"], request.expected_identity):
            raise ConsistencyError("准备活动越过了尚未收场的设备检查")
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


def recheck_start_availability(owned, ticket):
    """实际进入 START 前核对原票据及尚未收场的检查；不保存未派发结果。"""
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.operations.models import AttemptTicket
    from camctl.persistence.transaction import row_facts

    if not isinstance(ticket, AttemptTicket) or ticket.operation != "control":
        raise ValueError("START 派发复核需要原控制票据")
    connection = owned.connection
    run = row_facts(connection, "operation_runs", ticket.run_id)
    if (run is None or run["kind"] != int(enum_for("operation_runs.kind").START)
            or run["responsibility_key"] != ticket.responsibility_key
            or run["responsibility_key"] != f"start/{run['action_id']}"
            or run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                     int(enum_for("operation_runs.status").ACTIVE))
            or ticket.target_id != str(run["activity_id"]) or run["attempts_used"] != ticket.attempt_id):
        raise ConsistencyError("START 派发票据与原责任不一致")
    with closing(connection.execute("SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                                    (run["id"], ticket.attempt_id))) as cursor:
        found = cursor.fetchone()
    attempt = None if found is None else row_facts(connection, "operation_attempts", found[0])
    if (attempt is None or attempt["run_id"] != run["id"] or attempt["attempt_no"] != ticket.attempt_id
            or attempt["status"] != int(enum_for("operation_attempts.status").RUNNING)
            or attempt["result_event_id"] is not None):
        raise ConsistencyError("START 派发缺少尚未交付结果的原尝试")
    action = row_facts(connection, "actions", run["action_id"])
    activity = row_facts(connection, "device_activities", run["activity_id"])
    if action is None or activity is None or activity["action_id"] != action["id"]:
        raise ConsistencyError("START 派发缺少原活动或动作")
    identity = None
    if action["driver_id"] == DRIVER_ID:
        try:
            identity = CameraIdentity(**activity["expected_identity_json"])
        except (TypeError, ValueError, KeyError) as error:
            raise ConsistencyError("START 派发缺少原固定机身") from error
        if activity["resource_key"] != identity.resource_key:
            raise ConsistencyError("START 原机身与资源键不一致")
    return "device_busy" if PreparationReads(connection).availability_in_flight(action["device_id"], identity) else None


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


from camctl.contracts.values import ConsistencyError


class BindingPreparationIneligible(ConsistencyError):
    """真实业务事实不允许此时终结绑定失败。"""


def verify_binding_preparation_failure(connection, action, request):
    """在原连接核对尚无活动及调用的逻辑绑定失败。"""
    from decimal import Decimal
    from camctl.contracts.workflow_errors import validate_error_details
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity

    details = request["details"]
    validate_error_details("device_binding_unavailable", details)
    if (action.get("driver_id") != DRIVER_ID or action.get("type") != 1
            or action.get("status") != 1 or action.get("execution_started") != 0
            or action.get("cancel_requested") != 0 or action.get("execution_spec_json") is None
            or details["device_id"] != action["device_id"]
            or details["expected_driver_id"] != action["driver_id"]
            or details["reason"] not in ("missing", "mismatch", "physical_identity_mismatch", "physical_identity_conflict")
            or (details["reason"] == "missing" and "actual_driver_id" in details)
            or (details["reason"] == "mismatch" and details.get("actual_driver_id") == action["driver_id"])
            or (details["reason"] not in ("physical_identity_mismatch", "physical_identity_conflict") and request["current_identity"] is not None)
            or not (Decimal(action["scheduled_at"]) - Decimal(request["prepare_lead_s"]) * 1_000_000
                    <= request["trusted_wall_now"] <= action["scheduled_at"] + action["max_delay_ms"] * 1000)):
        raise BindingPreparationIneligible("绑定失败不属于具备准备资格的原 Hasselblad 动作")
    from .capture_facts import load_start_facts
    facts = load_start_facts(connection, action)
    if not facts.not_started:
        raise BindingPreparationIneligible("原 START 未可靠确认未进入")
    if details["reason"] == "physical_identity_conflict":
        from .physical_alias import physical_alias_conflict
        try:
            current = CameraIdentity(**request["current_identity"])
        except (TypeError, ValueError) as error:
            raise BindingPreparationIneligible("缺少当前物理身份") from error
        if not physical_alias_conflict(connection, action["device_id"], current):
            raise BindingPreparationIneligible("当前机身没有其他逻辑设备的活动限制")
    if facts.activity is None:
        if details["reason"] == "physical_identity_mismatch":
            raise BindingPreparationIneligible("没有固定机身可核对物理身份")
        if connection.execute("SELECT 1 FROM operation_runs WHERE action_id=? LIMIT 1", (action["id"],)).fetchone():
            raise ConsistencyError("无活动但已有原操作责任")
        return
    if details["reason"] == "physical_identity_mismatch":
        raw = facts.activity["expected_identity_json"]
        try:
            original = CameraIdentity(**raw)
        except (TypeError, ValueError) as error:
            raise ConsistencyError("原活动缺少可靠固定机身身份") from error
        if facts.activity["resource_key"] != original.resource_key:
            raise ConsistencyError("原活动资源键与固定机身身份不符")
        try:
            current = CameraIdentity(**request["current_identity"])
        except (TypeError, ValueError) as error:
            raise BindingPreparationIneligible("缺少当前物理身份") from error
        if current == original:
            raise BindingPreparationIneligible("当前物理身份与原机身一致")
    if not preparation_calls_resolved(connection, facts.activity["id"]):
        raise BindingPreparationIneligible("原准备调用尚未可靠收场")
    if connection.execute("SELECT 1 FROM baseline_read_calls WHERE activity_id=? AND state=1 LIMIT 1",
                          (facts.activity["id"],)).fetchone():
        raise BindingPreparationIneligible("原基准调用尚未可靠收场")
    if connection.execute("SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1", (action["id"],)).fetchone():
        raise ConsistencyError("未启动的绑定失败不能已有正式产物")


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
