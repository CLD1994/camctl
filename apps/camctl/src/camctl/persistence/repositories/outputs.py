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
from decimal import Decimal
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Mapping, Protocol

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId, OperationKey, UtcMicros
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    PathRuleError, object_file_name, relative_file_path, validate_relative_file_path,
)
from camctl.operations.attempts import AttemptTarget, OperationKind, operation_responsibility_key
from camctl.outputs.catalog import OutputKind
from camctl.outputs.competition import has_product_predecessor
from camctl.outputs.copy import (
    AttemptRecord, CopyStateFacts, CopyTargetRef, PreparedCopy, PreparedDisposition,
    PreparedRequest, PreparedSaveOutcome, RecopyDisposition, RecopyRegistration,
    RecopyResult, ReliableSegment, SegmentSaveDisposition, SegmentSaveOutcome,
    SourceChecksumSupport, TargetResetDecision, TargetResetOutcome, TargetResetRequest,
    VerificationDisposition, VerificationResult, VerificationSave,
)
from camctl.outputs.handoff import (
    DeliveryFacts, DeliveryFailure, DeliveryStateFacts, FailureSaveDisposition,
    FailureSaveOutcome, IntentDisposition, IntentSaveOutcome, PublicationDisposition,
    PublicationIntentRequest, PublicationSaveOutcome, PublicationSaveRequest,
    UnconfirmedFailureSave,
)
from camctl.outputs.work_files import (
    CleanupChecked, CleanupCheckedDisposition, CleanupCheckedOutcome,
    CleanupIntent, CleanupIntentDisposition, CleanupIntentOutcome,
    CleanupResultDisposition, CleanupResultSave, CleanupResultSaveOutcome,
    RetentionDisposition, RetentionRelease, RetentionReleaseOutcome,
    WorkFileFailure, WorkFileFacts, WorkFileOutcome,
)
from camctl.outputs.definitions import read_selection_request
from camctl.outputs.obtain_summary import (
    ObtainFacts,
    ObtainItemStage,
    ObtainPhase,
    ObtainSourceFacts,
    decide_obtain_finish,
    obtain_item_stage,
)
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
    action_error_id, item_error_id, registered_error, registered_error_spec,
    validate_error_details,
)
from camctl.outputs.qualification import (
    FileCandidate,
    FileQualification,
    OperationConfig,
    QualificationOutcome,
)
from camctl.outputs.slots import SlotDecision, SlotOutcome, SlotRequest
from camctl.scheduling.order import (
    ActionCandidate as _OrderAction,
    FileCandidate as _OrderFile,
    file_key as _file_order_key,
)

_SOURCE_RESOLVED_EVENT = 3
_TARGETS_FIXED_EVENT = 4
_CLEANUP_CHANGED_EVENT = 24

#: TARGETS_FIXED 的清理/取消/失败分支与动作类型。
_TARGETS_CLEANUP_REASON = 2
_TARGETS_CANCEL_REASON = 3
_TARGETS_FAIL_REASON = 4
_DELETE_ACTION_TYPE = 5
_CANCEL_ACTION_TYPE = 6
_TARGET_PENDING = 1
_TARGET_FIXED = 2
_TARGET_FAILED = 3

_CLEANUP_ITEM_STATUS = enum_for("cleanup_items.status")
_CLEANUP_RESTRICTION = enum_for("cleanup_items.restriction_state")
_CLEANUP_OUTCOME = enum_for("cleanup_items.outcome")
_ACTION_STARTED_EVENT = 5
_READ_PERMISSION_EVENT = 21
_COPY_CHANGED_EVENT = 22
_DELIVERY_CHANGED_EVENT = 23
_INTERMEDIATE_FILE_EVENT = 26
_CLEANUP_CURSOR_EVENT = 32
_RECORDING_DECIDED_EVENT = 18
_RECORDING_PROCESSED_EVENT = 19
#: RECORDING_DECIDED 与 RECORDING_PROCESSED 的分支 reason。
_DECIDED_CHECK_REASON = 1
_DECIDED_REPAIR_REASON = 2
_PROCESSED_CHECK_REASON = 1
_PROCESSED_REPAIR_REASON = 2
_PROCESSED_DISCARD_REASON = 3
#: INTERMEDIATE_FILE_CHANGED.CLEANUP_INTENT/RESULT 与 CLEANUP_CURSOR_MOVED.CHECKED。
_CLEANUP_INTENT_REASON = 3
_CLEANUP_RESULT_REASON = 4
_CURSOR_CHECKED_REASON = 1
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

_ACTION_FINISHED_EVENT = 8
_PLAN_STATUS_EVENT = 9
_PLAN_COMPLETE = 3

#: 取回逐项最终失败的动作错误（具体失败项由条目事实表达）。
_OBTAIN_ITEMS_FAILED = "obtain_items_failed"
_OUTPUT_KIND = enum_for("outputs.kind")
_DEVICE_FILE_ROLE = enum_for("device_files.role")
_DEVICE_FILE_COMPLETION = enum_for("device_files.completion_state")
_INTERMEDIATE_PURPOSE = enum_for("intermediate_files.purpose")
_CAPTURE_TYPES = frozenset(
    int(member.value)
    for member in _ACTION_TYPE
    if member.name in {"CAMERA_TAKE_PHOTO", "CAMERA_RECORD", "CAMERA_TIMELAPSE"}
)
_SOURCE_RESOLUTION_FAILED_CODE = action_error_id("source_resolution_failed")
_CHECK_STATE = enum_for("recording_processing.check_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_DISCARD_STATE = enum_for("recording_processing.discard_state")

#: 录像处理中仍未结束的状态；存在即表示适用产物处理未完成。
_UNFINISHED_PROCESSING_CHECK = frozenset({int(_CHECK_STATE.RUNNING)})
_UNFINISHED_PROCESSING_REPAIR = frozenset(
    {int(member) for member in _REPAIR_STATE
     if member.name in {"PENDING", "RUNNING"}}
)
_UNFINISHED_PROCESSING_DISCARD = frozenset(
    {int(member) for member in _DISCARD_STATE
     if member.name in {"PENDING", "RUNNING"}}
)

#: READ_PERMISSION_CHANGED 的分支。
_GRANT_REASON = 1
_REJECT_REASON = 2
_RESOLVE_REASON = 4
#: COPY_CHANGED.SLOT：保存设备读取机会变化。
_SLOT_REASON = 6
_RESET_REASON = 5
#: COPY_CHANGED.SEGMENT：保存已同步的可靠段进度。
_SEGMENT_REASON = 2
#: COPY_CHANGED.VERIFY/RECOPY/CONFIGURE：保存校验、重拷轮次与判定上限。
_VERIFY_REASON = 3
_RECOPY_REASON = 4
_CONFIGURE_REASON = 7
#: READ_PERMISSION_CHANGED.RELEASE：解除原源文件保护。
_RELEASE_REASON = 3
#: DELIVERY_CHANGED.PREPARE：保存副本准备阶段。
_PREPARE_REASON = 2
#: DELIVERY_CHANGED.INTENT/PUBLISH/FAIL：发布意图、可靠交接完成与终局失败。
_INTENT_REASON = 3
_PUBLISH_REASON = 4
_DELIVERY_FAIL_REASON = 5
#: INTERMEDIATE_FILE_CHANGED.LIFECYCLE：保存完整字节及保留用途变化。
_LIFECYCLE_REASON = 2
#: 资格逐项最终失败的错误码（workflow-codes.json 权威装载）。
_OUTPUT_UNAVAILABLE_CODE = item_error_id("obtain_items", "output_unavailable")
_OUTPUT_NOT_FOUND_CODE = item_error_id("obtain_items", "output_not_found")
_OUTPUT_SOURCE_MISMATCH_CODE = item_error_id("obtain_items", "output_source_mismatch")
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

    def __post_init__(self) -> None:
        ObjectId(self.selection_id)
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事件时间超出 SQLite 整数微秒范围: {self.occurred_at!r}")


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
        if not isinstance(self._command, FixSelection):
            raise TypeError("选择固定申请必须使用 FixSelection")
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
        reads = _CatalogReads(connection)
        reads.require_empty_selection(command.selection_id)
        snapshot = command.snapshot
        if not snapshot.is_fixed:
            raise TransactionError("未完成选择不能固定")
        if (owner["type"] != _OBTAIN_TYPE
                or owner["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED)
                or source["type"] not in _CAPTURE_TYPES
                or owner["resolved_source_plan_id"] != source["plan_id"]):
            raise ConsistencyError("首次选择要求已固定的取回来源及同计划拍摄成员")
        if owner["status"] != int(_ACTION_STATUS.RUNNING) or owner["cancel_requested"]:
            raise TransactionError("选择固定要求取回动作执行中且未取消")
        if source["status"] not in _ACTION_TERMINAL:
            raise TransactionError(
                f"来源动作未终态不能固定选择: {source_action_id}"
            )
        if not _processing_completed_facts(reads.processing(source_action_id)):
            raise TransactionError(
                f"来源适用产物处理未完成: {source_action_id}"
            )

        self._owners[("obtain_source_selections", command.selection_id)] = (
            "action", owner_action_id,
        )
        try:
            mode, requested = read_selection_request(owner["execution_spec_json"], owner["input_fields_json"])
        except ValueError as error:
            raise ConsistencyError("已保存的取回选择定义或原请求无法解释") from error
        members = reads.catalog(source_action_id)
        entries = tuple(member.entry for member in members)
        facts = SelectionFacts(source_action_id, True, entries,
                               checked_output_sources=reads.checked_sources(entries, requested))
        expected = select_outputs(
            SourceResolution(state=ResolutionState.FIXED, member_action_ids=(source_action_id,),
                             source_plan_id=source["plan_id"]), facts, mode, requested)
        if not _same_selection(snapshot, expected):
            raise TransactionError("选择快照与已保存请求及当前来源事实不一致")
        for table, current in reads.state_rows.items():
            self._state.setdefault(table, {}).update(current)

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
            read_coverage=reads.read_coverage(),
        )

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"])
                != (_TARGETS_FIXED_EVENT, _OBTAIN_REASON)):
            raise TransactionError("操作身份已用于其他阶段，不能作为选择固定重送")
        event = saved[0]
        command = self._command
        selections = [row for row in event["body"]["rows"] if row["table"] == "obtain_source_selections"]
        if len(selections) != 1:
            raise ConsistencyError("原选择固定事务必须包含一个来源选择")
        selected = selections[0]
        if (not json_equal(selected["id"], command.selection_id)
                or not json_equal(event["occurred_at"], command.occurred_at)):
            raise TransactionError("选择固定的目标或事实时刻与原事务不同")
        items = sorted((row for row in event["body"]["rows"] if row["table"] == "obtain_items"),
                       key=lambda row: row["id"])
        if any(not json_equal(row["after"]["values"]["selection_id"], command.selection_id) for row in items):
            raise ConsistencyError("原选择固定事务包含其他来源选择的条目")
        after = selected["after"]["values"]
        snapshot = SelectionSnapshot(
            is_fixed=True,
            source_error_code=after.get("error_code"),
            items=tuple(_selected_item(row["after"]["values"]) for row in items),
        )
        if not _same_selection(command.snapshot, snapshot):
            raise TransactionError("选择固定的完整快照与原事务不同")
        _check_fixed_selection(connection, command.selection_id, after, items)
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            result=SelectionSaved(selection_id=command.selection_id, snapshot=snapshot),
            read_only=True,
        )


def _check_fixed_selection(connection, selection_id, selected, items) -> None:
    """原成员与固定依据必须仍存在；后续进度不参与首次响应。"""
    selection = row_facts(connection, "obtain_source_selections", selection_id)
    if selection is None or any(not json_equal(selection[name], selected.get(name))
            for name in ("status", "error_code", "error_details_json")):
        raise ConsistencyError(f"已提交选择固定的当前来源选择不一致: {selection_id}")
    changing = {"status", "output_id", "source_dependency", "delivery_id", "error_code", "error_details_json"}
    with closing(connection.execute(
        "SELECT id FROM obtain_items WHERE selection_id = ? ORDER BY id", (selection_id,),
    )) as cursor:
        # 逐项读取并核对完整成员；多出的第一项即足以证明集合不一致。
        for original in items:
            current_id = cursor.fetchone()
            if current_id is None or current_id[0] != original["id"]:
                raise ConsistencyError(f"已固定来源选择的成员缺失或改变: {selection_id}")
            current = row_facts(connection, "obtain_items", current_id[0])
            initial = original["after"]["values"]
            if (current is None or any(not json_equal(current[name], value)
                           for name, value in initial.items() if name not in changing)):
                raise ConsistencyError(f"已固定取回项的创建身份或选择依据改变: {original['id']}")
            output_id = current["output_id"]
            if initial["status"] == int(_ITEM_STATUS.UNRESOLVED):
                output_valid = output_id is None or output_id == initial["requested_output_id"]
            else:
                output_valid = output_id == initial["output_id"]
            terminal = initial["status"] in (int(_ITEM_STATUS.FAILED), int(_ITEM_STATUS.CANCELED))
            if (not output_valid
                    or (initial["status"] != int(_ITEM_STATUS.UNRESOLVED)
                        and current["status"] == int(_ITEM_STATUS.UNRESOLVED))
                    or (terminal and any(not json_equal(current[name], initial[name]) for name in changing))):
                raise ConsistencyError(f"已固定取回项的目标或终态改变: {original['id']}")
            known = _known_output_ids(initial) | _known_output_ids(current)
            if initial["status"] == int(_ITEM_STATUS.UNRESOLVED):
                known.add(initial["requested_output_id"])
            _require_output_records(connection, known)
        if cursor.fetchone() is not None:
            raise ConsistencyError(f"已固定来源选择的成员增加: {selection_id}")


