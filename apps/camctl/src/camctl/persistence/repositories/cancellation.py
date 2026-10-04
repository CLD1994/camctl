"""取消目标固定与解析失败的持久化事务。

完整目标集合经自包含检查后在一个事务内创建全部 `cancel_items` 行
（TARGETS_FIXED.CANCEL），保存每个目标的直接或联动依据及初始取消
效果；目标解析失败（包含自身或可靠不存在）以登记错误结束取消动作
（TARGETS_FIXED.FAIL），不创建任何成员。原键重送核实后只读恢复。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelActionDisposition,
    CancelActionFinished,
    CancelApplyMode,
    CancelOutcomeChoice,
    CancelTargetError,
    CancelTargetsDisposition,
    CancelTargetsSaved,
    CancellationEffect,
    FailCancelTargets,
    FinishCancelAction,
    FixCancelTargets,
    RecordCancelResult,
    SelectionBasis,
)
from camctl.cancellation.targets import CancelLookup, CancelLookupError
from camctl.contracts.enums import enum_for
from camctl.contracts.values import (
    ConsistencyError,
    ObjectId,
    OperationKey,
    parse_object_id,
)
from camctl.contracts.workflow_errors import (
    item_error_id,
    registered_error,
    validate_error_details,
)
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id,
    row_change as _row,
    row_facts,
    saved_transaction_events,
    update_change as _update,
)

__all__ = [
    "CancellationRepository",
    "CancelTargetsDisposition",
    "SqliteCancelLookup",
    "sqlite_auto_candidates",
]

_CANCEL_ACTION_TYPE = 6
_TARGETS_FIXED_EVENT = 4
_TARGETS_CANCEL_REASON = 3
_TARGETS_FAIL_REASON = 4

_ACTION_STATUS = enum_for("actions.status")
_TARGET_PENDING, _TARGET_FIXED, _TARGET_FAILED = 1, 2, 3
#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

#: 目标解析失败仅有的登记错误。
_TARGET_ERROR_CODES = ("cancel_self_target", "cancel_target_not_found")

_CANCEL_CHANGED_EVENT = 25
_ACTION_FINISHED_EVENT = 8
_PLAN_STATUS_EVENT = 9
_PLAN_COMPLETE = 3
_CANCEL_APPLY_REASON, _CANCEL_RESULT_REASON, _CANCEL_STOP_WAIT_REASON = 1, 2, 3
#: cancel_items.status 的登记编号。
_ITEM_PENDING, _ITEM_RUNNING, _ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED =     1, 2, 3, 4, 5
_CANCEL_ITEMS_FAILED = "cancel_items_failed"
#: 动作状态：PENDING=1、RUNNING=2、SUCCEEDED=3、FAILED=4、CANCELED=6。
_ACTION_SUCCEEDED, _ACTION_FAILED, _ACTION_CANCELED = 3, 4, 6


@dataclass(frozen=True)
class _ItemValues:
    target_action_id: int
    selection_basis: int
    cancellation_effect: int


class _FixCancelTargetsCommand:
    """固定取消动作的完整目标集合（TARGETS_FIXED.CANCEL）。

    全部成员在一个事务内创建：中断或重送不产生部分取消。自身包含
    检查由调用方（prepare_cancel_set）完成，命令拒绝包含自身的输入。
    """

    def __init__(self, command: FixCancelTargets, key: OperationKey) -> None:
        if not isinstance(command, FixCancelTargets):
            raise TypeError("取消目标固定申请必须使用 FixCancelTargets")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise ConsistencyError(f"取消动作不存在: {command.action_id}")
        if action["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("取消目标固定要求取消动作")
        if action["target_selection_state"] == _TARGET_FAILED:
            raise TransactionError("目标集合已失败，不重新固定")
        if action["target_selection_state"] == _TARGET_FIXED:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelTargetsSaved(
                    CancelTargetsDisposition.ALREADY,
                    _saved_item_targets(connection, command.action_id)))
        if action["status"] != int(_ACTION_STATUS.RUNNING) \
                or action["cancel_requested"]:
            raise TransactionError("目标固定要求取消动作执行中且未取消")
        items = command.targets.targets
        if not items:
            raise TransactionError("取消目标集合为空，按目标解析失败处理")
        self._state["actions"] = {command.action_id: dict(action)}
        self._state["cancel_items"] = {}
        next_item = next_row_id(connection, "cancel_items")
        rows = (_update(
            "actions", command.action_id,
            {"target_selection_state": _TARGET_PENDING},
            {"target_selection_state": _TARGET_FIXED}),)
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        linked_obtains: list[int] = []
        seen: set[int] = set()
        for target in items:
            if target.action_id == command.action_id:
                raise ConsistencyError(
                    "固定集合包含取消动作自身，自包含检查缺失")
            if target.action_id in seen:
                raise ConsistencyError(
                    f"固定集合目标重复: {target.action_id}")
            seen.add(target.action_id)
            if target.basis is not SelectionBasis.DIRECT:
                linked_obtains.append(target.action_id)
            values = {
                "action_id": command.action_id,
                "target_action_id": target.action_id,
                "selection_basis": target.basis.value,
                "status": 1,
                "cancellation_effect": target.cancellation_effect.value,
                "outcome": None,
                "error_code": None,
                "error_details_json": None,
            }
            self._owners[("cancel_items", next_item)] = (
                "action", command.action_id)
            self._state["cancel_items"][next_item] = dict(values, id=next_item)
            rows += (_row("cancel_items", next_item, values),)
            next_item += 1
        self._load_links(connection, linked_obtains)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _TARGETS_FIXED_EVENT, _TARGETS_CANCEL_REASON, rows,
            command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(
                CancelTargetsDisposition.SAVED, tuple(sorted(seen))))

    def _load_links(self, connection, obtain_ids: list[int]) -> None:
        """装载联动依据：AUTO_PREVIEW/BOTH 成员的有效自动关联。"""
        if not obtain_ids:
            return
        links = self._state.setdefault("auto_preview_links", {})
        with closing(connection.execute(
            "SELECT id FROM auto_preview_links WHERE is_valid = 1"
            " AND obtain_action_id IN (%s) ORDER BY id"
            % ",".join("?" * len(obtain_ids)), obtain_ids,
        )) as cursor:
            ids = tuple(int(row[0]) for row in cursor.fetchall())
        for link_id in ids:
            link = row_facts(connection, "auto_preview_links", link_id)
            if link is not None:
                links[link_id] = link

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实固定分支与成员集合后恢复首次响应。"""
        command = self._command
        if not saved or (saved[0]["type"], saved[0]["reason"]) != (
                _TARGETS_FIXED_EVENT, _TARGETS_CANCEL_REASON):
            raise TransactionError("原事务不是取消目标固定，不能作为重送核实")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("目标固定的事实时刻与原事务不同")
        created = {
            row["after"]["values"]["target_action_id"]
            for event in saved
            for row in event["body"]["rows"]
            if row["table"] == "cancel_items"}
        if created != {target.action_id for target in command.targets.targets}:
            raise TransactionError("目标固定的成员集合与重送输入不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(
                CancelTargetsDisposition.ALREADY,
                _saved_item_targets(connection, command.action_id)))


