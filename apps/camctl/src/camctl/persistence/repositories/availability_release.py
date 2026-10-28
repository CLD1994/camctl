"""检查真实结果与全部旧活动释放的共同事务。"""

from dataclasses import replace
from contextlib import closing
from enum import Enum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.history.events import load_event_registry
from camctl.history.validators import EventContext
from camctl.operations.attempts import RunFinish, RunOutcome
from camctl.operations.models import ErrorValue
from camctl.persistence.transaction import (BoundedCommandPlan, CommandPlan, commit_operation,
    event_envelope, row_facts, update_change, read_saved_transaction_page, read_transaction_range)
from .availability_target import AvailabilityTargetBlocked, iter_fixed_targets, iter_release_targets
from .availability_observation import require_saved_availability


def _check(connection, run):
    check = row_facts(connection, "device_availability_checks", run["availability_check_id"])
    if check is None or check["action_id"] != run["action_id"]:
        raise ConsistencyError("设备检查缺少原责任及固定身份")
    return check


def _release_pages(connection, check):
    """重新装满待释放页，避免已解决目标造成多余事件或遗漏。"""
    page = []
    for targets in iter_release_targets(connection, check):
        for identity in targets:
            page.append(identity)
            if len(page) == 128:
                yield tuple(page)
                page.clear()
    if page:
        yield tuple(page)


def _no_conflicts(connection, check, attempt_id, *, exclude_read_attempt_id=None):
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.capture.software_recovery import recovery_locked
    from camctl.scheduling.resources import device_read_in_flight, device_start_in_flight
    from .physical_alias import physical_alias_conflict
    identity = CameraIdentity(**check["expected_identity_json"])
    read_in_flight = (device_read_in_flight(connection, check["device_id"]) if exclude_read_attempt_id is None else
                      device_read_in_flight(connection, check["device_id"], exclude_attempt_id=exclude_read_attempt_id))
    if (recovery_locked(connection, check["device_id"])
            or read_in_flight
            or device_start_in_flight(connection, check["device_id"])
            or physical_alias_conflict(connection, check["device_id"], identity)):
        raise AvailabilityTargetBlocked("原设备仍有其他实际调用")
    running = int(enum_for("operation_attempts.status").RUNNING)
    queries = (
        ("SELECT 1 FROM device_activities d JOIN operation_runs r ON r.activity_id=d.id"
         " JOIN operation_attempts t ON t.run_id=r.id WHERE d.resource_key=? AND t.status=? LIMIT 1",
         (check["resource_key"], running)),
        ("SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
         " JOIN device_availability_checks c ON c.id=r.availability_check_id"
         " WHERE c.resource_key=? AND t.status=? AND (? IS NULL OR t.id<>?) LIMIT 1",
         (check["resource_key"], running, attempt_id, attempt_id)),
        ("SELECT 1 FROM device_activities d WHERE d.resource_key=? AND d.occupancy_state=?"
         " AND NOT EXISTS(SELECT 1 FROM device_availability_targets t WHERE t.check_id=? AND t.activity_id=d.id) LIMIT 1",
         (check["resource_key"], int(enum_for("device_activities.occupancy_state").HELD), check["id"])),
        ("SELECT 1 FROM actions a JOIN baseline_read_calls b ON b.action_id=a.id"
         " WHERE a.device_id=? AND b.state=1 LIMIT 1", (check["device_id"],)),
    )
    for sql, args in queries:
        with closing(connection.execute(sql, args)) as cursor:
            if cursor.fetchone() is not None:
                raise AvailabilityTargetBlocked("原设备仍有其他未收场责任")
    from .retry_wait_reads import read_reservation_blocks_control
    if (read_reservation_blocks_control(connection, check["device_id"]) if exclude_read_attempt_id is None else
            read_reservation_blocks_control(connection, check["device_id"], exclude_attempt_id=exclude_read_attempt_id)):
        raise AvailabilityTargetBlocked("原设备读取调用尚未收场")


