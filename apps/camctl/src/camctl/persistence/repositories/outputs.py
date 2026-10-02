"""取回来源固定与逐来源选择的持久化事务。

SOURCE_RESOLVED 在取得执行资格后一次固定跨计划来源成员（或可
靠失败），每个固定成员在同一事件共同初始化 PENDING 选择；
TARGETS_FIXED 将一个来源的完整选择（含合法空选择与逐项失败）与
FIXED 状态同一事务保存。已固定的来源与选择不因重送、重启或新文
件出现而重选或扩大；此前已可靠确认存在的产物记录缺失按状态库
矛盾拒绝，不解释为普通不存在。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    PathRuleError, object_file_name, relative_file_path, validate_relative_file_path,
)
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
from camctl.contracts.workflow_errors import action_error_id, item_error_id
from camctl.outputs.qualification import (
    FileCandidate,
    FileQualification,
    OperationConfig,
    QualificationOutcome,
)

_SOURCE_RESOLVED_EVENT = 3
_TARGETS_FIXED_EVENT = 4
_ACTION_STARTED_EVENT = 5
_READ_PERMISSION_EVENT = 21
_COPY_CHANGED_EVENT = 22
_DELIVERY_CHANGED_EVENT = 23
_INTERMEDIATE_FILE_EVENT = 26
_OPERATION_CONFIGURED_EVENT = 10

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
_OUTPUT_KIND = enum_for("outputs.kind")
_DEVICE_FILE_ROLE = enum_for("device_files.role")
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

#: READ_PERMISSION_CHANGED 的分支。
_GRANT_REASON = 1
_REJECT_REASON = 2
#: 资格逐项最终失败的错误码（workflow-codes.json 权威装载）。
_OUTPUT_UNAVAILABLE_CODE = item_error_id("obtain_items", "output_unavailable")
_OUTPUT_CLEANUP_STARTED_CODE = item_error_id("obtain_items", "output_cleanup_started")
_SOURCE_FILE_UNCONFIRMED_CODE = item_error_id("obtain_items", "source_file_unconfirmed")

_RUN_KIND = enum_for("operation_runs.kind")
_RUN_STATUS = enum_for("operation_runs.status")
_AVAILABILITY = enum_for("outputs.availability")
_PRESENCE = enum_for("device_files.presence_state")
_COMPLETION = enum_for("device_files.completion_state")
_RESTRICTION = enum_for("cleanup_items.restriction_state")
_CLEANUP_STATUS = enum_for("cleanup_items.status")
_PURPOSE = enum_for("intermediate_files.purpose")
_RETENTION = enum_for("intermediate_files.retention_state")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")
_DELIVERY_STATUS = enum_for("deliveries.status")
_WITHDRAWAL = enum_for("deliveries.withdrawal_state")
_RESET_STATE = enum_for("file_copies.reset_state")
_VERIFICATION = enum_for("file_copies.verification_state")

#: 清理项仍对源产物构成读取限制的状态组合。
_ACTIVE_RESTRICTIONS = (
    int(_RESTRICTION.ACTIVE),
    int(_RESTRICTION.IRREVERSIBLE),
)
_ACTIVE_CLEANUP_STATUS = (
    int(_CLEANUP_STATUS.UNRESOLVED),
    int(_CLEANUP_STATUS.PENDING_DELETE),
    int(_CLEANUP_STATUS.DELETING),
)
#: 读取流程仍占用设备机会或源保护的状态。
_UNFINISHED_RUN_STATUS = (
    int(_RUN_STATUS.PENDING),
    int(_RUN_STATUS.ACTIVE),
)


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
    if not isinstance(raw, str):
        return dict(raw)
    try:
        return parse_exact_json(raw)
    except ValueError as error:
        raise ConsistencyError("已保存产物选择的错误详情不是有效精确 JSON") from error


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


class _GrantFileCommand:
    """读取资格授予的完整事务命令。

    一个事务内核对全部可靠限制与业务顺序：授予时目标文件、交
    付（取回分支）、读取流程、拷贝及取回项更新按五（或内部处理
    三）个事件共同建档；清理限制与产物不可用保存逐项最终失败；
    排序阻挡与设备占用作为可等待拒绝返回，不落库。
    """

    #: 命令涉及并供守卫与报告关联读取的表。
    _TABLES = (
        "actions",
        "action_dependencies",
        "obtain_source_selections",
        "obtain_items",
        "outputs",
        "device_files",
        "cleanup_items",
        "recording_processing",
        "deliveries",
        "file_copies",
        "operation_runs",
        "intermediate_files",
    )

    def __init__(self, command: FileCandidate, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        if not isinstance(self._command, FileCandidate):
            raise TypeError("读取资格申请必须使用 FileCandidate")
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        command = self._command
        action = self._required(connection, "actions", command.action_id)
        is_device_source = command.source_device_file_id is not None
        source_file = self._required(
            connection, "device_files" if is_device_source else "intermediate_files",
            command.source_device_file_id if is_device_source else command.source_intermediate_file_id,
        )
        output = self._load_target(connection, action, source_file)
        device_id = self._source_device(connection, source_file) if is_device_source else None
        if action["status"] != int(_ACTION_STATUS.RUNNING) or action["cancel_requested"]:
            return self._wait("action_not_eligible")
        if command.item_id is not None:
            item = self._state["obtain_items"][command.item_id]
            if item["status"] == int(_ITEM_STATUS.DELIVERY_CREATED):
                return self._item_already_granted(connection, item)
            if item["status"] != int(_ITEM_STATUS.SELECTED):
                return CommandPlan(
                    events=(),
                    owners=self._owners,
                    state_rows=self._state,
                    read_only=True,
                    result=FileQualification(
                        outcome=QualificationOutcome.REJECTED_FINAL,
                        copy_id=None,
                        run_id=None,
                        delivery_id=None,
                        target_file_id=None,
                        reason="item_finished",
                    ),
                )
        final_error = self._final_rejection(connection, output, source_file)
        if final_error is not None:
            return self._reject_item(scope, final_error)

        if device_id is not None and self._device_busy(connection, device_id):
            return self._wait("device_busy")
        if device_id is not None and self._source_protected(connection, command.source_device_file_id):
            return self._wait("source_protected")
        if device_id is not None and not self._wins_business_order(connection, action, device_id):
            return self._wait("business_order")

        return self._grant(scope, source_file, device_id)

    # ---- 资格核对 ----

    def _required(self, connection, table: str, row_id: int) -> dict:
        facts = row_facts(connection, table, row_id)
        if facts is None:
            raise ConsistencyError(f"读取责任关联记录缺失: {table}#{row_id}")
        self._state[table][row_id] = facts
        return facts

    def _load_target(self, connection, action, source_file) -> dict | None:
        """先核对固定归属，再由调用方判断当前执行状态。"""
        command = self._command
        if command.item_id is None:
            processing = self._required(connection, "recording_processing", command.processing_id)
            if (
                action["type"] != int(_ACTION_TYPE.CAMERA_RECORD)
                or processing["action_id"] != command.action_id
                or processing["source_device_file_id"] != command.source_device_file_id
                or source_file["source_action_id"] != command.action_id
                or source_file["role"] != int(_DEVICE_FILE_ROLE.ORIGINAL)
            ):
                raise ConsistencyError("内部读取的录像、处理责任与原片归属不一致")
            return None

        item = self._required(connection, "obtain_items", command.item_id)
        selection = self._required(connection, "obtain_source_selections", item["selection_id"])
        dependency = self._required(connection, "action_dependencies", selection["dependency_id"])
        output = self._required(connection, "outputs", command.output_id)
        source_action = self._required(connection, "actions", dependency["depends_on_action_id"])
        source_role = {
            int(_OUTPUT_KIND.ORIGINAL): int(_DEVICE_FILE_ROLE.ORIGINAL),
            int(_OUTPUT_KIND.PREVIEW): int(_DEVICE_FILE_ROLE.PREVIEW),
        }.get(output["kind"])
        if (
            action["type"] != _OBTAIN_TYPE
            or action["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED)
            or action["resolved_source_plan_id"] != source_action["plan_id"]
            or dependency["action_id"] != command.action_id
            or selection["status"] != int(_SELECTION_STATUS.FIXED)
            or item["output_id"] != command.output_id
            or dependency["depends_on_action_id"] != output["source_action_id"]
            or output["device_file_id"] != command.source_device_file_id
            or output["intermediate_file_id"] != command.source_intermediate_file_id
        ):
            raise ConsistencyError("取回动作、固定选择、产物与源文件归属不一致")
        if command.source_device_file_id is not None:
            if (source_role is None or source_file["role"] != source_role
                    or output["source_action_id"] != source_file["source_action_id"]):
                raise ConsistencyError("设备产物与源文件角色或归属不一致")
        elif (
            output["kind"] != int(_OUTPUT_KIND.REPAIRED)
            or source_action["type"] != int(_ACTION_TYPE.CAMERA_RECORD)
            or source_file["owner_action_id"] != output["source_action_id"]
            or source_file["owner_delivery_id"] is not None
            or source_file["purpose"] != int(_PURPOSE.REPAIR_OUTPUT)
            or source_file["retention_state"] != int(_RETENTION.PROMOTED)
            or source_file["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)
            or source_file["size_bytes"] is None
        ):
            raise ConsistencyError("主机产物必须关联原录像已提升的完整修复文件")
        if command.source_intermediate_file_id is not None:
            try:
                validate_relative_file_path(
                    FilePurpose.REPAIR_OUTPUT, command.source_intermediate_file_id,
                    source_file["relative_path"],
                )
            except PathRuleError as error:
                raise ConsistencyError("主机源文件的保存路径与固定身份不一致") from error
        return output

    def _final_rejection(self, connection, output, source_file) -> int | None:
        """不可授予的最终失败错误码；None 表示可通过。"""
        if output is not None and output["availability"] != int(_AVAILABILITY.AVAILABLE):
            return _OUTPUT_UNAVAILABLE_CODE
        if self._command.source_device_file_id is not None and (
            source_file["presence_state"] != int(_PRESENCE.PRESENT)
            or source_file["completion_state"] != int(_COMPLETION.COMPLETE)
        ):
            return _SOURCE_FILE_UNCONFIRMED_CODE
        if output is None:
            return None
        cleanup = connection.execute(
            "SELECT status FROM cleanup_items"
            " WHERE output_id = ? AND restriction_state IN (?, ?)"
            " AND status IN (?, ?, ?) LIMIT 1",
            (
                self._command.output_id,
                *_ACTIVE_RESTRICTIONS,
                *_ACTIVE_CLEANUP_STATUS,
            ),
        ).fetchone()
        if cleanup is not None:
            if cleanup[0] == int(_CLEANUP_STATUS.DELETING):
                return _OUTPUT_CLEANUP_STARTED_CODE
            return _OUTPUT_UNAVAILABLE_CODE
        return None

    def _source_device(self, connection, device_file) -> str:
        observer = self._required(connection, "actions", device_file["observer_action_id"])
        source = self._required(connection, "actions", device_file["source_action_id"])
        binding = (observer["device_id"], observer["driver_id"])
        if (
            observer["type"] not in _CAPTURE_TYPES
            or source["type"] not in _CAPTURE_TYPES
            or not all(isinstance(value, str) and value for value in binding)
            or binding != (source["device_id"], source["driver_id"])
        ):
            raise ConsistencyError("设备文件观察者与可靠来源的原设备绑定不一致")
        return observer["device_id"]

    def _device_busy(self, connection, device_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM file_copies fc"
            " JOIN operation_runs r ON r.copy_id = fc.id AND r.kind = 3"
            " WHERE fc.slot_device_id = ? AND r.status IN (?, ?) LIMIT 1",
            (device_id, *_UNFINISHED_RUN_STATUS),
        ).fetchone()
        return row is not None

    def _source_protected(self, connection, device_file_id: int) -> bool:
        row = connection.execute(
            "SELECT 1 FROM file_copies fc"
            " JOIN operation_runs r ON r.copy_id = fc.id AND r.kind = 3"
            " WHERE fc.source_device_file_id = ? AND r.status IN (?, ?) LIMIT 1",
            (device_file_id, *_UNFINISHED_RUN_STATUS),
        ).fetchone()
        return row is not None

    def _wins_business_order(self, connection, action, device_id: str) -> bool:
        """同设备存在排序更早的合格候选时不授予本候选。

        排序键为计划时间、同时间取回优先、计划与输入顺序；协程
        唤醒顺序不影响判定。
        """
        mine = self._order_key(
            action["scheduled_at"], int(action["type"]),
            action["plan_id"], action["input_index"],
        )
        obtain_rows = connection.execute(
            "SELECT a.id, a.scheduled_at, a.plan_id, a.input_index, a.type"
            " FROM obtain_items oi"
            " JOIN obtain_source_selections s ON s.id = oi.selection_id"
            " JOIN action_dependencies ad ON ad.id = s.dependency_id"
            " JOIN actions src ON src.id = ad.depends_on_action_id"
            " JOIN device_files df ON df.source_action_id = src.id"
            " JOIN outputs o ON o.device_file_id = df.id"
            " JOIN actions a ON a.id = ad.action_id"
            " WHERE oi.status = ? AND a.status = ? AND a.cancel_requested = 0"
            " AND a.scheduled_at IS NOT NULL AND src.device_id = ?"
            " AND df.presence_state = ? AND df.completion_state = ?"
            " AND o.availability = ?",
            (
                int(_ITEM_STATUS.SELECTED),
                int(_ACTION_STATUS.RUNNING),
                device_id,
                int(_PRESENCE.PRESENT),
                int(_COMPLETION.COMPLETE),
                int(_AVAILABILITY.AVAILABLE),
            ),
        ).fetchall()
        processing_rows = connection.execute(
            "SELECT a.id, a.scheduled_at, a.plan_id, a.input_index, a.type"
            " FROM recording_processing rp"
            " JOIN actions a ON a.id = rp.action_id"
            " JOIN device_files df ON df.id = rp.source_device_file_id"
            " JOIN actions src ON src.id = df.source_action_id"
            " WHERE rp.repair_state = ? AND a.status = ? AND a.cancel_requested = 0"
            " AND a.scheduled_at IS NOT NULL AND src.device_id = ?"
            " AND df.presence_state = ? AND df.completion_state = ?",
            (
                int(enum_for("recording_processing.repair_state").PENDING),
                int(_ACTION_STATUS.RUNNING),
                device_id,
                int(_PRESENCE.PRESENT),
                int(_COMPLETION.COMPLETE),
            ),
        ).fetchall()
        for action_id, scheduled_at, plan_id, input_index, action_type in (
            list(obtain_rows) + list(processing_rows)
        ):
            if int(action_id) == self._command.action_id:
                continue
            other = self._order_key(scheduled_at, int(action_type), plan_id, input_index)
            if other < mine:
                return False
        return True

    @staticmethod
    def _order_key(scheduled_at, action_type: int, plan_id, input_index):
        return (scheduled_at, 0 if action_type == _OBTAIN_TYPE else 1, plan_id, input_index)

    # ---- 授予建档 ----

    def _grant(self, scope, source_file, device_id: str | None) -> CommandPlan:
        command = self._command
        is_delivery = command.item_id is not None
        config = command.config

        target_file_id = next_row_id(scope.connection, "intermediate_files")
        delivery_id = (
            next_row_id(scope.connection, "deliveries") if is_delivery else None
        )
        copy_id = next_row_id(scope.connection, "file_copies")
        run_id = next_row_id(scope.connection, "operation_runs")
        purpose = FilePurpose.DELIVERY_COPY if is_delivery else FilePurpose.RECORDING_INPUT
        target_path = relative_file_path(purpose, target_file_id, command.target_extension)
        delivery_name = object_file_name(delivery_id, command.delivery_extension) if is_delivery else None

        specs: list[tuple[int, int, tuple]] = []
        intermediate_row = _row(
            "intermediate_files",
            target_file_id,
            {
                "owner_action_id": command.action_id if not is_delivery else None,
                "owner_delivery_id": delivery_id,
                "purpose": int(_PURPOSE.DELIVERY_COPY if is_delivery else _PURPOSE.RECORDING_INPUT),
                "relative_path": target_path,
                "retention_state": int(_RETENTION.REQUIRED),
                "cleanup_state": int(_FILE_CLEANUP.NOT_NEEDED),
                "size_bytes": None,
                "sha256": None,
                "last_error_json": None,
            },
        )
        specs.append((_INTERMEDIATE_FILE_EVENT, 1, (intermediate_row,)))
        self._owners[("intermediate_files", target_file_id)] = (
            "intermediate_file", target_file_id,
        )

        if is_delivery:
            delivery_row = _row(
                "deliveries",
                delivery_id,
                {
                    "action_id": command.action_id,
                    "output_id": command.output_id,
                    "file_name": delivery_name,
                    "display_name": command.delivery_display_name,
                    "status": int(_DELIVERY_STATUS.PENDING),
                    "publication_intent_event_id": None,
                    "published_event_id": None,
                    "withdrawal_state": int(_WITHDRAWAL.NOT_REQUESTED),
                    "withdrawal_error_json": None,
                    "error_json": None,
                },
            )
            specs.append((_DELIVERY_CHANGED_EVENT, 1, (delivery_row,)))
            self._owners[("deliveries", delivery_id)] = ("delivery", delivery_id)

        run_row = _row(
            "operation_runs",
            run_id,
            {
                "action_id": command.action_id,
                "delivery_id": delivery_id,
                "kind": int(_RUN_KIND.READ_FILE),
                "query_purpose": None,
                "responsibility_key": f"read/{copy_id}",
                "activity_id": None,
                "copy_id": copy_id,
                "cleanup_item_id": None,
                "session_key": None,
                "status": int(_RUN_STATUS.PENDING),
                "attempts_used": 0,
                "max_attempts_used": config.max_attempts if config is not None else 1,
                "timeout_s_json": config.timeout_s if config is not None else None,
                "retry_interval_s_json": config.retry_interval_s if config is not None else None,
                "retry_wait_required": 0,
                "error_json": None,
            },
        )
        specs.append((_OPERATION_CONFIGURED_EVENT, 1, (run_row,)))

        copy_owner = (
            ("delivery", delivery_id) if is_delivery else ("action", command.action_id)
        )
        copy_row = _row(
            "file_copies",
            copy_id,
            {
                "delivery_id": delivery_id,
                "processing_id": command.processing_id,
                "source_device_file_id": command.source_device_file_id,
                "source_intermediate_file_id": command.source_intermediate_file_id,
                "target_file_id": target_file_id,
                "round": 1,
                "recopies_used": 0,
                "max_recopies_used": 0,
                "source_size": source_file["size_bytes"],
                "source_sha256": source_file["sha256"],
                "committed_bytes": 0,
                "reset_state": int(_RESET_STATE.READY),
                "slot_device_id": device_id,
                "verification_state": int(_VERIFICATION.NOT_PERFORMED),
                "target_sha256": None,
                "verification_error_json": None,
            },
        )
        specs.append((_COPY_CHANGED_EVENT, 1, (copy_row,)))
        self._owners[("file_copies", copy_id)] = copy_owner
        self._owners[("operation_runs", run_id)] = copy_owner

        if is_delivery:
            item = self._state["obtain_items"][command.item_id]
            item_row = _update(
                "obtain_items",
                command.item_id,
                {
                    "status": item["status"],
                    "source_dependency": item["source_dependency"],
                    "delivery_id": item["delivery_id"],
                },
                {
                    "status": int(_ITEM_STATUS.DELIVERY_CREATED),
                    "source_dependency": 1,
                    "delivery_id": delivery_id,
                },
            )
            specs.append((_READ_PERMISSION_EVENT, _GRANT_REASON, (item_row,)))
            self._owners[("obtain_items", command.item_id)] = (
                "action", command.action_id,
            )

        events = self._envelopes(scope, specs)
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=FileQualification(
                outcome=QualificationOutcome.GRANTED,
                copy_id=copy_id,
                run_id=run_id,
                delivery_id=delivery_id,
                target_file_id=target_file_id,
                reason=None,
            ),
        )

    def _envelopes(self, scope, specs: list[tuple[int, int, tuple]]):
        """一次性分配本组事件的编号并构造事件信封。"""
        allocation = scope.allocate(len(specs))
        return tuple(
            _envelope(
                allocation.first_event_id + index,
                allocation.txn_id,
                event_type,
                reason,
                rows,
                self._command.occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )

    def _reject_item(self, scope, error_code: int) -> CommandPlan:
        """清理限制、删除处理者或产物不可用：保存逐项最终失败。"""
        command = self._command
        if command.item_id is None:
            # 内部处理的读取失败由录像处理自身事件表达，本命令只返回。
            return self._wait_final(error_code)
        item = self._state["obtain_items"][command.item_id]
        details = {"output_id": command.output_id}
        self._owners[("obtain_items", command.item_id)] = (
            "action", command.action_id,
        )
        row = _update(
            "obtain_items",
            command.item_id,
            {
                "status": item["status"],
                "error_code": item["error_code"],
                "error_details_json": item["error_details_json"],
            },
            {
                "status": int(_ITEM_STATUS.FAILED),
                "error_code": error_code,
                "error_details_json": details,
            },
        )
        event = self._envelopes(
            scope, [(_READ_PERMISSION_EVENT, _REJECT_REASON, (row,))]
        )[0]
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=FileQualification(
                outcome=QualificationOutcome.REJECTED_FINAL,
                copy_id=None,
                run_id=None,
                delivery_id=None,
                target_file_id=None,
                reason=f"error_code={error_code}",
            ),
        )

    def _wait_final(self, error_code: int) -> CommandPlan:
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FileQualification(
                outcome=QualificationOutcome.REJECTED_FINAL,
                copy_id=None,
                run_id=None,
                delivery_id=None,
                target_file_id=None,
                reason=f"error_code={error_code}",
            ),
        )

    def _wait(self, reason: str) -> CommandPlan:
        """可等待拒绝：不落库，稍后按原资格重新申请。"""
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FileQualification(
                outcome=QualificationOutcome.REJECTED,
                copy_id=None,
                run_id=None,
                delivery_id=None,
                target_file_id=None,
                reason=reason,
            ),
        )

    def _item_already_granted(self, connection, item) -> CommandPlan:
        delivery_id = item["delivery_id"]
        copy = connection.execute(
            "SELECT id FROM file_copies WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        run = connection.execute(
            "SELECT id FROM operation_runs WHERE copy_id = ? AND kind = 3",
            (copy[0] if copy else -1,),
        ).fetchone()
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FileQualification(
                outcome=QualificationOutcome.GRANTED,
                copy_id=copy[0] if copy else None,
                run_id=run[0] if run else None,
                delivery_id=delivery_id,
                target_file_id=None,
                reason="already_granted",
            ),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """同键重送：从已保存事件重建原结果。"""
        copy_id = run_id = delivery_id = target_file_id = None
        granted = False
        for event in saved:
            for row in event.get("body", {}).get("rows", []):
                values = row.get("after", {})
                if not values.get("exists", True) and row.get("table") != "obtain_items":
                    continue
                after = values.get("values", values)
                table = row.get("table")
                if table == "file_copies" and after.get("id"):
                    copy_id = after["id"]
                elif table == "operation_runs" and after.get("id"):
                    run_id = after["id"]
                elif table == "deliveries" and after.get("id"):
                    delivery_id = after["id"]
                elif table == "intermediate_files" and after.get("id"):
                    target_file_id = after["id"]
                elif table == "obtain_items" and after.get("status") == int(
                    _ITEM_STATUS.DELIVERY_CREATED
                ):
                    granted = True
        if not granted and copy_id is None:
            outcome = QualificationOutcome.REJECTED_FINAL
        elif granted or copy_id is not None:
            outcome = QualificationOutcome.GRANTED
        else:
            outcome = QualificationOutcome.REJECTED
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FileQualification(
                outcome=outcome,
                copy_id=copy_id,
                run_id=run_id,
                delivery_id=delivery_id,
                target_file_id=target_file_id,
                reason="resent",
            ),
        )


class OutputsRepository:
    """来源固定、选择与读取资格的 SQLite 仓储。"""

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

    def grant_file(
        self, command: FileCandidate, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FileQualification]:
        receipt = commit_operation(_GrantFileCommand(command, key), key, owned)
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


def _intermediate_guard(event, context) -> None:
    """中间文件建档守卫：用途与归属互斥、初始保留与清理状态固定。"""
    if event.event_type != _INTERMEDIATE_FILE_EVENT or event.reason != 1:
        return
    for row in event.rows:
        if row.table != "intermediate_files" or row.before.exists:
            raise EventValidationError("中间文件建档必须是创建行")
        values = row.after.values
        purpose = values.get("purpose")
        if values.get("retention_state") != int(_RETENTION.REQUIRED):
            raise EventValidationError("新建中间文件必须处于 REQUIRED 保留状态")
        if values.get("cleanup_state") != int(_FILE_CLEANUP.NOT_NEEDED):
            raise EventValidationError("新建中间文件不得登记清理待办")
        if values.get("size_bytes") is not None or values.get("sha256") is not None:
            raise EventValidationError("新建中间文件尚无内容事实")
        has_delivery = values.get("owner_delivery_id") is not None
        has_action = values.get("owner_action_id") is not None
        if purpose == int(_PURPOSE.DELIVERY_COPY):
            if not has_delivery or has_action:
                raise EventValidationError("交付副本必须归属交付且不归属动作")
        elif has_delivery or not has_action:
            raise EventValidationError("处理用途的中间文件必须归属动作")


def _delivery_guard(event, context) -> None:
    """交付建档守卫：初始待准备、未发布且未撤回。"""
    if event.event_type != _DELIVERY_CHANGED_EVENT or event.reason != 1:
        return
    for row in event.rows:
        if row.table != "deliveries" or row.before.exists:
            raise EventValidationError("交付建档必须是创建行")
        values = row.after.values
        if values.get("status") != int(_DELIVERY_STATUS.PENDING):
            raise EventValidationError("新建交付必须处于 PENDING")
        if values.get("withdrawal_state") != int(_WITHDRAWAL.NOT_REQUESTED):
            raise EventValidationError("新建交付不得携带撤回状态")
        if (
            values.get("publication_intent_event_id") is not None
            or values.get("published_event_id") is not None
            or values.get("error_json") is not None
        ):
            raise EventValidationError("新建交付不得携带发布或错误事实")


def _copy_guard(event, context) -> None:
    """拷贝建档守卫：首轮、零进度、初始验证与重置状态。"""
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != 1:
        return
    for row in event.rows:
        if row.table != "file_copies" or row.before.exists:
            raise EventValidationError("拷贝建档必须是创建行")
        values = row.after.values
        expected = {
            "round": 1,
            "recopies_used": 0,
            "committed_bytes": 0,
            "reset_state": int(_RESET_STATE.READY),
            "verification_state": int(_VERIFICATION.NOT_PERFORMED),
            "target_sha256": None,
            "verification_error_json": None,
        }
        for column, value in expected.items():
            if values.get(column) != value:
                raise EventValidationError(
                    f"新建拷贝的 {column} 必须是 {value!r}: {values.get(column)!r}"
                )
        if values.get("source_size") is None:
            raise EventValidationError("新建拷贝必须携带源长度")
        if values.get("source_device_file_id") is not None and not values.get("slot_device_id"):
            raise EventValidationError("设备来源拷贝必须占用所属设备的读取机会")
        if values.get("source_intermediate_file_id") is not None and values.get("slot_device_id") is not None:
            raise EventValidationError("主机来源拷贝不占用相机读取机会")
        if (values.get("delivery_id") is None) == (values.get("processing_id") is None):
            raise EventValidationError("拷贝必须恰归属交付或录像处理之一")


def _copy_links_guard(event, context) -> None:
    """拷贝关联守卫：每份新拷贝与唯一 READ_FILE 流程共同保存。"""
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != 1:
        return
    runs = context.association_rows.get("operation_runs", {})
    for row in event.rows:
        if row.table != "file_copies" or row.before.exists:
            continue
        copy_id = row.row_id
        values = row.after.values
        matches = [
            facts
            for facts in runs.values()
            if facts.get("kind") == int(_RUN_KIND.READ_FILE)
            and facts.get("copy_id") == copy_id
        ]
        if len(matches) != 1:
            raise EventValidationError(
                f"每份新拷贝必须恰有一条 READ_FILE 流程: copy {copy_id}"
                f" 流程 {len(matches)} 条"
            )
        run = matches[0]
        if run.get("responsibility_key") != f"read/{copy_id}":
            raise EventValidationError("READ_FILE 流程责任键与拷贝不一致")
        if run.get("delivery_id") != values.get("delivery_id"):
            raise EventValidationError("READ_FILE 流程与拷贝的交付归属不一致")


def _read_permission_guard(event, context) -> None:
    """读取资格守卫：授予与逐项拒绝均恰好作用于一条 SELECTED 项。"""
    if event.event_type == _READ_PERMISSION_EVENT and event.reason == _GRANT_REASON:
        updates = [
            row
            for row in event.rows
            if row.table == "obtain_items" and row.before.exists
        ]
        if len(updates) != 1:
            raise EventValidationError("资格授予必须恰好更新一条取回项")
        row = updates[0]
        before = row.before.values
        after = row.after.values
        if (
            before.get("status") != int(_ITEM_STATUS.SELECTED)
            or before.get("source_dependency") != 0
            or before.get("delivery_id") is not None
        ):
            raise EventValidationError("只有未依赖源的 SELECTED 项能被授予")
        if (
            after.get("status") != int(_ITEM_STATUS.DELIVERY_CREATED)
            or after.get("source_dependency") != 1
            or after.get("delivery_id") is None
        ):
            raise EventValidationError("授予必须建立源依赖并回填交付")
        delivery_id = after.get("delivery_id")
        delivery = context.state_rows.get("deliveries", {}).get(delivery_id)
        if delivery is None or delivery.get("status") != int(_DELIVERY_STATUS.PENDING):
            raise EventValidationError("授予回填的交付必须已在同事务建档")
    elif event.event_type == _READ_PERMISSION_EVENT and event.reason == _REJECT_REASON:
        updates = [
            row
            for row in event.rows
            if row.table == "obtain_items" and row.before.exists
        ]
        if len(updates) != 1:
            raise EventValidationError("逐项拒绝必须恰好更新一条取回项")
        after = updates[0].after.values
        if (
            after.get("status") != int(_ITEM_STATUS.FAILED)
            or after.get("error_code") is None
            or after.get("error_details_json") is None
        ):
            raise EventValidationError("逐项拒绝必须保存最终失败及错误")
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 1:
        for row in event.rows:
            if row.table != "deliveries" or row.before.exists:
                continue
            items = context.state_rows.get("obtain_items", {})
            if not items:
                raise EventValidationError("交付建档必须提供待授予的取回项事实")


def register_outputs_guards() -> None:
    """注册来源、选择与读取资格事件的正式业务守卫（装配期调用）。"""
    register_guard("selection_initialization", _selection_initialization_guard)
    register_guard("source_selection", _source_selection_guard)
    register_guard("intermediate", _intermediate_guard)
    register_guard("delivery", _delivery_guard)
    register_guard("copy", _copy_guard)
    register_guard("copy_links", _copy_links_guard)
    register_guard("read_permission", _read_permission_guard)