def _decoded(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return dict(raw)
    try:
        return parse_exact_json(raw)
    except ValueError as error:
        raise ConsistencyError("已保存产物选择的错误详情不是有效精确 JSON") from error


def _same_selection(left: SelectionSnapshot, right: SelectionSnapshot) -> bool:
    """逐项精确比较完整快照，不复制整组条目；布尔值不能充当编号。"""
    def item_facts(item):
        return {**{field.name: getattr(item, field.name) for field in fields(SelectedItem)},
                "error_details": None if item.error_details is None else dict(item.error_details)}

    return (left.is_fixed is right.is_fixed
            and json_equal(left.source_error_code, right.source_error_code)
            and len(left.items) == len(right.items)
            and all(json_equal(item_facts(actual), item_facts(expected))
                    for actual, expected in zip(left.items, right.items)))


def _processing_completed(connection, source_action_id: int) -> bool:
    """来源动作的适用产物处理是否已完成（录像检查/修复/丢弃）。"""
    return _processing_completed_facts(_CatalogReads(connection).processing(source_action_id))


def _processing_completed_facts(row) -> bool:
    if row is None:
        return True
    try:
        states = tuple(row[column] for column in ("check_state", "repair_state", "discard_state"))
        if not all(is_json_integer(value) for value in states):
            raise ValueError("处理状态必须是登记的整数编号")
        check_state, repair_state, discard_state = (
            enum(value) for enum, value in zip((_CHECK_STATE, _REPAIR_STATE, _DISCARD_STATE), states))
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("来源处理状态缺失或无效") from error
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


def _internal_input_requirement(processing) -> str | None:
    """三类准备记录均无时的当前内部输入需求判定。

    返回 None 表示存在输入需求（独立检查或修复需求）；否则返回只
    读不授予的原因。工具阶段已证明输入存在、或检查需求仍在而修
    复已终局结束（2026-10-03 决策按不可解释状态处理）时抛一致性
    错误。检查已失败或未确认时不保留待执行修复的输入需求。
    """
    try:
        decision = _CHECK_DECISION(processing["check_decision"])
        check = _CHECK_STATE(processing["check_state"])
        repair = _REPAIR_STATE(processing["repair_state"])
        discard = _DISCARD_STATE(processing["discard_state"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原录像的处理决定或阶段缺失或无法解释") from error
    if _processing_requires_existing_input(processing):
        raise ConsistencyError("原片工具处理已经开始，但原输入准备责任缺失")
    if discard in (_DISCARD_STATE.PENDING, _DISCARD_STATE.RUNNING):
        return "discard_active"
    if decision is _CHECK_DECISION.REQUIRED and check is _CHECK_STATE.NOT_PERFORMED:
        if repair in (_REPAIR_STATE.FAILED, _REPAIR_STATE.CANCELED):
            raise ConsistencyError("检查需求仍在而修复已终局结束，处理阶段组合不可解释")
        return None
    if repair is _REPAIR_STATE.PENDING:
        if check in (_CHECK_STATE.FAILED, _CHECK_STATE.UNCONFIRMED):
            return "input_need_ended"
        return None
    if (check in (_CHECK_STATE.FAILED, _CHECK_STATE.UNCONFIRMED)
            or repair in (_REPAIR_STATE.FAILED, _REPAIR_STATE.CANCELED)):
        return "input_need_ended"
    if decision is _CHECK_DECISION.UNDETERMINED or repair is _REPAIR_STATE.UNDETERMINED:
        return "input_undecided"
    return "input_not_needed"


def _read_is_due(action, occurred_at: int) -> bool:
    """首次读取建档的时间条件；不代替会话墙钟可信性检查。"""
    if action is None or not is_json_integer(action.get("scheduled_at")):
        raise ConsistencyError("读取发起动作的计划时间缺失或无法解释")
    return action["scheduled_at"] <= occurred_at


def _read_owner_eligible(action) -> bool:
    """首次建档的发起动作必须仍在执行且未取消；缺事实不能当作可执行。"""
    if action is None:
        raise ConsistencyError("读取发起动作缺失")
    status, canceled = action.get("status"), action.get("cancel_requested")
    if (not is_json_integer(status) or not is_json_integer(canceled)
            or canceled not in (0, 1)):
        raise ConsistencyError("读取发起动作的状态或取消标志无效")
    try:
        status = _ACTION_STATUS(int(status))
    except ValueError as error:
        raise ConsistencyError("读取发起动作的状态未登记") from error
    return status == _ACTION_STATUS.RUNNING and canceled == 0


def _guard_read_owner(event, context, action_id) -> None:
    action = context.state_rows.get("actions", {}).get(action_id)
    try:
        if not _read_owner_eligible(action):
            raise EventValidationError("首次读取建档要求发起动作执行中且未取消")
        due = _read_is_due(action, event.occurred_at)
    except ConsistencyError as error:
        raise EventValidationError(str(error)) from error
    if not due:
        raise EventValidationError("读取发起动作尚未到计划时间")


@dataclass(frozen=True)
class _CatalogMember:
    """已核对文件关系的产物行及其选择依据。"""

    row: Mapping[str, Any]
    entry: CatalogEntry


class _FamilyReads(Protocol):
    """一份原片关系解释所需的可靠行及完整关联范围。"""

    def required(self, table: str, identity: int) -> Mapping[str, Any]: ...
    def origin(self, identity: int) -> int | None: ...
    def related(self, original_id: int) -> tuple[int, ...]: ...


class _CatalogReads:
    """在同一数据库读取快照中保留原始事实及成功查询的范围。"""

    def __init__(self, connection):
        self.connection = connection
        self.state_rows: dict[str, dict[int, dict[str, Any]]] = {}
        self._ranges: dict[tuple[str, str], set[int]] = {}
        self._full_rows: set[tuple[str, int]] = set()

    def _covered(self, table, column, identity):
        self._ranges.setdefault((table, column), set()).add(identity)

    def read_coverage(self) -> ReadCoverage:
        return ReadCoverage(self._ranges)

    def _remember(self, table, identity, facts, *, complete=False):
        """合并同一快照的已读字段；局部读取不能使完整行退化。"""
        rows = self.state_rows.setdefault(table, {})
        existing = rows.get(identity)
        if existing is None:
            existing = rows[identity] = facts
        else:
            for column, value in facts.items():
                if column in existing and not json_equal(existing[column], value):
                    raise ConsistencyError(f"同一读取快照中的记录事实矛盾: {table}#{identity}.{column}")
            for column, value in facts.items():
                existing.setdefault(column, value)
        if complete:
            self._full_rows.add((table, identity))
        return existing

    def processing(self, source_action_id: int) -> dict[str, Any] | None:
        with closing(self.connection.execute(
            "SELECT check_state, repair_state, discard_state, id, action_id FROM recording_processing"
            " WHERE action_id = ?", (source_action_id,),
        )) as cursor:
            row = cursor.fetchone()
        facts = None
        if row is not None:
            facts = dict(zip(("check_state", "repair_state", "discard_state", "id", "action_id"), row))
            facts = self._remember("recording_processing", row[3], facts)
        self._covered("recording_processing", "action_id", source_action_id)
        return facts

    def require_empty_selection(self, selection_id: int) -> None:
        with closing(self.connection.execute(
            "SELECT 1 FROM obtain_items WHERE selection_id=? LIMIT 1", (selection_id,),
        )) as cursor:
            has_items = cursor.fetchone() is not None
        if has_items:
            raise ConsistencyError("尚未固定的来源选择不能已有目标条目")
        self._covered("obtain_items", "selection_id", selection_id)

    def checked_sources(self, entries, identities) -> dict[int, int | None]:
        """只补查目录外的实际目标；失败不返回部分核实结果。"""
        local_ids = {entry.output_id for entry in entries}
        query_ids = sorted(set(identities) - local_ids)
        checked: dict[int, int | None] = {}
        for offset in range(0, len(query_ids), 128):
            batch = query_ids[offset:offset + 128]
            placeholders = ",".join("?" for _ in batch)
            with closing(self.connection.execute(
                f"SELECT id, source_action_id FROM outputs WHERE id IN ({placeholders})", batch,
            )) as cursor:
                rows = cursor.fetchall()
            checked.update(dict.fromkeys(batch))
            for identity, source in rows:
                self._remember("outputs", identity, {
                    "id": identity, "source_action_id": source})
                checked[identity] = source
            for identity in batch:
                self._covered("outputs", "id", identity)
        return checked

    def required(self, table: str, identity: int) -> dict[str, Any]:
        ObjectId(identity)
        rows = self.state_rows.setdefault(table, {})
        if (table, identity) not in self._full_rows:
            facts = row_facts(self.connection, table, identity)
            if facts is None:
                raise ConsistencyError(f"产物关联记录缺失: {table}#{identity}")
            self._remember(table, identity, facts, complete=True)
            self._covered(table, "id", identity)
        return rows[identity]

    def origin(self, identity: int) -> int | None:
        with closing(self.connection.execute(
            "SELECT original_output_id, id, output_id FROM output_origins WHERE output_id=?", (identity,),
        )) as cursor:
            row = cursor.fetchone()
        if row is not None:
            self._remember("output_origins", row[1], {
                "id": row[1], "output_id": row[2], "original_output_id": row[0]})
        self._covered("output_origins", "output_id", identity)
        return row[0] if row is not None else None

    def related(self, original_id: int) -> tuple[int, ...]:
        with closing(self.connection.execute(
            "SELECT output_id, id, original_output_id FROM output_origins"
            " WHERE original_output_id=? ORDER BY output_id LIMIT 3", (original_id,),
        )) as cursor:
            rows = cursor.fetchall()
        if len(rows) > 2:
            raise ConsistencyError("同一原片最多登记一份预览和一份修复成品")
        for output_id, identity, original in rows:
            self._remember("output_origins", identity, {
                "id": identity, "output_id": output_id, "original_output_id": original})
        self._covered("output_origins", "original_output_id", original_id)
        return tuple(row[0] for row in rows)

    def catalog(self, source_action_id: int) -> tuple[_CatalogMember, ...]:
        """分批枚举完整来源；只有读取和解释全部成功才声明完整范围。"""
        ObjectId(source_action_id)
        members: dict[int, _CatalogMember] = {}
        with closing(self.connection.execute(
            "SELECT id FROM outputs WHERE source_action_id = ? ORDER BY id", (source_action_id,),
        )) as cursor:
            while batch := cursor.fetchmany(128):
                for (output_id,) in batch:
                    _include_family(self, members, source_action_id, output_id)
        self._covered("outputs", "source_action_id", source_action_id)
        return tuple(sorted(members.values(), key=lambda member: member.entry.output_id))


def _include_family(reads: _FamilyReads, members, source_action_id, output_id) -> None:
    if output_id in members:
        return
    for member in _family_members(reads, output_id):
        if member.row["source_action_id"] != source_action_id:
            raise ConsistencyError("产物关联集合不属于指定来源")
        members[member.entry.output_id] = member


class _CurrentCatalogReads:
    """按当前行建立关联索引，查询不扫描未来提案或重复扫描全部关联。"""

    def __init__(self, context):
        self.context = context
        self._origins = {}
        self._related = {}
        for row in context.state_rows.get("output_origins", {}).values():
            try:
                output_id, original_id = row["output_id"], row["original_output_id"]
                for identity in (output_id, original_id):
                    if not is_json_integer(identity):
                        raise ValueError("关联身份必须是整数")
                    ObjectId(int(identity))
            except (KeyError, ValueError) as error:
                raise ConsistencyError("产物原片关联身份缺失或无效") from error
            if output_id in self._origins:
                raise ConsistencyError("同一产物不能重复登记原片关联")
            self._origins[int(output_id)] = int(original_id)
            self._related.setdefault(int(original_id), []).append(int(output_id))

    def required(self, table: str, identity: int) -> Mapping[str, Any]:
        if not is_json_integer(identity):
            raise ConsistencyError(f"产物关联身份无效: {table}#{identity!r}")
        try:
            identity = ObjectId(int(identity))
        except ValueError as error:
            raise ConsistencyError(f"产物关联身份越界: {table}#{identity!r}") from error
        facts = self.context.state_rows.get(table, {}).get(identity)
        if facts is None:
            raise ConsistencyError(f"产物关联记录缺失: {table}#{identity}")
        if "id" in facts:
            if not is_json_integer(facts["id"]) or facts["id"] != identity:
                raise ConsistencyError(f"产物关联行身份矛盾: {table}#{identity}")
            return facts
        return {**facts, "id": int(identity)}

    def _require_coverage(self, column, identity):
        if not self.context.read_coverage.covers("output_origins", column, identity):
            raise EventValidationError(f"缺少原片关联完整读取范围: {column}={identity}")

    def origin(self, identity: int) -> int | None:
        self._require_coverage("output_id", identity)
        return self._origins.get(identity)

    def related(self, original_id: int) -> tuple[int, ...]:
        self._require_coverage("original_output_id", original_id)
        return tuple(sorted(self._related.get(original_id, ())))

    def catalog(self, source_action_id: int) -> tuple[_CatalogMember, ...]:
        rows = self.context.complete_rows("outputs", "source_action_id", source_action_id)
        members: dict[int, _CatalogMember] = {}
        for output_id in sorted(rows):
            _include_family(self, members, source_action_id, output_id)
        return tuple(sorted(members.values(), key=lambda member: member.entry.output_id))


class _ProductReads(_CatalogReads):
    """逐产物候选的 SQL 读取；只为需要进一步判断的动作加载完整原请求。"""

    def __init__(self, connection, state_rows):
        super().__init__(connection)
        self.state_rows = state_rows
        for table, rows in state_rows.items():
            for identity in rows:
                self._full_rows.add((table, identity))
                self._covered(table, "id", identity)

    def optional(self, table, identity):
        ObjectId(identity)
        if (table, identity) not in self._full_rows:
            row = row_facts(self.connection, table, identity)
            if row is not None:
                self._remember(table, identity, row, complete=True)
            self._full_rows.add((table, identity))
            self._covered(table, "id", identity)
        return self.state_rows.get(table, {}).get(identity)

    _SOURCE_COLUMNS = ("id", "type", "status", "plan_id", "name", "group_name")

    def action(self, identity):
        """来源只读元数据；完整候选请求仍由 required 按需补读。"""
        ObjectId(identity)
        row = self.state_rows.get("actions", {}).get(identity)
        if row is not None and all(column in row for column in self._SOURCE_COLUMNS):
            self._covered("actions", "id", identity)
            return row
        if identity not in self._ranges.get(("actions", "id"), ()):
            with closing(self.connection.execute(
                f"SELECT {', '.join(self._SOURCE_COLUMNS)} FROM actions WHERE id=?", (identity,),
            )) as cursor:
                values = cursor.fetchone()
            if values is not None:
                row = self._remember("actions", identity, dict(zip(self._SOURCE_COLUMNS, values)))
            self._covered("actions", "id", identity)
        return row

    def plan_actions(self, plan_id):
        """完整计划成员范围包括无关分组和终态动作，但不读取它们的正文。"""
        ObjectId(plan_id)
        if plan_id not in self._ranges.get(("actions", "plan_id"), ()):
            with closing(self.connection.execute(
                f"SELECT {', '.join(self._SOURCE_COLUMNS)} FROM actions WHERE plan_id=? ORDER BY id",
                (plan_id,),
            )) as cursor:
                while batch := cursor.fetchmany(128):
                    for values in batch:
                        self._remember("actions", values[0], dict(zip(self._SOURCE_COLUMNS, values)))
                        self._covered("actions", "id", values[0])
            self._covered("actions", "plan_id", plan_id)
        return {identity: row for identity, row in self.state_rows.get("actions", {}).items()
                if row["plan_id"] == plan_id}

    def members(self, table, column, value):
        # 表和列仅由内部规则提供；完整范围不包含隐含的状态过滤。
        if value not in self._ranges.get((table, column), ()):
            with closing(self.connection.execute(
                f"SELECT id FROM {table} WHERE {column}=? ORDER BY id", (value,),
            )) as cursor:
                while batch := cursor.fetchmany(128):
                    for (identity,) in batch:
                        self.required(table, identity)
            self._covered(table, column, value)
        return {identity: row for identity, row in self.state_rows.get(table, {}).items()
                if row[column] == value}

    def actions(self):
        states = (int(_ACTION_STATUS.PENDING), int(_ACTION_STATUS.RUNNING))
        columns = ("id", "type", "status", "scheduled_at", "cancel_requested", "plan_id", "input_index")
        with closing(self.connection.execute(
            f"SELECT {', '.join(columns)} FROM actions WHERE status IN (?, ?) ORDER BY id", states,
        )) as cursor:
            while batch := cursor.fetchmany(128):
                for row in batch:
                    self._remember("actions", row[0], dict(zip(columns, row)))
        for state in states:
            self._covered("actions", "status", state)
        # 后续按需读取来源动作会扩展事实字典，枚举只保留本次动作身份。
        return tuple((identity, row) for identity, row in self.state_rows["actions"].items()
                     if row["status"] in states)

    def family(self, output_id):
        return _original_outputs(self, output_id)

    def processing_completed(self, source_id):
        return _processing_completed_facts(self.processing(source_id))


class _CurrentProductReads(_CurrentCatalogReads):
    """正式资格守卫复核同一范围的当前行，不读取事务未来提案。"""

    def optional(self, table, identity):
        rows = self.context.complete_rows(table, "id", identity)
        return self.required(table, identity) if rows else None

    def members(self, table, column, value):
        rows = self.context.complete_rows(table, column, value)
        return {identity: self.required(table, identity) for identity in rows}

    def action(self, identity):
        return self.optional("actions", identity)

    def plan_actions(self, plan_id):
        return self.members("actions", "plan_id", plan_id)

    def actions(self):
        rows = {}
        for state in (_ACTION_STATUS.PENDING, _ACTION_STATUS.RUNNING):
            rows.update(self.members("actions", "status", int(state)))
        return tuple(rows.items())

    def family(self, output_id):
        return _original_outputs(self, output_id)

    def processing_completed(self, source_id):
        rows = self.members("recording_processing", "action_id", source_id)
        if len(rows) > 1:
            raise ConsistencyError("录像处理责任重复")
        return _processing_completed_facts(next(iter(rows.values()), None))


def _original_outputs(reads, output_id):
    members = _family_members(reads, output_id)
    by_kind = {member.entry.kind: member.entry for member in members}
    return OriginalOutputs(members[0].row["source_action_id"], by_kind[OutputKind.ORIGINAL],
                           by_kind.get(OutputKind.PREVIEW), by_kind.get(OutputKind.REPAIRED))


def _catalog_members(connection, source_action_id: int) -> tuple[_CatalogMember, ...]:
    return _CatalogReads(connection).catalog(source_action_id)


def load_selection_facts(
    connection,
    source_action_id: int,
    *,
    requested_output_ids: tuple[int, ...] = (),
    previously_confirmed_ids: frozenset[int] = frozenset(),
) -> SelectionFacts:
    """读取一个来源动作的选择事实。

    source_completed 由动作终态与适用产物处理共同推导。完整目录
    证明本来源成员；只补查目录外的实际请求及此前确认 ID，并保留
    实际归属和可靠不存在。调用方在同一数据库读取快照中使用。
    """
    ObjectId(source_action_id)
    requested = frozenset(ObjectId(identity) for identity in requested_output_ids)
    previous = frozenset(ObjectId(identity) for identity in previously_confirmed_ids)
    source = row_facts(connection, "actions", source_action_id)
    if source is None:
        raise ConsistencyError(f"来源动作不存在: {source_action_id}")
    completed = (
        source["status"] in _ACTION_TERMINAL
        and _processing_completed(connection, source_action_id)
    )
    entries = tuple(member.entry for member in _catalog_members(connection, source_action_id))
    return SelectionFacts(
        source_action_id=source_action_id,
        source_completed=completed,
        outputs=entries,
        checked_output_sources=_checked_output_sources(connection, entries, requested | previous),
        previously_confirmed_ids=previous,
    )


def _checked_output_sources(connection, entries, identities) -> dict[int, int | None]:
    """完整目录之外的实际目标按有界批次核实；失败不返回部分事实。"""
    return _CatalogReads(connection).checked_sources(entries, identities)


def load_output_family(connection, output_id: int) -> OriginalOutputs:
    """只读取得当前产物所属的一份原片及派生关系，最多三个产物。

    目标必须已经登记；派生关系缺失与可靠没有派生产物分别处理。
    第三个派生 ID 只用于识别超出一份预览及一份修复成品的矛盾，
    不读取同一来源的其他原片或全库目录。调用方使用同一事务快照。
    """
    return _original_outputs(_CatalogReads(connection), output_id)


def _load_family_members(connection, output_id: int) -> tuple[_CatalogMember, ...]:
    return _family_members(_CatalogReads(connection), output_id)


def _family_members(reads: _FamilyReads, output_id: int) -> tuple[_CatalogMember, ...]:
    """逐产物、完整来源及当前事实解释共用的固定关系边界。"""
    ObjectId(output_id)
    required, origin = reads.required, reads.origin

    target = required("outputs", output_id)
    original_id = origin(output_id)
    if target["kind"] == int(_OUTPUT_KIND.ORIGINAL):
        if original_id is not None:
            raise ConsistencyError("原片不能携带派生关联")
        original = target
        original_id = output_id
    else:
        if original_id is None:
            raise ConsistencyError("派生产物缺少原片关联")
        original = required("outputs", original_id)
        if original["kind"] != int(_OUTPUT_KIND.ORIGINAL) or origin(original_id) is not None:
            raise ConsistencyError("派生产物必须指向没有派生关联的原片")
    related_ids = reads.related(original_id)
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
        by_kind[kind] = _CatalogMember(
            row=entry,
            entry=CatalogEntry(
                output_id=identity, kind=kind, availability=entry["availability"], size_bytes=size,
                original_output_id=None if identity == original_id else original_id,
            ),
        )
    if output_id not in (original_id, *related_ids):
        raise ConsistencyError("目标产物未保留在原片派生关系中")
    return tuple(by_kind.values())


def _known_output_ids(values: Mapping[str, Any]) -> set[int]:
    """保存的关联及来源不匹配证明记录存在；裸请求不构成存在证明。"""
    known = {values.get(name) for name in ("output_id", "original_output_id", "preview_output_id")} - {None}
    if values.get("error_code") == _OUTPUT_SOURCE_MISMATCH_CODE:
        requested = values.get("requested_output_id")
        if requested is None:
            raise ConsistencyError("来源不匹配的取回项缺少原请求产物身份")
        known.add(requested)
    return known


def _require_output_records(connection, identities: set[int]) -> None:
    for identity in sorted(identities):
        with closing(connection.execute("SELECT 1 FROM outputs WHERE id = ?", (identity,))) as cursor:
            if cursor.fetchone() is None:
                raise ConsistencyError(f"已保存取回项引用的产物记录缺失: {identity}")


def _selected_item(values: Mapping[str, Any]) -> SelectedItem:
    return SelectedItem(**{
        field.name: (_decoded(values["error_details_json"]) if field.name == "error_details" else values[field.name])
        for field in fields(SelectedItem)
    })


def _saved_items(connection, selection_id: int) -> list[SelectedItem]:
    columns = ("requested_output_id", "output_id", "basis", "original_output_id", "preview_output_id",
               "preview_size", "repaired_size", "status", "error_code", "error_details_json")
    items: list[SelectedItem] = []
    with closing(connection.execute(
        f"SELECT {', '.join(columns)} FROM obtain_items WHERE selection_id = ? ORDER BY id", (selection_id,),
    )) as cursor:
        while rows := cursor.fetchmany(128):
            for row in rows:
                values = dict(zip(columns, row))
                _require_output_records(connection, _known_output_ids(values))
                items.append(_selected_item(values))
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
    同产物较早候选作为可等待拒绝返回，不落库；相机机会另行取得。
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
        self._read_coverage = ReadCoverage()
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
        if not _read_owner_eligible(action):
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
        else:
            requirement = _internal_input_requirement(
                self._state["recording_processing"][command.processing_id])
            if requirement is not None:
                return self._wait(requirement)
        if not _read_is_due(action, command.occurred_at):
            return self._wait("not_due")
        readiness = self._source_readiness(scope, output, source_file)
        if readiness is not None:
            return readiness

        if output is not None:
            reads = _ProductReads(connection, self._state)
            if has_product_predecessor(reads, command.action_id, command.output_id, command.occurred_at):
                return self._wait("business_order")
            self._read_coverage = reads.read_coverage()

        return self._grant(scope, source_file)

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
        for identity in _known_output_ids(item):
            if identity not in self._state["outputs"]:
                self._required(connection, "outputs", identity)
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

    # ---- 授予建档 ----

    def _grant(self, scope, source_file) -> CommandPlan:
        command = self._command
        is_delivery = command.item_id is not None
        target_file_id = next_row_id(scope.connection, "intermediate_files")
        delivery_id = (
            next_row_id(scope.connection, "deliveries") if is_delivery else None
        )
        copy_id = next_row_id(scope.connection, "file_copies")
        run_id = next_row_id(scope.connection, "operation_runs")
        specs, owners = self._grant_specs(source_file, target_file_id, delivery_id, copy_id, run_id,
            self._state["obtain_items"][command.item_id] if is_delivery else None)
        self._owners.update(owners)
        return CommandPlan(
            events=self._envelopes(scope, specs), owners=self._owners, state_rows=self._state,
            read_coverage=self._read_coverage,
            result=FileQualification(outcome=QualificationOutcome.GRANTED, copy_id=copy_id,
                run_id=run_id, delivery_id=delivery_id, target_file_id=target_file_id, reason=None),
        )

    def _grant_specs(self, source_file, target_file_id, delivery_id, copy_id, run_id, item):
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
                "slot_device_id": None,
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
            specs, _ = self._grant_specs(source_snapshot,
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
                         or source["sha256"] != source_snapshot["sha256"]))):
            raise ConsistencyError("原建档事务的源内容不一致")
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


#: 机会变化的方向；授予与释放分别是独立事务入口。
_SLOT_GRANT = "grant"
_SLOT_RELEASE = "release"

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_RUN_TERMINAL = frozenset(
    int(_RUN_STATUS[member]) for member in ("SUCCEEDED", "FAILED", "CANCELED", "UNCONFIRMED", "EXPIRED")
)


def _copy_owner_of(connection, copy, state, owners) -> None:
    """解析拷贝的发起责任并把固定关联行保存为当前事实。"""
    if copy["delivery_id"] is not None:
        facts = row_facts(connection, "deliveries", copy["delivery_id"])
        if facts is None:
            raise ConsistencyError(f"机会责任关联记录缺失: deliveries#{copy['delivery_id']}")
        state.setdefault("deliveries", {})[copy["delivery_id"]] = facts
        owners[("file_copies", copy["id"])] = ("delivery", copy["delivery_id"])
        return
    if copy["processing_id"] is not None:
        facts = row_facts(connection, "recording_processing", copy["processing_id"])
        if facts is None:
            raise ConsistencyError(
                f"机会责任关联记录缺失: recording_processing#{copy['processing_id']}")
        state.setdefault("recording_processing", {})[copy["processing_id"]] = facts
        owners[("file_copies", copy["id"])] = ("action", facts["action_id"])
        return
    raise ConsistencyError("拷贝缺少交付或处理归属")


class _SlotChangeCommand:
    """相机读取机会的授予或释放事务命令。

    使用已建档拷贝和原责任，不创建替代记录。授予在写事务内核对
    当前持有者、资格及候选排序后保存归属；释放核对原责任和实际
    读取及重试结束，不能凭迟到通知清空新归属。
    """

    _TABLES = (
        "file_copies", "device_files", "operation_runs", "operation_attempts",
        "actions", "deliveries", "recording_processing",
    )

    def __init__(self, request: SlotRequest, direction: str, key: OperationKey) -> None:
        if not isinstance(request, SlotRequest):
            raise TypeError("机会变化申请必须使用 SlotRequest")
        if direction not in (_SLOT_GRANT, _SLOT_RELEASE):
            raise ValueError(f"未知的机会变化方向: {direction!r}")
        self._request = request
        self._direction = direction
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._coverage = ReadCoverage()
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        copy = self._load_copy(connection)
        source = self._load_source(connection, copy)
        device_id = self._source_device(connection, source)
        run = self._load_run(connection, copy["id"])
        if self._direction == _SLOT_GRANT:
            action = self._load_owner_action(connection, copy)
            return self._grant(scope, copy, device_id, run, action)
        return self._release(scope, copy, device_id, run)

    # ---- 共同事实加载 ----

    def _required(self, connection, table: str, row_id: int) -> dict:
        facts = row_facts(connection, table, row_id)
        if facts is None:
            raise ConsistencyError(f"机会责任关联记录缺失: {table}#{row_id}")
        self._state[table][row_id] = facts
        return facts

    def _load_copy(self, connection) -> dict:
        copy = self._required(connection, "file_copies", self._request.copy_id)
        if not is_json_integer(copy["id"]):
            raise ConsistencyError("拷贝身份无效")
        self._owners[("file_copies", copy["id"])] = self._copy_owner(connection, copy)
        return copy

    def _copy_owner(self, connection, copy) -> tuple[str, int]:
        _copy_owner_of(connection, copy, self._state, self._owners)
        return self._owners[("file_copies", copy["id"])]

    def _load_source(self, connection, copy) -> dict:
        if copy["source_device_file_id"] is None:
            raise ConsistencyError("主机源拷贝不申请相机读取机会")
        return self._required(connection, "device_files", copy["source_device_file_id"])

    def _source_device(self, connection, source) -> str:
        observer = self._required(connection, "actions", source["observer_action_id"])
        origin = self._required(connection, "actions", source["source_action_id"])
        binding = (observer["device_id"], observer["driver_id"])
        if (not all(isinstance(value, str) and value for value in binding)
                or binding != (origin["device_id"], origin["driver_id"])):
            raise ConsistencyError("读取源观察者与可靠来源的原设备绑定不一致")
        return binding[0]

    def _load_run(self, connection, copy_id: int) -> dict:
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (f"read/{copy_id}",),
        )) as cursor:
            identities = cursor.fetchall()
        if len(identities) != 1:
            raise ConsistencyError("拷贝的 READ_FILE 流程缺失或重复")
        return self._required(connection, "operation_runs", identities[0][0])

    def _load_owner_action(self, connection, copy) -> dict:
        if copy["delivery_id"] is not None:
            action_id = self._state["deliveries"][copy["delivery_id"]]["action_id"]
        elif copy["processing_id"] is not None:
            action_id = self._state["recording_processing"][copy["processing_id"]]["action_id"]
        else:
            raise ConsistencyError("拷贝缺少交付或处理归属")
        return self._required(connection, "actions", action_id)

    def _load_attempts(self, connection, run_id: int) -> None:
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ?", (run_id,),
        )) as cursor:
            identities = [row[0] for row in cursor.fetchall()]
        for identity in identities:
            self._required(connection, "operation_attempts", identity)
        self._coverage = ReadCoverage({("operation_attempts", "run_id"): frozenset({ObjectId(run_id)})})

    # ---- 授予 ----

    def _grant(self, scope, copy, device_id: str, run, action) -> CommandPlan:
        slot = copy["slot_device_id"]
        if slot == device_id:
            return self._decision(SlotOutcome.HELD)
        if slot is not None:
            raise ConsistencyError("拷贝的读取机会不属于原来源设备")
        if run["status"] in _RUN_TERMINAL or not _read_owner_eligible(action):
            return self._decision(SlotOutcome.FINISHED)
        if not _read_is_due(action, self._request.occurred_at):
            return self._decision(SlotOutcome.WAIT, "not_due")
        if run["retry_wait_required"] == 1:
            return self._decision(SlotOutcome.WAIT, "retry_wait")
        if self._device_holder(scope.connection, device_id) is not None:
            return self._decision(SlotOutcome.WAIT, "device_busy")
        eligible, reason = self._first_candidate(scope.connection, device_id)
        if not eligible:
            return self._decision(SlotOutcome.WAIT, reason)
        return self._save(scope, copy, None, device_id, SlotOutcome.GRANTED)

    def _device_holder(self, connection, device_id: str) -> int | None:
        with closing(connection.execute(
            "SELECT id FROM file_copies WHERE slot_device_id = ?", (device_id,),
        )) as cursor:
            rows = cursor.fetchall()
        others = [row[0] for row in rows if row[0] != self._request.copy_id]
        if len(others) > 1:
            raise ConsistencyError("同一设备的读取机会存在多个持有者")
        return others[0] if others else None

    def _first_candidate(self, connection, device_id: str) -> tuple[bool, str]:
        """本拷贝是否为该设备统一文件顺序中最早的合格候选。"""
        occurred_at = self._request.occurred_at
        keys: list[tuple] = []
        mine: tuple | None = None
        with closing(connection.execute(
            "SELECT fc.id AS copy_id, fc.slot_device_id,"
            " df.completion_state, df.observer_action_id,"
            " r.id AS run_id, r.status AS run_status, r.retry_wait_required,"
            " a.id AS action_id, a.scheduled_at, a.plan_id, a.input_index,"
            " a.status AS action_status, a.cancel_requested"
            " FROM file_copies fc"
            " JOIN device_files df ON df.id = fc.source_device_file_id"
            " JOIN actions ob ON ob.id = df.observer_action_id"
            " LEFT JOIN deliveries d ON d.id = fc.delivery_id"
            " LEFT JOIN recording_processing rp ON rp.id = fc.processing_id"
            " LEFT JOIN actions a ON a.id = COALESCE(d.action_id, rp.action_id)"
            " LEFT JOIN operation_runs r ON r.responsibility_key = 'read/' || fc.id"
            " WHERE ob.device_id = ? AND fc.slot_device_id IS NULL",
            (device_id,),
        )) as cursor:
            rows = cursor.fetchall()
        for row in rows:
            copy_id, _, completion, _, run_id, run_status, retry_wait, action_id, \
                scheduled_at, plan_id, input_index, action_status, cancel_requested = row
            if run_id is None or action_id is None:
                raise ConsistencyError("同设备候选拷贝缺少读取流程或发起动作")
            eligible = (
                action_status == int(_ACTION_STATUS.RUNNING)
                and cancel_requested == 0
                and scheduled_at is not None and scheduled_at <= occurred_at
                and run_status not in _RUN_TERMINAL
                and retry_wait == 0
                and completion == 3
            )
            if not eligible:
                continue
            key = _file_order_key(_OrderFile(
                action=_OrderAction(action_id, scheduled_at, plan_id, input_index),
                entry_index=copy_id,
            ))
            keys.append(key)
            if copy_id == self._request.copy_id:
                mine = key
        if mine is None:
            return False, "source_not_ready"
        if any(key < mine for key in keys):
            return False, "predecessor"
        return True, ""

    # ---- 释放 ----

    def _release(self, scope, copy, device_id: str, run) -> CommandPlan:
        slot = copy["slot_device_id"]
        if slot is None:
            return self._decision(SlotOutcome.ALREADY_RELEASED)
        if slot != device_id:
            raise ConsistencyError("拷贝的读取机会不属于原来源设备")
        self._load_attempts(scope.connection, run["id"])
        if any(attempt["status"] == int(_ATTEMPT_STATUS.RUNNING)
               for attempt in self._state["operation_attempts"].values()):
            return self._decision(SlotOutcome.WAIT, "read_active")
        if run["retry_wait_required"] == 1:
            return self._decision(SlotOutcome.WAIT, "retry_wait")
        if run["status"] not in _RUN_TERMINAL and copy["committed_bytes"] < copy["source_size"]:
            return self._decision(SlotOutcome.WAIT, "still_reading")
        return self._save(scope, copy, device_id, None, SlotOutcome.RELEASED)

    # ---- 事件与出口 ----

    def _save(self, scope, copy, before_slot, after_slot, outcome) -> CommandPlan:
        copy_id = copy["id"]
        row = _update(
            "file_copies", copy_id,
            {"slot_device_id": before_slot},
            {"slot_device_id": after_slot},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _COPY_CHANGED_EVENT, _SLOT_REASON, (row,),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            read_coverage=self._coverage,
            result=SlotDecision(outcome=outcome),
        )

    def _decision(self, outcome: SlotOutcome, reason: str | None = None) -> CommandPlan:
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            read_coverage=self._coverage,
            result=SlotDecision(outcome=outcome, reason=reason),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次机会变化响应；不重新判定当前资格或归属。"""
        if len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT or saved[0]["reason"] != _SLOT_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为机会变化重送")
        event = saved[0]
        if event["occurred_at"] != self._request.occurred_at:
            raise TransactionError("机会变化的事实时刻与原事务不同")
        rows = event["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "file_copies" or rows[0]["id"] != self._request.copy_id:
            raise TransactionError("原机会变化的目标拷贝与输入不符")
        row = rows[0]
        before_slot = row["before"]["values"].get("slot_device_id") if row["before"]["exists"] else None
        after_slot = row["after"]["values"].get("slot_device_id")
        if after_slot is not None:
            if self._direction != _SLOT_GRANT or before_slot is not None:
                raise TransactionError("原机会变化的方向与输入不符")
            outcome = SlotOutcome.GRANTED
        else:
            if self._direction != _SLOT_RELEASE or before_slot is None:
                raise TransactionError("原机会变化的方向与输入不符")
            outcome = SlotOutcome.RELEASED
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=SlotDecision(outcome=outcome),
        )


#: 中间文件用途编号与共用文件模型的对应；所有入口共用一份映射。
_PURPOSE_MODELS = {int(member): FilePurpose[member.name] for member in _PURPOSE}


def _target_purpose_model(copy, target) -> FilePurpose:
    """核对目标中间文件的用途与拷贝归属一致，并返回共用模型。"""
    purpose = _PURPOSE_MODELS.get(target["purpose"])
    if purpose is None:
        raise ConsistencyError(f"目标用途不属于登记枚举: {target['purpose']!r}")
    expected = (
        FilePurpose.DELIVERY_COPY if copy["delivery_id"] is not None
        else FilePurpose.RECORDING_INPUT
    )
    if purpose is not expected:
        raise ConsistencyError(
            f"目标用途 {purpose.value} 与拷贝归属要求的 {expected.value} 不一致")
    return purpose


#: device_files.checksum_support 与共用能力模型的对应；主机源总是可计算摘要。
_CHECKSUM_SUPPORT = {
    1: SourceChecksumSupport.UNDETERMINED,
    2: SourceChecksumSupport.SUPPORTED,
    3: SourceChecksumSupport.UNSUPPORTED,
}


def _verify_source_identity(connection, copy) -> tuple[int, SourceChecksumSupport]:
    """核对源文件身份仍然固定，并返回其摘要能力。

    当前长度必须等于建档时保存的长度；主机源文件总可以计算
    摘要，设备源按已保存的能力判定表达。
    """
    if copy["source_device_file_id"] is not None:
        source = row_facts(connection, "device_files", copy["source_device_file_id"])
        if source is None:
            raise ConsistencyError(
                f"拷贝的设备源文件缺失: {copy['source_device_file_id']}")
        size = source["size_bytes"]
        support = _CHECKSUM_SUPPORT.get(source["checksum_support"])
        if support is None:
            raise ConsistencyError(
                f"设备源摘要能力不属于登记枚举: {source['checksum_support']!r}")
    elif copy["source_intermediate_file_id"] is not None:
        source = row_facts(
            connection, "intermediate_files", copy["source_intermediate_file_id"])
        if source is None:
            raise ConsistencyError(
                f"拷贝的主机源文件缺失: {copy['source_intermediate_file_id']}")
        size = source["size_bytes"]
        support = SourceChecksumSupport.SUPPORTED
    else:
        raise ConsistencyError("拷贝缺少设备或主机源文件")
    if not is_json_integer(size) or size != copy["source_size"]:
        raise ConsistencyError(
            f"源文件当前长度 {size!r} 与建档固定长度 {copy['source_size']!r} 不一致")
    return copy["source_size"], support


def _read_attempt_facts(connection, copy_id: int) -> tuple[AttemptRecord, ...]:
    """读取拷贝唯一 READ_FILE 流程的全部尝试事实。"""
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ?",
        (f"read/{copy_id}",),
    )) as cursor:
        identities = cursor.fetchall()
    if len(identities) != 1:
        raise ConsistencyError("拷贝的 READ_FILE 流程缺失或重复")
    with closing(connection.execute(
        "SELECT id, attempt_no, status, copy_round FROM operation_attempts"
        " WHERE run_id = ? ORDER BY attempt_no", (identities[0][0],),
    )) as cursor:
        rows = cursor.fetchall()
    try:
        return tuple(
            AttemptRecord(
                attempt_id=row[0], attempt_no=row[1], status=row[2], copy_round=row[3],
            )
            for row in rows
        )
    except ValueError as error:
        raise ConsistencyError(f"读取尝试事实不可解释: {error}") from error


def _load_copy_state_facts(connection, copy_id: int) -> CopyStateFacts:
    """只读加载一份已建档拷贝的固定事实，供续传准备消费。"""
    ObjectId(copy_id)
    copy = row_facts(connection, "file_copies", copy_id)
    if copy is None:
        raise ConsistencyError(f"拷贝记录不存在: {copy_id}")
    target = row_facts(connection, "intermediate_files", copy["target_file_id"])
    if target is None:
        raise ConsistencyError(f"拷贝的目标中间文件缺失: {copy['target_file_id']}")
    purpose = _target_purpose_model(copy, target)
    try:
        validate_relative_file_path(
            purpose, copy["target_file_id"], target["relative_path"])
    except PathRuleError as error:
        raise ConsistencyError(f"目标保存路径不可定位: {error}") from error
    _, support = _verify_source_identity(connection, copy)
    return CopyStateFacts(
        copy_id=copy_id,
        source_size=copy["source_size"],
        committed_bytes=copy["committed_bytes"],
        round=copy["round"],
        reset_pending=copy["reset_state"] == int(_RESET_STATE.RESET_PENDING),
        target=CopyTargetRef(
            file_id=copy["target_file_id"], purpose=purpose,
            relative_path=target["relative_path"],
        ),
        attempts=_read_attempt_facts(connection, copy_id),
        source_sha256=copy["source_sha256"],
        target_sha256=copy["target_sha256"],
        verification_state=copy["verification_state"],
        recopies_used=copy["recopies_used"],
        max_recopies_used=copy["max_recopies_used"],
        source_support=support,
    )


class _CopyResetCommand:
    """目标重置完成事务命令。

    物理重置（截断或重建）已经可靠完成后，把重置意图翻转为
    READY；不重新分配交付或重拷轮次，不改动次数与进度。
    """

    _TABLES = ("file_copies", "deliveries", "recording_processing")

    def __init__(self, request: TargetResetRequest, key: OperationKey) -> None:
        if not isinstance(request, TargetResetRequest):
            raise TypeError("目标重置申请必须使用 TargetResetRequest")
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        copy = row_facts(connection, "file_copies", self._request.copy_id)
        if copy is None:
            raise ConsistencyError(f"拷贝记录不存在: {self._request.copy_id}")
        self._state["file_copies"][copy["id"]] = copy
        _copy_owner_of(connection, copy, self._state, self._owners)
        if copy["reset_state"] == int(_RESET_STATE.READY):
            return self._decision(TargetResetOutcome.ALREADY_READY)
        if copy["committed_bytes"] != 0:
            raise ConsistencyError("重置完成前可靠进度必须已经归零")
        row = _update(
            "file_copies", copy["id"],
            {"reset_state": int(_RESET_STATE.RESET_PENDING)},
            {"reset_state": int(_RESET_STATE.READY)},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _COPY_CHANGED_EVENT, _RESET_REASON, (row,),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=TargetResetDecision(TargetResetOutcome.COMPLETED),
        )

    def _decision(self, outcome: TargetResetOutcome) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True, result=TargetResetDecision(outcome),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次重置响应；不重复保存或按当前状态重判。"""
        if len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT \
                or saved[0]["reason"] != _RESET_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为目标重置重送")
        event = saved[0]
        if event["occurred_at"] != self._request.occurred_at:
            raise TransactionError("目标重置的事实时刻与原事务不同")
        rows = event["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "file_copies" \
                or rows[0]["id"] != self._request.copy_id:
            raise TransactionError("原目标重置的目标拷贝与输入不符")
        before = rows[0]["before"]["values"].get("reset_state")
        after = rows[0]["after"]["values"].get("reset_state")
        if before != int(_RESET_STATE.RESET_PENDING) \
                or after != int(_RESET_STATE.READY):
            raise TransactionError("原目标重置的状态转换与登记不符")
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=TargetResetDecision(TargetResetOutcome.COMPLETED),
        )


