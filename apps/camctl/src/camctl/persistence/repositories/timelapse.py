"""延时摄影等待的持久化事务。

发送成功后的等待安排按 CAPTURE_WAIT_CHANGED 事件保存：首次安排
（SCHEDULE）保存驱动必要余量、本次额外等待与预计检查时间；重启
后配置变化按 RECONFIGURE 保存新安排，不覆盖已保存的等待完成事
实。额外等待毫秒由调用方从本次配置显式提供，不从计划推导。等待
到期后按完成分支保存一次引用自身事件的完成事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from camctl.capture.models import WaitCompletedSave, validate_capture_spec
from camctl.contracts.enums import enum_for
from camctl.devices.tasks import CompletionMode, EndControl, StartReturn
from camctl.capture.timelapse import WaitPlan
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import MAX_OBJECT_ID, ConsistencyError, OperationKey
from camctl.devices.capture_timing import CaptureTiming
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import load_activity_of_action
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    row_facts,
    saved_transaction_events,
    update_change as _update,
)

_CAPTURE_WAIT_EVENT = 15

_SCHEDULE_REASON = 1
_RECONFIGURE_REASON = 2
_WAIT_COMPLETED_REASON = 3


@dataclass(frozen=True)
class ScheduleWait:
    """一次等待安排保存的输入。

    extra_wait_ms 是本次运行实际采用的部署额外等待；driver_margin_ms
    是首次固定的驱动必要余量，仅在首次安排时保存。
    """

    action_id: int
    plan: WaitPlan
    driver_margin_ms: int
    extra_wait_ms: int
    occurred_at: int


@dataclass(frozen=True)
class WaitSaved:
    """等待安排保存结果。"""

    expected_check_at: int | None


@dataclass(frozen=True)
class WaitCompleted:
    """等待完成保存结果：引用保存该事实的事件。"""

    event_id: int


class WaitCompletedCommand:
    """保存等待完成事实的事务命令（CAPTURE_WAIT_CHANGED.COMPLETE）。

    要求发送事实与预计检查时间已保存且完成事实尚不存在；引用的
    事件就是本命令创建的事件本身，保存一次后不再改写。
    """

    def __init__(self, command: WaitCompletedSave, key: OperationKey) -> None:
        if not isinstance(command, WaitCompletedSave):
            raise TypeError("等待完成申请必须使用 WaitCompletedSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        command = self._command
        activity = load_activity_of_action(connection, command.action_id)
        self._state["device_activities"] = {activity["id"]: activity}
        action = row_facts(connection, "actions", activity["action_id"])
        if action is None:
            raise ConsistencyError("等待缺少原冻结动作定义")
        self._state["actions"] = {action["id"]: action}
        _load_wait_start(connection, activity, self._state)
        self._owners[("device_activities", activity["id"])] = (
            "action", activity["action_id"])
        if activity["sent_at"] is None or activity["expected_check_at"] is None:
            raise ConsistencyError("等待完成要求已保存发送事实与预计检查时间")
        if activity["wait_completed_event_id"] is not None:
            raise ConsistencyError("等待完成事实已保存，不因新安排改写")
        allocation = scope.allocate(1)
        row = _update(
            "device_activities", activity["id"],
            {"wait_completed_event_id": None},
            {"wait_completed_event_id": allocation.first_event_id},
        )
        event = _envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _CAPTURE_WAIT_EVENT,
            _WAIT_COMPLETED_REASON,
            (row,),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=WaitCompleted(event_id=allocation.first_event_id),
        )

    def _reuse(self, scope, saved) -> CommandPlan:
        """原键重送：核实原完成分支与输入后恢复首次响应。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_CAPTURE_WAIT_EVENT, _WAIT_COMPLETED_REASON)]:
            raise TransactionError("操作身份已用于其他事务，不能作为等待完成重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("等待完成的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "device_activities":
            raise TransactionError("原等待完成属于其他活动")
        event_id = row["after"]["values"]["wait_completed_event_id"]
        activity = load_activity_of_action(scope.connection, command.action_id)
        if row["id"] != activity["id"]:
            raise TransactionError("原等待完成属于其他活动")
        if activity["wait_completed_event_id"] != event_id:
            raise TransactionError("原等待完成的可靠记录与输入不符")
        action = row_facts(scope.connection, "actions", command.action_id)
        _load_wait_start(scope.connection, activity, self._state)
        _verify_wait_basis(activity, action, self._state,
            occurred_at=command.occurred_at, complete=True)
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=WaitCompleted(event_id=event_id),
        )


