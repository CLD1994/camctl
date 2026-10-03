"""录像完成终态与正式产物登记的唯一事务。

经 P3 事务内核组织：动作成功终态、正式产物登记及父计划状态在
同一事务共同保存，任一写入失败整组回滚；登记前经 X1 纯规则校
验，事件守卫从当前文件事实复核来源、原设备绑定、文件角色与初
始可用性组合。停止、活动结束与产物、动作结果分别保存，不互相混同。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord, RecordStatus
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import OperationKey
from camctl.history.validators import EventValidationError, register_guard
from camctl.outputs.catalog import (
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
    validate_output_registration,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id as _next_id,
    row_change as _row,
    row_facts,
    saved_transaction_events,
    update_change as _update,
)

_ACTION_FINISHED_EVENT = 8
_OUTPUT_REGISTERED_EVENT = 20
_PLAN_STATUS_EVENT = 9
_EMERGENCY_RECORDED_EVENT = 33

_ACTION_RUNNING = 2
_ACTION_SUCCEEDED = 3
_PLAN_COMPLETE = 3

#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

_KIND_CODES = {OutputKind.ORIGINAL: 1, OutputKind.REPAIRED: 2, OutputKind.PREVIEW: 3}

#: device_files.role 与产物种类的对应；修复产物承载于中间文件。
_FILE_ROLE_FOR_KIND = {1: 2, 3: 3}
_ACTION_TYPE = enum_for("actions.type")
_CAPTURE_TYPES = frozenset({
    _ACTION_TYPE.CAMERA_TAKE_PHOTO,
    _ACTION_TYPE.CAMERA_RECORD,
    _ACTION_TYPE.CAMERA_TIMELAPSE,
})


@dataclass(frozen=True)
class FinishCapture:
    """一次录像完成登记的完整输入：终态事实与全部适用产物。"""

    action_id: int
    drafts: tuple[OutputDraft, ...]
    catalog_facts: OutputCatalogFacts
    occurred_at: int


@dataclass(frozen=True)
class CaptureResult:
    """完成登记的已保存事实。"""

    action_status: int
    plan_status: int
    output_ids: tuple[int, ...]


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _action_finish_guard(event, context) -> None:
    """动作终态组合与产物同事务校验。"""
    for row in event.rows:
        if row.table == "actions" and row.before.exists:
            after = row.after.values
            if after.get("status") == _ACTION_SUCCEEDED:
                facts = _guard_facts(context, "actions", row.row_id)
                facts.update(row.before.values)
                if facts.get("cancel_requested") != 0:
                    raise EventValidationError("取消请求生效的动作不能保存成功终态")
        elif row.table == "outputs" and not row.before.exists:
            source = row.after.values.get("source_action_id")
            action = _guard_facts(context, "actions", source)
            if action.get("status") not in _ACTION_TERMINAL:
                raise EventValidationError(
                    f"产物登记要求源动作终态同事务成立: 动作 {source} 状态"
                    f" {action.get('status')}"
                )


def _output_guard(event, context) -> None:
    """从登记事件当前事实核对来源、原设备绑定及文件角色。"""
    for row in event.rows:
        if row.table != "outputs" or row.before.exists:
            continue
        values = row.after.values
        source_id = values["source_action_id"]
        source = _registration_facts(context, "actions", source_id)
        source_binding = _capture_binding(source)
        kind = values.get("kind")
        device_file_id = values.get("device_file_id")
        if device_file_id is not None:
            facts = _registration_facts(context, "device_files", device_file_id)
            if facts.get("source_action_id") != source_id or facts.get("ownership_evidence_json") is None:
                raise EventValidationError("产物承载文件缺少该来源动作的可靠归属")
            observer = _registration_facts(context, "actions", facts.get("observer_action_id"))
            if _capture_binding(observer) != source_binding:
                raise EventValidationError("产物来源与文件观察者的原设备或驱动绑定不一致")
            expected_role = _FILE_ROLE_FOR_KIND.get(kind)
            if expected_role is not None and facts.get("role") != expected_role:
                raise EventValidationError(
                    f"产物种类与文件角色不符: kind={kind}"
                    f" role={facts.get('role')}"
                )
            if facts.get("completion_state") != 3:
                raise EventValidationError(
                    f"产物承载文件未完成: {device_file_id}"
                )
        else:
            facts = _registration_facts(context, "intermediate_files", values.get("intermediate_file_id"))
            if (facts.get("owner_action_id") != source_id
                    or "owner_delivery_id" not in facts
                    or facts["owner_delivery_id"] is not None):
                raise EventValidationError("产物中间文件不属于来源动作的文件责任")


def _registration_facts(context, table: str, identity: int | None) -> Mapping[str, Any]:
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"产物登记缺少当前关联记录: {table}#{identity}")
    return facts


def _capture_binding(action: Mapping[str, Any]) -> tuple[str, str]:
    if action.get("type") not in _CAPTURE_TYPES:
        raise EventValidationError("产物来源和设备文件观察者必须是拍摄动作")
    device, driver = action.get("device_id"), action.get("driver_id")
    if not isinstance(device, str) or not device or not isinstance(driver, str) or not driver:
        raise EventValidationError("拍摄动作缺少已保存的设备或驱动绑定")
    return device, driver


def _cleanup_aggregate_guard(event, context) -> None:
    """产物初始可用性与清理状态组合校验。"""
    for row in event.rows:
        if row.table != "outputs" or row.before.exists:
            continue
        values = row.after.values
        if values.get("cleanup_status") != 1:
            raise EventValidationError("正式登记的初始清理状态必须是未请求")
        availability = values.get("availability")
        if availability not in (1, 5):
            raise EventValidationError(f"登记初始可用性非法: {availability!r}")
        if availability == 5 and values.get("error_json") is None:
            raise EventValidationError("未知可用性必须携带错误依据")


def _plan_aggregate_guard(event, context) -> None:
    """计划状态与动作聚合一致性（受理创建与终态推进共用）。"""
    for row in event.rows:
        if row.table != "plans":
            continue
        if not row.before.exists:
            plan_id = row.row_id
            actions = [
                values
                for values in context.state_rows.get("actions", {}).values()
                if values.get("plan_id") == plan_id
            ]
            statuses = {values.get("status") for values in actions}
            expected = 3 if (actions and statuses == {4}) else 1
            if row.after.values.get("status") != expected:
                raise EventValidationError(f"计划状态与动作聚合不符: 期望 {expected}")
            continue
        if row.after.values.get("status") == _PLAN_COMPLETE:
            plan_id = row.row_id
            actions = [
                values
                for values in context.state_rows.get("actions", {}).values()
                if values.get("plan_id") == plan_id
            ]
            if not actions or any(
                values.get("status") not in _ACTION_TERMINAL for values in actions
            ):
                raise EventValidationError("计划完成要求全部动作终态")


def register_capture_guards() -> None:
    """注册采集终态事务的正式业务守卫（装配期调用）。"""
    register_guard("action_finish", _action_finish_guard)
    register_guard("output", _output_guard)
    register_guard("cleanup_aggregate", _cleanup_aggregate_guard)
    register_guard("plan_aggregate", _plan_aggregate_guard)
    register_guard("emergency", _emergency_guard)
    register_guard("activity", _activity_guard)
    register_guard("release", _release_guard)


class FinishCaptureCommand:
    """一次录像完成登记的完整事务命令。"""

    def __init__(self, command: FinishCapture, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("完成登记的重送须由调用方按原事务核实")

        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {command.action_id}")
        siblings = self._sibling_actions(connection, action)
        self._state["actions"] = dict(siblings)
        self._state.setdefault("outputs", {})
        self._state.setdefault("device_files", {})
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        if action["status"] != _ACTION_RUNNING or action["cancel_requested"]:
            raise TransactionError(
                f"只有未取消的执行中动作能保存成功终态: {command.action_id}"
                f" status={action['status']}"
            )

        # 登记规则在同一事务内校验：任一草稿不合法整组拒绝。
        _capture_binding(action)
        if command.catalog_facts.action_id != command.action_id:
            raise TransactionError("目录上下文与完成命令的动作身份不一致")
        changes = validate_output_registration(command.drafts, command.catalog_facts)
        for output in changes.outputs:
            is_device = output.device_file_id is not None
            table = "device_files" if is_device else "intermediate_files"
            file_id = output.device_file_id if is_device else output.intermediate_file_id
            facts = row_facts(connection, table, file_id)
            if facts is None:
                raise TransactionError(f"产物承载文件不存在: {table}#{file_id}")
            self._state.setdefault(table, {})[file_id] = facts
            if is_device:
                observer_id = facts["observer_action_id"]
                if observer_id not in self._state["actions"]:
                    observer = row_facts(connection, "actions", observer_id)
                    if observer is None:
                        raise TransactionError(f"文件观察者不存在: {observer_id}")
                    self._state["actions"][observer_id] = observer

        templates = [
            _envelope(
                0, 0, _ACTION_FINISHED_EVENT, 1,
                (
                    _update(
                        "actions",
                        command.action_id,
                        {"status": action["status"]},
                        {"status": _ACTION_SUCCEEDED},
                    ),
                ),
                command.occurred_at,
            )
        ]
        output_ids: list[int] = []
        next_output_id = _next_id(connection, "outputs")
        for output in changes.outputs:
            output_ids.append(next_output_id)
            values = {
                "source_action_id": command.action_id,
                "kind": _KIND_CODES[output.kind],
                "device_file_id": output.device_file_id,
                "intermediate_file_id": output.intermediate_file_id,
                "original_name": None,
                "media_type": None,
                "availability": 1,
                "cleanup_status": 1,
                "cleanup_error_json": None,
                "media_json": {},
                "error_json": None,
            }
            self._owners[("outputs", next_output_id)] = ("output", next_output_id)
            templates.append(
                _envelope(
                    0, 0, _OUTPUT_REGISTERED_EVENT, _KIND_CODES[output.kind],
                    (_row("outputs", next_output_id, values),), command.occurred_at,
                )
            )
            next_output_id += 1
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id,
        )
        plan_complete = all(
            values.get("status") in _ACTION_TERMINAL
            or values["id"] == command.action_id
            for values in siblings.values()
        )
        plan_status = plan["status"]
        if plan_complete and plan["status"] in (1, 2):
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            templates.append(
                _envelope(
                    0, 0, _PLAN_STATUS_EVENT, 2,
                    (
                        _update(
                            "plans",
                            plan["id"],
                            {"status": plan["status"]},
                            {"status": _PLAN_COMPLETE},
                        ),
                    ),
                    command.occurred_at,
                )
            )
            plan_status = _PLAN_COMPLETE

        allocation = scope.allocate(len(templates))
        events = tuple(
            replace(
                template,
                event_id=allocation.first_event_id + index,
                transaction_id=allocation.txn_id,
            )
            for index, template in enumerate(templates)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=CaptureResult(
                action_status=_ACTION_SUCCEEDED,
                plan_status=plan_status,
                output_ids=tuple(output_ids),
            ),
        )

    def _sibling_actions(self, connection, action) -> dict[int, dict[str, Any]]:
        rows = {}
        for row in connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?", (action["plan_id"],)
        ).fetchall():
            facts = row_facts(connection, "actions", int(row[0]))
            if facts is not None:
                rows[int(row[0])] = facts
        return rows


class CaptureRepository:
    """采集完成终态事务的 SQLite 仓储。"""

    def save_emergency(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        owned: OwnedConnection,
        timeout_s=None,
        retry_interval_s=None,
    ) -> DbOutcome[EmergencySave]:
        command = SaveEmergencyCommand(
            session_key=session_key,
            action_id=action_id,
            activity_id=activity_id,
            record=record,
            attempts=attempts,
            occurred_at=occurred_at,
            key=key,
            timeout_s=timeout_s,
            retry_interval_s=retry_interval_s,
        )
        receipt = commit_operation(command, key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_capture(
        self, command: FinishCapture, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CaptureResult]:
        receipt = commit_operation(FinishCaptureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


# -- 应急停止最终补记 -------------------------------------------------


@dataclass(frozen=True)
class EmergencySave:
    """应急补记的保存结果：持久化状态与原流程事实。"""

    record_status: RecordStatus
    run_id: int
    attempts_saved: int


class SaveEmergencyCommand:
    """一次应急停止最终补记的完整事务命令。

    一个目标的最终流程（kind=9）与全部实际尝试、适用设备活动变
    化在一个事务共同创建；进行中补记、普通意图引用、超限与缺项
    均拒绝。
    """

    def __init__(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        timeout_s=None,
        retry_interval_s=None,
    ) -> None:
        self._session_key = session_key
        self._timeout_s = timeout_s
        self._retry_interval_s = retry_interval_s
        self._action_id = action_id
        self._activity_id = activity_id
        self._record = record
        self._attempts = attempts
        self._occurred_at = occurred_at
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("应急补记的重送须按原事务核实")
        action = row_facts(connection, "actions", self._action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {self._action_id}")
        activity = row_facts(connection, "device_activities", self._activity_id)
        if activity is None:
            raise TransactionError(f"设备活动不存在: {self._activity_id}")
        self._state["device_activities"] = {self._activity_id: activity}
        self._state["actions"] = {self._action_id: action}
        self._state.setdefault("operation_runs", {})
        self._state.setdefault("operation_attempts", {})

        record = self._record
        if len(self._attempts) != record.attempts_used:
            raise TransactionError(
                f"补记尝试行数与实际次数不符: {len(self._attempts)}"
                f" != {record.attempts_used}"
            )
        if record.attempts_used > record.max_attempts:
            raise TransactionError("实际次数超过本会话固定限额")
        if record.attempts_used > 0 and not self._complete_config():
            raise TransactionError("已有尝试的补记要求完整配置")

        responsibility_key = (
            f"emergency/{self._session_key}/{self._activity_id}"
        )
        existed = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (responsibility_key,),
        ).fetchone()
        if existed is not None:
            raise TransactionError("同一会话对同一活动只有一条应急流程")

        if record.outcome is EmergencyOutcome.STOPPED:
            run_status = 3
            run_error = None
            activity_after = 3
            activity_error = None
        elif record.outcome is EmergencyOutcome.UNCONFIRMED:
            if record.attempts_used == 0:
                raise TransactionError("停止未确认且有尝试的补记要求次数大于 0")
            run_status = 6
            run_error = {"code": "emergency_stop_unconfirmed", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error
        else:
            if record.attempts_used != 0:
                raise TransactionError("未能尝试的补记不创建尝试行")
            run_status = 4
            run_error = {"code": "emergency_not_attempted", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error

        allocation = scope.allocate(1)
        first_id = allocation.first_event_id
        run_id = _next_id(connection, "operation_runs")
        owner = ("action", action["id"])
        self._owners[("operation_runs", run_id)] = owner

        run_values = {
            "action_id": self._action_id,
            "delivery_id": None,
            "kind": 9,
            "query_purpose": None,
            "responsibility_key": responsibility_key,
            "activity_id": self._activity_id,
            "copy_id": None,
            "cleanup_item_id": None,
            "session_key": self._session_key,
            "status": run_status,
            "attempts_used": record.attempts_used,
            "max_attempts_used": record.max_attempts,
            "timeout_s_json": self._timeout_s,
            "retry_interval_s_json": self._retry_interval_s,
            "retry_wait_required": 0,
            "error_json": run_error,
        }
        rows = [_row("operation_runs", run_id, run_values)]
        attempt_ids: list[int] = []
        next_attempt_id = _next_id(connection, "operation_attempts")
        for index, attempt in enumerate(self._attempts):
            attempt_id = next_attempt_id + index
            attempt_ids.append(attempt_id)
            self._owners[("operation_attempts", attempt_id)] = owner
            values = dict(attempt)
            values.update(
                {
                    "run_id": run_id,
                    "attempt_no": index + 1,
                    "copy_round": None,
                    "intent_event_id": None,
                    "result_event_id": first_id,
                    "max_attempts_used": record.max_attempts,
                    "timeout_s_json": self._timeout_s,
                    "retry_interval_s_json": self._retry_interval_s,
                }
            )
            rows.append(_row("operation_attempts", attempt_id, values))
        before = {"activity_state": activity["activity_state"],
                  "last_error_json": activity["last_error_json"]}
        after = {"activity_state": activity_after, "last_error_json": activity_error}
        if not json_equal(before, after):
            self._owners[("device_activities", self._activity_id)] = owner
            rows.append(_update("device_activities", self._activity_id, before, after))
        event = _envelope(
            first_id,
            allocation.txn_id,
            _EMERGENCY_RECORDED_EVENT,
            1,
            tuple(rows),
            self._occurred_at,
            evidence={"session_key": self._session_key},
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=EmergencySave(
                record_status=RecordStatus.RECORDED,
                run_id=run_id,
                attempts_saved=record.attempts_used,
            ),
        )

    def _complete_config(self) -> bool:
        return self._timeout_s is not None and self._retry_interval_s is not None


def _emergency_guard(event, context) -> None:
    """应急补记的组合守卫：责任键、尝试归属、次数与结果组合。"""
    session_key = event.evidence.get("session_key")
    if not isinstance(session_key, str) or len(session_key) != 32:
        raise EventValidationError("应急补记必须携带本会话身份")
    run_values = None
    run_id = None
    attempts: list[dict] = []
    for row in event.rows:
        if row.table == "operation_runs" and not row.before.exists:
            run_values = row.after.values
            run_id = row.row_id
        elif row.table == "operation_attempts" and not row.before.exists:
            attempts.append(row.after.values)
    if run_values is None:
        raise EventValidationError("应急补记缺少最终流程行")
    expected_key = (
        f"emergency/{session_key}/{run_values.get('activity_id')}"
    )
    if run_values.get("responsibility_key") != expected_key:
        raise EventValidationError("应急责任键与会话及活动不符")
    if run_values.get("kind") != 9 or run_values.get("retry_wait_required") != 0:
        raise EventValidationError("应急流程必须是补记终态")
    status = run_values.get("status")
    if status not in (3, 4, 6):
        raise EventValidationError("应急补记不保存进行中状态")
    if status == 3 and run_values.get("error_json") is not None:
        raise EventValidationError("应急成功不携带流程错误")
    if status in (4, 6) and run_values.get("error_json") is None:
        raise EventValidationError("应急失败或未确认必须携带原因")
    if status == 6 and not attempts:
        raise EventValidationError("停止未确认的补记必须有实际尝试")
    if status == 4 and attempts:
        raise EventValidationError("未能尝试的补记不创建尝试行")
    numbers = [a.get("attempt_no") for a in attempts]
    if numbers != list(range(1, len(attempts) + 1)):
        raise EventValidationError("应急尝试必须从 1 连续编号")
    if run_values.get("attempts_used") != len(attempts):
        raise EventValidationError("累计次数与尝试行数不符")
    maximum = run_values.get("max_attempts_used")
    if maximum is not None and len(attempts) > maximum:
        raise EventValidationError("尝试次数超过本会话固定限额")
    for attempt in attempts:
        if attempt.get("run_id") != run_id:
            raise EventValidationError("应急尝试必须归属本次流程")
        if attempt.get("intent_event_id") is not None:
            raise EventValidationError("应急尝试不携带普通意图引用")
        if attempt.get("result_event_id") != event.event_id:
            raise EventValidationError("应急尝试结果必须指向本事件")
        if attempt.get("status") == 1:
            raise EventValidationError("应急补记不保存运行中尝试")
        if attempt.get("copy_round") is not None:
            raise EventValidationError("应急尝试没有拷贝轮次")


def _activity_guard(event, context) -> None:
    """活动状态守卫：结束不由路径、超时或本地退出补造。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        before_state = row.before.values.get("activity_state")
        after_state = row.after.values.get("activity_state")
        if after_state == 3 and before_state != 3:
            # 活动结束必须由本事务的可靠停止事实承载；同事件创建的
            # 最终流程行即为该事实。
            stopped = any(
                row.table == "operation_runs"
                and not row.before.exists
                and row.after.values.get("status") == 3
                for row in event.rows
            )
            runs = context.state_rows.get("operation_runs", {})
            stopped = stopped or any(
                values.get("status") == 3 for values in runs.values()
            )
            if not stopped:
                raise EventValidationError("活动结束缺少可靠停止事实")


def _release_guard(event, context) -> None:
    """占用释放守卫：ENDED 仍可保持 HELD，释放要求活动已结束。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        if (
            row.after.values.get("occupancy_state") == 2
            and row.before.values.get("occupancy_state") == 1
        ):
            facts = dict(
                context.state_rows.get("device_activities", {})
                .get(row.row_id, {})
            )
            facts.update(row.after.values)
            if facts.get("activity_state") != 3:
                raise EventValidationError("占用释放要求活动已经结束")