class _SegmentSaveCommand:
    """一段可靠进度的保存事务命令。

    段字节已实际写入并同步成功后，在写事务内核对当前拷贝轮次、
    旧进度与发起责任的执行资格，再保存进度事件；取消或动作不在
    执行时不新增进度提交。不新建读取尝试，不改动预算与等待。
    """

    _TABLES = ("file_copies", "deliveries", "recording_processing", "actions")

    def __init__(self, command: ReliableSegment, key: OperationKey) -> None:
        if not isinstance(command, ReliableSegment):
            raise TypeError("段保存申请必须使用 ReliableSegment")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        command = self._command
        copy = row_facts(connection, "file_copies", command.copy_id)
        if copy is None:
            raise ConsistencyError(f"拷贝记录不存在: {command.copy_id}")
        self._state["file_copies"][copy["id"]] = copy
        _copy_owner_of(connection, copy, self._state, self._owners)
        action = self._owner_action(connection, copy)
        if copy["reset_state"] != int(_RESET_STATE.READY):
            raise ConsistencyError("重置意图未完成前不能保存拷贝进度")
        if copy["round"] != command.copy_round:
            raise ConsistencyError(
                f"段所属轮次 {command.copy_round} 与当前拷贝轮次 {copy['round']} 不一致"
            )
        if copy["committed_bytes"] != command.committed_before:
            raise ConsistencyError(
                f"当前进度 {copy['committed_bytes']} 与段申请的旧进度"
                f" {command.committed_before} 不一致"
            )
        if command.segment_end > copy["source_size"]:
            raise ConsistencyError("段范围越过固定源长度")
        if action["cancel_requested"] == 1:
            return self._skip("cancel_requested", copy)
        if not _read_owner_eligible(action):
            return self._skip("owner_not_running", copy)
        row = _update(
            "file_copies", copy["id"],
            {"committed_bytes": copy["committed_bytes"]},
            {"committed_bytes": command.segment_end},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _COPY_CHANGED_EVENT, _SEGMENT_REASON, (row,),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=SegmentSaveOutcome(
                disposition=SegmentSaveDisposition.SAVED,
                committed_bytes=command.segment_end,
            ),
        )

    def _owner_action(self, connection, copy) -> dict:
        if copy["delivery_id"] is not None:
            action_id = self._state["deliveries"][copy["delivery_id"]]["action_id"]
        else:
            action_id = self._state["recording_processing"][copy["processing_id"]]["action_id"]
        facts = row_facts(connection, "actions", action_id)
        if facts is None:
            raise ConsistencyError(f"发起动作记录缺失: actions#{action_id}")
        self._state["actions"][action_id] = facts
        return facts

    def _skip(self, reason: str, copy) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=SegmentSaveOutcome(
                disposition=SegmentSaveDisposition.SKIPPED,
                committed_bytes=copy["committed_bytes"], reason=reason,
            ),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次段保存响应；不按当前状态重新判定资格。"""
        if len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT \
                or saved[0]["reason"] != _SEGMENT_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为段保存重送")
        event = saved[0]
        if event["occurred_at"] != self._command.occurred_at:
            raise TransactionError("段保存的事实时刻与原事务不同")
        rows = event["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "file_copies" \
                or rows[0]["id"] != self._command.copy_id:
            raise TransactionError("原段保存的目标拷贝与输入不符")
        before = rows[0]["before"]["values"].get("committed_bytes")
        after = rows[0]["after"]["values"].get("committed_bytes")
        if before != self._command.committed_before or after != self._command.segment_end:
            raise TransactionError("原段保存的进度范围与输入不符")
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=SegmentSaveOutcome(
                disposition=SegmentSaveDisposition.SAVED,
                committed_bytes=self._command.segment_end,
            ),
        )


class _CopyCompletionTablesMixin:
    """完整性收尾命令的共同事实加载与发起责任核对。"""

    _TABLES: tuple[str, ...]

    def _load_copy(self, connection, copy_id: int) -> dict:
        copy = row_facts(connection, "file_copies", copy_id)
        if copy is None:
            raise ConsistencyError(f"拷贝记录不存在: {copy_id}")
        self._state["file_copies"][copy["id"]] = copy
        _copy_owner_of(connection, copy, self._state, self._owners)
        return copy

    def _owner_action(self, connection, copy) -> dict:
        if copy["delivery_id"] is not None:
            action_id = self._state["deliveries"][copy["delivery_id"]]["action_id"]
        else:
            action_id = self._state["recording_processing"][copy["processing_id"]]["action_id"]
        facts = row_facts(connection, "actions", action_id)
        if facts is None:
            raise ConsistencyError(f"发起动作记录缺失: actions#{action_id}")
        self._state["actions"][action_id] = facts
        return facts

    def _require_full_progress(self, copy) -> None:
        if copy["reset_state"] != int(_RESET_STATE.READY):
            raise ConsistencyError("重置意图未完成前不能进入完整性收尾")
        if copy["committed_bytes"] != copy["source_size"]:
            raise ConsistencyError(
                f"完整性收尾要求可靠进度到达固定源长度:"
                f" {copy['committed_bytes']}/{copy['source_size']}"
            )

    def _decision(self, result, *, read_only: bool = False) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=read_only, result=result,
        )


class _IntegritySaveCommand(_CopyCompletionTablesMixin):
    """校验结果保存事务命令。

    全部字节可靠保存后保存源端与主机端摘要的比较结论；获取失败
    保存诊断，不降级为不支持。取消或动作不在执行时不新增校验
    事实。不改动轮次、进度与预算。
    """

    _TABLES = ("file_copies", "deliveries", "recording_processing", "actions")

    def __init__(self, command: VerificationSave, key: OperationKey) -> None:
        if not isinstance(command, VerificationSave):
            raise TypeError("校验保存申请必须使用 VerificationSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved, connection)
        command = self._command
        copy = self._load_copy(connection, command.copy_id)
        action = self._owner_action(connection, copy)
        self._require_full_progress(copy)
        if copy["verification_state"] not in (
            int(_VERIFICATION.NOT_PERFORMED), int(_VERIFICATION.RUNNING),
        ):
            raise ConsistencyError(
                f"校验事实已经保存: {copy['verification_state']!r}")
        if command.source_sha256 is not None \
                and copy["source_sha256"] is not None \
                and command.source_sha256 != copy["source_sha256"]:
            raise ConsistencyError("登记源摘要与已保存的源摘要不一致")
        if action["cancel_requested"] == 1:
            return self._decision(
                VerificationResult(
                    VerificationDisposition.SKIPPED, copy["verification_state"],
                    "cancel_requested"),
                read_only=True)
        if not _read_owner_eligible(action):
            return self._decision(
                VerificationResult(
                    VerificationDisposition.SKIPPED, copy["verification_state"],
                    "owner_not_running"),
                read_only=True)
        before = {"verification_state": copy["verification_state"]}
        after = {"verification_state": command.state}
        if copy["source_sha256"] is None and command.source_sha256 is not None:
            before["source_sha256"] = None
            after["source_sha256"] = command.source_sha256
        if command.target_sha256 != copy["target_sha256"]:
            before["target_sha256"] = copy["target_sha256"]
            after["target_sha256"] = command.target_sha256
        if command.error_json is not None and not json_equal(
                command.error_json, copy["verification_error_json"]):
            before["verification_error_json"] = copy["verification_error_json"]
            after["verification_error_json"] = command.error_json
        row = _update("file_copies", copy["id"], before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _COPY_CHANGED_EVENT, _VERIFY_REASON, (row,),
            command.occurred_at,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=VerificationResult(VerificationDisposition.SAVED, command.state),
        )

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        """原键恢复首次校验保存响应；不按当前状态重新判定资格。"""
        if len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT \
                or saved[0]["reason"] != _VERIFY_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为校验保存重送")
        event = saved[0]
        if event["occurred_at"] != self._command.occurred_at:
            raise TransactionError("校验保存的事实时刻与原事务不同")
        rows = event["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "file_copies" \
                or rows[0]["id"] != self._command.copy_id:
            raise TransactionError("原校验保存的目标拷贝与输入不符")
        after = rows[0]["after"]["values"]
        committed = row_facts(connection, "file_copies", self._command.copy_id)
        if after.get("verification_state") != self._command.state:
            raise TransactionError("原校验保存的状态与输入不符")
        for column, expected in (
            ("source_sha256", self._command.source_sha256),
            ("target_sha256", self._command.target_sha256),
            ("verification_error_json", self._command.error_json),
        ):
            actual = after.get(column, committed[column] if committed else None)
            if actual != expected:
                raise TransactionError("原校验保存的摘要事实与输入不符")
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=VerificationResult(VerificationDisposition.SAVED, self._command.state),
        )


class _RecopyCommand(_CopyCompletionTablesMixin):
    """整片重拷登记事务命令。

    不一致事实尚未保存时先在同一事务保存 VERIFY(MISMATCHED)，随
    后消耗一次重拷次数并把进度归零、登记重置意图；额度耗尽只保
    存判定上限，取消或动作不在执行时不开始新重拷。
    """

    _TABLES = ("file_copies", "deliveries", "recording_processing", "actions")

    def __init__(self, command: RecopyRegistration, key: OperationKey) -> None:
        if not isinstance(command, RecopyRegistration):
            raise TypeError("重拷登记申请必须使用 RecopyRegistration")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved, connection)
        command = self._command
        copy = self._load_copy(connection, command.copy_id)
        action = self._owner_action(connection, copy)
        self._require_full_progress(copy)
        state = copy["verification_state"]
        if state not in (int(_VERIFICATION.NOT_PERFORMED),
                         int(_VERIFICATION.MISMATCHED)):
            raise ConsistencyError(
                f"只有未校验或已不一致的拷贝能登记重拷: {state!r}")
        if copy["source_sha256"] is not None \
                and copy["source_sha256"] != command.source_sha256:
            raise ConsistencyError("已保存的源摘要与登记输入不一致")
        if state == int(_VERIFICATION.MISMATCHED) \
                and copy["target_sha256"] != command.target_sha256:
            raise ConsistencyError("已保存的主机摘要与登记输入不一致")
        if action["cancel_requested"] == 1:
            return self._decision(
                RecopyResult(RecopyDisposition.SKIPPED, reason="cancel_requested"),
                read_only=True)
        if not _read_owner_eligible(action):
            return self._decision(
                RecopyResult(RecopyDisposition.SKIPPED, reason="owner_not_running"),
                read_only=True)
        if copy["recopies_used"] >= command.max_recopies:
            return self._exhausted(scope, copy)
        specs = []
        if state == int(_VERIFICATION.NOT_PERFORMED):
            specs.append((
                _COPY_CHANGED_EVENT, _VERIFY_REASON, (self._verify_mismatch_row(copy),),
            ))
        round_now, used_now = copy["round"], copy["recopies_used"]
        before = {
            "round": round_now,
            "recopies_used": used_now,
            "committed_bytes": copy["source_size"],
            "reset_state": int(_RESET_STATE.READY),
            "verification_state": int(_VERIFICATION.MISMATCHED),
            "target_sha256": command.target_sha256,
        }
        after = {
            "round": round_now + 1,
            "recopies_used": used_now + 1,
            "committed_bytes": 0,
            "reset_state": int(_RESET_STATE.RESET_PENDING),
            "verification_state": int(_VERIFICATION.NOT_PERFORMED),
            "target_sha256": None,
        }
        if command.max_recopies != copy["max_recopies_used"]:
            before["max_recopies_used"] = copy["max_recopies_used"]
            after["max_recopies_used"] = command.max_recopies
        specs.append((
            _COPY_CHANGED_EVENT, _RECOPY_REASON,
            (_update("file_copies", copy["id"], before, after),),
        ))
        allocation = scope.allocate(len(specs))
        events = tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, reason, rows, command.occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=RecopyResult(
                RecopyDisposition.REGISTERED,
                round=round_now + 1, recopies_used=used_now + 1,
            ),
        )

    def _verify_mismatch_row(self, copy) -> tuple:
        """构造不一致事实行：源摘要与主机摘要都保存为比较依据。"""
        before = {"verification_state": copy["verification_state"]}
        after = {"verification_state": int(_VERIFICATION.MISMATCHED)}
        if copy["source_sha256"] is None:
            before["source_sha256"] = None
            after["source_sha256"] = self._command.source_sha256
        before["target_sha256"] = copy["target_sha256"]
        after["target_sha256"] = self._command.target_sha256
        return _update("file_copies", copy["id"], before, after)

    def _exhausted(self, scope, copy) -> CommandPlan:
        """额度耗尽：保存不一致诊断与实际判定上限，不登记新轮次。"""
        specs = []
        if copy["verification_state"] == int(_VERIFICATION.NOT_PERFORMED):
            specs.append((
                _COPY_CHANGED_EVENT, _VERIFY_REASON, (self._verify_mismatch_row(copy),),
            ))
        if self._command.max_recopies != copy["max_recopies_used"]:
            specs.append((
                _COPY_CHANGED_EVENT, _CONFIGURE_REASON,
                (_update(
                    "file_copies", copy["id"],
                    {"max_recopies_used": copy["max_recopies_used"]},
                    {"max_recopies_used": self._command.max_recopies},
                ),),
            ))
        result = RecopyResult(
            RecopyDisposition.EXHAUSTED,
            round=copy["round"], recopies_used=copy["recopies_used"],
        )
        if not specs:
            return self._decision(result, read_only=True)
        allocation = scope.allocate(len(specs))
        events = tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, reason, rows, self._command.occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=result,
        )

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        """原键恢复首次重拷登记响应；不重新判定额度或资格。"""
        types = [(event["type"], event["reason"]) for event in saved]
        registered = ([(_COPY_CHANGED_EVENT, _VERIFY_REASON),
                       (_COPY_CHANGED_EVENT, _RECOPY_REASON)],
                      [(_COPY_CHANGED_EVENT, _RECOPY_REASON)])
        exhausted = ([(_COPY_CHANGED_EVENT, _VERIFY_REASON),
                      (_COPY_CHANGED_EVENT, _CONFIGURE_REASON)],
                     [(_COPY_CHANGED_EVENT, _CONFIGURE_REASON)])
        if types not in registered and types not in exhausted:
            raise TransactionError("操作身份已用于其他阶段，不能作为重拷登记重送")
        for event in saved:
            if event["occurred_at"] != self._command.occurred_at:
                raise TransactionError("重拷登记的事实时刻与原事务不同")
            for row in event["body"]["rows"]:
                if row["table"] != "file_copies" \
                        or row["id"] != self._command.copy_id:
                    raise TransactionError("原重拷登记的目标拷贝与输入不符")
        committed = row_facts(connection, "file_copies", self._command.copy_id)
        final_rows = saved[-1]["body"]["rows"][0]
        if types in registered:
            before = final_rows["before"]["values"]
            after = final_rows["after"]["values"]
            if before.get("target_sha256") != self._command.target_sha256:
                raise TransactionError("原重拷登记的主机摘要与输入不符")
            limit = after.get("max_recopies_used", committed["max_recopies_used"]
                              if committed else None)
            if limit != self._command.max_recopies:
                raise TransactionError("原重拷登记的判定上限与输入不符")
            if (_COPY_CHANGED_EVENT, _VERIFY_REASON) in types:
                verify_values = saved[0]["body"]["rows"][0]["after"]["values"]
                source = verify_values.get(
                    "source_sha256", committed["source_sha256"] if committed else None)
                if source != self._command.source_sha256:
                    raise TransactionError("原不一致事实的源摘要与输入不符")
            elif before.get("verification_state") != int(_VERIFICATION.MISMATCHED):
                raise TransactionError("单独重拷事件必须以已保存的不一致事实为前提")
            return CommandPlan(
                events=(), owners={}, state_rows={}, read_only=True,
                result=RecopyResult(
                    RecopyDisposition.REGISTERED,
                    round=after.get("round"), recopies_used=after.get("recopies_used"),
                ),
            )
        if (_COPY_CHANGED_EVENT, _VERIFY_REASON) in types:
            verify_values = saved[0]["body"]["rows"][0]["after"]["values"]
            source = verify_values.get(
                "source_sha256", committed["source_sha256"] if committed else None)
            if source != self._command.source_sha256 \
                    or verify_values.get("target_sha256") != self._command.target_sha256:
                raise TransactionError("原不一致事实的摘要与输入不符")
        if (_COPY_CHANGED_EVENT, _CONFIGURE_REASON) in types:
            configured = saved[-1]["body"]["rows"][0]["after"]["values"]
            if configured.get("max_recopies_used") != self._command.max_recopies:
                raise TransactionError("原判定上限与输入不符")
        else:
            limit = committed["max_recopies_used"] if committed else None
            if limit != self._command.max_recopies:
                raise TransactionError("原判定的实际上限与输入不符")
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=RecopyResult(
                RecopyDisposition.EXHAUSTED,
                round=committed["round"] if committed else None,
                recopies_used=committed["recopies_used"] if committed else None,
            ),
        )


class _PreparedSaveCommand(_CopyCompletionTablesMixin):
    """副本准备完成事务命令。

    校验通过或明确不支持降级完成后，保存目标文件完整字节事实，
    普通交付与进入 PREPARED、解除取回源依赖共同提交；内部输入
    副本不解除取回源依赖，源保留由所属处理状态表达。
    """

    _TABLES = (
        "file_copies", "intermediate_files", "deliveries", "obtain_items",
        "obtain_source_selections", "action_dependencies", "recording_processing",
        "actions",
    )

    def __init__(self, request: PreparedRequest, key: OperationKey) -> None:
        if not isinstance(request, PreparedRequest):
            raise TypeError("准备完成申请必须使用 PreparedRequest")
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        request = self._request
        copy = self._load_copy(connection, request.copy_id)
        self._require_full_progress(copy)
        if copy["verification_state"] not in (
            int(_VERIFICATION.MATCHED),
            int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
        ):
            raise ConsistencyError(
                "副本完成字节校验前不能准备完成:"
                f" {copy['verification_state']!r}")
        if copy["target_sha256"] != request.target_sha256:
            raise ConsistencyError("准备完成申请的主机摘要与已保存校验不一致")
        if copy["source_size"] != request.size_bytes:
            raise ConsistencyError("准备完成申请的完整长度与固定源长度不一致")
        target = row_facts(connection, "intermediate_files", copy["target_file_id"])
        if target is None:
            raise ConsistencyError(f"拷贝的目标中间文件缺失: {copy['target_file_id']}")
        self._state["intermediate_files"][target["id"]] = target
        self._owners[("intermediate_files", target["id"])] = (
            "intermediate_file", target["id"],
        )
        is_delivery = copy["delivery_id"] is not None
        item = None
        delivery = None
        if is_delivery:
            self._owners[("deliveries", copy["delivery_id"])] = (
                "delivery", copy["delivery_id"],
            )
            with closing(connection.execute(
                "SELECT id FROM obtain_items WHERE delivery_id=?", (copy["delivery_id"],),
            )) as cursor:
                found = cursor.fetchone()
            if found is None:
                raise ConsistencyError("交付缺少关联的取回项")
            item = row_facts(connection, "obtain_items", found[0])
            if item is None:
                raise ConsistencyError(f"取回项记录缺失: {found[0]}")
            self._state["obtain_items"][item["id"]] = item
            selection = row_facts(
                connection, "obtain_source_selections", item["selection_id"])
            if selection is None:
                raise ConsistencyError(f"取回项的选择记录缺失: {item['selection_id']}")
            self._state["obtain_source_selections"][selection["id"]] = selection
            dependency = row_facts(
                connection, "action_dependencies", selection["dependency_id"])
            if dependency is None:
                raise ConsistencyError(
                    f"选择的来源依赖缺失: {selection['dependency_id']}")
            self._state["action_dependencies"][dependency["id"]] = dependency
            self._owners[("obtain_items", item["id"])] = (
                "action", self._state["deliveries"][copy["delivery_id"]]["action_id"],
            )
            delivery = self._state["deliveries"][copy["delivery_id"]]
            already = (
                delivery["status"] == int(_DELIVERY_STATUS.PREPARED)
                and item["source_dependency"] == 0
                and target["size_bytes"] == request.size_bytes
                and target["sha256"] == request.target_sha256
            )
            if already:
                return self._already(copy, target, released=True)
            if delivery["status"] not in (
                int(_DELIVERY_STATUS.PENDING), int(_DELIVERY_STATUS.PREPARING),
            ):
                raise ConsistencyError(
                    f"交付已离开准备阶段: {delivery['status']!r}")
            if item["source_dependency"] != 1:
                raise ConsistencyError("取回项源依赖已解除，不能重复准备完成")
        elif (target["size_bytes"] == request.size_bytes
                and target["sha256"] == request.target_sha256):
            return self._already(copy, target, released=False)
        action = self._owner_action(connection, copy)
        if action["cancel_requested"] == 1:
            return self._decision(
                PreparedSaveOutcome(PreparedDisposition.SKIPPED, reason="cancel_requested"),
                read_only=True)
        if not _read_owner_eligible(action):
            return self._decision(
                PreparedSaveOutcome(
                    PreparedDisposition.SKIPPED, reason="owner_not_running"),
                read_only=True)
        specs = [(
            _INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON,
            (_update(
                "intermediate_files", target["id"],
                {"size_bytes": target["size_bytes"], "sha256": target["sha256"]},
                {"size_bytes": request.size_bytes, "sha256": request.target_sha256},
            ),),
        )]
        prepared = PreparedCopy(
            copy_id=copy["id"], target_file_id=target["id"],
            size_bytes=request.size_bytes, sha256=request.target_sha256,
            source_dependency_released=is_delivery,
        )
        if is_delivery:
            specs.append((
                _DELIVERY_CHANGED_EVENT, _PREPARE_REASON,
                (_update(
                    "deliveries", delivery["id"],
                    {"status": delivery["status"]},
                    {"status": int(_DELIVERY_STATUS.PREPARED)},
                ),),
            ))
            specs.append((
                _READ_PERMISSION_EVENT, _RELEASE_REASON,
                (_update(
                    "obtain_items", item["id"],
                    {"source_dependency": 1},
                    {"source_dependency": 0},
                ),),
            ))
        allocation = scope.allocate(len(specs))
        events = tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, reason, rows, request.occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=PreparedSaveOutcome(PreparedDisposition.SAVED, prepared=prepared),
        )

    def _already(self, copy, target, *, released: bool) -> CommandPlan:
        """先前事务已完整提交本次事实；恢复重放不重复保存。"""
        return self._decision(PreparedSaveOutcome(
            PreparedDisposition.ALREADY,
            prepared=PreparedCopy(
                copy_id=copy["id"], target_file_id=target["id"],
                size_bytes=target["size_bytes"], sha256=target["sha256"],
                source_dependency_released=released,
            ),
        ), read_only=True)

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次准备完成响应；已提交事实不重复保存。"""
        types = [(event["type"], event["reason"]) for event in saved]
        expected = [
            (_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON),
            (_DELIVERY_CHANGED_EVENT, _PREPARE_REASON),
            (_READ_PERMISSION_EVENT, _RELEASE_REASON),
        ]
        if types != expected[:1] and types != expected:
            raise TransactionError("操作身份已用于其他阶段，不能作为准备完成重送")
        first = saved[0]
        if first["occurred_at"] != self._request.occurred_at:
            raise TransactionError("准备完成的事实时刻与原事务不同")
        target_file_id = None
        for event in saved:
            for row in event["body"]["rows"]:
                after = row["after"]["values"]
                if row["table"] == "intermediate_files":
                    target_file_id = row["id"]
                    if (after.get("size_bytes") != self._request.size_bytes
                            or after.get("sha256") != self._request.target_sha256):
                        raise TransactionError("原准备完成的字节事实与输入不符")
        if target_file_id is None:
            raise TransactionError("原准备完成缺少目标文件事实")
        released = (_READ_PERMISSION_EVENT, _RELEASE_REASON) in types
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=PreparedSaveOutcome(
                PreparedDisposition.ALREADY,
                prepared=PreparedCopy(
                    copy_id=self._request.copy_id, target_file_id=target_file_id,
                    size_bytes=self._request.size_bytes,
                    sha256=self._request.target_sha256,
                    source_dependency_released=released,
                ),
            ),
        )


