"""有限检查失败向原触发工作交付的固定依据。"""

from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.contracts.workflow_errors import action_error_id, validate_error_details
from camctl.persistence.transaction import (CommandPlan, commit_operation, event_envelope,
    row_facts, saved_transaction_events, update_change)
from .availability_target import iter_fixed_targets


def failure_basis(connection, check_id, action_id):
    """返回仍阻挡该动作的已终结检查错误；已解决或非失败返回 None。"""
    check = row_facts(connection, "device_availability_checks", check_id)
    if check is None or check["action_id"] != action_id:
        raise ConsistencyError("可用性失败缺少原检查与触发动作")
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE availability_check_id=? AND kind=? LIMIT 2",
        (check_id, int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise ConsistencyError("可用性失败缺少唯一原检查流程")
    run = row_facts(connection, "operation_runs", rows[0][0])
    if run is None or run["action_id"] != action_id or run["responsibility_key"] != f"availability/{check_id}":
        raise ConsistencyError("可用性失败与原检查流程不一致")
    statuses = enum_for("operation_runs.status")
    if run["status"] not in (int(statuses.FAILED), int(statuses.UNCONFIRMED)):
        return None
    code = "source_device_unavailable" if check["trigger_copy_id"] is not None else "device_availability_unconfirmed"
    stage = "source_read" if check["trigger_copy_id"] is not None else "execution"
    error = run["error_json"]
    if not isinstance(error, dict) or error.get("code") != code or error.get("stage") != stage:
        raise ConsistencyError("原检查终态缺少对应的可用性失败")
    details = error.get("details")
    validate_error_details(code, details)
    if (details["device_id"] != check["device_id"] or details["operation_run_id"] != str(run["id"])
            or details["attempts_used"] != run["attempts_used"]):
        raise ConsistencyError("原检查失败详情与流程事实不一致")
    count = 0
    blocked = False
    occupancy = enum_for("device_activities.occupancy_state")
    for page in iter_fixed_targets(connection, check_id):
        for _, target_action, activity_id in page:
            activity = row_facts(connection, "device_activities", activity_id)
            owner = None if activity is None else row_facts(connection, "actions", activity["action_id"])
            if (target_action != action_id or activity is None
                    or owner is None or (owner["device_id"], owner["driver_id"]) != (check["device_id"], check["driver_id"])
                    or activity["resource_key"] != check["resource_key"]
                    or not json_equal(activity["expected_identity_json"], check["expected_identity_json"])):
                raise ConsistencyError("检查失败的固定目标与原机身不一致")
            if activity["occupancy_state"] == int(occupancy.HELD):
                blocked = True
            elif activity["occupancy_state"] != int(occupancy.RELEASED):
                raise ConsistencyError("检查失败的原目标占用不可解释")
            count += 1
    if count == 0:
        raise ConsistencyError("检查失败缺少固定目标")
    return (check, run, details) if blocked else None


class FailureDisposition(Enum):
    FAILED = "failed"
    WAIT = "wait"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class PhotoFailureRequest:
    check_id: int
    trusted_wall_now: int
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.check_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)


def _photo_basis(connection, request):
    check = row_facts(connection, "device_availability_checks", request.check_id)
    action = None if check is None else row_facts(connection, "actions", check["action_id"])
    if check is None or action is None or check["trigger_copy_id"] is not None:
        raise ConsistencyError("拍摄可用性失败缺少原拍摄检查")
    if action["status"] in (3, 4, 5, 6):
        return action, None, FailureDisposition.TERMINAL
    if (action["status"] != 1 or action["execution_started"] != 0 or action["cancel_requested"]
            or request.trusted_wall_now > action["scheduled_at"] + action["max_delay_ms"] * 1000):
        return action, None, FailureDisposition.WAIT
    if action["type"] != int(enum_for("actions.type").CAMERA_TAKE_PHOTO):
        raise ConsistencyError("拍摄检查没有原单张动作")
    if (action["device_id"], action["driver_id"]) != (check["device_id"], check["driver_id"]):
        raise ConsistencyError("拍摄检查与原动作绑定不一致")
    with closing(connection.execute("SELECT 1 FROM device_activities WHERE action_id=? LIMIT 1", (action["id"],))) as cursor:
        if cursor.fetchone() is not None:
            raise ConsistencyError("未开始检查失败的动作已有活动")
    with closing(connection.execute("SELECT 1 FROM operation_runs WHERE action_id=? AND kind=? LIMIT 1",
            (action["id"], int(enum_for("operation_runs.kind").START)))) as cursor:
        if cursor.fetchone() is not None:
            raise ConsistencyError("未开始检查失败的动作已有 START")
    basis = failure_basis(connection, request.check_id, action["id"])
    return action, None if basis is None else basis[2], FailureDisposition.WAIT if basis is None else FailureDisposition.FAILED


