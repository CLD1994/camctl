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
class WaitingFinishRequest:
    check_id: int
    trusted_wall_now: int
    occurred_at: int
    max_attempts: int

    def __post_init__(self):
        ObjectId(self.check_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)
        ObjectId(self.max_attempts)


def _document(request):
    return {"check_id": request.check_id, "trusted_wall_now": request.trusted_wall_now,
            "occurred_at": request.occurred_at, "max_attempts": request.max_attempts}


def _request(raw):
    if not isinstance(raw, dict) or set(raw) != {"check_id", "trusted_wall_now", "occurred_at", "max_attempts"}:
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
            if values["status"] in (int(statuses.FAILED), int(statuses.UNCONFIRMED)):
                error = values.get("error_json")
                expected = {"device_id": check["device_id"], "operation_run_id": str(run["id"]),
                            "attempts_used": run["attempts_used"], "max_attempts": request.max_attempts}
                if not isinstance(error, dict) or not json_equal(error.get("details"), expected):
                    raise ConsistencyError("检查等待终结原错误与申请不一致")
            if any(not json_equal(run[name], value) for name, value in values.items()):
                raise ConsistencyError("检查等待终结原结果与当前终态不一致")
            return CommandPlan((), {}, {}, WaitingFinishDisposition.FINISHED, read_only=True)
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
        return CommandPlan((event,), {("operation_runs", run["id"]): ("action", action["id"])}, state, disposition)


def finish_waiting_availability(check_id, trusted_wall_now, occurred_at, max_attempts, key, owned):
    from .operations import _outcome_of
    request = WaitingFinishRequest(check_id, trusted_wall_now, occurred_at, max_attempts)
    return _outcome_of(commit_operation(FinishWaitingAvailabilityCommand(request, key), key, owned))