class _DeliveryPublicationMixin:
    """交付发布事务的共同表事实：交付行、唯一拷贝、目标文件与发起动作。"""

    _TABLES = ("deliveries", "file_copies", "actions", "intermediate_files")

    def _reset_state(self) -> None:
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def _load_delivery(self, connection, delivery_id: int) -> tuple[dict, dict]:
        delivery = row_facts(connection, "deliveries", delivery_id)
        if delivery is None:
            raise ConsistencyError(f"交付记录不存在: {delivery_id}")
        self._state["deliveries"][delivery["id"]] = delivery
        self._owners[("deliveries", delivery["id"])] = ("delivery", delivery["id"])
        with closing(connection.execute(
            "SELECT id FROM file_copies WHERE delivery_id=?", (delivery_id,),
        )) as cursor:
            copies = cursor.fetchall()
        if len(copies) != 1:
            raise ConsistencyError("发布事务必须对应唯一交付拷贝")
        copy = row_facts(connection, "file_copies", copies[0][0])
        if copy is None:
            raise ConsistencyError(f"交付拷贝记录缺失: {copies[0][0]}")
        self._state["file_copies"][copy["id"]] = copy
        return delivery, copy

    def _owner_action(self, connection, delivery) -> dict:
        action = row_facts(connection, "actions", delivery["action_id"])
        if action is None:
            raise ConsistencyError(f"交付的发起动作缺失: {delivery['action_id']}")
        self._state["actions"][action["id"]] = action
        return action

    def _load_target(self, connection, copy) -> dict:
        target = row_facts(
            connection, "intermediate_files", copy["target_file_id"])
        if target is None:
            raise ConsistencyError(
                f"交付拷贝的目标中间文件缺失: {copy['target_file_id']}")
        self._state["intermediate_files"][target["id"]] = target
        self._owners[("intermediate_files", target["id"])] = (
            "intermediate_file", target["id"])
        return target

    def _decision(self, result, *, read_only: bool = False) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            result=result, read_only=read_only,
        )

    def _intent_row(self, delivery, event_id: int):
        return _update(
            "deliveries", delivery["id"],
            {"status": int(_DELIVERY_STATUS.PREPARED),
             "publication_intent_event_id": None},
            {"status": int(_DELIVERY_STATUS.PUBLISHING),
             "publication_intent_event_id": event_id},
        )

    def _publish_row(self, delivery, event_id: int):
        return _update(
            "deliveries", delivery["id"],
            {"status": int(_DELIVERY_STATUS.PUBLISHING),
             "published_event_id": None},
            {"status": int(_DELIVERY_STATUS.PUBLISHED),
             "published_event_id": event_id},
        )

    def _envelopes(self, scope, specs, occurred_at: int):
        allocation = scope.allocate(len(specs))
        return tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, reason, rows, occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )


