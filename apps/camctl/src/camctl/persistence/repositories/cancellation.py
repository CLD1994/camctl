"""取消目标固定与解析失败的持久化事务。

完整目标集合经自包含检查后在一个事务内创建全部 `cancel_items` 行
（TARGETS_FIXED.CANCEL），保存每个目标的直接或联动依据及初始取消
效果；目标解析失败（包含自身或可靠不存在）以登记错误结束取消动作
（TARGETS_FIXED.FAIL），不创建任何成员。原键重送核实后只读恢复。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any

from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelActionDisposition,
    CancelActionFinished,
    CancelApplyMode,
    CancelOutcomeChoice,
    CancelStartDisposition,
    CancelStartResult,
    CancelTargetError,
    CancelTargetsDisposition,
    CancelTargetsSaved,
    CancellationEffect,
    FailCancelTargets,
    FinishCancelAction,
    FixCancelTargets,
    RecordCancelResult,
    SelectionBasis,
    StartCancelAction,
    StopWaitCancelItems,
)
from camctl.cancellation.targets import CancelLookup, CancelLookupError
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
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
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture_facts import (
    include_start_facts, load_start_facts, start_finish_event,
    unstarted_events, verify_unstarted_final,
)
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.repositories.transactions import (
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

from .cancellation_guards import (
    _ACTION_STATUS,
    _ACTION_TERMINAL,
    _CANCEL_ACTION_TYPE,
    _CANCEL_APPLY_REASON,
    _CANCEL_CHANGED_EVENT,
    _CANCEL_RESULT_REASON,
    _CANCEL_STOP_WAIT_REASON,
    _CancelContextLookup,
    _DELIVERY_STATUS,
    _ITEM_CANCELED,
    _ITEM_FAILED,
    _ITEM_PENDING,
    _ITEM_RUNNING,
    _ITEM_SUCCEEDED,
    _OBTAIN_TARGET_TYPE,
    _TARGETS_CANCEL_REASON,
    _TARGETS_FAIL_REASON,
    _TARGETS_FIXED_EVENT,
    _TARGET_ERROR_CODES,
    _TARGET_FAILED,
    _TARGET_FIXED,
    _TARGET_PENDING,
    _WITHDRAWAL_STATUS,
    _cancel_targets_guard,
    _guard_cancel_apply,
    _guard_cancel_changed,
    _guard_cancel_result,
    _guard_photo_entry_request,
    _pending_start_confirmation,
    _photo_preview_source_allowed,
    _saved_cancel_target,
    _source_start_rows,
    _validate_photo_preview_scope,
    _withdrawal_guard,
    register_cancellation_guards,
)

_ACTION_STARTED_EVENT = 5
_ACTION_STARTED_REASON = 1

_SYNC_STATUS = enum_for("state_syncs.status")
#: 动作终态集合。

#: 目标解析失败仅有的登记错误。

_ACTION_FINISHED_EVENT = 8
_PLAN_STATUS_EVENT = 9
_PLAN_COMPLETE = 3
#: cancel_items.status 的登记编号。
_CANCEL_ITEMS_FAILED = "cancel_items_failed"
#: deliveries.status PUBLISHED 与 withdrawal_state NOT_REQUESTED。
_DELIVERY_PUBLISHED = 5
_WITHDRAWAL_NOT_REQUESTED = 1
#: 动作状态：PENDING=1、RUNNING=2、SUCCEEDED=3、FAILED=4、CANCELED=6。
_ACTION_SUCCEEDED, _ACTION_FAILED, _ACTION_CANCELED = 3, 4, 6


@dataclass(frozen=True)
class _ItemValues:
    target_action_id: int
    selection_basis: int
    cancellation_effect: int


class _StartCancelCommand:
    """取消动作开始执行的事务命令（ACTION_STARTED.START）。

    只保存开始事实；目标解析与集合固定由后续事务推进。时间资格由
    调用入口判断，本命令不复验墙钟。
    """

    def __init__(self, command: StartCancelAction, key: OperationKey) -> None:
        if not isinstance(command, StartCancelAction):
            raise TypeError("取消动作开始申请必须使用 StartCancelAction")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise ConsistencyError(f"取消动作不存在: {command.action_id}")
        self._state["actions"] = {command.action_id: dict(action)}
        if action["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError(
                f"开始事务只适用于取消动作: {command.action_id}"
                f" type={action['type']!r}")
        if action["status"] in _ACTION_TERMINAL:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelStartResult(
                    disposition=CancelStartDisposition.ALREADY))
        if action["cancel_requested"]:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelStartResult(
                    disposition=CancelStartDisposition.REJECTED,
                    reason="canceled"))
        if action["status"] != 1 or action["execution_started"] != 0:
            raise TransactionError(
                f"取消动作不在待执行状态: {command.action_id}"
                f" status={action['status']!r}"
                f" execution_started={action['execution_started']!r}")
        # 动作开始事实与计划首次开始同一事务保存（计划执行状态规格：
        # 曾有动作开始且未全部终态的计划为执行中）。
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError(f"计划不存在: {action['plan_id']}")
        self._state.setdefault("plans", {})[plan["id"]] = plan
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        allocation = scope.allocate(2 if plan["status"] == 1 else 1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTION_STARTED_EVENT, _ACTION_STARTED_REASON,
            (_update(
                "actions", command.action_id,
                {"status": 1, "execution_started": 0},
                {"status": 2, "execution_started": 1}),),
            command.occurred_at)
        events = [event]
        if plan["status"] == 1:
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            events.append(_envelope(
                allocation.last_event_id, allocation.txn_id,
                _PLAN_STATUS_EVENT, 1,
                (_update("plans", plan["id"],
                 {"status": 1}, {"status": 2}),),
                command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=CancelStartResult(disposition=CancelStartDisposition.SAVED))

    def _reuse(self, scope, saved) -> CommandPlan:
        """原键重送：核实原开始分支与输入后恢复首次响应。"""
        command = self._command
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds not in (
                [(_ACTION_STARTED_EVENT, _ACTION_STARTED_REASON)],
                [(_ACTION_STARTED_EVENT, _ACTION_STARTED_REASON),
                 (_PLAN_STATUS_EVENT, 1)]):
            raise TransactionError("操作身份已用于其他事务，不能作为取消开始重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("取消开始的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "actions" or row["id"] != command.action_id:
            raise TransactionError("原取消开始属于其他动作")
        facts = row_facts(scope.connection, "actions", command.action_id)
        if facts is None or facts["status"] != 2 \
                or facts["execution_started"] != 1:
            raise TransactionError("原取消开始的可靠记录与输入不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelStartResult(disposition=CancelStartDisposition.SAVED))


def _cancel_origin_abandoned(action):
    return (action is not None and action['type'] == _CANCEL_ACTION_TYPE
            and action['target_selection_state'] == _TARGET_PENDING
            and (action['cancel_requested'] or action['status'] in (3, 4, 5, 6)))


class _FixCancelTargetsCommand:
    """固定取消动作的完整目标集合（TARGETS_FIXED.CANCEL）。

    全部成员在一个事务内创建：中断或重送不产生部分取消。自身包含
    检查由调用方（prepare_cancel_set）完成，命令拒绝包含自身的输入。
    """

    def __init__(self, command: FixCancelTargets, key: OperationKey, *, reserved_sources=frozenset()) -> None:
        self._reserved_sources = reserved_sources
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
        if _cancel_origin_abandoned(action):
            return CommandPlan((), {}, {}, CancelTargetsSaved(CancelTargetsDisposition.ABANDONED), read_only=True)
        if action["target_selection_state"] == _TARGET_FAILED:
            raise TransactionError("目标集合已失败，不重新固定")
        if action["target_selection_state"] == _TARGET_FIXED:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelTargetsSaved(
                    CancelTargetsDisposition.ALREADY,
                    _saved_cancel_item_rows(connection, command.action_id)))
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
        seen: set[int] = set()
        for target in items:
            if target.action_id == command.action_id:
                raise ConsistencyError(
                    "固定集合包含取消动作自身，自包含检查缺失")
            if target.action_id in seen:
                raise ConsistencyError(
                    f"固定集合目标重复: {target.action_id}")
            seen.add(target.action_id)
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
        coverage = self._load_links(connection, [target.action_id for target in items])
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _TARGETS_FIXED_EVENT, _TARGETS_CANCEL_REASON, rows,
            command.occurred_at)
        from camctl.contracts.history_values import TransactionRange
        from camctl.history.validators import EventContext
        _validate_photo_preview_scope(action, [row.after.values for row in rows if row.table == "cancel_items"],
            EventContext(TransactionRange(allocation.txn_id, allocation.first_event_id, allocation.last_event_id),
                self._owners, self._state, read_coverage=coverage), self._reserved_sources,
            allow_pending_reserved=True)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            read_coverage=coverage,
            result=CancelTargetsSaved(
                CancelTargetsDisposition.SAVED,
                tuple(sorted(self._state["cancel_items"]))))

    def _load_links(self, connection, obtain_ids: list[int]) -> ReadCoverage:
        """从原地址加载完整直接范围，再读双向关联；不由提交标签选范围。"""
        from camctl.cancellation.targets import resolve_cancel_target
        origin = self._state["actions"][self._command.action_id]
        target = _saved_cancel_target(origin)
        ranges = {}
        actions = self._state.setdefault("actions", {})
        plans = self._state.setdefault("plans", {})
        if target.action_instance_id is not None:
            identities = {target.action_instance_id}
        else:
            if target.request_id is not None:
                request_id = parse_object_id(target.request_id)
                ranges[("plans", "request_id")] = {request_id}
                plan_ids = [row[0] for row in connection.execute(
                    "SELECT id FROM plans WHERE request_id=?", (request_id,))]
            else:
                plan_ids = [target.plan_instance_id]
            identities = set()
            ranges[("plans", "id")] = set(plan_ids)
            ranges[("actions", "plan_id")] = set(plan_ids)
            for plan_id in plan_ids:
                plan = row_facts(connection, "plans", plan_id)
                if plan is not None:
                    plans[plan_id] = plan
                identities.update(row[0] for row in connection.execute(
                    "SELECT id FROM actions WHERE plan_id=?", (plan_id,)))
        resolution = resolve_cancel_target(target, SqliteCancelLookup(connection))
        if resolution.missing or resolution.lookup_failed is not None:
            raise ConsistencyError("原取消地址不能可靠解析为完整直接范围")
        direct = resolution.action_ids
        links = self._state.setdefault("auto_preview_links", {})
        ranges[("auto_preview_links", "source_action_id")] = set(direct)
        for source_id in direct:
            for link_id, in connection.execute(
                    "SELECT id FROM auto_preview_links WHERE source_action_id=?", (source_id,)):
                links[link_id] = row_facts(connection, "auto_preview_links", link_id)
        obtains = set(obtain_ids) | set(direct) | {link["obtain_action_id"] for link in links.values()}
        ranges[("auto_preview_links", "obtain_action_id")] = obtains
        for obtain_id in obtains:
            for link_id, in connection.execute(
                    "SELECT id FROM auto_preview_links WHERE obtain_action_id=?", (obtain_id,)):
                links[link_id] = row_facts(connection, "auto_preview_links", link_id)
        scope_identities = obtains | {link["source_action_id"] for link in links.values() if link["source_action_id"] is not None}
        identities.update(scope_identities)
        ranges[("actions", "id")] = identities
        from camctl.devices.start_entry import is_hasselblad_photo
        for identity in identities:
            action = row_facts(connection, "actions", identity)
            if action is None:
                raise ConsistencyError("固定取消来源或目标动作不存在")
            actions[identity] = action
            plan = row_facts(connection, "plans", action["plan_id"])
            if plan is None:
                raise ConsistencyError("固定取消来源或目标缺少原计划")
            plans[plan["id"]] = plan
            ranges.setdefault(("plans", "id"), set()).add(plan["id"])
        if any(is_hasselblad_photo(actions[identity]) for identity in scope_identities):
            for identity in direct:
                action = actions[identity]
                if action["type"] in (1, 2, 3):
                    coverage = include_photo_entry_rows(connection, action, self._state, self._owners)
                    for key, values in coverage.ranges.items():
                        ranges.setdefault(key, set()).update(values)
        return ReadCoverage({key: frozenset(values) for key, values in ranges.items()})

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实固定分支与成员集合后恢复首次响应。"""
        command = self._command
        if not saved or (saved[0]["type"], saved[0]["reason"]) != (
                _TARGETS_FIXED_EVENT, _TARGETS_CANCEL_REASON):
            raise TransactionError("原事务不是取消目标固定，不能作为重送核实")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("目标固定的事实时刻与原事务不同")
        origins = [row["id"] for row in saved[0]["body"]["rows"] if row["table"] == "actions"]
        if origins != [command.action_id] or any(
                row["after"]["values"]["action_id"] != command.action_id
                for event in saved for row in event["body"]["rows"] if row["table"] == "cancel_items"):
            raise TransactionError("目标固定的原取消动作与重送输入不同")
        created = {
            (row["after"]["values"]["target_action_id"], row["after"]["values"]["selection_basis"],
             row["after"]["values"]["cancellation_effect"])
            for event in saved
            for row in event["body"]["rows"]
            if row["table"] == "cancel_items"}
        if (len(created) != len(command.targets.targets)
                or created != {(target.action_id, target.basis.value, target.cancellation_effect.value)
                               for target in command.targets.targets}):
            raise TransactionError("目标固定的成员集合与重送输入不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(
                CancelTargetsDisposition.ALREADY,
                _saved_cancel_item_rows(connection, command.action_id)))


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