class _FailCancelTargetsCommand:
    """保存目标集合解析失败（TARGETS_FIXED.FAIL）。

    包含自身或目标可靠不存在时，取消动作以登记错误结束，不创建任
    何取消成员，也不向目标施加取消。
    """

    def __init__(self, command: FailCancelTargets, key: OperationKey) -> None:
        if not isinstance(command, FailCancelTargets):
            raise TypeError("目标解析失败申请必须使用 FailCancelTargets")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise ConsistencyError(f"取消动作不存在: {command.action_id}")
        if action["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("目标解析失败要求取消动作")
        self._state["actions"] = {command.action_id: dict(action)}
        if action["status"] in _ACTION_TERMINAL:
            return self._recover(action)
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError(
                f"取消动作不在执行中: {command.action_id}"
                f" status={action['status']}")
        if action["cancel_requested"]:
            raise TransactionError(
                f"取消请求已生效的取消动作不保存目标解析失败:"
                f" {command.action_id}")
        if action["target_selection_state"] != _TARGET_PENDING:
            raise TransactionError("目标集合已固定，不再按解析失败结束")
        error_code = registered_error(command.error.code)["action_error_id"]
        validate_error_details(command.error.code, command.error.details)
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        allocation = scope.allocate(1)
        fail = _update(
            "actions", command.action_id,
            {"target_selection_state": _TARGET_PENDING,
             "status": action["status"],
             "error_code": None, "error_details_json": None},
            {"target_selection_state": _TARGET_FAILED,
             "status": int(_ACTION_STATUS.FAILED),
             "error_code": error_code,
             "error_details_json": dict(command.error.details)})
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _TARGETS_FIXED_EVENT, _TARGETS_FAIL_REASON, (fail,),
            command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(CancelTargetsDisposition.SAVED, ()))

    def _recover(self, action) -> CommandPlan:
        """终态后的新键：解析失败终态按原错误只读恢复。"""
        if (action["status"] == int(_ACTION_STATUS.FAILED)
                and action["error_code"]
                == registered_error(self._command.error.code)[
                    "action_error_id"]):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelTargetsSaved(
                    CancelTargetsDisposition.ALREADY, ()))
        raise TransactionError(
            f"取消动作终态与目标解析失败不符: {action['status']!r}")

    def _reuse(self, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds != [(_TARGETS_FIXED_EVENT, _TARGETS_FAIL_REASON)]:
            raise TransactionError("原事务不是目标解析失败，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("目标解析失败的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "actions" or row["id"] != self._command.action_id:
            raise TransactionError("原目标解析失败属于其他动作")
        if row["after"]["values"].get("error_code") != registered_error(
                self._command.error.code)["action_error_id"]:
            raise TransactionError("目标解析失败的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY, ()))


class _ApplyCancelTargetCommand:
    """保存一个目标的取消生效（CANCEL_CHANGED.APPLY）。

    可靠未启动的目标同事务终态取消并保存成功结果；已启动且支持停
    止的只保存取消标记，停止收场另行推进；终态目标不改写，按既有
    终态保存成功；目标取消已生效时只保存本项效果，复用原责任。
    """

    _MODE_EVENTS = {
        CancelApplyMode.PRE_START: 2,
        CancelApplyMode.WITH_STOP: 1,
        CancelApplyMode.TERMINAL: 2,
        CancelApplyMode.ALREADY: 1,
    }

    def __init__(self, command: ApplyCancelTarget, key: OperationKey) -> None:
        if not isinstance(command, ApplyCancelTarget):
            raise TypeError("取消生效申请必须使用 ApplyCancelTarget")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        item, target = self._load(connection)
        if item["status"] in (_ITEM_SUCCEEDED, _ITEM_FAILED):
            return self._already()
        if item["status"] == _ITEM_CANCELED:
            raise ConsistencyError("取消发起者收场的成员不经普通生效")
        if item["status"] != _ITEM_PENDING:
            return self._already()
        mode = command.mode
        target_id = item["target_action_id"]
        action_rows: tuple = ()
        outcome_event = None
        if mode is CancelApplyMode.PRE_START:
            if target["status"] != 1 or target["cancel_requested"]:
                raise ConsistencyError("未启动取消要求目标待执行且未取消")
            action_rows = (_update(
                "actions", target_id,
                {"cancel_requested": 0, "status": 1},
                {"cancel_requested": 1, "status": _ACTION_CANCELED}),)
            outcome_event = _ITEM_SUCCEEDED, CancelOutcomeChoice.CANCELED.value
            effect_after = CancellationEffect.APPLIED.value
        elif mode is CancelApplyMode.WITH_STOP:
            if target["status"] != 2 or target["cancel_requested"]:
                raise ConsistencyError("停止收场取消要求目标执行中且未取消")
            action_rows = (_update(
                "actions", target_id,
                {"cancel_requested": 0}, {"cancel_requested": 1}),)
            effect_after = CancellationEffect.APPLIED.value
        elif mode is CancelApplyMode.TERMINAL:
            if target["status"] not in _ACTION_TERMINAL:
                raise ConsistencyError("终态取消要求目标已终态")
            outcome_event = (_ITEM_SUCCEEDED,
                             CancelOutcomeChoice.ALREADY_TERMINAL.value)
            effect_after = CancellationEffect.NOT_REQUIRED.value
        elif mode is CancelApplyMode.ALREADY:
            if not target["cancel_requested"]:
                raise ConsistencyError("复用取消责任要求目标取消已生效")
            effect_after = CancellationEffect.APPLIED.value
        else:
            raise ConsistencyError(f"取消生效方式不可解释: {mode!r}")
        apply_row = _update(
            "cancel_items", command.item_id,
            {"status": _ITEM_PENDING,
             "cancellation_effect": CancellationEffect.NOT_APPLIED.value},
            {"status": _ITEM_RUNNING, "cancellation_effect": effect_after})
        self._claim(item, target_id)
        allocation = scope.allocate(self._MODE_EVENTS[mode])
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _CANCEL_CHANGED_EVENT, _CANCEL_APPLY_REASON,
            (apply_row,) + action_rows, command.occurred_at)]
        if outcome_event is not None:
            final_status, outcome_value = outcome_event
            result_row = _update(
                "cancel_items", command.item_id,
                {"status": _ITEM_RUNNING, "outcome": None,
                 "error_code": None, "error_details_json": None},
                {"status": final_status, "outcome": outcome_value,
                 "error_code": None, "error_details_json": None})
            events.append(_envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _CANCEL_CHANGED_EVENT, _CANCEL_RESULT_REASON,
                (result_row,), command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(CancelTargetsDisposition.SAVED,
                                      (command.item_id,)))

    def _load(self, connection):
        item = row_facts(connection, "cancel_items", self._command.item_id)
        if item is None:
            raise ConsistencyError(f"取消成员不存在: {self._command.item_id}")
        self._state.setdefault("cancel_items", {})[item["id"]] = dict(item)
        origin = row_facts(connection, "actions", item["action_id"])
        if origin is None:
            raise ConsistencyError(f"取消动作不存在: {item['action_id']}")
        self._state["actions"] = {origin["id"]: dict(origin)}
        if origin["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("取消生效要求取消动作")
        if origin["target_selection_state"] != _TARGET_FIXED:
            raise ConsistencyError("取消生效要求目标集合已固定")
        if origin["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError("取消生效要求取消动作执行中")
        target = row_facts(connection, "actions", item["target_action_id"])
        if target is None:
            raise ConsistencyError(
                f"取消目标不存在: {item['target_action_id']}")
        self._state["actions"][target["id"]] = dict(target)
        return item, target

    def _claim(self, item, target_id: int) -> None:
        self._owners[("cancel_items", item["id"])] = (
            "action", item["action_id"])
        # 目标动作行属于其自身历史；取消成员行属于取消动作。
        self._owners[("actions", target_id)] = ("action", target_id)

    def _already(self) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY,
                                      (self._command.item_id,)))

    def _reuse(self, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        if not kinds or kinds[0] != (_CANCEL_CHANGED_EVENT, _CANCEL_APPLY_REASON):
            raise TransactionError("原事务不是取消生效，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("取消生效的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if not rows or rows[0]["table"] != "cancel_items"                 or rows[0]["id"] != self._command.item_id:
            raise TransactionError("原取消生效属于其他成员")
        return self._already()


class _RecordCancelResultCommand:
    """保存一个取消项的最终结果（CANCEL_CHANGED.RESULT）。"""

    def __init__(self, command: RecordCancelResult, key: OperationKey) -> None:
        if not isinstance(command, RecordCancelResult):
            raise TypeError("取消项结果申请必须使用 RecordCancelResult")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        item, origin = self._load(connection)
        if item["status"] in (_ITEM_SUCCEEDED, _ITEM_FAILED):
            return self._already()
        if item["status"] == _ITEM_CANCELED:
            raise ConsistencyError("取消发起者收场的成员不改写结果")
        if item["status"] not in (_ITEM_PENDING, _ITEM_RUNNING):
            raise TransactionError(
                f"取消成员状态不可解释: {command.item_id} {item['status']!r}")
        before = {"status": item["status"], "outcome": None,
                  "error_code": None, "error_details_json": None}
        if command.outcome is not None:
            after = {"status": _ITEM_SUCCEEDED,
                     "outcome": command.outcome.value,
                     "error_code": None, "error_details_json": None}
        else:
            error_id = item_error_id("cancel_items", command.code)
            validate_error_details(command.code, command.details)
            after = {"status": _ITEM_FAILED, "outcome": None,
                     "error_code": error_id,
                     "error_details_json": dict(command.details)}
        allocation = scope.allocate(1)
        result_row = _update(
            "cancel_items", command.item_id, before, after)
        self._owners[("cancel_items", item["id"])] = (
            "action", item["action_id"])
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _CANCEL_CHANGED_EVENT, _CANCEL_RESULT_REASON, (result_row,),
            command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(CancelTargetsDisposition.SAVED,
                                      (command.item_id,)))

    def _load(self, connection):
        item = row_facts(connection, "cancel_items", self._command.item_id)
        if item is None:
            raise ConsistencyError(f"取消成员不存在: {self._command.item_id}")
        self._state.setdefault("cancel_items", {})[item["id"]] = dict(item)
        origin = row_facts(connection, "actions", item["action_id"])
        if origin is None:
            raise ConsistencyError(f"取消动作不存在: {item['action_id']}")
        self._state["actions"] = {origin["id"]: dict(origin)}
        if origin["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("取消项结果要求取消动作")
        if origin["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError("取消项结果要求取消动作执行中")
        return item, origin

    def _already(self) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY,
                                      (self._command.item_id,)))

    def _reuse(self, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds != [(_CANCEL_CHANGED_EVENT, _CANCEL_RESULT_REASON)]:
            raise TransactionError("原事务不是取消项结果，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("取消项结果的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "cancel_items" or row["id"] != self._command.item_id:
            raise TransactionError("原取消项结果属于其他成员")
        return self._already()


class CancellationRepository:
    """取消目标固定与解析失败事务的 SQLite 仓储。"""

    def fix_cancel_targets(
        self, command: FixCancelTargets, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _FixCancelTargetsCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def fail_cancel_targets(
        self, command: FailCancelTargets, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _FailCancelTargetsCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def apply_cancel_target(
        self, command: ApplyCancelTarget, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _ApplyCancelTargetCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def record_cancel_result(
        self, command: RecordCancelResult, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _RecordCancelResultCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def finish_cancel_action(
        self, command: FinishCancelAction, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelActionFinished]:
        receipt = commit_operation(
            _FinishCancelActionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


class _FinishCancelActionCommand:
    """取消动作汇总终态：全部成员终态后保存动作结果与父计划状态。

    任一成员最终失败按公共错误 cancel_items_failed 汇总失败；全部成
    功才成功。取消已生效的取消动作不保存普通终态（发起者收场归取
    消发起者语义处理）。
    """

    def __init__(self, command: FinishCancelAction, key: OperationKey) -> None:
        if not isinstance(command, FinishCancelAction):
            raise TypeError("取消汇总申请必须使用 FinishCancelAction")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "actions": {}, "cancel_items": {}, "plans": {},
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {command.action_id}")
        if action["type"] != _CANCEL_ACTION_TYPE:
            raise TransactionError(f"动作不是取消动作: {command.action_id}")
        self._state["actions"][command.action_id] = action
        if action["status"] in _ACTION_TERMINAL:
            return self._recover(connection, action)
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError(
                f"取消动作不在执行中: {command.action_id}"
                f" status={action['status']}")
        if action["cancel_requested"]:
            raise TransactionError(
                f"取消请求已生效的取消动作不能保存普通终态:"
                f" {command.action_id}")
        if action["target_selection_state"] != _TARGET_FIXED:
            raise TransactionError(
                f"取消动作目标集合尚未固定: {command.action_id}")
        items = self._load_items(connection)
        if not items:
            raise TransactionError(
                f"取消动作没有可汇总成员: {command.action_id}")
        for item in items.values():
            if item["status"] == _ITEM_CANCELED:
                raise ConsistencyError(
                    "取消发起者收场的成员不经普通汇总"
                    f" {command.action_id}/{item['id']}")
            if item["status"] not in (_ITEM_SUCCEEDED, _ITEM_FAILED):
                raise TransactionError(
                    f"取消成员尚未全部终态: {item['id']}"
                    f" status={item['status']}")
        failed = sum(1 for item in items.values()
                     if item["status"] == _ITEM_FAILED)
        if failed:
            action_status = _ACTION_FAILED
            reason = 2
            error_code = registered_error(_CANCEL_ITEMS_FAILED)["action_error_id"]
        else:
            action_status = _ACTION_SUCCEEDED
            reason = 1
            error_code = None
        before = {"status": action["status"]}
        after = {"status": action_status}
        if failed:
            before.update(error_code=action["error_code"],
                          error_details_json=action["error_details_json"])
            after.update(error_code=error_code, error_details_json={})
        siblings = self._load_siblings(connection, action)
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        templates = [(
            _ACTION_FINISHED_EVENT, reason,
            (_update("actions", command.action_id, before, after),),
        )]
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        plan_status = plan["status"]
        if self._plan_complete(siblings, command.action_id) \
                and plan["status"] in (1, 2):
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            templates.append((
                _PLAN_STATUS_EVENT, 2,
                (_update("plans", plan["id"],
                         {"status": plan["status"]},
                         {"status": _PLAN_COMPLETE}),),
            ))
            plan_status = _PLAN_COMPLETE
        allocation = scope.allocate(len(templates))
        events = tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, event_reason, rows, command.occurred_at,
            )
            for index, (event_type, event_reason, rows) in enumerate(templates)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=CancelActionFinished(
                disposition=CancelActionDisposition.SAVED,
                action_status=action_status, plan_status=plan_status,
                succeeded=len(items) - failed, failed=failed),
        )

    @staticmethod
    def _plan_complete(siblings, current_action_id: int) -> bool:
        return all(
            values.get("status") in _ACTION_TERMINAL
            or values["id"] == current_action_id
            for values in siblings.values())

    def _load_items(self, connection) -> dict[int, dict[str, Any]]:
        with closing(connection.execute(
            "SELECT id FROM cancel_items WHERE action_id = ? ORDER BY id",
            (self._command.action_id,),
        )) as cursor:
            ids = tuple(int(row[0]) for row in cursor.fetchall())
        items = {}
        for item_id in ids:
            item = row_facts(connection, "cancel_items", item_id)
            if item is not None:
                items[item_id] = item
                self._owners[("cancel_items", item_id)] = (
                    "action", self._command.action_id)
        self._state["cancel_items"] = dict(items)
        return items

    def _load_siblings(self, connection, action) -> dict[int, dict[str, Any]]:
        with closing(connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (action["plan_id"],),
        )) as cursor:
            siblings = {
                row[0]: row_facts(connection, "actions", row[0])
                for row in cursor}
        siblings = {key: value for key, value in siblings.items() if value}
        self._state["actions"].update(siblings)
        return siblings

    def _recover(self, connection, action) -> CommandPlan:
        """终态后的新键：按既有事实恢复结果，不重新登记。"""
        if action["status"] not in (_ACTION_SUCCEEDED, _ACTION_FAILED):
            raise TransactionError(
                f"取消动作终态不是汇总结果: {action['status']!r}")
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return self._result_from(
            action, plan["status"], CancelActionDisposition.ALREADY)

    def _result_from(self, action, plan_status, disposition) -> CommandPlan:
        items = self._state["cancel_items"]
        failed = sum(1 for item in items.values()
                     if item["status"] == _ITEM_FAILED)
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelActionFinished(
                disposition=disposition, action_status=action["status"],
                plan_status=plan_status, succeeded=len(items) - failed,
                failed=failed),
        )

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实事务身份后按既有事实恢复首次响应。"""
        kinds = [(event["type"], event["reason"]) for event in saved]
        if not kinds or kinds[0][0] != _ACTION_FINISHED_EVENT \
                or kinds[0][1] not in (1, 2) \
                or kinds[1:] not in ([], [(_PLAN_STATUS_EVENT, 2)]):
            raise TransactionError("原事务不是取消汇总登记，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("取消汇总的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" \
                or action_row["id"] != self._command.action_id:
            raise TransactionError("原取消汇总属于其他动作")
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return self._result_from(
            action, plan["status"], CancelActionDisposition.ALREADY)


def _outcome_of(receipt) -> DbOutcome[CancelTargetsSaved]:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _saved_item_targets(connection, action_id: int) -> tuple[int, ...]:
    with closing(connection.execute(
        "SELECT target_action_id FROM cancel_items WHERE action_id = ?"
        " ORDER BY target_action_id", (action_id,),
    )) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())


class SqliteCancelLookup:
    """执行期目标查询的 SQLite 实现；只读已保存事实。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    def plan_by_request(self, request_id: str) -> tuple[int, ...] | None:
        try:
            identity = parse_object_id(request_id)
        except ValueError as error:
            raise CancelLookupError(f"请求标识无法解释: {request_id!r}") from error
        row = self._connection.execute(
            "SELECT id FROM plans WHERE request_id = ?", (identity,)).fetchone()
        if row is None:
            return None
        return self._actions_of_plan(int(row[0]))

    def plan_actions(self, plan_id: int) -> tuple[int, ...] | None:
        ObjectId(plan_id)
        row = self._connection.execute(
            "SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        return self._actions_of_plan(plan_id)

    def group_actions(self, plan_id: int, group: str) -> tuple[int, ...] | None:
        ObjectId(plan_id)
        row = self._connection.execute(
            "SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        with closing(self._connection.execute(
            "SELECT id FROM actions WHERE plan_id = ? AND group_name = ?"
            " ORDER BY id", (plan_id, group),
        )) as cursor:
            return tuple(int(row[0]) for row in cursor.fetchall())

    def action_exists(self, action_id: int) -> bool:
        ObjectId(action_id)
        return self._connection.execute(
            "SELECT 1 FROM actions WHERE id = ?", (action_id,)).fetchone() \
            is not None

    def _actions_of_plan(self, plan_id: int) -> tuple[int, ...]:
        with closing(self._connection.execute(
            "SELECT id FROM actions WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        )) as cursor:
            return tuple(int(row[0]) for row in cursor.fetchall())


def sqlite_auto_candidates(connection, action_ids) -> tuple[tuple[int, int], ...]:
    """已保存有效自动关联的联动候选：(来源拍摄, 自动预览取回)。"""
    identities = tuple(action_ids)
    if not identities:
        return ()
    with closing(connection.execute(
        "SELECT source_action_id, obtain_action_id FROM auto_preview_links"
        " WHERE is_valid = 1 AND source_action_id IN (%s) ORDER BY id"
        % ",".join("?" * len(identities)), identities,
    )) as cursor:
        return tuple(
            (int(row[0]), int(row[1])) for row in cursor.fetchall())


# 取消目标固定事件由 outputs 的 target_set 守卫核对动作类型；本守卫
# （登记名 cancel）补充成员初始值、联动依据与失败分支的成员零创建。
def _cancel_targets_guard(event, context) -> None:
    """登记名 cancel：核对目标固定、取消生效与逐项结果的完整约束。"""
    if event.event_type == _CANCEL_CHANGED_EVENT:
        _guard_cancel_changed(event, context)
        return
    if event.event_type != _TARGETS_FIXED_EVENT:
        return
    action_rows = [row for row in event.rows if row.table == "actions"]
    if len(action_rows) != 1 or not action_rows[0].before.exists:
        raise EventValidationError("取消目标事件必须恰好更新一条动作行")
    action_id = action_rows[0].row_id
    action = context.state_rows.get("actions", {}).get(action_id)
    if action is None:
        raise EventValidationError("取消目标事件缺少动作当前事实")
    created = [row for row in event.rows
               if row.table == "cancel_items" and not row.before.exists]
    if event.reason == _TARGETS_CANCEL_REASON:
        if not created:
            raise EventValidationError("取消目标固定必须创建取消成员")
        direct: set[int] = set()
        linked: set[int] = set()
        for row in created:
            values = row.after.values
            if values.get("action_id") != action_id:
                raise EventValidationError("取消成员必须属于目标固定动作")
            if values.get("target_action_id") == action_id:
                raise EventValidationError("取消成员不能以取消动作自身为目标")
            if (values.get("status") != 1
                    or values.get("outcome") is not None
                    or values.get("error_code") is not None):
                raise EventValidationError("取消成员初始值必须是待处理且无结果")
            basis = values.get("selection_basis")
            effect = values.get("cancellation_effect")
            if basis not in (member.value for member in SelectionBasis):
                raise EventValidationError(f"取消成员依据非法: {basis!r}")
            if effect not in (CancellationEffect.NOT_APPLIED.value,
                              CancellationEffect.NOT_REQUIRED.value):
                raise EventValidationError(f"取消成员初始效果非法: {effect!r}")
            if basis == SelectionBasis.DIRECT.value:
                direct.add(values.get("target_action_id"))
            else:
                linked.add(values.get("target_action_id"))
        links = context.state_rows.get("auto_preview_links", {})
        for obtain_id in linked:
            for source_id in direct:
                if any(
                    link.get("obtain_action_id") == obtain_id
                    and link.get("source_action_id") == source_id
                    and link.get("is_valid") == 1
                    for link in links.values()
                ):
                    break
            else:
                raise EventValidationError(
                    f"联动成员缺少有效自动关联依据: {obtain_id!r}")
        return
    if event.reason == _TARGETS_FAIL_REASON:
        if created:
            raise EventValidationError("目标解析失败不创建取消成员")
        after = action_rows[0].after.values
        allowed = {
            registered_error(code)["action_error_id"]
            for code in _TARGET_ERROR_CODES
        }
        if after.get("error_code") not in allowed:
            raise EventValidationError("目标解析失败的错误码不在登记集合")
        return


def _guard_cancel_changed(event, context) -> None:
    """CANCEL_CHANGED：生效转换、动作标记一致与逐项结果约束。"""
    items = [row for row in event.rows if row.table == "cancel_items"]
    if not items:
        raise EventValidationError("取消变化事件必须携带取消成员行")
    for row in items:
        before_facts = context.state_rows.get("cancel_items", {}).get(row.row_id)
        if before_facts is None:
            raise EventValidationError("取消成员更新缺少当前事实")
        origin = context.state_rows.get("actions", {}).get(
            before_facts.get("action_id"))
        if (origin is None
                or origin.get("type") != _CANCEL_ACTION_TYPE
                or origin.get("target_selection_state") != _TARGET_FIXED):
            raise EventValidationError("取消成员推进要求已固定的取消动作")
        if row.after.values.get("action_id") not in (
                None, before_facts.get("action_id")):
            raise EventValidationError("取消成员归属保持不变")
    if event.reason == _CANCEL_APPLY_REASON:
        _guard_cancel_apply(event, context, items)
    elif event.reason == _CANCEL_RESULT_REASON:
        _guard_cancel_result(event, context, items)
    elif event.reason == _CANCEL_STOP_WAIT_REASON:
        for row in items:
            if row.after.values.get("status") != _ITEM_CANCELED:
                raise EventValidationError("结束等待的成员必须转为已取消")
    else:
        raise EventValidationError(f"取消变化分支不可解释: {event.reason!r}")


def _guard_cancel_apply(event, context, items) -> None:
    """APPLY：项进入处理中、效果一次确定、目标标记与项目标一致。"""
    marked: set[int] = set()
    for row in items:
        values = row.after.values
        if values.get("status") != _ITEM_RUNNING:
            raise EventValidationError("取消生效把成员推进到处理中")
        effect = values.get("cancellation_effect")
        if effect not in (CancellationEffect.APPLIED.value,
                          CancellationEffect.NOT_REQUIRED.value):
            raise EventValidationError(f"取消生效的效果非法: {effect!r}")
    for row in event.rows:
        if row.table != "actions":
            continue
        if row.after.values.get("cancel_requested") != 1 \
                or row.before.values.get("cancel_requested") != 0:
            raise EventValidationError("取消生效把目标标记置为已请求")
        after_status = row.after.values.get("status")
        if after_status not in (2, 6, None):
            raise EventValidationError(f"取消生效的目标状态非法: {after_status!r}")
        targets = {
            item_row.after.values.get("target_action_id")
            if not item_row.before.exists or item_row.before.values.get(
                "target_action_id") is None
            else item_row.before.values.get("target_action_id")
            for item_row in items}
        facts = context.state_rows.get("cancel_items", {}).get(row.row_id, {})
        if row.row_id not in targets and row.row_id not in {
                item.get("target_action_id")
                for item in context.state_rows.get(
                    "cancel_items", {}).values()}:
            raise EventValidationError("被标记目标必须属于本次取消成员")
        marked.add(row.row_id)
    for row in event.rows:
        if row.table == "cancel_delivery_items" and not row.before.exists:
            values = row.after.values
            if values.get("status") != 1 or values.get("error_code") is not None:
                raise EventValidationError("撤回明细初始必须是待处理且无错误")


def _guard_cancel_result(event, context, items) -> None:
    """RESULT：成功携带完成依据、失败携带登记错误，效果不改写。"""
    for row in items:
        before_facts = context.state_rows.get("cancel_items", {}).get(row.row_id)
        values = row.after.values
        if values.get("cancellation_effect") not in (
                None, before_facts.get("cancellation_effect")):
            raise EventValidationError("最终结果不改写取消效果")
        if values.get("status") == _ITEM_SUCCEEDED:
            if values.get("outcome") not in (member.value for member in
                                             CancelOutcomeChoice):
                raise EventValidationError("取消成功必须携带完成依据")
            if values.get("error_code") is not None:
                raise EventValidationError("取消成功不携带错误")
        elif values.get("status") == _ITEM_FAILED:
            if not isinstance(values.get("error_code"), int):
                raise EventValidationError("取消失败必须携带登记错误")
        else:
            raise EventValidationError(
                f"最终结果的成员状态非法: {values.get('status')!r}")


def _withdrawal_guard(event, context) -> None:
    """登记名 withdrawal：撤回明细属于本次目标的取回交付。

    完整撤回状态机由交付模块（N5 组合）拥有；本守卫只核对取消生
    效事务中创建的撤回明细身份。
    """
    for row in event.rows:
        if row.table != "cancel_delivery_items" or row.before.exists:
            continue
        values = row.after.values
        item = context.state_rows.get("cancel_items", {}).get(
            values.get("cancel_item_id"))
        if item is None:
            raise EventValidationError(
                f"撤回明细缺少所属取消成员: {values.get('cancel_item_id')!r}")
        delivery = context.state_rows.get("deliveries", {}).get(
            values.get("delivery_id"))
        if delivery is None or delivery.get("action_id") != item.get(
                "target_action_id"):
            raise EventValidationError(
                f"撤回明细的交付不属于目标取回: {values.get('delivery_id')!r}")


def register_cancellation_guards() -> None:
    """注册取消目标固定事件的正式业务守卫（装配期调用）。"""
    register_guard("cancel", _cancel_targets_guard)
    register_guard("withdrawal", _withdrawal_guard)