class ScheduleWaitCommand:
    """保存或重算等待安排的完整事务命令。"""

    def __init__(self, command: ScheduleWait, key: OperationKey, reason: int) -> None:
        self._command = command
        self._key = key
        self._reason = reason
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        _validate_wait_values({"expected_check_at": command.plan.check_at_utc,
            "result_wait_margin_ms": command.driver_margin_ms, "extra_wait_ms_used": command.extra_wait_ms})
        if command.plan.check_at_utc is None:
            raise TransactionError("等待安排要求完整预计检查时间")
        activity = load_activity_of_action(connection, command.action_id)
        self._state["device_activities"] = {activity["id"]: activity}
        action = row_facts(connection, "actions", activity["action_id"])
        if action is None:
            raise ConsistencyError("等待缺少原冻结动作定义")
        self._state["actions"] = {action["id"]: action}
        _load_wait_start(connection, activity, self._state)
        self._owners[("device_activities", activity["id"])] = (
            "action", activity["action_id"],
        )
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(activity, saved)
        if self._reason == _SCHEDULE_REASON:
            if activity["expected_check_at"] is not None:
                raise TransactionError("首次等待安排要求原预计检查为空")
            before = {
                "result_wait_margin_ms": activity["result_wait_margin_ms"],
                "extra_wait_ms_used": activity["extra_wait_ms_used"],
                "expected_check_at": activity["expected_check_at"],
            }
            after = {
                "result_wait_margin_ms": command.driver_margin_ms,
                "extra_wait_ms_used": command.extra_wait_ms,
                "expected_check_at": command.plan.check_at_utc,
            }
        else:
            if activity["wait_completed_event_id"] is not None:
                raise TransactionError("等待完成事实不被配置变化重写")
            before = {
                "extra_wait_ms_used": activity["extra_wait_ms_used"],
                "expected_check_at": activity["expected_check_at"],
            }
            after = {
                "extra_wait_ms_used": command.extra_wait_ms,
                "expected_check_at": command.plan.check_at_utc,
            }
        _validate_wait_values(after)
        if self._reason == _RECONFIGURE_REASON and json_equal(before, after):
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                               read_only=True,
                               result=WaitSaved(expected_check_at=activity["expected_check_at"]))
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _CAPTURE_WAIT_EVENT,
            self._reason,
            (_update("device_activities", activity["id"], before, after),),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=WaitSaved(expected_check_at=command.plan.check_at_utc),
        )


    def _reuse(self, activity, saved) -> CommandPlan:
        """同一原键只复用完整持久输入；不要求当前投影仍是原安排。"""
        command = self._command
        expected_after = {"extra_wait_ms_used": command.extra_wait_ms,
                          "expected_check_at": command.plan.check_at_utc}
        if self._reason == _SCHEDULE_REASON:
            expected_after["result_wait_margin_ms"] = command.driver_margin_ms
        elif not json_equal(activity["result_wait_margin_ms"], command.driver_margin_ms):
            raise TransactionError("等待重配置重送改变原固定结果余量")
        if (len(saved) != 1 or saved[0]["type"] != _CAPTURE_WAIT_EVENT
                or saved[0]["reason"] != self._reason
                or saved[0]["occurred_at"] != command.occurred_at):
            raise TransactionError("等待安排重送改变原分支或事实时刻")
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "device_activities"
                or rows[0]["id"] != activity["id"]
                or not rows[0]["before"]["exists"]
                or not rows[0]["after"]["exists"]
                or set(rows[0]["before"]["values"]) != set(expected_after)
                or not json_equal(rows[0]["after"]["values"], expected_after)):
            raise TransactionError("等待安排重送改变原活动或完整等待规则")
        original_activity = {**activity, **expected_after}
        action = self._state["actions"][activity["action_id"]]
        _verify_wait_basis(original_activity, action, self._state,
            occurred_at=command.occurred_at, complete=False)
        return CommandPlan((), self._owners, self._state, read_only=True,
                           result=WaitSaved(command.plan.check_at_utc))


class _VerifyWaitCommand:
    """在可靠事务内核对原键；缺键只返回尚未提交，不发布候选安排。"""

    def __init__(self, original, key):
        self.original, self.key = original, key

    def plan(self, scope):
        if saved_transaction_events(scope.connection, self.key) is None:
            return CommandPlan((), {}, {}, read_only=True, result=None)
        return self.original.plan(scope)


