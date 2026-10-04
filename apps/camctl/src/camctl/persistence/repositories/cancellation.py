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
    CancelTargetError,
    CancelTargetsDisposition,
    CancelTargetsSaved,
    CancellationEffect,
    FailCancelTargets,
    FixCancelTargets,
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
from camctl.contracts.workflow_errors import registered_error, validate_error_details
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
    """登记名 cancel：核对取消成员初始值、联动依据与失败分支零创建。"""
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


def register_cancellation_guards() -> None:
    """注册取消目标固定事件的正式业务守卫（装配期调用）。"""
    register_guard("cancel", _cancel_targets_guard)