def verify_photo_failure(connection, event, context):
    raw = event.evidence.get("availability_failure_request")
    if not isinstance(raw, dict) or set(raw) != {"check_id", "trusted_wall_now"}:
        raise ConsistencyError("拍摄可用性失败缺少完整申请")
    request = PhotoFailureRequest(raw["check_id"], raw["trusted_wall_now"], event.occurred_at)
    action, details, disposition = _photo_basis(connection, request)
    row = event.rows[0]
    if (len(event.rows) != 1 or row.table != "actions" or row.row_id != action["id"]
            or disposition is not FailureDisposition.FAILED
            or row.after.values.get("error_code") != action_error_id("device_availability_unconfirmed")
            or not json_equal(row.after.values.get("error_details_json"), details)
            or not json_equal(context.state_rows.get("actions", {}).get(action["id"]), action)):
        raise ConsistencyError("拍摄可用性失败与原检查事实不一致")


class FailPhotoAvailabilityCommand:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        from .scheduling import _sibling_actions
        db, request = scope.connection, self.request
        check = row_facts(db, "device_availability_checks", request.check_id)
        action = None if check is None else row_facts(db, "actions", check["action_id"])
        if action is None:
            raise ConsistencyError("拍摄可用性失败缺少原动作")
        saved = saved_transaction_events(db, self.key)
        evidence = {"check_id": request.check_id, "trusted_wall_now": request.trusted_wall_now}
        if saved is not None:
            if (len(saved) not in (1, 2) or (saved[0]["type"], saved[0]["reason"]) != (8, 7)
                    or saved[0]["occurred_at"] != request.occurred_at
                    or not json_equal(saved[0]["body"]["evidence"], {"availability_failure_request": evidence})
                    or len(saved[0]["body"]["rows"]) != 1
                    or saved[0]["body"]["rows"][0]["id"] != action["id"]
                    or action["status"] != 4 or action["execution_started"] != 0
                    or action["error_code"] != action_error_id("device_availability_unconfirmed")
                    or not json_equal(action["error_details_json"], saved[0]["body"]["rows"][0]["after"]["values"].get("error_details_json"))):
                raise ConsistencyError("拍摄可用性失败原键与事实不一致")
            row = saved[0]["body"]["rows"][0]
            if (row["table"] != "actions" or row["before"]["values"] != {
                    "status": 1, "error_code": None, "error_details_json": None}
                    or row["after"]["values"] != {"status": 4,
                        "error_code": action_error_id("device_availability_unconfirmed"),
                        "error_details_json": action["error_details_json"]}):
                raise ConsistencyError("拍摄可用性失败原事件行不一致")
            if len(saved) == 2:
                plan_event = saved[1]
                rows = plan_event["body"]["rows"]
                plan = row_facts(db, "plans", action["plan_id"])
                if ((plan_event["type"], plan_event["reason"]) != (9, 2)
                        or plan_event["occurred_at"] != request.occurred_at or len(rows) != 1
                        or rows[0]["table"] != "plans" or rows[0]["id"] != action["plan_id"]
                        or rows[0]["after"]["values"] != {"status": 3}
                        or plan is None or plan["status"] != 3):
                    raise ConsistencyError("拍摄可用性失败的原计划终态不一致")
            return CommandPlan((), {}, {}, FailureDisposition.FAILED, read_only=True)
        action, details, disposition = _photo_basis(db, request)
        if disposition is not FailureDisposition.FAILED:
            return CommandPlan((), {}, {}, disposition, read_only=True)
        siblings = _sibling_actions(db, action)
        plan = row_facts(db, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError("拍摄可用性失败缺少原计划")
        complete = plan["status"] in (1, 2) and all(
            row["id"] == action["id"] or row["status"] in (3, 4, 5, 6) for row in siblings.values())
        allocation = scope.allocate(1 + int(complete))
        row = update_change("actions", action["id"],
            {"status": 1, "error_code": None, "error_details_json": None},
            {"status": 4, "error_code": action_error_id("device_availability_unconfirmed"), "error_details_json": details})
        events = [event_envelope(allocation.first_event_id, allocation.txn_id, 8, 7, (row,), request.occurred_at,
                                 {"availability_failure_request": evidence})]
        owners = {("actions", action["id"]): ("action", action["id"])}
        if complete:
            owners[("plans", plan["id"])] = ("plan", plan["id"])
            events.append(event_envelope(allocation.last_event_id, allocation.txn_id, 9, 2,
                (update_change("plans", plan["id"], {"status": plan["status"]}, {"status": 3}),), request.occurred_at))
        return CommandPlan(tuple(events), owners, {"actions": siblings, "plans": {plan["id"]: plan}}, disposition)


def fail_photo_availability(check_id, trusted_wall_now, occurred_at, key, owned):
    from .operations import _outcome_of
    request = PhotoFailureRequest(check_id, trusted_wall_now, occurred_at)
    return _outcome_of(commit_operation(FailPhotoAvailabilityCommand(request, key), key, owned))


def _read_basis(connection, check_id, copy_id):
    from .outputs import _read_owner_eligible
    from .physical_alias import source_physical_identity
    check = row_facts(connection, "device_availability_checks", check_id)
    copy = row_facts(connection, "file_copies", copy_id)
    if check is None or check["trigger_copy_id"] is None or copy is None or copy["delivery_id"] is None:
        raise ConsistencyError("取回可用性失败缺少检查或原设备拷贝")
    delivery = row_facts(connection, "deliveries", copy["delivery_id"])
    action = None if delivery is None else row_facts(connection, "actions", delivery["action_id"])
    source = None if copy["source_device_file_id"] is None else row_facts(connection, "device_files", copy["source_device_file_id"])
    output = None if delivery is None else row_facts(connection, "outputs", delivery["output_id"])
    target = row_facts(connection, "intermediate_files", copy["target_file_id"])
    if (action is None or source is None or output is None or check["action_id"] != action["id"]
            or target is None or output["device_file_id"] != source["id"]
            or output["source_action_id"] != source["source_action_id"]):
        raise ConsistencyError("取回失败与原检查、交付或设备文件不一致")
    observer = row_facts(connection, "actions", source["observer_action_id"])
    origin = row_facts(connection, "actions", source["source_action_id"])
    if observer is None or origin is None:
        raise ConsistencyError("取回失败缺少原设备绑定")
    fixed = source_physical_identity(connection, source, observer, origin)
    if (fixed.resource_key != check["resource_key"]
            or not json_equal({"serial": fixed.serial, "product_id": fixed.product_id}, check["expected_identity_json"])
            or (observer["device_id"], observer["driver_id"]) != (check["device_id"], check["driver_id"])):
        raise ConsistencyError("取回来源与原检查固定机身不一致")
    with closing(connection.execute("SELECT id FROM operation_runs WHERE copy_id=? LIMIT 2", (copy_id,))) as cursor:
        runs = cursor.fetchall()
    with closing(connection.execute("SELECT id FROM obtain_items WHERE delivery_id=? LIMIT 2", (delivery["id"],))) as cursor:
        items = cursor.fetchall()
    if len(runs) != 1 or len(items) != 1:
        raise ConsistencyError("取回失败缺少唯一原读取流程或取回项")
    run = row_facts(connection, "operation_runs", runs[0][0])
    item = row_facts(connection, "obtain_items", items[0][0])
    if (run is None or item is None or run["kind"] != int(enum_for("operation_runs.kind").READ_FILE)
            or run["action_id"] != action["id"]
            or run["responsibility_key"] != f"read/{copy_id}" or item["output_id"] != output["id"]):
        raise ConsistencyError("取回失败的原读取或条目归属不一致")
    selection = row_facts(connection, "obtain_source_selections", item["selection_id"])
    dependency = None if selection is None else row_facts(connection, "action_dependencies", selection["dependency_id"])
    if (selection is None or dependency is None or dependency["action_id"] != action["id"]
            or dependency["depends_on_action_id"] != output["source_action_id"]):
        raise ConsistencyError("取回失败缺少原选择和来源依赖")
    state = {"actions": {action["id"]: action, observer["id"]: observer, origin["id"]: origin},
             "device_files": {source["id"]: source}, "file_copies": {copy_id: copy},
             "deliveries": {delivery["id"]: delivery}, "obtain_items": {item["id"]: item},
             "outputs": {output["id"]: output}, "operation_runs": {run["id"]: run},
             "intermediate_files": {target["id"]: target}, "operation_attempts": {},
             "obtain_source_selections": {selection["id"]: selection},
             "action_dependencies": {dependency["id"]: dependency}}
    if (not _read_owner_eligible(action) or delivery["status"] not in (1, 2)
            or run["status"] not in (1, 2) or item["source_dependency"] != 1):
        return state, None
    with closing(connection.execute("SELECT id FROM operation_attempts WHERE run_id=? ORDER BY id", (run["id"],))) as cursor:
        while page := cursor.fetchmany(128):
            for (attempt_id,) in page:
                attempt = row_facts(connection, "operation_attempts", attempt_id)
                if attempt is None:
                    raise ConsistencyError("原读取尝试记录缺失")
                state["operation_attempts"][attempt_id] = attempt
                if attempt["status"] == 1 or attempt["result_json"] is None:
                    return state, None
    basis = failure_basis(connection, check_id, action["id"])
    return state, None if basis is None else basis[2]


def verify_read_failure(connection, event, context, row):
    error = row.after.values.get("error_json")
    if not isinstance(error, dict) or error.get("code") != "source_device_unavailable":
        return
    details = error.get("details")
    validate_error_details("source_device_unavailable", details)
    check_run = row_facts(connection, "operation_runs", ObjectId(int(details["operation_run_id"])))
    if check_run is None or check_run.get("availability_check_id") is None:
        raise ConsistencyError("设备读取失败缺少原检查流程")
    copies = [copy for copy in context.state_rows.get("file_copies", {}).values()
              if copy.get("delivery_id") == row.row_id]
    if len(copies) != 1:
        raise ConsistencyError("设备读取失败缺少唯一交付拷贝")
    state, actual = _read_basis(connection, check_run["availability_check_id"], copies[0]["id"])
    action = state["actions"][check_run["action_id"]]
    if actual is None or action["scheduled_at"] > event.occurred_at or not json_equal(details, actual):
        raise ConsistencyError("设备读取失败与原检查事实不一致")


def verify_read_failure_group(connection, event, planned_events, transaction):
    """核对有限事务提案的完整结构，不以未来事件判断当前资格。"""
    rows = event.rows
    if len(rows) != 1 or rows[0].table != "deliveries":
        raise ConsistencyError("设备读取失败必须对应唯一交付")
    error = rows[0].after.values.get("error_json")
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, dict):
        raise ConsistencyError("设备读取失败缺少检查详情")
    check_run = row_facts(connection, "operation_runs", ObjectId(int(details["operation_run_id"])))
    with closing(connection.execute("SELECT id FROM file_copies WHERE delivery_id=? LIMIT 2", (rows[0].row_id,))) as cursor:
        copies = cursor.fetchall()
    if check_run is None or check_run["availability_check_id"] is None or len(copies) != 1:
        raise ConsistencyError("设备读取失败缺少原检查或唯一拷贝")
    state, actual = _read_basis(connection, check_run["availability_check_id"], copies[0][0])
    if actual is None or not json_equal(details, actual):
        raise ConsistencyError("设备读取失败缺少可交付的原检查结论")
    copy = state["file_copies"][copies[0][0]]
    delivery = state["deliveries"][rows[0].row_id]
    run = next(iter(state["operation_runs"].values()))
    item = next(iter(state["obtain_items"].values()))
    changes = [(23, 5, update_change("deliveries", delivery["id"],
                {"status": delivery["status"], "error_json": delivery["error_json"]},
                {"status": 6, "error_json": error})),
               (10, 3, update_change("operation_runs", run["id"],
                {"status": run["status"], "retry_wait_required": run["retry_wait_required"], "error_json": run["error_json"]},
                {"status": 4, "retry_wait_required": 0, "error_json": error})),
               (21, 3, update_change("obtain_items", item["id"], {"source_dependency": 1}, {"source_dependency": 0}))]
    if copy["slot_device_id"] is not None:
        from .outputs import _COPY_CHANGED_EVENT, _SLOT_REASON
        changes.append((_COPY_CHANGED_EVENT, _SLOT_REASON, update_change(
            "file_copies", copy["id"], {"slot_device_id": copy["slot_device_id"]}, {"slot_device_id": None})))
    if (event.event_id != transaction.first_event_id
            or transaction.last_event_id - transaction.first_event_id + 1 != len(changes)
            or len(planned_events) != len(changes)):
        raise ConsistencyError("设备读取失败必须完整提交原读取收场与保护释放")
    for offset, (proposal, (kind, reason, change)) in enumerate(zip(planned_events, changes)):
        if (proposal.event_id != transaction.first_event_id + offset
                or proposal.transaction_id != transaction.txn_id or proposal.occurred_at != event.occurred_at
                or (proposal.event_type, proposal.reason) != (kind, reason)
                or proposal.rows != (change,) or proposal.evidence):
            raise ConsistencyError("设备读取失败事件组与原责任不一致")


