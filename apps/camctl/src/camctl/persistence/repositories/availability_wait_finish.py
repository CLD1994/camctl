"""等待中的可用性检查按原事实结束，不登记新的设备调用。"""

from contextlib import closing
from dataclasses import dataclass
from enum import Enum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.history.events import load_event_registry
from camctl.persistence.transaction import (CommandPlan, commit_operation, event_envelope,
    row_facts, saved_transaction_events, update_change)
from .availability_finish import AvailabilityFinishReads
from .availability_target import AvailabilityTargetBlocked, iter_release_targets


class WaitingFinishDisposition(Enum):
    FINISHED = "finished"
    WAIT = "wait"
    RESOLVED = "resolved"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class WaitingBindingResult:
    """绑定申请实际产生的检查终态，供原结果消费者交付诊断。"""
    status: int
    error: dict | None


@dataclass(frozen=True)
class WaitingFinishRequest:
    check_id: int
    trusted_wall_now: int
    occurred_at: int
    max_attempts: int | None
    binding_details: dict | None = None
    current_identity: dict | None = None

    def __post_init__(self):
        ObjectId(self.check_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)
        if self.max_attempts is not None:
            ObjectId(self.max_attempts)
        elif self.binding_details is None:
            raise ValueError("普通检查等待终结必须保存实际采用的次数上限")
        if self.binding_details is None and self.current_identity is not None:
            raise ValueError("当前机身身份必须属于绑定失败申请")


def _document(request):
    result = {"check_id": request.check_id, "trusted_wall_now": request.trusted_wall_now,
            "occurred_at": request.occurred_at, "max_attempts": request.max_attempts}
    if request.binding_details is not None:
        result.update(binding_details=request.binding_details, current_identity=request.current_identity)
    return result


def _request(raw):
    base = {"check_id", "trusted_wall_now", "occurred_at", "max_attempts"}
    if not isinstance(raw, dict) or set(raw) not in (base, base | {"binding_details", "current_identity"}):
        raise ConsistencyError("检查等待终结缺少完整申请")
    try:
        return WaitingFinishRequest(**raw)
    except (TypeError, ValueError) as error:
        raise ConsistencyError("检查等待终结申请无效") from error


def _facts(connection, request):
    check = row_facts(connection, "device_availability_checks", request.check_id)
    if check is None:
        raise ConsistencyError("检查等待终结缺少原检查")
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE availability_check_id=? AND kind=? LIMIT 2",
        (request.check_id, int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)),
    )) as cursor:
        found = cursor.fetchall()
    if len(found) != 1:
        raise ConsistencyError("检查等待终结缺少唯一原流程")
    run = row_facts(connection, "operation_runs", found[0][0])
    action = row_facts(connection, "actions", check["action_id"])
    if (run is None or action is None or run["action_id"] != check["action_id"]
            or run["responsibility_key"] != f"availability/{request.check_id}"):
        raise ConsistencyError("检查等待终结的原流程或动作关联不一致")
    return check, run, action


