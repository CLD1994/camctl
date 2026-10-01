"""取回来源固定与逐来源选择的持久化事务。

SOURCE_RESOLVED 在取得执行资格后一次固定跨计划来源成员（或可
靠失败），每个固定成员在同一事件共同初始化 PENDING 选择；
TARGETS_FIXED 将一个来源的完整选择（含合法空选择与逐项失败）与
FIXED 状态同一事务保存。已固定的来源与选择不因重送、重启或新文
件出现而重选或扩大；此前已可靠确认存在的产物记录缺失按状态库
矛盾拒绝，不解释为普通不存在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.history.validators import EventValidationError, register_guard
from camctl.outputs.catalog import OutputKind
from camctl.outputs.sources import (
    ActionFacts,
    CatalogEntry,
    ResolutionState,
    SelectionFacts,
    SelectionMode,
    SelectionSnapshot,
    SelectedItem,
    SourceResolution,
    SourceSpec,
    SELECTION_NO_OUTPUTS,
    resolve_source,
    select_outputs,
)
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
from camctl.contracts.workflow_errors import action_error_id

_SOURCE_RESOLVED_EVENT = 3
_TARGETS_FIXED_EVENT = 4
_ACTION_STARTED_EVENT = 5

_FIX_REASON = 1
_FAIL_REASON = 2
_OBTAIN_REASON = 1

_RESOLUTION_STATE = enum_for("actions.source_resolution_state")
_ACTION_STATUS = enum_for("actions.status")
_ACTION_TYPE = enum_for("actions.type")
_SELECTION_STATUS = enum_for("obtain_source_selections.status")
_ITEM_STATUS = enum_for("obtain_items.status")
_ITEM_BASIS = enum_for("obtain_items.basis")

_ACTION_TERMINAL = (
    int(_ACTION_STATUS.SUCCEEDED),
    int(_ACTION_STATUS.FAILED),
    int(_ACTION_STATUS.EXPIRED),
    int(_ACTION_STATUS.CANCELED),
)
_OBTAIN_TYPE = int(_ACTION_TYPE.OBTAIN_ACTION_OUTPUTS)
_CAPTURE_TYPES = frozenset(
    int(member.value)
    for member in _ACTION_TYPE
    if member.name in {"CAMERA_TAKE_PHOTO", "CAMERA_RECORD", "CAMERA_TIMELAPSE"}
)
_SOURCE_RESOLUTION_FAILED_CODE = action_error_id("source_resolution_failed")

#: 录像处理中仍未结束的状态；存在即表示适用产物处理未完成。
_UNFINISHED_PROCESSING_CHECK = frozenset({int(enum_for("recording_processing.check_state").RUNNING)})
_UNFINISHED_PROCESSING_REPAIR = frozenset(
    {int(member) for member in enum_for("recording_processing.repair_state")
     if member.name in {"PENDING", "RUNNING"}}
)
_UNFINISHED_PROCESSING_DISCARD = frozenset(
    {int(member) for member in enum_for("recording_processing.discard_state")
     if member.name in {"PENDING", "RUNNING"}}
)

_KIND_BY_CODE = {
    int(code): kind
    for code, kind in (
        (1, OutputKind.ORIGINAL),
        (2, OutputKind.REPAIRED),
        (3, OutputKind.PREVIEW),
    )
}


@dataclass(frozen=True)
class ResolveSources:
    """一次执行期来源解析的输入。"""

    action_id: int
    spec: SourceSpec
    occurred_at: int


@dataclass(frozen=True)
class ResolveSourcesOutcome:
    """来源解析的已提交结果。"""

    fixed: bool
    member_action_ids: tuple[int, ...] = ()
    selection_ids: tuple[int, ...] = ()
    source_plan_id: int | None = None
    error_code: int | None = None
    error_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class FixSelection:
    """一次选择固定的输入。"""

    selection_id: int
    snapshot: SelectionSnapshot
    occurred_at: int


@dataclass(frozen=True)
class SelectionSaved:
    """选择保存结果。"""

    selection_id: int
    snapshot: SelectionSnapshot


class SqliteSourceLookup:
    """来源解析在 SQLite 上的只读查询实现。"""

    def __init__(self, connection, action_id: int) -> None:
        self._connection = connection
        self._action_id = action_id

    def owner_plan_id(self) -> int:
        row = self._connection.execute(
            "SELECT plan_id FROM actions WHERE id = ?", (self._action_id,)
        ).fetchone()
        if row is None:
            raise TransactionError(f"取回动作不存在: {self._action_id}")
        return int(row[0])

    def plan_exists(self, plan_id: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return row is not None

    def action_by_id(self, action_id: int) -> ActionFacts | None:
        row = self._connection.execute(
            "SELECT id, plan_id, type, name, group_name FROM actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return ActionFacts(
            action_id=int(row[0]),
            plan_id=int(row[1]),
            action_type=int(row[2]),
            name=row[3],
            group_name=row[4],
        )

    def plan_actions(self, plan_id: int) -> tuple[ActionFacts, ...]:
        rows = self._connection.execute(
            "SELECT id, plan_id, type, name, group_name FROM actions"
            " WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        return tuple(
            ActionFacts(
                action_id=int(row[0]),
                plan_id=int(row[1]),
                action_type=int(row[2]),
                name=row[3],
                group_name=row[4],
            )
            for row in rows
        )


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


class _ResolveSourcesCommand:
    """执行期来源解析的完整事务命令。"""

    def __init__(self, command: ResolveSources, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        # 报告关联解析沿登记路由读取各表事实，预置空映射。
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "actions": {},
            "action_dependencies": {},
            "obtain_source_selections": {},
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise TransactionError(f"取回动作不存在: {command.action_id}")
        if action["type"] != _OBTAIN_TYPE:
            raise TransactionError(f"来源解析只适用于取回动作: {command.action_id}")
        state = action["source_resolution_state"]
        if state == int(_RESOLUTION_STATE.FIXED):
            # 已固定集合不重新解析或扩大：按已保存事实返回。
            return CommandPlan(
                events=(),
                owners=self._owners,
                state_rows=self._state,
                result=self._saved_outcome(connection),
                read_only=True,
            )
        if state == int(_RESOLUTION_STATE.FAILED):
            return CommandPlan(
                events=(),
                owners=self._owners,
                state_rows=self._state,
                result=ResolveSourcesOutcome(
                    fixed=False,
                    error_code=action["error_code"],
                    error_details=_decoded(action["error_details_json"]),
                ),
                read_only=True,
            )
        if state != int(_RESOLUTION_STATE.PENDING):
            raise TransactionError(
                f"来源解析状态不可解释: {command.action_id} {state!r}"
            )
        if action["status"] != int(_ACTION_STATUS.RUNNING) or not action["execution_started"]:
            raise TransactionError("执行期来源解析要求取回动作处于执行中")
        self._state["actions"] = {command.action_id: action}

        resolution = resolve_source(
            command.spec, SqliteSourceLookup(connection, command.action_id)
        )
        if resolution.state == ResolutionState.FAILED:
            error_code, details = resolution.action_error(command.spec)
            before = {
                "source_resolution_state": action["source_resolution_state"],
                "status": action["status"],
                "error_code": action["error_code"],
                "error_details_json": action["error_details_json"],
            }
            after = {
                "source_resolution_state": int(_RESOLUTION_STATE.FAILED),
                "status": int(_ACTION_STATUS.FAILED),
                "error_code": error_code,
                "error_details_json": details,
            }
            self._owners[("actions", command.action_id)] = (
                "action", command.action_id,
            )
            allocation = scope.allocate(1)
            event = _envelope(
                allocation.first_event_id,
                allocation.txn_id,
                _SOURCE_RESOLVED_EVENT,
                _FAIL_REASON,
                (
                    _update("actions", command.action_id, before, after),
                ),
                command.occurred_at,
            )
            return CommandPlan(
                events=(event,),
                owners=self._owners,
                state_rows=self._state,
                result=ResolveSourcesOutcome(
                    fixed=False, error_code=error_code, error_details=details
                ),
            )

        rows = []
        member_state = dict(self._state["actions"])
        for member_id in resolution.member_action_ids:
            member = row_facts(connection, "actions", member_id)
            if member is None:
                raise ConsistencyError(f"来源成员动作不存在: {member_id}")
            member_state[member_id] = member
        self._state["actions"] = member_state
        before = {
            "source_resolution_state": action["source_resolution_state"],
            "resolved_source_plan_id": action["resolved_source_plan_id"],
        }
        after = {
            "source_resolution_state": int(_RESOLUTION_STATE.FIXED),
            "resolved_source_plan_id": resolution.source_plan_id,
        }
        rows.append(_update("actions", command.action_id, before, after))
        self._owners[("actions", command.action_id)] = ("action", command.action_id)

        dependency_id = next_row_id(connection, "action_dependencies")
        selection_id = next_row_id(connection, "obtain_source_selections")
        selection_ids: list[int] = []
        for member_id in resolution.member_action_ids:
            rows.append(
                _row(
                    "action_dependencies",
                    dependency_id,
                    {
                        "action_id": command.action_id,
                        "depends_on_action_id": member_id,
                    },
                )
            )
            rows.append(
                _row(
                    "obtain_source_selections",
                    selection_id,
                    {
                        "dependency_id": dependency_id,
                        "status": int(_SELECTION_STATUS.PENDING),
                        "error_code": None,
                        "error_details_json": None,
                    },
                )
            )
            self._owners[("action_dependencies", dependency_id)] = (
                "action", command.action_id,
            )
            self._owners[("obtain_source_selections", selection_id)] = (
                "action", command.action_id,
            )
            selection_ids.append(selection_id)
            dependency_id += 1
            selection_id += 1
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _SOURCE_RESOLVED_EVENT,
            _FIX_REASON,
            tuple(rows),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=ResolveSourcesOutcome(
                fixed=True,
                member_action_ids=resolution.member_action_ids,
                selection_ids=tuple(selection_ids),
                source_plan_id=resolution.source_plan_id,
            ),
        )

    def _saved_outcome(self, connection) -> ResolveSourcesOutcome:
        members = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT depends_on_action_id FROM action_dependencies"
                " WHERE action_id = ? ORDER BY id",
                (self._command.action_id,),
            )
        )
        selections = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT s.id FROM obtain_source_selections s"
                " JOIN action_dependencies d ON d.id = s.dependency_id"
                " WHERE d.action_id = ? ORDER BY s.id",
                (self._command.action_id,),
            )
        )
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        return ResolveSourcesOutcome(
            fixed=True,
            member_action_ids=members,
            selection_ids=selections,
            source_plan_id=action["resolved_source_plan_id"],
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        resolved = [
            event
            for event in saved
            if event["type"] == _SOURCE_RESOLVED_EVENT
        ]
        if not resolved:
            raise TransactionError("操作身份已用于其他阶段，不能作为来源解析重送")
        body = resolved[0]["body"]
        if resolved[0]["reason"] == _FAIL_REASON:
            for row in body.get("rows", []):
                if row["table"] == "actions" and row["after"]["exists"]:
                    values = row["after"]["values"]
                    return CommandPlan(
                        events=(),
                        owners=self._owners,
                        state_rows=self._state,
                        result=ResolveSourcesOutcome(
                            fixed=False,
                            error_code=values.get("error_code"),
                            error_details=_decoded(values.get("error_details_json")),
                        ),
                        read_only=True,
                    )
            raise TransactionError("来源解析失败事件缺少动作行")
        members: list[int] = []
        selections: list[int] = []
        source_plan_id: int | None = None
        for row in body.get("rows", []):
            if not row["after"]["exists"]:
                continue
            values = row["after"]["values"]
            if row["table"] == "action_dependencies":
                members.append(int(values["depends_on_action_id"]))
            elif row["table"] == "obtain_source_selections":
                selections.append(int(row["id"]))
            elif row["table"] == "actions":
                source_plan_id = values.get("resolved_source_plan_id")
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            result=ResolveSourcesOutcome(
                fixed=True,
                member_action_ids=tuple(members),
                selection_ids=tuple(selections),
                source_plan_id=source_plan_id,
            ),
            read_only=True,
        )


class _FixSelectionCommand:
    """固定一个来源完整选择的完整事务命令。"""

    def __init__(self, command: FixSelection, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "actions": {},
            "action_dependencies": {},
            "obtain_source_selections": {},
            "obtain_items": {},
            "outputs": {},
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved, connection)
        command = self._command
        selection = row_facts(connection, "obtain_source_selections", command.selection_id)
        if selection is None:
            raise TransactionError(f"来源选择不存在: {command.selection_id}")
        dependency = row_facts(
            connection, "action_dependencies", selection["dependency_id"]
        )
        if dependency is None:
            raise ConsistencyError(
                f"来源选择的依赖关系缺失: {command.selection_id}"
            )
        owner_action_id = dependency["action_id"]
        source_action_id = dependency["depends_on_action_id"]
        owner = row_facts(connection, "actions", owner_action_id)
        source = row_facts(connection, "actions", source_action_id)
        if owner is None or source is None:
            raise ConsistencyError(
                f"来源选择的动作事实缺失: 选择 {command.selection_id}"
            )
        self._state["obtain_source_selections"] = {command.selection_id: selection}
        self._state["action_dependencies"] = {
            selection["dependency_id"]: dependency
        }
        self._state["actions"] = {owner_action_id: owner, source_action_id: source}

        if selection["status"] == int(_SELECTION_STATUS.FIXED):
            # 已固定选择不重选：按已保存条目返回。
            return CommandPlan(
                events=(),
                owners=self._owners,
                state_rows=self._state,
                result=SelectionSaved(
                    selection_id=command.selection_id,
                    snapshot=SelectionSnapshot(
                        is_fixed=True,
                        items=tuple(_saved_items(connection, command.selection_id)),
                        source_error_code=selection["error_code"],
                    ),
                ),
                read_only=True,
            )
        if selection["status"] != int(_SELECTION_STATUS.PENDING):
            raise TransactionError(
                f"来源选择状态不可解释: {command.selection_id}"
                f" {selection['status']!r}"
            )
        snapshot = command.snapshot
        if not snapshot.is_fixed:
            raise TransactionError("未完成选择不能固定")
        if owner["status"] != int(_ACTION_STATUS.RUNNING) or owner["cancel_requested"]:
            raise TransactionError("选择固定要求取回动作执行中且未取消")
        if source["status"] not in _ACTION_TERMINAL:
            raise TransactionError(
                f"来源动作未终态不能固定选择: {source_action_id}"
            )
        if not _processing_completed(connection, source_action_id):
            raise TransactionError(
                f"来源适用产物处理未完成: {source_action_id}"
            )

        self._owners[("obtain_source_selections", command.selection_id)] = (
            "action", owner_action_id,
        )
        for entry in _catalog_rows(connection, source_action_id):
            self._state.setdefault("outputs", {})[entry["id"]] = entry

        rows = []
        if snapshot.source_error_code is None:
            rows.append(
                _update(
                    "obtain_source_selections",
                    command.selection_id,
                    {
                        "status": selection["status"],
                        "error_code": selection["error_code"],
                        "error_details_json": selection["error_details_json"],
                    },
                    {
                        "status": int(_SELECTION_STATUS.FIXED),
                        "error_code": None,
                        "error_details_json": None,
                    },
                )
            )
        else:
            rows.append(
                _update(
                    "obtain_source_selections",
                    command.selection_id,
                    {
                        "status": selection["status"],
                        "error_code": selection["error_code"],
                        "error_details_json": selection["error_details_json"],
                    },
                    {
                        "status": int(_SELECTION_STATUS.FIXED),
                        "error_code": snapshot.source_error_code,
                        "error_details_json": _source_error_details(
                            snapshot.source_error_code, source_action_id
                        ),
                    },
                )
            )
        item_id = next_row_id(connection, "obtain_items")
        for item in snapshot.items:
            rows.append(
                _row("obtain_items", item_id, _item_values(command.selection_id, item))
            )
            self._owners[("obtain_items", item_id)] = ("action", owner_action_id)
            item_id += 1
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _TARGETS_FIXED_EVENT,
            _OBTAIN_REASON,
            tuple(rows),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=SelectionSaved(
                selection_id=command.selection_id, snapshot=snapshot
            ),
        )

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        fixed = [event for event in saved if event["type"] == _TARGETS_FIXED_EVENT]
        if not fixed:
            raise TransactionError("操作身份已用于其他阶段，不能作为选择固定重送")
        selection_id = self._command.selection_id
        selection = row_facts(connection, "obtain_source_selections", selection_id)
        if selection is None:
            raise ConsistencyError(
                f"已提交选择固定找不到来源选择: {selection_id}"
            )
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            result=SelectionSaved(
                selection_id=selection_id,
                snapshot=SelectionSnapshot(
                    is_fixed=True,
                    items=tuple(_saved_items(connection, selection_id)),
                    source_error_code=selection["error_code"],
                ),
            ),
            read_only=True,
        )


def _decoded(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _processing_completed(connection, source_action_id: int) -> bool:
    """来源动作的适用产物处理是否已完成（录像检查/修复/丢弃）。"""
    row = connection.execute(
        "SELECT check_state, repair_state, discard_state FROM recording_processing"
        " WHERE action_id = ?",
        (source_action_id,),
    ).fetchone()
    if row is None:
        return True
    check_state, repair_state, discard_state = (int(value) for value in row)
    return (
        check_state not in _UNFINISHED_PROCESSING_CHECK
        and repair_state not in _UNFINISHED_PROCESSING_REPAIR
        and discard_state not in _UNFINISHED_PROCESSING_DISCARD
    )


def _catalog_rows(connection, source_action_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT o.id, o.source_action_id, o.kind, o.device_file_id,"
        " o.intermediate_file_id, o.availability, orig.original_output_id,"
        " df.size_bytes AS device_size, im.size_bytes AS intermediate_size"
        " FROM outputs o"
        " LEFT JOIN output_origins orig ON orig.output_id = o.id"
        " LEFT JOIN device_files df ON df.id = o.device_file_id"
        " LEFT JOIN intermediate_files im ON im.id = o.intermediate_file_id"
        " WHERE o.source_action_id = ? ORDER BY o.id",
        (source_action_id,),
    ).fetchall()
    return [dict(zip(("id", "source_action_id", "kind", "device_file_id",
                      "intermediate_file_id", "availability", "original_output_id",
                      "device_size", "intermediate_size"), row)) for row in rows]


def load_selection_facts(
    connection,
    source_action_id: int,
    *,
    previously_confirmed_ids: frozenset[int] = frozenset(),
) -> SelectionFacts:
    """读取一个来源动作的选择事实。

    source_completed 由动作终态与适用产物处理共同推导；known 覆盖
    全目录以区分不存在与归属不匹配。
    """
    source = row_facts(connection, "actions", source_action_id)
    if source is None:
        raise ConsistencyError(f"来源动作不存在: {source_action_id}")
    completed = (
        source["status"] in _ACTION_TERMINAL
        and _processing_completed(connection, source_action_id)
    )
    entries = tuple(
        CatalogEntry(
            output_id=int(row["id"]),
            kind=_KIND_BY_CODE[int(row["kind"])],
            availability=int(row["availability"]),
            original_output_id=(
                int(row["original_output_id"])
                if row["original_output_id"] is not None
                else None
            ),
            size_bytes=(
                row["device_size"]
                if row["device_size"] is not None
                else row["intermediate_size"]
            ),
        )
        for row in _catalog_rows(connection, source_action_id)
    )
    known = frozenset(
        int(row[0]) for row in connection.execute("SELECT id FROM outputs")
    )
    return SelectionFacts(
        source_action_id=source_action_id,
        source_completed=completed,
        outputs=entries,
        known_output_ids=known,
        previously_confirmed_ids=previously_confirmed_ids,
    )


def _saved_items(connection, selection_id: int) -> list[SelectedItem]:
    rows = connection.execute(
        "SELECT requested_output_id, output_id, basis, original_output_id,"
        " preview_output_id, preview_size, repaired_size, status, error_code,"
        " error_details_json FROM obtain_items WHERE selection_id = ? ORDER BY id",
        (selection_id,),
    ).fetchall()
    items: list[SelectedItem] = []
    for row in rows:
        output_id = row[1]
        if output_id is not None:
            exists = connection.execute(
                "SELECT 1 FROM outputs WHERE id = ?", (output_id,)
            ).fetchone()
            if exists is None:
                raise ConsistencyError(
                    f"已保存取回项引用的产物记录缺失: 选择 {selection_id}"
                    f" 产物 {output_id}"
                )
        items.append(
            SelectedItem(
                basis=int(row[2]),
                status=int(row[7]),
                output_id=output_id,
                requested_output_id=row[0],
                original_output_id=row[3],
                preview_output_id=row[4],
                preview_size=row[5],
                repaired_size=row[6],
                error_code=row[8],
                error_details=_decoded(row[9]),
            )
        )
    return items


def load_selection(connection, selection_id: int) -> SelectionSnapshot:
    """读取一个来源选择的已保存快照；记录缺失按状态库矛盾拒绝。"""
    selection = row_facts(connection, "obtain_source_selections", selection_id)
    if selection is None:
        raise TransactionError(f"来源选择不存在: {selection_id}")
    if selection["status"] != int(_SELECTION_STATUS.FIXED):
        return SelectionSnapshot(is_fixed=False)
    return SelectionSnapshot(
        is_fixed=True,
        items=tuple(_saved_items(connection, selection_id)),
        source_error_code=selection["error_code"],
    )


def _item_values(selection_id: int, item: SelectedItem) -> dict[str, Any]:
    return {
        "selection_id": selection_id,
        "requested_output_id": item.requested_output_id,
        "output_id": item.output_id,
        "basis": int(item.basis),
        "original_output_id": item.original_output_id,
        "preview_output_id": item.preview_output_id,
        "preview_size": item.preview_size,
        "repaired_size": item.repaired_size,
        "status": int(item.status),
        "source_dependency": 0,
        "delivery_id": None,
        "error_code": item.error_code,
        "error_details_json": (
            dict(item.error_details) if item.error_details is not None else None
        ),
    }


def _source_error_details(code: int, source_action_id: int) -> dict[str, Any]:
    if code == SELECTION_NO_OUTPUTS:
        return {}
    return {"source_action_instance_id": source_action_id}


class OutputsRepository:
    """来源固定与选择的 SQLite 仓储。"""

    def resolve_sources(
        self, command: ResolveSources, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ResolveSourcesOutcome]:
        receipt = commit_operation(_ResolveSourcesCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def fix_selection(
        self, command: FixSelection, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[SelectionSaved]:
        receipt = commit_operation(_FixSelectionCommand(command, key), key, owned)
        return _outcome_of(receipt)


def _outcome_of(receipt) -> DbOutcome:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _selection_initialization_guard(event, context) -> None:
    """选择初始化守卫：每个固定来源恰好一条 PENDING 选择。

    执行期来源固定在 SOURCE_RESOLVED.FIX 内共同建立依赖与选择；
    取回开始（ACTION_STARTED）为受理时已固定的来源初始化选择。
    取消生效的取回不再普通初始化（由行规格 before 核对）。
    """
    created = [
        row
        for row in event.rows
        if row.table == "obtain_source_selections" and not row.before.exists
    ]
    if event.event_type == _SOURCE_RESOLVED_EVENT and event.reason == _FIX_REASON:
        dependencies = [
            row
            for row in event.rows
            if row.table == "action_dependencies" and not row.before.exists
        ]
        dependency_ids = {row.row_id for row in dependencies}
        if len(created) != len(dependency_ids):
            raise EventValidationError(
                f"来源固定必须为每个依赖恰好创建一条选择: 依赖 {sorted(dependency_ids)}"
                f" 选择 {len(created)} 条"
            )
    elif event.event_type == _ACTION_STARTED_EVENT:
        owner_id = None
        for row in event.rows:
            if row.table == "actions" and row.before.exists:
                owner_id = row.row_id
        if owner_id is None:
            return
        existing = {
            row_id
            for row_id, values in context.state_rows.get(
                "action_dependencies", {}
            ).items()
            if values.get("action_id") == owner_id
        }
        created_dependencies = {
            row.after.values["dependency_id"] for row in created
        }
        if len(created) != len(existing) or created_dependencies != existing:
            raise EventValidationError(
                f"动作开始必须为每个固定来源初始化恰好一条选择:"
                f" 依赖 {sorted(existing)} 选择 {sorted(created_dependencies)}"
            )
    else:
        if created:
            raise EventValidationError("只有来源固定或动作开始能初始化选择")
        return
    for row in created:
        after = row.after.values
        if after.get("status") != int(_SELECTION_STATUS.PENDING):
            raise EventValidationError("初始化的选择必须是 PENDING")
        if after.get("error_code") is not None or after.get("error_details_json") is not None:
            raise EventValidationError("初始化的选择不携带来源错误")


def _source_selection_guard(event, context) -> None:
    """逐来源选择守卫：PENDING 一次固定，条目与来源事实共同提交。"""
    if event.event_type != _TARGETS_FIXED_EVENT or event.reason != _OBTAIN_REASON:
        return
    selection_updates = [
        row
        for row in event.rows
        if row.table == "obtain_source_selections" and row.before.exists
    ]
    items = [
        row
        for row in event.rows
        if row.table == "obtain_items" and not row.before.exists
    ]
    if len(selection_updates) != 1:
        raise EventValidationError("选择固定必须恰好更新一条来源选择")
    selection = selection_updates[0]
    if selection.before.values.get("status") != int(_SELECTION_STATUS.PENDING):
        raise EventValidationError("只有 PENDING 选择能固定")
    dependency_id = None
    for row_id, values in context.state_rows.get(
        "obtain_source_selections", {}
    ).items():
        if row_id == selection.row_id:
            dependency_id = values.get("dependency_id")
    if dependency_id is None:
        # 命令须在 state_rows 提供选择事实。
        facts = _guard_facts(context, "obtain_source_selections", selection.row_id)
        dependency_id = facts.get("dependency_id")
    dependency = _guard_facts(context, "action_dependencies", int(dependency_id or 0))
    owner = _guard_facts(context, "actions", int(dependency.get("action_id") or 0))
    if owner.get("status") != int(_ACTION_STATUS.RUNNING) or owner.get("cancel_requested"):
        raise EventValidationError("选择固定要求取回动作执行中且未取消")
    source = _guard_facts(
        context, "actions", int(dependency.get("depends_on_action_id") or 0)
    )
    if source.get("status") not in _ACTION_TERMINAL:
        raise EventValidationError("选择固定要求来源动作已终态")
    error_code = selection.after.values.get("error_code")
    for item in items:
        values = item.after.values
        if values.get("selection_id") != selection.row_id:
            raise EventValidationError("选择条目必须属于所固定的来源选择")
        if values.get("delivery_id") is not None or values.get("source_dependency") != 0:
            raise EventValidationError("选择阶段不建立交付或源依赖")
        output_id = values.get("output_id")
        if output_id is not None:
            output = context.state_rows.get("outputs", {}).get(output_id)
            if output is None or output.get("source_action_id") != source.get("id"):
                raise EventValidationError(
                    f"选择条目引用不属于来源的产物: {output_id}"
                )
        status = values.get("status")
        basis = values.get("basis")
        if status == int(_ITEM_STATUS.UNRESOLVED) and (
            basis != int(_ITEM_BASIS.EXPLICIT) or output_id is not None
        ):
            raise EventValidationError("未决条目只适用于显式请求")
        if status == int(_ITEM_STATUS.FAILED) and values.get("error_code") is None:
            raise EventValidationError("失败条目必须携带错误")
        if status != int(_ITEM_STATUS.FAILED) and values.get("error_code") is not None:
            raise EventValidationError("只有失败条目携带错误")
    if error_code is not None and items:
        # 来源级错误表达整来源无可选目标，不与逐项选择并存。
        for item in items:
            if item.after.values.get("status") == int(_ITEM_STATUS.SELECTED):
                raise EventValidationError(
                    "来源级错误不能与已选中条目并存"
                )


def register_outputs_guards() -> None:
    """注册来源与选择事件的正式业务守卫（装配期调用）。"""
    register_guard("selection_initialization", _selection_initialization_guard)
    register_guard("source_selection", _source_selection_guard)