def _release_context(connection, event, transaction, run_id, check_id, attempt_id):
    run = row_facts(connection, "operation_runs", run_id)
    attempt = row_facts(connection, "operation_attempts", attempt_id)
    check = row_facts(connection, "device_availability_checks", check_id)
    if run is None or attempt is None or check is None:
        raise ConsistencyError("检查事务缺少原流程、尝试或检查")
    state = {"operation_runs": {run_id: run}, "operation_attempts": {attempt_id: attempt},
             "device_availability_checks": {check_id: check}, "actions": {}}
    owners = {}
    for row in event.rows:
        if row.table == "device_activities":
            activity = row_facts(connection, "device_activities", row.row_id)
            if activity is None:
                raise ConsistencyError("释放活动不存在")
            state.setdefault("device_activities", {})[row.row_id] = activity
            action_id = activity["action_id"]
        else:
            action_id = run["action_id"]
        action = row_facts(connection, "actions", action_id)
        if action is None:
            raise ConsistencyError("检查事务所属动作不存在")
        state["actions"][action_id] = action
        owners[(row.table, row.row_id)] = ("action", action_id)
    return EventContext(transaction, owners, state)


def _resolved_context(connection, event, transaction, run_id, check_id):
    run = row_facts(connection, "operation_runs", run_id)
    check = row_facts(connection, "device_availability_checks", check_id)
    action = None if run is None else row_facts(connection, "actions", run["action_id"])
    if run is None or check is None or action is None:
        raise ConsistencyError("检查终结缺少原流程、检查或动作")
    return EventContext(transaction, {("operation_runs", run_id): ("action", run["action_id"])},
        {"operation_runs": {run_id: run}, "device_availability_checks": {check_id: check},
         "actions": {action["id"]: action}})


class ResolvedGroup:
    """无新调用地终结全部目标已可靠解除的开放检查。"""

    def __init__(self, connection, event, transaction):
        if event.event_id != transaction.first_event_id or transaction.last_event_id != event.event_id or len(event.rows) != 1:
            raise ConsistencyError("已解决检查终结必须是唯一事务事件")
        row = event.rows[0]
        run = row_facts(connection, "operation_runs", row.row_id)
        if (row.table != "operation_runs" or run is None
                or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
                or run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                         int(enum_for("operation_runs.status").ACTIVE))):
            raise ConsistencyError("已解决检查缺少开放原流程")
        self.connection, self.transaction, self.run = connection, transaction, run
        self.check = _check(connection, run)
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts WHERE run_id=? AND (status=? OR result_event_id IS NULL) LIMIT 1",
            (run["id"], int(enum_for("operation_attempts.status").RUNNING)),
        )) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("原检查调用或结果尚未收场")
        if next(iter(iter_release_targets(connection, self.check)), None) is not None:
            raise ConsistencyError("检查仍有未解决的固定目标")
        self.pending = None
        self.sealed = False

    def bind(self, event, context):
        registry = load_event_registry()["events"]["OPERATION_CONFIGURED"]
        if (self.pending is not None or self.sealed or event.transaction_id != self.transaction.txn_id
                or event.event_id != self.transaction.first_event_id
                or event.event_type != registry["id"] or event.reason != registry["branches"]["FINISH"]["reason"]
                or len(event.rows) != 1 or event.rows[0].table != "operation_runs"
                or event.rows[0].row_id != self.run["id"]
                or event.rows[0].after.values.get("status") != int(enum_for("operation_runs.status").SUCCEEDED)
                or context != _resolved_context(self.connection, event, self.transaction, self.run["id"], self.check["id"])):
            raise ConsistencyError("已解决检查的终结事件或上下文不一致")
        self.pending = replace(event, change_seq=None)

    def seal(self, validated):
        if self.pending != replace(validated.envelope, change_seq=None):
            raise ConsistencyError("已解决检查终结没有通过原事件校验")
        self.pending, self.sealed = None, True

    def finish(self):
        if self.pending is not None or not self.sealed:
            raise ConsistencyError("已解决检查终结没有封口")


