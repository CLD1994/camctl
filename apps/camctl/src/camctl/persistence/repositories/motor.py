"""电机意图及结果的权威历史事务；已有意图从不产生恢复发送许可。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from camctl.contracts.enums import decode_member
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.contracts.workflow_errors import action_error_id
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.motor.rules import (
    MotorRequestRuleError, derive_finish_values, expected_motor_plan_status,
    validate_motor_combination as _validate_combination, validate_motor_plan_change,
    validate_motor_send_intent, validate_motor_finish_request, validate_motor_finish_result,
)
from camctl.motor.models import (
    FinishSendRequest,
    FinishSendResult,
    MotorActionFacts,
    MotorFinalKind,
    PrepareOutcome,
    PrepareSendRequest,
    PrepareSendResult,
    SendFacts,
    SendOutcome,
    SendPermit,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.repositories.transactions import (
    CommandPlan,
    TransactionError,
    TransactionScope,
    commit_operation,
    event_envelope,
    next_row_id,
    row_change,
    row_facts,
    saved_transaction_events,
    update_change,
)
from camctl.scheduling.rules import (
    ExpirationReason,
    LaunchWindow,
    WindowPhase,
    window_phase,
)

_EVENT = 34
from .motor_guards import (
    _BRANCH,
    _finish_values,
    _motor_guard,
    _notice,
    register_motor_guards,
)






def _verify_saved_rows(connection, event, action_id):
    """按原完整边界核对历史中的每项结果，容许后来可靠结果继续推进。"""
    with closing(
        connection.execute(
            "SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
        )
    ) as cursor:
        latest = cursor.fetchone()
    if latest is None:
        raise ConsistencyError("电机事实缺少历史事务")
    current_boundary = HistoryBoundary(*latest)
    boundary = HistoryBoundary(
        event["transaction"].txn_id, event["transaction"].last_event_id
    )
    for row in event["body"]["rows"]:
        if (
            row["table"] not in ("actions", "plans", "motor_notifications")
            or not row["after"]["exists"]
        ):
            raise ConsistencyError("原电机意图事务包含无法解释的记录")
        current = row_facts(connection, row["table"], row["id"])
        if current is None:
            raise ConsistencyError("原电机意图事务的投影记录缺失")
        owner = (
            ("plan", row["id"]) if row["table"] == "plans" else ("action", action_id)
        )
        historical = read_row_values_at_boundary(
            connection,
            owner=owner,
            table=row["table"],
            row_id=row["id"],
            columns=frozenset(row["after"]["values"]),
            current_values=current,
            boundary=boundary,
            current_boundary=current_boundary,
        )
        if not json_equal(historical, row["after"]["values"]):
            raise ConsistencyError("原电机意图事务的完整结果与历史不符")


def _verify_intent(connection, action, notice):
    if notice.intent_operation_key is None:
        return
    try:
        saved = saved_transaction_events(
            connection, OperationKey(notice.intent_operation_key)
        )
        if (
            saved is None
            or len(saved) != 1
            or saved[0]["type"] != _EVENT
            or saved[0]["reason"] != 1
        ):
            raise ConsistencyError("电机意图没有对应的唯一原发送意图事务")
        event = saved[0]
        request = PrepareSendRequest(**event["body"]["evidence"]["request"])
        UtcMicros(request.trusted_wall_now)
        if (
            request.action_id != action["id"]
            or request.occurred_at != notice.intent_at
            or event["occurred_at"] != notice.intent_at
        ):
            raise ConsistencyError("原发送意图事务身份或输入与发送事实不符")
        window = LaunchWindow(
            action["scheduled_at"],
            action["scheduled_at"] + action["max_delay_ms"] * 1000,
        )
        if window_phase(window, request.trusted_wall_now) is not WindowPhase.IN_WINDOW:
            raise ConsistencyError("原发送意图事务缺少时间资格")
        actions = [row for row in event["body"]["rows"] if row["table"] == "actions"]
        notices = [
            row
            for row in event["body"]["rows"]
            if row["table"] == "motor_notifications"
        ]
        plans = [row for row in event["body"]["rows"] if row["table"] == "plans"]
        if (
            len(actions) != 1
            or actions[0]["id"] != action["id"]
            or actions[0]["after"]["values"].get("status") != 2
            or actions[0]["after"]["values"].get("execution_started") != 1
        ):
            raise ConsistencyError("原发送意图事务缺少共同保存的开始事实")
        expected = {
            "action_id": action["id"],
            "intent_at": notice.intent_at,
            "intent_operation_key": notice.intent_operation_key,
            "outcome": 1,
            "written_bytes": None,
            "errno": None,
            "finished_at": None,
        }
        if (
            len(notices) != 1
            or notices[0]["id"] != notice.notification_id
            or notices[0]["before"]["exists"]
            or not json_equal(notices[0]["after"]["values"], expected)
        ):
            raise ConsistencyError("原发送意图事务缺少完整的唯一发送记录")
        if len(plans) > 1 or any(
            row["id"] != action["plan_id"] or row["after"]["values"] != {"status": 2}
            for row in plans
        ):
            raise ConsistencyError("原发送意图事务的父计划变化不符")
        _verify_saved_rows(connection, event, action["id"])
    except (TypeError, KeyError, ValueError) as error:
        if isinstance(error, ConsistencyError):
            raise
        raise ConsistencyError("原发送意图事务无法完整核实") from error


def read_motor_facts(connection, action_id):
    """共同读取可靠视图；已有写事务内复用视图，普通调用持有短读事务。"""
    if connection.in_transaction:
        return _read_motor_facts(connection, action_id)
    with closing(connection.execute("BEGIN")):
        pass
    try:
        result = _read_motor_facts(connection, action_id)
        with closing(connection.execute("COMMIT")):
            pass
        return result
    except BaseException:
        with closing(connection.execute("ROLLBACK")):
            pass
        raise


def _read_motor_facts(connection, action_id):
    """共同读取并验证动作与专属事实；数据库错误不转换为空状态。"""
    ObjectId(action_id)
    action = row_facts(connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("电机动作不存在")
    with closing(
        connection.execute(
            "SELECT id FROM motor_notifications WHERE action_id=?", (action_id,)
        )
    ) as cursor:
        found = cursor.fetchone()
    values = (
        None
        if found is None
        else row_facts(connection, "motor_notifications", found[0])
    )
    notice = None if values is None else _notice(values)
    _validate_combination(action, notice)
    if notice is not None:
        _verify_intent(connection, action, notice)
    return MotorActionFacts(action, notice)


def _request_evidence(request):
    if hasattr(request, "trusted_wall_now"):
        return {
            "action_id": request.action_id,
            "trusted_wall_now": request.trusted_wall_now,
            "occurred_at": request.occurred_at,
        }
    return {
        "action_id": request.action_id,
        "occurred_at": request.occurred_at,
        "kind": request.kind.value,
        "permit": (
            None
            if request.permit is None
            else {
                "action_id": request.permit.action_id,
                "notification_id": request.permit.notification_id,
                "operation_key": str(request.permit.operation_key),
            }
        ),
        "written_bytes": request.written_bytes,
        "errno": request.errno,
        "reason": request.reason,
        "expiration_reason": (
            None
            if request.expiration_reason is None
            else request.expiration_reason.value
        ),
    }




class _MotorCommand:
    def __init__(self, request, key, prepare):
        self.request, self.key, self.prepare = request, key, prepare

    def plan(self, scope):
        connection = scope.connection
        request = self.request
        ObjectId(request.action_id)
        UtcMicros(request.occurred_at)
        evidence = _request_evidence(request)
        saved = saved_transaction_events(connection, self.key)
        if saved is not None:
            return self._reuse(scope, saved, evidence)
        facts = read_motor_facts(connection, request.action_id)
        action = dict(facts.action)
        notice = facts.notification
        terminal = action["status"] in (3, 4, 5, 6)
        if self.prepare:
            UtcMicros(request.trusted_wall_now)
            if terminal:
                return self._readonly(
                    PrepareSendResult(PrepareOutcome.REJECTED, reason="terminal")
                )
            if notice is not None:
                return self._readonly(PrepareSendResult(PrepareOutcome.ALREADY))
            if action["cancel_requested"]:
                return self._readonly(
                    PrepareSendResult(PrepareOutcome.REJECTED, reason="canceled")
                )
            window = LaunchWindow(
                action["scheduled_at"],
                action["scheduled_at"] + action["max_delay_ms"] * 1000,
            )
            phase = window_phase(window, request.trusted_wall_now)
            if phase is not WindowPhase.IN_WINDOW:
                return self._readonly(
                    PrepareSendResult(
                        PrepareOutcome.REJECTED,
                        reason=(
                            "too_early"
                            if phase is WindowPhase.BEFORE_START
                            else "window_ended"
                        ),
                    )
                )
            result_values = dict(
                action_id=request.action_id,
                intent_at=request.occurred_at,
                intent_operation_key=str(self.key),
                outcome=int(SendOutcome.PENDING),
                written_bytes=None,
                errno=None,
                finished_at=None,
            )
            action_values = {"status": 2, "execution_started": 1}
            if action["first_window_observed_at"] is None:
                action_values["first_window_observed_at"] = request.trusted_wall_now
            branch = 1
        else:
            if terminal:
                raise TransactionError("不同操作身份不能替换或伪装已有电机终态")
            status, outcome, code, details, expiry = _finish_values(request, facts)
            action_values = {"status": status}
            if status == 4:
                action_values.update(error_code=code, error_details_json=details)
            if request.kind is MotorFinalKind.CHANNEL_UNAVAILABLE:
                action_values["execution_started"] = 1
            if status == 5:
                action_values["expiration_reason"] = expiry
            result_values = dict(
                action_id=request.action_id,
                intent_at=None if notice is None else notice.intent_at,
                intent_operation_key=(
                    None if notice is None else notice.intent_operation_key
                ),
                outcome=int(outcome),
                written_bytes=request.written_bytes,
                errno=request.errno if request.kind is MotorFinalKind.FAILED else None,
                finished_at=request.occurred_at,
            )
            if notice is None and request.kind is MotorFinalKind.EXPIRED:
                result_values = None
            branch = _BRANCH[request.kind]
        state = {"actions": {}, "motor_notifications": {}, "plans": {}}
        # 聚合只读取所属计划中的公共状态；完整目标动作另外提供原输入。
        with closing(
            connection.execute(
                "SELECT id,plan_id,status,execution_started FROM actions WHERE plan_id=?",
                (action["plan_id"],),
            )
        ) as cursor:
            for row in cursor:
                state["actions"][row[0]] = dict(
                    zip(("id", "plan_id", "status", "execution_started"), row)
                )
        state["actions"][action["id"]] = action
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError("电机动作缺少父计划")
        state["plans"][plan["id"]] = plan
        rows = [
            update_change(
                "actions",
                action["id"],
                {name: action[name] for name in action_values},
                action_values,
            )
        ]
        owners = {
            ("actions", action["id"]): ("action", action["id"]),
            ("plans", plan["id"]): ("plan", plan["id"]),
        }
        notification_id = None
        if notice is not None:
            notification_id = notice.notification_id
            old = row_facts(connection, "motor_notifications", notification_id)
            state["motor_notifications"][notification_id] = old
            mutable = ("outcome", "written_bytes", "errno", "finished_at")
            rows.append(
                update_change(
                    "motor_notifications",
                    notification_id,
                    {name: old[name] for name in mutable},
                    {name: result_values[name] for name in mutable},
                )
            )
        elif result_values is not None:
            notification_id = next_row_id(connection, "motor_notifications")
            rows.append(
                row_change("motor_notifications", notification_id, result_values)
            )
        if notification_id is not None:
            owners[("motor_notifications", notification_id)] = ("action", action["id"])
        statuses = [
            action_values["status"] if identity == action["id"] else values["status"]
            for identity, values in state["actions"].items()
        ]
        complete = all(value in (3, 4, 5, 6) for value in statuses)
        started = action_values.get(
            "execution_started", action["execution_started"]
        ) or any(v["execution_started"] for v in state["actions"].values())
        plan_status = 3 if complete else 2 if started else 1
        if plan_status != plan["status"]:
            rows.append(
                update_change(
                    "plans",
                    plan["id"],
                    {"status": plan["status"]},
                    {"status": plan_status},
                )
            )
        allocation = scope.allocate(1)
        event = event_envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _EVENT,
            branch,
            rows,
            request.occurred_at,
            {"request": evidence},
        )
        result = (
            PrepareSendResult(
                PrepareOutcome.GRANTED,
                SendPermit(action["id"], notification_id, self.key),
            )
            if self.prepare
            else FinishSendResult(action_values["status"])
        )
        return CommandPlan(
            (event,),
            owners,
            state,
            result=result,
            read_coverage=ReadCoverage(
                {
                    ("actions", "plan_id"): frozenset([plan["id"]]),
                    ("motor_notifications", "action_id"): frozenset([action["id"]]),
                }
            ),
        )

    @staticmethod
    def _readonly(result):
        return CommandPlan((), {}, {}, result=result, read_only=True)

    def _reuse(self, scope, saved, evidence):
        if (
            len(saved) != 1
            or saved[0]["type"] != _EVENT
            or saved[0]["reason"] != (1 if self.prepare else _BRANCH[self.request.kind])
        ):
            raise TransactionError("原操作身份不属于该电机阶段")
        event = saved[0]
        if event["occurred_at"] != self.request.occurred_at or not json_equal(
            event["body"]["evidence"], {"request": evidence}
        ):
            raise TransactionError("原电机操作输入与本次核实不符")
        action_rows = [
            row for row in event["body"]["rows"] if row["table"] == "actions"
        ]
        if len(action_rows) != 1 or action_rows[0]["id"] != self.request.action_id:
            raise TransactionError("原电机操作的动作身份不符")
        for row in event["body"]["rows"]:
            if (
                row["table"] not in ("actions", "plans", "motor_notifications")
                or not row["after"]["exists"]
            ):
                raise TransactionError("原电机事务包含无法解释的记录")
            current = row_facts(scope.connection, row["table"], row["id"])
            if current is None:
                raise ConsistencyError("原电机事务的投影记录缺失")
            owner = (
                ("plan", row["id"])
                if row["table"] == "plans"
                else ("action", self.request.action_id)
            )
            columns = frozenset(row["after"]["values"])
            historical = read_row_values_at_boundary(
                scope.connection,
                owner=owner,
                table=row["table"],
                row_id=row["id"],
                columns=columns,
                current_values=current,
                boundary=HistoryBoundary(
                    event["transaction"].txn_id, event["transaction"].last_event_id
                ),
                current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id),
            )
            if not json_equal(historical, row["after"]["values"]):
                raise ConsistencyError("原电机事务的完整结果与历史恢复不符")
        read_motor_facts(scope.connection, self.request.action_id)
        return self._readonly(
            PrepareSendResult(PrepareOutcome.ALREADY)
            if self.prepare
            else FinishSendResult(action_rows[0]["after"]["values"]["status"], True)
        )






class MotorRepository:
    def read_facts(self, action_id, owned):
        return read_motor_facts(owned.connection, action_id)

    def prepare_send(self, request, key, owned):
        return self._write(_MotorCommand(request, key, True), key, owned)

    def finish_send(self, request, key, owned):
        return self._write(_MotorCommand(request, key, False), key, owned)

    def observe_window(self, request, key, owned):
        from camctl.persistence.repositories.scheduling import SchedulingRepository

        return SchedulingRepository().observe_window(request, key, owned)

    def verify_operation(self, request, key, owned):
        """只核实已经结束的原提交；调用方先关闭旧连接，再交付新连接。

        缺少原键表示原事务可靠未提交。已有事务只解释原请求及完整
        结果，不运行 plan，不执行写入，也不重建发送许可。
        """
        from camctl.persistence.repositories.scheduling import (
            ObserveWindowCommand,
            ObserveWindowRequest,
        )

        if not isinstance(
            request, (PrepareSendRequest, FinishSendRequest, ObserveWindowRequest)
        ):
            raise TypeError("电机核实要求原阶段的完整请求")
        connection = owned.connection
        reading = False
        try:
            ObjectId(request.action_id)
            UtcMicros(request.occurred_at)
            key = OperationKey(str(key))
            if connection.in_transaction:
                raise TransactionError("原操作核实要求独立新连接，不加入既有事务")
            with closing(connection.execute("BEGIN")):
                pass
            reading = True
            saved = saved_transaction_events(connection, key)
            if saved is None:
                outcome = DbOutcome(
                    DbOutcomeKind.ROLLED_BACK,
                    error=TransactionError("原电机操作身份可靠未提交"),
                    stage="verification",
                )
            else:
                with closing(
                    connection.execute(
                        "SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
                    )
                ) as cursor:
                    latest = cursor.fetchone()
                if latest is None:
                    raise ConsistencyError("已存电机操作缺少完整当前历史边界")
                scope = TransactionScope(connection, *latest)
                if isinstance(request, PrepareSendRequest):
                    UtcMicros(request.trusted_wall_now)
                    plan = _MotorCommand(request, key, True)._reuse(
                        scope, saved, _request_evidence(request)
                    )
                elif isinstance(request, FinishSendRequest):
                    if not isinstance(request.kind, MotorFinalKind):
                        raise TransactionError("原结束输入缺少受约束的发送结果分类")
                    plan = _MotorCommand(request, key, False)._reuse(
                        scope, saved, _request_evidence(request)
                    )
                else:
                    UtcMicros(request.trusted_wall_now)
                    if (
                        len(saved) != 1
                        or saved[0]["type"] != 7
                        or saved[0]["reason"] != 1
                    ):
                        raise TransactionError("原操作身份不属于窗口观察阶段")
                    rows = saved[0]["body"]["rows"]
                    if (
                        len(rows) != 1
                        or rows[0]["table"] != "actions"
                        or rows[0]["id"] != request.action_id
                        or rows[0]["after"]["values"]
                        != {"first_window_observed_at": request.trusted_wall_now}
                    ):
                        raise TransactionError("原窗口观察的完整输入与本次核实不符")
                    plan = ObserveWindowCommand(request, key)._reuse(scope, saved)
                    read_motor_facts(connection, request.action_id)
                    _verify_saved_rows(connection, saved[0], request.action_id)
                if not plan.read_only or plan.events or scope.allocation is not None:
                    raise ConsistencyError("原电机操作核实不能产生写入或发送资格")
                transaction = saved[0]["transaction"]
                outcome = DbOutcome(
                    DbOutcomeKind.COMPLETED,
                    plan.result,
                    boundary=HistoryBoundary(
                        transaction.txn_id, transaction.last_event_id
                    ),
                    stage="verification",
                )
            with closing(connection.execute("COMMIT")):
                pass
            reading = False
            return outcome
        except (sqlite3.Error, ValueError, TypeError) as error:
            if reading:
                try:
                    with closing(connection.execute("ROLLBACK")):
                        pass
                except sqlite3.Error as rollback_error:
                    return DbOutcome(
                        DbOutcomeKind.UNKNOWN,
                        error=rollback_error,
                        stage="verification",
                    )
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=error, stage="verification")

    @staticmethod
    def _write(command, key, owned):
        receipt = commit_operation(command, key, owned)
        if receipt.kind == "completed":
            return DbOutcome(
                DbOutcomeKind.COMPLETED, receipt.result, boundary=receipt.boundary
            )
        return DbOutcome(
            (
                DbOutcomeKind.ROLLED_BACK
                if receipt.kind == "rolled_back"
                else DbOutcomeKind.UNKNOWN
            ),
            error=receipt.error,
        )