def _cancel_request(command):
    return {
        "item_id": command.item_id,
        "mode": command.mode.value,
        "occurred_at": command.occurred_at,
    }


def _motor_cancel_request(command):
    return _cancel_request(command)


def _preparation_cancel_pending(facts):
    from camctl.cancellation.rules import preparation_cancel_pending
    return preparation_cancel_pending(not_started=facts.not_started,
        preparation_resolved=facts.preparation_resolved, activity=facts.activity)


class _ApplyCancelTargetCommand:
    """保存一个目标的取消生效（CANCEL_CHANGED.APPLY）。

    PENDING 目标同事务终态取消并保存成功结果；RUNNING 目标保存取消
    标记并结束普通启动责任，本地或设备收场另行推进；终态目标不改写，有已发
    布交付时先保存撤回明细并保持成员处理中，收场完成后按既有终态
    保存成功；目标取消已生效时只保存本项效果，复用原责任。
    """

    def __init__(self, command: ApplyCancelTarget, key: OperationKey, motor_permit=None,
                 *, fixed_item=None, result_events=(), prior_pre_start_ids=frozenset()) -> None:
        if not isinstance(command, ApplyCancelTarget):
            raise TypeError("取消生效申请必须使用 ApplyCancelTarget")
        self._motor_permit = motor_permit
        self._fixed_item, self._result_events = fixed_item, result_events
        self._prior_pre_start_ids = prior_pre_start_ids
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
        item, target = self._load(connection)
        if item["status"] in (_ITEM_SUCCEEDED, _ITEM_FAILED):
            return self._already()
        if item["status"] == _ITEM_CANCELED:
            raise ConsistencyError("取消发起者收场的成员不经普通生效")
        if item["status"] != _ITEM_PENDING:
            return self._already()
        from camctl.devices.start_entry import is_hasselblad_photo
        if is_hasselblad_photo(target):
            if command.mode is CancelApplyMode.WITH_STOP:
                raise ConsistencyError("Hasselblad START 不支持停止取消")
            self._photo_coverage = include_photo_entry_rows(connection, target, self._state, self._owners)
        else:
            self._photo_coverage = ReadCoverage()
        mode = command.mode
        target_id = item["target_action_id"]
        action_rows: tuple = ()
        motor_rows: tuple = ()
        motor_evidence = ({"motor_request": _motor_cancel_request(command)}
                          if target["type"] == 8
                          else {"cancel_request": _cancel_request(command)})
        outcome_event = None
        plan_row = None
        start_events = []
        canceled_sync = None
        withdrawal_rows = self._withdrawal_rows(connection, item, target)
        from camctl.cancellation.rules import (validate_stored_cancel_mode, pre_start_cancel_finishes_target,
            immediate_cancel_member_outcome)
        validate_stored_cancel_mode(target, mode)
        if mode is CancelApplyMode.PRE_START:
            if target["type"] in (1, 2, 3):
                start = load_start_facts(connection, target, result_events=self._result_events)
                if not start.not_started:
                    raise ConsistencyError("未启动取消缺少可靠未启动依据")
                include_start_facts(load_start_facts(connection, target), self._state, self._owners)
                start_events = unstarted_events(
                    start, command.occurred_at,
                    run_status=int(enum_for("operation_runs.status").CANCELED), release=False)
            elif target['type'] == 8:
                from camctl.motor.rules import owns_send_permit
                from camctl.persistence.repositories.motor import read_motor_facts
                facts = read_motor_facts(connection, target_id)
                if facts.notification is not None:
                    permit = self._motor_permit
                    if not owns_send_permit(facts,permit):
                        raise ConsistencyError('电机未发送取消缺少本地原意图许可')
                    notice = row_facts(connection,'motor_notifications',facts.notification.notification_id)
                    self._state.setdefault('motor_notifications',{})[notice['id']] = notice
                    self._owners[('motor_notifications',notice['id'])] = ('action',target_id)
                    motor_rows = (_update('motor_notifications',notice['id'],
                        {'outcome':1,'finished_at':None}, {'outcome':2,'finished_at':command.occurred_at}),)
                    motor_evidence['motor_permit'] = {'action_id':permit.action_id,
                        'notification_id':permit.notification_id,'operation_key':str(permit.operation_key)}
            elif target["status"] != 1:
                raise ConsistencyError("非拍摄目标的未启动取消要求待执行状态")
            before, after = {"cancel_requested": 0}, {"cancel_requested": 1}
            if pre_start_cancel_finishes_target(target):
                before["status"], after["status"] = target["status"], _ACTION_CANCELED
                preparation_pending = target["type"] in (1, 2, 3) and _preparation_cancel_pending(start)
                outcome = immediate_cancel_member_outcome(mode, target,
                    withdrawal_pending=False, preparation_pending=preparation_pending)
                if outcome is not None:
                    outcome_event = _ITEM_SUCCEEDED, outcome.value
                plan_row = self._pre_start_plan_row(connection, target)
            action_rows = (_update("actions", target_id, before, after),)
            effect_after = CancellationEffect.APPLIED.value
            # 直接终态取消可能使目标计划全部动作终态：同一事务补计划
            # 完成事实，否则执行前取消的计划无人推进（计划执行状态
            # 规格：全部动作在执行前取消的计划也进入完成）。
        elif mode is CancelApplyMode.WITH_STOP:
            action_rows = (_update(
                "actions", target_id,
                {"cancel_requested": 0}, {"cancel_requested": 1}),)
            effect_after = CancellationEffect.APPLIED.value
        elif mode is CancelApplyMode.TERMINAL:
            preparation_pending = False
            if not withdrawal_rows and target["type"] in (1, 2, 3):
                preparation_pending = _preparation_cancel_pending(load_start_facts(connection, target))
            outcome = immediate_cancel_member_outcome(mode, target,
                withdrawal_pending=bool(withdrawal_rows), preparation_pending=preparation_pending)
            if outcome is not None:
                outcome_event = _ITEM_SUCCEEDED, outcome.value
            effect_after = CancellationEffect.NOT_REQUIRED.value
        elif mode is CancelApplyMode.ALREADY:
            effect_after = CancellationEffect.APPLIED.value
        else:
            raise ConsistencyError(f"取消生效方式不可解释: {mode!r}")
        if target["type"] == 7 and target["status"] == 2:
            sync_id = connection.execute(
                "SELECT id FROM state_syncs WHERE action_id = ?", (target_id,),
            ).fetchone()
            if sync_id is None or target["execution_started"] != 1:
                raise ConsistencyError("运行中报告目标缺少原同步责任")
            sync = row_facts(connection, "state_syncs", sync_id[0])
            self._state.setdefault("state_syncs", {})[sync["id"]] = sync
            if sync["local_report_id"] is not None:
                raise ConsistencyError("运行中报告目标已保存本地成功结果")
            if sync["status"] == int(_SYNC_STATUS.OUTSTANDING):
                if mode is not CancelApplyMode.WITH_STOP:
                    raise ConsistencyError("已生效报告取消的同步责任仍未结束")
                canceled_sync = sync
                self._owners[("state_syncs", sync["id"])] = ("state_sync", sync["id"])
            elif sync["status"] == int(_SYNC_STATUS.CANCELED):
                if mode is not CancelApplyMode.ALREADY:
                    raise ConsistencyError("未取消报告目标的同步责任已取消")
            elif sync["status"] != int(_SYNC_STATUS.ACKNOWLEDGED):
                raise ConsistencyError("报告目标的同步状态不可解释")
        if (mode in (CancelApplyMode.WITH_STOP, CancelApplyMode.ALREADY)
                and target["type"] in (1, 2, 3) and target["status"] == 2):
            start = load_start_facts(connection, target)
            include_start_facts(start, self._state, self._owners)
            finish = start_finish_event(
                start.run, int(enum_for("operation_runs.status").CANCELED), command.occurred_at)
            if finish is not None:
                start_events.append(finish)
        current_effect = item["cancellation_effect"]
        apply_before = {"status": _ITEM_PENDING}
        apply_after = {"status": _ITEM_RUNNING}
        if current_effect != effect_after:
            apply_before["cancellation_effect"] = current_effect
            apply_after["cancellation_effect"] = effect_after
        apply_row = _update(
            "cancel_items", command.item_id, apply_before, apply_after)
        if withdrawal_rows:
            # 报告关联解析沿撤回明细→取消成员/交付取事实。
            self._state.setdefault("cancel_delivery_items", {})
            self._state.setdefault("deliveries", {})
        self._claim(item, target_id)
        # 生效、结果与可能计划完成事件按实际组合分配，避免留空洞。
        allocation = scope.allocate(
            1 + int(canceled_sync is not None) + len(start_events)
            + int(outcome_event is not None) + int(plan_row is not None))
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _CANCEL_CHANGED_EVENT, _CANCEL_APPLY_REASON,
            (apply_row,) + action_rows + motor_rows + withdrawal_rows,
            command.occurred_at, motor_evidence)]
        next_event_id = allocation.first_event_id + 1
        if canceled_sync is not None:
            events.append(_envelope(
                next_event_id, allocation.txn_id, 29, 4,
                (_update("state_syncs", canceled_sync["id"],
                         {"status": int(_SYNC_STATUS.OUTSTANDING), "ended_event_id": None},
                         {"status": int(_SYNC_STATUS.CANCELED), "ended_event_id": next_event_id}),),
                command.occurred_at))
            next_event_id += 1
        for event in start_events:
            events.append(replace(event, event_id=next_event_id,
                                  transaction_id=allocation.txn_id))
            next_event_id += 1
        if outcome_event is not None:
            final_status, outcome_value = outcome_event
            result_row = _update(
                "cancel_items", command.item_id,
                {"status": _ITEM_RUNNING, "outcome": None,
                 "error_code": None, "error_details_json": None},
                {"status": final_status, "outcome": outcome_value,
                 "error_code": None, "error_details_json": None})
            events.append(_envelope(
                next_event_id, allocation.txn_id,
                _CANCEL_CHANGED_EVENT, _CANCEL_RESULT_REASON,
                (result_row,), command.occurred_at))
            next_event_id += 1
        if plan_row is not None:
            events.append(_envelope(
                next_event_id, allocation.txn_id,
                _PLAN_STATUS_EVENT, 2, (plan_row,), command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(CancelTargetsDisposition.SAVED,
                                      (command.item_id,)),
            read_coverage=ReadCoverage({("motor_notifications","action_id"):frozenset([target_id])}) if target["type"] == 8 else self._photo_coverage)

    def _load(self, connection):
        item = self._fixed_item or row_facts(connection, "cancel_items", self._command.item_id)
        if item is None:
            raise ConsistencyError(f"取消成员不存在: {self._command.item_id}")
        self._state.setdefault("cancel_items", {})[item["id"]] = dict(item)
        origin = row_facts(connection, "actions", item["action_id"])
        if origin is None:
            raise ConsistencyError(f"取消动作不存在: {item['action_id']}")
        self._state["actions"] = {origin["id"]: dict(origin)}
        if origin["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("取消生效要求取消动作")
        if origin["target_selection_state"] != _TARGET_FIXED and self._fixed_item is None:
            raise ConsistencyError("取消生效要求目标集合已固定")
        if origin["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError("取消生效要求取消动作执行中")
        target = row_facts(connection, "actions", item["target_action_id"])
        if target is None:
            raise ConsistencyError(
                f"取消目标不存在: {item['target_action_id']}")
        self._state["actions"][target["id"]] = dict(target)
        return item, target

    def _withdrawal_rows(self, connection, item, target) -> tuple:
        """取回目标生效时为已发布交付创建撤回明细（待处理）。"""
        if target["type"] != _OBTAIN_TARGET_TYPE:
            return ()
        next_item = next_row_id(connection, "cancel_delivery_items")
        with closing(connection.execute(
            "SELECT id FROM deliveries WHERE action_id = ? AND (status = ?"
            " OR (status = ? AND withdrawal_state = ?)) ORDER BY id",
            (target["id"], _DELIVERY_PUBLISHED, int(_DELIVERY_STATUS.CANCELED),
             int(_WITHDRAWAL_STATUS.UNKNOWN)),
        )) as cursor:
            delivery_ids = tuple(int(row[0]) for row in cursor.fetchall())
        rows = []
        for delivery_id in delivery_ids:
            delivery_row = row_facts(connection, "deliveries", delivery_id)
            if delivery_row is not None:
                self._state.setdefault("deliveries", {})[
                    delivery_id] = delivery_row
            values = {
                "cancel_item_id": item["id"],
                "delivery_id": delivery_id,
                "status": 1,
                "error_code": None,
                "error_details_json": None,
            }
            rows.append(_row("cancel_delivery_items", next_item, values))
            self._owners[("cancel_delivery_items", next_item)] = (
                "action", item["action_id"])
            next_item += 1
        return tuple(rows)

    def _claim(self, item, target_id: int) -> None:
        self._owners[("cancel_items", item["id"])] = (
            "action", item["action_id"])
        # 目标动作行属于其自身历史；取消成员行属于取消动作。
        self._owners[("actions", target_id)] = ("action", target_id)

    def _pre_start_plan_row(self, connection, target):
        """执行前直接终态取消的目标计划可能因此全部动作终态。

        目标恰好是计划最后一个未终态动作时返回同事务补保存的计
        划完成行；其他成员未终态时计划保持原状态，不伪造完成。
        """
        plan = row_facts(connection, "plans", target["plan_id"])
        if plan is None:
            raise ConsistencyError(f"目标计划不存在: {target['plan_id']}")
        if plan["status"] not in (1, 2):
            return None
        with closing(connection.execute(
                "SELECT id, status FROM actions WHERE plan_id = ?",
                (plan["id"],),
        )) as cursor:
            rows = cursor.fetchall()
        unfinished = {
            int(action_id) for action_id, status in rows
            if status not in _ACTION_TERMINAL}
        unfinished.difference_update(self._prior_pre_start_ids)
        if not rows or unfinished != {target["id"]}:
            return None
        self._state.setdefault("plans", {})[plan["id"]] = plan
        self._owners[("plans", plan["id"])] = ("plan", plan["id"])
        return _update("plans", plan["id"],
                       {"status": plan["status"]},
                       {"status": _PLAN_COMPLETE})

    def _already(self) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY,
                                      (self._command.item_id,)))

    def _reuse(self, connection, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        order = {(25, 1): 0, (29, 4): 1, (13, 2): 2, (10, 3): 3, (25, 2): 4, (9, 2): 5}
        if (not kinds or kinds[0] != (25, 1)
                or any(kind not in order for kind in kinds)
                or len(set(kinds)) != len(kinds)
                or [order[kind] for kind in kinds] != sorted(order[kind] for kind in kinds)):
            raise TransactionError("原事务不是取消生效，不能作为重送核实")
        if any(event["occurred_at"] != self._command.occurred_at for event in saved):
            raise TransactionError("取消生效的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if not rows or rows[0]["table"] != "cancel_items"                 or rows[0]["id"] != self._command.item_id:
            raise TransactionError("原取消生效属于其他成员")
        original_item = row_facts(connection,'cancel_items',self._command.item_id)
        if original_item is None:
            raise ConsistencyError('原取消事务的成员不存在')
        original_target = row_facts(connection,'actions',original_item['target_action_id'])
        if original_target is None:
            raise ConsistencyError('原取消事务的目标不存在')
        evidence = saved[0]["body"]["evidence"]
        if (original_target["type"] != 8
                and not json_equal(evidence.get("cancel_request"), _cancel_request(self._command))):
            raise TransactionError("原取消事务的完整输入与本次核实不符")
        if original_target["type"] in (4, 5, 6, 7):
            target_rows = [row for row in rows if row["table"] == "actions"]
            effect = rows[0]["after"]["values"].get(
                "cancellation_effect", original_item["cancellation_effect"])
            if target_rows:
                if len(target_rows) != 1 or target_rows[0]["id"] != original_target["id"]:
                    raise ConsistencyError("原取消事务标记了其他目标")
                change = target_rows[0]
                after = change["after"]["values"]
                before = change["before"]["values"]
                if after == {"status": 6, "cancel_requested": 1} and before == {
                        "status": 1, "cancel_requested": 0}:
                    original_mode = CancelApplyMode.PRE_START
                elif after == {"cancel_requested": 1} and before == {"cancel_requested": 0}:
                    original_mode = CancelApplyMode.WITH_STOP
                else:
                    raise ConsistencyError("原取消事务的目标状态转换不可解释")
                if effect != CancellationEffect.APPLIED.value or original_target["cancel_requested"] != 1:
                    raise ConsistencyError("原取消事务的目标标记与成员效果不符")
            elif effect == CancellationEffect.APPLIED.value:
                original_mode = CancelApplyMode.ALREADY
                if original_target["cancel_requested"] != 1:
                    raise ConsistencyError("原复用取消责任的目标标记未保持")
            elif effect == CancellationEffect.NOT_REQUIRED.value:
                original_mode = CancelApplyMode.TERMINAL
                if original_target["status"] not in _ACTION_TERMINAL:
                    raise ConsistencyError("原终态取消的目标终态未保持")
            else:
                raise ConsistencyError("原取消事务的成员效果不可解释")
            if self._command.mode is not original_mode:
                raise TransactionError("取消生效重送的 mode 与原输入不同")
            if (original_target["type"] == 7 and original_target["execution_started"] == 1
                    and original_mode is not CancelApplyMode.TERMINAL):
                sync_identity = connection.execute(
                    "SELECT id FROM state_syncs WHERE action_id = ?", (original_target["id"],),
                ).fetchone()
                if sync_identity is None:
                    raise ConsistencyError("原报告取消目标缺少同步责任")
                sync = row_facts(connection, "state_syncs", sync_identity[0])
                if (original_mode is not CancelApplyMode.TERMINAL
                        and (sync["status"] not in (int(_SYNC_STATUS.CANCELED), int(_SYNC_STATUS.ACKNOWLEDGED))
                             or sync["ended_event_id"] is None or sync["local_report_id"] is not None)):
                    raise ConsistencyError("原报告取消的同步结束事实未保持")
        sync_events = [event for event in saved if (event["type"], event["reason"]) == (29, 4)]
        if sync_events:
            if original_target["type"] != 7:
                raise ConsistencyError("非报告取消事务携带同步结束")
            event = sync_events[0]
            sync_rows = event["body"]["rows"]
            if len(sync_rows) != 1 or sync_rows[0]["table"] != "state_syncs":
                raise ConsistencyError("原同步取消必须结束唯一同步记录")
            sync = row_facts(connection, "state_syncs", sync_rows[0]["id"])
            if (sync is None or sync["action_id"] != original_target["id"]
                    or sync["status"] != int(_SYNC_STATUS.CANCELED)
                    or sync["ended_event_id"] != event["event_id"]
                    or sync_rows[0]["after"]["values"] != {
                        "status": int(_SYNC_STATUS.CANCELED), "ended_event_id": event["event_id"]}):
                raise ConsistencyError("原取消事务的同步结束事实不符")
        if original_target['type'] == 8 and not json_equal(saved[0]['body']['evidence'].get('motor_request'),_motor_cancel_request(self._command)):
            raise TransactionError('原电机取消事务的完整输入与本次核实不符')
        if self._command.mode is CancelApplyMode.PRE_START:
            item = row_facts(connection, "cancel_items", self._command.item_id)
            target = row_facts(connection, "actions", item["target_action_id"])
            if target['type'] == 8:
                from camctl.persistence.repositories.motor import read_motor_facts
                from camctl.motor.models import SendOutcome
                facts = read_motor_facts(connection,target['id'])
                if facts.action['status'] != 6 or (facts.notification is not None and facts.notification.outcome is not SendOutcome.NOT_SENT):
                    raise ConsistencyError('原电机取消缺少共同保存的未发送终态')
            if target["type"] in (1, 2, 3):
                verify_unstarted_final(connection, target, saved, released=False)
        elif self._command.mode in (CancelApplyMode.WITH_STOP, CancelApplyMode.ALREADY):
            item = row_facts(connection, "cancel_items", self._command.item_id)
            target = row_facts(connection, "actions", item["target_action_id"])
            if target["type"] in (1, 2, 3):
                start = load_start_facts(connection, target)
                if start.run is not None and start.run["status"] in (1, 2):
                    raise ConsistencyError("取消生效后普通启动责任仍未结束")
                for event in saved:
                    if (event["type"], event["reason"]) != (10, 3):
                        continue
                    finish_rows = event["body"]["rows"]
                    if len(finish_rows) != 1 or start.run is None:
                        raise ConsistencyError("取消生效的启动收场事实缺失")
                    row = finish_rows[0]
                    values = row["after"]["values"]
                    if (row["table"] != "operation_runs" or row["id"] != start.run["id"]
                            or values.get("status") != int(enum_for("operation_runs.status").CANCELED)
                            or set(values) - {"status", "retry_wait_required", "error_json"}
                            or any(start.run[column] != value for column, value in values.items())):
                        raise ConsistencyError("取消生效的启动收场与原责任不符")
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
        target = row_facts(connection, "actions", item["target_action_id"])
        if target is None:
            raise ConsistencyError("取消结果缺少目标动作")
        self._state["actions"][target["id"]] = target
        if (target["type"] == 7 and command.outcome is not None
                and target["status"] not in _ACTION_TERMINAL):
            raise ConsistencyError("报告取消成功结果要求目标已终态")
        if target["type"] in (1, 2, 3) and command.outcome is not None:
            start = load_start_facts(connection, target)
            if _preparation_cancel_pending(start):
                raise ConsistencyError("准备取消尚未完成原调用和占用释放")
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
        if (origin["status"] != int(_ACTION_STATUS.RUNNING)
                and origin["status"] not in _ACTION_TERMINAL):
            raise TransactionError(
                f"取消动作状态不可解释: {origin['id']} {origin['status']!r}")
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


def _verify_motor_cancel_rows(connection, saved, item):
    with closing(
        connection.execute(
            "SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
        )
    ) as cursor:
        latest = cursor.fetchone()
    if latest is None:
        raise ConsistencyError("电机取消事务缺少当前完整历史边界")
    current_boundary = HistoryBoundary(*latest)
    transaction = saved[0]["transaction"]
    boundary = HistoryBoundary(transaction.txn_id, transaction.last_event_id)
    final = {}
    for event in saved:
        for row in event["body"]["rows"]:
            if (
                row["table"]
                not in ("actions", "plans", "cancel_items", "motor_notifications")
                or not row["after"]["exists"]
            ):
                raise ConsistencyError("电机取消原事务包含未知所属记录")
            final.setdefault((row["table"], row["id"]), {}).update(
                row["after"]["values"]
            )
    for (table, identity), values in final.items():
        current = row_facts(connection, table, identity)
        if current is None:
            raise ConsistencyError("电机取消原事务的结果记录缺失")
        if table == "plans":
            owner = ("plan", identity)
        elif table == "actions":
            owner = ("action", identity)
        elif table == "cancel_items":
            if identity != item["id"] or current["action_id"] != item["action_id"]:
                raise ConsistencyError("电机取消原事务成员身份不符")
            owner = ("action", item["action_id"])
        else:
            if current["action_id"] != item["target_action_id"]:
                raise ConsistencyError("电机取消发送记录所属不符")
            owner = ("action", item["target_action_id"])
        actual = read_row_values_at_boundary(
            connection,
            owner=owner,
            table=table,
            row_id=identity,
            columns=frozenset(values),
            current_values=current,
            boundary=boundary,
            current_boundary=current_boundary,
        )
        if not json_equal(actual, values):
            raise ConsistencyError("电机取消原事务的完整结果与历史不符")
    return boundary


class CancellationRepository:
    """取消目标固定与解析失败事务的 SQLite 仓储。"""

    def read_eligibility_inputs(self, owned, action_id, *, motor_permit=None):
        """在当前数据库读取内取得原资格事实，交由业务继续决策。"""
        return load_eligibility_inputs(owned.connection, action_id, motor_permit=motor_permit)


    def verify_motor_operation(self, command, key, owned):
        """使用新连接只读核实电机目标的原生效或原结果事务。"""
        from camctl.persistence.repositories.motor import read_motor_facts

        if not isinstance(command, (ApplyCancelTarget, RecordCancelResult)):
            raise TypeError("电机取消核实要求原生效或结果请求")
        connection = owned.connection
        reading = False
        try:
            key = OperationKey(str(key))
            if connection.in_transaction:
                raise TransactionError("电机取消核实要求独立新连接")
            with closing(connection.execute("BEGIN")):
                pass
            reading = True
            item = row_facts(connection, "cancel_items", command.item_id)
            if item is None:
                raise ConsistencyError("电机取消核实缺少原成员")
            read_motor_facts(connection, item["target_action_id"])
            saved = saved_transaction_events(connection, key)
            if saved is None:
                outcome = DbOutcome(
                    DbOutcomeKind.ROLLED_BACK,
                    error=TransactionError("原电机取消操作身份可靠未提交"),
                    stage="verification",
                )
            else:
                if isinstance(command, ApplyCancelTarget):
                    plan = _ApplyCancelTargetCommand(
                        command, key
                    )._reuse(connection, saved)
                else:
                    plan = _RecordCancelResultCommand(command, key)._reuse(saved)
                    rows = saved[0]["body"]["rows"]
                    if len(rows) != 1:
                        raise TransactionError("原电机取消项结果的事务组成不符")
                    if command.outcome is not None:
                        expected = {
                            "status": _ITEM_SUCCEEDED,
                            "outcome": command.outcome.value,
                            "error_code": None,
                            "error_details_json": None,
                        }
                    else:
                        validate_error_details(command.code, command.details)
                        expected = {
                            "status": _ITEM_FAILED,
                            "outcome": None,
                            "error_code": item_error_id("cancel_items", command.code),
                            "error_details_json": command.details,
                        }
                    after = rows[0]["after"]["values"]
                    if any(
                        not json_equal(after.get(name), value)
                        for name, value in expected.items()
                    ):
                        raise TransactionError("原电机取消项结果的完整输入与本次核实不符")
                if not plan.read_only or plan.events:
                    raise ConsistencyError("电机取消核实不得产生新事件")
                boundary = _verify_motor_cancel_rows(connection, saved, item)
                outcome = DbOutcome(
                    DbOutcomeKind.COMPLETED,
                    plan.result,
                    boundary=boundary,
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
                        DbOutcomeKind.UNKNOWN, error=rollback_error, stage="verification"
                    )
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=error, stage="verification")

    def start_cancel_action(
        self, command: StartCancelAction, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelStartResult]:
        receipt = commit_operation(
            _StartCancelCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def fix_cancel_targets(
        self, command: FixCancelTargets, key: OperationKey,
        owned: OwnedConnection, *, reserved_entries=(),
    ) -> DbOutcome[CancelTargetsSaved]:
        planner = (_FixPhotoEntriesCommand(command, key, reserved_entries, owned.metadata.instance_id)
                   if command.entry_blocks else _FixCancelTargetsCommand(command, key))
        receipt = commit_operation(planner, key, owned)
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
        owned: OwnedConnection, *, motor_permit=None,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _ApplyCancelTargetCommand(command, key, motor_permit), key, owned)
        return _outcome_of(receipt)

    def record_cancel_result(
        self, command: RecordCancelResult, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _RecordCancelResultCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def stop_wait_cancel_items(
        self, command: StopWaitCancelItems, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelTargetsSaved]:
        receipt = commit_operation(
            _StopWaitCancelItemsCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def finish_origin_canceled(
        self, command: FinishCancelAction, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CancelActionFinished]:
        receipt = commit_operation(
            _FinishOriginCanceledCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

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


class _StopWaitCancelItemsCommand:
    """取消发起者结束等待：未结束项转 CANCELED 并保留取消效果。

    自身取消已生效后不再向目标施加取消；尚未结束的项保留已生效责
    任（效果不改写），由目标所属流程独立继续。
    """

    def __init__(self, command: StopWaitCancelItems, key: OperationKey) -> None:
        if not isinstance(command, StopWaitCancelItems):
            raise TypeError("结束等待申请必须使用 StopWaitCancelItems")
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
        action = self._load_action(connection)
        items = {}
        with closing(connection.execute(
            "SELECT id FROM cancel_items WHERE action_id = ? ORDER BY id",
            (command.action_id,),
        )) as cursor:
            ids = tuple(int(row[0]) for row in cursor.fetchall())
        for item_id in ids:
            item = row_facts(connection, "cancel_items", item_id)
            if item is not None:
                items[item_id] = item
        self._state["cancel_items"] = dict(items)
        waiting = [
            item for item in items.values()
            if item["status"] in (_ITEM_PENDING, _ITEM_RUNNING)]
        if not waiting:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY, ()))
        rows = tuple(
            _update("cancel_items", item["id"],
                    {"status": item["status"]},
                    {"status": _ITEM_CANCELED})
            for item in waiting)
        for item in waiting:
            self._owners[("cancel_items", item["id"])] = (
                "action", command.action_id)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _CANCEL_CHANGED_EVENT, _CANCEL_STOP_WAIT_REASON, rows,
            command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CancelTargetsSaved(
                CancelTargetsDisposition.SAVED,
                tuple(item["id"] for item in waiting)))

    def _load_action(self, connection):
        action = row_facts(connection, "actions", self._command.action_id)
        if action is None:
            raise ConsistencyError(
                f"取消动作不存在: {self._command.action_id}")
        self._state["actions"] = {self._command.action_id: dict(action)}
        if action["type"] != _CANCEL_ACTION_TYPE:
            raise ConsistencyError("结束等待要求取消动作")
        if not action["cancel_requested"]:
            raise TransactionError("结束等待要求自身取消已生效")
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError("结束等待要求取消动作执行中")
        if action["target_selection_state"] != _TARGET_FIXED:
            raise ConsistencyError("结束等待要求目标集合已固定")
        return action

    def _reuse(self, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds != [(_CANCEL_CHANGED_EVENT, _CANCEL_STOP_WAIT_REASON)]:
            raise TransactionError("原事务不是结束等待，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("结束等待的事实时刻与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CancelTargetsSaved(CancelTargetsDisposition.ALREADY, ()))


class _FinishOriginCanceledCommand:
    """取消发起者以 canceled 结束（ACTION_FINISHED.CANCEL）。

    自身取消已生效、全部成员终态（结束等待或逐项结果）后保存；已
    生效目标的责任由目标流程独立继续，不由本终态撤销。
    """

    def __init__(self, command: FinishCancelAction, key: OperationKey) -> None:
        if not isinstance(command, FinishCancelAction):
            raise TypeError("取消收场申请必须使用 FinishCancelAction")
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
                f"取消动作不在执行中: {command.action_id}")
        if not action["cancel_requested"]:
            raise TransactionError(
                f"自身取消未生效的取消动作不能按取消终态收场:"
                f" {command.action_id}")
        items = _load_all_items(connection, self._state, self._owners,
                                command.action_id)
        if action["target_selection_state"] == _TARGET_PENDING:
            if items:
                raise ConsistencyError("待固定取消动作不能已有成员")
        elif action["target_selection_state"] != _TARGET_FIXED or not items:
            raise TransactionError("取消收场要求可靠待固定空集合或已固定非空集合")
        for item in items.values():
            if item["status"] in (_ITEM_PENDING, _ITEM_RUNNING):
                raise TransactionError(
                    f"仍有未结束的取消成员，先结束等待: {item['id']}")
        succeeded = sum(1 for item in items.values()
                        if item["status"] == _ITEM_SUCCEEDED)
        failed = sum(1 for item in items.values()
                     if item["status"] == _ITEM_FAILED)
        siblings = _load_siblings(connection, self._state,
                                  action["plan_id"])
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        templates = [(
            _ACTION_FINISHED_EVENT, 4,
            (_update("actions", command.action_id,
                     {"status": action["status"]},
                     {"status": _ACTION_CANCELED}),),
        )]
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        plan_status = plan["status"]
        if _siblings_complete(siblings, command.action_id) \
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
                action_status=_ACTION_CANCELED, plan_status=plan_status,
                succeeded=succeeded, failed=failed),
        )

    def _recover(self, connection, action) -> CommandPlan:
        if action["status"] != _ACTION_CANCELED:
            raise TransactionError(
                f"取消动作终态不是取消收场结果: {action['status']!r}")
        _load_all_items(connection, self._state, self._owners, action["id"])
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return _origin_result_from(self._state, self._owners, action,
                                   plan["status"],
                                   CancelActionDisposition.ALREADY)

    def _reuse(self, connection, saved) -> CommandPlan:
        kinds = [(event["type"], event["reason"]) for event in saved]
        if not kinds or kinds[0] != (_ACTION_FINISHED_EVENT, 4) \
                or kinds[1:] not in ([], [(_PLAN_STATUS_EVENT, 2)]):
            raise TransactionError("原事务不是取消收场登记，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("取消收场的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" \
                or action_row["id"] != self._command.action_id:
            raise TransactionError("原取消收场属于其他动作")
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        _load_all_items(connection, self._state, self._owners,
                        self._command.action_id)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return _origin_result_from(self._state, self._owners, action,
                                   plan["status"],
                                   CancelActionDisposition.ALREADY)


def _load_all_items(connection, state, owners, action_id):
    with closing(connection.execute(
        "SELECT id FROM cancel_items WHERE action_id = ? ORDER BY id",
        (action_id,),
    )) as cursor:
        ids = tuple(int(row[0]) for row in cursor.fetchall())
    items = {}
    for item_id in ids:
        item = row_facts(connection, "cancel_items", item_id)
        if item is None:
            raise ConsistencyError("完整取消成员查询与原行读取矛盾")
        items[item_id] = item
        owners[("cancel_items", item_id)] = ("action", action_id)
    state["cancel_items"] = dict(items)
    return items


def _load_siblings(connection, state, plan_id):
    with closing(connection.execute(
        "SELECT id FROM actions WHERE plan_id=?", (plan_id,),
    )) as cursor:
        siblings = {
            row[0]: row_facts(connection, "actions", row[0])
            for row in cursor}
    siblings = {key: value for key, value in siblings.items() if value}
    state["actions"].update(siblings)
    return siblings


def _siblings_complete(siblings, current_action_id: int) -> bool:
    return all(
        values.get("status") in _ACTION_TERMINAL
        or values["id"] == current_action_id
        for values in siblings.values())


def _origin_result_from(state, owners, action, plan_status, disposition):
    items = state["cancel_items"]
    succeeded = sum(1 for item in items.values()
                    if item["status"] == _ITEM_SUCCEEDED)
    failed = sum(1 for item in items.values()
                 if item["status"] == _ITEM_FAILED)
    return CommandPlan(
        events=(), owners=owners, state_rows=state, read_only=True,
        result=CancelActionFinished(
            disposition=disposition, action_status=action["status"],
            plan_status=plan_status, succeeded=succeeded, failed=failed))


def _outcome_of(receipt) -> DbOutcome[CancelTargetsSaved]:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _saved_cancel_item_rows(connection, action_id: int) -> tuple[int, ...]:
    """取消动作已保存成员的行编号（按创建顺序）。"""
    with closing(connection.execute(
        "SELECT id FROM cancel_items WHERE action_id = ? ORDER BY id",
        (action_id,),
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












def include_photo_entry_rows(connection, action, state, owners):
    """为写入守卫提供原 START 的完整行范围，而非可猜测的资格布尔值。"""
    ranges = {('device_activities', 'action_id'): frozenset([action['id']]),
              ('operation_runs', 'action_id'): frozenset([action['id']])}
    for table in ('device_activities', 'operation_runs'):
        for identity, in connection.execute(f'SELECT id FROM {table} WHERE action_id=?', (action['id'],)):
            row = row_facts(connection, table, identity)
            state.setdefault(table, {})[identity] = row
            owners[(table, identity)] = ('action', action['id'])
            if table == 'operation_runs':
                target_id = row['activity_id']
                if target_id is not None:
                    target = row_facts(connection, 'device_activities', target_id)
                    if target is None:
                        raise ConsistencyError('原操作目标活动不存在')
                    state.setdefault('device_activities', {})[target_id] = target
                    owners[('device_activities', target_id)] = ('action', target['action_id'])
                    ranges.setdefault(('device_activities', 'id'), set()).add(target_id)
                ranges.setdefault(('operation_attempts', 'run_id'), set()).add(identity)
                for attempt_id, in connection.execute('SELECT id FROM operation_attempts WHERE run_id=?', (identity,)):
                    state.setdefault('operation_attempts', {})[attempt_id] = row_facts(connection, 'operation_attempts', attempt_id)
                    owners[('operation_attempts', attempt_id)] = ('action', action['id'])
    return ReadCoverage({key: frozenset(value) for key, value in ranges.items()})


class _FixPhotoEntriesCommand:
    """只组合已封住入口的 Hasselblad 原结果、固定集合与源取消。"""

    def __init__(self, command, key, reserved_entries, database_id):
        self.command, self.key = command, key
        self.reserved_entries, self.database_id = dict(reserved_entries), database_id

    def _request(self):
        from dataclasses import asdict
        return {'action_id': self.command.action_id, 'occurred_at': self.command.occurred_at,
                'targets': [{'action_id': t.action_id, 'basis': t.basis.value,
                    'effect': t.cancellation_effect.value} for t in self.command.targets.targets],
                'blocks': [asdict(value) for value in self.command.entry_blocks]}

    def plan(self, scope):
        from camctl.devices.start_entry import EntryPhase, PREVENTED, is_hasselblad_photo
        from camctl.devices.evidence import EvidenceRegistry
        from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, ErrorValue, Settlement, SettlementBasis, EvidenceValue
        from camctl.operations.attempts import AttemptFinish
        from camctl.operations.validation import validate_outcome
        from camctl.persistence.repositories.capture import (
            _CompositeScope, _FinishStartResultCommand, ActivityObservationSave, _merged_state_rows)
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            fixed = [event for event in saved if event['type'] == _TARGETS_FIXED_EVENT]
            if len(fixed) != 1 or not json_equal(fixed[0]['body']['evidence'].get('photo_entry_request'), self._request()):
                raise TransactionError('原入口取消复合申请内容不同')
            result = _FixCancelTargetsCommand(self.command, self.key)._reuse(scope.connection, fixed)
            return result
        origin = row_facts(scope.connection, 'actions', self.command.action_id)
        if _cancel_origin_abandoned(origin):
            return CommandPlan((), {}, {}, CancelTargetsSaved(CancelTargetsDisposition.ABANDONED), read_only=True)
        plans = []
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        for block in self.command.entry_blocks:
            phase = self.reserved_entries.get(block)
            target = row_facts(scope.connection, 'actions', block.action_id)
            if (block.database_id != self.database_id or phase is not EntryPhase.RESERVED
                    or not is_hasselblad_photo(target) or target['cancel_requested']
                    or target['status'] not in (1,2)):
                raise ConsistencyError('取消原 START 缺少匹配且已封住的未进入许可')
            facts = load_start_facts(scope.connection, target)
            if block.ticket is None:
                if facts.attempts_used:
                    raise ConsistencyError('无票据取消不能覆盖已有 START 意图')
                continue
            if (facts.run is None or facts.run['id'] != block.ticket.run_id
                    or facts.run['activity_id'] != int(block.ticket.target_id)
                    or facts.run['action_id'] != block.action_id
                    or facts.attempts_used != block.ticket.attempt_id):
                raise ConsistencyError('入口阻止票据与原 START 身份不符')
            outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                error=ErrorValue('canceled', 'dispatch'), observations=(),
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue.of(PREVENTED)))
            finish = AttemptFinish(block.ticket, validate_outcome(block.ticket, outcome, EvidenceRegistry((PREVENTED,))),
                                   self.command.occurred_at)
            plans.append(_FinishStartResultCommand(finish,
                ActivityObservationSave(block.action_id, self.command.occurred_at, dispatch_state=1),
                None, None, None, self.key).plan(sub))
        result_events = tuple(event for plan in plans for event in plan.events)
        fixed = _FixCancelTargetsCommand(self.command, self.key,
            reserved_sources=frozenset(block.action_id for block in self.command.entry_blocks)).plan(sub)
        fixed = replace(fixed, events=tuple(replace(event,
            evidence={'photo_entry_request': self._request()}) for event in fixed.events))
        plans.append(fixed)
        created = {row.after.values['target_action_id']: {'id': row.row_id, **row.after.values}
                   for event in fixed.events for row in event.rows if row.table == 'cancel_items'}
        prior_pre_start_ids = set()
        for block in self.command.entry_blocks:
            item = created[block.action_id]
            plans.append(_ApplyCancelTargetCommand(ApplyCancelTarget(item['id'], CancelApplyMode.PRE_START,
                self.command.occurred_at), self.key, fixed_item=item,
                result_events=result_events,
                prior_pre_start_ids=frozenset(prior_pre_start_ids)).plan(sub))
            if row_facts(scope.connection, "actions", block.action_id)["status"] == 1:
                prior_pre_start_ids.add(block.action_id)
        events = tuple(event for plan in plans for event in plan.events)
        scope.allocate(len(events))
        state, owners, ranges = {}, {}, {}
        for plan in plans:
            state = _merged_state_rows(state, plan.state_rows)
            owners.update(plan.owners)
            for key, values in plan.read_coverage.ranges.items():
                ranges.setdefault(key,set()).update(values)
        from camctl.history.reads import PlanReadScope
        source_plan_id = row_facts(scope.connection, "actions", self.command.entry_blocks[0].action_id)["plan_id"]
        return CommandPlan(events=events, state_rows=state, owners=owners, result=fixed.result,
                           read_coverage=ReadCoverage({key:frozenset(value) for key,value in ranges.items()}),
                           plan_read_scope=PlanReadScope(source_plan_id, 128))
















def load_eligibility_facts(connection, action_id: int, *, motor_permit=None):
    """读取目标持久资格事实，不读取会话入口许可。"""
    return load_eligibility_inputs(connection, action_id, motor_permit=motor_permit).facts


def load_eligibility_inputs(connection, action_id: int, *, motor_permit=None):
    """读取目标动作及其直接事实，交给业务规则装配取消资格。"""
    from camctl.cancellation.rules import (LoadedEligibility, action_eligibility_facts,
        capture_eligibility_facts, dispatch_phase, needs_capture_start_facts)
    from camctl.persistence.repositories.capture_facts import load_start_facts
    from camctl.persistence.repositories.transactions import row_facts
    action = row_facts(connection, "actions", action_id)
    if action is None:
        raise ConsistencyError(f"取消目标动作不存在: {action_id}")
    kind = action["type"]
    if kind == 8:
        from camctl.persistence.repositories.motor import read_motor_facts
        facts = read_motor_facts(connection, action_id)
        return LoadedEligibility(action, action_eligibility_facts(
            action, motor_facts=facts, motor_permit=motor_permit))
    if kind not in (1, 2, 3):
        return LoadedEligibility(action, action_eligibility_facts(action))
    with closing(connection.execute("SELECT id FROM device_activities WHERE action_id = ?", (action_id,))) as cursor:
        found = cursor.fetchone()
    activity = None if found is None else row_facts(connection, "device_activities", int(found[0]))
    not_started = None
    entered_dispatch = None
    if needs_capture_start_facts(action):
        start = load_start_facts(connection, action)
        from camctl.devices.start_entry import is_hasselblad_photo
        not_started = start.not_started
        if is_hasselblad_photo(action):
            entered_dispatch = dispatch_phase(activity)
    return LoadedEligibility(action, capture_eligibility_facts(
        action, activity, not_started=not_started), entered_dispatch)