class ReleaseGroup:
    """内核在结果前固定全部目标，逐事件验证并在提交前封口。"""

    def __init__(self, connection, event, transaction):
        if event.event_id != transaction.first_event_id or len(event.rows) != 1:
            raise ConsistencyError("检查结果必须是共同事务的首事件")
        row = event.rows[0]
        if row.table != "operation_attempts" or not row.before.exists:
            raise ConsistencyError("检查结果缺少原尝试")
        attempt = row_facts(connection, "operation_attempts", row.row_id)
        run = None if attempt is None else row_facts(connection, "operation_runs", attempt["run_id"])
        if run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY):
            raise ConsistencyError("检查结果缺少原检查流程")
        self.connection, self.transaction = connection, transaction
        self.run_id, self.attempt_id = run["id"], attempt["id"]
        self.check = _check(connection, run)
        self.terminal = run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                               int(enum_for("operation_runs.status").ACTIVE))
        self.blocked = False
        self.expected_count = 0
        self.targets = iter(())
        if not self.terminal:
            try:
                self.expected_count = sum(len(page) for page in iter_release_targets(connection, self.check))
                if self.expected_count:
                    _no_conflicts(connection, self.check, self.attempt_id)
            except AvailabilityTargetBlocked:
                self.blocked = True
            if not self.blocked:
                self.targets = (identity for page in iter_release_targets(connection, self.check) for identity in page)
        self.position, self.count, self.pending = 0, 0, None

    def bind(self, event, context):
        registry = load_event_registry()["events"]
        if (self.pending is not None or event.transaction_id != self.transaction.txn_id
                or event.event_id != self.transaction.first_event_id + self.position
                or context != _release_context(self.connection, event, self.transaction,
                                                self.run_id, self.check["id"], self.attempt_id)):
            raise ConsistencyError("设备检查事务的时序或上下文不一致")
        if self.position == 0:
            if (event.event_type != registry["ATTEMPT_RESULT"]["id"]
                    or event.reason != registry["ATTEMPT_RESULT"]["branches"]["SUCCEED"]["reason"]
                    or len(event.rows) != 1 or event.rows[0].row_id != self.attempt_id):
                raise ConsistencyError("设备检查事务首事件不是原成功尝试结果")
        elif self.position == 1:
            if self.terminal:
                raise ConsistencyError("原检查已终态时不能改写其流程")
            attempt = row_facts(self.connection, "operation_attempts", self.attempt_id)
            run = row_facts(self.connection, "operation_runs", self.run_id)
            require_saved_availability(attempt, run, self.check)
            allowed = ((int(enum_for("operation_runs.status").FAILED),) if self.blocked
                       else (int(enum_for("operation_runs.status").SUCCEEDED),))
            if (event.event_type != registry["OPERATION_CONFIGURED"]["id"]
                    or event.reason != registry["OPERATION_CONFIGURED"]["branches"]["FINISH"]["reason"]
                    or len(event.rows) != 1 or event.rows[0].table != "operation_runs"
                    or event.rows[0].row_id != self.run_id
                    or event.rows[0].after.values.get("status") not in allowed):
                raise ConsistencyError("检查结果没有共同结束原流程")
            if self.blocked and not json_equal(event.rows[0].after.values.get("error_json"),
                                               _failure_document(self.check, run)):
                raise ConsistencyError("检查失败结论与实际阻挡及原尝试不一致")
        else:
            if self.terminal or self.blocked:
                raise ConsistencyError("检查未取得释放资格，不能生成活动释放")
            if (event.event_type != registry["AVAILABILITY_CHECK"]["id"]
                    or event.reason != registry["AVAILABILITY_CHECK"]["branches"]["RELEASE"]["reason"]
                    or not 1 <= len(event.rows) <= 128):
                raise ConsistencyError("检查释放页的事件分支或大小无效")
            for row in event.rows:
                expected = next(self.targets, None)
                if (row.table != "device_activities" or row.row_id != expected
                        or row.after.values != {"occupancy_state": int(enum_for("device_activities.occupancy_state").RELEASED),
                                                "availability_release_attempt_id": self.attempt_id}):
                    raise ConsistencyError("检查释放页与固定目标不一致")
                self.count += 1
        self.pending = replace(event, change_seq=None)

    def seal(self, validated):
        if self.pending != replace(validated.envelope, change_seq=None):
            raise ConsistencyError("检查事务事件没有通过原校验")
        self.pending = None
        self.position += 1

    def finish(self):
        expected_events = 1 if self.terminal else 2
        if (self.pending is not None or self.position < expected_events
                or (self.terminal or self.blocked) and self.position != expected_events
                or self.transaction.first_event_id + self.position - 1 != self.transaction.last_event_id
                or not self.blocked and (self.count != self.expected_count or next(self.targets, None) is not None)):
            raise ConsistencyError("检查结果和全部目标释放未共同封口")


