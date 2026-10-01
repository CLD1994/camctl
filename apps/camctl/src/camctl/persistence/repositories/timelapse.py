"""延时摄影等待安排的持久化事务。

发送成功后的等待安排按 CAPTURE_WAIT_CHANGED 事件保存：首次安排
（SCHEDULE）保存驱动必要余量、本次额外等待与预计检查时间；重启
后配置变化按 RECONFIGURE 保存新安排，不覆盖已保存的等待完成事
实。额外等待毫秒由调用方从本次配置显式提供，不从计划推导。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from camctl.capture.timelapse import WaitPlan
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import OperationKey
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.models import DbOutcome, DbOutcomeKind
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
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("等待安排的重送须由调用方按原事务核实")
        command = self._command
        activity = row_facts(connection, "device_activities", command.action_id)
        if activity is None:
            raise TransactionError(f"设备活动不存在: {command.action_id}")
        self._state["device_activities"] = {command.action_id: activity}
        self._owners[("device_activities", command.action_id)] = (
            "action", activity["action_id"],
        )
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
            (_update("device_activities", command.action_id, before, after),),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=WaitSaved(expected_check_at=command.plan.check_at_utc),
        )


class TimelapseRepository:
    """延时等待安排事务的 SQLite 仓储。"""

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


def _outcome_of(receipt) -> DbOutcome[WaitSaved]:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _capture_wait_guard(event, context) -> None:
    """等待安排的数值与事实守卫。

    预计检查时间必须为正的墙钟微秒；余量与本次额外等待为非负毫
    秒；重算不覆盖已保存的等待完成事实由行规格 before 核对。
    """
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        _validate_wait_values(row.after.values)


def _validate_wait_values(values) -> None:
    check = values.get("expected_check_at")
    if check is not None and (isinstance(check, bool) or not isinstance(check, int) or check <= 0):
        raise EventValidationError(f"预计检查时间必须为正微秒: {check!r}")
    for column in ("result_wait_margin_ms", "extra_wait_ms_used"):
        if column in values:
            value = values[column]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise EventValidationError(f"{column} 必须是非负毫秒: {value!r}")


def register_timelapse_guards() -> None:
    """注册等待安排事件的正式业务守卫（装配期调用）。"""
    register_guard("capture_wait", _capture_wait_guard)