def saved_binding_check_failure(connection, action_id, *, resource_key=None, expected_identity=None):
    """按检查终态的原操作键恢复绑定失败申请及原次配置身份。"""
    registry = load_event_registry()["events"]["OPERATION_CONFIGURED"]
    event_type, reason = registry["id"], registry["branches"]["FINISH"]["reason"]
    condition = "c.trigger_copy_id IS NULL" if resource_key is None else (
        "c.trigger_copy_id IS NOT NULL AND c.resource_key=?")
    params = (action_id,) + (() if resource_key is None else (resource_key,))
    with closing(connection.execute(
        "SELECT c.id,r.id FROM device_availability_checks c"
        " JOIN operation_runs r ON r.availability_check_id=c.id"
        " WHERE c.action_id=? AND " + condition + " AND r.status=4"
        " AND json_extract(r.error_json,'$.code')='device_binding_unavailable' ORDER BY c.id LIMIT 2",
        params,
    )) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ConsistencyError("原触发工作存在多个绑定失败检查")
    check_id, run_id = rows[0]
    with closing(connection.execute(
        "SELECT h.operation_key FROM history_events e JOIN history_transactions h ON h.id=e.transaction_id"
        " WHERE e.event_type=? AND json_extract(e.body_json,'$.reason')=?"
        " AND json_extract(e.body_json,'$.evidence.availability_wait_finish_request.check_id')=? LIMIT 2",
        (event_type, reason, check_id),
    )) as cursor:
        keys = cursor.fetchall()
    if len(keys) != 1:
        raise ConsistencyError("原检查绑定失败缺少唯一完整历史")
    key = keys[0][0]
    check = row_facts(connection, "device_availability_checks", check_id)
    run = row_facts(connection, "operation_runs", run_id)
    events = saved_transaction_events(connection, key)
    if (check is None or run is None or check.get("action_id") != action_id
            or run.get("action_id") != action_id or run.get("availability_check_id") != check_id
            or run.get("responsibility_key") != f"availability/{check_id}"
            or run.get("status") != 4 or not events or len(events) != 1):
        raise ConsistencyError("原检查绑定失败与触发工作或完整历史不一致")
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    try:
        fixed = CameraIdentity(**check["expected_identity_json"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原检查绑定失败缺少固定机身身份") from error
    if check.get("resource_key") != fixed.resource_key:
        raise ConsistencyError("原检查绑定失败资源键与固定身份不符")
    from .availability_target import verify_binding_check_targets
    verify_binding_check_targets(connection, check, run)
    AvailabilityFinishReads(connection).require_settled(run_id)
    if expected_identity is not None and fixed != expected_identity:
        return None
    event = events[0]
    request = _request(event["body"]["evidence"].get("availability_wait_finish_request"))
    rows = event["body"]["rows"]
    expected = {"code": "device_binding_unavailable", "stage": "execution", "details": request.binding_details}
    if ((event["type"], event["reason"]) != (event_type, reason) or event["occurred_at"] != request.occurred_at
            or request.check_id != check_id or request.binding_details is None
            or (request.binding_details.get("device_id"), request.binding_details.get("expected_driver_id"))
               != (check.get("device_id"), check.get("driver_id"))
            or len(rows) != 1 or rows[0].get("table") != "operation_runs" or rows[0].get("id") != run_id
            or rows[0].get("before", {}).get("exists") is not True
            or rows[0].get("after", {}).get("exists") is not True
            or rows[0].get("after", {}).get("values") != {"status": 4, "retry_wait_required": 0, "error_json": expected}
            or run.get("retry_wait_required") != 0 or not json_equal(run.get("error_json"), expected)):
        raise ConsistencyError("原检查绑定失败事件与当前事实不一致")
    return request.binding_details, request.current_identity


def _verify_binding_failure(connection, check, request):
    from camctl.contracts.workflow_errors import validate_error_details
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    details = request.binding_details
    try:
        validate_error_details("device_binding_unavailable", details)
        if (details["device_id"] != check["device_id"]
                or details["expected_driver_id"] != check["driver_id"]):
            raise ValueError("绑定与原检查不同")
        fixed = CameraIdentity(**check["expected_identity_json"])
        if check["resource_key"] != fixed.resource_key:
            raise ValueError("检查资源键不符")
        reason = details["reason"]
        if reason in ("physical_identity_mismatch", "physical_identity_conflict"):
            current = CameraIdentity(**request.current_identity)
            if reason == "physical_identity_mismatch":
                from .availability_target import logical_held_identity_mismatch
                mismatch = logical_held_identity_mismatch(connection, check["device_id"], current)
                return fixed != current or mismatch
            from .physical_alias import physical_alias_conflict
            return physical_alias_conflict(connection, check["device_id"], current)
        return request.current_identity is None and reason in ("missing", "mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("检查绑定失败缺少可靠原身份或申请") from error


def _decision(connection, request, check, run, action):
    statuses = enum_for("operation_runs.status")
    if run["status"] not in (int(statuses.PENDING), int(statuses.ACTIVE)):
        return WaitingFinishDisposition.TERMINAL, None, None
    try:
        AvailabilityFinishReads(connection).require_settled(run["id"])
    except ConsistencyError:
        return WaitingFinishDisposition.WAIT, None, None
    try:
        if next(iter(iter_release_targets(connection, check)), None) is None:
            return WaitingFinishDisposition.RESOLVED, None, None
    except AvailabilityTargetBlocked:
        pass
    action_status = enum_for("actions.status")
    if action["cancel_requested"] or action["status"] == int(action_status.CANCELED):
        return WaitingFinishDisposition.FINISHED, int(statuses.CANCELED), None
    if check["trigger_copy_id"] is None and (action["status"] == int(action_status.EXPIRED)
            or request.trusted_wall_now > action["scheduled_at"] + action["max_delay_ms"] * 1000):
        return WaitingFinishDisposition.FINISHED, int(statuses.EXPIRED), None
    if action["status"] not in (int(action_status.PENDING), int(action_status.RUNNING)):
        return WaitingFinishDisposition.WAIT, None, None
    if request.binding_details is not None:
        if not _verify_binding_failure(connection, check, request):
            return WaitingFinishDisposition.WAIT, None, None
        return WaitingFinishDisposition.FINISHED, int(statuses.FAILED), {
            "code": "device_binding_unavailable", "stage": "execution", "details": request.binding_details}
    if run["attempts_used"] < request.max_attempts:
        return WaitingFinishDisposition.WAIT, None, None
    with closing(connection.execute(
        "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (run["id"], run["attempts_used"]),
    )) as cursor:
        found = cursor.fetchone()
    attempt = None if found is None else row_facts(connection, "operation_attempts", found[0])
    if attempt is None or attempt["result_event_id"] is None or attempt["result_json"] is None:
        raise ConsistencyError("检查耗尽缺少最近可靠尝试结果")
    attempt_status = enum_for("operation_attempts.status")
    if attempt["status"] == int(attempt_status.UNKNOWN):
        final = int(statuses.UNCONFIRMED)
    elif attempt["status"] == int(attempt_status.FAILED):
        final = int(statuses.FAILED)
    else:
        raise ConsistencyError("检查耗尽的最近尝试状态不可解释")
    device_source = check["trigger_copy_id"] is not None
    error = {"code": "source_device_unavailable" if device_source else "device_availability_unconfirmed",
        "stage": "source_read" if device_source else "execution", "details": {
            "device_id": check["device_id"], "operation_run_id": str(run["id"]),
            "attempts_used": run["attempts_used"], "max_attempts": request.max_attempts}}
    return WaitingFinishDisposition.FINISHED, final, error


def verify_waiting_finish(connection, event, context, run_id):
    request = _request(event.evidence.get("availability_wait_finish_request"))
    check, run, action = _facts(connection, request)
    if (request.occurred_at != event.occurred_at or run["id"] != run_id
            or context.transaction.first_event_id != event.event_id
            or context.transaction.last_event_id != event.event_id):
        raise ConsistencyError("检查等待终结与原申请不一致")
    disposition, status, error = _decision(connection, request, check, run, action)
    row = event.rows[0]
    if (disposition is not WaitingFinishDisposition.FINISHED or len(event.rows) != 1
            or row.table != "operation_runs" or row.row_id != run_id
            or row.after.values.get("status") != status
            or row.after.values.get("retry_wait_required", 0) != 0
            or not json_equal(row.after.values.get("error_json", run["error_json"]), error)
            or not json_equal(context.state_rows.get("operation_runs", {}).get(run_id), run)):
        raise ConsistencyError("检查等待终结与当前事实不一致")


class FinishWaitingAvailabilityCommand:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        db, request = scope.connection, self.request
        check, run, action = _facts(db, request)
        saved = saved_transaction_events(db, self.key)
        if saved is not None:
            if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"]) != (10, 3)
                    or saved[0]["occurred_at"] != request.occurred_at
                    or not json_equal(saved[0]["body"]["evidence"], {"availability_wait_finish_request": _document(request)})
                    or len(saved[0]["body"]["rows"]) != 1
                    or saved[0]["body"]["rows"][0]["table"] != "operation_runs"
                    or saved[0]["body"]["rows"][0]["id"] != run["id"]):
                raise ConsistencyError("检查等待终结原键与申请不一致")
            values = saved[0]["body"]["rows"][0]["after"]["values"]
            statuses = enum_for("operation_runs.status")
            if values.get("status") not in (int(statuses.CANCELED), int(statuses.EXPIRED),
                                            int(statuses.FAILED), int(statuses.UNCONFIRMED)):
                raise ConsistencyError("检查等待终结原键包含不适用的状态")
            if values["status"] in (int(statuses.FAILED), int(statuses.UNCONFIRMED)) and request.binding_details is not None:
                if (values["status"] != int(statuses.FAILED) or values.get("error_json") != {
                        "code": "device_binding_unavailable", "stage": "execution", "details": request.binding_details}):
                    raise ConsistencyError("检查绑定终结原错误与申请不一致")
            elif values["status"] in (int(statuses.FAILED), int(statuses.UNCONFIRMED)):
                error = values.get("error_json")
                expected = {"device_id": check["device_id"], "operation_run_id": str(run["id"]),
                            "attempts_used": run["attempts_used"], "max_attempts": request.max_attempts}
                if not isinstance(error, dict) or not json_equal(error.get("details"), expected):
                    raise ConsistencyError("检查等待终结原错误与申请不一致")
            if any(not json_equal(run[name], value) for name, value in values.items()):
                raise ConsistencyError("检查等待终结原结果与当前终态不一致")
            result = (WaitingBindingResult(values["status"], values.get("error_json"))
                      if request.binding_details is not None else WaitingFinishDisposition.FINISHED)
            return CommandPlan((), {}, {}, result, read_only=True)
        disposition, status, error = _decision(db, request, check, run, action)
        state = {"operation_runs": {run["id"]: run}, "actions": {action["id"]: action},
                 "device_availability_checks": {check["id"]: check}}
        if disposition is not WaitingFinishDisposition.FINISHED:
            return CommandPlan((), {}, state, disposition, read_only=True)
        allocation = scope.allocate(1)
        spec = load_event_registry()["events"]["OPERATION_CONFIGURED"]
        row = update_change("operation_runs", run["id"],
            {"status": run["status"], "retry_wait_required": run["retry_wait_required"], "error_json": run["error_json"]},
            {"status": status, "retry_wait_required": 0, "error_json": error})
        event = event_envelope(allocation.first_event_id, allocation.txn_id, spec["id"], spec["branches"]["FINISH"]["reason"],
            (row,), request.occurred_at, {"availability_wait_finish_request": _document(request)})
        result = WaitingBindingResult(status, error) if request.binding_details is not None else disposition
        return CommandPlan((event,), {("operation_runs", run["id"]): ("action", action["id"])}, state, result)


def finish_waiting_availability(check_id, trusted_wall_now, occurred_at, max_attempts, key, owned,
                                *, binding_details=None, current_identity=None):
    from .operations import _outcome_of
    request = WaitingFinishRequest(check_id, trusted_wall_now, occurred_at, max_attempts,
                                   binding_details, current_identity)
    return _outcome_of(commit_operation(FinishWaitingAvailabilityCommand(request, key), key, owned))