def _failure_document(check, run):
    device_source = check["trigger_copy_id"] is not None
    return {"code": "source_device_unavailable" if device_source else "device_availability_unconfirmed",
            "stage": "source_read" if device_source else "execution", "details": {
                "device_id": check["device_id"], "operation_run_id": str(run["id"]),
                "attempts_used": run["attempts_used"], "max_attempts": run["max_attempts_used"]}}


def release_guard(event, context):
    group = context.availability_reads
    if not isinstance(group, ReleaseGroup) or group.pending != replace(event, change_seq=None):
        raise ConsistencyError("设备释放缺少原内核独立事务组")


def require_result_group(event, context):
    """成功检查结果只接受内核正在校验的首事件。"""
    group = context.availability_reads
    if (type(group) is not ReleaseGroup or group.position != 0
            or group.pending != replace(event, change_seq=None)):
        raise ConsistencyError("设备检查成功结果必须属于内核绑定的完整结果事务")


class FinishAvailabilityCommand:
    """保存实际 PASS；可释放时与整批旧活动及成功终结共同提交。"""

    def __init__(self, finish, key):
        self.finish, self.key = finish, key

    def plan(self, scope):
        from .capture import _CompositeScope
        from .operations import FinishAttemptCommand
        from camctl.operations.models import AttemptStatus
        if (self.finish.outcome.outcome.status is not AttemptStatus.SUCCEEDED
                or self.finish.run_finish is None or self.finish.run_finish.status is not RunOutcome.SUCCEEDED
                or self.finish.retry_wait):
            raise ConsistencyError("检查释放要求原 PASS 及成功终结申请")
        db = scope.connection
        existing = db.execute("SELECT id FROM history_transactions WHERE operation_key=?", (str(self.key),)).fetchone()
        if existing is not None:
            return self._reuse(scope, read_transaction_range(db, existing[0]))
        run = row_facts(db, "operation_runs", self.finish.ticket.run_id)
        if run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY):
            raise ConsistencyError("检查结果票据没有对应的检查流程")
        check = _check(db, run)
        found = db.execute("SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                           (run["id"], self.finish.ticket.attempt_id)).fetchone()
        if found is None:
            raise ConsistencyError("检查结果票据没有对应的原尝试")
        attempt_id = found[0]
        def bounded_result(finish):
            sub = _CompositeScope(scope, scope.max_event_id + 1)
            planned = FinishAttemptCommand(finish, self.key).plan(sub)
            if planned.read_only:
                return planned
            scope.allocate(len(planned.events))
            return BoundedCommandPlan(planned.events, lambda event, tx: _release_context(
                db, event, tx, run["id"], check["id"], attempt_id), planned.result, lambda connection: None)
        if run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                  int(enum_for("operation_runs.status").ACTIVE)):
            return bounded_result(self.finish)
        try:
            release_count = sum(len(page) for page in iter_release_targets(db, check))
            if release_count:
                _no_conflicts(db, check, attempt_id)
        except AvailabilityTargetBlocked:
            error = ErrorValue(**_failure_document(check, run))
            finish = replace(self.finish, run_finish=RunFinish(RunOutcome.FAILED, error))
            return bounded_result(finish)
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        result = FinishAttemptCommand(self.finish, self.key).plan(sub)
        if result.read_only or len(result.events) != 2:
            raise ConsistencyError("检查结果和成功终结必须是新的完整事实")
        allocation = scope.allocate(2 + (release_count + 127) // 128)
        from camctl.contracts.history_values import TransactionRange
        transaction = TransactionRange(allocation.txn_id, allocation.first_event_id, allocation.last_event_id)
        registry = load_event_registry()["events"]["AVAILABILITY_CHECK"]
        def events():
            yield from result.events
            event_id = transaction.first_event_id + 2
            for page in _release_pages(db, check):
                rows = tuple(update_change("device_activities", activity_id,
                    {"occupancy_state": int(enum_for("device_activities.occupancy_state").HELD),
                     "availability_release_attempt_id": None},
                    {"occupancy_state": int(enum_for("device_activities.occupancy_state").RELEASED),
                     "availability_release_attempt_id": attempt_id}) for activity_id in page)
                yield event_envelope(event_id, transaction.txn_id, registry["id"],
                    registry["branches"]["RELEASE"]["reason"], rows, self.finish.occurred_at)
                event_id += 1
        return BoundedCommandPlan(events(), lambda event, tx: _release_context(
            db, event, tx, run["id"], check["id"], attempt_id), result.result, lambda connection: None)

    def _reuse(self, scope, transaction):
        from camctl.contracts.history_values import HistoryBoundary
        from camctl.history.validators import validate_event_structure
        from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunStatus
        from camctl.operations.models import AttemptStatus
        from camctl.operations.result_format import result_document
        from camctl.persistence.row_history import read_row_values_at_boundary
        from .operations import _verify_result_ticket
        db, finish = scope.connection, self.finish
        cursor, position, last_target, released_count = None, 0, 0, 0
        attempt = run = check = None
        final_status = None
        while True:
            page = read_saved_transaction_page(db, OperationKey(self.key), transaction, cursor, limit=64)
            for event in page.items:
                name, branch, _ = validate_event_structure(event)
                if event.occurred_at != finish.occurred_at:
                    raise ConsistencyError("检查结果重送改变原事实时刻")
                if position == 0:
                    if name != "ATTEMPT_RESULT" or branch != "SUCCEED" or len(event.rows) != 1:
                        raise ConsistencyError("原结果事务缺少成功尝试")
                    attempt = row_facts(db, "operation_attempts", event.rows[0].row_id)
                    run = None if attempt is None else row_facts(db, "operation_runs", attempt["run_id"])
                    if run is None:
                        raise ConsistencyError("原结果缺少检查流程")
                    _verify_result_ticket(finish, run, attempt)
                    check = _check(db, run)
                    if (attempt["result_event_id"] != event.event_id
                            or not json_equal(attempt["result_json"], result_document(finish.outcome.outcome))
                            or any(not json_equal(attempt.get(k), v) for k, v in event.rows[0].after.values.items())):
                        raise ConsistencyError("重送检查结果与原尝试事实不同")
                    require_saved_availability(attempt, run, check)
                elif position == 1:
                    if (name != "OPERATION_CONFIGURED" or branch != "FINISH" or len(event.rows) != 1
                            or event.rows[0].table != "operation_runs" or event.rows[0].row_id != run["id"]):
                        raise ConsistencyError("原检查结果缺少共同终结")
                    final_status = event.rows[0].after.values.get("status")
                    if final_status not in (int(enum_for("operation_runs.status").SUCCEEDED),
                                            int(enum_for("operation_runs.status").FAILED)):
                        raise ConsistencyError("原检查结果终结状态不适用")
                    if run["status"] != final_status or run["retry_wait_required"] != 0:
                        raise ConsistencyError("原检查终态与当前流程事实不一致")
                    if final_status == int(enum_for("operation_runs.status").FAILED):
                        device_source = check["trigger_copy_id"] is not None
                        expected_error = {"code": "source_device_unavailable" if device_source else "device_availability_unconfirmed",
                            "stage": "source_read" if device_source else "execution", "details": {
                                "device_id": check["device_id"], "operation_run_id": str(run["id"]),
                                "attempts_used": attempt["attempt_no"], "max_attempts": attempt["max_attempts_used"]}}
                        if not json_equal(run["error_json"], expected_error) or not json_equal(
                                event.rows[0].after.values.get("error_json"), expected_error):
                            raise ConsistencyError("原检查失败结论与实际尝试不同")
                    elif run["error_json"] is not None:
                        raise ConsistencyError("原成功检查不能携带流程错误")
                else:
                    if (final_status != int(enum_for("operation_runs.status").SUCCEEDED)
                            or name != "AVAILABILITY_CHECK" or branch != "RELEASE" or not event.rows):
                        raise ConsistencyError("原检查结果包含非释放事件")
                    for row in event.rows:
                        if (row.table != "device_activities" or row.after.values != {
                                "occupancy_state": int(enum_for("device_activities.occupancy_state").RELEASED),
                                "availability_release_attempt_id": attempt["id"]}):
                            raise ConsistencyError("原释放事件改变了目标或依据")
                        found = db.execute("SELECT id FROM device_availability_targets WHERE check_id=? AND activity_id=?",
                                           (check["id"], row.row_id)).fetchone()
                        if found is None or found[0] <= last_target:
                            raise ConsistencyError("原释放事件不是固定目标的有序唯一页")
                        last_target, released_count = found[0], released_count + 1
                position += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        if position == 1:
            boundary = HistoryBoundary(transaction.txn_id, transaction.last_event_id)
            current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)
            original = read_row_values_at_boundary(db, owner=("action", run["action_id"]),
                table="operation_runs", row_id=run["id"], columns=frozenset({"status"}),
                current_values=run, boundary=boundary, current_boundary=current)
            if original["status"] in (int(enum_for("operation_runs.status").PENDING),
                                       int(enum_for("operation_runs.status").ACTIVE)):
                raise ConsistencyError("原单结果事务没有可靠终态流程")
            return CommandPlan((), {}, {}, FinishAttemptResult(FinishDisposition.SAVED,
                AttemptStatus.SUCCEEDED, RunStatus[enum_for("operation_runs.status")(original["status"]).name]),
                read_only=True)
        if position < 2:
            raise ConsistencyError("原检查结果事务不完整")
        if final_status == int(enum_for("operation_runs.status").SUCCEEDED):
            matches = count = 0
            boundary = HistoryBoundary(transaction.txn_id, transaction.last_event_id)
            current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)
            for page in iter_fixed_targets(db, check["id"]):
                for _, action_id, activity_id in page:
                    activity = row_facts(db, "device_activities", activity_id)
                    if action_id != check["action_id"] or activity is None:
                        raise ConsistencyError("原检查固定目标归属不一致")
                    state = read_row_values_at_boundary(db, owner=("action", activity["action_id"]),
                        table="device_activities", row_id=activity_id,
                        columns=frozenset({"occupancy_state", "availability_release_attempt_id"}),
                        current_values=activity, boundary=boundary, current_boundary=current)
                    if state["occupancy_state"] != int(enum_for("device_activities.occupancy_state").RELEASED):
                        raise ConsistencyError("原成功事务未解除全部固定目标")
                    matches += int(state["availability_release_attempt_id"] == attempt["id"])
                    count += 1
            if count == 0 or matches != released_count:
                raise ConsistencyError("原释放历史与目标边界不一致")
        elif position != 2:
            raise ConsistencyError("原失败检查不得包含释放事件")
        return CommandPlan((), {}, {}, FinishAttemptResult(FinishDisposition.SAVED,
            AttemptStatus.SUCCEEDED, RunStatus[enum_for("operation_runs.status")(final_status).name]), read_only=True)