class _PublicationIntentCommand(_DeliveryPublicationMixin):
    """发布意图保存事务命令。

    副本准备完成且发起责任仍在执行时，把交付推进到 PUBLISHING，
    并把意图登记为本事务的意图事件；意图不等于发布成功。
    """

    def __init__(self, request: PublicationIntentRequest, key: OperationKey) -> None:
        if not isinstance(request, PublicationIntentRequest):
            raise TypeError("发布意图申请必须使用 PublicationIntentRequest")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        delivery, _copy = self._load_delivery(connection, self._request.delivery_id)
        if delivery["status"] == int(_DELIVERY_STATUS.PUBLISHING) \
                and delivery["publication_intent_event_id"] is not None:
            return self._decision(
                IntentSaveOutcome(IntentDisposition.ALREADY), read_only=True)
        if delivery["status"] != int(_DELIVERY_STATUS.PREPARED):
            raise ConsistencyError(
                "交付未处于准备完成状态，不能保存发布意图:"
                f" {delivery['status']!r}")
        action = self._owner_action(connection, delivery)
        if action["cancel_requested"] == 1:
            return self._decision(
                IntentSaveOutcome(
                    IntentDisposition.SKIPPED, reason="cancel_requested"),
                read_only=True)
        if not _read_owner_eligible(action):
            return self._decision(
                IntentSaveOutcome(
                    IntentDisposition.SKIPPED, reason="owner_not_running"),
                read_only=True)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DELIVERY_CHANGED_EVENT, _INTENT_REASON,
            (self._intent_row(delivery, allocation.first_event_id),),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=IntentSaveOutcome(IntentDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次意图保存响应；已提交事实不重复保存。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DELIVERY_CHANGED_EVENT, _INTENT_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为发布意图重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("发布意图的事实时刻与原事务不同")
        return self._decision(
            IntentSaveOutcome(IntentDisposition.ALREADY), read_only=True)


class _PublicationSaveCommand(_DeliveryPublicationMixin):
    """本地交付完成事实保存事务命令。

    只有可靠交接依据（原子移动及目录同步完成，或恢复观察到交接
    位置存在同一完整副本）才允许保存；确认已发生的外部事实不受
    发起责任取消影响。意图缺失时（恢复观察到副本已在交接位置）
    在同一事务先补存意图再保存完成。
    """

    def __init__(self, request: PublicationSaveRequest, key: OperationKey) -> None:
        if not isinstance(request, PublicationSaveRequest):
            raise TypeError("完成事实申请必须使用 PublicationSaveRequest")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        delivery, copy = self._load_delivery(connection, self._request.delivery_id)
        status = delivery["status"]
        if status == int(_DELIVERY_STATUS.PUBLISHED):
            if delivery["published_event_id"] is None:
                raise ConsistencyError("已发布的交付缺少完成事件引用")
            return self._decision(
                PublicationSaveOutcome(PublicationDisposition.ALREADY),
                read_only=True)
        if status == int(_DELIVERY_STATUS.PUBLISHING):
            specs = [(_DELIVERY_CHANGED_EVENT, _PUBLISH_REASON)]
        elif status == int(_DELIVERY_STATUS.PREPARED):
            specs = [
                (_DELIVERY_CHANGED_EVENT, _INTENT_REASON),
                (_DELIVERY_CHANGED_EVENT, _PUBLISH_REASON),
            ]
        else:
            raise ConsistencyError(
                f"交付状态不能保存本地完成事实: {status!r}")
        target = self._load_target(connection, copy)
        if target["retention_state"] == int(_RETENTION.REQUIRED):
            # 交接完成后副本所有权归交接位置；留存工作副本经释放流程清理。
            specs.append((_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON))
        elif target["retention_state"] != int(_RETENTION.HANDED_OFF):
            raise ConsistencyError(
                "交付完成时目标文件的保留状态不可交接:"
                f" {target['retention_state']!r}")
        allocation = scope.allocate(len(specs))
        first = allocation.first_event_id
        publish_id = first + specs.index(
            (_DELIVERY_CHANGED_EVENT, _PUBLISH_REASON))
        rows_by_reason = {
            _INTENT_REASON: (self._intent_row(delivery, first),),
            _PUBLISH_REASON: (self._publish_row(delivery, publish_id),),
            _LIFECYCLE_REASON: (self._handoff_row(target),),
        }
        events = tuple(
            _envelope(
                first + index, allocation.txn_id,
                event_type, reason, rows_by_reason[reason],
                self._request.occurred_at,
            )
            for index, (event_type, reason) in enumerate(specs)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=PublicationSaveOutcome(PublicationDisposition.SAVED),
        )

    def _handoff_row(self, target):
        return _update(
            "intermediate_files", target["id"],
            {"retention_state": int(_RETENTION.REQUIRED)},
            {"retention_state": int(_RETENTION.HANDED_OFF)},
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次完成事实响应；覆盖补意图与直接保存两组合。"""
        types = [(event["type"], event["reason"]) for event in saved]
        expected = [
            (_DELIVERY_CHANGED_EVENT, _INTENT_REASON),
            (_DELIVERY_CHANGED_EVENT, _PUBLISH_REASON),
            (_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON),
        ]
        if types != expected and types != expected[1:]:
            raise TransactionError("操作身份已用于其他阶段，不能作为完成事实重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("完成事实的时刻与原事务不同")
        return self._decision(
            PublicationSaveOutcome(PublicationDisposition.ALREADY), read_only=True)


class _UnconfirmedFailureCommand(_DeliveryPublicationMixin):
    """交接未知终局失败保存事务命令。

    三个交接位置均无副本且无完成事实时，把交付结束为 FAILED，
    保存公共错误 delivery_handoff_unconfirmed；不自动重投，
    不因后续重启重新激活。
    """

    def __init__(self, request: UnconfirmedFailureSave, key: OperationKey) -> None:
        if not isinstance(request, UnconfirmedFailureSave):
            raise TypeError("终局失败申请必须使用 UnconfirmedFailureSave")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        delivery, _copy = self._load_delivery(connection, self._request.delivery_id)
        status = delivery["status"]
        if status == int(_DELIVERY_STATUS.PUBLISHED):
            raise ConsistencyError("已保存的完成事实不能改判终局失败")
        if status not in (
            int(_DELIVERY_STATUS.PREPARED), int(_DELIVERY_STATUS.PUBLISHING),
        ):
            raise ConsistencyError(
                f"交付状态不属于交接未知失败的保存范围: {status!r}")
        row = _update(
            "deliveries", delivery["id"],
            {"status": status, "error_json": None},
            {"status": int(_DELIVERY_STATUS.FAILED),
             "error_json": self._request.failure.as_json()},
        )
        events = self._envelopes(
            scope, [(_DELIVERY_CHANGED_EVENT, _DELIVERY_FAIL_REASON, (row,))],
            self._request.occurred_at,
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=FailureSaveOutcome(FailureSaveDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次终局失败响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DELIVERY_CHANGED_EVENT, _DELIVERY_FAIL_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为终局失败重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("终局失败的事实时刻与原事务不同")
        return self._decision(
            FailureSaveOutcome(FailureSaveDisposition.ALREADY), read_only=True)


class _WorkFileMixin:
    """中间文件清理事务的共同表事实：文件行、归属与运行状态。"""

    _TABLES = ("intermediate_files", "deliveries", "actions", "runtime_state")

    #: REQUIRED→RELEASABLE 允许的归属交付终态：留存副本、失败与取消。
    _REQUIRED_RELEASE_DELIVERY = frozenset({
        int(_DELIVERY_STATUS.PUBLISHED),
        int(_DELIVERY_STATUS.FAILED),
        int(_DELIVERY_STATUS.CANCELED),
    })
    #: HANDED_OFF→RELEASABLE 允许的归属交付终态：留存副本与已撤回。
    _HANDOFF_RELEASE_DELIVERY = frozenset({
        int(_DELIVERY_STATUS.PUBLISHED),
        int(_DELIVERY_STATUS.WITHDRAWN),
    })
    _TERMINAL_ACTIONS = frozenset({3, 4, 5, 6})
    _TERMINAL_DELIVERIES = frozenset({
        int(_DELIVERY_STATUS.PUBLISHED),
        int(_DELIVERY_STATUS.FAILED),
        int(_DELIVERY_STATUS.CANCELED),
        int(_DELIVERY_STATUS.WITHDRAWN),
    })

    def _reset_state(self) -> None:
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
        }

    def _load_file(self, connection, file_id: int) -> dict:
        file = row_facts(connection, "intermediate_files", file_id)
        if file is None:
            raise ConsistencyError(f"中间文件记录不存在: {file_id}")
        self._state["intermediate_files"][file["id"]] = file
        self._owners[("intermediate_files", file["id"])] = (
            "intermediate_file", file["id"])
        return file

    def _load_owner(self, connection, file) -> tuple[dict, str]:
        """归属交付或动作行；归属缺失属于状态库矛盾。"""
        if file["owner_delivery_id"] is not None:
            owner = row_facts(
                connection, "deliveries", file["owner_delivery_id"])
            table = "deliveries"
        else:
            owner = row_facts(
                connection, "actions", file["owner_action_id"])
            table = "actions"
        if owner is None:
            raise ConsistencyError(
                f"中间文件的归属记录缺失: {file['id']}")
        self._state[table][owner["id"]] = owner
        return owner, table

    def _require_stopped_operations(self, connection, file) -> None:
        """释放前核对目标拷贝没有未结束的读取尝试。"""
        with closing(connection.execute(
            "SELECT COUNT(*) FROM operation_attempts AS a"
            " JOIN operation_runs AS r ON a.run_id = r.id"
            " JOIN file_copies AS c ON r.copy_id = c.id"
            " WHERE c.target_file_id = ? AND a.status = 1",
            (file["id"],),
        )) as cursor:
            running = cursor.fetchone()[0]
        if running:
            raise ConsistencyError(
                f"中间文件 {file['id']} 仍有 {running} 个未结束操作尝试")

    def _cursor_row(self, connection, file_id: int):
        """历史清理游标推进行；引用本次可靠检查的中间文件。"""
        with closing(connection.execute(
            "SELECT cleanup_cursor_file_id FROM runtime_state WHERE id = 1",
        )) as cursor:
            saved = cursor.fetchone()
        if saved is None:
            raise ConsistencyError("全局运行状态记录缺失")
        current = saved[0]
        if current == file_id:
            return None
        self._owners[("runtime_state", 1)] = ("runtime_state", 1)
        self._state["runtime_state"][1] = {"cleanup_cursor_file_id": current}
        return _update(
            "runtime_state", 1,
            {"cleanup_cursor_file_id": current},
            {"cleanup_cursor_file_id": file_id},
        )

    def _decision(self, result, *, read_only: bool = False) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            result=result, read_only=read_only,
        )


class _RetentionReleaseCommand(_WorkFileMixin):
    """释放保留状态事务命令。

    归属交付或动作已可靠终态、目标拷贝没有未结束尝试时，把
    REQUIRED（或已交接后的 HANDED_OFF）推进到 RELEASABLE 并建立
    清理待办；这是清理意图与物理删除的共同前提。
    """

    def __init__(self, request: RetentionRelease, key: OperationKey) -> None:
        if not isinstance(request, RetentionRelease):
            raise TypeError("释放申请必须使用 RetentionRelease")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        file = self._load_file(connection, self._request.file_id)
        retention = file["retention_state"]
        if retention == int(_RETENTION.RELEASABLE):
            return self._decision(
                RetentionReleaseOutcome(RetentionDisposition.ALREADY),
                read_only=True)
        owner, table = self._load_owner(connection, file)
        if retention == int(_RETENTION.REQUIRED):
            allowed = (self._REQUIRED_RELEASE_DELIVERY if table == "deliveries"
                       else self._TERMINAL_ACTIONS)
        elif retention == int(_RETENTION.HANDED_OFF):
            if table != "deliveries":
                raise ConsistencyError("动作归属文件没有已交接保留状态")
            allowed = self._HANDOFF_RELEASE_DELIVERY
        else:
            raise ConsistencyError(
                f"已提升为正式产物的文件不能释放清理: {retention!r}")
        if owner["status"] not in allowed:
            raise ConsistencyError(
                f"归属尚未进入允许释放的终态: {owner['status']!r}")
        self._require_stopped_operations(connection, file)
        row = _update(
            "intermediate_files", file["id"],
            {"retention_state": retention,
             "cleanup_state": int(_FILE_CLEANUP.NOT_NEEDED)},
            {"retention_state": int(_RETENTION.RELEASABLE),
             "cleanup_state": int(_FILE_CLEANUP.PENDING)},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON, (row,),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=RetentionReleaseOutcome(RetentionDisposition.RELEASED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次释放响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为释放重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("释放的事实时刻与原事务不同")
        return self._decision(
            RetentionReleaseOutcome(RetentionDisposition.ALREADY),
            read_only=True)


class _CleanupIntentCommand(_WorkFileMixin):
    """自动清理意图事务命令：意图先于物理删除可靠保存。"""

    def __init__(self, request: CleanupIntent, key: OperationKey) -> None:
        if not isinstance(request, CleanupIntent):
            raise TypeError("意图申请必须使用 CleanupIntent")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        file = self._load_file(connection, self._request.file_id)
        if file["retention_state"] != int(_RETENTION.RELEASABLE):
            raise ConsistencyError(
                "清理意图要求文件已释放保留状态:"
                f" {file['retention_state']!r}")
        cleanup = file["cleanup_state"]
        if cleanup == int(_FILE_CLEANUP.RUNNING):
            raise ConsistencyError("清理意图已保存，等待实际结果")
        if cleanup == int(_FILE_CLEANUP.COMPLETED):
            raise ConsistencyError("清理已完成，不再保存意图")
        if cleanup not in (
            int(_FILE_CLEANUP.PENDING),
            int(_FILE_CLEANUP.FAILED),
            int(_FILE_CLEANUP.UNKNOWN),
        ):
            raise ConsistencyError(f"清理状态不能保存意图: {cleanup!r}")
        row = _update(
            "intermediate_files", file["id"],
            {"cleanup_state": cleanup},
            {"cleanup_state": int(_FILE_CLEANUP.RUNNING)},
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _INTERMEDIATE_FILE_EVENT, _CLEANUP_INTENT_REASON, (row,),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CleanupIntentOutcome(CleanupIntentDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次意图响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, _CLEANUP_INTENT_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为清理意图重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("清理意图的事实时刻与原事务不同")
        return self._decision(
            CleanupIntentOutcome(CleanupIntentDisposition.ALREADY),
            read_only=True)


class _CleanupResultCommand(_WorkFileMixin):
    """自动清理结果事务命令；可与游标推进同事务提交。

    完成清除当前错误；失败保存按公共登记构造的错误对象。恢复
    观察到文件已不存在时按完成补记，不重复物理删除。
    """

    def __init__(self, request: CleanupResultSave, key: OperationKey) -> None:
        if not isinstance(request, CleanupResultSave):
            raise TypeError("结果申请必须使用 CleanupResultSave")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        file = self._load_file(connection, self._request.file_id)
        if file["retention_state"] != int(_RETENTION.RELEASABLE):
            raise ConsistencyError(
                "清理结果要求文件处于释放保留状态:"
                f" {file['retention_state']!r}")
        before = file["cleanup_state"]
        if before not in (
            int(_FILE_CLEANUP.PENDING),
            int(_FILE_CLEANUP.RUNNING),
            int(_FILE_CLEANUP.FAILED),
            int(_FILE_CLEANUP.UNKNOWN),
        ):
            raise ConsistencyError(f"清理状态不能保存结果: {before!r}")
        after = (int(_FILE_CLEANUP.COMPLETED)
                 if self._request.outcome is WorkFileOutcome.COMPLETED
                 else int(_FILE_CLEANUP.FAILED))
        error_json = (None if self._request.error is None
                      else self._request.error.as_json())
        specs = [(
            _INTERMEDIATE_FILE_EVENT, _CLEANUP_RESULT_REASON,
            (_update(
                "intermediate_files", file["id"],
                {"cleanup_state": before,
                 "last_error_json": file["last_error_json"]},
                {"cleanup_state": after, "last_error_json": error_json},
            ),),
        )]
        if self._request.advance_cursor:
            cursor_row = self._cursor_row(connection, file["id"])
            if cursor_row is not None:
                specs.append((
                    _CLEANUP_CURSOR_EVENT, _CURSOR_CHECKED_REASON,
                    (cursor_row,),
                ))
        allocation = scope.allocate(len(specs))
        events = tuple(
            _envelope(
                allocation.first_event_id + index, allocation.txn_id,
                event_type, reason, rows, self._request.occurred_at,
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=CleanupResultSaveOutcome(CleanupResultDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次结果响应；游标推进可选共存。"""
        types = [(event["type"], event["reason"]) for event in saved]
        expected = [
            (_INTERMEDIATE_FILE_EVENT, _CLEANUP_RESULT_REASON),
            (_CLEANUP_CURSOR_EVENT, _CURSOR_CHECKED_REASON),
        ]
        if types != expected and types != expected[:1]:
            raise TransactionError("操作身份已用于其他阶段，不能作为清理结果重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("清理结果的事实时刻与原事务不同")
        return self._decision(
            CleanupResultSaveOutcome(CleanupResultDisposition.ALREADY),
            read_only=True)


class _CleanupCheckedCommand(_WorkFileMixin):
    """历史清理游标推进事务命令。

    检查过但本次不能安全删除的记录经此保存继续位置；只更新
    runtime_state 的清理字段组，不产生业务变化。
    """

    def __init__(self, request: CleanupChecked, key: OperationKey) -> None:
        if not isinstance(request, CleanupChecked):
            raise TypeError("游标申请必须使用 CleanupChecked")
        self._request = request
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        file = self._load_file(connection, self._request.file_id)
        row = self._cursor_row(connection, file["id"])
        if row is None:
            return self._decision(
                CleanupCheckedOutcome(CleanupCheckedDisposition.ALREADY),
                read_only=True)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _CLEANUP_CURSOR_EVENT, _CURSOR_CHECKED_REASON, (row,),
            self._request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=CleanupCheckedOutcome(CleanupCheckedDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次游标推进响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_CLEANUP_CURSOR_EVENT, _CURSOR_CHECKED_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为游标推进重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("游标推进的事实时刻与原事务不同")
        return self._decision(
            CleanupCheckedOutcome(CleanupCheckedDisposition.ALREADY),
            read_only=True)


def _load_work_file_facts(connection, file_id: int) -> WorkFileFacts:
    """只读加载一份中间文件的清理判定事实。

    归属终态与操作停止都按当前可靠记录核实；记录缺失或不可解释
    属于状态库矛盾，不解释为归属活跃或已停止。
    """
    ObjectId(file_id)
    file = row_facts(connection, "intermediate_files", file_id)
    if file is None:
        raise ConsistencyError(f"中间文件记录不存在: {file_id}")
    if file["owner_delivery_id"] is not None:
        delivery = row_facts(
            connection, "deliveries", file["owner_delivery_id"])
        if delivery is None:
            raise ConsistencyError(
                f"中间文件的归属交付缺失: {file['owner_delivery_id']}")
        owner_finished = delivery["status"] in _WorkFileMixin._TERMINAL_DELIVERIES
    else:
        action = row_facts(
            connection, "actions", file["owner_action_id"])
        if action is None:
            raise ConsistencyError(
                f"中间文件的归属动作缺失: {file['owner_action_id']}")
        owner_finished = action["status"] in _WorkFileMixin._TERMINAL_ACTIONS
    with closing(connection.execute(
        "SELECT COUNT(*) FROM operation_attempts AS a"
        " JOIN operation_runs AS r ON a.run_id = r.id"
        " JOIN file_copies AS c ON r.copy_id = c.id"
        " WHERE c.target_file_id = ? AND a.status = 1",
        (file_id,),
    )) as cursor:
        operations_stopped = cursor.fetchone()[0] == 0
    try:
        return WorkFileFacts(
            file_id=file_id,
            purpose=file["purpose"],
            owner_action_id=file["owner_action_id"],
            owner_delivery_id=file["owner_delivery_id"],
            relative_path=file["relative_path"],
            retention_state=file["retention_state"],
            cleanup_state=file["cleanup_state"],
            owner_finished=owner_finished,
            operations_stopped=operations_stopped,
        )
    except ValueError as error:
        raise ConsistencyError(f"中间文件事实不可解释: {error}") from error


def _load_delivery_state_facts(
    connection, delivery_id: int,
) -> DeliveryStateFacts:
    """只读加载一份交付的交接事实、定位与准备记录。"""
    ObjectId(delivery_id)
    delivery = row_facts(connection, "deliveries", delivery_id)
    if delivery is None:
        raise ConsistencyError(f"交付记录不存在: {delivery_id}")
    with closing(connection.execute(
        "SELECT id FROM file_copies WHERE delivery_id=?", (delivery_id,),
    )) as cursor:
        copies = cursor.fetchall()
    if len(copies) != 1:
        raise ConsistencyError("交付必须对应唯一拷贝")
    copy = row_facts(connection, "file_copies", copies[0][0])
    if copy is None:
        raise ConsistencyError(f"交付拷贝记录缺失: {copies[0][0]}")
    target = row_facts(connection, "intermediate_files", copy["target_file_id"])
    if target is None:
        raise ConsistencyError(f"拷贝的目标中间文件缺失: {copy['target_file_id']}")
    purpose = _target_purpose_model(copy, target)
    try:
        validate_relative_file_path(
            purpose, copy["target_file_id"], target["relative_path"])
    except PathRuleError as error:
        raise ConsistencyError(f"目标保存路径不可定位: {error}") from error
    if not isinstance(delivery["file_name"], str) or not delivery["file_name"]:
        raise ConsistencyError("交付缺少交接文件名")
    try:
        facts = DeliveryFacts(
            delivery_id=delivery_id,
            status=delivery["status"],
            publication_intent_event_id=delivery["publication_intent_event_id"],
            published_event_id=delivery["published_event_id"],
            prepared_size=target["size_bytes"],
            prepared_sha256=target["sha256"],
            withdrawal_requested=(
                delivery["withdrawal_state"]
                != int(_WITHDRAWAL.NOT_REQUESTED)),
        )
    except ValueError as error:
        raise ConsistencyError(f"交付交接事实不可解释: {error}") from error
    return DeliveryStateFacts(
        facts=facts,
        file_name=delivery["file_name"],
        target=CopyTargetRef(
            file_id=target["id"], purpose=purpose,
            relative_path=target["relative_path"],
        ),
        action_id=delivery["action_id"],
    )


@dataclass(frozen=True)
class FinishObtain:
    """一次取回完成登记的输入。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)


class ObtainFinishDisposition(Enum):
    """取回完成事务的保存结果：新保存或恢复首次结果。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class ObtainFinishResult:
    """取回完成登记的已保存事实。"""

    action_status: int
    plan_status: int
    prepared: int
    failed: int
    disposition: ObtainFinishDisposition = ObtainFinishDisposition.SAVED


class _FinishObtainCommand:
    """取回动作终态汇总事务命令。

    汇总判定确定且成功交付已全部发布后保存动作终态；任一逐项最
    终失败按公共动作错误登记整次失败（成功交付保留，具体失败项
    由条目事实表达），全部成功才保存成功终态。父计划状态与动作
    终态同事务推进；原键重送与终态新键恢复首次结果。
    """

    _TABLES = (
        "actions",
        "action_dependencies",
        "obtain_source_selections",
        "obtain_items",
        "deliveries",
        "plans",
    )

    def __init__(self, command: FinishObtain, key: OperationKey) -> None:
        if not isinstance(command, FinishObtain):
            raise TypeError("取回完成申请必须使用 FinishObtain")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            table: {} for table in self._TABLES
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
        if action["type"] != _OBTAIN_TYPE:
            raise TransactionError(f"动作不是取回动作: {command.action_id}")
        self._state["actions"][command.action_id] = action
        if action["status"] == int(_ACTION_STATUS.RUNNING) \
                and action["cancel_requested"]:
            raise TransactionError(
                f"取消请求已生效的取回动作不能保存终态: {command.action_id}")
        if action["status"] in _ACTION_TERMINAL:
            return self._recover(connection, action)
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError(
                f"取回动作不在执行中: {command.action_id} status={action['status']}")

        decision = decide_obtain_finish(self._load_facts(connection))
        if decision.phase is not ObtainPhase.READY_TO_PUBLISH:
            raise TransactionError(
                f"取回汇总尚未确定，不能保存终态: {decision.phase.value}")
        unpublished = self._unpublished_deliveries(connection)
        if unpublished:
            raise TransactionError(f"成功交付尚未全部发布: {sorted(unpublished)}")
        if decision.failed:
            action_status = int(_ACTION_STATUS.FAILED)
            error_code = registered_error(_OBTAIN_ITEMS_FAILED)["action_error_id"]
        elif decision.prepared:
            action_status = int(_ACTION_STATUS.SUCCEEDED)
            error_code = None
        else:
            raise TransactionError("取回没有可汇总条目，不能保存终态")

        siblings = self._load_siblings(connection, action)
        before = {"status": action["status"]}
        after = {"status": action_status}
        reason = 2 if decision.failed else 1
        if decision.failed:
            before.update(error_code=action["error_code"],
                          error_details_json=action["error_details_json"])
            after.update(error_code=error_code, error_details_json={})
        templates = [(
            _ACTION_FINISHED_EVENT, reason,
            (_update("actions", command.action_id, before, after),),
        )]
        plan_status = action_plan = row_facts(
            connection, "plans", action["plan_id"])
        assert action_plan is not None
        self._state["plans"] = {action_plan["id"]: action_plan}
        plan_status = action_plan["status"]
        if plan_complete(siblings, command.action_id) \
                and action_plan["status"] in (1, 2):
            self._owners[("plans", action_plan["id"])] = (
                "plan", action_plan["id"])
            templates.append((
                _PLAN_STATUS_EVENT, 2,
                (_update("plans", action_plan["id"],
                         {"status": action_plan["status"]},
                         {"status": _PLAN_COMPLETE}),),
            ))
            plan_status = _PLAN_COMPLETE
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
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
            result=ObtainFinishResult(
                action_status=action_status, plan_status=plan_status,
                prepared=decision.prepared, failed=decision.failed),
        )

    def _load_facts(self, connection) -> ObtainFacts:
        """从已保存行装配汇总事实；装载规则与选择、条目、交付状态一致。"""
        command = self._command
        sources: list[ObtainSourceFacts] = []
        items: list[ObtainItemStage] = []
        with closing(connection.execute(
            "SELECT s.id, s.status FROM obtain_source_selections s"
            " JOIN action_dependencies d ON d.id = s.dependency_id"
            " WHERE d.action_id=? ORDER BY s.id", (command.action_id,),
        )) as cursor:
            selections = cursor.fetchall()
        for selection_id, status in selections:
            facts = row_facts(connection, "obtain_source_selections", selection_id)
            if facts is not None:
                self._state["obtain_source_selections"][selection_id] = facts
            with closing(connection.execute(
                "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?"
                " AND status=1", (selection_id,),
            )) as cursor:
                unresolved = cursor.fetchone()[0]
            sources.append(ObtainSourceFacts(
                selection_fixed=status == int(_SELECTION_STATUS.FIXED),
                unresolved_items=unresolved))
            with closing(connection.execute(
                "SELECT id, status, delivery_id FROM obtain_items"
                " WHERE selection_id=? AND status<>1 ORDER BY id",
                (selection_id,),
            )) as cursor:
                rows = cursor.fetchall()
            for item_id, item_status, delivery_id in rows:
                item = row_facts(connection, "obtain_items", item_id)
                if item is not None:
                    self._state["obtain_items"][item_id] = item
                delivery = None
                if delivery_id is not None:
                    delivery = row_facts(connection, "deliveries", delivery_id)
                    if delivery is not None:
                        self._state["deliveries"][delivery_id] = delivery
                    delivery = delivery["status"] if delivery else None
                items.append(obtain_item_stage(item_status, delivery))
        return ObtainFacts(sources=tuple(sources), items=tuple(items))

    def _unpublished_deliveries(self, connection) -> tuple[int, ...]:
        """汇总确定后仍处于准备或发布中的成功交付。"""
        with closing(connection.execute(
            "SELECT d.id FROM deliveries d"
            " JOIN obtain_items i ON i.delivery_id = d.id"
            " JOIN obtain_source_selections s ON s.id = i.selection_id"
            " JOIN action_dependencies dep ON dep.id = s.dependency_id"
            " WHERE dep.action_id=? AND i.status IN (?, ?)"
            " AND d.status IN (?, ?) ORDER BY d.id",
            (self._command.action_id,
             int(_ITEM_STATUS.SELECTED), int(_ITEM_STATUS.DELIVERY_CREATED),
             int(_DELIVERY_STATUS.PREPARED), int(_DELIVERY_STATUS.PUBLISHING)),
        )) as cursor:
            return tuple(row[0] for row in cursor)

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

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实原事务身份后恢复首次结果。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if not types or types[0][0] != _ACTION_FINISHED_EVENT \
                or types[1:] not in ([], [(_PLAN_STATUS_EVENT, 2)]):
            raise TransactionError("原事务不是取回完成登记，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("取回完成登记的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" \
                or action_row["id"] != self._command.action_id:
            raise TransactionError("原完成登记属于其他动作")
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        return self._recovered_result(connection, action)

    def _recover(self, connection, action) -> CommandPlan:
        """终态后的新键：不重新登记，按既有事实恢复结果或拒绝。"""
        if action["status"] not in (int(_ACTION_STATUS.SUCCEEDED),
                                    int(_ACTION_STATUS.FAILED)):
            raise TransactionError(
                f"取回动作终态不是完成登记结果: {action['status']!r}")
        return self._recovered_result(connection, action)

    def _recovered_result(self, connection, action) -> CommandPlan:
        decision = decide_obtain_finish(self._load_facts(connection))
        if decision.phase is not ObtainPhase.READY_TO_PUBLISH:
            raise TransactionError(
                f"取回汇总事实与终态不符: {decision.phase.value}")
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["actions"][action["id"]] = action
        self._state["plans"] = {plan["id"]: plan}
        self._owners[("actions", action["id"])] = ("action", action["id"])
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObtainFinishResult(
                action_status=action["status"], plan_status=plan["status"],
                prepared=decision.prepared, failed=decision.failed,
                disposition=ObtainFinishDisposition.ALREADY),
        )


def plan_complete(siblings, current_action_id: int) -> bool:
    """父计划的全部动作（含本事务动作）是否都已终态。"""
    return all(
        values.get("status") in _ACTION_TERMINAL
        or values["id"] == current_action_id
        for values in siblings.values()
    )


class OutputsRepository:
    """来源固定、选择与读取资格的 SQLite 仓储。"""

    def resolve_sources(
        self, command: ResolveSources, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ResolveSourcesOutcome]:
        receipt = commit_operation(_ResolveSourcesCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def finish_obtain(
        self, command: FinishObtain, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObtainFinishResult]:
        receipt = commit_operation(_FinishObtainCommand(command, key), key, owned)
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

    def grant_read_slot(
        self, request: SlotRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[SlotDecision]:
        receipt = commit_operation(_SlotChangeCommand(request, _SLOT_GRANT, key), key, owned)
        return _outcome_of(receipt)

    def release_read_slot(
        self, request: SlotRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[SlotDecision]:
        receipt = commit_operation(_SlotChangeCommand(request, _SLOT_RELEASE, key), key, owned)
        return _outcome_of(receipt)

    def load_copy_state(self, copy_id: int, owned: OwnedConnection) -> CopyStateFacts:
        return _load_copy_state_facts(owned.connection, copy_id)

    def reset_copy_target(
        self, request: TargetResetRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[TargetResetDecision]:
        receipt = commit_operation(_CopyResetCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_segment(
        self, command: ReliableSegment, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[SegmentSaveOutcome]:
        receipt = commit_operation(_SegmentSaveCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def save_verification(
        self, command: VerificationSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[VerificationResult]:
        receipt = commit_operation(_IntegritySaveCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def register_recopy(
        self, command: RecopyRegistration, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[RecopyResult]:
        receipt = commit_operation(_RecopyCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def save_prepared(
        self, request: PreparedRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[PreparedSaveOutcome]:
        receipt = commit_operation(_PreparedSaveCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def load_delivery_state(
        self, delivery_id: int, owned: OwnedConnection
    ) -> DeliveryStateFacts:
        return _load_delivery_state_facts(owned.connection, delivery_id)

    def save_publication_intent(
        self, request: PublicationIntentRequest, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[IntentSaveOutcome]:
        receipt = commit_operation(
            _PublicationIntentCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_publication(
        self, request: PublicationSaveRequest, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[PublicationSaveOutcome]:
        receipt = commit_operation(
            _PublicationSaveCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_unconfirmed_failure(
        self, request: UnconfirmedFailureSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[FailureSaveOutcome]:
        receipt = commit_operation(
            _UnconfirmedFailureCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def load_work_file_state(
        self, file_id: int, owned: OwnedConnection,
    ) -> WorkFileFacts:
        return _load_work_file_facts(owned.connection, file_id)

    def save_retention_release(
        self, request: RetentionRelease, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[RetentionReleaseOutcome]:
        receipt = commit_operation(
            _RetentionReleaseCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_cleanup_intent(
        self, request: CleanupIntent, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupIntentOutcome]:
        receipt = commit_operation(
            _CleanupIntentCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_cleanup_result(
        self, request: CleanupResultSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupResultSaveOutcome]:
        receipt = commit_operation(
            _CleanupResultCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def save_cleanup_checked(
        self, request: CleanupChecked, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupCheckedOutcome]:
        receipt = commit_operation(
            _CleanupCheckedCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def load_cleanup_cursor(self, owned: OwnedConnection) -> int | None:
        with closing(owned.connection.execute(
            "SELECT cleanup_cursor_file_id FROM runtime_state WHERE id = 1",
        )) as cursor:
            saved = cursor.fetchone()
        if saved is None:
            raise ConsistencyError("全局运行状态记录缺失")
        return saved[0]

    def next_cleanup_candidates(
        self, after_id: int, ceiling: int, limit: int,
        owned: OwnedConnection,
    ) -> tuple[int, ...]:
        if limit <= 0:
            return ()
        with closing(owned.connection.execute(
            "SELECT id FROM intermediate_files"
            " WHERE retention_state = 2 AND cleanup_state <> 4"
            " AND id > ? AND id <= ? ORDER BY id LIMIT ?",
            (after_id, ceiling, limit),
        )) as cursor:
            return tuple(int(row[0]) for row in cursor.fetchall())

    def max_cleanup_candidate_id(self, owned: OwnedConnection) -> int:
        with closing(owned.connection.execute(
            "SELECT MAX(id) FROM intermediate_files"
            " WHERE retention_state = 2 AND cleanup_state <> 4",
        )) as cursor:
            return int(cursor.fetchone()[0] or 0)


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


def _current_selection_snapshot(context, selection_id: int) -> tuple[int, SelectionSnapshot]:
    """只按完整当前事实解释首次选择，不采用事件结果或未来事务行。"""
    reads = _CurrentCatalogReads(context)
    selection = reads.required("obtain_source_selections", selection_id)
    if (not json_equal(selection["status"], int(_SELECTION_STATUS.PENDING))
            or selection["error_code"] is not None or selection["error_details_json"] is not None):
        raise EventValidationError("首次固定要求当前选择为无错误的 PENDING")
    dependency = reads.required("action_dependencies", selection["dependency_id"])
    owner = reads.required("actions", dependency["action_id"])
    source = reads.required("actions", dependency["depends_on_action_id"])
    source_id = int(source["id"])
    for facts, columns in (
        (owner, ("type", "status", "source_resolution_state", "resolved_source_plan_id")),
        (source, ("type", "status", "plan_id")),
    ):
        if not all(is_json_integer(facts[column]) for column in columns):
            raise EventValidationError("当前动作的类型、状态或来源身份无效")
    source_plan_id = ObjectId(int(source["plan_id"]))
    if (owner["type"] != _OBTAIN_TYPE
            or owner["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED)
            or source["type"] not in _CAPTURE_TYPES
            or owner["resolved_source_plan_id"] != source_plan_id):
        raise EventValidationError("首次选择要求已固定的取回来源及同计划拍摄成员")
    if (owner["status"] != int(_ACTION_STATUS.RUNNING)
            or not json_equal(owner["cancel_requested"], 0)
            or not json_equal(owner["execution_started"], 1)):
        raise EventValidationError("选择固定要求取回动作执行中且未取消")
    if source["status"] not in _ACTION_TERMINAL:
        raise EventValidationError("选择固定要求来源动作已终态")
    if context.complete_rows("obtain_items", "selection_id", selection_id):
        raise EventValidationError("PENDING 选择已有条目，不能追加首次选择")
    mode, requested = read_selection_request(owner["execution_spec_json"], owner["input_fields_json"])
    processing = context.complete_rows("recording_processing", "action_id", source_id)
    if len(processing) > 1:
        raise EventValidationError("来源动作不能存在多条处理记录")
    if not _processing_completed_facts(next(iter(processing.values()), None)):
        raise EventValidationError("来源适用产物处理未完成")
    entries = tuple(member.entry for member in reads.catalog(source_id))
    local_ids = {entry.output_id for entry in entries}
    checked = {}
    for identity in requested:
        if identity in local_ids:
            continue
        found = context.complete_rows("outputs", "id", identity)
        if not found:
            checked[identity] = None
        else:
            actual_source = reads.required("outputs", identity)["source_action_id"]
            if not is_json_integer(actual_source):
                raise EventValidationError("请求产物的当前来源身份无效")
            checked[identity] = int(ObjectId(int(actual_source)))
    expected = select_outputs(
        SourceResolution(state=ResolutionState.FIXED, member_action_ids=(source_id,),
                         source_plan_id=source_plan_id),
        SelectionFacts(source_id, True, entries, checked_output_sources=checked), mode, requested)
    return source_id, expected


def _source_selection_guard(event, context) -> None:
    """逐来源选择守卫：按保存请求核对完整结果及条目 ID 对应的顺序。"""
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
    try:
        source_id, expected = _current_selection_snapshot(context, selection.row_id)
    except (KeyError, TypeError, ValueError) as error:
        raise EventValidationError(f"首次选择的当前事实无法解释: {error}") from error
    items.sort(key=lambda row: row.row_id)
    if len(items) != len(expected.items) or any(
        not json_equal(row.after.values, _item_values(selection.row_id, item))
        for row, item in zip(items, expected.items)
    ):
        raise EventValidationError("选择条目的完整集合、保存顺序或依据与原请求及当前事实不一致")
    error_code = selection.after.values.get("error_code")
    details = selection.after.values.get("error_details_json")
    if not json_equal(error_code, expected.source_error_code):
        raise EventValidationError("来源选择错误与原请求及当前事实不一致")
    _validate_saved_error("obtain_source_selections", error_code, details)
    if details is not None:
        if ("source_action_instance_id" in details
                and details["source_action_instance_id"] != str(source_id)):
            raise EventValidationError("来源错误必须指向实际固定来源")
        if "original_output_id" in details:
            original = context.state_rows.get("outputs", {}).get(int(details["original_output_id"]))
            if (original is None or original.get("source_action_id") != source_id
                    or original.get("kind") != int(_OUTPUT_KIND.ORIGINAL)):
                raise EventValidationError("来源错误中的原片必须属于实际固定来源")
    for item in items:
        _validate_obtain_error(item.after.values, source_id)


def _intermediate_guard(event, context) -> None:
    """中间文件建档与字节事实守卫：用途互斥、初始状态固定。"""
    if event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == 1:
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
    elif event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == _LIFECYCLE_REASON:
        for row in event.rows:
            if row.table != "intermediate_files":
                continue
            after = row.after.values
            if "sha256" in after or "size_bytes" in after:
                if after.get("sha256") is None or after.get("size_bytes") is None:
                    raise EventValidationError("完整字节事实必须同时携带长度与摘要")
    elif event.event_type == _INTERMEDIATE_FILE_EVENT \
            and event.reason == _CLEANUP_INTENT_REASON:
        for row in event.rows:
            if row.table != "intermediate_files" or not row.before.exists:
                raise EventValidationError("清理意图必须是中间文件更新行")
            after = row.after.values
            if set(after) != {"cleanup_state"}:
                raise EventValidationError("清理意图只推进清理状态")
            if after.get("cleanup_state") != int(_FILE_CLEANUP.RUNNING):
                raise EventValidationError("清理意图必须推进到 RUNNING")
            file = context.state_rows.get("intermediate_files", {}).get(row.row_id)
            if file is None or file.get("retention_state") != int(_RETENTION.RELEASABLE):
                raise EventValidationError("清理意图要求文件已释放保留")
    elif event.event_type == _INTERMEDIATE_FILE_EVENT \
            and event.reason == _CLEANUP_RESULT_REASON:
        for row in event.rows:
            if row.table != "intermediate_files" or not row.before.exists:
                raise EventValidationError("清理结果必须是中间文件更新行")
            after = row.after.values
            if set(after) - {"cleanup_state", "last_error_json"}:
                raise EventValidationError("清理结果只更新清理状态与错误")
            target = after.get("cleanup_state")
            if target not in (
                int(_FILE_CLEANUP.COMPLETED),
                int(_FILE_CLEANUP.FAILED),
                int(_FILE_CLEANUP.UNKNOWN),
            ):
                raise EventValidationError("清理结果必须进入完成或失败分区")
            error = after.get("last_error_json")
            if target == int(_FILE_CLEANUP.COMPLETED) and error is not None:
                raise EventValidationError("清理完成不得携带错误")
            if target in (
                int(_FILE_CLEANUP.FAILED), int(_FILE_CLEANUP.UNKNOWN),
            ):
                if not isinstance(error, Mapping):
                    raise EventValidationError("清理失败必须保存结构化错误")
                code, stage = error.get("code"), error.get("stage")
                details = error.get("details")
                try:
                    spec = registered_error(code)
                    if stage != spec["stage"]:
                        raise ValueError(
                            f"失败阶段与公共登记不符: {stage!r} != {spec['stage']!r}")
                    validate_error_details(code, details)
                except (TypeError, ValueError) as failure:
                    raise EventValidationError(str(failure)) from failure


def _cursor_guard(event, context) -> None:
    """历史清理游标守卫：只推进继续位置且必须指向已检查文件。"""
    if event.event_type == _CLEANUP_CURSOR_EVENT \
            and event.reason == _CURSOR_CHECKED_REASON:
        for row in event.rows:
            if row.table != "runtime_state" or not row.before.exists:
                raise EventValidationError("游标推进必须是运行状态更新行")
            after = row.after.values
            if set(after) != {"cleanup_cursor_file_id"}:
                raise EventValidationError("游标事件只更新清理继续位置")
            if after.get("cleanup_cursor_file_id") is None:
                raise EventValidationError("游标推进必须指向已检查的中间文件")


def _delivery_guard(event, context) -> None:
    """交付建档与准备阶段守卫：初始待准备，完成准备以校验为前提。"""
    if event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 1:
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
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _PREPARE_REASON:
        for row in event.rows:
            if row.table != "deliveries" or row.after.values.get("status") != \
                    int(_DELIVERY_STATUS.PREPARED):
                continue
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _INTENT_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            after = row.after.values
            if after.get("status") != int(_DELIVERY_STATUS.PUBLISHING) \
                    or after.get("publication_intent_event_id") != event.event_id:
                raise EventValidationError(
                    "发布意图必须推进到 PUBLISHING 并引用本意图事件")
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == _PUBLISH_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            after = row.after.values
            if after.get("status") != int(_DELIVERY_STATUS.PUBLISHED) \
                    or after.get("published_event_id") != event.event_id:
                raise EventValidationError(
                    "交接完成必须推进到 PUBLISHED 并引用本完成事件")
            _require_publishable_copy(context, row.row_id)
    elif event.event_type == _DELIVERY_CHANGED_EVENT \
            and event.reason == _DELIVERY_FAIL_REASON:
        for row in event.rows:
            if row.table != "deliveries":
                continue
            error = row.after.values.get("error_json")
            if row.after.values.get("status") != int(_DELIVERY_STATUS.FAILED):
                continue
            if not isinstance(error, Mapping):
                raise EventValidationError("交付终局失败必须保存结构化错误")
            code, stage = error.get("code"), error.get("stage")
            details = error.get("details")
            try:
                spec = registered_error(code)
                if stage != spec["stage"]:
                    raise ValueError(
                        f"失败阶段与公共登记不符: {stage!r} != {spec['stage']!r}")
                validate_error_details(code, details)
            except (TypeError, ValueError) as failure:
                raise EventValidationError(str(failure)) from failure
            if details.get("delivery_id") != str(row.row_id):
                raise EventValidationError("交付失败详情必须关联本次交付")


def _require_publishable_copy(context, delivery_id: int) -> None:
    """发布前提：唯一交付拷贝已完成校验且字节完整保存。"""
    copies = [
        facts for facts in context.state_rows.get("file_copies", {}).values()
        if facts.get("delivery_id") == delivery_id
    ]
    if len(copies) != 1:
        raise EventValidationError("准备或发布必须对应唯一交付拷贝")
    copy = copies[0]
    if copy.get("verification_state") not in (
        int(_VERIFICATION.MATCHED),
        int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
    ):
        raise EventValidationError("副本未完成字节校验不能准备或发布")
    if copy.get("committed_bytes") != copy.get("source_size"):
        raise EventValidationError("副本字节未完整保存不能准备或发布")


def _copy_guard(event, context) -> None:
    """拷贝建档、进度段、目标重置、校验与重拷守卫。"""
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RESET_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("目标重置缺少当前拷贝事实")
            if copy.get("committed_bytes") != 0:
                raise EventValidationError("重置完成要求可靠进度已经归零")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _SEGMENT_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("进度段缺少当前拷贝事实")
            before = row.before.values.get("committed_bytes")
            after = row.after.values.get("committed_bytes")
            if not isinstance(after, int) or isinstance(after, bool) \
                    or not isinstance(before, int) or isinstance(before, bool):
                raise EventValidationError("进度段必须携带整数进度范围")
            if after <= before:
                raise EventValidationError("进度段必须推进可靠进度")
            if after > copy.get("source_size"):
                raise EventValidationError("进度段不能越过固定源长度")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _VERIFY_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("校验事实缺少当前拷贝事实")
            if copy.get("committed_bytes") != copy.get("source_size"):
                raise EventValidationError("校验要求全部字节可靠保存")
            after = row.after.values
            state = after.get("verification_state")
            source = after.get("source_sha256", copy.get("source_sha256"))
            target = after.get("target_sha256", copy.get("target_sha256"))
            if state == int(_VERIFICATION.MATCHED):
                if source is None or target is None or source != target:
                    raise EventValidationError("校验通过要求源摘要与主机摘要一致")
            elif state == int(_VERIFICATION.MISMATCHED):
                if source is None or target is None or source == target:
                    raise EventValidationError("摘要不一致要求两个摘要都存在且不等")
            elif state == int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE):
                if source is not None:
                    raise EventValidationError("明确不支持源端摘要不能携带源摘要")
            elif state == int(_VERIFICATION.FAILED):
                if after.get("verification_error_json") is None:
                    raise EventValidationError("校验失败必须携带失败诊断")
            else:
                raise EventValidationError("校验事务只能保存终局校验状态")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RECOPY_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            if copy is None:
                raise EventValidationError("重拷登记缺少当前拷贝事实")
            before = row.before.values
            after = row.after.values
            if before.get("committed_bytes") != copy.get("source_size"):
                raise EventValidationError("重拷登记要求全部字节已经可靠保存")
            if after.get("round") != before.get("round", 0) + 1 \
                    or after.get("recopies_used") != before.get("recopies_used", -1) + 1:
                raise EventValidationError("重拷登记必须恰好增加一轮并消耗一次额度")
            if after.get("committed_bytes") != 0:
                raise EventValidationError("重拷登记必须把可靠进度归零")
            if after.get("target_sha256") is not None:
                raise EventValidationError("新一轮拷贝不能沿用旧目标摘要")
        return
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _CONFIGURE_REASON:
        for row in event.rows:
            if row.table != "file_copies":
                continue
            limit = row.after.values.get("max_recopies_used")
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise EventValidationError("判定上限必须是非负整数")
        return
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
        if (values.get("delivery_id") is None) == (values.get("processing_id") is None):
            raise EventValidationError("拷贝必须恰归属交付或录像处理之一")
        if values.get("processing_id") is not None:
            processing = context.state_rows.get("recording_processing", {}).get(values["processing_id"])
            try:
                requirement = _internal_input_requirement(processing)
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            if requirement is not None:
                raise EventValidationError(
                    "当前处理不具备新的内部输入需求，不能创建输入拷贝")
            parent = processing
        else:
            parent = context.association_rows.get("deliveries", {}).get(values["delivery_id"])
        if parent is None:
            raise EventValidationError("新建拷贝缺少发起责任的固定关联")
        _guard_read_owner(event, context, parent.get("action_id"))


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
    for identity in _known_output_ids(item):
        _required_current_facts(context, "outputs", identity)
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
        code = row.after.values.get("error_code")
        requested = item.get("requested_output_id")
        if code == _OUTPUT_NOT_FOUND_CODE:
            if context.complete_rows("outputs", "id", requested):
                raise EventValidationError("当前查询已有请求产物，不能保存为不存在")
        elif code == _OUTPUT_SOURCE_MISMATCH_CODE:
            target = _required_current_facts(context, "outputs", requested)
            target_source = target.get("source_action_id")
            if (not is_json_integer(target_source) or target_source <= 0
                    or target_source == dependency["depends_on_action_id"]):
                raise EventValidationError("来源不匹配必须由当前其他来源产物证明")
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


def _slot_run(context, copy_id: int) -> dict[str, Any]:
    matches = [
        facts
        for facts in context.state_rows.get("operation_runs", {}).values()
        if facts.get("copy_id") == copy_id
    ]
    if len(matches) != 1:
        raise EventValidationError("机会变化必须提供拷贝的唯一 READ_FILE 流程事实")
    run = matches[0]
    if run.get("kind") != int(_RUN_KIND.READ_FILE):
        raise EventValidationError("机会变化关联的流程不是 READ_FILE")
    return run


def _slot_owner_action(context, copy) -> dict[str, Any]:
    action_id = None
    if copy.get("delivery_id") is not None:
        action_id = context.state_rows.get("deliveries", {}).get(
            copy["delivery_id"], {}).get("action_id")
    elif copy.get("processing_id") is not None:
        action_id = context.state_rows.get("recording_processing", {}).get(
            copy["processing_id"], {}).get("action_id")
    if not is_json_integer(action_id):
        raise EventValidationError("机会授予缺少发起动作的固定关联")
    return _guard_facts(context, "actions", action_id)


def _read_slot_guard(event, context) -> None:
    """机会变化守卫：授予核对绑定与当前资格，释放核对读取及重试结束。"""
    if event.event_type != _COPY_CHANGED_EVENT or event.reason != _SLOT_REASON:
        return
    for row in event.rows:
        if row.table != "file_copies":
            raise EventValidationError("机会变化必须作用于拷贝行")
        if set(row.after.values) != {"slot_device_id"}:
            raise EventValidationError("机会变化只保存归属列")
        copy_id = row.row_id
        copy = _guard_facts(context, "file_copies", copy_id)
        run = _slot_run(context, copy_id)
        source_id = copy.get("source_device_file_id")
        if source_id is None:
            raise EventValidationError("主机源拷贝不能占用相机读取机会")
        source = _guard_facts(context, "device_files", source_id)
        observer = _guard_facts(context, "actions", source["observer_action_id"])
        origin = _guard_facts(context, "actions", source["source_action_id"])
        binding = (observer["device_id"], observer["driver_id"])
        if (not all(isinstance(value, str) and value for value in binding)
                or binding != (origin["device_id"], origin["driver_id"])):
            raise EventValidationError("读取源观察者与可靠来源的原设备绑定不一致")
        after_slot = row.after.values["slot_device_id"]
        if after_slot is not None:
            before_slot = row.before.values.get("slot_device_id") if row.before.exists else None
            if before_slot is not None:
                raise EventValidationError("授予必须从空闲机会开始")
            if after_slot != binding[0]:
                raise EventValidationError("授予的设备必须等于原来源设备绑定")
            if run["status"] in _RUN_TERMINAL:
                raise EventValidationError("读取责任已结束，不能再授予机会")
            if run["retry_wait_required"] != 0:
                raise EventValidationError("重试等待期间不授予新的读取机会")
            action = _slot_owner_action(context, copy)
            try:
                if not _read_owner_eligible(action):
                    raise EventValidationError("机会授予要求发起动作执行中且未取消")
                if not _read_is_due(action, event.occurred_at):
                    raise EventValidationError("机会授予要求发起动作已到计划时间")
            except ConsistencyError as error:
                raise EventValidationError(str(error)) from error
            continue
        if not row.before.exists or row.before.values.get("slot_device_id") is None:
            raise EventValidationError("释放要求当前持有机会")
        if row.before.values["slot_device_id"] != binding[0]:
            raise EventValidationError("释放的机会必须属于原来源设备")
        attempts = context.complete_rows("operation_attempts", "run_id", run["id"])
        if any(attempt.get("status") == int(_ATTEMPT_STATUS.RUNNING)
               for attempt in attempts.values()):
            raise EventValidationError("实际读取未结束，不能释放机会")
        if run["retry_wait_required"] != 0:
            raise EventValidationError("重试等待期间保留机会")
        if run["status"] not in _RUN_TERMINAL and copy["committed_bytes"] < copy["source_size"]:
            raise EventValidationError("源内容尚未读完，机会保留")


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
        for identity in _known_output_ids(item):
            _required_current_facts(context, "outputs", identity)
        selection = _required_current_facts(context, "obtain_source_selections", item.get("selection_id"))
        dependency = _required_current_facts(context, "action_dependencies", selection.get("dependency_id"))
        if (delivery.get("action_id") != dependency.get("action_id")
                or delivery.get("output_id") != item.get("output_id")):
            raise EventValidationError("授予回填的交付必须属于原取回动作及同一产物")
        _guard_read_owner(event, context, dependency.get("action_id"))
        try:
            if has_product_predecessor(_CurrentProductReads(context), dependency["action_id"],
                                       item["output_id"], event.occurred_at):
                raise EventValidationError("较早的合格产物候选尚未取得资格")
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error
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
    elif event.event_type == _READ_PERMISSION_EVENT and event.reason == _RELEASE_REASON:
        for row in event.rows:
            if row.table != "obtain_items" or not row.before.exists:
                continue
            if row.before.values.get("source_dependency") != 1 \
                    or row.after.values.get("source_dependency") != 0:
                raise EventValidationError("解除源依赖必须从有效依赖翻转为解除")
            item = context.state_rows.get("obtain_items", {}).get(row.row_id)
            if item is None:
                raise EventValidationError("解除源依赖缺少取回项事实")
            delivery_id = item.get("delivery_id")
            delivery = context.state_rows.get("deliveries", {}).get(delivery_id)
            if delivery_id is None or delivery is None:
                raise EventValidationError("解除源依赖缺少交付事实")
            copies = [
                facts for facts in context.state_rows.get("file_copies", {}).values()
                if facts.get("delivery_id") == delivery_id
            ]
            if len(copies) != 1:
                raise EventValidationError("解除源依赖必须对应唯一交付拷贝")
            copy = copies[0]
            if copy.get("verification_state") not in (
                int(_VERIFICATION.MATCHED),
                int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
            ):
                raise EventValidationError("副本可靠准备前不能解除源保护")
            if copy.get("committed_bytes") != copy.get("source_size"):
                raise EventValidationError("副本字节未完整保存前不能解除源保护")
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 1:
        for row in event.rows:
            if row.table != "deliveries" or row.before.exists:
                continue
            items = context.state_rows.get("obtain_items", {})
            if not items:
                raise EventValidationError("交付建档必须提供待授予的取回项事实")


def _require_processing_error_document(name: str, document) -> None:
    """协议 error 结构的处理诊断校验；三键完整。"""
    if not isinstance(document, Mapping):
        raise EventValidationError(f"{name} 必须是结构化对象")
    for key in ("code", "stage"):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise EventValidationError(f"{name}.{key} 必须是非空文本")
    if not isinstance(document.get("details"), Mapping):
        raise EventValidationError(f"{name}.details 必须是对象")


def _is_precise_non_negative(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    number = value if isinstance(value, Decimal) else Decimal(value)
    return number.is_finite() and number >= 0


def _validate_check_basis(basis) -> None:
    """检查依据结构校验；未知成员省略，不默认为零。"""
    if not isinstance(basis, Mapping):
        raise EventValidationError("检查依据必须是结构化对象")
    reason = basis.get("reason")
    if isinstance(reason, bool) or not isinstance(reason, int) or reason not in (1, 2, 3):
        raise EventValidationError(f"检查依据来源不在登记范围: {reason!r}")
    target = basis.get("target_duration_ms")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise EventValidationError(f"检查依据必须保存正整数目标时长: {target!r}")
    elapsed = basis.get("control_elapsed_ns")
    if elapsed is not None and (
            isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0):
        raise EventValidationError(f"控制耗时必须是非负整数: {elapsed!r}")
    evidence = basis.get("continuity_evidence")
    if evidence is not None:
        if not isinstance(evidence, list) or not evidence or any(
                not isinstance(item, str) or not item for item in evidence):
            raise EventValidationError(f"连续性证据必须是非空文本列表: {evidence!r}")


def _validate_repair_basis(basis) -> None:
    """修复依据结构校验；门槛秒数随比较理由必填或省略。"""
    if not isinstance(basis, Mapping):
        raise EventValidationError("修复依据必须是结构化对象")
    reason = basis.get("reason")
    if isinstance(reason, bool) or not isinstance(reason, int) or reason not in (1, 2, 3, 4):
        raise EventValidationError(f"修复依据来源不在登记范围: {reason!r}")
    target = basis.get("target_duration_ms")
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise EventValidationError(f"修复依据必须保存正整数目标时长: {target!r}")
    threshold = basis.get("threshold_s")
    if reason in (1, 2):
        if not _is_precise_non_negative(threshold):
            raise EventValidationError(f"门槛比较理由必须提供精确门槛秒数: {threshold!r}")
    elif threshold is not None:
        raise EventValidationError("该修复理由不适用门槛秒数，应省略")
    for key in ("actual_duration_s",):
        value = basis.get(key)
        if value is not None and not _is_precise_non_negative(value):
            raise EventValidationError(f"{key} 必须是非负精确数值: {value!r}")
    elapsed = basis.get("control_elapsed_ns")
    if elapsed is not None and (
            isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0):
        raise EventValidationError(f"控制耗时必须是非负整数: {elapsed!r}")


#: 检查阶段与公共 media 结构 check_status 的对应。
_MEDIA_STATUS_BY_STATE = {2: "running", 3: "completed", 4: "failed", 5: "unconfirmed"}


def _validate_media_document(document, check_state: int) -> None:
    """公共 media 结构与保存阶段的一致性校验。"""
    if not isinstance(document, Mapping):
        raise EventValidationError("媒体观察必须是结构化对象")
    if document.get("check_status") != _MEDIA_STATUS_BY_STATE.get(check_state):
        raise EventValidationError(
            f"媒体观察阶段与保存状态不一致: {document.get('check_status')!r}"
            f" / {check_state!r}")
    duration = document.get("duration")
    if not isinstance(duration, Mapping):
        raise EventValidationError("媒体时长必须是结构化对象")
    duration_status = duration.get("status")
    if duration_status == "available":
        if not _is_precise_non_negative(duration.get("seconds")):
            raise EventValidationError("可用时长必须保存非负精确秒数")
    elif duration_status != "unknown":
        raise EventValidationError(f"媒体时长状态不在允许范围: {duration_status!r}")
    if check_state == 2:
        if duration_status == "available" or "error" in document or "issues" in document:
            raise EventValidationError("检查进行中不能携带时长、错误或问题结论")
    if check_state == 3:
        if duration_status != "available":
            raise EventValidationError("检查完成必须保存可靠视频时长")
        if "error" in document:
            raise EventValidationError("检查完成不能同时携带错误")
    if check_state in (4, 5):
        error = document.get("error")
        if error is None:
            raise EventValidationError("检查失败或未确认必须保存实际错误")
        _require_processing_error_document("媒体错误", error)
    issues = document.get("issues")
    if issues is not None:
        if not isinstance(issues, list):
            raise EventValidationError(f"媒体问题必须是列表: {issues!r}")
        for issue in issues:
            _require_processing_error_document("媒体问题", issue)


def _single_processing_row(event, context):
    """取本事件唯一的处理行及其当前事实。"""
    rows = [row for row in event.rows if row.table == "recording_processing"]
    if len(rows) != 1:
        raise EventValidationError("处理事件必须恰好更新一条处理记录")
    row = rows[0]
    processing = context.state_rows.get("recording_processing", {}).get(row.row_id)
    if processing is None:
        raise EventValidationError("处理事件缺少当前处理事实")
    return row, processing


def _recording_source_guard(event, context) -> None:
    """RECORDING_DECIDED.SOURCE：首次关联的原片必须可靠且属于本动作。"""
    row, processing = _single_processing_row(event, context)
    file_id = row.after.values.get("source_device_file_id")
    if row.before.values.get("source_device_file_id") is not None:
        raise EventValidationError("可靠原片只能首次关联")
    if not isinstance(file_id, int) or isinstance(file_id, bool):
        raise EventValidationError(f"原片身份必须是整数: {file_id!r}")
    file = context.state_rows.get("device_files", {}).get(file_id)
    if file is None:
        raise EventValidationError("关联原片缺少当前文件事实")
    if file.get("role") != int(_DEVICE_FILE_ROLE.ORIGINAL):
        raise EventValidationError(f"关联文件不是原片角色: {file.get('role')!r}")
    if file.get("completion_state") != int(_DEVICE_FILE_COMPLETION.COMPLETE):
        raise EventValidationError("关联原片尚未确认写完")
    if file.get("source_action_id") != processing.get("action_id"):
        raise EventValidationError("关联原片属于其他动作")


def _decided_processing_guard(event, context) -> None:
    """RECORDING_DECIDED 检查与修复决定：一次固定、依据完整。"""
    if event.reason == _DECIDED_CHECK_REASON:
        row, _ = _single_processing_row(event, context)
        decision = row.after.values.get("check_decision")
        if row.before.values.get("check_decision") != 1 or decision not in (2, 3):
            raise EventValidationError("检查决定必须从未判定一次固定")
        _validate_check_basis(row.after.values.get("check_basis_json"))
    elif event.reason == _DECIDED_REPAIR_REASON:
        row, _ = _single_processing_row(event, context)
        state = row.after.values.get("repair_state")
        if row.before.values.get("repair_state") != 1 or state not in (2, 3, 7):
            raise EventValidationError("修复决定必须从未判定一次固定")
        _validate_repair_basis(row.after.values.get("repair_basis_json"))


def _processed_processing_guard(event, context) -> None:
    """RECORDING_PROCESSED 三分支：阶段、媒体与成品事实共同一致。"""
    if event.reason == _PROCESSED_CHECK_REASON:
        row, processing = _single_processing_row(event, context)
        if processing.get("check_decision") != 3:
            raise EventValidationError(
                f"检查执行要求检查决定固定为需要检查: {processing.get('check_decision')!r}")
        state = row.after.values.get("check_state")
        if state not in (2, 3, 4, 5):
            raise EventValidationError(f"检查阶段不在允许范围: {state!r}")
        _validate_media_document(row.after.values.get("media_json"), state)
    elif event.reason == _PROCESSED_REPAIR_REASON:
        row, processing = _single_processing_row(event, context)
        if row.before.values.get("repair_state") not in (3, 4):
            raise EventValidationError(
                "修复结果只能在待执行或执行中阶段保存")
        state = row.after.values.get("repair_state")
        if state not in (4, 5, 6, 7):
            raise EventValidationError(f"修复阶段不在允许范围: {state!r}")
        if state == 5:
            file_id = row.after.values.get("repair_output_file_id")
            if not isinstance(file_id, int) or isinstance(file_id, bool):
                raise EventValidationError(f"修复成功必须指向修复输出: {file_id!r}")
            output = context.state_rows.get("intermediate_files", {}).get(file_id)
            if output is None:
                raise EventValidationError("修复成功缺少输出文件事实")
            if output.get("purpose") != int(_INTERMEDIATE_PURPOSE.REPAIR_OUTPUT):
                raise EventValidationError(
                    f"修复成功必须指向修复输出用途: {output.get('purpose')!r}")
            if output.get("owner_action_id") != processing.get("action_id"):
                raise EventValidationError("修复输出属于其他动作")
            if output.get("size_bytes") is None or output.get("sha256") is None:
                raise EventValidationError("修复成功要求输出已有完整字节事实")
        elif state == 6:
            _require_processing_error_document(
                "修复错误", row.after.values.get("repair_error_json"))
    elif event.reason == _PROCESSED_DISCARD_REASON:
        row, _ = _single_processing_row(event, context)
        state = row.after.values.get("discard_state")
        if state not in (2, 3, 4, 5, 6):
            raise EventValidationError(f"收场进度不在允许范围: {state!r}")
        if state in (5, 6):
            _require_processing_error_document(
                "收场错误", row.after.values.get("discard_error_json"))


def _processing_guard(event, context) -> None:
    """录像处理守卫：字节事实与处理状态分支共同约束。

    字节分支要求处理输入副本保存完整字节事实前完成唯一拷贝
    校验；状态分支校验 RECORDING_DECIDED/RECORDING_PROCESSED 的
    决定、依据、媒体结构与成品事实。
    """
    if event.event_type == _RECORDING_DECIDED_EVENT:
        _decided_processing_guard(event, context)
        return
    if event.event_type == _RECORDING_PROCESSED_EVENT:
        _processed_processing_guard(event, context)
        return
    if event.event_type != _INTERMEDIATE_FILE_EVENT or event.reason != _LIFECYCLE_REASON:
        return
    for row in event.rows:
        if row.table != "intermediate_files":
            continue
        if row.after.values.get("sha256") is None:
            continue
        copies = [
            facts for facts in context.state_rows.get("file_copies", {}).values()
            if facts.get("target_file_id") == row.row_id
        ]
        if not copies:
            continue
        if len(copies) != 1:
            raise EventValidationError("处理输入的完整字节事实必须对应唯一拷贝")
        copy = copies[0]
        if copy.get("verification_state") not in (
            int(_VERIFICATION.MATCHED),
            int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
        ):
            raise EventValidationError("处理输入副本未完成字节校验不能保存完整事实")
        if copy.get("committed_bytes") != copy.get("source_size"):
            raise EventValidationError("处理输入副本字节未完整保存不能保存完整事实")


def _requested_cleanup_ids(action):
    """精确清理的原请求 ID 列表；范围清理返回 None。"""
    params = action.get("input_fields_json")
    if not isinstance(params, Mapping):
        raise EventValidationError("清理动作缺少原请求参数")
    values = params.get("params")
    if not isinstance(values, Mapping) or "output_ids" not in values:
        return None
    ids = values["output_ids"]
    if not isinstance(ids, list) or not ids:
        raise EventValidationError("精确清理缺少非空原目标列表")
    return tuple(int(identity) for identity in ids)


def _target_set_guard(event, context) -> None:
    """TARGETS_FIXED 清理/取消/失败：类型、目标状态与成员集合一致。"""
    if event.event_type != _TARGETS_FIXED_EVENT:
        return
    if event.reason not in (_TARGETS_CLEANUP_REASON, _TARGETS_CANCEL_REASON,
                            _TARGETS_FAIL_REASON):
        return
    action_rows = [row for row in event.rows if row.table == "actions"]
    if len(action_rows) != 1 or not action_rows[0].before.exists:
        raise EventValidationError("目标固定必须恰好更新一条动作行")
    action_id = action_rows[0].row_id
    action = context.state_rows.get("actions", {}).get(action_id)
    if action is None:
        raise EventValidationError("目标固定缺少动作当前事实")
    if event.reason == _TARGETS_FAIL_REASON:
        if action.get("type") not in (_DELETE_ACTION_TYPE, _CANCEL_ACTION_TYPE):
            raise EventValidationError("目标固定失败要求清理或取消动作")
    else:
        expected = (_CANCEL_ACTION_TYPE
                    if event.reason == _TARGETS_CANCEL_REASON
                    else _DELETE_ACTION_TYPE)
        if action.get("type") != expected:
            raise EventValidationError(
                f"目标固定分支与动作类型不符: {action.get('type')!r}")
    created = [row for row in event.rows
               if row.table == "cleanup_items" and not row.before.exists]
    if event.reason == _TARGETS_FAIL_REASON:
        if created:
            raise EventValidationError("目标固定失败不创建清理成员")
        return
    if event.reason != _TARGETS_CLEANUP_REASON:
        return
    if not created:
        raise EventValidationError("清理目标固定必须创建清理成员")
    requested = []
    for row in created:
        values = row.after.values
        if values.get("action_id") != action_id:
            raise EventValidationError("清理成员必须属于目标固定动作")
        if (values.get("status") != int(_CLEANUP_ITEM_STATUS.UNRESOLVED)
                or values.get("restriction_state")
                != int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED)
                or values.get("output_id") is not None
                or values.get("outcome") is not None
                or values.get("final_event_id") is not None
                or values.get("error_code") is not None):
            raise EventValidationError("清理成员初始值必须是未解析且无限制")
        requested.append(values.get("requested_output_id"))
    if len(set(requested)) != len(requested):
        raise EventValidationError("清理成员的原请求目标不能重复")
    explicit = _requested_cleanup_ids(action)
    if explicit is not None and tuple(requested) != explicit:
        raise EventValidationError("精确清理固定全部原请求 ID 且保持顺序")
    if explicit is None:
        dependencies = context.state_rows.get("action_dependencies", {})
        sources = {
            values.get("depends_on_action_id")
            for values in dependencies.values()
            if values.get("action_id") == action_id
        }
        outputs = context.state_rows.get("outputs", {})
        for identity in requested:
            output = outputs.get(identity)
            if output is None or output.get("source_action_id") not in sources:
                raise EventValidationError(
                    f"范围清理成员不属于本动作固定来源: {identity!r}")


def _cleanup_member_guard(event, context) -> None:
    """CLEANUP_CHANGED：成员归属、身份保持与直接终态事务边界。"""
    if event.event_type != _CLEANUP_CHANGED_EVENT:
        return
    for row in event.rows:
        if row.table != "cleanup_items":
            continue
        if not row.before.exists:
            values = row.after.values
            action = context.state_rows.get("actions", {}).get(
                values.get("action_id"))
            if (action is None
                    or action.get("target_selection_state") != _TARGET_FIXED):
                raise EventValidationError(
                    "直接终态清理成员只能属于已固定的目标集合事务")
            if values.get("final_event_id") != event.event_id:
                raise EventValidationError("清理成员的最终事件必须是本事件")
            continue
        before_facts = context.state_rows.get("cleanup_items", {}).get(row.row_id)
        if before_facts is None:
            raise EventValidationError("清理成员更新缺少当前事实")
        action = context.state_rows.get("actions", {}).get(
            before_facts.get("action_id"))
        if (action is None
                or action.get("target_selection_state") != _TARGET_FIXED):
            raise EventValidationError("清理成员推进要求目标集合已固定")
        if (row.after.values.get("requested_output_id")
                not in (None, before_facts.get("requested_output_id"))
                or row.after.values.get("action_id")
                not in (None, before_facts.get("action_id"))):
            raise EventValidationError("清理成员身份与原请求保持不变")
        if event.reason == 1:
            confirmed = row.after.values.get("output_id")
            if (confirmed is not None
                    and before_facts.get("output_id") is not None):
                raise EventValidationError("产物身份只能从空值一次确认")
            if (confirmed is not None
                    and confirmed != before_facts.get("requested_output_id")):
                raise EventValidationError("确认的产物必须是原请求目标")


def _cleanup_guard(event, context) -> None:
    """CLEANUP_CHANGED：终态依据、最终事件与唯一删除处理者。"""
    if event.event_type != _CLEANUP_CHANGED_EVENT:
        return
    for row in event.rows:
        if row.table != "cleanup_items":
            continue
        values = row.after.values
        terminal = values.get("status") in (
            int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
            int(_CLEANUP_ITEM_STATUS.FAILED),
            int(_CLEANUP_ITEM_STATUS.CANCELED),
        )
        if terminal:
            if values.get("final_event_id") != event.event_id:
                raise EventValidationError("清理成员的最终事件必须是本事件")
            if values.get("status") == int(_CLEANUP_ITEM_STATUS.SUCCEEDED):
                _require_cleanup_success_basis(event, row, context)
            continue
        if values.get("status") == int(_CLEANUP_ITEM_STATUS.DELETING):
            before = context.state_rows.get("cleanup_items", {}).get(
                row.row_id, {})
            target = before.get("output_id") or values.get("output_id")
            if target is None:
                continue
            for identity, facts in context.state_rows.get(
                    "cleanup_items", {}).items():
                if identity == row.row_id:
                    continue
                if (facts.get("output_id") == target
                        and facts.get("status")
                        == int(_CLEANUP_ITEM_STATUS.DELETING)):
                    raise EventValidationError("同一产物只能有一个删除中成员")


def _require_cleanup_success_basis(event, row, context) -> None:
    """成功依据按结果分类核对：实际删除、已有完成或存在性查询。"""
    outcome = row.after.values.get("outcome")
    if outcome is None:
        raise EventValidationError("清理成功必须携带结果依据")
    target = (row.after.values.get("output_id")
              or row.before.values.get("output_id"))
    if outcome == int(_CLEANUP_OUTCOME.ALREADY_CLEANED):
        output = context.state_rows.get("outputs", {}).get(target)
        if (output is None
                or output.get("availability") != int(_AVAILABILITY.CLEANED)):
            raise EventValidationError("已有完成依据要求产物已可靠清理")
        return
    output = context.state_rows.get("outputs", {}).get(target)
    file_id = None
    if output is not None:
        file_id = (output.get("device_file_id")
                   or output.get("intermediate_file_id"))
    if outcome == int(_CLEANUP_OUTCOME.DELETED):
        if not _completed_call_basis(context, row.row_id, file_id):
            raise EventValidationError(
                "实际删除依据要求已完成的删除调用或文件缺席事实")
    elif outcome == int(_CLEANUP_OUTCOME.ABSENCE_CONFIRMED):
        if file_id is not None and not _file_absent(context, file_id):
            raise EventValidationError("存在性查询依据要求文件缺席事实")
    else:
        raise EventValidationError(f"未登记的清理结果依据: {outcome!r}")


def _completed_call_basis(context, item_id, file_id) -> bool:
    """本事件之前已有完成的删除调用或文件缺席事实。"""
    if file_id is not None and _file_absent(context, file_id):
        return True
    runs = {
        values.get("id"): values
        for values in context.state_rows.get("operation_runs", {}).values()
        if values.get("cleanup_item_id") == item_id
    }
    for attempt in context.state_rows.get("operation_attempts", {}).values():
        run = runs.get(attempt.get("run_id"))
        if (run is not None and attempt.get("status") == 2
                and attempt.get("effect_state") == 3):
            return True
    return False


def _file_absent(context, file_id) -> bool:
    device = context.state_rows.get("device_files", {}).get(file_id)
    if device is not None and device.get("presence_state") == 3:
        return True
    local = context.state_rows.get("intermediate_files", {}).get(file_id)
    if local is not None and local.get("cleanup_state") == 4:
        return True
    return False

def register_outputs_guards() -> None:
    """注册来源、选择与读取资格事件的正式业务守卫（装配期调用）。"""
    register_guard("selection_initialization", _selection_initialization_guard)
    register_guard("source_selection", _source_selection_guard)
    register_guard("intermediate", _intermediate_guard)
    register_guard("cursor", _cursor_guard)
    register_guard("delivery", _delivery_guard)
    register_guard("copy", _copy_guard)
    register_guard("copy_links", _copy_links_guard)
    register_guard("read_slot", _read_slot_guard)
    register_guard("read_permission", _read_permission_guard)
    register_guard("processing", _processing_guard)
    register_guard("recording_source", _recording_source_guard)
    register_guard("obtain_member", _obtain_member_guard)
    register_guard("target_set", _target_set_guard)
    register_guard("cleanup_member", _cleanup_member_guard)
    register_guard("cleanup", _cleanup_guard)
