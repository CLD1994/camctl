"""取回来源固定与逐来源选择的持久化事务。

SOURCE_RESOLVED 在取得执行资格后一次固定跨计划来源成员（或可
靠失败），每个固定成员在同一事件共同初始化 PENDING 选择；
TARGETS_FIXED 将一个来源的完整选择（含合法空选择与逐项失败）与
FIXED 状态同一事务保存。已固定的来源与选择不因重送、重启或新文
件出现而重选或扩大；此前已可靠确认存在的产物记录缺失按状态库
矛盾拒绝，不解释为普通不存在。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    PathRuleError, object_file_name, relative_file_path, validate_relative_file_path,
)
from camctl.operations.attempts import AttemptTarget, OperationKind, operation_responsibility_key
from camctl.outputs.catalog import OutputKind
from camctl.outputs.sources import (
    ActionFacts,
    CatalogEntry,
    OriginalOutputs,
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
from camctl.contracts.workflow_errors import (
    action_error_id, item_error_id, registered_error_spec, validate_error_details,
)
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
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")

#: 录像处理中仍未结束的状态；存在即表示适用产物处理未完成。
_UNFINISHED_PROCESSING_CHECK = frozenset({int(_CHECK_STATE.RUNNING)})
_UNFINISHED_PROCESSING_REPAIR = frozenset(
    {int(member) for member in _REPAIR_STATE
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
_RESOLVE_REASON = 4
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
_OUTPUT_CLEANUP_STATUS = enum_for("outputs.cleanup_status")
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


def _required_current_facts(context, table, identity):
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"取回成员的当前关联事实缺失: {table}#{identity}")
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


def _processing_requires_existing_input(processing) -> bool:
    """工具阶段是否证明原输入已经建立；False 不授予首次输入资格。"""
    try:
        check, repair = processing["check_state"], processing["repair_state"]
        if not is_json_integer(check) or not is_json_integer(repair):
            raise ValueError("处理阶段必须是登记的整数编号")
        check, repair = _CHECK_STATE(check), _REPAIR_STATE(repair)
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原录像的检查或修复阶段缺失或无法解释") from error
    return (check in (_CHECK_STATE.RUNNING, _CHECK_STATE.COMPLETED)
            or repair in (_REPAIR_STATE.RUNNING, _REPAIR_STATE.SUCCEEDED))


def _read_is_due(action, occurred_at: int) -> bool:
    """首次读取建档的时间条件；不代替会话墙钟可信性检查。"""
    if action is None or not is_json_integer(action.get("scheduled_at")):
        raise ConsistencyError("读取发起动作的计划时间缺失或无法解释")
    return action["scheduled_at"] <= occurred_at


def _guard_read_time(event, context, action_id) -> None:
    action = context.state_rows.get("actions", {}).get(action_id)
    try:
        due = _read_is_due(action, event.occurred_at)
    except ConsistencyError as error:
        raise EventValidationError(str(error)) from error
    if not due:
        raise EventValidationError("读取发起动作尚未到计划时间")


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


def load_output_family(connection, output_id: int) -> OriginalOutputs:
    """只读取得当前产物所属的一份原片及派生关系，最多三个产物。

    目标必须已经登记；派生关系缺失与可靠没有派生产物分别处理。
    第三个派生 ID 只用于识别超出一份预览及一份修复成品的矛盾，
    不读取同一来源的其他原片或全库目录。调用方使用同一事务快照。
    """
    ObjectId(output_id)

    def required(table, identity):
        facts = row_facts(connection, table, identity)
        if facts is None:
            raise ConsistencyError(f"产物关联记录缺失: {table}#{identity}")
        return facts

    def origin(identity):
        with closing(connection.execute(
            "SELECT original_output_id FROM output_origins WHERE output_id=?", (identity,),
        )) as cursor:
            row = cursor.fetchone()
        return row[0] if row is not None else None

    target = required("outputs", output_id)
    original_id = origin(output_id)
    if target["kind"] == int(_OUTPUT_KIND.ORIGINAL):
        if original_id is not None:
            raise ConsistencyError("原片不能携带派生关联")
        original = target
    else:
        if original_id is None:
            raise ConsistencyError("派生产物缺少原片关联")
        original = required("outputs", original_id)
        if original["kind"] != int(_OUTPUT_KIND.ORIGINAL) or origin(original_id) is not None:
            raise ConsistencyError("派生产物必须指向没有派生关联的原片")
    original_id = original["id"]
    with closing(connection.execute(
        "SELECT output_id FROM output_origins WHERE original_output_id=? ORDER BY output_id LIMIT 3",
        (original_id,),
    )) as cursor:
        related_ids = tuple(row[0] for row in cursor.fetchall())
    if len(related_ids) > 2:
        raise ConsistencyError("同一原片最多登记一份预览和一份修复成品")
    by_kind = {}
    for identity in (original_id, *related_ids):
        entry = original if identity == original_id else target if identity == output_id else required("outputs", identity)
        kind = OutputKind[_OUTPUT_KIND(entry["kind"]).name]
        if kind in by_kind or entry["source_action_id"] != original["source_action_id"]:
            raise ConsistencyError("派生产物重复或不属于原片来源")
        is_device = entry["device_file_id"] is not None
        file = required("device_files" if is_device else "intermediate_files",
                        entry["device_file_id"] if is_device else entry["intermediate_file_id"])
        if file["source_action_id" if is_device else "owner_action_id"] != original["source_action_id"]:
            raise ConsistencyError("产物承载文件不属于原片来源")
        if is_device:
            expected_role = {
                OutputKind.ORIGINAL: _DEVICE_FILE_ROLE.ORIGINAL,
                OutputKind.PREVIEW: _DEVICE_FILE_ROLE.PREVIEW,
            }.get(kind)
            if expected_role is None or file["role"] != int(expected_role):
                raise ConsistencyError("设备产物与承载文件角色不一致")
            if file["completion_state"] != int(_COMPLETION.COMPLETE):
                raise ConsistencyError("已登记设备产物必须保留文件完成事实")
            if kind is OutputKind.PREVIEW:
                if (file["original_device_file_id"] != original["device_file_id"]
                        or file["pairing_evidence_json"] is None):
                    raise ConsistencyError("预览的设备文件配对与产物原片关联不一致")
            elif (file["original_device_file_id"] is not None
                  or file["pairing_evidence_json"] is not None):
                raise ConsistencyError("原片文件不能携带预览配对")
        elif (kind is not OutputKind.REPAIRED
              or file["owner_delivery_id"] is not None
              or file["purpose"] != int(_PURPOSE.REPAIR_OUTPUT)
              or file["retention_state"] != int(_RETENTION.PROMOTED)
              or file["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)):
            raise ConsistencyError("修复产物必须由来源动作的已提升修复文件承载")
        size = file["size_bytes"]
        if size is not None and (not is_json_integer(size) or size < 0):
            raise ConsistencyError("产物完整长度无法解释")
        by_kind[kind] = CatalogEntry(
            output_id=identity, kind=kind, availability=entry["availability"], size_bytes=size,
            original_output_id=None if identity == original_id else original_id,
        )
    if output_id not in (original_id, *related_ids):
        raise ConsistencyError("目标产物未保留在原片派生关系中")
    return OriginalOutputs(original["source_action_id"], by_kind[OutputKind.ORIGINAL],
                           by_kind.get(OutputKind.PREVIEW), by_kind.get(OutputKind.REPAIRED))


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
    return {"source_action_instance_id": str(source_action_id)}


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
            return self._reuse(connection, saved)
        command = self._command
        action, source_file, output, device_id = self._load_inputs(connection)
        existing = self._existing_preparation(connection, source_file, device_id)
        if action["status"] != int(_ACTION_STATUS.RUNNING) or action["cancel_requested"]:
            return self._wait("action_not_eligible")
        if existing is not None:
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                               read_only=True, result=existing)
        if command.item_id is not None:
            item = self._state["obtain_items"][command.item_id]
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
        if not _read_is_due(action, command.occurred_at):
            return self._wait("not_due")
        readiness = self._source_readiness(scope, output, source_file)
        if readiness is not None:
            return readiness

        if device_id is not None and self._device_busy(connection, device_id):
            return self._wait("device_busy")
        if device_id is not None and self._source_protected(connection, command.source_device_file_id):
            return self._wait("source_protected")
        if device_id is not None and not self._wins_business_order(connection, action, device_id):
            return self._wait("business_order")

        return self._grant(scope, source_file, device_id)

    # ---- 资格核对 ----

    def _load_inputs(self, connection):
        command = self._command
        action = self._required(connection, "actions", command.action_id)
        is_device = command.source_device_file_id is not None
        source = self._required(connection, "device_files" if is_device else "intermediate_files",
            command.source_device_file_id if is_device else command.source_intermediate_file_id)
        output = self._load_target(connection, action, source)
        device = self._source_device(connection, source) if is_device else None
        return action, source, output, device

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
            if source_file["completion_state"] != int(_COMPLETION.COMPLETE):
                raise ConsistencyError("已登记设备产物必须保留文件完成事实")
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

    def _source_readiness(self, scope, output, source_file) -> CommandPlan | None:
        """核对源事实；等待与最终拒绝分开，None 表示可继续协调。"""
        if output is None:
            if (source_file["completion_state"] == int(_COMPLETION.UNCONFIRMED)
                    or source_file["presence_state"] == int(_PRESENCE.ABSENT)):
                return self._wait_final(_SOURCE_FILE_UNCONFIRMED_CODE)
            if (source_file["completion_state"] != int(_COMPLETION.COMPLETE)
                    or source_file["presence_state"] == int(_PRESENCE.UNKNOWN)):
                return self._wait("source_file_unconfirmed")
            return None

        phase = self._cleanup_phase(scope.connection, output["id"])
        availability = _AVAILABILITY(output["availability"])
        if phase is not None:
            expected = (_AVAILABILITY.CLEANED if phase == _OUTPUT_CLEANUP_STATUS.COMPLETED
                        else _AVAILABILITY.RESTRICTED)
            if availability != expected or output["cleanup_status"] != int(phase):
                raise ConsistencyError("产物的清理投影与有效清理责任不一致")
        elif (
            availability in (_AVAILABILITY.CLEANED, _AVAILABILITY.RESTRICTED)
            or output["cleanup_status"] not in (
                int(_OUTPUT_CLEANUP_STATUS.NOT_REQUESTED), int(_OUTPUT_CLEANUP_STATUS.CANCELED),
            )
        ):
            raise ConsistencyError("产物清理投影缺少有效限制或清理完成事实")
        elif self._command.source_device_file_id is not None:
            expected = {
                int(_PRESENCE.PRESENT): _AVAILABILITY.AVAILABLE,
                int(_PRESENCE.ABSENT): _AVAILABILITY.MISSING,
                int(_PRESENCE.UNKNOWN): _AVAILABILITY.UNKNOWN,
            }[source_file["presence_state"]]
            if availability != expected:
                raise ConsistencyError("产物可用性与设备文件存在事实不一致")

        details = {"output_id": str(output["id"])}
        if availability in (_AVAILABILITY.CLEANED, _AVAILABILITY.MISSING):
            details["availability"] = availability.name.lower()
            return self._reject_item(scope, _OUTPUT_UNAVAILABLE_CODE, details)
        if availability == _AVAILABILITY.RESTRICTED:
            return self._reject_item(scope, _OUTPUT_CLEANUP_STARTED_CODE, details)
        if availability == _AVAILABILITY.UNKNOWN:
            return self._wait("source_file_unconfirmed")
        return None

    def _cleanup_phase(self, connection, output_id):
        """汇总同产物有效限制和成功优先级，不重建已解除限制的历史。"""
        # 受登记约束的枚举常量内联，使 SQLite 能匹配有效限制的部分索引。
        restrictions = ",".join(str(value) for value in _ACTIVE_RESTRICTIONS)
        with closing(connection.execute(
            "SELECT COUNT(*), MAX(status = ?), MAX(status = ?), MAX(status = ?)"
            f" FROM cleanup_items WHERE output_id = ? AND restriction_state IN ({restrictions})",
            (
                int(_CLEANUP_STATUS.SUCCEEDED), int(_CLEANUP_STATUS.DELETING),
                int(_CLEANUP_STATUS.PENDING_DELETE), output_id,
            ),
        )) as cursor:
            count, completed, deleting, pending = cursor.fetchone()
        if count == 0:
            return None
        if completed:
            return _OUTPUT_CLEANUP_STATUS.COMPLETED
        if deleting:
            return _OUTPUT_CLEANUP_STATUS.RUNNING
        if pending:
            return _OUTPUT_CLEANUP_STATUS.PENDING
        return _OUTPUT_CLEANUP_STATUS.INCOMPLETE

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
        target_file_id = next_row_id(scope.connection, "intermediate_files")
        delivery_id = (
            next_row_id(scope.connection, "deliveries") if is_delivery else None
        )
        copy_id = next_row_id(scope.connection, "file_copies")
        run_id = next_row_id(scope.connection, "operation_runs")
        specs, owners = self._grant_specs(source_file, device_id, target_file_id, delivery_id, copy_id, run_id,
            self._state["obtain_items"][command.item_id] if is_delivery else None)
        self._owners.update(owners)
        return CommandPlan(
            events=self._envelopes(scope, specs), owners=self._owners, state_rows=self._state,
            result=FileQualification(outcome=QualificationOutcome.GRANTED, copy_id=copy_id,
                run_id=run_id, delivery_id=delivery_id, target_file_id=target_file_id, reason=None),
        )

    def _grant_specs(self, source_file, device_id, target_file_id, delivery_id, copy_id, run_id, item):
        """以固定输入形成完整建档事实，供首次保存和原键核对共用。"""
        command = self._command
        is_delivery = command.item_id is not None
        config = command.config
        purpose = FilePurpose.DELIVERY_COPY if is_delivery else FilePurpose.RECORDING_INPUT
        target_path = relative_file_path(purpose, target_file_id, command.target_extension)
        delivery_name = object_file_name(delivery_id, command.delivery_extension) if is_delivery else None

        specs: list[tuple[int, int, tuple]] = []
        owners = {}
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
        owners[("intermediate_files", target_file_id)] = (
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
            owners[("deliveries", delivery_id)] = ("delivery", delivery_id)

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
        owners[("file_copies", copy_id)] = copy_owner
        owners[("operation_runs", run_id)] = copy_owner

        if is_delivery:
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
            owners[("obtain_items", command.item_id)] = (
                "action", command.action_id,
            )

        return specs, owners

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

    def _reject_item(self, scope, error_code: int, details: dict) -> CommandPlan:
        """清理限制、删除处理者或产物不可用：保存逐项最终失败。"""
        command = self._command
        item = self._state["obtain_items"][command.item_id]
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

    def _find_one(self, connection, table: str, condition: str, parameters: tuple) -> dict | None:
        """按内部固定查询定位一条责任；重复与缺失分别处理。"""
        with closing(connection.execute(
            f"SELECT id FROM {table} WHERE {condition} LIMIT 2", parameters,
        )) as cursor:
            identities = cursor.fetchall()
        if len(identities) > 1:
            raise ConsistencyError(f"读取准备责任存在重复记录: {table}")
        return self._required(connection, table, identities[0][0]) if identities else None

    def _existing_preparation(self, connection, source_file, device_id) -> FileQualification | None:
        """核对原准备责任的完整关联；不改写其配置、进度或生命周期。"""
        command = self._command
        delivery_id = None
        internal_run = None
        if command.item_id is not None:
            item = self._state["obtain_items"][command.item_id]
            delivery_id = item["delivery_id"]
            delivery = self._find_one(connection, "deliveries", "action_id = ? AND output_id = ?",
                                      (command.action_id, command.output_id))
            if delivery_id is None:
                if delivery is not None or item["status"] == int(_ITEM_STATUS.DELIVERY_CREATED):
                    raise ConsistencyError("取回项缺少原交付关联")
                return None
            if (delivery is None or delivery["id"] != delivery_id
                    or item["status"] != int(_ITEM_STATUS.DELIVERY_CREATED)):
                raise ConsistencyError("原交付与取回动作、产物或条目不一致")
            try:
                _, separator, extension = delivery["file_name"].partition(".")
                if not separator or delivery["file_name"] != object_file_name(delivery_id, extension):
                    raise PathRuleError("交付文件名不符合原身份")
            except PathRuleError as error:
                raise ConsistencyError("原交付文件名与固定身份不一致") from error
            copy = self._find_one(connection, "file_copies", "delivery_id = ?", (delivery_id,))
            target = self._find_one(connection, "intermediate_files", "owner_delivery_id = ? AND purpose = ?",
                                    (delivery_id, int(_PURPOSE.DELIVERY_COPY)))
            purpose = FilePurpose.DELIVERY_COPY
        else:
            copy = self._find_one(connection, "file_copies", "processing_id = ?", (command.processing_id,))
            target = self._find_one(connection, "intermediate_files", "owner_action_id = ? AND purpose = ?",
                                    (command.action_id, int(_PURPOSE.RECORDING_INPUT)))
            internal_run = self._find_one(connection, "operation_runs",
                f"action_id = ? AND kind = {int(_RUN_KIND.READ_FILE)}", (command.action_id,))
            purpose = FilePurpose.RECORDING_INPUT
            if copy is None and target is None and internal_run is None:
                if _processing_requires_existing_input(self._state["recording_processing"][command.processing_id]):
                    raise ConsistencyError("原片工具处理已经开始，但原输入准备责任缺失")
                return None
        if copy is None or target is None:
            raise ConsistencyError("已有读取准备责任缺少拷贝或目标文件")
        if (
            copy["delivery_id"] != delivery_id or copy["processing_id"] != command.processing_id
            or copy["source_device_file_id"] != command.source_device_file_id
            or copy["source_intermediate_file_id"] != command.source_intermediate_file_id
            or copy["target_file_id"] != target["id"]
            or copy["source_size"] != source_file["size_bytes"]
            or copy["slot_device_id"] not in (None, device_id)
        ):
            raise ConsistencyError("原拷贝与业务责任、源文件、目标或设备归属不一致")
        if (copy["source_sha256"] is not None and source_file["sha256"] is not None
                and copy["source_sha256"] != source_file["sha256"]):
            raise ConsistencyError("原拷贝与源文件的已知摘要不一致")
        try:
            validate_relative_file_path(purpose, target["id"], target["relative_path"])
        except PathRuleError as error:
            raise ConsistencyError("原读取目标路径与固定身份不一致") from error
        key = operation_responsibility_key(OperationKind.READ_FILE, command.action_id,
                                           AttemptTarget(copy_id=copy["id"]), None)
        # READ_FILE 是有限登记常量；字面值让 SQLite 使用 one_read_flow 部分索引。
        run = self._find_one(connection, "operation_runs", f"copy_id = ? AND kind = {int(_RUN_KIND.READ_FILE)}",
                             (copy["id"],))
        keyed_run = self._find_one(connection, "operation_runs", "responsibility_key = ?", (key,))
        if run is None or keyed_run is None or run["id"] != keyed_run["id"]:
            raise ConsistencyError("原拷贝缺少唯一且责任键一致的读取流程")
        if command.processing_id is not None and (internal_run is None or internal_run["id"] != run["id"]):
            raise ConsistencyError("内部读取流程与原录像处理责任不一致")
        expected = {"action_id": command.action_id, "delivery_id": delivery_id,
                    "kind": int(_RUN_KIND.READ_FILE), "copy_id": copy["id"],
                    "responsibility_key": key, "activity_id": None,
                    "cleanup_item_id": None, "query_purpose": None, "session_key": None}
        if any(run[name] != value for name, value in expected.items()):
            raise ConsistencyError("原读取流程的类型、目标或归属不一致")
        return FileQualification(
            outcome=QualificationOutcome.GRANTED, copy_id=copy["id"], run_id=run["id"],
            delivery_id=delivery_id, target_file_id=target["id"], reason="already_granted",
        )

    def _reuse(self, connection, saved: list[dict]) -> CommandPlan:
        """原键核对完整业务组和原输入；不重新判断当前普通资格。"""
        if any(event["occurred_at"] != self._command.occurred_at for event in saved):
            raise TransactionError("读取申请的事实时刻与原事务不同")
        if len(saved) == 1 and (saved[0]["type"], saved[0]["reason"]) == (_READ_PERMISSION_EVENT, _REJECT_REASON):
            return self._reuse_rejection(connection, saved[0])
        required = {"intermediate_files", "operation_runs", "file_copies"}
        if self._command.item_id is not None:
            required.update(("deliveries", "obtain_items"))
        rows, events = {}, {}
        for event in saved:
            changes = event["body"]["rows"]
            if len(changes) != 1 or changes[0]["table"] in rows:
                raise ConsistencyError("原建档事务的业务成员重复或不完整")
            row = changes[0]
            rows[row["table"]], events[row["table"]] = row, event
        if rows.keys() != required:
            raise TransactionError("原操作身份不属于本次读取建档阶段")
        for table, row in rows.items():
            if (row["before"]["exists"] != (table == "obtain_items") or not row["after"]["exists"]):
                raise TransactionError("原操作身份的创建或资格变更阶段不符")
        copy = rows["file_copies"]
        source_snapshot = {"size_bytes": copy["after"]["values"]["source_size"],
                           "sha256": copy["after"]["values"]["source_sha256"]}
        ids = {name: row["id"] for name, row in rows.items()}
        try:
            specs, _ = self._grant_specs(source_snapshot, copy["after"]["values"]["slot_device_id"],
                ids["intermediate_files"], ids.get("deliveries"), ids["file_copies"], ids["operation_runs"],
                {"status": int(_ITEM_STATUS.SELECTED), "source_dependency": 0, "delivery_id": None})
        except PathRuleError as error:
            raise TransactionError("重送的文件名称无法对应原建档身份") from error
        for event_type, reason, (expected,) in specs:
            original, event = rows[expected.table], events[expected.table]
            if (event["type"], event["reason"]) != (event_type, reason):
                raise TransactionError("原操作身份的事件阶段与读取建档不符")
            if (original["id"] != expected.row_id
                    or original["before"]["exists"] != expected.before.exists
                    or original["after"]["exists"] != expected.after.exists
                    or not json_equal(original["before"].get("values", {}), expected.before.values)
                    or not json_equal(original["after"]["values"], expected.after.values)):
                raise TransactionError("原建档事实与本次读取申请的目标或采用参数不同")
        _, source, _, device = self._load_inputs(connection)
        existing = self._existing_preparation(connection, source, device)
        if (existing is None or existing.copy_id != ids["file_copies"]
                or existing.run_id != ids["operation_runs"] or existing.target_file_id != ids["intermediate_files"]
                or existing.delivery_id != ids.get("deliveries")):
            raise ConsistencyError("原建档事务与当前准备责任的身份不一致")
        current_copy = self._state["file_copies"][existing.copy_id]
        if (not json_equal(current_copy["source_size"], source_snapshot["size_bytes"])
                or (source_snapshot["sha256"] is not None
                    and (current_copy["source_sha256"] != source_snapshot["sha256"]
                         or source["sha256"] != source_snapshot["sha256"]))
                or copy["after"]["values"]["slot_device_id"] != device):
            raise ConsistencyError("原建档事务的源内容或设备身份不一致")
        for table, names in (("intermediate_files", ("relative_path",)),
                             ("deliveries", ("file_name", "display_name"))):
            if table not in rows:
                continue
            current = self._state[table][ids[table]]
            if (current["created_event_id"] != events[table]["event_id"]
                    or any(not json_equal(current[name], rows[table]["after"]["values"][name]) for name in names)):
                raise ConsistencyError("原建档记录的创建引用或固定名称不一致")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=FileQualification(
                outcome=QualificationOutcome.GRANTED, copy_id=existing.copy_id, run_id=existing.run_id,
                delivery_id=existing.delivery_id, target_file_id=existing.target_file_id, reason=None,
            ),
        )

    def _reuse_rejection(self, connection, event) -> CommandPlan:
        rows = event["body"]["rows"]
        command = self._command
        if (command.item_id is None or len(rows) != 1 or rows[0]["table"] != "obtain_items"
                or rows[0]["id"] != command.item_id):
            raise TransactionError("原拒绝事务不属于本次取回项")
        row = rows[0]
        before = {"status": int(_ITEM_STATUS.SELECTED), "error_code": None, "error_details_json": None}
        after = row["after"]["values"]
        if (not row["before"]["exists"] or not row["after"]["exists"]
                or not json_equal(row["before"]["values"], before)
                or set(after) != set(before) or after["status"] != int(_ITEM_STATUS.FAILED)
                or after["error_code"] not in (_OUTPUT_UNAVAILABLE_CODE, _OUTPUT_CLEANUP_STARTED_CODE)):
            raise ConsistencyError("原事务不是已选中产物的建档前拒绝")
        _, source, output, device = self._load_inputs(connection)
        item = self._state["obtain_items"][command.item_id]
        _validate_obtain_error({**item, **after}, output["source_action_id"])
        if (self._existing_preparation(connection, source, device) is not None
                or any(not json_equal(item[name], value) for name, value in after.items())):
            raise ConsistencyError("已拒绝取回项未保留原事务的最终事实")
        return self._wait_final(after["error_code"])


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
    details = selection.after.values.get("error_details_json")
    _validate_saved_error("obtain_source_selections", error_code, details)
    if details is not None:
        if ("source_action_instance_id" in details
                and details["source_action_instance_id"] != str(source["id"])):
            raise EventValidationError("来源错误必须指向实际固定来源")
        if "original_output_id" in details:
            original = context.state_rows.get("outputs", {}).get(int(details["original_output_id"]))
            if (original is None or original.get("source_action_id") != source["id"]
                    or original.get("kind") != int(_OUTPUT_KIND.ORIGINAL)):
                raise EventValidationError("来源错误中的原片必须属于实际固定来源")
    for item in items:
        values = item.after.values
        _validate_obtain_error(values, source["id"])
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
        if values.get("processing_id") is not None:
            processing = context.state_rows.get("recording_processing", {}).get(values["processing_id"])
            try:
                requires_existing = _processing_requires_existing_input(processing)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            if requires_existing:
                raise EventValidationError("原片工具处理已经开始，不能创建替代输入拷贝")
            parent = processing
        else:
            parent = context.association_rows.get("deliveries", {}).get(values["delivery_id"])
        if parent is None:
            raise EventValidationError("新建拷贝缺少发起责任的固定关联")
        _guard_read_time(event, context, parent.get("action_id"))


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


def _validate_saved_error(table, code, details) -> None:
    """错误详情使用公共登记的格式，不在报告生成时补救。"""
    if code is None:
        if details is not None:
            raise EventValidationError("没有错误码时不能保存错误详情")
        return
    try:
        name, _ = registered_error_spec(f"item_error_ids.{table}", code)
        validate_error_details(name, details)
    except (TypeError, ValueError) as error:
        raise EventValidationError(str(error)) from error


def _validate_obtain_error(values, source_id) -> None:
    details = values.get("error_details_json")
    _validate_saved_error("obtain_items", values.get("error_code"), details)
    if details is None:
        return
    identities = {
        "output_id": values.get("output_id"),
        "requested_output_id": values.get("requested_output_id"),
        "original_output_id": values.get("original_output_id"),
        "source_action_instance_id": source_id,
    }
    for name, identity in identities.items():
        if name in details and (identity is None or details[name] != str(identity)):
            raise EventValidationError(f"取回错误详情与原目标不一致: {name}")


def _obtain_member_guard(event, context) -> None:
    """核实或拒绝只推进事件发生前已固定且仍有普通资格的成员。"""
    if event.event_type != _READ_PERMISSION_EVENT or event.reason not in (
        _REJECT_REASON, _RESOLVE_REASON,
    ):
        return

    updates = [row for row in event.rows if row.table == "obtain_items" and row.before.exists]
    if len(updates) != 1:
        raise EventValidationError("取回成员核实或拒绝必须恰好推进一条已有项")
    row = updates[0]
    item = _required_current_facts(context, "obtain_items", row.row_id)
    selection = _required_current_facts(context, "obtain_source_selections", item.get("selection_id"))
    dependency = _required_current_facts(context, "action_dependencies", selection.get("dependency_id"))
    owner = _required_current_facts(context, "actions", dependency.get("action_id"))
    source = _required_current_facts(context, "actions", dependency.get("depends_on_action_id"))
    if (
        selection.get("status") != int(_SELECTION_STATUS.FIXED)
        or selection.get("error_code") is not None
        or selection.get("error_details_json") is not None
        or owner.get("type") != _OBTAIN_TYPE
        or owner.get("status") != int(_ACTION_STATUS.RUNNING)
        or owner.get("cancel_requested") != 0
        or owner.get("source_resolution_state") != int(_RESOLUTION_STATE.FIXED)
        or owner.get("resolved_source_plan_id") is None
        or owner.get("resolved_source_plan_id") != source.get("plan_id")
        or source.get("type") not in _CAPTURE_TYPES
        or source.get("status") not in _ACTION_TERMINAL
    ):
        raise EventValidationError("取回成员要求来源拍摄已终态、选择已固定且无来源级错误，取回仍在执行中并未取消")
    if (
        item.get("status") not in (int(_ITEM_STATUS.UNRESOLVED), int(_ITEM_STATUS.SELECTED))
        or item.get("source_dependency") != 0
        or item.get("delivery_id") is not None
        or item.get("error_code") is not None
        or item.get("error_details_json") is not None
    ):
        raise EventValidationError("只有尚未建档或结束的取回项能核实或拒绝")
    unresolved = item["status"] == int(_ITEM_STATUS.UNRESOLVED)
    explicit = item.get("basis") == int(_ITEM_BASIS.EXPLICIT)
    if unresolved and (not explicit or item.get("output_id") is not None):
        raise EventValidationError("未核实成员必须是尚未关联产物的显式请求")
    if explicit and item.get("requested_output_id") is None:
        raise EventValidationError("显式成员必须保留原请求产物 ID")
    if event.reason == _REJECT_REASON:
        _validate_obtain_error({**item, **row.after.values}, dependency["depends_on_action_id"])
    if event.reason == _RESOLVE_REASON:
        if not unresolved:
            raise EventValidationError("显式核实只能推进原 UNRESOLVED 项")
        output_id = row.after.values.get("output_id")
    else:
        output_id = item.get("output_id")
    if output_id is None:
        if event.reason == _RESOLVE_REASON or not unresolved:
            raise EventValidationError("已选中或核实成功的成员必须关联真实产物")
        return
    output = _required_current_facts(context, "outputs", output_id)
    if output.get("source_action_id") != dependency.get("depends_on_action_id"):
        raise EventValidationError("取回成员的真实产物必须属于固定来源")
    if explicit and output_id != item["requested_output_id"]:
        raise EventValidationError("显式成员的真实产物必须等于原请求 ID")


def _read_permission_guard(event, context) -> None:
    """授予推进一条 SELECTED 项；拒绝保存一条未建档项的最终错误。"""
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
        item = _required_current_facts(context, "obtain_items", row.row_id)
        selection = _required_current_facts(context, "obtain_source_selections", item.get("selection_id"))
        dependency = _required_current_facts(context, "action_dependencies", selection.get("dependency_id"))
        if (delivery.get("action_id") != dependency.get("action_id")
                or delivery.get("output_id") != item.get("output_id")):
            raise EventValidationError("授予回填的交付必须属于原取回动作及同一产物")
        _guard_read_time(event, context, dependency.get("action_id"))
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
    register_guard("obtain_member", _obtain_member_guard)