def finish_availability(finish, key, owned):
    from .operations import _outcome_of
    return _outcome_of(commit_operation(FinishAvailabilityCommand(finish, key), key, owned))


class ResolveDisposition(Enum):
    SUCCEEDED = "succeeded"
    WAIT = "wait"
    TERMINAL = "terminal"


class ResolveAvailabilityCommand:
    def __init__(self, check_id, occurred_at, key):
        from camctl.contracts.values import ObjectId, UtcMicros
        self.check_id, self.occurred_at, self.key = ObjectId(check_id), UtcMicros(occurred_at), OperationKey(key)

    def plan(self, scope):
        db = scope.connection
        found = db.execute("SELECT id FROM operation_runs WHERE availability_check_id=? AND kind=?",
            (self.check_id, int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY))).fetchone()
        if found is None:
            raise ConsistencyError("检查缺少唯一原流程")
        run = row_facts(db, "operation_runs", found[0])
        check = _check(db, run)
        saved = db.execute("SELECT id FROM history_transactions WHERE operation_key=?", (str(self.key),)).fetchone()
        if saved is not None:
            from camctl.contracts.history_values import HistoryBoundary
            from camctl.history.validators import validate_event_structure
            from camctl.persistence.row_history import read_row_values_at_boundary
            tx = read_transaction_range(db, saved[0])
            page = read_saved_transaction_page(db, self.key, tx, limit=2)
            if (page.next_cursor is not None or len(page.items) != 1
                    or validate_event_structure(page.items[0])[:2] != ("OPERATION_CONFIGURED", "FINISH")
                    or page.items[0].occurred_at != self.occurred_at or len(page.items[0].rows) != 1
                    or page.items[0].rows[0].table != "operation_runs"
                    or page.items[0].rows[0].row_id != run["id"]
                    or page.items[0].rows[0].after.values.get("status") != int(enum_for("operation_runs.status").SUCCEEDED)
                    or run["status"] != int(enum_for("operation_runs.status").SUCCEEDED)
                    or run["retry_wait_required"] != 0 or run["error_json"] is not None):
                raise ConsistencyError("原键不是此检查的无调用终结")
            boundary, current = HistoryBoundary(tx.txn_id, tx.last_event_id), HistoryBoundary(scope.max_txn_id, scope.max_event_id)
            count = 0
            for targets in iter_fixed_targets(db, check["id"]):
                for _, action_id, activity_id in targets:
                    activity = row_facts(db, "device_activities", activity_id)
                    if activity is None or action_id != check["action_id"]:
                        raise ConsistencyError("原检查目标归属无效")
                    state = read_row_values_at_boundary(db, owner=("action", activity["action_id"]),
                        table="device_activities", row_id=activity_id, columns=frozenset({"occupancy_state"}),
                        current_values=activity, boundary=boundary, current_boundary=current)
                    if state["occupancy_state"] != int(enum_for("device_activities.occupancy_state").RELEASED):
                        raise ConsistencyError("原终结边界尚未解决全部目标")
                    count += 1
            if count == 0:
                raise ConsistencyError("原检查缺少固定目标")
            return CommandPlan((), {}, {}, ResolveDisposition.SUCCEEDED, read_only=True)
        if run["status"] not in (int(enum_for("operation_runs.status").PENDING), int(enum_for("operation_runs.status").ACTIVE)):
            return CommandPlan((), {}, {}, ResolveDisposition.TERMINAL, read_only=True)
        with closing(db.execute("SELECT 1 FROM operation_attempts WHERE run_id=? AND (status=? OR result_event_id IS NULL) LIMIT 1",
                               (run["id"], int(enum_for("operation_attempts.status").RUNNING)))) as cursor:
            if cursor.fetchone() is not None:
                return CommandPlan((), {}, {}, ResolveDisposition.WAIT, read_only=True)
        try:
            pending = next(iter(iter_release_targets(db, check)), None)
        except AvailabilityTargetBlocked:
            pending = True
        if pending is not None:
            return CommandPlan((), {}, {}, ResolveDisposition.WAIT, read_only=True)
        allocation = scope.allocate(1)
        registry = load_event_registry()["events"]["OPERATION_CONFIGURED"]
        event = event_envelope(allocation.first_event_id, allocation.txn_id, registry["id"],
            registry["branches"]["FINISH"]["reason"], (update_change("operation_runs", run["id"],
            {"status": run["status"], "retry_wait_required": run["retry_wait_required"]},
            {"status": int(enum_for("operation_runs.status").SUCCEEDED), "retry_wait_required": 0}),), self.occurred_at)
        return BoundedCommandPlan((event,), lambda proposed, tx: _resolved_context(db, proposed, tx, run["id"], check["id"]),
                                  ResolveDisposition.SUCCEEDED, lambda connection: None)


def finish_if_resolved(check_id, occurred_at, key, owned):
    from .operations import _outcome_of
    return _outcome_of(commit_operation(ResolveAvailabilityCommand(check_id, occurred_at, key), key, owned))