class FailReadAvailabilityCommand:
    def __init__(self, check_id, copy_id, occurred_at, key):
        self.check_id, self.copy_id = ObjectId(check_id), ObjectId(copy_id)
        self.occurred_at, self.key = UtcMicros(occurred_at), OperationKey(key)

    def plan(self, scope):
        db = scope.connection
        saved = saved_transaction_events(db, self.key)
        if saved is not None:
            kinds = [(event["type"], event["reason"]) for event in saved]
            if (kinds not in ([(23, 5), (10, 3), (21, 3)], [(23, 5), (10, 3), (21, 3), (22, 6)])
                    or any(event["occurred_at"] != self.occurred_at for event in saved)
                    or len(saved[0]["body"]["rows"]) != 1):
                raise ConsistencyError("取回可用性失败原键与完整事件组不一致")
            delivery_id = saved[0]["body"]["rows"][0]["id"]
            copy = row_facts(db, "file_copies", self.copy_id)
            delivery = row_facts(db, "deliveries", delivery_id)
            error = saved[0]["body"]["rows"][0]["after"]["values"].get("error_json")
            check = row_facts(db, "device_availability_checks", self.check_id)
            check_run = (None if not isinstance(error, dict) or not isinstance(error.get("details"), dict)
                         else row_facts(db, "operation_runs", int(error["details"]["operation_run_id"])))
            if (copy is None or delivery is None or check is None or copy["delivery_id"] != delivery_id
                    or delivery["status"] != 6 or not json_equal(delivery["error_json"], error)
                    or not isinstance(error, dict) or error.get("code") != "source_device_unavailable"
                    or error.get("stage") != "source_read" or check_run is None
                    or check_run["availability_check_id"] != self.check_id
                    or check_run["action_id"] != delivery["action_id"]):
                raise ConsistencyError("原取回可用性失败不属于当前申请")
            if any(len(event["body"]["rows"]) != 1 for event in saved):
                raise ConsistencyError("原取回可用性失败事件行数不一致")
            read_run = row_facts(db, "operation_runs", saved[1]["body"]["rows"][0]["id"])
            item = row_facts(db, "obtain_items", saved[2]["body"]["rows"][0]["id"])
            if (read_run is None or read_run["copy_id"] != self.copy_id
                    or read_run["action_id"] != delivery["action_id"]
                    or item is None or item["delivery_id"] != delivery_id
                    or (len(saved) == 4 and saved[3]["body"]["rows"][0]["id"] != self.copy_id)):
                raise ConsistencyError("原取回可用性失败的读取流程或条目不一致")
            for event, table in zip(saved, ("deliveries", "operation_runs", "obtain_items", "file_copies")):
                rows = event["body"]["rows"]
                if len(rows) != 1 or rows[0]["table"] != table:
                    raise ConsistencyError("原取回可用性失败事件组成不一致")
                current = row_facts(db, table, rows[0]["id"])
                if current is None or any(not json_equal(current.get(name), value)
                                          for name, value in rows[0]["after"]["values"].items()):
                    raise ConsistencyError("原取回可用性失败事件与当前事实不一致")
            return CommandPlan((), {}, {}, FailureDisposition.FAILED, read_only=True)
        state, details = _read_basis(db, self.check_id, self.copy_id)
        if details is None:
            return CommandPlan((), {}, state, FailureDisposition.WAIT, read_only=True)
        copy = state["file_copies"][self.copy_id]
        delivery = state["deliveries"][copy["delivery_id"]]
        if state["actions"][delivery["action_id"]]["scheduled_at"] > self.occurred_at:
            return CommandPlan((), {}, state, FailureDisposition.WAIT, read_only=True)
        item = next(iter(state["obtain_items"].values()))
        run = next(iter(state["operation_runs"].values()))
        error = {"code": "source_device_unavailable", "stage": "source_read", "details": details}
        changes = [(23, 5, update_change("deliveries", delivery["id"],
                        {"status": delivery["status"], "error_json": delivery["error_json"]},
                        {"status": 6, "error_json": error})),
                   (10, 3, update_change("operation_runs", run["id"],
                        {"status": run["status"], "retry_wait_required": run["retry_wait_required"], "error_json": run["error_json"]},
                        {"status": 4, "retry_wait_required": 0, "error_json": error})),
                   (21, 3, update_change("obtain_items", item["id"], {"source_dependency": 1}, {"source_dependency": 0}))]
        if copy["slot_device_id"] is not None:
            from .outputs import _COPY_CHANGED_EVENT, _SLOT_REASON
            changes.append((_COPY_CHANGED_EVENT, _SLOT_REASON, update_change(
                "file_copies", copy["id"], {"slot_device_id": copy["slot_device_id"]}, {"slot_device_id": None})))
        allocation = scope.allocate(len(changes))
        events = tuple(event_envelope(allocation.first_event_id + i, allocation.txn_id, kind, reason,
                                      (row,), self.occurred_at) for i, (kind, reason, row) in enumerate(changes))
        owners = {("deliveries", delivery["id"]): ("delivery", delivery["id"]),
                  ("operation_runs", run["id"]): ("delivery", delivery["id"]),
                  ("obtain_items", item["id"]): ("action", delivery["action_id"]),
                  ("file_copies", copy["id"]): ("delivery", delivery["id"]),
                  ("intermediate_files", copy["target_file_id"]): ("intermediate_file", copy["target_file_id"])}
        from camctl.history.reads import ReadCoverage
        coverage = ReadCoverage({("operation_runs", "copy_id"): {self.copy_id},
                                 ("operation_attempts", "run_id"): {run["id"]}})
        return CommandPlan(events, owners, state, FailureDisposition.FAILED, read_coverage=coverage)


def fail_read_availability(check_id, copy_id, occurred_at, key, owned):
    from .outputs import _outcome_of
    return _outcome_of(commit_operation(FailReadAvailabilityCommand(check_id, copy_id, occurred_at, key), key, owned))
