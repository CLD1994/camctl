"""录像完成终态与正式产物登记的唯一事务。

经 P3 事务内核组织：动作成功终态、正式产物登记及父计划状态在
同一事务共同保存，任一写入失败整组回滚；登记前经 X1 纯规则校
验，事件守卫复核文件角色与初始可用性组合。停止、活动结束与产
物、动作结果分别保存，不互相混同。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

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

_ACTION_RUNNING = 2
_ACTION_SUCCEEDED = 3
_PLAN_COMPLETE = 3

#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

_KIND_CODES = {OutputKind.ORIGINAL: 1, OutputKind.REPAIRED: 2, OutputKind.PREVIEW: 3}

#: device_files.role 与产物种类的对应；修复产物承载于中间文件。
_FILE_ROLE_FOR_KIND = {1: 2, 3: 3}


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
    """产物文件身份与角色一致性校验。"""
    for row in event.rows:
        if row.table != "outputs" or row.before.exists:
            continue
        values = row.after.values
        kind = values.get("kind")
        device_file_id = values.get("device_file_id")
        if device_file_id is not None:
            facts = _guard_facts(context, "device_files", device_file_id)
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
        self._state["actions"] = self._sibling_actions(connection, action)
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
        changes = validate_output_registration(command.drafts, command.catalog_facts)
        file_ids = [
            output.device_file_id
            for output in changes.outputs
            if output.device_file_id is not None
        ]
        self._state["device_files"] = {}
        for file_id in file_ids:
            facts = row_facts(connection, "device_files", file_id)
            if facts is None:
                raise TransactionError(f"设备文件不存在: {file_id}")
            self._state["device_files"][file_id] = facts

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
            for values in self._state["actions"].values()
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

    def finish_capture(
        self, command: FinishCapture, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CaptureResult]:
        receipt = commit_operation(FinishCaptureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)