class TimelapseRepository:
    """延时等待安排事务的 SQLite 仓储。"""

    def verify_wait(self, command: ScheduleWait | WaitCompletedSave, key: OperationKey,
                    owned: OwnedConnection, *, reconfigure: bool = False):
        """UNKNOWN 先核对原完整申请，不用当前投影推断提交结果。"""
        original = (WaitCompletedCommand(command, key) if isinstance(command, WaitCompletedSave)
                    else ScheduleWaitCommand(command, key, _RECONFIGURE_REASON if reconfigure else _SCHEDULE_REASON))
        return _outcome_of(commit_operation(_VerifyWaitCommand(original, key), key, owned))

    def schedule_wait(
        self, command: ScheduleWait, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[WaitSaved]:
        receipt = commit_operation(
            ScheduleWaitCommand(command, key, _SCHEDULE_REASON), key, owned
        )
        return _outcome_of(receipt)

    def reconfigure_wait(
        self, command: ScheduleWait, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[WaitSaved]:
        receipt = commit_operation(
            ScheduleWaitCommand(command, key, _RECONFIGURE_REASON), key, owned
        )
        return _outcome_of(receipt)

    def complete_wait(
        self, command: WaitCompletedSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[WaitCompleted]:
        receipt = commit_operation(
            WaitCompletedCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _outcome_of(receipt) -> DbOutcome[WaitSaved]:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _load_wait_start(connection, activity, state):
    """有界装载原可靠 START，守卫与历史读取使用同一来源合同。"""
    from .capture import _history_source_row

    found = connection.execute(
        "SELECT r.id,a.id FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=?"
        " ORDER BY a.attempt_no DESC LIMIT 1",
        (f"start/{activity['action_id']}", activity["action_id"], activity["id"])).fetchone()
    if found is None:
        raise ConsistencyError("自然等待缺少原 START 结果")
    run = row_facts(connection, "operation_runs", found[0])
    attempt = row_facts(connection, "operation_attempts", found[1])
    state.setdefault("operation_runs", {})[run["id"]] = run
    state.setdefault("operation_attempts", {})[attempt["id"]] = attempt
    if attempt["result_event_id"] is None:
        raise ConsistencyError("自然等待缺少可靠原发送事件")
    state.setdefault("history_events", {})[attempt["result_event_id"]] = _history_source_row(
        connection, attempt["result_event_id"])


def _verify_wait_basis(activity, action, state, *, occurred_at, complete, event_id=None):
    """新旧自然定义统一核对真实 START、完整公式和完成真实时刻。"""
    from .capture import _ATTEMPT_RESULT_EVENT, _history_source_event

    try:
        _validate_wait_values(activity)
        if action is None or action["type"] != int(enum_for("actions.type").CAMERA_TIMELAPSE):
            raise ValueError("自然等待不是延时任务")
        spec = validate_capture_spec("camera_timelapse", action["execution_spec_json"])
        if (not spec["duration_based"] or not spec["wait_after_send"]
                or spec["end_control"] != int(EndControl.DEVICE)
                or spec["start_return_meaning"] != int(StartReturn.SENT)
                or spec["completion_mode"] != int(CompletionMode.TIME_AND_OUTPUTS)
                or activity["action_id"] != action["id"]):
            raise ValueError("自然等待缺少原有限自然任务合同")
        runs = [run for run in state.get("operation_runs", {}).values()
                if run["responsibility_key"] == f"start/{action['id']}"
                and run["action_id"] == action["id"] and run["activity_id"] == activity["id"]]
        if len(runs) != 1 or runs[0]["kind"] != int(enum_for("operation_runs.kind").START):
            raise ValueError("自然等待 START 与原活动不符")
        run = runs[0]
        attempts = [attempt for attempt in state.get("operation_attempts", {}).values()
                    if attempt["run_id"] == run["id"]]
        attempt = max(attempts, key=lambda value: value["attempt_no"])
        source = _history_source_event(state, attempt["result_event_id"])
        rows = [row for row in source.rows if row.table == "operation_attempts" and row.row_id == attempt["id"]]
        if (attempt["status"] != int(enum_for("operation_attempts.status").SUCCEEDED)
                or attempt["effect_state"] != int(enum_for("operation_attempts.effect_state").CONFIRMED)
                or source.event_type != _ATTEMPT_RESULT_EVENT or len(rows) != 1
                or source.occurred_at != activity["sent_at"]
                or (event_id is not None and source.event_id >= event_id)
                or any(not json_equal(rows[0].after.values.get(name), attempt[name])
                       for name in ("status", "effect_state", "result_json", "error_json"))):
            raise ValueError("自然等待不匹配原可靠发送")
        expected = CaptureTiming(spec["target_duration_ms"], spec.get("startup_margin_ms", 0),
            spec["result_wait_margin_ms"], activity["extra_wait_ms_used"]).deadline_us(activity["sent_at"])
        if (activity["result_wait_margin_ms"] != spec["result_wait_margin_ms"]
                or activity["expected_check_at"] != expected):
            raise ValueError("自然等待改变原启动余量、目标或发送锚点")
        if complete and occurred_at < expected:
            raise ValueError("自然等待实际完成时刻早于原预计时点")
    except (KeyError, TypeError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


def _capture_wait_guard(event, context) -> None:
    """三个分支共同验证合法自然任务、原来源、完整公式与完成资格。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        _validate_wait_values(row.after.values)
        activity = dict(context.state_rows.get("device_activities", {}).get(row.row_id, {}))
        activity.update(row.after.values)
        action = context.state_rows.get("actions", {}).get(activity.get("action_id"))
        _verify_wait_basis(activity, action, context.state_rows,
            occurred_at=event.occurred_at, complete=event.reason == _WAIT_COMPLETED_REASON,
            event_id=event.event_id)


def _validate_wait_values(values) -> None:
    check = values.get("expected_check_at")
    if check is not None and (isinstance(check, bool) or not isinstance(check, int) or not 0 < check <= MAX_OBJECT_ID):
        raise EventValidationError(f"预计检查时间必须为正微秒: {check!r}")
    for column in ("result_wait_margin_ms", "extra_wait_ms_used"):
        if column in values:
            value = values[column]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OBJECT_ID
            ):
                raise EventValidationError(f"{column} 必须是非负毫秒: {value!r}")


def register_timelapse_guards() -> None:
    """注册等待安排事件的正式业务守卫（装配期调用）。"""
    register_guard("capture_wait", _capture_wait_guard)
