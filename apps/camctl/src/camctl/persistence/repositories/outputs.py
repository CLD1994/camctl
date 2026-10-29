"""取回来源固定与逐来源选择的持久化事务。

SOURCE_RESOLVED 在取得执行资格后一次固定取回或范围清理的跨计划
来源成员（或可靠失败），取回成员在同一事务共同初始化 PENDING 选择；
TARGETS_FIXED 将一个来源的完整选择（含合法空选择与逐项失败）与
FIXED 状态同一事务保存。已固定的来源与选择不因重送、重启或新文
件出现而重选或扩大；此前已可靠确认存在的产物记录缺失按状态库
矛盾拒绝，不解释为普通不存在。
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from decimal import Decimal
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from types import SimpleNamespace
from typing import Any, Mapping, Protocol

from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.logging_runtime.diagnostics import DiagnosticState
from camctl.history.decoding import decode_event_row
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import (
    ConsistencyError, MAX_OBJECT_ID, ObjectId, OperationKey, UtcMicros, parse_object_id,
)
from camctl.devices.bindings import BindingResult, binding_failure_details
from camctl.history.reads import PlanReadScope, ReadCoverage
from camctl.history.events import business_columns, reason_for_branch
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    PathRuleError, object_file_name, relative_file_path, validate_relative_file_path,
)
from camctl.operations.attempts import (
    AttemptFinish, AttemptIntent, AttemptTarget, BeginAttemptResult, BeginDisposition,
    FinishAttemptResult, OperationKind, RunOutcome, StaleRunFinish, operation_responsibility_key)
from camctl.operations.models import ErrorValue, SettlementBasis
from camctl.persistence.repositories.operations import (
    BeginAttemptCommand, FinishAttemptCommand, _FinishStaleRunsCommand)
from camctl.outputs.catalog import OutputKind
from camctl.outputs.cleanup_flow import (
    CancelCleanupItem,
    CleanupActionDisposition,
    CleanupActionFinished,
    CleanupItemDisposition,
    CleanupItemSaved,
    CleanupOutcomeChoice,
    CleanupStartDisposition,
    CleanupStartResult,
    CleanupTargetsDisposition,
    CleanupTargetsSaved,
    FailCleanupItem,
    FinishCanceledCleanupAction,
    FinishCleanupAction,
    FinishCleanupItem,
    FixCleanupTargets,
    ProgressCleanupItem,
    RestrictCleanupItem,
    StartCleanupAction,
)
from camctl.outputs.competition import has_product_predecessor
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE, device_holder, first_eligible_read, positive_limit
from camctl.outputs.copy import (
    AttemptRecord, CopyStateFacts, CopyTargetRef, PreparedCopy, PreparedDisposition,
    PreparedRequest, PreparedSaveOutcome, RecopyDisposition, RecopyRegistration,
    RecopyResult, ReliableSegment, SegmentSaveDisposition, SegmentSaveOutcome,
    SourceChecksumSupport, TargetResetDecision, TargetResetOutcome, TargetResetRequest,
    VerificationDisposition, VerificationResult, VerificationSave,
)
from camctl.outputs.handoff import (
    AdvanceWithdrawal,
    WithdrawalChoice,
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
from camctl.outputs.definitions import read_excluded_output_ids, read_selection_request
from camctl.outputs.obtain_summary import (
    ObtainFacts, ObtainAggregate,
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
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    TransactionAllocations,
    commit_operation,
    event_envelope as _envelope,
    next_row_id,
    row_change as _row,
    row_facts,
    saved_transaction_events,
    read_transaction_range,
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
_FILE_PRESENCE = enum_for("device_files.presence_state")
_OUTPUT_REGISTERED_EVENT = 20
_OUTPUT_OBSERVATION_REASON = 4
_CLEANUP_ITEM_NOT_FOUND = item_error_id("cleanup_items", "output_not_found")
_CLEANUP_ITEMS_FAILED_ID = action_error_id("cleanup_items_failed")
_CLEANUP_RESTRICTION = enum_for("cleanup_items.restriction_state")
_CLEANUP_OUTCOME = enum_for("cleanup_items.outcome")
_ACTION_STARTED_EVENT = 5
_READ_PERMISSION_EVENT = 21
_COPY_CHANGED_EVENT = 22
_COPY_SOURCE_FALLBACK_REASON = reason_for_branch(
    "COPY_CHANGED", "SOURCE_FALLBACK")
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
#: 清理逐项最终失败的动作错误（具体失败项由成员事实表达）。
_CLEANUP_ITEMS_FAILED = "cleanup_items_failed"
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
_PHOTO_READ_START_REASON = reason_for_branch("COPY_CHANGED", "PHOTO_READ_START")
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
_FILE_CHECKSUM = enum_for("device_files.checksum_support")
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
_SEGMENT_INSPECTION_STATE = enum_for("segment_inspections.state")

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


    def __post_init__(self):
        ObjectId(self.action_id)
        if not isinstance(self.spec, SourceSpec):
            raise TypeError("来源解析申请必须使用 SourceSpec")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError("来源解析时刻超出 SQLite 整数微秒范围")


@dataclass(frozen=True)
class ResolveSourcesOutcome:
    """来源解析的有限回执；成员与选择身份从持久关系分页读取。"""

    fixed: bool
    member_count: int = 0
    selection_count: int = 0
    source_plan_id: int | None = None
    error_code: int | None = None
    error_details: dict[str, Any] | None = None
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


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


@dataclass(frozen=True)
class StartObtainAction:
    """一次取回动作开始执行的输入；时间资格由调用入口判断。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class ObtainStartDisposition(Enum):
    """取回动作开始事务的结果分区。"""

    SAVED = "saved"
    #: 动作已终态：只读恢复，不重复开始。
    ALREADY = "already"
    #: 未开始且不再普通执行（取消已生效），由取消链收场。
    REJECTED = "rejected"


@dataclass(frozen=True)
class ObtainStartResult:
    """取回动作开始事务的已保存事实。"""

    disposition: ObtainStartDisposition = ObtainStartDisposition.SAVED
    reason: str | None = None


class _StartObtainCommand:
    """取回动作开始执行的事务命令（ACTION_STARTED.START）。

    只保存开始事实；来源解析、选择固定与读取推进由后续事务依次
    推进。受理时已固定的同计划来源在开始事务中为每条既有依赖初
    始化一条 PENDING 选择（守卫强制一一对应）；执行期解析的来源
    没有既有依赖，选择由来源固定事务创建。到时取回动作开始时不
    占用设备活动，与拍摄动作的启动互不影响；父计划首次开始与开
    始事实同事务保存。
    """

    def __init__(self, command: StartObtainAction, key: OperationKey, read_batch_size=DEFAULT_BATCH_SIZE) -> None:
        if not isinstance(command, StartObtainAction):
            raise TypeError("取回开始申请必须使用 StartObtainAction")
        self._command = command
        self._key = key
        self._read_batch_size = positive_limit(read_batch_size)
    def plan(self, scope):
        from .source_transactions import plan_source
        return plan_source(scope, self._command, self._key, self._read_batch_size, start=True)


class _StartCleanupCommand:
    """清理动作开始执行的事务命令（ACTION_STARTED.START）。

    只保存开始事实；目标集合固定、逐成员删除与汇总终态由后续事
    务依次推进。到时清理动作开始时不占用设备活动，与拍摄动作的
    启动互不影响；父计划首次开始与开始事实同事务保存。
    """

    def __init__(self, command: StartCleanupAction, key: OperationKey) -> None:
        if not isinstance(command, StartCleanupAction):
            raise TypeError("清理开始申请必须使用 StartCleanupAction")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved, connection)
        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise ConsistencyError(f"清理动作不存在: {command.action_id}")
        self._state["actions"] = {command.action_id: dict(action)}
        if action["type"] != _DELETE_ACTION_TYPE:
            raise ConsistencyError(
                f"开始事务只适用于清理动作: {command.action_id}"
                f" type={action['type']!r}")
        if action["status"] in _ACTION_TERMINAL:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupStartResult(
                    disposition=CleanupStartDisposition.ALREADY))
        if action["cancel_requested"]:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupStartResult(
                    disposition=CleanupStartDisposition.REJECTED,
                    reason="canceled"),
            )
        if action["status"] != int(_ACTION_STATUS.PENDING) \
                or action["execution_started"] != 0:
            raise TransactionError(
                f"清理动作不在待执行状态: {command.action_id}"
                f" status={action['status']!r}"
                f" execution_started={action['execution_started']!r}")
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError(f"计划不存在: {action['plan_id']}")
        self._state.setdefault("plans", {})[plan["id"]] = plan
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        rows = [_update(
            "actions", command.action_id,
            {"status": int(_ACTION_STATUS.PENDING),
             "execution_started": 0},
            {"status": int(_ACTION_STATUS.RUNNING),
             "execution_started": 1})]
        allocation = scope.allocate(2 if plan["status"] == 1 else 1)
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTION_STARTED_EVENT, 1, tuple(rows),
            command.occurred_at)]
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
            result=CleanupStartResult())

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        """原键重送：核实原开始分支与输入后恢复首次响应。"""
        command = self._command
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds not in (
                [(_ACTION_STARTED_EVENT, 1)],
                [(_ACTION_STARTED_EVENT, 1), (_PLAN_STATUS_EVENT, 1)]):
            raise TransactionError(
                "操作身份已用于其他事务，不能作为清理开始重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("清理开始的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "actions" or row["id"] != command.action_id:
            raise TransactionError("原清理开始属于其他动作")
        facts = row_facts(connection, "actions", command.action_id)
        if facts is None or facts["status"] != int(_ACTION_STATUS.RUNNING) \
                or facts["execution_started"] != 1:
            raise TransactionError("原清理开始的可靠记录与输入不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupStartResult())


class _ResolveSourcesCommand:
    """执行期来源解析的完整事务命令。"""

    def __init__(self, command: ResolveSources, key: OperationKey, read_batch_size=DEFAULT_BATCH_SIZE) -> None:
        if not isinstance(command, ResolveSources):
            raise TypeError("来源解析申请必须使用 ResolveSources")
        self._command = command
        self._key = key
        self._read_batch_size = positive_limit(read_batch_size)
    def plan(self, scope):
        from .source_transactions import plan_source
        return plan_source(scope, self._command, self._key, self._read_batch_size)


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
        from .segment_selections import is_segmented_source

        if is_segmented_source(connection, source_action_id):
            raise ConsistencyError("分段来源须按已发布范围逐项固定选择")
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
        facts = SelectionFacts(
            source_action_id,
            True,
            entries,
            checked_output_sources=reads.checked_sources(entries, requested),
            source_failed=(source["status"] == int(_ACTION_STATUS.FAILED)),
        )
        expected = select_outputs(
            SourceResolution(state=ResolutionState.FIXED, member_action_ids=(source_action_id,),
                             source_plan_id=source["plan_id"]), facts, mode, requested,
            excluded_output_ids=read_excluded_output_ids(owner["input_fields_json"]))
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
        # Saved JSON facts may represent an integral database ID as Decimal.
        # Once exact-integrality, equality, and ObjectId range have been checked,
        # expose the canonical integer form to strict file-path identity APIs.
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
    """静态事件边界的完整事实装载；默认资格链使用逐批读取端口。"""

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

    def __new__(cls, context):
        if context.current_reads is not None:
            return _BoundedProductReads(context.current_reads)
        return super().__new__(cls)

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


class _BoundedProductReads:
    """资格计算与守卫各自读取当前批；不把比较对象累计到命令提案。"""

    def __init__(self, view):
        self.view = view

    def required(self, table, identity):
        return self.view.required(table, identity)

    def optional(self, table, identity):
        return self.view.optional(table, identity)

    def action(self, identity):
        return self.view.optional("actions", identity, self.view.SOURCE_COLUMNS)

    def iter_members(self, table, column, identity):
        for batch in self.view.iter_members(table, column, identity):
            yield from batch

    def iter_plan_actions(self, identity):
        for batch in self.view.iter_members("actions", "plan_id", identity, columns=self.view.SOURCE_COLUMNS):
            yield from batch

    def actions(self):
        return self.view.actions()

    def family(self, output_id):
        return _original_outputs(self, output_id)

    def origin(self, output_id):
        rows = iter(self.iter_members("output_origins", "output_id", output_id))
        first = next(rows, None)
        if next(rows, None) is not None:
            raise ConsistencyError("同一产物不能重复登记原片关联")
        return None if first is None else first[1]["original_output_id"]

    def related(self, original_id):
        result = []
        for _, row in self.iter_members("output_origins", "original_output_id", original_id):
            result.append(row["output_id"])
            if len(result) > 2:
                raise ConsistencyError("同一原片最多登记一份预览和一份修复成品")
        return tuple(result)

    def processing_completed(self, source_id):
        rows = iter(self.iter_members("recording_processing", "action_id", source_id))
        first = next(rows, None)
        if next(rows, None) is not None:
            raise ConsistencyError("录像处理责任重复")
        return _processing_completed_facts(None if first is None else first[1])


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
        source_failed=(source["status"] == int(_ACTION_STATUS.FAILED)),
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


def _host_source_purpose(kind, action_type):
    """正式主机源的角色用途；修复与单张拍摄分别限定所属动作。"""
    if kind == int(_OUTPUT_KIND.REPAIRED) and action_type == int(_ACTION_TYPE.CAMERA_RECORD):
        return _PURPOSE.REPAIR_OUTPUT
    if action_type == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO):
        if kind == int(_OUTPUT_KIND.ORIGINAL):
            return _PURPOSE.CAPTURE_ORIGINAL
        if kind == int(_OUTPUT_KIND.PREVIEW):
            return _PURPOSE.CAPTURE_PREVIEW
    raise ConsistencyError("主机产物种类与来源动作不一致")


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
        else:
            source_action = required("actions", original["source_action_id"])
            purpose = _host_source_purpose(entry["kind"], source_action["type"])
            if (file["owner_delivery_id"] is not None
                    or file["purpose"] != int(purpose)
                    or file["retention_state"] != int(_RETENTION.PROMOTED)
                    or file["cleanup_state"] not in (int(_FILE_CLEANUP.NOT_NEEDED), int(_FILE_CLEANUP.COMPLETED))):
                raise ConsistencyError("主机产物必须由来源动作的已提升角色文件承载")
            if (file["cleanup_state"] == int(_FILE_CLEANUP.COMPLETED)
                    and entry["availability"] != int(_AVAILABILITY.CLEANED)):
                raise ConsistencyError("已清理主机源必须保留对应产物的可靠清理状态")
            validate_relative_file_path(FilePurpose[purpose.name], file["id"], file["relative_path"])
            if kind is OutputKind.PREVIEW and original["intermediate_file_id"] is None:
                raise ConsistencyError("主机预览必须关联同源主机原图")
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


def _output_cleanup_phase(connection, output_id):
    """汇总同产物仍有效的读取限制；已解除限制的历史不参与判断。"""
    # 共同物理源可由配对成员的可靠删除事实完成；本产物的请求项
    # 可能仍待消费该事实。
    row = connection.execute(
        "SELECT availability,cleanup_status FROM outputs WHERE id=?", (output_id,)).fetchone()
    if row is not None and row == (int(_AVAILABILITY.CLEANED), int(_OUTPUT_CLEANUP_STATUS.COMPLETED)):
        return _OUTPUT_CLEANUP_STATUS.COMPLETED
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
        "segment_inspections",
        "segment_sets",
        "segment_members",
        "deliveries",
        "file_copies",
        "operation_runs",
        "intermediate_files",
    )

    def __init__(self, command: FileCandidate, key: OperationKey, read_batch_size=DEFAULT_BATCH_SIZE) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._read_coverage = ReadCoverage()
        self._read_batch_size = positive_limit(read_batch_size)
        self._product_read_scope = None
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
        binding_failure = self._binding_failure(source_file)
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
        elif command.processing_id is not None:
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
            from camctl.history.reads import ProductReadScope
            from camctl.persistence.event_reads import SqlEventReads

            self._product_read_scope = ProductReadScope(command.action_id, command.output_id,
                command.occurred_at, self._read_batch_size)
            reads = _BoundedProductReads(SqlEventReads(connection, self._product_read_scope))
            if has_product_predecessor(reads, command.action_id, command.output_id, command.occurred_at):
                return self._wait("business_order")

        if (binding_failure is not None and binding_failure["reason"] == "physical_identity_conflict"
                and command.source_device_file_id is not None):
            observer = self._state["actions"][source_file["observer_action_id"]]
            from .physical_alias import source_physical_alias_conflict
            origin = self._state["actions"][source_file["source_action_id"]]
            if not source_physical_alias_conflict(connection, source_file, observer, origin, action_id=command.action_id):
                binding_failure = None
        if binding_failure is not None and binding_failure["reason"] == "physical_identity_mismatch":
            from .physical_alias import verify_source_identity_mismatch
            observer = self._state["actions"][source_file["observer_action_id"]]
            origin = self._state["actions"][source_file["source_action_id"]]
            if not verify_source_identity_mismatch(connection, source_file, observer, origin,
                                                   command.binding_result.current_identity, action_id=command.action_id):
                raise TransactionError("读取机身不符申请与固定来源一致")
        if binding_failure is not None:
            if command.item_id is None:
                raise ConsistencyError("内部输入的绑定失败须由所属处理责任保存")
            return self._reject_item(scope, item_error_id(
                "obtain_items", "device_binding_unavailable"), binding_failure)

        return self._grant(scope, source_file)

    def _binding_failure(self, source_file) -> dict | None:
        result = self._command.binding_result
        if result is None:
            return None
        observer = self._state["actions"].get(source_file.get("observer_action_id"))
        if observer is None or (observer["device_id"], observer["driver_id"]) != (
                result.binding.device_id, result.binding.driver_id):
            raise TransactionError("读取候选的绑定核对与原文件观察者身份不同")
        details = binding_failure_details(result)
        if details is not None:
            validate_error_details("device_binding_unavailable", details)
            if details["reason"] == "mismatch" and details.get("actual_driver_id") == observer["driver_id"]:
                raise TransactionError("驱动不一致必须采用不同的实际驱动")
        return details

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
            if command.processing_id is not None:
                processing = self._required(connection, "recording_processing", command.processing_id)
                if (
                    action["type"] != int(_ACTION_TYPE.CAMERA_RECORD)
                    or processing["action_id"] != command.action_id
                    or processing["source_device_file_id"] != command.source_device_file_id
                    or source_file["source_action_id"] != command.action_id
                    or source_file["role"] != int(_DEVICE_FILE_ROLE.ORIGINAL)
                ):
                    raise ConsistencyError("内部读取的录像、处理责任与原片归属不一致")
            else:
                inspection = self._required(connection, "segment_inspections", command.inspection_id)
                member = self._required(connection, "segment_members", inspection["member_id"])
                segment_set = self._required(connection, "segment_sets", inspection["set_id"])
                from camctl.devices.tasks import CaptureResultPolicy

                budget = {"max_attempts": command.config.max_attempts,
                          "timeout_s": command.config.timeout_s,
                          "retry_interval_s": command.config.retry_interval_s}
                action6_type = action["type"] in (
                    int(_ACTION_TYPE.CAMERA_RECORD),
                    int(_ACTION_TYPE.CAMERA_TIMELAPSE),
                )
                if (not action6_type
                        or action["execution_spec_json"].get("result_policy")
                        != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
                        or inspection["action_id"] != command.action_id
                        or inspection["state"] != int(_SEGMENT_INSPECTION_STATE.PENDING)
                        or inspection["source_device_file_id"] != command.source_device_file_id
                        or segment_set["action_id"] != command.action_id
                        or member["set_id"] != inspection["set_id"]
                        or member["file_id"] != command.source_device_file_id
                        or source_file["source_action_id"] != command.action_id
                        or source_file["role"] != int(_DEVICE_FILE_ROLE.ORIGINAL)
                        or not json_equal(inspection["budget_json"], budget)):
                    raise ConsistencyError("分段检查责任、原片或首次保存的读取预算不一致")
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
        reuse_check_copy = command.reuse_inspection_id is not None
        reuse_photo_cache = (command.reuse_photo_page_event_id is not None
                             or command.reuse_photo_start_event_id is not None)
        reuse_device_cache = reuse_check_copy or reuse_photo_cache
        if (
            action["type"] != _OBTAIN_TYPE
            or action["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED)
            or action["resolved_source_plan_id"] != source_action["plan_id"]
            or dependency["action_id"] != command.action_id
            or selection["status"] != int(_SELECTION_STATUS.FIXED)
            or item["output_id"] != command.output_id
            or dependency["depends_on_action_id"] != output["source_action_id"]
            or (not reuse_device_cache and (
                output["device_file_id"] != command.source_device_file_id
                or output["intermediate_file_id"] != command.source_intermediate_file_id))
            or (reuse_device_cache and (
                command.source_device_file_id is not None
                or command.source_intermediate_file_id is None
                or output["device_file_id"] is None
                or output["intermediate_file_id"] is not None))
        ):
            raise ConsistencyError("取回动作、固定选择、产物与源文件归属不一致")
        if reuse_check_copy:
            from camctl.outputs.segment_copy_reuse import read_bound_check_copy

            bound = read_bound_check_copy(
                source_action_id=output["source_action_id"],
                source_device_file_id=output["device_file_id"],
                obtain_action_id=command.action_id,
                selection_id=selection["id"],
                owned=SimpleNamespace(connection=connection))
            if (bound is None
                    or bound.inspection.inspection_id != command.reuse_inspection_id
                    or bound.copy.target_file_id != command.source_intermediate_file_id):
                raise ConsistencyError("正式取回无权复用该来源提交的分段检查副本")
            inspection = self._required(
                connection, "segment_inspections", bound.inspection.inspection_id)
            member = self._required(connection, "segment_members", inspection["member_id"])
            segment_set = self._required(connection, "segment_sets", inspection["set_id"])
            self._required(connection, "file_copies", bound.copy.copy_id)
            original = self._required(connection, "device_files", output["device_file_id"])
            if (
                inspection["action_id"] != output["source_action_id"]
                or inspection["state"] != int(_SEGMENT_INSPECTION_STATE.COMPLETED)
                or inspection["source_device_file_id"] != output["device_file_id"]
                or member["file_id"] != output["device_file_id"]
                or member["set_id"] != segment_set["id"]
                or segment_set["action_id"] != output["source_action_id"]
                or original["source_action_id"] != output["source_action_id"]
                or original["role"] != int(_DEVICE_FILE_ROLE.ORIGINAL)
                or original["completion_state"] != int(_COMPLETION.COMPLETE)
                or source_file["owner_action_id"] != output["source_action_id"]
                or source_file["owner_delivery_id"] is not None
                or source_file["purpose"] != int(_PURPOSE.RECORDING_INPUT)
                or source_file["retention_state"] != int(_RETENTION.REQUIRED)
                or source_file["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)
                or source_file["size_bytes"] != bound.copy.source_size
                or source_file["sha256"] != bound.copy.target_sha256
                or source_file["relative_path"] != bound.copy.input_relative_path
            ):
                raise ConsistencyError("分段检查副本的保存责任与正式来源不一致")
        elif reuse_photo_cache:
            from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
            bound = read_bound_photo_cache(output_id=output["id"], obtain_action_id=command.action_id,
                                           selection_id=selection["id"], owned=SimpleNamespace(connection=connection))
            if (bound is None or bound.result_page_event_id != command.reuse_photo_page_event_id
                    or bound.start_result_event_id != command.reuse_photo_start_event_id
                    or bound.intermediate_file_id != command.source_intermediate_file_id):
                raise ConsistencyError("正式取回无权复用该照片原角色缓存")
            self._required(connection, "device_files", output["device_file_id"])
        elif command.source_device_file_id is not None:
            if (source_role is None or source_file["role"] != source_role
                    or output["source_action_id"] != source_file["source_action_id"]):
                raise ConsistencyError("设备产物与源文件角色或归属不一致")
            if source_file["completion_state"] != int(_COMPLETION.COMPLETE):
                raise ConsistencyError("已登记设备产物必须保留文件完成事实")
        elif (
            source_file["owner_action_id"] != output["source_action_id"]
            or source_file["owner_delivery_id"] is not None
            or source_file["purpose"] != int(_host_source_purpose(output["kind"], source_action["type"]))
            or source_file["retention_state"] != int(_RETENTION.PROMOTED)
            or source_file["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)
            or source_file["size_bytes"] is None
        ):
            raise ConsistencyError("主机产物必须关联来源动作已提升的完整角色文件")
        if command.source_intermediate_file_id is not None and not reuse_device_cache:
            try:
                validate_relative_file_path(
                    FilePurpose[_host_source_purpose(output["kind"], source_action["type"]).name], command.source_intermediate_file_id,
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
        from camctl.outputs.cleanup_group import cleanup_group
        group_reads = _ProductReads(scope.connection, self._state)
        for member_id in cleanup_group(group_reads, output["id"]):
            if member_id == output["id"]:
                continue
            member_phase = _output_cleanup_phase(scope.connection, member_id)
            if member_phase == _OUTPUT_CLEANUP_STATUS.COMPLETED:
                return self._reject_item(scope, _OUTPUT_UNAVAILABLE_CODE,
                                         {**details, "availability": "cleaned"})
            if member_phase is not None:
                return self._reject_item(scope, _OUTPUT_CLEANUP_STARTED_CODE, details)
        if (self._command.reuse_inspection_id is not None
                or self._command.reuse_photo_page_event_id is not None
                or self._command.reuse_photo_start_event_id is not None):
            if availability == _AVAILABILITY.CLEANED:
                details["availability"] = availability.name.lower()
                return self._reject_item(scope, _OUTPUT_UNAVAILABLE_CODE, details)
            if availability == _AVAILABILITY.RESTRICTED:
                return self._reject_item(scope, _OUTPUT_CLEANUP_STARTED_CODE, details)
            return None
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
        return _output_cleanup_phase(connection, output_id)

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
            product_read_scope=self._product_read_scope,
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
                "cleanup_item_id": None, "obtain_item_id": None, "availability_check_id": None,
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
                "inspection_id": command.inspection_id,
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
                "read_end_event_id": None,
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
        allocation = scope.allocate(1)
        identity = (command.binding_result.current_identity if command.binding_result is not None else None)
        event = _envelope(allocation.first_event_id, allocation.txn_id,
                          _READ_PERMISSION_EVENT, _REJECT_REASON, (row,), command.occurred_at,
                          evidence={"read_current_identity": dict(identity)} if identity is not None else None)
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
                error_code=error_code, error_details=details,
            ),
        )

    def _wait_final(self, error_code: int) -> CommandPlan:
        item = self._state["obtain_items"].get(self._command.item_id)
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
                error_code=error_code,
                error_details=_decoded(item["error_details_json"]) if item is not None else {},
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
            if command.processing_id is not None:
                copy = self._find_one(connection, "file_copies", "processing_id = ?", (command.processing_id,))
                target = self._find_one(connection, "intermediate_files", "owner_action_id = ? AND purpose = ?",
                                        (command.action_id, int(_PURPOSE.RECORDING_INPUT)))
                internal_run = self._find_one(connection, "operation_runs",
                    f"action_id = ? AND kind = {int(_RUN_KIND.READ_FILE)}", (command.action_id,))
            else:
                copy = self._find_one(connection, "file_copies", "inspection_id = ?", (command.inspection_id,))
                target = (self._find_one(connection, "intermediate_files", "id = ?",
                                          (copy["target_file_id"],)) if copy is not None else None)
                internal_run = None
            purpose = FilePurpose.RECORDING_INPUT
            if copy is None and target is None and internal_run is None:
                if (command.processing_id is not None and
                        _processing_requires_existing_input(self._state["recording_processing"][command.processing_id])):
                    raise ConsistencyError("原片工具处理已经开始，但原输入准备责任缺失")
                return None
        if copy is None or target is None:
            raise ConsistencyError("已有读取准备责任缺少拷贝或目标文件")
        if (
            copy["delivery_id"] != delivery_id or copy["processing_id"] != command.processing_id
            or copy.get("inspection_id") != command.inspection_id
            or copy["source_device_file_id"] != command.source_device_file_id
            or copy["source_intermediate_file_id"] != command.source_intermediate_file_id
            or copy["target_file_id"] != target["id"]
            or (copy["source_size"] != source_file["size_bytes"]
                and not _deferred_photo_source(connection, source_file))
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
                    "cleanup_item_id": None, "obtain_item_id": None, "availability_check_id": None, "query_purpose": None, "session_key": None}
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
        if ((not json_equal(current_copy["source_size"], source_snapshot["size_bytes"])
                and not (source_snapshot["size_bytes"] is None
                         and _deferred_photo_source(connection, source)))
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
                or after["error_code"] not in (_OUTPUT_UNAVAILABLE_CODE, _OUTPUT_CLEANUP_STARTED_CODE,
                    item_error_id("obtain_items", "device_binding_unavailable"))):
            raise ConsistencyError("原事务不是已选中产物的建档前拒绝")
        _, source, output, device = self._load_inputs(connection)
        details = self._binding_failure(source)
        if after["error_code"] == item_error_id("obtain_items", "device_binding_unavailable"):
            if details is None or not json_equal(after["error_details_json"], details):
                raise TransactionError("原读取绑定失败与重送核对输入不同")
            expected_identity = None if command.binding_result is None else command.binding_result.current_identity
            if not json_equal(event["body"].get("evidence", {}).get("read_current_identity"), expected_identity):
                raise TransactionError("原读取绑定失败的当前机身与重送输入不同")
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
    if copy.get("inspection_id") is not None:
        facts = row_facts(connection, "segment_inspections", copy["inspection_id"])
        if facts is None:
            raise ConsistencyError(
                f"机会责任关联记录缺失: segment_inspections#{copy['inspection_id']}")
        state.setdefault("segment_inspections", {})[copy["inspection_id"]] = facts
        owners[("file_copies", copy["id"])] = ("action", facts["action_id"])
        return
    raise ConsistencyError("拷贝缺少交付、处理或分段检查归属")


def _copy_action_id(connection, copy, state) -> int:
    """按三种读取责任取得拥有动作，并缓存固定父行。"""
    if copy["delivery_id"] is not None:
        parent_table, parent_id = "deliveries", copy["delivery_id"]
    elif copy["processing_id"] is not None:
        parent_table, parent_id = "recording_processing", copy["processing_id"]
    elif copy.get("inspection_id") is not None:
        parent_table, parent_id = "segment_inspections", copy["inspection_id"]
    else:
        raise ConsistencyError("拷贝缺少交付、处理或分段检查归属")
    parent = state.setdefault(parent_table, {}).get(parent_id)
    if parent is None:
        parent = row_facts(connection, parent_table, parent_id)
        if parent is None:
            raise ConsistencyError(f"拷贝责任父行缺失: {parent_table}#{parent_id}")
        state[parent_table][parent_id] = parent
    return parent["action_id"]


def _canceled_binding_processing_settled(connection, copy, run, action):
    """取消已生效的处理副本只按原绑定读取结果释放机会。"""
    error = run.get("error_json")
    if (copy.get("processing_id") is None or action.get("cancel_requested") != 1
            or action.get("status") not in (2, 6) or run.get("status") != int(_RUN_STATUS.FAILED)
            or not isinstance(error, Mapping) or error.get("code") != "device_binding_unavailable"):
        return None
    from .binding_read_cancel import load_saved_binding_read_failure
    load_saved_binding_read_failure(connection, action["id"], copy["id"], error)
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND (t.status=1 OR t.result_json IS NULL) LIMIT 1", (action["id"],),
    )) as cursor:
        return cursor.fetchone() is None


class _SlotChangeCommand:
    """相机读取机会的授予或释放事务命令。

    使用已建档拷贝和原责任，不创建替代记录。授予在写事务内核对
    当前持有者、资格及候选排序后保存归属；释放核对原责任和实际
    读取及重试结束，不能凭迟到通知清空新归属。
    """

    _TABLES = (
        "file_copies", "device_files", "operation_runs", "operation_attempts",
        "actions", "deliveries", "recording_processing", "segment_inspections",
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
            return self._reuse(saved, connection)
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
            "SELECT id FROM operation_runs WHERE responsibility_key = ? LIMIT 2",
            (f"read/{copy_id}",),
        )) as cursor:
            identities = cursor.fetchall()
        if len(identities) != 1:
            raise ConsistencyError("拷贝的 READ_FILE 流程缺失或重复")
        return self._required(connection, "operation_runs", identities[0][0])

    def _load_owner_action(self, connection, copy) -> dict:
        action_id = _copy_action_id(connection, copy, self._state)
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
        holder = self._device_holder(scope.connection, device_id)
        slot = copy["slot_device_id"]
        if slot == device_id:
            return self._decision(SlotOutcome.HELD)
        if slot is not None:
            raise ConsistencyError("拷贝的读取机会不属于原来源设备")
        if copy["read_end_event_id"] is not None:
            return self._decision(SlotOutcome.FINISHED)
        if run["status"] in _RUN_TERMINAL or not _read_owner_eligible(action):
            return self._decision(SlotOutcome.FINISHED)
        source = self._state["device_files"][copy["source_device_file_id"]]
        observer = self._state["actions"][source["observer_action_id"]]
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        identity = None
        if observer["driver_id"] == DRIVER_ID:
            from .physical_alias import source_physical_alias_conflict, source_physical_identity
            origin = self._state["actions"][source["source_action_id"]]
            if source_physical_alias_conflict(scope.connection, source, observer, origin):
                return self._decision(SlotOutcome.WAIT, "physical_identity_conflict")
            identity = source_physical_identity(scope.connection, source, observer, origin)
            from .capture_selection import read_capture_priority
            if self._request.capture_selection is None:
                return self._decision(SlotOutcome.WAIT, "capture_selection_unavailable")
            if read_capture_priority(scope.connection, self._request.capture_selection, scope.max_event_id,
                                     device_id=device_id, identity=identity,
                                     occurred_at=self._request.occurred_at) is not None:
                return self._decision(SlotOutcome.WAIT, "capture_priority")
        from .preparation import PreparationReads
        if PreparationReads(scope.connection).availability_in_flight(device_id, identity):
            return self._decision(SlotOutcome.WAIT, "device_busy")
        if not _read_is_due(action, self._request.occurred_at):
            return self._decision(SlotOutcome.WAIT, "not_due")
        retrying_source_fallback = (
            run["retry_wait_required"] == 1
            and self._is_source_fallback_retry(scope.connection, copy, run))
        if run["retry_wait_required"] == 1 and not retrying_source_fallback:
            return self._decision(SlotOutcome.WAIT, "retry_wait")
        from camctl.scheduling.resources import current_preparation_holder, device_start_in_flight
        if current_preparation_holder(scope.connection, device_id) is not None:
            return self._decision(SlotOutcome.WAIT, "preparation_held")
        if device_start_in_flight(scope.connection, device_id):
            return self._decision(SlotOutcome.WAIT, "device_busy")
        if holder is not None:
            return self._decision(SlotOutcome.WAIT, "device_busy")
        eligible, reason = self._first_candidate(
            scope.connection, device_id,
            retry_wait_copy_id=copy["id"] if retrying_source_fallback else None,
        )
        if not eligible:
            return self._decision(SlotOutcome.WAIT, reason)
        return self._save(
            scope, copy, None, device_id, SlotOutcome.GRANTED,
            plan_read_scope=(PlanReadScope(action["plan_id"], 1)
                             if retrying_source_fallback else None),
        )

    def _is_source_fallback_retry(self, connection, copy, run) -> bool:
        """只为有原始 SOURCE_FALLBACK 历史的失败本地读取续接机会。"""
        self._load_attempts(connection, run["id"])
        attempt = _source_fallback_retry_attempt(
            copy, run, self._state["operation_attempts"], connection)
        return attempt is not None

    def _device_holder(self, connection, device_id: str) -> int | None:
        return device_holder(connection, device_id)

    def _first_candidate(
            self, connection, device_id: str, *, retry_wait_copy_id=None,
    ) -> tuple[bool, str]:
        first = first_eligible_read(
            connection, device_id, self._request.occurred_at,
            retry_wait_copy_id=retry_wait_copy_id)
        if first == self._request.copy_id:
            return True, ""
        return False, "predecessor" if first is not None else "source_not_ready"

    # ---- 释放 ----

    def _release(self, scope, copy, device_id: str, run) -> CommandPlan:
        if self._request.capture_selection is not None:
            raise TransactionError("释放读取机会不能携带拍摄选择申请")
        slot = copy["slot_device_id"]
        if slot is None:
            return self._decision(SlotOutcome.ALREADY_RELEASED)
        if slot != device_id:
            raise ConsistencyError("拷贝的读取机会不属于原来源设备")
        if (copy.get("inspection_id") is not None and run["status"] == int(_RUN_STATUS.PENDING)
                and run["attempts_used"] == 0 and run["retry_wait_required"] == 0):
            inspection = self._state["segment_inspections"][copy["inspection_id"]]
            action = self._load_owner_action(scope.connection, copy)
            if (run["kind"] != int(_RUN_KIND.READ_FILE) or run["copy_id"] != copy["id"]
                    or run["action_id"] != action["id"] or run["error_json"] is not None):
                raise ConsistencyError("分段检查的原读取流程身份或初始状态不符")
            from .retry_wait_reads import RetryWaitReads
            if (inspection["state"] in (int(_SEGMENT_INSPECTION_STATE.COMPLETED),
                                         int(_SEGMENT_INSPECTION_STATE.FAILED),
                                         int(_SEGMENT_INSPECTION_STATE.UNCONFIRMED))
                    and type(action["cancel_requested"]) is int and action["cancel_requested"] == 1
                    and RetryWaitReads(scope.connection).no_attempts(run["id"])):
                return self._cancel_unstarted_inspection(scope, copy, device_id, run, action)
        self._load_attempts(scope.connection, run["id"])
        if copy.get("processing_id") is not None:
            action = self._load_owner_action(scope.connection, copy)
            if _canceled_binding_processing_settled(scope.connection, copy, run, action) is False:
                return self._decision(SlotOutcome.WAIT, "action_active")
        if any(attempt["status"] == int(_ATTEMPT_STATUS.RUNNING)
               for attempt in self._state["operation_attempts"].values()):
            return self._decision(SlotOutcome.WAIT, "read_active")
        if run["retry_wait_required"] == 1:
            return self._decision(SlotOutcome.WAIT, "retry_wait")
        if run["status"] not in _RUN_TERMINAL and (
                copy["source_size"] is None or copy["committed_bytes"] < copy["source_size"]):
            return self._decision(SlotOutcome.WAIT, "still_reading")
        return self._save(scope, copy, device_id, None, SlotOutcome.RELEASED)

    def _cancel_unstarted_inspection(self, scope, copy, device_id, run, action):
        allocation = scope.allocate(2)
        run_row = _update("operation_runs", run["id"],
            {"status": run["status"], "retry_wait_required": 0, "error_json": run["error_json"]},
            {"status": int(_RUN_STATUS.CANCELED), "retry_wait_required": 0, "error_json": None})
        slot_row = _update("file_copies", copy["id"], {"slot_device_id": device_id}, {"slot_device_id": None})
        self._owners[("operation_runs", run["id"])] = ("action", action["id"])
        return CommandPlan(events=(
            _envelope(allocation.first_event_id, allocation.txn_id, _OPERATION_CONFIGURED_EVENT, 3,
                      (run_row,), self._request.occurred_at),
            _envelope(allocation.first_event_id + 1, allocation.txn_id, _COPY_CHANGED_EVENT, _SLOT_REASON,
                      (slot_row,), self._request.occurred_at)),
            owners=self._owners, state_rows=self._state, result=SlotDecision(outcome=SlotOutcome.RELEASED))

    # ---- 事件与出口 ----

    def _save(self, scope, copy, before_slot, after_slot, outcome, *,
              plan_read_scope=None) -> CommandPlan:
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
            ({"capture_selection": self._request.capture_selection.document()}
             if after_slot is not None and self._request.capture_selection is not None else None),
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            read_coverage=self._coverage,
            result=SlotDecision(outcome=outcome),
            plan_read_scope=plan_read_scope,
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

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        """原键恢复首次机会变化响应；不重新判定当前资格或归属。"""
        if len(saved) == 2:
            first = saved[0]
            rows = first["body"]["rows"]
            run = self._load_run(connection, self._request.copy_id)
            if (self._direction != _SLOT_RELEASE or (first["type"], first["reason"]) != (_OPERATION_CONFIGURED_EVENT, 3)
                    or first["occurred_at"] != self._request.occurred_at or len(rows) != 1
                    or rows[0]["table"] != "operation_runs" or rows[0]["id"] != run["id"]
                    or rows[0]["before"].get("exists") is not True or rows[0]["after"].get("exists") is not True
                    or rows[0]["before"]["values"] != {"status": int(_RUN_STATUS.PENDING)}
                    or rows[0]["after"]["values"] != {"status": int(_RUN_STATUS.CANCELED)}):
                raise TransactionError("原分段检查取消读取的流程事实不符")
            saved = saved[1:]
        if len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT or saved[0]["reason"] != _SLOT_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为机会变化重送")
        event = saved[0]
        if event["occurred_at"] != self._request.occurred_at:
            raise TransactionError("机会变化的事实时刻与原事务不同")
        selection = self._request.capture_selection
        expected = None if selection is None else selection.document()
        if not json_equal(event["body"].get("evidence", {}).get("capture_selection"), expected):
            raise TransactionError("机会变化的拍摄选择输入与原事务不同")
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


def _deferred_photo_source(connection, source) -> bool:
    """识别原动作固定的按需照片来源，同时核对定位身份。"""
    if source is None or source.get("size_bytes") is not None or source.get("sha256") is not None:
        return False
    action = row_facts(connection, "actions", source["source_action_id"])
    if action is None or read_action_spec(action).get("photo_source_policy") != "deferred":
        return False
    from camctl.devices.drivers.hasselblad.files import DeferredCameraFileIdentity, camera_file_identity_from_source
    identity = camera_file_identity_from_source(source["identity_key"], source["locator_json"])
    return isinstance(identity, DeferredCameraFileIdentity)


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
        if _deferred_photo_source(connection, source):
            size = copy["source_size"]
            if not is_json_integer(size) or size <= 0 or copy["source_sha256"] is not None:
                raise ConsistencyError("按需照片读取缺少本次实际下载长度")
            if support is not SourceChecksumSupport.UNSUPPORTED:
                raise ConsistencyError("按需照片读取不能使用固定的源摘要")
            return size, support
    elif copy["source_intermediate_file_id"] is not None:
        source = row_facts(
            connection, "intermediate_files", copy["source_intermediate_file_id"])
        if source is None:
            raise ConsistencyError(
                f"拷贝的主机源文件缺失: {copy['source_intermediate_file_id']}")
        size = source["size_bytes"]
        support = SourceChecksumSupport.SUPPORTED
        if source["sha256"] is None and source["purpose"] in (
                int(_PURPOSE.CAPTURE_ORIGINAL), int(_PURPOSE.CAPTURE_PREVIEW)):
            origin = row_facts(connection, "actions", source["owner_action_id"])
            if origin is not None and read_action_spec(origin).get("photo_source_policy") == "deferred":
                support = SourceChecksumSupport.UNSUPPORTED
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

    _TABLES = ("file_copies", "deliveries", "recording_processing", "segment_inspections", "actions")

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
        action_id = _copy_action_id(connection, copy, self._state)
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
        action_id = _copy_action_id(connection, copy, self._state)
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


class _PhotoReadStartCommand(_CopyCompletionTablesMixin):
    """记录原 READ 已打开的照片下载长度，重读前持久化目标重置。"""

    _TABLES = ("file_copies", "deliveries", "recording_processing", "segment_inspections", "actions",
               "device_files", "operation_runs", "operation_attempts")

    def __init__(self, request, key):
        from camctl.outputs.photo_read import PhotoReadStart
        if not isinstance(request, PhotoReadStart):
            raise TypeError("照片读取长度申请必须使用 PhotoReadStart")
        self._request, self._key = request, key
        self._owners = {}
        self._state = {table: {} for table in self._TABLES}

    def plan(self, scope):
        from camctl.devices.drivers.hasselblad.files import DeferredCameraFileIdentity, camera_file_identity_from_source
        from camctl.outputs.photo_read import decide_photo_read_start

        connection, request = scope.connection, self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            if (len(saved) != 1 or saved[0]["type"] != _COPY_CHANGED_EVENT
                    or saved[0]["reason"] != _PHOTO_READ_START_REASON
                    or saved[0]["occurred_at"] != request.occurred_at
                    or not json_equal(saved[0]["body"].get("evidence", {}).get("photo_read_start"), request.evidence())):
                raise TransactionError("照片读取长度重送与原完整申请不一致")
            return self._decision(None, read_only=True)
        copy = self._load_copy(connection, request.copy_id)
        action = self._owner_action(connection, copy)
        source = None if copy["source_device_file_id"] is None else row_facts(
            connection, "device_files", copy["source_device_file_id"])
        origin = None if source is None else row_facts(connection, "actions", source["source_action_id"])
        run = row_facts(connection, "operation_runs", request.ticket.run_id)
        with closing(connection.execute(
                "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (request.ticket.run_id, request.ticket.attempt_id))) as cursor:
            attempt_row = cursor.fetchone()
        attempt = None if attempt_row is None else row_facts(connection, "operation_attempts", attempt_row[0])
        if (copy["delivery_id"] is None or copy["source_intermediate_file_id"] is not None
                or source is None or origin is None or run is None or attempt is None
                or read_action_spec(origin).get("photo_source_policy") != "deferred"
                or run["kind"] != int(_RUN_KIND.READ_FILE) or run["status"] != int(_RUN_STATUS.ACTIVE)
                or run["copy_id"] != request.copy_id
                or run["action_id"] != action["id"] or run["attempts_used"] != request.ticket.attempt_id
                or run["responsibility_key"] != request.ticket.responsibility_key
                or attempt["status"] != int(_ATTEMPT_STATUS.RUNNING) or attempt["result_event_id"] is not None
                or attempt["intent_event_id"] is None or attempt["copy_round"] != copy["round"]
                or copy["slot_device_id"] != origin["device_id"] or copy["read_end_event_id"] is not None
                or copy["verification_state"] != int(_VERIFICATION.NOT_PERFORMED)
                or copy["source_sha256"] is not None or copy["target_sha256"] is not None):
            raise ConsistencyError("照片读取长度不属于持槽的原按需读取尝试")
        identity = camera_file_identity_from_source(source["identity_key"], source["locator_json"])
        if not isinstance(identity, DeferredCameraFileIdentity):
            raise ConsistencyError("照片读取长度不属于按需来源身份")
        for table, value in (("device_files", source), ("actions", origin),
                             ("operation_runs", run), ("operation_attempts", attempt)):
            self._state[table][value["id"]] = value
        decision = decide_photo_read_start(previous_size=copy["source_size"],
            committed_bytes=copy["committed_bytes"],
            reset_pending=copy["reset_state"] == int(_RESET_STATE.RESET_PENDING),
            current_size=request.size_bytes)
        before = {name: copy[name] for name in ("source_size", "committed_bytes", "reset_state")}
        after = {"source_size": decision.source_size, "committed_bytes": decision.committed_bytes,
                 "reset_state": int(_RESET_STATE.RESET_PENDING if decision.reset_pending else _RESET_STATE.READY)}
        if json_equal(before, after):
            return self._decision(None, read_only=True)
        row = _update("file_copies", request.copy_id, before, after)
        allocation = scope.allocate(1)
        event = _envelope(allocation.first_event_id, allocation.txn_id, _COPY_CHANGED_EVENT,
            _PHOTO_READ_START_REASON, (row,), request.occurred_at,
            evidence={"photo_read_start": request.evidence()})
        return CommandPlan(events=(event,), owners=self._owners, state_rows=self._state, result=None)


class _IntegritySaveCommand(_CopyCompletionTablesMixin):
    """校验结果保存事务命令。

    全部字节可靠保存后保存源端与主机端摘要的比较结论；获取失败
    保存诊断，不降级为不支持。取消或动作不在执行时不新增校验
    事实。不改动轮次、进度与预算。
    """

    _TABLES = ("file_copies", "deliveries", "recording_processing", "segment_inspections", "actions")

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
        if (copy["read_end_event_id"] is not None and copy["slot_device_id"] is not None
                and command.source_sha256 is not None):
            before["slot_device_id"] = copy["slot_device_id"]
            after["slot_device_id"] = None
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

    _TABLES = ("file_copies", "deliveries", "recording_processing", "segment_inspections", "actions")

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
            "read_end_event_id": copy["read_end_event_id"],
        }
        after = {
            "round": round_now + 1,
            "recopies_used": used_now + 1,
            "committed_bytes": 0,
            "reset_state": int(_RESET_STATE.RESET_PENDING),
            "verification_state": int(_VERIFICATION.NOT_PERFORMED),
            "target_sha256": None,
            "read_end_event_id": None,
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
        if copy["read_end_event_id"] is not None and copy["slot_device_id"] is not None:
            before["slot_device_id"] = copy["slot_device_id"]
            after["slot_device_id"] = None
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


def delivery_read_succeeded(connection, delivery_id):
    """交付发布以前，原读取流程及尝试必须已有可靠成功结果。"""
    from .retry_wait_reads import RetryWaitReads

    with closing(connection.execute(
        "SELECT r.id,r.status,r.attempts_used,r.retry_wait_required,"
        " t.status,t.result_json,t.result_event_id FROM file_copies c"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " LEFT JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=r.attempts_used"
        " WHERE c.delivery_id=? LIMIT 2", (int(_RUN_KIND.READ_FILE), delivery_id),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        return False
    run_id, status, used, waiting, attempt_status, result, event_id = rows[0]
    return (status == int(_RUN_STATUS.SUCCEEDED) and used > 0 and waiting == 0
            and attempt_status == int(_ATTEMPT_STATUS.SUCCEEDED)
            and result is not None and event_id is not None
            and RetryWaitReads(connection).no_unsettled_attempts(run_id))


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
        publishing = delivery["status"] == int(_DELIVERY_STATUS.PUBLISHING)
        if not publishing and delivery["status"] != int(_DELIVERY_STATUS.PREPARED):
            raise ConsistencyError(
                "交付未处于准备完成状态，不能保存发布意图:"
                f" {delivery['status']!r}")
        if publishing and delivery["publication_intent_event_id"] is None:
            raise ConsistencyError("发布中的交付缺少原发布意图")
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
        if not delivery_read_succeeded(connection, delivery["id"]):
            return self._decision(
                IntentSaveOutcome(IntentDisposition.SKIPPED, reason="read_result_unconfirmed"),
                read_only=True)
        if publishing:
            return self._decision(
                IntentSaveOutcome(IntentDisposition.ALREADY), read_only=True)
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
        action = self._owner_action(connection, delivery)
        if action["cancel_requested"]:
            return self._decision(FailureSaveOutcome(FailureSaveDisposition.CANCELED), read_only=True)
        if not _read_owner_eligible(action):
            return self._decision(FailureSaveOutcome(FailureSaveDisposition.RETIRED), read_only=True)
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


@dataclass(frozen=True)
class FailReadDelivery:
    """一次读取终局失败交付的输入；预算及校验事实从原记录读取。"""

    delivery_id: int
    occurred_at: int
    checksum_mismatch: bool = False
    source_verification_failed: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.delivery_id)
        UtcMicros(self.occurred_at)
        if (type(self.checksum_mismatch) is not bool
                or type(self.source_verification_failed) is not bool):
            raise TypeError("读取终局失败分区必须使用布尔值")
        if self.checksum_mismatch and self.source_verification_failed:
            raise ValueError("摘要不一致与源摘要查询失败不能同时成立")


@dataclass(frozen=True)
class FailReadBinding:
    """原读取已实际结束后，原设备异常导致独立交付无法继续准备。"""

    copy_id: int
    source_device_file_id: int
    binding_result: BindingResult
    occurred_at: int
    read_result_key: OperationKey | None = None

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        ObjectId(self.source_device_file_id)
        UtcMicros(self.occurred_at)
        if self.read_result_key is not None:
            OperationKey(self.read_result_key)
        if not isinstance(self.binding_result, BindingResult):
            raise TypeError("读取绑定失败必须携带 BindingResult")
        if binding_failure_details(self.binding_result) is None:
            raise ValueError("匹配绑定不能作为读取绑定失败")


@dataclass(frozen=True)
class ReadSourceFallback:
    """一次本地检查副本读取失败后的原源切换申请。"""

    copy_id: int
    failed_attempt_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        ObjectId(self.failed_attempt_id)
        UtcMicros(self.occurred_at)


class ReadSourceFallbackDisposition(Enum):
    """正式取回检查副本回退到原设备文件的处理结果。"""

    SWITCHED = "switched"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ReadSourceFallbackOutcome:
    disposition: ReadSourceFallbackDisposition


def read_source_fallback_key(copy_id: int) -> OperationKey:
    """为一份交付拷贝生成跨重启稳定的唯一换源操作身份。"""
    ObjectId(copy_id)
    identity = hashlib.sha256(
        f"camctl.output.read-source-fallback.v1/{copy_id}".encode("ascii")
    ).hexdigest()[:32]
    return OperationKey(identity)


class _ReadSourceFallbackCommand:
    """在原 READ_FILE 预算未耗尽时把同一交付拷贝单向换回原设备源。"""

    def __init__(self, request: ReadSourceFallback, key: OperationKey, owned):
        if not isinstance(request, ReadSourceFallback):
            raise TypeError("读取换源申请必须使用 ReadSourceFallback")
        self._request = request
        self._key = key
        self._owned = owned
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)

        request = self._request
        copy = row_facts(connection, "file_copies", request.copy_id)
        if copy is None:
            raise ConsistencyError(f"正式取回拷贝不存在: {request.copy_id}")
        self._state.setdefault("file_copies", {})[copy["id"]] = copy
        _copy_owner_of(connection, copy, self._state, self._owners)
        if (copy["delivery_id"] is None or copy["processing_id"] is not None
                or copy.get("inspection_id") is not None):
            raise TransactionError("读取换源只适用于正式取回交付拷贝")
        if copy["source_device_file_id"] is not None:
            return self._decision(ReadSourceFallbackDisposition.BLOCKED)
        if copy["source_intermediate_file_id"] is None:
            raise ConsistencyError("正式取回拷贝缺少当前读取源")

        delivery = self._required(connection, "deliveries", copy["delivery_id"])
        items = self._find_rows(
            connection, "obtain_items", "delivery_id=?", (delivery["id"],))
        if len(items) != 1:
            raise ConsistencyError("读取换源要求交付唯一关联原取回项")
        item = self._required(connection, "obtain_items", int(items[0][0]))
        output = self._required(connection, "outputs", delivery["output_id"])
        selection = self._required(
            connection, "obtain_source_selections", item["selection_id"])
        dependency = self._required(
            connection, "action_dependencies", selection["dependency_id"])
        action = self._required(connection, "actions", delivery["action_id"])
        source_action = self._required(connection, "actions", int(output["source_action_id"]))
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        photo = (source_action["driver_id"] == DRIVER_ID
                 and source_action["type"] == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
                 and source_action["execution_spec_json"].get("output_storage") == "device")

        if (item["delivery_id"] != delivery["id"]
                or item["output_id"] != output["id"]
                or item["source_dependency"] != 1
                or delivery["action_id"] != action["id"]
                or dependency["action_id"] != action["id"]
                or dependency["depends_on_action_id"] != output["source_action_id"]
                or output["kind"] not in ((int(_OUTPUT_KIND.ORIGINAL), int(_OUTPUT_KIND.PREVIEW))
                                          if photo else (int(_OUTPUT_KIND.ORIGINAL),))
                or output["device_file_id"] is None
                or output["intermediate_file_id"] is not None):
            raise ConsistencyError("读取换源的交付、固定项、来源或原片身份不一致")

        current_file_id = int(copy["source_intermediate_file_id"])
        input_file = self._required(
            connection, "intermediate_files", current_file_id)
        source = self._required(connection, "device_files", int(output["device_file_id"]))
        if photo:
            from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
            bound = read_bound_photo_cache(output_id=output["id"], obtain_action_id=action["id"],
                                           selection_id=selection["id"], owned=self._owned)
            expected_role = (int(_DEVICE_FILE_ROLE.ORIGINAL) if output["kind"] == int(_OUTPUT_KIND.ORIGINAL)
                             else int(_DEVICE_FILE_ROLE.PREVIEW))
            if (bound is None or bound.intermediate_file_id != current_file_id
                    or source["source_action_id"] != output["source_action_id"]
                    or source["role"] != expected_role or source["completion_state"] != int(_COMPLETION.COMPLETE)
                    or input_file["owner_action_id"] != output["source_action_id"]
                    or input_file["size_bytes"] != copy["source_size"]
                    or input_file["sha256"] != copy["source_sha256"]):
                raise ConsistencyError("读取换源没有同一照片角色的原缓存与相机来源")
        else:
            self._verify_inspection_fallback(connection, copy, output, input_file, source, source_action)

        run_rows = self._find_rows(
            connection, "operation_runs", "copy_id=? AND kind=?",
            (copy["id"], int(_RUN_KIND.READ_FILE)))
        if len(run_rows) != 1:
            raise ConsistencyError("读取换源的正式取回拷贝缺少唯一 READ_FILE 流程")
        run = self._required(connection, "operation_runs", int(run_rows[0][0]))
        attempts = self._find_rows(
            connection, "operation_attempts", "run_id=? ORDER BY attempt_no DESC LIMIT 1",
            (run["id"],))
        if len(attempts) != 1:
            raise ConsistencyError("读取换源缺少原本地读取失败尝试")
        attempt = self._required(
            connection, "operation_attempts", int(attempts[0][0]))
        if (attempt["id"] != request.failed_attempt_id
                or attempt["run_id"] != run["id"]
                or attempt["attempt_no"] != run["attempts_used"]
                or attempt["status"] != int(_ATTEMPT_STATUS.FAILED)
                or attempt["result_json"] is None
                or not isinstance(attempt["error_json"], Mapping)
                or attempt["error_json"].get("code") != "local_read_failed"
                or attempt["error_json"].get("stage") != "source_read"
                or not isinstance(attempt["error_json"].get("details"), Mapping)
                or attempt["result_event_id"] is None):
            raise ConsistencyError("读取换源没有可确认的原本地读取失败结果")
        details = attempt["error_json"]["details"]
        if details.get("source_failed") is not True and (
                not photo or details.get("photo_cache_checksum_mismatch") is not True
                or copy["reset_state"] != int(_RESET_STATE.READY)
                or _photo_cache_recopy_proof(connection, copy, attempt, require_reset=True) is None):
            raise ConsistencyError("读取换源缺少原照片缓存摘要不符及重置事实")
        if not _copy_round_chain_proven(
                connection, copy["id"], attempt["copy_round"], copy["round"],
                attempt["intent_event_id"], attempt["result_event_id"],
                copy["source_size"]):
            raise ConsistencyError("读取换源的当前拷贝轮次不在原 READ_FILE 尝试事件链内")
        with closing(connection.execute(
            "SELECT occurred_at FROM history_events WHERE id=?",
            (attempt["result_event_id"],),
        )) as cursor:
            result_event = cursor.fetchone()
        if result_event is None or result_event[0] != request.occurred_at:
            raise TransactionError("读取换源时间必须固定为原本地失败结果时间")

        if (run["responsibility_key"] != f"read/{copy['id']}"
                or run["action_id"] != action["id"]
                or run["delivery_id"] != delivery["id"]
                or run["status"] != int(_RUN_STATUS.ACTIVE)
                or run["retry_wait_required"] != 1
                or run["attempts_used"] >= run["max_attempts_used"]):
            return self._decision(ReadSourceFallbackDisposition.BLOCKED)
        if not _read_owner_eligible(action):
            return self._decision(ReadSourceFallbackDisposition.BLOCKED)

        if (not self._output_is_available(connection, output)
                or source["presence_state"] != int(_PRESENCE.PRESENT)):
            return self._decision(ReadSourceFallbackDisposition.BLOCKED)

        before = {"source_device_file_id": copy["source_device_file_id"],
                  "source_intermediate_file_id": copy["source_intermediate_file_id"]}
        after = {"source_device_file_id": source["id"], "source_intermediate_file_id": None}
        if copy["read_end_event_id"] is not None:
            before["read_end_event_id"] = copy["read_end_event_id"]
            after["read_end_event_id"] = None
        row = _update("file_copies", copy["id"], before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _COPY_CHANGED_EVENT, _COPY_SOURCE_FALLBACK_REASON, (row,),
            request.occurred_at, evidence={"attempt_id": attempt["id"]},
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ReadSourceFallbackOutcome(ReadSourceFallbackDisposition.SWITCHED),
            plan_read_scope=PlanReadScope(action["plan_id"], 1),
        )

    def _verify_inspection_fallback(self, connection, copy, output, input_file, source, source_action):
        current_file_id = input_file["id"]
        check_copies = self._find_rows(
            connection, "file_copies", "target_file_id=?", (current_file_id,))
        if len(check_copies) != 1:
            raise ConsistencyError("读取换源的主机来源不是唯一检查副本")
        check_copy = self._required(
            connection, "file_copies", int(check_copies[0][0]))
        if check_copy.get("inspection_id") is None:
            raise ConsistencyError("读取换源的主机来源不是检查副本")
        inspection = self._required(
            connection, "segment_inspections", int(check_copy["inspection_id"]))
        from camctl.persistence.repositories.segment_inspections import (
            read_inspection_for_member,
        )

        bound = read_inspection_for_member(
            int(inspection["action_id"]), int(inspection["member_id"]), self._owned)
        if (bound.inspection is None or bound.copy is None
                or bound.inspection.inspection_id != inspection["id"]
                or bound.copy.copy_id != check_copy["id"]
                or bound.copy.target_file_id != current_file_id
                or bound.copy.source_device_file_id != output["device_file_id"]
                or bound.copy.inspection_state != int(_SEGMENT_INSPECTION_STATE.COMPLETED)
                or bound.copy.verification_state not in (
                    int(_VERIFICATION.MATCHED),
                    int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE))
                or bound.copy.retention_state != int(_RETENTION.REQUIRED)
                or bound.copy.cleanup_state != int(_FILE_CLEANUP.NOT_NEEDED)):
            raise ConsistencyError("读取换源没有完整、已校验且仍受保留保护的检查副本事实")

        from camctl.devices.tasks import CaptureResultPolicy

        if (source["source_action_id"] != output["source_action_id"]
                or source["role"] != int(_DEVICE_FILE_ROLE.ORIGINAL)
                or source["completion_state"] != int(_COMPLETION.COMPLETE)
                or source_action["type"] not in (
                    int(_ACTION_TYPE.CAMERA_RECORD),
                    int(_ACTION_TYPE.CAMERA_TIMELAPSE))
                or source_action["execution_spec_json"].get("result_policy")
                != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
                or input_file["owner_action_id"] != output["source_action_id"]
                or input_file["owner_delivery_id"] is not None
                or input_file["purpose"] != int(_PURPOSE.RECORDING_INPUT)
                or input_file["retention_state"] != int(_RETENTION.REQUIRED)
                or input_file["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)
                or input_file["relative_path"] != bound.copy.input_relative_path
                or input_file["size_bytes"] != bound.copy.source_size
                or input_file["sha256"] != bound.copy.target_sha256
                or bound.copy.source_size != copy["source_size"]):
            raise ConsistencyError("读取换源的设备文件不是检查副本所属完整原片")


    def _required(self, connection, table, identity):
        facts = row_facts(connection, table, identity)
        if facts is None:
            raise ConsistencyError(f"读取换源关联事实缺失: {table}#{identity}")
        self._state.setdefault(table, {})[identity] = facts
        return facts

    def _find_rows(self, connection, table, condition, parameters):
        with closing(connection.execute(
            f"SELECT id FROM {table} WHERE {condition}", parameters,
        )) as cursor:
            return cursor.fetchall()

    def _output_is_available(self, connection, output):
        phase = _output_cleanup_phase(connection, output["id"])
        availability = output["availability"]
        if phase is not None:
            expected = (_AVAILABILITY.CLEANED
                        if phase == _OUTPUT_CLEANUP_STATUS.COMPLETED
                        else _AVAILABILITY.RESTRICTED)
            if (availability != int(expected)
                    or output["cleanup_status"] != int(phase)):
                raise ConsistencyError("读取换源时产物清理投影与有效限制不一致")
            return False
        if availability in (int(_AVAILABILITY.CLEANED), int(_AVAILABILITY.RESTRICTED)):
            raise ConsistencyError("读取换源时产物清理投影缺少有效限制事实")
        if output["cleanup_status"] not in (
                int(_OUTPUT_CLEANUP_STATUS.NOT_REQUESTED),
                int(_OUTPUT_CLEANUP_STATUS.CANCELED)):
            raise ConsistencyError("读取换源时产物清理终态与限制状态不一致")
        return availability == int(_AVAILABILITY.AVAILABLE)

    def _decision(self, disposition):
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ReadSourceFallbackOutcome(disposition),
        )

    def _reuse(self, saved):
        request = self._request
        if (len(saved) != 1
                or saved[0]["type"] != _COPY_CHANGED_EVENT
                or saved[0]["reason"] != _COPY_SOURCE_FALLBACK_REASON
                or saved[0]["occurred_at"] != request.occurred_at
                or saved[0]["body"].get("evidence") != {
                    "attempt_id": request.failed_attempt_id}):
            raise TransactionError("读取换源原键已经用于其他事实")
        rows = saved[0]["body"].get("rows")
        if (not isinstance(rows, list) or len(rows) != 1
                or rows[0].get("table") != "file_copies"
                or rows[0].get("id") != request.copy_id):
            raise TransactionError("原读取换源的拷贝身份与请求不同")
        before, after = rows[0].get("before", {}).get("values"), rows[0].get(
            "after", {}).get("values")
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or set(before) - {"read_end_event_id"} != {"source_device_file_id", "source_intermediate_file_id"}
                or set(after) != set(before)
                or ("read_end_event_id" in after and after["read_end_event_id"] is not None)
                or before["source_device_file_id"] is not None
                or before["source_intermediate_file_id"] is None
                or after["source_device_file_id"] is None
                or after["source_intermediate_file_id"] is not None):
            raise TransactionError("原读取换源不是本地检查副本到设备原片的单向切换")
        return self._decision(ReadSourceFallbackDisposition.SWITCHED)


@dataclass(frozen=True)
class FailCleanupBinding:
    """原清理调用已结束后的逐项绑定失败，固定适用未完成责任。"""

    item_id: int
    source_device_file_id: int
    binding_result: BindingResult
    responsibility_keys: tuple[str, ...]
    occurred_at: int
    canceled: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        ObjectId(self.source_device_file_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.binding_result, BindingResult):
            raise TypeError("清理绑定失败必须携带 BindingResult")
        if binding_failure_details(self.binding_result) is None:
            raise ValueError("匹配绑定不能作为清理绑定失败")
        if (not isinstance(self.responsibility_keys, tuple)
                or len(set(self.responsibility_keys)) != len(self.responsibility_keys)
                or any(key not in (f"delete/{self.item_id}", f"exists/{self.item_id}")
                       for key in self.responsibility_keys)):
            raise ValueError("清理绑定失败只拥有原成员的删除与查询责任")
        if not isinstance(self.canceled, bool):
            raise TypeError("取消分支必须是布尔值")


def _load_binding_facts(connection, file_id, result, state) -> dict:
    """从原观察者与来源动作核对稳定设备身份，保留完整关联事实。"""
    def required(table, identity):
        facts = row_facts(connection, table, identity)
        if facts is None:
            raise ConsistencyError(f"绑定失败的原记录缺失: {table}#{identity}")
        state.setdefault(table, {})[identity] = facts
        return facts

    source = required("device_files", file_id)
    observer = required("actions", source["observer_action_id"])
    origin = required("actions", source["source_action_id"])
    binding = (observer["device_id"], observer["driver_id"])
    if (binding != (origin["device_id"], origin["driver_id"])
            or binding != (result.binding.device_id, result.binding.driver_id)):
        raise TransactionError("绑定失败输入与原文件观察者或来源身份不同")
    details = binding_failure_details(result)
    if details is None:
        raise TransactionError("匹配绑定不能保存为失败")
    validate_error_details("device_binding_unavailable", details)
    if details["reason"] == "mismatch" and details.get("actual_driver_id") == binding[1]:
        raise TransactionError("驱动不一致必须采用不同的实际驱动")
    return details


class _ReadBindingFailCommand(_DeliveryPublicationMixin):
    """交付、原读取流程、源保护和机会的同事务绑定失败收场。"""

    _TABLES = (*_DeliveryPublicationMixin._TABLES,
               "obtain_items", "obtain_source_selections", "action_dependencies",
               "outputs", "device_files", "operation_runs", "operation_attempts")

    def __init__(self, command: FailReadBinding, key: OperationKey) -> None:
        self._command, self._key = command, key
        self._reset_state()

    def _facts(self, connection):
        command = self._command
        copy = row_facts(connection, "file_copies", command.copy_id)
        if (copy is None or copy["delivery_id"] is None
                or (copy["source_device_file_id"] != command.source_device_file_id
                    and not (copy["source_device_file_id"] is None and copy["source_intermediate_file_id"] is not None))):
            raise TransactionError("读取绑定失败必须指向原设备交付或其照片缓存")
        delivery, loaded_copy = self._load_delivery(connection, copy["delivery_id"])
        if loaded_copy["id"] != command.copy_id:
            raise ConsistencyError("交付的唯一拷贝与失败输入不同")
        copy = loaded_copy
        action = self._owner_action(connection, delivery)
        self._load_target(connection, copy)
        details = _load_binding_facts(connection, command.source_device_file_id,
                                     command.binding_result, self._state)
        with closing(connection.execute(
            "SELECT id FROM obtain_items WHERE delivery_id=?", (delivery["id"],),
        )) as cursor:
            item_ids = cursor.fetchall()
        if len(item_ids) != 1:
            raise ConsistencyError("失败交付必须属于唯一取回项")
        item = row_facts(connection, "obtain_items", item_ids[0][0])
        output = row_facts(connection, "outputs", delivery["output_id"])
        if (item is None or output is None or item["output_id"] != output["id"]
                or output["device_file_id"] != command.source_device_file_id):
            raise ConsistencyError("失败交付、取回项、产物及原文件关联不一致")
        self._state["obtain_items"][item["id"]] = item
        self._state["outputs"][output["id"]] = output
        if copy["source_device_file_id"] is None:
            cache = row_facts(connection, "intermediate_files", copy["source_intermediate_file_id"])
            if cache is None:
                raise ConsistencyError("照片缓存绑定失败缺少原缓存文件")
            self._state["intermediate_files"][cache["id"]] = cache
        selection = row_facts(connection, "obtain_source_selections", item["selection_id"])
        if selection is None:
            raise ConsistencyError("原取回项的来源选择缺失")
        dependency = row_facts(connection, "action_dependencies", selection["dependency_id"])
        if (dependency is None or dependency["action_id"] != action["id"]
                or dependency["depends_on_action_id"] != output["source_action_id"]):
            raise ConsistencyError("原取回项的选择、依赖及来源归属不一致")
        self._state["obtain_source_selections"][selection["id"]] = selection
        self._state["action_dependencies"][dependency["id"]] = dependency
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE copy_id=?", (copy["id"],),
        )) as cursor:
            run_ids = cursor.fetchall()
        if len(run_ids) != 1:
            raise ConsistencyError("原交付拷贝必须具有唯一读取流程")
        run = row_facts(connection, "operation_runs", run_ids[0][0])
        if (run is None or run["kind"] != int(_RUN_KIND.READ_FILE)
                or run["action_id"] != action["id"]
                or run["responsibility_key"] != f"read/{copy['id']}"):
            raise ConsistencyError("原读取流程身份与交付拷贝不一致")
        self._state["operation_runs"][run["id"]] = run
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY id", (run["id"],),
        )) as cursor:
            attempt_ids = cursor.fetchall()
        for (identity,) in attempt_ids:
            attempt = row_facts(connection, "operation_attempts", identity)
            if attempt is None:
                raise ConsistencyError("原读取尝试记录缺失")
            self._state["operation_attempts"][identity] = attempt
        self._coverage = ReadCoverage({
            ("operation_runs", "copy_id"): {copy["id"]},
            ("operation_attempts", "run_id"): {run["id"]}})
        self._owners.update({
            ("operation_runs", run["id"]): ("delivery", delivery["id"]),
            ("obtain_items", item["id"]): ("action", action["id"]),
            ("file_copies", copy["id"]): ("delivery", delivery["id"])})
        return copy, delivery, action, item, run, details

    def plan(self, scope) -> CommandPlan:
        saved = saved_transaction_events(scope.connection, self._key)
        facts = self._facts(scope.connection)
        copy, delivery, action, item, run, details = facts
        if self._command.read_result_key is not None:
            from .binding_read_cancel import saved_binding_read_failure
            saved_binding_read_failure(scope.connection, self._command.read_result_key, action["id"], copy["id"],
                {"code": "device_binding_unavailable", "stage": "execution", "details": details},
                self._command.binding_result.current_identity)
        if saved is not None:
            return self._reuse(scope, saved, facts)
        disposition = None
        if delivery["status"] in (6, 7, 8) or copy["verification_state"] in (3, 5):
            disposition = FailureSaveDisposition.ALREADY
        elif (self._command.read_result_key is not None and action["cancel_requested"]
              and action["status"] in (2, 6)):
            disposition = FailureSaveDisposition.RETIRED
        if copy["source_device_file_id"] is None:
            from camctl.outputs.photo_cache_reuse import verify_photo_cache_binding_failure
            historical = self._command.read_result_key is not None and disposition is not None
            before_event_id = scope.max_event_id + 1
            if historical:
                original = saved_transaction_events(scope.connection, self._command.read_result_key)
                if not original:
                    raise ConsistencyError("原绑定读取结果事务缺失")
                before_event_id = original[0]["event_id"]
            if verify_photo_cache_binding_failure(scope.connection, copy,
                    before_event_id=before_event_id, historical=historical) != self._command.source_device_file_id:
                raise TransactionError("照片缓存绑定失败改变原设备来源")
        if disposition is not None:
            return self._decision(FailureSaveOutcome(disposition), read_only=True)
        if details["reason"] == "physical_identity_mismatch":
            from .physical_alias import read_mismatch_can_finish, verify_source_identity_mismatch
            source = self._state["device_files"][self._command.source_device_file_id]
            observer = self._state["actions"][source["observer_action_id"]]
            origin = self._state["actions"][source["source_action_id"]]
            if not verify_source_identity_mismatch(scope.connection, source, observer, origin,
                                                   self._command.binding_result.current_identity, action_id=action["id"]):
                raise TransactionError("读取机身不符申请与固定来源一致")
            if not read_mismatch_can_finish(owner_eligible=_read_owner_eligible(action),
                    attempts=self._state["operation_attempts"].values()):
                return self._decision(FailureSaveOutcome(FailureSaveDisposition.RETIRED), read_only=True)
        if details["reason"] == "physical_identity_conflict":
            from .physical_alias import source_physical_alias_conflict
            source = self._state["device_files"][self._command.source_device_file_id]
            observer = self._state["actions"][source["observer_action_id"]]
            origin = self._state["actions"][source["source_action_id"]]
            if not source_physical_alias_conflict(scope.connection, source, observer, origin, action_id=action["id"]):
                return self._decision(FailureSaveOutcome(FailureSaveDisposition.RETIRED), read_only=True)
        if (delivery["status"] not in (1, 2) or not _read_owner_eligible(action)
                or not self._run_can_close(run, details) or item["source_dependency"] != 1):
            raise TransactionError("绑定失败不能重开原交付或读取终态")
        if any(attempt["status"] == 1 or attempt["result_json"] is None
               for attempt in self._state["operation_attempts"].values()):
            raise TransactionError("原读取没有可靠结束结果，不能解除保护或机会")
        specs = self._specs(facts)
        allocation = scope.allocate(len(specs))
        current = self._command.binding_result.current_identity
        return CommandPlan(events=tuple(_envelope(
            allocation.first_event_id + index, allocation.txn_id, kind, reason, (row,), self._command.occurred_at,
            evidence=({"read_current_identity": dict(current)} if index == 0 and current is not None else None))
            for index, (kind, reason, row) in enumerate(specs)), owners=self._owners,
            state_rows=self._state, read_coverage=self._coverage,
            result=FailureSaveOutcome(FailureSaveDisposition.SAVED))

    def _specs(self, facts):
        """原业务事实决定完整变化组，未变化列由 _update 统一省略。"""
        copy, delivery, _action, item, run, details = facts
        failure = {"code": "device_binding_unavailable", "stage": "execution", "details": details}
        specs = [
            (_DELIVERY_CHANGED_EVENT, _DELIVERY_FAIL_REASON, _update(
                "deliveries", delivery["id"], {"status": delivery["status"], "error_json": delivery["error_json"]},
                {"status": 6, "error_json": failure})),
        ]
        if run["status"] in (1, 2):
            specs.append((10, 3, _update("operation_runs", run["id"],
                {"status": run["status"], "retry_wait_required": run["retry_wait_required"], "error_json": run["error_json"]},
                {"status": 4, "retry_wait_required": 0, "error_json": failure})))
        specs.append((_READ_PERMISSION_EVENT, _RELEASE_REASON, _update(
            "obtain_items", item["id"], {"source_dependency": 1}, {"source_dependency": 0})))
        if copy["slot_device_id"] is not None:
            specs.append((_COPY_CHANGED_EVENT, _SLOT_REASON, _update(
                "file_copies", copy["id"], {"slot_device_id": copy["slot_device_id"]}, {"slot_device_id": None})))
        return specs

    @staticmethod
    def _run_can_close(run, details):
        return run["status"] in (1, 2) or (run["status"] == 4 and run["retry_wait_required"] == 0
            and run["error_json"] == {"code": "device_binding_unavailable", "stage": "execution", "details": details})

    def _reuse(self, scope, saved, facts) -> CommandPlan:
        """倒放至原 F 前的完整 H，重建并核实原完整 READ 绑定组。"""
        copy, delivery, action, item, run, details = facts
        transaction = saved[0]["transaction"]
        prior = read_transaction_range(scope.connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        boundary = HistoryBoundary(prior.txn_id, prior.last_event_id) if prior else HistoryBoundary(0, 0)
        current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)
        action_owner = ("action", action["id"])
        delivery_owner = ("delivery", delivery["id"])
        for table, rows in self._state.items():
            for identity, row in rows.items():
                if table in ("deliveries", "file_copies", "operation_runs", "operation_attempts"):
                    owner = delivery_owner
                elif table == "actions":
                    owner = ("action", identity)
                elif table == "device_files":
                    owner = ("device_file", identity)
                elif table == "intermediate_files":
                    owner = ("intermediate_file", identity)
                elif table == "outputs":
                    owner = ("output", identity)
                elif table in ("obtain_items", "obtain_source_selections", "action_dependencies"):
                    owner = action_owner
                else:
                    raise ConsistencyError("原读取绑定组包含未定义历史归属的关联表")
                row.update(read_row_values_at_boundary(scope.connection, owner=owner,
                    table=table, row_id=identity, columns=business_columns(table), current_values=row,
                    boundary=boundary, current_boundary=current))
        if (copy["delivery_id"] != delivery["id"]
                or (copy["source_device_file_id"] != self._command.source_device_file_id
                    and not (copy["source_device_file_id"] is None and copy["source_intermediate_file_id"] is not None))
                or delivery["action_id"] != action["id"] or item["delivery_id"] != delivery["id"]
                or item["output_id"] != delivery["output_id"] or run["copy_id"] != copy["id"]
                or run["action_id"] != action["id"] or run["kind"] != int(_RUN_KIND.READ_FILE)
                or run["responsibility_key"] != f"read/{copy['id']}"):
            raise TransactionError("原 H 的读取绑定组归属和原责任不符")
        source = self._state["device_files"][self._command.source_device_file_id]
        if copy["source_device_file_id"] is None:
            from camctl.outputs.photo_cache_reuse import verify_photo_cache_binding_failure
            if verify_photo_cache_binding_failure(scope.connection, copy, before_event_id=transaction.first_event_id,
                                                   historical=True) != source["id"]:
                raise TransactionError("原绑定失败改变照片缓存的原设备来源")
        observer = self._state["actions"][source["observer_action_id"]]
        origin = self._state["actions"][source["source_action_id"]]
        output = self._state["outputs"][delivery["output_id"]]
        selection = self._state["obtain_source_selections"][item["selection_id"]]
        dependency = self._state["action_dependencies"][selection["dependency_id"]]
        target = self._state["intermediate_files"][copy["target_file_id"]]
        binding = self._command.binding_result.binding
        if ((observer["device_id"], observer["driver_id"]) != (origin["device_id"], origin["driver_id"])
                or (origin["device_id"], origin["driver_id"]) != (binding.device_id, binding.driver_id)
                or output["device_file_id"] != source["id"] or output["source_action_id"] != origin["id"]
                or dependency["action_id"] != action["id"] or dependency["depends_on_action_id"] != origin["id"]
                or target["owner_delivery_id"] != delivery["id"] or target["owner_action_id"] is not None
                or target["purpose"] != int(_PURPOSE.DELIVERY_COPY)
                or (copy["slot_device_id"] is not None and copy["slot_device_id"] != binding.device_id)):
            raise TransactionError("原 H 的来源、目标、依赖或机会与原绑定归属不同")
        if (delivery["status"] not in (1, 2) or not _read_owner_eligible(action)
                or not self._run_can_close(run, details) or item["source_dependency"] != 1
                or copy["verification_state"] in (3, 5)):
            raise TransactionError("原 H 不属于读取绑定失败的未完成分区")
        if any(attempt["run_id"] != run["id"] or attempt["status"] == 1 or attempt["result_json"] is None
               for attempt in self._state["operation_attempts"].values()):
            raise TransactionError("原 H 的实际读取没有可靠结束结果")
        specs = self._specs(facts)
        if len(saved) != len(specs):
            raise TransactionError("原读取绑定失败的完整事件组不同")
        if not json_equal(saved[0]["body"].get("evidence", {}).get("read_current_identity"),
                          self._command.binding_result.current_identity):
            raise TransactionError("原读取绑定失败的当前机身与重送输入不同")
        for index, (event, (event_type, reason, row)) in enumerate(zip(saved, specs)):
            identity = self._command.binding_result.current_identity
            evidence = {"read_current_identity": dict(identity)} if index == 0 and identity is not None else {}
            body = {"reason": reason, "evidence": evidence, "rows": [{
                "table": row.table, "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)}}]}
            if (event["type"] != event_type or event["reason"] != reason
                    or event["occurred_at"] != self._command.occurred_at
                    or not json_equal(event["body"], body)):
                raise TransactionError("原读取绑定失败的完整正文与原 H 或重送输入不同")
        return self._decision(FailureSaveOutcome(FailureSaveDisposition.ALREADY), read_only=True)


class _ReadDeliveryFailCommand(_DeliveryPublicationMixin):
    """读取终局失败的交付保存事务命令。

    读取流程已按 FAILED 终态收场且没有在途尝试时，把交付结束为
    FAILED 并保存原读取错误。读取耗尽、摘要不一致和源摘要查询失败
    分别从其可靠流程与校验事实核实。
    """

    _TABLES = (
        "deliveries", "file_copies", "actions", "intermediate_files",
        "operation_runs", "operation_attempts", "obtain_items", "device_files",
        "outputs", "obtain_source_selections", "action_dependencies",
    )

    def __init__(self, command: FailReadDelivery, key: OperationKey) -> None:
        if not isinstance(command, FailReadDelivery):
            raise TypeError("读取耗尽失败申请必须使用 FailReadDelivery")
        self._command = command
        self._key = key
        self._reset_state()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved, connection)
        delivery, copy = self._load_delivery(connection, self._command.delivery_id)
        status = delivery["status"]
        if status == int(_DELIVERY_STATUS.FAILED):
            return self._decision(
                FailureSaveOutcome(FailureSaveDisposition.ALREADY),
                read_only=True)
        action = self._owner_action(connection, delivery)
        if (action.get("cancel_requested") == 1
                or status in (int(_DELIVERY_STATUS.CANCELED), int(_DELIVERY_STATUS.WITHDRAWN))):
            return self._decision(
                FailureSaveOutcome(FailureSaveDisposition.CANCELED), read_only=True)
        if status not in (
            int(_DELIVERY_STATUS.PENDING), int(_DELIVERY_STATUS.PREPARING),
        ):
            raise ConsistencyError(
                f"交付状态不属于读取失败的保存范围: {status!r}")
        run = self._load_read_run(connection, copy["id"])
        self._require_read_finished(connection, run)
        with closing(connection.execute(
            "SELECT id FROM obtain_items WHERE delivery_id=?", (delivery["id"],),
        )) as cursor:
            identities = cursor.fetchall()
        if len(identities) != 1:
            raise ConsistencyError("读取耗尽交付必须属于唯一取回项")
        item = row_facts(connection, "obtain_items", identities[0][0])
        if item is None or item["output_id"] != delivery["output_id"] or item["source_dependency"] != 1:
            raise ConsistencyError("读取耗尽交付的原取回项及源依赖不一致")
        self._state["obtain_items"][item["id"]] = item
        self._owners[("obtain_items", item["id"])] = ("action", delivery["action_id"])
        self._owners[("file_copies", copy["id"])] = ("delivery", delivery["id"])
        output = row_facts(connection, "outputs", item["output_id"])
        selection = row_facts(connection, "obtain_source_selections", item["selection_id"])
        if output is None or selection is None:
            raise ConsistencyError("读取耗尽的原取回项缺少产物或来源选择")
        dependency = row_facts(connection, "action_dependencies", selection["dependency_id"])
        if (dependency is None or dependency["action_id"] != delivery["action_id"]
                or dependency["depends_on_action_id"] != output["source_action_id"]):
            raise ConsistencyError("读取耗尽交付的来源选择与原动作依赖不一致")
        self._state["outputs"][output["id"]] = output
        self._state["obtain_source_selections"][selection["id"]] = selection
        self._state["action_dependencies"][dependency["id"]] = dependency
        if copy["source_device_file_id"] is not None:
            source = row_facts(connection, "device_files", copy["source_device_file_id"])
            if source is None:
                raise ConsistencyError("读取耗尽缺少原来源文件")
            self._state["device_files"][source["id"]] = source
            for action_id in (source["observer_action_id"], source["source_action_id"]):
                action = row_facts(connection, "actions", action_id)
                if action is None:
                    raise ConsistencyError("读取耗尽缺少原来源动作")
                self._state["actions"][action_id] = action
        if self._command.source_verification_failed:
            verification_failed = int(_VERIFICATION.FAILED)
            if (copy["source_device_file_id"] is None
                    or copy["verification_state"] != verification_failed
                    or copy["committed_bytes"] != copy["source_size"]
                    or copy["source_sha256"] is not None
                    or copy["target_sha256"] is None
                    or copy["verification_error_json"] is None):
                raise ConsistencyError("源摘要查询失败必须沿用完整副本的已保存校验失败事实")
            verification_error = copy["verification_error_json"]
            if (not isinstance(verification_error, dict)
                    or not isinstance(verification_error.get("reason"), str)):
                raise ConsistencyError("源摘要查询失败缺少原校验错误")
            source = row_facts(connection, "device_files", copy["source_device_file_id"])
            if (source is None
                    or source["checksum_support"] != int(_FILE_CHECKSUM.SUPPORTED)):
                raise ConsistencyError("源摘要查询失败要求原设备源已确认支持摘要")
            code = "source_file_unconfirmed"
            details = {"output_id": str(output["id"])}
        elif self._command.checksum_mismatch:
            if (copy["verification_state"] != int(_VERIFICATION.MISMATCHED)
                    or copy["committed_bytes"] != copy["source_size"]
                    or copy["source_sha256"] is None or copy["target_sha256"] is None
                    or copy["source_sha256"] == copy["target_sha256"]
                    or copy["recopies_used"] < copy["max_recopies_used"]):
                raise ConsistencyError("摘要不一致失败必须核对可靠比较和本次重拷耗尽事实")
            code = "checksum_mismatch"
            details = {"max_recopies": copy["max_recopies_used"], "recopies_used": copy["recopies_used"]}
        else:
            code = "read_attempts_exhausted"
            details = {
                "max_read_attempts": int(run["max_attempts_used"]),
                "attempts_used": int(run["attempts_used"]),
            }
        if run["error_json"] is None or run["error_json"]["code"] != code:
            raise ConsistencyError("所属交付失败必须沿原读取流程的可靠最终错误")
        if (self._command.source_verification_failed
                and run["error_json"] != {
                    "code": code, "stage": registered_error(code)["stage"],
                    "details": details}):
            raise ConsistencyError("源摘要失败交付必须沿用原读取流程的完整错误")
        failure = DeliveryFailure(code=code, stage=registered_error(code)["stage"], details=details)
        row = _update(
            "deliveries", delivery["id"],
            {"status": status, "error_json": None},
            {"status": int(_DELIVERY_STATUS.FAILED),
             "error_json": failure.as_json()},
        )
        specs = [(_DELIVERY_CHANGED_EVENT, _DELIVERY_FAIL_REASON, (row,)),
                 (_READ_PERMISSION_EVENT, _RELEASE_REASON, (_update(
                     "obtain_items", item["id"], {"source_dependency": 1}, {"source_dependency": 0}),))]
        if copy["slot_device_id"] is not None:
            specs.append((_COPY_CHANGED_EVENT, _SLOT_REASON, (_update(
                "file_copies", copy["id"], {"slot_device_id": copy["slot_device_id"]},
                {"slot_device_id": None}),)))
        events = self._envelopes(
            scope, specs,
            self._command.occurred_at,
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            read_coverage=self._coverage,
            result=FailureSaveOutcome(FailureSaveDisposition.SAVED),
        )

    def _load_read_run(self, connection, copy_id: int) -> dict:
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (f"read/{copy_id}",),
        )) as cursor:
            identities = cursor.fetchall()
        if len(identities) != 1:
            raise ConsistencyError("交付拷贝的读取流程缺失或重复")
        run = row_facts(connection, "operation_runs", identities[0][0])
        if run is None:
            raise ConsistencyError(f"读取流程记录缺失: {identities[0][0]}")
        self._state["operation_runs"][run["id"]] = run
        self._coverage = ReadCoverage({
            ("operation_runs", "copy_id"): {copy_id}})
        self._owners[("operation_runs", run["id"])] = ("action", run["action_id"])
        return run

    def _require_read_finished(self, connection, run) -> None:
        """读取终局失败的保存前提：流程终态失败且无在途尝试。"""
        _RUN_STATUS = enum_for("operation_runs.status")
        if run["status"] != int(_RUN_STATUS.FAILED):
            raise ConsistencyError(
                f"读取流程未按失败终态收场: {run['status']!r}")
        from .retry_wait_reads import RetryWaitReads
        if not RetryWaitReads(connection).no_unsettled_attempts(run["id"]):
            raise ConsistencyError("读取仍有未保存结束结果的尝试，不能保存终局失败")

    def _reuse(self, saved: list[dict], connection) -> CommandPlan:
        """原键恢复首次读取终局失败响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        required = [(_DELIVERY_CHANGED_EVENT, _DELIVERY_FAIL_REASON),
                    (_READ_PERMISSION_EVENT, _RELEASE_REASON)]
        if types[:2] != required or types[2:] not in ([], [(_COPY_CHANGED_EVENT, _SLOT_REASON)]):
            raise TransactionError(
                "操作身份已用于其他阶段，不能作为读取耗尽失败重送")
        if any(event["occurred_at"] != self._command.occurred_at for event in saved):
            raise TransactionError("读取耗尽失败的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "deliveries" or rows[0]["id"] != self._command.delivery_id:
            raise TransactionError("读取耗尽失败的交付身份与原事务不同")
        expected_code = (
            "source_file_unconfirmed" if self._command.source_verification_failed
            else "checksum_mismatch" if self._command.checksum_mismatch
            else "read_attempts_exhausted")
        error = rows[0]["after"]["values"].get("error_json")
        if not isinstance(error, dict) or error.get("code") != expected_code:
            raise TransactionError("读取失败的所属错误与原事务不同")
        if self._command.source_verification_failed:
            current = row_facts(connection, "deliveries", self._command.delivery_id)
            if (current is None
                    or error.get("stage") != registered_error(expected_code)["stage"]
                    or error.get("details") != {"output_id": str(current["output_id"])}):
                raise TransactionError("源摘要失败的交付产物身份与原事务不同")
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
        """释放前核对源与目标两端没有未结束的读取尝试。"""
        running = _running_file_operations(connection, file["id"])
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

    def _saved_file_row(self, saved, scope, *, before_columns, after_values):
        """原键必须属于同一文件的完整更新，不采用当前可变状态。"""
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "intermediate_files"
                or rows[0]["id"] != self._request.file_id
                or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]
                or not set(rows[0]["before"]["values"]) <= set(before_columns)
                or set(rows[0]["before"]["values"]) != set(rows[0]["after"]["values"])):
            raise TransactionError("原清理事务的文件身份、阶段或保存输入不同")
        transaction = saved[0]["transaction"]
        current = row_facts(scope.connection, "intermediate_files", self._request.file_id)
        if current is None:
            raise ConsistencyError("原清理文件的可靠当前行缺失")
        original_after = read_row_values_at_boundary(
            scope.connection, owner=("intermediate_file", self._request.file_id),
            table="intermediate_files", row_id=self._request.file_id,
            columns=frozenset(before_columns), current_values=current,
            boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
        if not json_equal(original_after, after_values):
            raise TransactionError("原清理事务的文件身份、阶段或保存输入不同")
        # 历史仅记录真实变化；没有变化的错误空值也必须沿原 H 核实。
        return {**rows[0], "before": {"exists": True, "values": {
            **original_after, **rows[0]["before"]["values"]}},
            "after": {"exists": True, "values": original_after}}

    def _original_cursor(self, connection, first_event_id):
        """从原完整事务之前的不可变游标事实取得原位置。"""
        entity_type = load_enum_registry()["history_objects"]["runtime_state"]["id"]
        with closing(connection.execute(
            "SELECT e.id, e.transaction_id, e.event_type, e.event_version,"
            " e.occurred_at, e.clock_status, e.change_seq, e.body_json"
            " FROM entity_event_links l JOIN history_events e ON e.id = l.event_id"
            " WHERE l.entity_type = ? AND l.entity_id = 1 AND l.event_id < ?"
            " AND e.event_type = ? ORDER BY l.event_id DESC LIMIT 1",
            (entity_type, first_event_id, _CLEANUP_CURSOR_EVENT),
        )) as cursor:
            stored = cursor.fetchone()
        if stored is None:
            return None
        event = decode_event_row(stored)
        transaction = read_transaction_range(connection, event.transaction_id)
        if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
            raise ConsistencyError("原清理游标事件不属于可靠完整事务")
        if (event.reason != _CURSOR_CHECKED_REASON or len(event.rows) != 1
                or event.rows[0].table != "runtime_state" or event.rows[0].row_id != 1
                or not event.rows[0].before.exists or not event.rows[0].after.exists
                or set(event.rows[0].after.values) != {"cleanup_cursor_file_id"}):
            raise ConsistencyError("原清理游标事件的阶段或身份不符")
        return event.rows[0].after.values["cleanup_cursor_file_id"]


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
            return self._reuse(saved, scope)
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
        if table == "actions":
            from camctl.outputs.photo_cache_reuse import photo_cache_waiters_settled
            if not photo_cache_waiters_settled(file["id"], SimpleNamespace(connection=connection)):
                raise ConsistencyError("照片角色缓存仍被 H 时固定的取回使用")
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

    def _reuse(self, saved: list[dict], scope) -> CommandPlan:
        """原键恢复首次释放响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为释放重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("释放的事实时刻与原事务不同")
        row = self._saved_file_row(saved, scope,
            before_columns=("retention_state", "cleanup_state"),
            after_values={"retention_state": int(_RETENTION.RELEASABLE),
                          "cleanup_state": int(_FILE_CLEANUP.PENDING)})
        if (row["before"]["values"]["retention_state"] not in
                (int(_RETENTION.REQUIRED), int(_RETENTION.HANDED_OFF))
                or row["before"]["values"]["cleanup_state"] != int(_FILE_CLEANUP.NOT_NEEDED)):
            raise TransactionError("原事务不是保留释放阶段")
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
            return self._reuse(saved, scope)
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

    def _reuse(self, saved: list[dict], scope) -> CommandPlan:
        """原键恢复首次意图响应。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_INTERMEDIATE_FILE_EVENT, _CLEANUP_INTENT_REASON)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为清理意图重送")
        if saved[0]["occurred_at"] != self._request.occurred_at:
            raise TransactionError("清理意图的事实时刻与原事务不同")
        row = self._saved_file_row(saved, scope, before_columns=("cleanup_state",),
            after_values={"cleanup_state": int(_FILE_CLEANUP.RUNNING)})
        if row["before"]["values"]["cleanup_state"] not in (
                int(_FILE_CLEANUP.PENDING), int(_FILE_CLEANUP.FAILED), int(_FILE_CLEANUP.UNKNOWN)):
            raise TransactionError("原事务不是清理意图阶段")
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
            return self._reuse(saved, scope)
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
                evidence=({"cleanup_request": {
                    "advance_cursor": self._request.advance_cursor}}
                    if event_type == _INTERMEDIATE_FILE_EVENT else None),
            )
            for index, (event_type, reason, rows) in enumerate(specs)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=CleanupResultSaveOutcome(CleanupResultDisposition.SAVED),
        )

    def _reuse(self, saved: list[dict], scope) -> CommandPlan:
        """原键恢复首次结果响应；游标推进可选共存。"""
        types = [(event["type"], event["reason"]) for event in saved]
        expected = [
            (_INTERMEDIATE_FILE_EVENT, _CLEANUP_RESULT_REASON),
            (_CLEANUP_CURSOR_EVENT, _CURSOR_CHECKED_REASON),
        ]
        if types != expected and types != expected[:1]:
            raise TransactionError("操作身份已用于其他阶段，不能作为清理结果重送")
        if any(event["occurred_at"] != self._request.occurred_at for event in saved):
            raise TransactionError("清理结果的事实时刻与原事务不同")
        request = saved[0]["body"]["evidence"].get("cleanup_request")
        if (not isinstance(request, Mapping) or set(request) != {"advance_cursor"}
                or type(request["advance_cursor"]) is not bool):
            raise ConsistencyError("原清理结果缺少可靠的布尔申请输入")
        if request["advance_cursor"] is not self._request.advance_cursor:
            raise TransactionError("原清理结果的游标申请输入与重送不同")
        after = {"cleanup_state": (
            int(_FILE_CLEANUP.COMPLETED) if self._request.outcome is WorkFileOutcome.COMPLETED
            else int(_FILE_CLEANUP.FAILED)), "last_error_json": (
            None if self._request.error is None else self._request.error.as_json())}
        row = self._saved_file_row(saved, scope,
            before_columns=("cleanup_state", "last_error_json"), after_values=after)
        if row["before"]["values"]["cleanup_state"] not in (
                int(_FILE_CLEANUP.PENDING), int(_FILE_CLEANUP.RUNNING),
                int(_FILE_CLEANUP.FAILED), int(_FILE_CLEANUP.UNKNOWN)):
            raise TransactionError("原事务不是清理结果阶段")
        original_cursor = self._original_cursor(
            scope.connection, saved[0]["transaction"].first_event_id)
        cursor_changed = self._request.advance_cursor and original_cursor != self._request.file_id
        expected_types = expected if cursor_changed else expected[:1]
        if types != expected_types:
            raise TransactionError("原清理结果的完整游标责任集合与重送输入不同")
        if cursor_changed:
            rows = saved[1]["body"]["rows"]
            if (len(rows) != 1 or rows[0]["table"] != "runtime_state" or rows[0]["id"] != 1
                    or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]
                    or not json_equal(rows[0]["before"]["values"],
                                      {"cleanup_cursor_file_id": original_cursor})
                    or not json_equal(rows[0]["after"]["values"],
                                      {"cleanup_cursor_file_id": self._request.file_id})):
                raise TransactionError("原清理结果的游标身份、原位置或结果不同")
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
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "runtime_state" or rows[0]["id"] != 1
                or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]
                or set(rows[0]["before"]["values"]) != {"cleanup_cursor_file_id"}
                or not json_equal(rows[0]["after"]["values"],
                                  {"cleanup_cursor_file_id": self._request.file_id})):
            raise TransactionError("原游标事务的检查文件身份不同")
        return self._decision(
            CleanupCheckedOutcome(CleanupCheckedDisposition.ALREADY),
            read_only=True)


def _load_work_file_facts(owned: OwnedConnection, file_id: int) -> WorkFileFacts:
    """只读加载一份中间文件的清理判定事实。

    归属终态与操作停止都按当前可靠记录核实；记录缺失或不可解释
    属于状态库矛盾，不解释为归属活跃或已停止。
    """
    connection = owned.connection
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
        if owner_finished and file["purpose"] in (int(_PURPOSE.CAPTURE_ORIGINAL), int(_PURPOSE.CAPTURE_PREVIEW)):
            from camctl.outputs.photo_cache_reuse import photo_cache_waiters_settled
            owner_finished = photo_cache_waiters_settled(file_id, owned)
        if file["purpose"] == int(_PURPOSE.RECORDING_INPUT):
            with closing(connection.execute(
                "SELECT inspection_id FROM file_copies"
                " WHERE target_file_id=? AND inspection_id IS NOT NULL",
                (file_id,),
            )) as cursor:
                inspection_rows = cursor.fetchall()
            if len(inspection_rows) > 1:
                raise ConsistencyError(
                    f"检查副本输入对应多个检查责任: {file_id}")
            if inspection_rows:
                inspection_id = inspection_rows[0][0]
                inspection = row_facts(
                    connection, "segment_inspections", inspection_id)
                if inspection is None:
                    raise ConsistencyError(
                        f"检查副本责任缺失: {inspection_id}")
                member = row_facts(
                    connection, "segment_members", inspection["member_id"])
                if member is None:
                    raise ConsistencyError(
                        f"检查副本的分段成员缺失: {inspection['member_id']}")
                from camctl.persistence.repositories.segment_inspections import (
                    read_inspection_for_member,
                )
                from camctl.outputs.segment_copy_reuse import (
                    all_bound_waiters_terminal,
                )

                facts = read_inspection_for_member(
                    int(file["owner_action_id"]), int(member["id"]), owned)
                if (facts.inspection is None or facts.copy is None
                        or facts.inspection.inspection_id != inspection_id
                        or facts.copy.target_file_id != file_id
                        or facts.inspection.action_id != file["owner_action_id"]):
                    raise ConsistencyError(
                        f"检查副本与工作文件责任不一致: {file_id}")
                if facts.inspection.state == int(_SEGMENT_INSPECTION_STATE.PENDING):
                    owner_finished = False
                with closing(connection.execute(
                    "SELECT published_event_id FROM segment_sets WHERE id=?",
                    (facts.inspection.set_id,),
                )) as cursor:
                    set_row = cursor.fetchone()
                if set_row is None:
                    raise ConsistencyError(
                        f"检查副本分段集合缺失: {facts.inspection.set_id}")
                published_event_id = set_row[0]
                if published_event_id is not None:
                    owner_finished = owner_finished and all_bound_waiters_terminal(
                        int(file["owner_action_id"]), int(published_event_id), owned)
    operations_stopped = _running_file_operations(connection, file_id) == 0
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


def _running_file_operations(connection, file_id):
    with closing(connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id"
        " JOIN file_copies c ON r.copy_id=c.id"
        " WHERE (c.target_file_id=? OR c.source_intermediate_file_id=?) AND a.status=1",
        (file_id, file_id))) as cursor:
        return cursor.fetchone()[0]


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
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ObtainFinishResult:
    """取回完成登记的结果；取消优先时汇总字段不适用。"""

    action_status: int
    plan_status: int | None
    prepared: int | None
    failed: int | None
    disposition: ObtainFinishDisposition = ObtainFinishDisposition.SAVED


@dataclass(frozen=True)
class SettleCanceledObtain:
    """取消已生效取回的固定成员及一次有限收场时刻。"""

    action_id: int
    occurred_at: int
    item_ids: tuple[int, ...]
    publishing_staging: tuple[tuple[int, int, int], ...] = ()

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.item_ids, tuple):
            raise TypeError("取消取回的固定成员必须使用元组")
        for identity in self.item_ids:
            ObjectId(identity)
        if tuple(sorted(set(self.item_ids))) != self.item_ids:
            raise ValueError("取消取回的固定成员必须按身份升序且不重复")
        if not isinstance(self.publishing_staging, tuple):
            raise TypeError("发布中断的原副本身份必须使用元组")
        for member in self.publishing_staging:
            if not isinstance(member, tuple) or len(member) != 3:
                raise ValueError("发布中断身份必须包含交付、拷贝和目标文件")
            for identity in member:
                ObjectId(identity)
        if tuple(sorted(set(self.publishing_staging))) != self.publishing_staging:
            raise ValueError("发布中断身份必须按序且不重复")


@dataclass(frozen=True)
class CanceledObtainSettlement:
    """原持久化责任是否已收场，以及本次必要的首次文件责任。"""

    complete: bool
    file_ids: tuple[int, ...] = ()


class _SettleCanceledObtainCommand:
    """同事务保存取消结果、原读取流程、源保护与读取机会的结束。"""

    _TABLES = ("actions", "plans", "action_dependencies", "obtain_source_selections",
               "obtain_items", "deliveries", "file_copies", "intermediate_files",
               "operation_runs", "operation_attempts", "outputs", "device_files")

    def __init__(self, command: SettleCanceledObtain, key: OperationKey):
        if not isinstance(command, SettleCanceledObtain):
            raise TypeError("取消取回收场必须使用 SettleCanceledObtain")
        self._command, self._key = command, key
        self._state = {table: {} for table in self._TABLES}
        self._owners = {}
        self._ranges: dict = {}
        self._members = []

    def _load(self, connection, table, identity, owner=None):
        facts = row_facts(connection, table, identity)
        if facts is None:
            raise ConsistencyError(f"取消取回的原责任记录缺失: {table}#{identity}")
        self._state[table][identity] = facts
        if owner is not None:
            self._owners[(table, identity)] = owner
        return facts

    def _ids(self, connection, table, column, identity):
        with closing(connection.execute(
            f"SELECT id FROM {table} WHERE {column}=? ORDER BY id", (identity,),
        )) as cursor:
            return tuple(row[0] for row in cursor.fetchall())

    def _facts(self, connection):
        action_id = self._command.action_id
        action = self._load(connection, "actions", action_id, ("action", action_id))
        if action["type"] != _OBTAIN_TYPE:
            raise TransactionError("取消取回输入必须指向原取回动作")
        self._load(connection, "plans", action["plan_id"], ("plan", action["plan_id"]))
        for identity in self._ids(connection, "actions", "plan_id", action["plan_id"]):
            self._load(connection, "actions", identity, ("action", identity))
        member_ids = []
        for dependency_id in self._ids(connection, "action_dependencies", "action_id", action_id):
            self._load(connection, "action_dependencies", dependency_id, ("action", action_id))
            for selection_id in self._ids(connection, "obtain_source_selections", "dependency_id", dependency_id):
                self._load(connection, "obtain_source_selections", selection_id, ("action", action_id))
                for item_id in self._ids(connection, "obtain_items", "selection_id", selection_id):
                    item = self._load(connection, "obtain_items", item_id, ("action", action_id))
                    member_ids.append(item_id)
                    if item["delivery_id"] is None:
                        self._members.append((item_id, None, None, None))
                        continue
                    delivery_id = item["delivery_id"]
                    owner = ("delivery", delivery_id)
                    delivery = self._load(connection, "deliveries", delivery_id, owner)
                    if (delivery["action_id"] != action_id or delivery["output_id"] != item["output_id"]):
                        raise ConsistencyError("取消取回的原成员与交付归属不一致")
                    copies = self._ids(connection, "file_copies", "delivery_id", delivery_id)
                    if len(copies) != 1:
                        raise ConsistencyError("取消取回的原交付必须对应唯一拷贝")
                    copy = self._load(connection, "file_copies", copies[0], owner)
                    target = self._load(connection, "intermediate_files", copy["target_file_id"])
                    if target["owner_delivery_id"] != delivery_id:
                        raise ConsistencyError("取消取回的原副本与交付归属不一致")
                    output = self._load(connection, "outputs", delivery["output_id"])
                    dependency = self._state["action_dependencies"][dependency_id]
                    photo_cache = False
                    if (output["device_file_id"] is not None and copy["source_device_file_id"] is None
                            and copy["source_intermediate_file_id"] is not None):
                        from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
                        bound = read_bound_photo_cache(output_id=output["id"], obtain_action_id=action_id,
                            selection_id=selection_id, owned=SimpleNamespace(connection=connection),
                            allow_released=True, historical=True)
                        photo_cache = (bound is not None
                                       and bound.intermediate_file_id == copy["source_intermediate_file_id"])
                    if (dependency["depends_on_action_id"] != output["source_action_id"]
                            or (not photo_cache and (output["device_file_id"] != copy["source_device_file_id"]
                            or output["intermediate_file_id"] != copy["source_intermediate_file_id"]))):
                        raise ConsistencyError("取消取回的原来源、产物与副本关联不一致")
                    try:
                        validate_relative_file_path(_target_purpose_model(copy, target),
                                                    target["id"], target["relative_path"])
                    except PathRuleError as error:
                        raise ConsistencyError("取消取回的原副本路径不可定位") from error
                    if copy["source_device_file_id"] is not None:
                        source = self._load(connection, "device_files", copy["source_device_file_id"])
                        self._load(connection, "actions", source["observer_action_id"])
                        self._load(connection, "actions", source["source_action_id"])
                    else:
                        self._load(connection, "intermediate_files", copy["source_intermediate_file_id"])
                    run_ids = self._ids(connection, "operation_runs", "copy_id", copy["id"])
                    if len(run_ids) != 1:
                        raise ConsistencyError("取消取回的原拷贝必须具有唯一读取流程")
                    run = self._load(connection, "operation_runs", run_ids[0], owner)
                    if (run["kind"] != int(_RUN_KIND.READ_FILE) or run["action_id"] != action_id
                            or run["responsibility_key"] != f"read/{copy['id']}"):
                        raise ConsistencyError("取消取回的原读取身份与拷贝不一致")
                    for identity in self._ids(connection, "operation_attempts", "run_id", run["id"]):
                        self._load(connection, "operation_attempts", identity, owner)
                    self._ranges.setdefault(("operation_runs", "copy_id"), set()).add(copy["id"])
                    self._ranges.setdefault(("operation_attempts", "run_id"), set()).add(run["id"])
                    self._members.append((item_id, delivery_id, copy["id"], run["id"]))
        if tuple(sorted(member_ids)) != self._command.item_ids:
            raise TransactionError("取消取回的固定成员集合与原持久化集合不同")
        self._members.sort()
        return action

    def _decision(self, result):
        return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
            read_only=True, read_coverage=ReadCoverage(self._ranges), result=result)

    def _templates(self):
        action = self._state["actions"][self._command.action_id]
        if action["status"] in _ACTION_TERMINAL:
            return (), CanceledObtainSettlement(True)
        if not action["cancel_requested"]:
            raise TransactionError("取回取消尚未生效，不能结束原责任")
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError("取消取回的目标不在执行中")
        # 持久化原意图没有结束结果时保留保护；本命令没有实际
        # 调用的拥有者，也不从会话重启推出读取已停止。
        if any(attempt["status"] == 1 or attempt["result_json"] is None
               for attempt in self._state["operation_attempts"].values()):
            return (), CanceledObtainSettlement(False)
        publishing = {delivery_id: (copy_id, self._state["file_copies"][copy_id]["target_file_id"])
                      for _, delivery_id, copy_id, _ in self._members
                      if delivery_id is not None and self._state["deliveries"][delivery_id]["status"]
                      == int(_DELIVERY_STATUS.PUBLISHING)}
        observed = {delivery: (copy, file) for delivery, copy, file in self._command.publishing_staging}
        if len(observed) != len(self._command.publishing_staging):
            raise ConsistencyError("发布中断观察不能重复交付身份")
        if any(publishing.get(delivery) != pair for delivery, pair in observed.items()):
            raise ConsistencyError("发布中断观察不属于原交付、拷贝及目标文件")
        if any(self._state["deliveries"][delivery]["publication_intent_event_id"] is None
               for delivery in observed):
            raise ConsistencyError("发布中断观察缺少原发布意图")
        unknown = {delivery for delivery in publishing
                   if self._state["deliveries"][delivery]["withdrawal_state"] == int(_WITHDRAWAL.UNKNOWN)}
        if set(observed) & unknown:
            raise ConsistencyError("同一交付不能同时确认 staging 并保持位置未知")
        if set(publishing) != set(observed) | unknown:
            return (), CanceledObtainSettlement(False)
        templates, first_files = [], []
        for item_id, delivery_id, copy_id, run_id in self._members:
            item = self._state["obtain_items"][item_id]
            if delivery_id is None:
                if item["status"] in (int(_ITEM_STATUS.UNRESOLVED), int(_ITEM_STATUS.SELECTED)):
                    templates.append((_READ_PERMISSION_EVENT, 5, (_update("obtain_items", item_id,
                        {"status": item["status"]}, {"status": int(_ITEM_STATUS.CANCELED)}),)))
                elif item["status"] not in (int(_ITEM_STATUS.FAILED), int(_ITEM_STATUS.CANCELED)):
                    raise ConsistencyError("未建档取回项的原状态不可解释")
                continue
            delivery = self._state["deliveries"][delivery_id]
            copy, run = self._state["file_copies"][copy_id], self._state["operation_runs"][run_id]
            if delivery["status"] in (int(_DELIVERY_STATUS.PUBLISHED), int(_DELIVERY_STATUS.WITHDRAWN)):
                continue
            new_responsibility = delivery["status"] not in (int(_DELIVERY_STATUS.FAILED), int(_DELIVERY_STATUS.CANCELED))
            if new_responsibility:
                templates.append((_DELIVERY_CHANGED_EVENT, 6, (_update("deliveries", delivery_id,
                    {"status": delivery["status"]}, {"status": int(_DELIVERY_STATUS.CANCELED)}),)))
            if run["status"] not in _RUN_TERMINAL:
                templates.append((_OPERATION_CONFIGURED_EVENT, 3, (_update("operation_runs", run_id,
                    {"status": run["status"], "retry_wait_required": run["retry_wait_required"], "error_json": run["error_json"]},
                    {"status": int(_RUN_STATUS.CANCELED), "retry_wait_required": 0, "error_json": None}),)))
                new_responsibility = True
            elif run["retry_wait_required"] != 0:
                raise ConsistencyError("原终态读取流程仍有重试责任")
            if item["source_dependency"]:
                templates.append((_READ_PERMISSION_EVENT, _RELEASE_REASON, (_update("obtain_items", item_id,
                    {"source_dependency": 1}, {"source_dependency": 0}),)))
                new_responsibility = True
            if copy["slot_device_id"] is not None:
                templates.append((_COPY_CHANGED_EVENT, _SLOT_REASON, (_update("file_copies", copy_id,
                    {"slot_device_id": copy["slot_device_id"]}, {"slot_device_id": None}),)))
                new_responsibility = True
            if new_responsibility:
                first_files.append(copy["target_file_id"])
        templates.append((_ACTION_FINISHED_EVENT, 4, (_update("actions", action["id"],
            {"status": action["status"]}, {"status": int(_ACTION_STATUS.CANCELED)}),)))
        plan = self._state["plans"][action["plan_id"]]
        siblings = {identity: facts for identity, facts in self._state["actions"].items()
                    if facts["plan_id"] == action["plan_id"]}
        if plan["status"] in (1, 2) and plan_complete(siblings, action["id"]):
            templates.append((_PLAN_STATUS_EVENT, 2, (_update("plans", plan["id"],
                {"status": plan["status"]}, {"status": _PLAN_COMPLETE}),)))
        return tuple(templates), CanceledObtainSettlement(True, tuple(sorted(first_files)))

    def _canceled_publication_evidence(self, kind, reason, rows):
        if kind != _DELIVERY_CHANGED_EVENT or reason != 6:
            return {}
        identities = {delivery: (copy, file)
                      for delivery, copy, file in self._command.publishing_staging}
        if len(rows) != 1 or rows[0].table != "deliveries":
            raise ConsistencyError("取消交付事件必须对应一份原交付")
        delivery_id = rows[0].row_id
        pair = identities.get(delivery_id)
        location = "staging"
        if pair is None:
            delivery = self._state["deliveries"][delivery_id]
            if (delivery["status"] != int(_DELIVERY_STATUS.PUBLISHING)
                    or delivery["withdrawal_state"] != int(_WITHDRAWAL.UNKNOWN)):
                return {}
            matching = [(copy_id, self._state["file_copies"][copy_id]["target_file_id"])
                        for _, item_delivery, copy_id, _ in self._members if item_delivery == delivery_id]
            if len(matching) != 1:
                raise ConsistencyError("未知交接结论缺少唯一原拷贝")
            pair, location = matching[0], "unknown"
        return {"canceled_publication": {"delivery_id": delivery_id,
            "copy_id": pair[0], "file_id": pair[1], "location": location}}

    def plan(self, scope):
        saved = saved_transaction_events(scope.connection, self._key)
        action = self._facts(scope.connection)
        if saved is not None:
            return self._reuse(scope, saved)
        if action["status"] == int(_ACTION_STATUS.CANCELED):
            # 新连接按原完整取消组恢复必要文件范围，而不是把全部
            # 历史失败文件接入任意取消请求的首次责任。
            return self._recover_terminal(scope)
        templates, result = self._templates()
        if not templates:
            return self._decision(result)
        allocation = scope.allocate(len(templates))
        events = tuple(_envelope(allocation.first_event_id + index, allocation.txn_id,
            kind, reason, rows, self._command.occurred_at,
            self._canceled_publication_evidence(kind, reason, rows))
            for index, (kind, reason, rows) in enumerate(templates))
        return CommandPlan(events=events, owners=self._owners, state_rows=self._state,
            read_coverage=ReadCoverage(self._ranges), result=result)

    def _restore_before(self, scope, saved):
        transaction = saved[0]["transaction"]
        prior = read_transaction_range(scope.connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        boundary = HistoryBoundary(0, 0) if prior is None else HistoryBoundary(prior.txn_id, prior.last_event_id)
        current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)
        restored = set()
        for event in saved:
            if event["occurred_at"] != self._command.occurred_at:
                raise TransactionError("取消取回的原事实时刻不同")
            for row in event["body"]["rows"]:
                identity = (row["table"], row["id"])
                if identity in restored:
                    continue
                if (identity not in self._owners or not row["before"]["exists"]
                        or not row["after"]["exists"]):
                    raise TransactionError("原取消取回组包含不同归属或阶段的责任")
                values = self._state[identity[0]][identity[1]]
                # 原事件表达变化列；流程身份和成员关联是首次确定后
                # 不变的事实，其余拟更新列在原完整边界恢复。
                columns = frozenset(row["before"]["values"])
                values.update(read_row_values_at_boundary(scope.connection,
                    owner=self._owners[identity], table=identity[0], row_id=identity[1],
                    columns=columns, current_values=values, boundary=boundary, current_boundary=current))
                restored.add(identity)

    def _reuse(self, scope, saved):
        self._restore_before(scope, saved)
        templates, result = self._templates()
        if not result.complete or len(saved) != len(templates):
            raise TransactionError("原取消取回事务缺少完整责任集合")
        for event, (kind, reason, rows) in zip(saved, templates):
            expected = [{"table": row.table, "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)}} for row in rows]
            if (event["type"] != kind or event["reason"] != reason
                    or not json_equal(event["body"]["evidence"],
                        self._canceled_publication_evidence(kind, reason, rows))
                    or not json_equal(event["body"]["rows"], expected)):
                raise TransactionError("原取消取回的身份、阶段或完整输入不同")
        return self._decision(result)

    def _recover_terminal(self, scope):
        # ACTION_OUTCOME 的独立取消事实唯一表达此目标首次终态。
        with closing(scope.connection.execute(
            "SELECT t.operation_key FROM history_transactions t JOIN history_events e ON e.transaction_id=t.id"
            " WHERE e.event_type=? AND json_extract(e.body_json,'$.reason')=4"
            " AND EXISTS (SELECT 1 FROM json_each(e.body_json,'$.rows') r"
            " WHERE json_extract(r.value,'$.table')='actions' AND json_extract(r.value,'$.id')=?)"
            " ORDER BY e.id LIMIT 1", (_ACTION_FINISHED_EVENT, self._command.action_id),
        )) as cursor:
            original = cursor.fetchone()
        if original is None:
            return self._decision(CanceledObtainSettlement(True))
        saved = saved_transaction_events(scope.connection, OperationKey(original[0]))
        assert saved is not None
        staging = []
        for event in saved:
            evidence = event["body"]["evidence"].get("canceled_publication")
            if evidence is None:
                continue
            if (set(evidence) != {"delivery_id", "copy_id", "file_id", "location"}
                    or evidence["location"] not in ("staging", "unknown")):
                raise ConsistencyError("原取消取回的发布中断依据不可解释")
            if evidence["location"] == "staging":
                staging.append((evidence["delivery_id"], evidence["copy_id"], evidence["file_id"]))
        self._command = replace(self._command, occurred_at=saved[0]["occurred_at"],
                                publishing_staging=tuple(sorted(staging)))
        return self._reuse(scope, saved)


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

    def __init__(self, command: FinishObtain, key: OperationKey, read_batch_size=DEFAULT_BATCH_SIZE) -> None:
        if not isinstance(command, FinishObtain):
            raise TypeError("取回完成申请必须使用 FinishObtain")
        self._command = command
        self._key = key
        self._read_batch_size = positive_limit(read_batch_size)
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
        if action["cancel_requested"]:
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                read_only=True, result=ObtainFinishResult(
                    action_status=action["status"], plan_status=None, prepared=None, failed=None,
                    disposition=ObtainFinishDisposition.SKIPPED))
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
        elif decision.prepared or self._empty_exclusion_proof(connection, action):
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
            plan_read_scope=PlanReadScope(action["plan_id"], self._read_batch_size),
        )

    def _load_facts(self, connection) -> ObtainAggregate:
        """从已保存行装配汇总事实；装载规则与选择、条目、交付状态一致。"""
        return load_obtain_facts(connection, self._command.action_id)

    def _empty_exclusion_proof(self, connection, action) -> bool:
        """原排除请求及已固定的无错来源共同证明零条目是完整选择。

        每条来源选择首次固定时已经由独立守卫核对完整目录和排除。
        完成阶段沿用这些事实，不按后来变化的目录重新选择。
        """
        mode, _ = read_selection_request(action["execution_spec_json"], action["input_fields_json"])
        if (mode is not SelectionMode.DEFAULT
                or not read_excluded_output_ids(action["input_fields_json"])
                or action["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED)):
            return False
        with closing(connection.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE s.status<>? OR s.error_code IS NOT NULL)"
            " FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id"
            " WHERE d.action_id=?",
            (int(_SELECTION_STATUS.FIXED), action["id"]),
        )) as cursor:
            count, invalid = cursor.fetchone()
        return count > 0 and invalid == 0

    def _unpublished_deliveries(self, connection) -> tuple[int, ...]:
        """汇总确定后仍处于准备或发布中的成功交付。"""
        with closing(connection.execute(
            "SELECT d.id FROM deliveries d"
            " JOIN obtain_items i ON i.delivery_id = d.id"
            " JOIN obtain_source_selections s ON s.id = i.selection_id"
            " JOIN action_dependencies dep ON dep.id = s.dependency_id"
            " WHERE dep.action_id=? AND i.status IN (?, ?)"
            " AND d.status IN (?, ?) ORDER BY d.id LIMIT 1",
            (self._command.action_id,
             int(_ITEM_STATUS.SELECTED), int(_ITEM_STATUS.DELIVERY_CREATED),
             int(_DELIVERY_STATUS.PREPARED), int(_DELIVERY_STATUS.PUBLISHING)),
        )) as cursor:
            return tuple(row[0] for row in cursor)

    def _load_siblings(self, connection, action) -> dict[int, dict[str, Any]]:
        siblings = _completion_siblings(connection, action)
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
        if (action["status"] == int(_ACTION_STATUS.SUCCEEDED)
                and not decision.prepared and not decision.failed
                and not self._empty_exclusion_proof(connection, action)):
            raise TransactionError("零条目成功缺少原排除请求及固定选择依据")
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


def load_obtain_facts(connection, action_id: int) -> ObtainAggregate:
    """SQLite 折叠完整来源与条目范围，Python 只保留固定数量的计数。"""
    ObjectId(action_id)
    with closing(
        connection.execute(
            "SELECT COUNT(*) FILTER (WHERE s.status<>?),"
            "COUNT(*) FILTER (WHERE s.status=? AND s.error_code IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM obtain_items i WHERE i.selection_id=s.id)),"
            "(SELECT COUNT(*) FROM obtain_items i"
            " JOIN obtain_source_selections os ON os.id=i.selection_id"
            " JOIN action_dependencies dep ON dep.id=os.dependency_id"
            " WHERE dep.action_id=? AND i.status=?)"
            " FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id"
            " WHERE d.action_id=?",
            (
                int(_SELECTION_STATUS.FIXED),
                int(_SELECTION_STATUS.FIXED),
                action_id,
                int(_ITEM_STATUS.UNRESOLVED),
                action_id,
            ),
        )
    ) as cursor:
        waiting, failed_sources, unresolved = cursor.fetchone()
    total = prepared = failed = 0
    # 状态分组的数量由登记枚举限制；不加载全部条目正文或交付行。
    with closing(connection.execute(
        "SELECT i.status,d.status,COUNT(*) FROM obtain_items i"
        " JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies dep ON dep.id=s.dependency_id"
        " LEFT JOIN deliveries d ON d.id=i.delivery_id"
        " WHERE dep.action_id=? AND i.status<>? GROUP BY i.status,d.status",
        (action_id, int(_ITEM_STATUS.UNRESOLVED)),
    )) as cursor:
        while row := cursor.fetchone():
            stage = obtain_item_stage(row[0], row[1])
            total += row[2]
            prepared += row[2] if stage is ObtainItemStage.PREPARED else 0
            failed += row[2] if stage is ObtainItemStage.FAILED else 0
    return ObtainAggregate(waiting, unresolved, total, prepared, failed, failed_sources)


def plan_complete(siblings, current_action_id: int) -> bool:
    """父计划的全部动作（含本事务动作）是否都已终态。"""
    return all(
        values.get("status") in _ACTION_TERMINAL
        or values["id"] == current_action_id
        for values in siblings.values()
    )


def _completion_siblings(connection, action):
    """只取一个未终态兄弟；独立聚合守卫另按原事务视图逐批复核。"""
    terminal = tuple(sorted(_ACTION_TERMINAL))
    marks = ",".join("?" for _ in terminal)
    with closing(connection.execute(
        f"SELECT id FROM actions WHERE plan_id=? AND id<>? AND status NOT IN ({marks})"
        " ORDER BY id LIMIT 1", (action["plan_id"], action["id"], *terminal),
    )) as cursor:
        blocker = cursor.fetchone()
    rows = {action["id"]: action}
    if blocker is not None:
        rows[blocker[0]] = row_facts(connection, "actions", blocker[0])
    return rows


class OutputsRepository:
    """来源固定、选择与读取资格的 SQLite 仓储。"""

    def __init__(self, read_batch_size=DEFAULT_BATCH_SIZE):
        self.read_batch_size = positive_limit(read_batch_size)

    def start_obtain_action(
        self, command: StartObtainAction, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObtainStartResult]:
        receipt = commit_operation(_StartObtainCommand(command, key, self.read_batch_size), key, owned)
        return _outcome_of(receipt)

    def start_cleanup_action(
        self, command: StartCleanupAction, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupStartResult]:
        receipt = commit_operation(_StartCleanupCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def resolve_sources(
        self, command: ResolveSources, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ResolveSourcesOutcome]:
        receipt = commit_operation(_ResolveSourcesCommand(command, key, self.read_batch_size), key, owned)
        return _outcome_of(receipt)

    def finish_obtain(
        self, command: FinishObtain, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObtainFinishResult]:
        receipt = commit_operation(_FinishObtainCommand(command, key, self.read_batch_size), key, owned)
        return _outcome_of(receipt)

    def settle_canceled_obtain(
        self, command: SettleCanceledObtain, key: OperationKey, owned: OwnedConnection,
    ) -> DbOutcome[CanceledObtainSettlement]:
        receipt = commit_operation(_SettleCanceledObtainCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def fix_selection(
        self, command: FixSelection, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[SelectionSaved]:
        receipt = commit_operation(_FixSelectionCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def grant_file(
        self, command: FileCandidate, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FileQualification]:
        receipt = commit_operation(_GrantFileCommand(command, key, self.read_batch_size), key, owned)
        return _outcome_of(receipt)

    def fallback_read_source(
        self, request: ReadSourceFallback, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ReadSourceFallbackOutcome]:
        receipt = commit_operation(
            _ReadSourceFallbackCommand(request, key, owned), key, owned)
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

    def save_photo_read_start(self, request, key: OperationKey, owned: OwnedConnection) -> DbOutcome:
        return _outcome_of(commit_operation(_PhotoReadStartCommand(request, key), key, owned))

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

    def fail_read_delivery(
        self, command: FailReadDelivery, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FailureSaveOutcome]:
        receipt = commit_operation(
            _ReadDeliveryFailCommand(command, key), key, owned)
        return _outcome_of(receipt)

    def fail_read_binding(
        self, command: FailReadBinding, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FailureSaveOutcome]:
        return _outcome_of(commit_operation(_ReadBindingFailCommand(command, key), key, owned))

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
        return _load_work_file_facts(owned, file_id)

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

    def fix_cleanup_targets(
        self, command: FixCleanupTargets, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupTargetsSaved]:
        receipt = commit_operation(
            _FixCleanupTargetsCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def restrict_cleanup_item(
        self, command: RestrictCleanupItem, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(_RestrictCleanupCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def progress_cleanup_item(
        self, command: ProgressCleanupItem, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(_ProgressCleanupCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def begin_cleanup_attempt(
        self, intent: AttemptIntent, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[BeginAttemptResult]:
        """清理首次处理归属、操作流程与第一次尝试的共同建档事务。"""
        receipt = commit_operation(
            _BeginCleanupAttemptCommand(intent, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_cleanup_attempt(
        self, finish: AttemptFinish, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FinishAttemptResult]:
        """清理 DELETE 尝试结果与不可撤销限制事实的共同保存事务。"""
        receipt = commit_operation(
            _FinishCleanupAttemptCommand(finish, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_cleanup_item(
        self, command: FinishCleanupItem, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(_FinishCleanupItemCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def handoff_cleanup_absence(self, command, key, owned):
        from camctl.persistence.repositories.cleanup_absence import HandoffCleanupAbsenceCommand
        if command.database_id != owned.metadata.instance_id:
            raise ConsistencyError("缺席交接申请不属于原数据库")
        return self._cleanup_item_outcome(commit_operation(
            HandoffCleanupAbsenceCommand(command, key), key, owned))

    def fail_cleanup_item(
        self, command: FailCleanupItem, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(_FailCleanupItemCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def fail_cleanup_binding(
        self, command: FailCleanupBinding, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        return self._cleanup_item_outcome(commit_operation(
            _CleanupBindingFailCommand(command, key), key, owned))

    def ensure_withdrawal_waiter(self, command, key, owned):
        return _outcome_of(commit_operation(_EnsureWithdrawalWaiterCommand(command, key), key, owned))

    def advance_withdrawal(
        self, command: AdvanceWithdrawal, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(
            _AdvanceWithdrawalCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def cancel_cleanup_item(
        self, command: CancelCleanupItem, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CleanupItemSaved]:
        receipt = commit_operation(
            _CancelCleanupItemCommand(command, key), key, owned)
        return self._cleanup_item_outcome(receipt)

    def finish_cleanup_action(
        self, command: FinishCleanupAction, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupActionFinished]:
        receipt = commit_operation(
            _FinishCleanupActionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_canceled_cleanup(
        self, command: FinishCanceledCleanupAction, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CleanupActionFinished]:
        receipt = commit_operation(
            _FinishCanceledCleanupCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    @staticmethod
    def _cleanup_item_outcome(receipt) -> DbOutcome[CleanupItemSaved]:
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

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
            "SELECT f.id " + _cleanup_candidate_source()
            + " AND f.id > ? AND f.id <= ? ORDER BY f.id LIMIT ?",
            (after_id, ceiling, limit),
        )) as cursor:
            return tuple(int(row[0]) for row in cursor.fetchall())

    def max_cleanup_candidate_id(self, owned: OwnedConnection) -> int:
        with closing(owned.connection.execute(
            "SELECT MAX(f.id) " + _cleanup_candidate_source(),
        )) as cursor:
            return int(cursor.fetchone()[0] or 0)


def _cleanup_candidate_source() -> str:
    """已释放责任及归属终态的 REQUIRED 文件共用候选规则。"""
    actions = ",".join(str(value) for value in _ACTION_TERMINAL)
    deliveries = ",".join(str(value) for value in sorted(_WorkFileMixin._TERMINAL_DELIVERIES))
    return (
        "FROM intermediate_files f LEFT JOIN actions a ON a.id = f.owner_action_id"
        " LEFT JOIN deliveries d ON d.id = f.owner_delivery_id WHERE ("
        f"(f.retention_state = {int(_RETENTION.RELEASABLE)}"
        f" AND f.cleanup_state <> {int(_FILE_CLEANUP.COMPLETED)}) OR"
        f" (f.retention_state = {int(_RETENTION.REQUIRED)}"
        f" AND (a.status IN ({actions}) OR d.status IN ({deliveries}))))")


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
    from .source_transactions import required_stream, verify_source_guard
    reads = context.current_reads
    if reads is not None and required_stream(reads.connection, event) is not None:
        verify_source_guard(event, context)
        return
    if event.event_type == _SOURCE_RESOLVED_EVENT and event.reason == _FIX_REASON:
        raise EventValidationError("执行期来源固定缺少原申请读取范围")
    created = [r for r in event.rows if r.table == "obtain_source_selections"]
    if created:
        raise EventValidationError("取回初始化缺少可靠原申请")


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
    from camctl.devices.tasks import CaptureResultPolicy

    source_spec = source.get("execution_spec_json")
    if (source_spec is not None
            and source_spec.get("result_policy")
            == CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value):
        raise EventValidationError("分段来源选择须核对原完整范围事务")
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
        SelectionFacts(
            source_id,
            True,
            entries,
            checked_output_sources=checked,
            source_failed=(source["status"] == int(_ACTION_STATUS.FAILED)),
        ), mode, requested,
        excluded_output_ids=read_excluded_output_ids(owner["input_fields_json"]))
    return source_id, expected


def _source_selection_guard(event, context) -> None:
    """逐来源选择守卫：按保存请求核对完整结果及条目 ID 对应的顺序。"""
    if event.event_type != _TARGETS_FIXED_EVENT or event.reason != _OBTAIN_REASON:
        return
    if "segment_selection_request" in event.evidence:
        from .segment_selections import verify_guard

        verify_guard(event, context)
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
    if event.event_type == _INTERMEDIATE_FILE_EVENT:
        from .host_capture import validate_host_photo_event
        validate_host_photo_event(event, context)
    if event.event_type == _INTERMEDIATE_FILE_EVENT and event.reason == _CLEANUP_RESULT_REASON:
        request = event.evidence.get("cleanup_request")
        if (not isinstance(request, Mapping) or set(request) != {"advance_cursor"}
                or type(request["advance_cursor"]) is not bool):
            raise EventValidationError("清理结果必须保存精确布尔 advance_cursor 申请")
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
            if after.get("retention_state") == int(_RETENTION.RELEASABLE):
                facts = context.state_rows.get("intermediate_files", {}).get(row.row_id)
                if facts is not None and facts.get("purpose") in (int(_PURPOSE.CAPTURE_ORIGINAL), int(_PURPOSE.CAPTURE_PREVIEW)):
                    reads = context.photo_result_reads
                    if reads is None:
                        raise EventValidationError("照片缓存释放缺少独立原事务读取")
                    from camctl.outputs.photo_cache_reuse import photo_cache_waiters_settled
                    if not photo_cache_waiters_settled(row.row_id, SimpleNamespace(connection=reads.connection)):
                        raise EventValidationError("照片缓存仍被 H 时固定的取回使用")
                    if _running_file_operations(reads.connection, row.row_id):
                        raise EventValidationError("照片缓存仍有源或目标读取尝试未结束")
            if "sha256" in after or "size_bytes" in after:
                sync_cache_size = (event.evidence.get("photo_sync_cache_request") is not None
                                   and set(after) == {"size_bytes"} and after["size_bytes"] is not None)
                if not sync_cache_size and (after.get("sha256") is None or after.get("size_bytes") is None):
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
            from .retry_wait_reads import RetryWaitReads
            reads = context.retry_wait_reads
            if type(reads) is not RetryWaitReads or not delivery_read_succeeded(reads.connection, row.row_id):
                raise EventValidationError("发布意图要求原读取流程可靠成功")
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
    elif event.event_type == _DELIVERY_CHANGED_EVENT and event.reason == 6:
        evidence = event.evidence.get("canceled_publication")
        for row in event.rows:
            if row.table != "deliveries":
                continue
            publishing = row.before.values.get("status") == int(_DELIVERY_STATUS.PUBLISHING)
            if not publishing:
                if evidence is not None:
                    raise EventValidationError("非发布中交付不能携带发布对账依据")
                continue
            if (not isinstance(evidence, Mapping)
                    or set(evidence) != {"delivery_id", "copy_id", "file_id", "location"}
                    or evidence.get("location") not in ("staging", "unknown")
                    or evidence.get("delivery_id") != row.row_id):
                raise EventValidationError("取消发布中交付缺少原副本观察依据")
            delivery = context.state_rows.get("deliveries", {}).get(row.row_id)
            copy = context.state_rows.get("file_copies", {}).get(evidence["copy_id"])
            target = context.state_rows.get("intermediate_files", {}).get(evidence["file_id"])
            action = None if delivery is None else context.state_rows.get("actions", {}).get(delivery["action_id"])
            if (delivery is None or delivery.get("publication_intent_event_id") is None
                    or action is None or action.get("type") != _OBTAIN_TYPE
                    or action.get("cancel_requested") != 1
                    or copy is None or copy.get("delivery_id") != row.row_id
                    or sum(candidate.get("delivery_id") == row.row_id for candidate
                           in context.state_rows.get("file_copies", {}).values()) != 1
                    or copy.get("target_file_id") != evidence["file_id"]
                    or target is None or target.get("owner_delivery_id") != row.row_id):
                raise EventValidationError("取消发布中交付的原责任或文件身份不一致")
            if (evidence["location"] == "unknown"
                    and delivery.get("withdrawal_state") != int(_WITHDRAWAL.UNKNOWN)):
                raise EventValidationError("未知位置结论必须已经可靠保存")
            if (evidence["location"] == "staging"
                    and delivery.get("withdrawal_state") == int(_WITHDRAWAL.UNKNOWN)):
                raise EventValidationError("未知位置不能作为 staging 副本处理")
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
            if code in ("read_attempts_exhausted", "checksum_mismatch", "source_file_unconfirmed",
                        "delivery_handoff_unconfirmed"):
                delivery = context.state_rows.get("deliveries", {}).get(row.row_id)
                action = None if delivery is None else context.state_rows.get("actions", {}).get(delivery.get("action_id"))
                if action is None or action.get("cancel_requested") != 0:
                    raise EventValidationError("交付失败要求发起动作的取消尚未生效")
                if code == "delivery_handoff_unconfirmed" and action.get("status") != int(_ACTION_STATUS.RUNNING):
                    raise EventValidationError("交接未知失败要求原取回仍在执行")
            try:
                spec = registered_error(code)
                if stage != spec["stage"]:
                    raise ValueError(
                        f"失败阶段与公共登记不符: {stage!r} != {spec['stage']!r}")
                validate_error_details(code, details)
            except (TypeError, ValueError) as failure:
                raise EventValidationError(str(failure)) from failure
            # 详情按公共登记结构化：带交付身份字段的（如交接未确
            # 认）必须与本行一致；无身份字段的详情由命令装配的输
            # 入交付定位，守卫不追加身份。
            if details.get("delivery_id") is not None \
                    and details.get("delivery_id") != str(row.row_id):
                raise EventValidationError("交付失败详情必须关联本次交付")
            if code == "source_device_unavailable":
                from .preparation import PreparationReads
                from .availability_failure import verify_read_failure
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("设备不可用失败缺少原连接事实")
                verify_read_failure(context.preparation_reads.connection, event, context, row)
            if code == "device_binding_unavailable" and details.get("reason") in ("missing", "mismatch"):
                copies = [copy for copy in context.state_rows.get("file_copies", {}).values()
                          if copy.get("delivery_id") == row.row_id]
                if len(copies) == 1 and copies[0].get("source_device_file_id") is None:
                    reads = context.current_reads
                    if reads is None:
                        raise EventValidationError("照片缓存绑定失败缺少原事务事实")
                    from camctl.outputs.photo_cache_reuse import verify_photo_cache_binding_failure
                    source_id = verify_photo_cache_binding_failure(reads.connection, copies[0], before_event_id=event.event_id)
                    source = row_facts(reads.connection, "device_files", source_id)
                    observer = None if source is None else row_facts(reads.connection, "actions", source["observer_action_id"])
                    if (observer is None or (details.get("device_id"), details.get("expected_driver_id"))
                            != (observer["device_id"], observer["driver_id"])):
                        raise EventValidationError("照片缓存绑定失败与原设备身份不同")
            if code == "device_binding_unavailable" and details.get("reason") in ("physical_identity_conflict", "physical_identity_mismatch"):
                from .preparation import PreparationReads
                from .physical_alias import source_physical_alias_conflict, verify_source_identity_mismatch
                if type(context.preparation_reads) is not PreparationReads:
                    raise EventValidationError("读取失败缺少机身冲突核对")
                copies = [copy for copy in context.state_rows.get("file_copies", {}).values()
                          if copy.get("delivery_id") == row.row_id]
                if len(copies) != 1:
                    raise EventValidationError("机身冲突失败缺少唯一原拷贝")
                source = _guard_facts(context, "device_files", copies[0]["source_device_file_id"])
                observer = _guard_facts(context, "actions", source["observer_action_id"])
                origin = _guard_facts(context, "actions", source["source_action_id"])
                delivery = _guard_facts(context, "deliveries", row.row_id)
                valid = (verify_source_identity_mismatch(context.preparation_reads.connection, source, observer, origin,
                                                         event.evidence.get("read_current_identity"), action_id=delivery["action_id"])
                         if details["reason"] == "physical_identity_mismatch"
                         else source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin,
                                                             action_id=delivery["action_id"]))
                if (details.get("device_id") != observer["device_id"]
                        or details.get("expected_driver_id") != observer["driver_id"]
                        or not valid):
                    raise EventValidationError("读取失败缺少其他逻辑设备的原责任")


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


def _photo_cache_recopy_proof(connection, copy, attempt, *, require_reset=False):
    """从原 READ_END、实际摘要不符及已登记新轮，证明照片缓存交付未完成。"""
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.read_session import ReadEnd
    from camctl.history.validators import validate_event_structure
    from camctl.persistence.fact_reads import read_event

    if (copy is None or attempt is None or copy.get("source_intermediate_file_id") is None
            or copy.get("source_device_file_id") is not None or copy.get("delivery_id") is None
            or attempt.get("copy_round") is None or copy.get("round") != attempt["copy_round"] + 1):
        return None
    with closing(connection.execute(
        "SELECT a.driver_id,a.type,a.execution_spec_json,o.device_file_id,f.owner_action_id,o.source_action_id"
        " FROM deliveries d JOIN outputs o ON o.id=d.output_id JOIN actions a ON a.id=o.source_action_id"
        " JOIN intermediate_files f ON f.id=? WHERE d.id=?",
        (copy["source_intermediate_file_id"], copy["delivery_id"]))) as cursor:
        source = cursor.fetchone()
    if source is None:
        return None
    spec = source[2] if isinstance(source[2], Mapping) else parse_exact_json(source[2])
    if (source[0] != DRIVER_ID or source[1] != int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
            or not isinstance(spec, Mapping) or spec.get("output_storage") != "device"
            or source[3] is None or source[4] != source[5]):
        return None
    end_limit = attempt.get("result_event_id") or (1 << 63) - 1
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
        " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.round')=? ORDER BY id LIMIT 2",
        (_COPY_CHANGED_EVENT, _RECOPY_REASON, attempt["intent_event_id"], end_limit,
         copy["id"], copy["round"]))) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    recopy = read_event(connection, rows[0][0])
    if (validate_event_structure(recopy.event)[:2] != ("COPY_CHANGED", "RECOPY")
            or len(recopy.event.rows) != 1):
        raise ConsistencyError("照片缓存重拷缺少原完整事件")
    row = recopy.event.rows[0]
    before, after = row.before.values, row.after.values
    end_id = before.get("read_end_event_id")
    target_hash = before.get("target_sha256")
    if (row.table != "file_copies" or row.row_id != copy["id"] or not row.before.exists or not row.after.exists
            or before.get("round") != attempt["copy_round"] or after.get("round") != copy["round"]
            or before.get("committed_bytes") != copy["source_size"] or after.get("committed_bytes") != 0
            or before.get("verification_state") != int(_VERIFICATION.MISMATCHED)
            or after.get("verification_state") != int(_VERIFICATION.NOT_PERFORMED)
            or before.get("reset_state") != int(_RESET_STATE.READY)
            or after.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
            or before.get("recopies_used") != copy["round"] - 2
            or after.get("recopies_used") != copy["round"] - 1
            or type(end_id) is not int or not isinstance(target_hash, str)
            or not isinstance(copy.get("source_sha256"), str) or target_hash == copy["source_sha256"]):
        raise ConsistencyError("照片缓存重拷与实际摘要不符或原轮次不一致")
    end_record = read_event(connection, end_id)
    end_event = end_record.event
    document = end_event.evidence.get("read_end")
    if (validate_event_structure(end_event)[:2] != ("COPY_CHANGED", "READ_END")
            or end_record.transaction.last_event_id >= recopy.transaction.first_event_id
            or not isinstance(document, Mapping) or not isinstance(document.get("ticket"), Mapping)
            or document["ticket"] != {"run_id": attempt["run_id"], "attempt_no": attempt["attempt_no"],
                "operation": "read", "target_id": str(copy["id"]), "responsibility_key": f"read/{copy['id']}"}
            or document.get("copy_round") != attempt["copy_round"]
            or document.get("source_intermediate_file_id") != copy["source_intermediate_file_id"]
            or document.get("source_device_file_id") is not None
            or document.get("target_file_id") != copy["target_file_id"]
            or document.get("source_size") != copy["source_size"]
            or document.get("committed_bytes") != copy["source_size"]
            or document.get("reset_state") != int(_RESET_STATE.READY)
            or len(end_event.rows) != 1 or end_event.rows[0].row_id != copy["id"]
            or end_event.rows[0].table != "file_copies"
            or end_event.rows[0].after.values.get("read_end_event_id") != end_id):
        raise ConsistencyError("照片缓存重拷缺少原读取实际收场事实")
    actual_end = document.get("end")
    if (not isinstance(actual_end, Mapping) or set(actual_end) != {"stopped", "bytes_read", "error"}
            or actual_end["stopped"] is not True or actual_end["error"] is not None
            or type(actual_end["bytes_read"]) is not int or not 0 <= actual_end["bytes_read"] <= copy["source_size"]):
        raise ConsistencyError("照片缓存重拷的原读取结束不完整")
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
        " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.verification_state')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.target_sha256')=? ORDER BY id DESC LIMIT 1",
        (_COPY_CHANGED_EVENT, _VERIFY_REASON, end_id, recopy.event.event_id, copy["id"],
         int(_VERIFICATION.MISMATCHED), target_hash))) as cursor:
        mismatch = cursor.fetchone()
    if mismatch is None:
        raise ConsistencyError("照片缓存重拷缺少原实际摘要不符事件")
    verification = read_event(connection, mismatch[0])
    if (validate_event_structure(verification.event)[:2] != ("COPY_CHANGED", "VERIFY")
            or len(verification.event.rows) != 1
            or verification.event.rows[0].table != "file_copies"
            or verification.event.rows[0].row_id != copy["id"]
            or verification.event.rows[0].after.values.get("verification_state") != int(_VERIFICATION.MISMATCHED)
            or verification.event.rows[0].after.values.get("target_sha256") != target_hash
            or (verification.transaction.txn_id != recopy.transaction.txn_id
                and verification.transaction.last_event_id >= recopy.transaction.first_event_id)):
        raise ConsistencyError("照片缓存原摘要不符事件与重拷事务不一致")
    if require_reset:
        with closing(connection.execute(
            "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
            " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
            " AND json_extract(body_json,'$.rows[0].id')=? ORDER BY id LIMIT 2",
            (_COPY_CHANGED_EVENT, _RESET_REASON, recopy.transaction.last_event_id, end_limit,
             copy["id"]))) as cursor:
            resets = cursor.fetchall()
        if len(resets) != 1:
            raise ConsistencyError("照片缓存重拷缺少原目标重置事件")
        reset = read_event(connection, resets[0][0])
        if (validate_event_structure(reset.event)[:2] != ("COPY_CHANGED", "RESET")
                or len(reset.event.rows) != 1 or reset.event.rows[0].table != "file_copies"
                or reset.event.rows[0].row_id != copy["id"]
                or reset.event.rows[0].before.values.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
                or reset.event.rows[0].after.values.get("reset_state") != int(_RESET_STATE.READY)):
            raise ConsistencyError("照片缓存原目标重置事件不完整")
    return ReadEnd(**actual_end)


def _copy_round_chain_proven(
        connection, copy_id, initial_round, current_round,
        intent_event_id, result_event_id, source_size) -> bool:
    """流式证明 READ_FILE 意图到结果间的连续重拷轮次。

    每次只解码一条涉及目标拷贝的历史事件，并仅累计期望轮次；内存
    不随重拷次数增长。调用方分别在写命令和独立事件守卫中校验该链。
    """
    values = (copy_id, initial_round, current_round, intent_event_id, result_event_id)
    if any(type(value) is not int or value <= 0 for value in values):
        return False
    if type(source_size) is not int or source_size < 0:
        return False
    if (initial_round > current_round
            or intent_event_id >= result_event_id):
        return False

    expected_round = initial_round
    with closing(connection.execute(
        "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,"
        " h.clock_status,h.change_seq,h.body_json"
        " FROM history_events h"
        " WHERE h.id>? AND h.id<?"
        " AND EXISTS (SELECT 1 FROM json_each(h.body_json,'$.rows') AS r"
        "  WHERE json_extract(r.value,'$.table')='file_copies'"
        "  AND json_extract(r.value,'$.id')=?)"
        " ORDER BY h.id",
        (intent_event_id, result_event_id, copy_id),
    )) as cursor:
        for stored_event in cursor:
            event = decode_event_row(stored_event)
            copy_change = None
            for change in event.rows:
                if change.table == "file_copies" and change.row_id == copy_id:
                    if copy_change is not None:
                        return False
                    copy_change = change
            if copy_change is None:
                return False

            before, after = copy_change.before.values, copy_change.after.values
            has_before_round = "round" in before
            has_after_round = "round" in after
            if event.event_type == _COPY_CHANGED_EVENT and event.reason == _RECOPY_REASON:
                if (not copy_change.before.exists or not copy_change.after.exists
                        or not has_before_round or not has_after_round
                        or before.get("round") != expected_round
                        or after.get("round") != expected_round + 1
                        or before.get("recopies_used") != expected_round - 1
                        or after.get("recopies_used") != expected_round
                        or before.get("committed_bytes", 0 if source_size == 0 else None) != source_size
                        or after.get("committed_bytes", 0 if source_size == 0 else None) != 0
                        or before.get("reset_state") != int(_RESET_STATE.READY)
                        or after.get("reset_state") != int(_RESET_STATE.RESET_PENDING)
                        or before.get("verification_state") != int(_VERIFICATION.MISMATCHED)
                        or after.get("verification_state") != int(_VERIFICATION.NOT_PERFORMED)
                        or "target_sha256" not in before
                        or after.get("target_sha256") is not None):
                    return False
                expected_round += 1
                continue

            if (has_before_round != has_after_round
                    or (has_before_round and before["round"] != after["round"])):
                return False

    return expected_round == current_round


def _copy_guard(event, context) -> None:
    """拷贝建档、进度段、目标重置、校验与重拷守卫。"""
    if event.event_type == _COPY_CHANGED_EVENT and event.reason == _PHOTO_READ_START_REASON:
        from camctl.outputs.photo_read import parse_photo_read_start, decide_photo_read_start

        try:
            if len(event.rows) != 1 or event.rows[0].table != "file_copies":
                raise ValueError("照片读取长度只能修改一份拷贝")
            row = event.rows[0]
            request = parse_photo_read_start(event.evidence["photo_read_start"], event.occurred_at)
            if request.copy_id != row.row_id:
                raise ValueError("照片读取票据和目标拷贝不同")
            copy = context.state_rows.get("file_copies", {}).get(row.row_id)
            source = None if copy is None else context.state_rows.get("device_files", {}).get(copy.get("source_device_file_id"))
            origin = None if source is None else context.state_rows.get("actions", {}).get(source.get("source_action_id"))
            run = context.state_rows.get("operation_runs", {}).get(request.ticket.run_id)
            attempts = [a for a in context.state_rows.get("operation_attempts", {}).values()
                        if a.get("run_id") == request.ticket.run_id
                        and a.get("attempt_no") == request.ticket.attempt_id]
            delivery = None if copy is None else context.state_rows.get("deliveries", {}).get(copy.get("delivery_id"))
            if (copy is None or source is None or origin is None or run is None or len(attempts) != 1
                    or delivery is None or context.photo_result_reads is None
                    or copy.get("source_intermediate_file_id") is not None
                    or copy.get("processing_id") is not None or copy.get("inspection_id") is not None
                    or read_action_spec(origin).get("photo_source_policy") != "deferred"
                    or source.get("size_bytes") is not None or source.get("sha256") is not None
                    or run.get("kind") != int(_RUN_KIND.READ_FILE) or run.get("status") != int(_RUN_STATUS.ACTIVE)
                    or run.get("copy_id") != row.row_id or run.get("delivery_id") != delivery.get("id")
                    or run.get("action_id") != delivery.get("action_id")
                    or run.get("attempts_used") != request.ticket.attempt_id
                    or run.get("responsibility_key") != request.ticket.responsibility_key
                    or attempts[0].get("status") != int(_ATTEMPT_STATUS.RUNNING)
                    or attempts[0].get("intent_event_id") is None or attempts[0].get("result_event_id") is not None
                    or attempts[0].get("copy_round") != copy.get("round")
                    or copy.get("slot_device_id") != origin.get("device_id")
                    or copy.get("read_end_event_id") is not None
                    or copy.get("verification_state") != int(_VERIFICATION.NOT_PERFORMED)
                    or copy.get("source_sha256") is not None or copy.get("target_sha256") is not None):
                raise ValueError("照片读取长度缺少持槽的原读取尝试及按需来源")
            completion = source.get("completion_evidence_json")
            observation = completion.get("observation") if isinstance(completion, Mapping) else None
            context.photo_result_reads.verify_device_file(source["id"], observation,
                before_event_id=event.event_id, role=source["role"], size_bytes=None,
                check_size=True, sha256=None, check_sha=True)
            decision = decide_photo_read_start(previous_size=copy["source_size"],
                committed_bytes=copy["committed_bytes"],
                reset_pending=copy["reset_state"] == int(_RESET_STATE.RESET_PENDING),
                current_size=request.size_bytes)
            expected = _update("file_copies", row.row_id,
                {name: copy[name] for name in ("source_size", "committed_bytes", "reset_state")},
                {"source_size": decision.source_size, "committed_bytes": decision.committed_bytes,
                 "reset_state": int(_RESET_STATE.RESET_PENDING if decision.reset_pending else _RESET_STATE.READY)})
            if (not json_equal(row.before.values, expected.before.values)
                    or not json_equal(row.after.values, expected.after.values)):
                raise ValueError("照片读取长度或重置状态与原申请不符")
        except (ConsistencyError, ValueError, TypeError, KeyError) as error:
            raise EventValidationError(f"照片读取长度事件无效: {error}") from error
        return
    if (event.event_type == _COPY_CHANGED_EVENT
            and event.reason == _COPY_SOURCE_FALLBACK_REASON):
        if len(event.rows) != 1 or event.rows[0].table != "file_copies":
            raise EventValidationError("读取换源只能更新一份正式取回拷贝")
        row = event.rows[0]
        copy = context.state_rows.get("file_copies", {}).get(row.row_id)
        before, after = row.before.values, row.after.values
        source_columns = {
            "source_device_file_id", "source_intermediate_file_id"}
        if (copy is None or set(before) - {"read_end_event_id"} != source_columns
                or set(after) - {"read_end_event_id"} != source_columns
                or ("read_end_event_id" in after and after["read_end_event_id"] is not None)
                or (copy.get("read_end_event_id") is not None
                    and (before.get("read_end_event_id") != copy["read_end_event_id"]
                         or "read_end_event_id" not in after or after["read_end_event_id"] is not None))
                or copy.get("source_device_file_id") is not None
                or copy.get("source_intermediate_file_id") is None
                or not all(json_equal(copy.get(name), value)
                           for name, value in before.items())
                or before["source_device_file_id"] is not None
                or before["source_intermediate_file_id"] is None
                or after["source_device_file_id"] is None
                or after["source_intermediate_file_id"] is not None
                or copy.get("delivery_id") is None
                or copy.get("processing_id") is not None
                or copy.get("inspection_id") is not None):
            raise EventValidationError("读取换源必须是同一交付拷贝从检查副本单向回到设备源")

        delivery = context.state_rows.get("deliveries", {}).get(
            copy["delivery_id"])
        attempt_id = event.evidence.get("attempt_id")
        attempt = context.state_rows.get("operation_attempts", {}).get(attempt_id)
        run_rows = [
            facts for facts in context.state_rows.get("operation_runs", {}).values()
            if facts.get("copy_id") == row.row_id
            and facts.get("kind") == int(_RUN_KIND.READ_FILE)]
        if (delivery is None or len(run_rows) != 1 or attempt is None):
            raise EventValidationError("读取换源缺少原交付、尝试或 READ_FILE 流程")
        run = run_rows[0]
        if (attempt.get("run_id") != run.get("id")
                or attempt.get("attempt_no") != run.get("attempts_used")
                or attempt.get("status") != int(_ATTEMPT_STATUS.FAILED)
                or attempt.get("intent_event_id") is None
                or attempt.get("result_event_id") is None
                or not isinstance(attempt.get("error_json"), Mapping)
                or attempt["error_json"].get("code") != "local_read_failed"
                or not isinstance(attempt["error_json"].get("details"), Mapping)
                or run.get("responsibility_key") != f"read/{row.row_id}"
                or run.get("delivery_id") != delivery.get("id")
                or run.get("status") != int(_RUN_STATUS.ACTIVE)
                or run.get("retry_wait_required") != 1
                or run.get("attempts_used", 0) >= run.get("max_attempts_used", 0)):
            raise EventValidationError("读取换源必须沿用仍有余量的原本地失败读取预算")
        reads = context.current_reads
        details = attempt["error_json"]["details"]
        if details.get("source_failed") is not True:
            if (reads is None or details.get("photo_cache_checksum_mismatch") is not True
                    or copy.get("reset_state") != int(_RESET_STATE.READY)
                    or _photo_cache_recopy_proof(reads.connection, copy, attempt, require_reset=True) is None):
                raise EventValidationError("照片缓存换源缺少原摘要不符、读取收场及目标重置")
        if (reads is None or not _copy_round_chain_proven(
                reads.connection, row.row_id, attempt.get("copy_round"),
                copy.get("round"), attempt.get("intent_event_id"),
                attempt.get("result_event_id"), copy.get("source_size"))):
            raise EventValidationError("读取换源的拷贝轮次没有原 READ_FILE 意图到结果的连续重拷链")

        item_rows = [
            facts for facts in context.state_rows.get("obtain_items", {}).values()
            if facts.get("delivery_id") == delivery.get("id")]
        if len(item_rows) != 1:
            raise EventValidationError("读取换源的交付必须属于唯一正式取回项")
        item = item_rows[0]
        output = context.state_rows.get("outputs", {}).get(delivery.get("output_id"))
        source = context.state_rows.get("device_files", {}).get(
            after["source_device_file_id"])
        source_role = (None if output is None else {
            int(_OUTPUT_KIND.ORIGINAL): int(_DEVICE_FILE_ROLE.ORIGINAL),
            int(_OUTPUT_KIND.PREVIEW): int(_DEVICE_FILE_ROLE.PREVIEW),
        }.get(output.get("kind")))
        if (output is None or source is None
                or output.get("id") != item.get("output_id")
                or source_role is None
                or output.get("device_file_id") != source.get("id")
                or source.get("source_action_id") != output.get("source_action_id")
                or source.get("role") != source_role
                or source.get("completion_state") != int(_COMPLETION.COMPLETE)
                or source.get("presence_state") != int(_PRESENCE.PRESENT)
                or output.get("availability") != int(_AVAILABILITY.AVAILABLE)):
            raise EventValidationError("读取换源必须回到同一产物仍可读取的完整原片")

        original_action = context.state_rows.get("actions", {}).get(
            output.get("source_action_id"))
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        if (original_action is not None and original_action.get("driver_id") == DRIVER_ID
                and original_action.get("type") == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
                and original_action.get("execution_spec_json", {}).get("output_storage") == "device"):
            from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
            if context.current_reads is None:
                raise EventValidationError("照片读取换源缺少独立原事务读取")
            try:
                bound = read_bound_photo_cache(output_id=output["id"], obtain_action_id=delivery["action_id"],
                    selection_id=item["selection_id"], owned=SimpleNamespace(connection=context.current_reads.connection))
            except (ValueError, TypeError, KeyError) as error:
                raise EventValidationError("照片读取换源的原来源或 H 无效") from error
            if (bound is None or bound.intermediate_file_id != before["source_intermediate_file_id"]
                    or bound.source_device_file_id != source["id"] or bound.size_bytes != copy["source_size"]
                    or bound.sha256 != copy["source_sha256"]):
                raise EventValidationError("照片读取换源必须回到 H 上固定的同一角色")
            return
        from camctl.devices.tasks import CaptureResultPolicy

        check_copies = [
            facts for facts in context.state_rows.get("file_copies", {}).values()
            if facts.get("target_file_id") == before["source_intermediate_file_id"]
            and facts.get("inspection_id") is not None]
        if len(check_copies) != 1:
            raise EventValidationError("读取换源的本地来源必须是唯一分段检查副本")
        check_copy = check_copies[0]
        inspection = context.state_rows.get("segment_inspections", {}).get(
            check_copy["inspection_id"])
        input_file = context.state_rows.get("intermediate_files", {}).get(
            before["source_intermediate_file_id"])
        if (inspection is None or input_file is None
                or inspection.get("action_id") != output.get("source_action_id")
                or inspection.get("source_device_file_id") != source.get("id")
                or inspection.get("state") != int(_SEGMENT_INSPECTION_STATE.COMPLETED)
                or check_copy.get("source_device_file_id") != source.get("id")
                or check_copy.get("verification_state") not in (
                    int(_VERIFICATION.MATCHED),
                    int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE))
                or input_file.get("owner_action_id") != output.get("source_action_id")
                or input_file.get("owner_delivery_id") is not None
                or input_file.get("purpose") != int(_PURPOSE.RECORDING_INPUT)
                or input_file.get("retention_state") != int(_RETENTION.REQUIRED)
                or input_file.get("cleanup_state") != int(_FILE_CLEANUP.NOT_NEEDED)
                or input_file.get("size_bytes") != copy.get("source_size")
                or input_file.get("sha256") != check_copy.get("target_sha256")):
            raise EventValidationError("读取换源的检查副本与固定 Action6 原片不一致")
        if (original_action is None
                or output.get("kind") != int(_OUTPUT_KIND.ORIGINAL)
                or original_action.get("type") not in (
                    int(_ACTION_TYPE.CAMERA_RECORD),
                    int(_ACTION_TYPE.CAMERA_TIMELAPSE))
                or original_action.get("execution_spec_json", {}).get("result_policy")
                != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
            raise EventValidationError("读取换源仅适用于 Action6 分段视频")
        return
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
            if "slot_device_id" in after:
                from .read_end import load_read_end
                reads = context.read_end_reads
                if (reads is None or source is None or row.before.values.get("slot_device_id") != copy.get("slot_device_id")
                        or after["slot_device_id"] is not None
                        or load_read_end(row.row_id, SimpleNamespace(connection=reads.connection)) is None):
                    raise EventValidationError("源摘要与释放机会必须关联原完整读取结束")
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
            if before.get("committed_bytes", copy.get("committed_bytes")) != copy.get("source_size"):
                raise EventValidationError("重拷登记要求全部字节已经可靠保存")
            if after.get("round") != before.get("round", 0) + 1 \
                    or after.get("recopies_used") != before.get("recopies_used", -1) + 1:
                raise EventValidationError("重拷登记必须恰好增加一轮并消耗一次额度")
            if after.get("committed_bytes", copy.get("committed_bytes")) != 0:
                raise EventValidationError("重拷登记必须把可靠进度归零")
            if after.get("target_sha256") is not None:
                raise EventValidationError("新一轮拷贝不能沿用旧目标摘要")
            if "read_end_event_id" in after and after["read_end_event_id"] is not None:
                raise EventValidationError("新一轮拷贝不能建立读取结束依据")
            if (copy.get("read_end_event_id") is not None
                    and (before.get("read_end_event_id") != copy["read_end_event_id"]
                         or "read_end_event_id" not in after or after["read_end_event_id"] is not None)):
                raise EventValidationError("新一轮拷贝不能沿用原读取结束依据")
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
            source = context.state_rows.get("device_files", {}).get(values.get("source_device_file_id"))
            reads = context.photo_result_reads
            if (values.get("delivery_id") is None or values.get("source_intermediate_file_id") is not None
                    or source is None or reads is None or not _deferred_photo_source(reads.connection, source)):
                raise EventValidationError("只有按需照片交付可以暂时不知道源长度")
            completion = source.get("completion_evidence_json")
            observation = completion.get("observation") if isinstance(completion, Mapping) else None
            try:
                reads.verify_device_file(source["id"], observation, before_event_id=event.event_id,
                    role=source["role"], size_bytes=None, check_size=True, sha256=None, check_sha=True)
            except (ConsistencyError, ValueError, TypeError, KeyError) as error:
                raise EventValidationError(f"未知长度照片来源无效: {error}") from error
        if sum(values.get(name) is not None for name in
               ("delivery_id", "processing_id", "inspection_id")) != 1:
            raise EventValidationError("拷贝必须恰归属交付、录像处理或分段检查之一")
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
        elif values.get("inspection_id") is not None:
            parent = context.state_rows.get("segment_inspections", {}).get(values["inspection_id"])
            member = (context.state_rows.get("segment_members", {}).get(parent.get("member_id"))
                      if parent is not None else None)
            source = context.state_rows.get("device_files", {}).get(values.get("source_device_file_id"))
            if (parent is None or member is None or source is None
                    or parent.get("state") != int(_SEGMENT_INSPECTION_STATE.PENDING)
                    or parent.get("source_device_file_id") != values.get("source_device_file_id")
                    or member.get("file_id") != values.get("source_device_file_id")
                    or source.get("source_action_id") != parent.get("action_id")):
                raise EventValidationError("检查拷贝必须关联 PENDING 检查及其固定原片")
        else:
            parent = context.association_rows.get("deliveries", {}).get(values["delivery_id"])
        if parent is None:
            raise EventValidationError("新建拷贝缺少发起责任的固定关联")
        if values.get("delivery_id") is not None and values.get("source_intermediate_file_id") is not None:
            output = context.state_rows.get("outputs", {}).get(parent.get("output_id"))
            source_action = (None if output is None else
                context.state_rows.get("actions", {}).get(output.get("source_action_id")))
            if output is None or (output.get("device_file_id") is not None and source_action is None):
                raise EventValidationError("本地复用交付缺少原产物或来源动作")
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            if (output.get("device_file_id") is not None
                    and source_action.get("driver_id") == DRIVER_ID
                    and source_action.get("execution_spec_json", {}).get("output_storage") == "device"):
                members = [item for item in context.association_rows.get("obtain_items", {}).values()
                           if item.get("delivery_id") == values["delivery_id"]]
                if len(members) != 1 or context.current_reads is None:
                    raise EventValidationError("照片缓存交付缺少原取回项及独立事务读取")
                from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
                try:
                    bound = read_bound_photo_cache(output_id=output["id"],
                        obtain_action_id=parent["action_id"], selection_id=members[0]["selection_id"],
                        owned=SimpleNamespace(connection=context.current_reads.connection))
                except (TypeError, ValueError, KeyError) as error:
                    raise EventValidationError("照片缓存交付的原来源或 H 无效") from error
                if (bound is None or bound.intermediate_file_id != values["source_intermediate_file_id"]
                        or bound.size_bytes != values["source_size"] or bound.sha256 != values["source_sha256"]):
                    raise EventValidationError("照片缓存交付不属于 H 时固定等待者的原角色")
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
        output_id = row.after.values.get("output_id", item.get("output_id"))
        if "output_id" in row.after.values and (not unresolved or not explicit):
            raise EventValidationError(
                "只有显式未解析项可以在拒绝时保存已确认的产物关联"
            )
    if output_id is None:
        if event.reason == _RESOLVE_REASON or not unresolved:
            raise EventValidationError("已选中或核实成功的成员必须关联真实产物")
        return
    output = _required_current_facts(context, "outputs", output_id)
    if output.get("source_action_id") != dependency.get("depends_on_action_id"):
        raise EventValidationError("取回成员的真实产物必须属于固定来源")
    if explicit and output_id != item["requested_output_id"]:
        raise EventValidationError("显式成员的真实产物必须等于原请求 ID")
    if event.reason == _REJECT_REASON and row.after.values.get("error_code") == item_error_id(
            "obtain_items", "device_binding_unavailable"):
        file_id = output.get("device_file_id")
        if file_id is None:
            raise EventValidationError("主机产物不能保存设备绑定失败")
        file = _required_current_facts(context, "device_files", file_id)
        observer = _required_current_facts(context, "actions", file["observer_action_id"])
        details = row.after.values["error_details_json"]
        if (details.get("device_id"), details.get("expected_driver_id")) != (
                observer["device_id"], observer["driver_id"]):
            raise EventValidationError("取回绑定失败必须指向原文件观察者")


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


def _has_source_fallback_retry_proof(connection, copy, attempt) -> bool:
    """核实原失败尝试已有指向同一拷贝和设备文件的换源事件。"""
    if (connection is None or not is_json_integer(attempt.get("id"))
            or not is_json_integer(attempt.get("result_event_id"))
            or not is_json_integer(copy.get("id"))
            or not is_json_integer(copy.get("source_device_file_id"))):
        return False
    from camctl.persistence.retry_wait_structure import verify_retry_wait_index
    try:
        verify_retry_wait_index(connection, "history_events_row")
    except ValueError as error:
        raise ConsistencyError(str(error)) from error
    with closing(connection.execute(
        "SELECT 1 FROM history_events e"
        " WHERE e.event_type=? AND json_extract(e.body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(e.body_json,'$.rows[0].id')=? AND e.id>?"
        " AND json_extract(e.body_json,'$.reason')=?"
        " AND json_extract(e.body_json,'$.evidence.attempt_id')=?"
        " AND json_array_length(e.body_json,'$.rows')=1"
        " AND json_extract(e.body_json,'$.rows[0].before.values.source_device_file_id') IS NULL"
        " AND json_extract(e.body_json,'$.rows[0].before.values.source_intermediate_file_id') IS NOT NULL"
        " AND json_extract(e.body_json,'$.rows[0].after.values.source_device_file_id')=?"
        " AND json_extract(e.body_json,'$.rows[0].after.values.source_intermediate_file_id') IS NULL"
        " LIMIT 1",
        (_COPY_CHANGED_EVENT, copy["id"], attempt["result_event_id"], _COPY_SOURCE_FALLBACK_REASON,
         attempt["id"], copy["source_device_file_id"]),
    )) as cursor:
        return cursor.fetchone() is not None


def _source_fallback_retry_attempt(copy, run, attempts, connection):
    """返回有连续重拷链和原换源事件证明的等待中本地源失败尝试。"""
    if (run.get("status") != int(_RUN_STATUS.ACTIVE)
            or run.get("retry_wait_required") != 1
            or run.get("attempts_used", 0) < 1
            or copy.get("source_device_file_id") is None
            or copy.get("source_intermediate_file_id") is not None):
        return None
    matches = [
        attempt for attempt in attempts.values()
        if attempt.get("run_id") == run.get("id")
        and attempt.get("attempt_no") == run.get("attempts_used")
    ]
    if len(matches) != 1:
        return None
    attempt = matches[0]
    if (attempt.get("status") != int(_ATTEMPT_STATUS.FAILED)
            or attempt.get("intent_event_id") is None
            or attempt.get("result_event_id") is None):
        return None
    error = attempt.get("error_json")
    if isinstance(error, str):
        error = parse_exact_json(error)
    if not isinstance(error, Mapping):
        return None
    details = error.get("details")
    if (error.get("code") != "local_read_failed"
            or error.get("stage") != "source_read"
            or not isinstance(details, Mapping)
            or (details.get("source_failed") is not True
                and details.get("photo_cache_checksum_mismatch") is not True)):
        return None
    if not _copy_round_chain_proven(
            connection, copy.get("id"), attempt.get("copy_round"),
            copy.get("round"), attempt.get("intent_event_id"),
            attempt.get("result_event_id"), copy.get("source_size")):
        return None
    if not _has_source_fallback_retry_proof(connection, copy, attempt):
        return None
    return attempt


def _slot_owner_action(context, copy) -> dict[str, Any]:
    action_id = None
    if copy.get("delivery_id") is not None:
        action_id = context.state_rows.get("deliveries", {}).get(
            copy["delivery_id"], {}).get("action_id")
    elif copy.get("processing_id") is not None:
        action_id = context.state_rows.get("recording_processing", {}).get(
            copy["processing_id"], {}).get("action_id")
    elif copy.get("inspection_id") is not None:
        action_id = context.state_rows.get("segment_inspections", {}).get(
            copy["inspection_id"], {}).get("action_id")
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
            if copy.get("read_end_event_id") is not None:
                raise EventValidationError("当前轮读取已经结束，不能再次授予相机机会")
            before_slot = row.before.values.get("slot_device_id") if row.before.exists else None
            if before_slot is not None:
                raise EventValidationError("授予必须从空闲机会开始")
            if after_slot != binding[0]:
                raise EventValidationError("授予的设备必须等于原来源设备绑定")
            from .preparation import PreparationReads
            if (type(context.preparation_reads) is not PreparationReads
                    or context.preparation_reads.device_start_in_flight(binding[0])):
                raise EventValidationError("读取机会授予缺少同设备启动收场依据")
            from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
            identity = None
            if binding[1] == DRIVER_ID:
                from .physical_alias import source_physical_alias_conflict, source_physical_identity
                if source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin):
                    raise EventValidationError("读取机会越过了其他逻辑设备的机身责任")
                identity = source_physical_identity(context.preparation_reads.connection, source, observer, origin)
                from .capture_selection import CaptureSelectionInput, read_capture_priority
                try:
                    selection = CaptureSelectionInput.from_document(event.evidence.get("capture_selection"))
                    selected = read_capture_priority(context.preparation_reads.connection, selection,
                        context.transaction.first_event_id - 1, device_id=binding[0], identity=identity,
                        occurred_at=event.occurred_at)
                except (ConsistencyError, ValueError) as error:
                    raise EventValidationError(f"读取机会缺少原拍摄选择资格: {error}") from error
                if selected is not None:
                    raise EventValidationError("读取机会越过了已选中的拍摄")
            if context.preparation_reads.availability_in_flight(binding[0], identity):
                raise EventValidationError("读取机会越过了尚未收场的设备检查")
            if run["status"] in _RUN_TERMINAL:
                raise EventValidationError("读取责任已结束，不能再授予机会")
            if run["retry_wait_required"] != 0:
                attempts = context.complete_rows(
                    "operation_attempts", "run_id", run["id"])
                reads = context.current_reads
                if (reads is None or _source_fallback_retry_attempt(
                        copy, run, attempts, reads.connection) is None):
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
        from .retry_wait_reads import RetryWaitReads
        reads = context.retry_wait_reads
        if type(reads) is not RetryWaitReads or not reads.no_unsettled_attempts(run["id"], context.validated_events):
            raise EventValidationError("实际读取未结束，不能释放机会")
        if copy.get("processing_id") is not None:
            action = _slot_owner_action(context, copy)
            if _canceled_binding_processing_settled(reads.connection, copy, run, action) is False:
                raise EventValidationError("取消处理的原实际调用未全部收场")
        if run["retry_wait_required"] != 0:
            raise EventValidationError("重试等待期间保留机会")
        if run["status"] not in _RUN_TERMINAL and (
                copy["source_size"] is None or copy["committed_bytes"] < copy["source_size"]):
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
        details = after.get("error_details_json")
        if isinstance(details, Mapping) and details.get("reason") in ("physical_identity_conflict", "physical_identity_mismatch"):
            from .preparation import PreparationReads
            from .physical_alias import source_physical_alias_conflict, verify_source_identity_mismatch
            if type(context.preparation_reads) is not PreparationReads:
                raise EventValidationError("读取机身冲突缺少原连接事实")
            item = _guard_facts(context, "obtain_items", updates[0].row_id)
            output = _guard_facts(context, "outputs", item["output_id"])
            source = _guard_facts(context, "device_files", output["device_file_id"])
            observer = _guard_facts(context, "actions", source["observer_action_id"])
            origin = _guard_facts(context, "actions", source["source_action_id"])
            selection = _guard_facts(context, "obtain_source_selections", item["selection_id"])
            dependency = _guard_facts(context, "action_dependencies", selection["dependency_id"])
            valid = (verify_source_identity_mismatch(context.preparation_reads.connection, source, observer, origin,
                                                     event.evidence.get("read_current_identity"), action_id=dependency["action_id"])
                     if details["reason"] == "physical_identity_mismatch"
                     else source_physical_alias_conflict(context.preparation_reads.connection, source, observer, origin,
                                                         action_id=dependency["action_id"]))
            if (details.get("device_id") != observer["device_id"]
                    or details.get("expected_driver_id") != observer["driver_id"]
                    or not valid):
                raise EventValidationError("读取机身冲突缺少未完成原责任")
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
            if delivery.get("status") in (6, 7):
                runs = context.complete_rows("operation_runs", "copy_id", copy["id"])
                if not runs or any(run.get("status") not in _RUN_TERMINAL
                                   or run.get("retry_wait_required") != 0 for run in runs.values()):
                    raise EventValidationError("最终失败或取消后解除源保护要求读取及后续重试已结束")
                for run in runs.values():
                    from .retry_wait_reads import RetryWaitReads
                    reads = context.retry_wait_reads
                    if type(reads) is not RetryWaitReads or not reads.no_unsettled_attempts(run["id"], context.validated_events):
                        raise EventValidationError("读取没有可靠结束结果，不能解除源保护")
                continue
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
            if context.current_reads is None:
                raise EventValidationError("修复成功缺少独立媒体读取端口")
            from camctl.capture.media_facts import read_probe
            try:
                raw = event.evidence.get("output_probe")
                context.current_reads.repair_media_proof(processing["action_id"], processing["id"], file_id,
                    output_probe=None if raw is None else read_probe(raw))
            except (ConsistencyError, ValueError) as error:
                raise EventValidationError(str(error)) from error
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
    explicit = _requested_cleanup_ids(action)
    if not created:
        if explicit is None:
            # 范围清理固定空集合：来源已可靠确认没有正式产物，动
            # 作成功收场由同事务的完成事件表达。
            return
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
    if explicit is not None:
        from camctl.outputs.cleanup_group import cleanup_group
        reads = _CurrentProductReads(context)
        expected = list(explicit)
        for identity in explicit:
            for member in cleanup_group(reads, identity):
                if member not in expected:
                    expected.append(member)
        actual = [row.after.values.get("requested_output_id")
                  for row in sorted(created, key=lambda row: row.row_id)]
        if actual != expected:
            raise EventValidationError("精确清理成员与原请求及机内照片配对不一致")
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
            output = context.state_rows.get("outputs", {}).get(confirmed)
            if output is not None:
                from camctl.outputs.cleanup_group import cleanup_group
                reads = _CurrentProductReads(context)
                if any(reads.required("outputs", group_id).get("availability") == int(_AVAILABILITY.AVAILABLE)
                       and has_product_predecessor(reads, before_facts["action_id"], group_id, event.occurred_at)
                       for group_id in cleanup_group(reads, confirmed)):
                    raise EventValidationError("较早的取回或清理尚未取得资格")


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
            elif values.get("status") == int(_CLEANUP_ITEM_STATUS.FAILED):
                item = context.state_rows.get("cleanup_items", {}).get(row.row_id)
                if (item is not None and item.get("output_id") is not None
                        and context.read_coverage.covers("cleanup_items", "action_id", item["action_id"])):
                    owner = _cleanup_group_call_owner(_CurrentProductReads(context), item)
                    if owner is not None and owner["id"] != row.row_id:
                        details = dict(owner.get("error_details_json") or {})
                        if "output_id" in details:
                            details["output_id"] = str(item["output_id"])
                        if (owner["status"] != int(_CLEANUP_ITEM_STATUS.FAILED)
                                or values.get("error_code") != owner.get("error_code")
                                or not json_equal(values.get("error_details_json"), details)):
                            raise EventValidationError("共同照片后续成员必须沿原处理者失败事实结束")
            continue
        if values.get("status") == int(_CLEANUP_ITEM_STATUS.DELETING):
            before = context.state_rows.get("cleanup_items", {}).get(
                row.row_id, {})
            target = before.get("output_id") or values.get("output_id")
            if target is None:
                continue
            if not _cleanup_entry_ready(_CurrentProductReads(context), before):
                raise EventValidationError("共同来源仍有读取依赖或其他删除处理者")
            owner = _cleanup_group_call_owner(_CurrentProductReads(context), before, exclude_self=True)
            if owner is not None and owner["id"] != row.row_id:
                raise EventValidationError("同一请求的共同照片已有处理者")
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
    if target is None:
        # 成功事件行不携带产物列；待删除成员复用完成事实时从当前
        # 成员事实解析目标。
        target = context.state_rows.get("cleanup_items", {}).get(
            row.row_id, {}).get("output_id")
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



# -- 源产物清理目标固定 -------------------------------------------------


class _FixCleanupTargetsCommand:
    """固定清理动作的完整目标集合（TARGETS_FIXED.CLEANUP）。

    精确清理按原请求逐项固定：可解析目标创建未解析成员；可靠不存
    在的目标先固定集合、再按 output_not_found 直接终态。范围清理等
    待全部固定来源终态且适用产物处理完成后，按来源枚举产物创建成
    员；来源未就绪时只读等待。动作的失败汇总（cleanup_items_failed）
    由完成登记事务在全部成员终态后保存。
    """

    def __init__(self, command, key: OperationKey) -> None:
        if not isinstance(command, FixCleanupTargets):
            raise TypeError("清理目标固定申请必须使用 FixCleanupTargets")
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
            raise ConsistencyError(f"清理动作不存在: {command.action_id}")
        requested = _explicit_cleanup_request(action)
        if action["type"] != _DELETE_ACTION_TYPE:
            raise ConsistencyError(
                f"清理目标固定只适用于清理动作: {command.action_id}")
        # 精确清理按原请求 ID 逐项核实，没有来源引用；范围清理按
        # 受理时固定的来源枚举。
        if requested is None and action["source_resolution_state"] \
                != int(_RESOLUTION_STATE.FIXED):
            raise ConsistencyError("范围清理目标固定要求已固定来源的清理动作")
        if action["target_selection_state"] == _TARGET_FAILED:
            raise TransactionError("目标集合已失败，不重新固定")
        if action["target_selection_state"] == _TARGET_FIXED:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupTargetsSaved(
                    CleanupTargetsDisposition.ALREADY,
                    _saved_member_ids(connection, command.action_id)))
        if (action["status"] != int(_ACTION_STATUS.RUNNING)
                or action["cancel_requested"]):
            raise TransactionError("目标固定要求清理动作执行中且未取消")
        self._state["actions"] = {command.action_id: dict(action)}
        # 公开投影路由从清理项走到产物表；装配空范围供解析。
        self._state.setdefault("cleanup_items", {})
        self._state.setdefault("outputs", {})
        reads = _ProductReads(connection, self._state)
        if requested is None:
            requested = self._scope_targets(connection)
            if requested is None:
                return CommandPlan(
                    events=(), owners=self._owners, state_rows=self._state,
                    read_only=True,
                    result=CleanupTargetsSaved(
                        CleanupTargetsDisposition.WAITING, ()))
            if not requested:
                # 范围来源可靠确认没有正式产物：固定空集合并同事务
                # 保存成功终态（output-cleanup.md 来源及产物状态表）。
                return self._empty_targets_plan(scope, action)
        else:
            from camctl.outputs.cleanup_group import cleanup_group
            expanded = list(requested)
            for identity in requested:
                for member in cleanup_group(reads, identity):
                    if member not in expanded:
                        expanded.append(member)
            requested = tuple(expanded)
        next_item = next_row_id(connection, "cleanup_items")
        members: list[tuple[int, int]] = []
        terminal: list[tuple[int, int]] = []
        for identity in requested:
            output = row_facts(connection, "outputs", identity)
            if output is not None:
                self._state.setdefault("outputs", {})[identity] = output
                members.append((next_item, identity))
            else:
                terminal.append((next_item, identity))
            next_item += 1
        # 先固定事件，缺失目标随后直接终态；全部不可解析时动作的
        # 失败终态由流程汇总步骤保存（与取回 finish 汇总同模式）。
        event_count = 1 + (1 if terminal else 0)
        allocation = scope.allocate(event_count)
        fix_event_id = allocation.first_event_id
        terminal_event_id = fix_event_id + 1
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        fix_rows = (_update(
            "actions", command.action_id,
            {"target_selection_state": _TARGET_PENDING},
            {"target_selection_state": _TARGET_FIXED}),)
        for row_id, identity in members + terminal:
            self._owners[("cleanup_items", row_id)] = (
                "action", command.action_id)
        for row_id, identity in members + terminal:
            self._state["cleanup_items"][row_id] = {
                "action_id": command.action_id,
                "requested_output_id": identity,
                "output_id": None,
                "status": int(_CLEANUP_ITEM_STATUS.UNRESOLVED),
                "restriction_state": int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED),
                "outcome": None,
                "final_event_id": None,
                "error_code": None,
                "error_details_json": None,
            }
        fix_rows += tuple(
            _row("cleanup_items", row_id, {
                "action_id": command.action_id,
                "requested_output_id": identity,
                "output_id": None,
                "status": int(_CLEANUP_ITEM_STATUS.UNRESOLVED),
                "restriction_state": int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED),
                "outcome": None,
                "final_event_id": None,
                "error_code": None,
                "error_details_json": None,
            }) for row_id, identity in members + terminal)
        events = [_envelope(
            fix_event_id, allocation.txn_id, _TARGETS_FIXED_EVENT,
            _TARGETS_CLEANUP_REASON, fix_rows, command.occurred_at)]
        if terminal:
            terminal_rows = tuple(
                _update("cleanup_items", row_id, {
                    "status": int(_CLEANUP_ITEM_STATUS.UNRESOLVED),
                    "restriction_state": int(
                        _CLEANUP_RESTRICTION.NOT_ESTABLISHED),
                    "outcome": None,
                    "final_event_id": None,
                    "error_code": None,
                    "error_details_json": None,
                }, {
                    "status": int(_CLEANUP_ITEM_STATUS.FAILED),
                    # 产物从未确认：限制保持未建立（表约束）。
                    "restriction_state": int(
                        _CLEANUP_RESTRICTION.NOT_ESTABLISHED),
                    "outcome": None,
                    "final_event_id": terminal_event_id,
                    "error_code": _CLEANUP_ITEM_NOT_FOUND,
                    "error_details_json": {
                        "requested_output_id": str(identity)},
                }) for row_id, identity in terminal)
            events.append(_envelope(
                terminal_event_id, allocation.txn_id,
                _CLEANUP_CHANGED_EVENT, 4, terminal_rows, command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            read_coverage=reads.read_coverage(),
            result=CleanupTargetsSaved(
                CleanupTargetsDisposition.SAVED,
                tuple(row_id for row_id, _ in members + terminal),
                tuple((row_id, ErrorValue("output_not_found", "output_selection",
                                         {"requested_output_id": str(identity)}))
                      for row_id, identity in terminal)))

    def _scope_targets(self, connection) -> tuple[int, ...] | None:
        """范围清理按固定来源枚举产物；来源未就绪时返回 None 表示等待。

        全部固定来源终态且适用产物处理完成后才枚举，保证目标集合
        不随来源后续变化扩大；守卫按同一来源事实核对成员资格。
        """
        dependencies = self._state.setdefault("action_dependencies", {})
        sources: list[int] = []
        with closing(connection.execute(
            "SELECT id, depends_on_action_id FROM action_dependencies"
            " WHERE action_id = ? ORDER BY id", (self._command.action_id,),
        )) as cursor:
            for dependency_id, source_id in cursor.fetchall():
                sources.append(int(source_id))
                dependency = row_facts(
                    connection, "action_dependencies", int(dependency_id))
                if dependency is not None:
                    dependencies[int(dependency_id)] = dependency
        if not sources:
            # 来源计划真实存在但没有拍摄成员，是已经固定的合法空范围。
            return ()
        actions = self._state["actions"]
        for source_id in sources:
            source = row_facts(connection, "actions", source_id)
            if source is None:
                raise ConsistencyError(f"来源动作不存在: {source_id}")
            actions[source_id] = dict(source)
            if source["status"] not in _ACTION_TERMINAL:
                return None
            if not _processing_completed(connection, source_id):
                return None
        outputs = self._state.setdefault("outputs", {})
        identities: list[int] = []
        with closing(connection.execute(
            "SELECT id FROM outputs WHERE source_action_id IN (%s) ORDER BY id"
            % ",".join("?" * len(sources)), sources,
        )) as cursor:
            for (identity,) in cursor.fetchall():
                identity = int(identity)
                identities.append(identity)
                output = row_facts(connection, "outputs", identity)
                if output is not None:
                    outputs[identity] = output
        return tuple(identities)

    def _empty_targets_plan(self, scope, action) -> CommandPlan:
        """固定空目标集合并同事务保存动作成功终态与父计划状态。"""
        command = self._command
        connection = scope.connection
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state.setdefault("plans", {})[plan["id"]] = plan
        siblings: dict[int, dict[str, Any]] = {}
        with closing(connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?",
            (action["plan_id"],),
        )) as cursor:
            for (identity,) in cursor.fetchall():
                facts = row_facts(connection, "actions", int(identity))
                if facts is not None:
                    siblings[int(identity)] = facts
        self._state["actions"].update(siblings)
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        templates = [
            (_TARGETS_FIXED_EVENT, _TARGETS_CLEANUP_REASON,
             (_update("actions", command.action_id,
                      {"target_selection_state": _TARGET_PENDING},
                      {"target_selection_state": _TARGET_FIXED}),)),
            (_ACTION_FINISHED_EVENT, 1,
             (_update("actions", command.action_id,
                      {"status": action["status"]},
                      {"status": int(_ACTION_STATUS.SUCCEEDED)}),)),
        ]
        plan_status = plan["status"]
        if plan_complete(siblings, command.action_id) \
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
            for index, (event_type, event_reason, rows)
            in enumerate(templates)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=CleanupTargetsSaved(CleanupTargetsDisposition.SAVED, ()))

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实固定分支与成员集合后恢复首次响应。"""
        command = self._command
        if not saved or saved[0]["type"] != _TARGETS_FIXED_EVENT:
            raise TransactionError("原事务不是清理目标固定，不能作为重送核实")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("目标固定的事实时刻与原事务不同")
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise ConsistencyError(f"清理动作不存在: {command.action_id}")
        created = {
            row["after"]["values"]["requested_output_id"]
            for event in saved
            for row in event["body"]["rows"]
            if row["table"] == "cleanup_items"
            and "requested_output_id" in row["after"]["values"]}
        expected = _explicit_cleanup_request(action)
        if expected is None:
            scope = self._scope_targets(connection)
            expected = () if scope is None else scope
        if _explicit_cleanup_request(action) is not None and created != set(expected):
            from camctl.outputs.cleanup_group import cleanup_group
            reads = _ProductReads(connection, self._state)
            expanded = set(expected)
            for identity in expected:
                for member in cleanup_group(reads, identity):
                    target = reads.optional("outputs", member)
                    if target is not None and target["created_event_id"] < saved[0]["event_id"]:
                        expanded.add(member)
            expected = tuple(expanded)
        if created != set(expected):
            raise TransactionError("目标固定的成员集合与重送输入不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupTargetsSaved(
                CleanupTargetsDisposition.ALREADY,
                _saved_member_ids(connection, command.action_id),
                tuple((row["id"], ErrorValue("output_not_found", "output_selection",
                          row["after"]["values"]["error_details_json"]))
                      for event in saved for row in event["body"]["rows"]
                      if row["table"] == "cleanup_items"
                      and row["after"]["values"].get("error_code") == _CLEANUP_ITEM_NOT_FOUND)))


def _explicit_cleanup_request(action) -> tuple[int, ...] | None:
    """精确清理的原请求目标；范围清理（params.source）返回 None。"""
    fields = action["input_fields_json"]
    if not isinstance(fields, Mapping) or not isinstance(
            fields.get("params"), Mapping):
        raise ConsistencyError("清理动作缺少原请求参数")
    values = fields["params"]
    if "output_ids" not in values:
        return None
    ids = values["output_ids"]
    if not isinstance(ids, list) or not ids:
        raise ConsistencyError("精确清理缺少非空原目标列表")
    identities = []
    for value in ids:
        try:
            identities.append(parse_object_id(value))
        except ValueError as error:
            raise ConsistencyError(
                f"精确清理目标不是合法产物身份: {value!r}") from error
    if len(set(identities)) != len(identities):
        raise ConsistencyError("精确清理目标不能重复")
    return tuple(identities)


def _saved_member_ids(connection, action_id: int) -> tuple[int, ...]:
    with closing(connection.execute(
        "SELECT id FROM cleanup_items WHERE action_id = ? ORDER BY id",
        (action_id,),
    )) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())
# -- 清理项生命周期：限制建立、删除推进与终态 -------------------------


class _CleanupItemCommandMixin:
    """清理项命令的共同装载：动作、成员、产物与删除尝试依据。"""

    def _load_item(self, connection, item_id: int) -> dict[str, Any]:
        item = row_facts(connection, "cleanup_items", item_id)
        if item is None:
            raise ConsistencyError(f"清理成员不存在: {item_id}")
        self._state.setdefault("cleanup_items", {})[item_id] = dict(item)
        action = row_facts(connection, "actions", item["action_id"])
        if action is None:
            raise ConsistencyError(f"清理动作不存在: {item['action_id']}")
        self._state["actions"] = {item["action_id"]: dict(action)}
        output_id = item["output_id"]
        if output_id is not None:
            output = row_facts(connection, "outputs", output_id)
            if output is None:
                raise ConsistencyError(f"产物不存在: {output_id}")
            self._state.setdefault("outputs", {})[output_id] = dict(output)
        else:
            self._state.setdefault("outputs", {})
        return item

    def _derive_projection(
        self, connection, output_id: int, item_after: Mapping[str, Any],
    ) -> dict[str, Any]:
        """按本产物全部清理成员派生清理汇总与可用性投影。

        自上而下：可靠清理完成优先，其次删除中、待删除的责任；责任
        结束但限制仍留下时按最终结果事件选择错误来源；限制全部解除
        或从未建立时按可靠文件事实恢复可用。本事务刚终态的成员总在
        已保存事件之后，错误选择时优先于其他候选。
        """
        output = self._state["outputs"][output_id]
        items: dict[int, dict[str, Any]] = {}
        with closing(connection.execute(
            "SELECT id FROM cleanup_items WHERE output_id = ? AND id <> ?",
            (output_id, item_after["id"]),
        )) as cursor:
            for (identity,) in cursor.fetchall():
                identity = int(identity)
                row = row_facts(connection, "cleanup_items", identity)
                if row is not None:
                    items[identity] = row
                    self._state.setdefault("cleanup_items", {})[identity] = row
        current = dict(self._state.setdefault("cleanup_items", {}).get(
            item_after["id"], {}))
        current.update(item_after)
        items[item_after["id"]] = current
        succeeded = int(_CLEANUP_ITEM_STATUS.SUCCEEDED)
        deleting = int(_CLEANUP_ITEM_STATUS.DELETING)
        pending = int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)
        canceled = int(_CLEANUP_ITEM_STATUS.CANCELED)
        if ((output["availability"] == int(_AVAILABILITY.CLEANED)
                and output["cleanup_status"] == int(_OUTPUT_CLEANUP_STATUS.COMPLETED))
                or any(item["status"] == succeeded for item in items.values())):
            return {"availability": int(_AVAILABILITY.CLEANED),
                    "cleanup_status": int(_OUTPUT_CLEANUP_STATUS.COMPLETED),
                    "cleanup_error_json": None}
        if any(item["status"] == deleting for item in items.values()):
            return {"availability": int(_AVAILABILITY.RESTRICTED),
                    "cleanup_status": int(_OUTPUT_CLEANUP_STATUS.RUNNING),
                    "cleanup_error_json": None}
        if any(item["status"] == pending for item in items.values()):
            return {"availability": int(_AVAILABILITY.RESTRICTED),
                    "cleanup_status": int(_OUTPUT_CLEANUP_STATUS.PENDING),
                    "cleanup_error_json": None}
        held = [
            item for item in items.values()
            if item["status"] in (int(_CLEANUP_ITEM_STATUS.FAILED), canceled)
            and item["restriction_state"] in (
                int(_CLEANUP_RESTRICTION.ACTIVE),
                int(_CLEANUP_RESTRICTION.IRREVERSIBLE))
        ]
        if held:
            # 本事务保存的终态事件晚于全部已保存事件，直接作为错误来源。
            chosen = next(
                (item for item in held if item["id"] == item_after["id"]), None)
            if chosen is None:
                chosen = max(held, key=lambda item: (
                    item["final_event_id"] or 0, item["id"]))
            name, spec = registered_error_spec(
                "item_error_ids.cleanup_items", chosen["error_code"])
            return {"availability": int(_AVAILABILITY.RESTRICTED),
                    "cleanup_status": int(_OUTPUT_CLEANUP_STATUS.INCOMPLETE),
                    "cleanup_error_json": {
                        "code": name, "stage": spec["stage"],
                        "details": chosen["error_details_json"]}}
        if output["availability"] not in (int(_AVAILABILITY.AVAILABLE),
                                          int(_AVAILABILITY.RESTRICTED)):
            raise ConsistencyError(
                "清理责任结束后没有可靠依据改写产物可用性:"
                f" {output_id} availability={output['availability']!r}")
        released = any(
            item["status"] == canceled
            and item["restriction_state"] == int(_CLEANUP_RESTRICTION.RELEASED)
            for item in items.values())
        return {"availability": int(_AVAILABILITY.AVAILABLE),
                "cleanup_status": (
                    int(_OUTPUT_CLEANUP_STATUS.CANCELED) if released
                    else int(_OUTPUT_CLEANUP_STATUS.NOT_REQUESTED)),
                "cleanup_error_json": None}

    def _projection_change(self, output, derived) -> tuple[dict, dict] | None:
        """派生投影与当前值的实际变化；无变化时不生成投影事件。"""
        before = {column: output[column] for column in derived}
        if all(json_equal(before[column], derived[column])
               for column in derived):
            return None
        return before, derived

    def _load_delete_basis(self, connection, item_id: int) -> None:
        """装载该成员的删除流程与尝试，供终态成功依据核对。"""
        self._state.setdefault("operation_runs", {})
        self._state.setdefault("operation_attempts", {})
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE cleanup_item_id = ?",
            (item_id,),
        )) as cursor:
            run_ids = tuple(int(row[0]) for row in cursor.fetchall())
        for run_id in run_ids:
            run = row_facts(connection, "operation_runs", run_id)
            if run is not None:
                self._state["operation_runs"][run_id] = run
            with closing(connection.execute(
                "SELECT id FROM operation_attempts WHERE run_id = ?",
                (run_id,),
            )) as cursor:
                attempt_ids = tuple(
                    int(row[0]) for row in cursor.fetchall())
            for attempt_id in attempt_ids:
                attempt = row_facts(
                    connection, "operation_attempts", attempt_id)
                if attempt is not None:
                    self._state["operation_attempts"][attempt_id] = attempt

    def _claim(self, item) -> None:
        self._owners[("cleanup_items", item["id"])] = (
            "action", item["action_id"])


class _RestrictCleanupCommand(_CleanupItemCommandMixin):
    """建立删除限制（CLEANUP_CHANGED.RESTRICT + 产物可用性投影）。

    成员从未解析进入限制中并首次确认产物身份；产物投影同事务进入
    RESTRICTED/PENDING，阻止后续取回资格。
    """

    def __init__(self, command: RestrictCleanupItem, key: OperationKey) -> None:
        if not isinstance(command, RestrictCleanupItem):
            raise TypeError("限制建立申请必须使用 RestrictCleanupItem")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._verify_and_reuse(saved, (24, 1))
        item = self._load_item(connection, command.item_id)
        action = self._state["actions"][item["action_id"]]
        if action["target_selection_state"] != _TARGET_FIXED:
            raise ConsistencyError("限制建立要求目标集合已固定")
        if action["status"] != int(_ACTION_STATUS.RUNNING) or action["cancel_requested"]:
            raise TransactionError("限制建立要求清理动作执行中且未取消")
        if item["status"] != int(_CLEANUP_ITEM_STATUS.UNRESOLVED):
            return self._already(item)
        if item["output_id"] is None:
            # 目标固定创建的成员尚未确认产物身份：按原请求目标装载。
            requested = row_facts(
                connection, "outputs", item["requested_output_id"])
            if requested is not None:
                self._state.setdefault("outputs", {})[
                    item["requested_output_id"]] = dict(requested)
        output = self._state["outputs"].get(item["requested_output_id"])
        if output is None:
            raise ConsistencyError(
                f"请求产物不存在，不能建立限制: {item['requested_output_id']}")
        if output["availability"] not in (
                int(_AVAILABILITY.AVAILABLE),
                int(_AVAILABILITY.RESTRICTED),
                int(_AVAILABILITY.CLEANED)):
            raise ConsistencyError("产物文件事实不允许建立删除限制")
        if action["scheduled_at"] > command.occurred_at:
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                read_only=True, result=CleanupItemSaved(CleanupItemDisposition.WAITING, command.item_id))
        reads = _ProductReads(connection, self._state)
        from camctl.outputs.cleanup_group import cleanup_group
        for group_id in cleanup_group(reads, item["requested_output_id"]):
            group_output = reads.required("outputs", group_id)
            if (group_output["availability"] == int(_AVAILABILITY.AVAILABLE)
                    and has_product_predecessor(reads, item["action_id"], group_id, command.occurred_at)):
                return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                    read_only=True, result=CleanupItemSaved(CleanupItemDisposition.WAITING, command.item_id))
        restrict = _update(
            "cleanup_items", command.item_id,
            {"output_id": item["output_id"],
             "status": int(_CLEANUP_ITEM_STATUS.UNRESOLVED),
             "restriction_state": int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED)},
            {"output_id": item["requested_output_id"],
             "status": int(_CLEANUP_ITEM_STATUS.PENDING_DELETE),
             "restriction_state": int(_CLEANUP_RESTRICTION.ACTIVE)},
        )
        self._claim(item)
        self._owners[("outputs", item["requested_output_id"])] = (
            "output", item["requested_output_id"])
        after_item = {
            "id": item["id"], "action_id": item["action_id"],
            "requested_output_id": item["requested_output_id"],
            "output_id": item["requested_output_id"],
            "status": int(_CLEANUP_ITEM_STATUS.PENDING_DELETE),
            "restriction_state": int(_CLEANUP_RESTRICTION.ACTIVE),
            "outcome": None, "final_event_id": None,
            "error_code": None, "error_details_json": None,
        }
        derived = self._derive_projection(
            connection, item["requested_output_id"], after_item)
        change = self._projection_change(output, derived)
        allocation = scope.allocate(1 + (1 if change else 0))
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _CLEANUP_CHANGED_EVENT, 1, (restrict,), command.occurred_at)]
        if change:
            events.append(_envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                (_update("outputs", item["requested_output_id"],
                         change[0], change[1]),),
                command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            read_coverage=reads.read_coverage(),
            result=CleanupItemSaved(
                CleanupItemDisposition.SAVED, command.item_id))

    def _already(self, item) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, item["id"]))

    def _verify_and_reuse(self, saved, expected):
        if [(event["type"], event["reason"]) for event in saved][0] != expected:
            raise TransactionError("原事务不是限制建立，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("限制建立的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "cleanup_items" or row["id"] != self._command.item_id:
            raise TransactionError("原限制建立属于其他成员")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, self._command.item_id))


class _ProgressCleanupCommand(_CleanupItemCommandMixin):
    """进入删除执行或保存不可撤销事实（CLEANUP_CHANGED.PROGRESS）。

    首次取得处理归属从限制中进入删除中并保持可撤销限制；不可撤销
    限制只在删除尝试取得可靠非未派发结果的事实保存事务中升级，未
    派发结果不升级（限制登记语义见 workflow-fields 的限制状态）。
    """

    def __init__(self, command: ProgressCleanupItem, key: OperationKey) -> None:
        if not isinstance(command, ProgressCleanupItem):
            raise TypeError("删除推进申请必须使用 ProgressCleanupItem")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            item = self._load_item(connection, command.item_id)
            return self._verify_and_reuse(scope, saved, item)
        item = self._load_item(connection, command.item_id)
        if command.irreversible:
            # 删除中成员取得可靠派发事实：限制从可撤销升级为不可撤销。
            if item["status"] != int(_CLEANUP_ITEM_STATUS.DELETING):
                raise ConsistencyError("不可撤销事实要求成员处于删除中")
            if item["restriction_state"] == int(_CLEANUP_RESTRICTION.IRREVERSIBLE):
                return CommandPlan(
                    events=(), owners=self._owners, state_rows=self._state,
                    read_only=True,
                    result=CleanupItemSaved(
                        CleanupItemDisposition.ALREADY, command.item_id))
            if item["restriction_state"] != int(_CLEANUP_RESTRICTION.ACTIVE):
                raise ConsistencyError("不可撤销事实要求本项限制仍可撤销")
            upgrade = _update(
                "cleanup_items", command.item_id,
                {"restriction_state": int(_CLEANUP_RESTRICTION.ACTIVE)},
                {"restriction_state": int(_CLEANUP_RESTRICTION.IRREVERSIBLE)},
            )
            self._claim(item)
            allocation = scope.allocate(1)
            return CommandPlan(
                events=(_envelope(
                    allocation.first_event_id, allocation.txn_id,
                    _CLEANUP_CHANGED_EVENT, 2, (upgrade,),
                    command.occurred_at),),
                owners=self._owners, state_rows=self._state,
                result=CleanupItemSaved(
                    CleanupItemDisposition.SAVED, command.item_id))
        if item["status"] == int(_CLEANUP_ITEM_STATUS.DELETING):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupItemSaved(
                    CleanupItemDisposition.ALREADY, command.item_id))
        if (item["status"] != int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)
                or item["restriction_state"] != int(_CLEANUP_RESTRICTION.ACTIVE)):
            raise ConsistencyError("删除推进要求成员处于限制中")
        output_id = item["output_id"]
        output = self._state["outputs"].get(output_id)
        if output is None or output["cleanup_status"] != int(
                _OUTPUT_CLEANUP_STATUS.PENDING):
            raise ConsistencyError("删除推进要求产物投影处于待删除")
        reads = _ProductReads(connection, self._state)
        owner = _cleanup_group_call_owner(reads, item)
        if (not _cleanup_entry_ready(reads, item)
                or (owner is not None and owner["id"] != command.item_id)):
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                read_only=True, result=CleanupItemSaved(CleanupItemDisposition.WAITING, command.item_id))
        allocation = scope.allocate(2)
        progress = _update(
            "cleanup_items", command.item_id,
            {"status": int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)},
            {"status": int(_CLEANUP_ITEM_STATUS.DELETING)},
        )
        projection = _update(
            "outputs", output_id,
            {"cleanup_status": output["cleanup_status"]},
            {"cleanup_status": int(_OUTPUT_CLEANUP_STATUS.RUNNING)},
        )
        self._claim(item)
        self._owners[("outputs", output_id)] = ("output", output_id)
        events = (
            _envelope(allocation.first_event_id, allocation.txn_id,
                      _CLEANUP_CHANGED_EVENT, 2, (progress,),
                      command.occurred_at),
            _envelope(allocation.first_event_id + 1, allocation.txn_id,
                      _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                      (projection,), command.occurred_at),
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            read_coverage=reads.read_coverage(),
            result=CleanupItemSaved(
                CleanupItemDisposition.SAVED, command.item_id))

    def _verify_and_reuse(self, scope, saved, item):
        command = self._command
        kinds = [(event["type"], event["reason"]) for event in saved]
        if command.irreversible:
            # 不可撤销事实是单成员事件；不同输入整组拒绝。
            if kinds != [(24, 2)]:
                raise TransactionError("原事务不是不可撤销事实，不能作为重送核实")
            if saved[0]["occurred_at"] != command.occurred_at:
                raise TransactionError("不可撤销事实的时刻与原事务不同")
            row = saved[0]["body"]["rows"][0]
            if (row["table"] != "cleanup_items" or row["id"] != command.item_id
                    or row["after"]["values"].get("restriction_state")
                    != int(_CLEANUP_RESTRICTION.IRREVERSIBLE)):
                raise TransactionError("原不可撤销事实属于其他成员或输入不同")
            boundary = _member_values_at_saved_boundary(scope, saved, item)
            if (boundary.get("status") != int(_CLEANUP_ITEM_STATUS.DELETING)
                    or boundary.get("restriction_state")
                    != int(_CLEANUP_RESTRICTION.IRREVERSIBLE)):
                raise TransactionError("原不可撤销事实的完整边界与成员结果不符")
        else:
            # 取得归属：成员事件与产物投影同组；不同输入整组拒绝。
            if kinds != [(24, 2), (_OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON)]:
                raise TransactionError("原事务不是归属推进，不能作为重送核实")
            if any(event["occurred_at"] != command.occurred_at for event in saved):
                raise TransactionError("归属推进的事实时刻与原事务不同")
            row = saved[0]["body"]["rows"][0]
            if (row["table"] != "cleanup_items" or row["id"] != command.item_id
                    or row["after"]["values"].get("status")
                    != int(_CLEANUP_ITEM_STATUS.DELETING)):
                raise TransactionError("原归属推进属于其他成员")
            projection = saved[1]["body"]["rows"]
            if (len(projection) != 1 or projection[0]["table"] != "outputs"
                    or projection[0]["id"] != item["output_id"]
                    or projection[0]["after"]["values"].get("cleanup_status")
                    != int(_OUTPUT_CLEANUP_STATUS.RUNNING)):
                raise TransactionError("原归属推进的投影不属于本产物")
            boundary = _member_values_at_saved_boundary(scope, saved, item)
            if (boundary.get("status") != int(_CLEANUP_ITEM_STATUS.DELETING)
                    or boundary.get("restriction_state")
                    != int(_CLEANUP_RESTRICTION.ACTIVE)):
                raise TransactionError("原归属推进的完整边界与成员阶段不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, self._command.item_id))


def _values_at_saved_boundary(scope, saved, owner, table: str, row_id: int,
                              current_values: Mapping[str, Any]) -> dict[str, Any]:
    """按原事务边界恢复指定业务列；不用后来推进的当前值替代。"""
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.persistence.row_history import read_row_values_at_boundary

    transaction = saved[0]["transaction"]
    return read_row_values_at_boundary(
        scope.connection, owner=owner, table=table, row_id=row_id,
        columns=frozenset(current_values), current_values=current_values,
        boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
        current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))


def _member_values_at_saved_boundary(scope, saved, item) -> dict[str, Any]:
    """清理成员在原事务边界处的完整结果列。"""
    return _values_at_saved_boundary(
        scope, saved, ("action", item["action_id"]), "cleanup_items", item["id"], {
            "status": item["status"],
            "restriction_state": item["restriction_state"],
            "outcome": item["outcome"],
            "final_event_id": item["final_event_id"],
            "error_code": item["error_code"],
            "error_details_json": item["error_details_json"],
        })


def _member_delete_dispatch_state(connection, item_id: int) -> tuple[bool, bool]:
    """本成员自身 DELETE 尝试的派发事实：返回（可能派发, 尚未收场）。

    可能派发按逐尝试的可靠结果判定：任何非 `not_dispatched` 收场依
    据的已结束尝试都算可能派发；可靠未派发不撤销其他尝试。另一成
    员的 DELETE 不进入本成员分类（按 cleanup_item_id 单向关联）。
    """
    from camctl.outputs.cleanup_results import cleanup_member_delete_state
    return cleanup_member_delete_state(connection,item_id)


def _cleanup_entry_ready(reads, item):
    """读取完整共同来源范围后判断能否取得物理删除责任。"""
    from camctl.outputs.cleanup_group import cleanup_group

    group = cleanup_group(reads, item["output_id"])
    own = reads.members("cleanup_items", "action_id", item["action_id"])
    for output_id in group:
        if any(row.get("source_dependency") == 1
               for row in reads.members("obtain_items", "output_id", output_id).values()):
            return False
        for identity, row in reads.members("cleanup_items", "output_id", output_id).items():
            if identity != item["id"] and row.get("status") == int(_CLEANUP_ITEM_STATUS.DELETING):
                return False
        # 旧的已固定集合允许没有配对项；新集合的每一项必须先建立限制。
        for row in own.values():
            if row.get("requested_output_id") == output_id and row.get("status") == int(_CLEANUP_ITEM_STATUS.UNRESOLVED):
                return False
    return True


def _cleanup_group_call_owner(reads, item, *, exclude_self=False):
    """从同请求中最早可能派发的票据取得共同照片处理者。"""
    from camctl.outputs.cleanup_group import cleanup_group
    from camctl.operations.result_format import read_result_document

    if item["output_id"] is None:
        return None
    group = cleanup_group(reads, item["output_id"])
    if len(group) == 1:
        return None
    attempts = []
    for identity, member in reads.members("cleanup_items", "action_id", item["action_id"]).items():
        if exclude_self and identity == item["id"]:
            continue
        if member["requested_output_id"] not in group:
            continue
        for run_id, run in reads.members("operation_runs", "cleanup_item_id", identity).items():
            if run["kind"] not in (4, 5):
                continue
            for attempt in reads.members("operation_attempts", "run_id", run_id).values():
                attempts.append((attempt["intent_event_id"], attempt["id"], member, attempt))
    for _, _, member, attempt in sorted(attempts, key=lambda value: (value[0], value[1])):
        if attempt["status"] != 1:
            try:
                actual = read_result_document(attempt["status"], attempt["effect_state"],
                    attempt["result_json"], attempt["error_json"])
            except (TypeError, ValueError) as exc:
                raise ConsistencyError("共同照片处理尝试无法解释") from exc
            if actual.settlement.basis is SettlementBasis.NOT_DISPATCHED:
                continue
        return member
    return None


def cleanup_group_call_owner(connection, item_id):
    reads = _ProductReads(connection, {})
    return _cleanup_group_call_owner(reads, reads.required("cleanup_items", item_id))


class _BeginCleanupAttemptCommand(_CleanupItemCommandMixin):
    """清理首次处理归属、操作流程与第一次尝试的共同建档事务。

    完整已提交边界不能只有首次尝试却没有处理归属，也不能只有归属
    却缺少对应流程；成员进入删除中、流程创建与首次尝试登记在同一
    事务提交（cleanup-coordination 流程关联与按需创建）。预算或资
    格拒绝时不进入处理归属，成员保持待删除。
    """

    def __init__(self, intent: AttemptIntent, key: OperationKey) -> None:
        if not isinstance(intent, AttemptIntent):
            raise TypeError("清理首次建档申请必须使用 AttemptIntent")
        if intent.target is None or intent.target.cleanup_item_id is None:
            raise TransactionError("清理首次建档要求清理成员目标")
        self._intent = intent
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        intent = self._intent
        item_id = intent.target.cleanup_item_id
        saved = saved_transaction_events(connection, self._key)
        item = self._load_item(connection, item_id)
        if saved is not None:
            return self._reuse(scope, saved, item)
        action = self._state["actions"][item["action_id"]]
        if action["target_selection_state"] != _TARGET_FIXED:
            raise ConsistencyError("清理首次建档要求目标集合已固定")
        if intent.action_id != item["action_id"]:
            raise TransactionError("清理首次建档的意图与成员所属动作不符")
        if item["status"] == int(_CLEANUP_ITEM_STATUS.PENDING_DELETE):
            if item["restriction_state"] != int(_CLEANUP_RESTRICTION.ACTIVE):
                raise ConsistencyError("清理首次建档要求成员处于可撤销限制")
            # 新处理责任不建立在取消已生效的动作上；已删除中成员的
            # 后续尝试（含取消收场 QUERY）按各自资格规则检查。
            if (action["status"] != int(_ACTION_STATUS.RUNNING)
                    or action["cancel_requested"]):
                raise TransactionError("清理首次建档要求清理动作执行中且未取消")
            entry = True
        elif item["status"] == int(_CLEANUP_ITEM_STATUS.DELETING):
            # 已在处理阶段：首次使用另一种操作或既有流程的后续尝试。
            entry = False
        else:
            raise ConsistencyError("清理首次建档要求成员待删除或删除中")
        reads = _ProductReads(connection, self._state)
        owner = _cleanup_group_call_owner(reads, item)
        if owner is not None and owner["id"] != item_id:
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                read_only=True, result=BeginAttemptResult(BeginDisposition.REJECTED, reason="cleanup_waiting"))
        if entry and not _cleanup_entry_ready(reads, item):
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                read_only=True, result=BeginAttemptResult(BeginDisposition.REJECTED, reason="cleanup_waiting"))
        sub = _CleanupBindingScope(scope)
        begin = BeginAttemptCommand(intent, self._key).plan(sub)
        if not begin.events:
            # 预算或资格拒绝：归属、流程与尝试都不建立，成员保持待删除。
            return _merge_cleanup_binding_plans([
                CommandPlan(events=(), owners=self._owners,
                            state_rows=self._state, result=begin.result),
                begin,
            ], result=begin.result, read_only=True)
        plans = [CommandPlan(events=(), owners=self._owners, state_rows=self._state), begin]
        if entry:
            plans.append(_ProgressCleanupCommand(
                ProgressCleanupItem(item_id, intent.occurred_at), self._key,
            ).plan(sub))
        merged = _merge_cleanup_binding_plans(plans, result=begin.result, coverage=reads.read_coverage())
        allocation = scope.allocate(len(merged.events))
        if (merged.events[0].event_id != allocation.first_event_id
                or merged.events[-1].event_id != allocation.last_event_id):
            raise TransactionError("清理首次建档的事件组编号不连续")
        return merged

    def _reuse(self, scope, saved, item):
        intent = self._intent
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds not in ([(11, 1)], [(11, 1), (24, 2),
                                    (_OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON)]):
            raise TransactionError("原事务不是完整清理尝试建档，不能作为重送核实")
        if saved[0]["occurred_at"] != intent.occurred_at:
            raise TransactionError("原清理意图的事实时刻不同")
        rows = saved[0]["body"]["rows"]
        attempts = [row for row in rows if row["table"] == "operation_attempts"]
        runs = [row for row in rows if row["table"] == "operation_runs"]
        if (len(rows) != 2 or len(attempts) != 1 or len(runs) != 1
                or attempts[0]["before"]["exists"] or not attempts[0]["after"]["exists"]):
            raise TransactionError("原清理意图缺少唯一尝试及流程")
        values = attempts[0]["after"]["values"]
        expected = {"max_attempts_used": intent.config.max_attempts,
                    "timeout_s_json": intent.config.timeout_s,
                    "retry_interval_s_json": intent.config.retry_interval_s,
                    "copy_round": intent.copy_round}
        if (any(not json_equal(values.get(name), value) for name, value in expected.items())
                or runs[0]["id"] != values.get("run_id")):
            raise TransactionError("原清理意图的配置或流程与申请不同")
        if len(saved) == 3:
            # 首次建档同时取得归属并更新对应产物；复用已有完整核对。
            _ProgressCleanupCommand(
                ProgressCleanupItem(intent.target.cleanup_item_id, intent.occurred_at),
                self._key)._verify_and_reuse(scope, saved[1:], item)
        # 完整清理组已逐项核实；通用意图只核其中唯一的原意图片段。
        # 不能让它重新读取整个复合组并将归属事件当作意图外的异组。
        begin = BeginAttemptCommand(intent, self._key)._reuse(scope.connection, saved[:1])
        merged = _merge_cleanup_binding_plans([
            CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                        result=begin.result),
            begin,
        ])
        return replace(merged, read_only=True)


class _FinishCleanupAttemptCommand(_CleanupItemCommandMixin):
    """清理 DELETE 尝试结果与不可撤销限制事实的共同保存事务。

    删除调用取得可靠非未派发结果的同一事务把成员限制从可撤销升
    级为不可撤销；可靠未派发结果保持可撤销限制，由取消按未发出删
    除分类解除。迟到或原键重送沿原事务事实核实，不重复升级。
    """

    def __init__(self, finish: AttemptFinish, key: OperationKey) -> None:
        if not isinstance(finish, AttemptFinish):
            raise TypeError("清理尝试结果申请必须使用 AttemptFinish")
        self._finish = finish
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        finish = self._finish
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        item_id = int(finish.ticket.target_id)
        item = self._load_item(connection, item_id)
        sub = _CleanupBindingScope(scope)
        base = FinishAttemptCommand(finish, self._key).plan(sub)
        merged = _merge_cleanup_binding_plans([
            CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                        result=base.result),
            base,
        ])
        if not base.events:
            # 原键重送或迟到结果：限制事实已随原结果事务保存。
            return replace(merged, read_only=True)
        dispatched = (finish.outcome.outcome.settlement.basis
                      is not SettlementBasis.NOT_DISPATCHED)
        if (dispatched and item["status"] == int(_CLEANUP_ITEM_STATUS.DELETING)
                and item["restriction_state"] == int(_CLEANUP_RESTRICTION.ACTIVE)):
            upgrade = _ProgressCleanupCommand(
                ProgressCleanupItem(
                    item_id, finish.occurred_at, irreversible=True),
                self._key,
            ).plan(sub)
            merged = _merge_cleanup_binding_plans([merged, upgrade], result=base.result)
        allocation = scope.allocate(len(merged.events))
        if (merged.events[0].event_id != allocation.first_event_id
                or merged.events[-1].event_id != allocation.last_event_id):
            raise TransactionError("清理尝试结果的事件组编号不连续")
        return merged

    def _reuse(self, scope, saved):
        finish = self._finish
        split = len(saved)
        for index, event in enumerate(saved):
            if event["type"] == _CLEANUP_CHANGED_EVENT and event["reason"] == 2:
                split = index
                break
        upgrade = saved[split:]
        if len(upgrade) > 1 or any(
                (event["type"], event["reason"]) != (24, 2) for event in upgrade):
            raise TransactionError("原清理尝试结果事务包含其他清理事件")
        item_id = int(finish.ticket.target_id)
        if upgrade:
            row = upgrade[0]["body"]["rows"][0]
            if (row["table"] != "cleanup_items" or row["id"] != item_id
                    or row["after"]["values"].get("restriction_state")
                    != int(_CLEANUP_RESTRICTION.IRREVERSIBLE)
                    or upgrade[0]["occurred_at"] != finish.occurred_at):
                raise TransactionError("原不可撤销事实与本次结果保存不符")
        base = FinishAttemptCommand(finish, self._key)._reuse(scope, saved[:split])
        merged = _merge_cleanup_binding_plans([
            CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                        result=base.result),
            base,
        ])
        return replace(merged, read_only=True)


class _FinishCleanupItemCommand(_CleanupItemCommandMixin):
    """保存清理成功终态（存在性观察 + 可用性 CLEANED + 成员 SUCCEEDED）。

    删除中成员保存实际删除或缺席依据；待删除成员可在无调用依据下
    复用已有可靠完成事实（already_cleaned）或文件缺席事实直接成功。
    文件缺席事实、产物投影与成员终态同事务提交；成功依据由 cleanup
    守卫按同事务先行事件或已完成的删除调用核对。
    """

    def __init__(self, command: FinishCleanupItem, key: OperationKey) -> None:
        if not isinstance(command, FinishCleanupItem):
            raise TypeError("清理成功申请必须使用 FinishCleanupItem")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        item = self._load_item(connection, command.item_id)
        if saved is not None:
            return self._verify_and_reuse(scope, saved, item)
        if item["status"] in (int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
                              int(_CLEANUP_ITEM_STATUS.FAILED),
                              int(_CLEANUP_ITEM_STATUS.CANCELED)):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupItemSaved(
                    CleanupItemDisposition.ALREADY, command.item_id))
        output_id = item["output_id"]
        output = self._state["outputs"].get(output_id)
        if output is None:
            raise ConsistencyError(f"清理成功要求确认产物身份: {command.item_id}")
        from_wait = item["status"] == int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)
        if from_wait:
            # 待删除成员没有本项删除调用：只复用可靠完成或缺席事实。
            if item["restriction_state"] != int(_CLEANUP_RESTRICTION.ACTIVE):
                raise ConsistencyError("待删除成员的成功要求可撤销限制")
            if command.outcome is CleanupOutcomeChoice.DELETED:
                raise ConsistencyError("没有本项删除调用不能保存实际删除依据")
            if (command.outcome is CleanupOutcomeChoice.ALREADY_CLEANED
                    and output["availability"] != int(_AVAILABILITY.CLEANED)):
                raise ConsistencyError("已有完成依据要求产物已可靠清理")
        elif (item["status"] != int(_CLEANUP_ITEM_STATUS.DELETING)
                or item["restriction_state"] not in (
                    int(_CLEANUP_RESTRICTION.ACTIVE),
                    int(_CLEANUP_RESTRICTION.IRREVERSIBLE))):
            # 删除中成员在派发事实保存前仍为可撤销限制；成功终态本身
            # 保存不可撤销完成事实（查询确认或缺席依据同样适用）。
            raise ConsistencyError("清理成功要求成员处于删除中")
        device_file_id = output.get("device_file_id")
        intermediate_file_id = output.get("intermediate_file_id")
        templates = []
        if device_file_id is not None:
            file = row_facts(connection, "device_files", device_file_id)
            if file is None:
                raise ConsistencyError(f"设备文件不存在: {device_file_id}")
            self._state.setdefault("device_files", {})[device_file_id] = dict(file)
            if file["presence_state"] == int(_FILE_PRESENCE.PRESENT):
                if from_wait:
                    raise ConsistencyError(
                        "无本项删除调用的成功要求文件缺席事实先行")
                templates.append((17, 5, (_update(
                    "device_files", device_file_id,
                    {"presence_state": int(_FILE_PRESENCE.PRESENT)},
                    {"presence_state": int(_FILE_PRESENCE.ABSENT)}),),
                    ("device_file", device_file_id)))
            elif file["presence_state"] != int(_FILE_PRESENCE.ABSENT):
                raise ConsistencyError("文件存在性未知，不能确认清理成功")
        elif intermediate_file_id is not None:
            # 主机派生成品：文件缺席事实由清理完成状态承载；本次成
            # 功先保存清理完成事件，同事务先行供成功依据核对。
            file = row_facts(connection, "intermediate_files",
                             intermediate_file_id)
            if file is None:
                raise ConsistencyError(
                    f"中间文件不存在: {intermediate_file_id}")
            self._state.setdefault("intermediate_files", {})[
                intermediate_file_id] = dict(file)
            if file["cleanup_state"] == int(_FILE_CLEANUP.COMPLETED):
                pass
            elif file["cleanup_state"] in (
                    int(_FILE_CLEANUP.NOT_NEEDED),
                    int(_FILE_CLEANUP.PENDING)):
                if from_wait:
                    raise ConsistencyError(
                        "无本项删除调用的成功要求文件缺席事实先行")
                templates.append((
                    _INTERMEDIATE_FILE_EVENT, _CLEANUP_RESULT_REASON,
                    (_update(
                        "intermediate_files", intermediate_file_id,
                        {"cleanup_state": file["cleanup_state"]},
                        {"cleanup_state": int(_FILE_CLEANUP.COMPLETED)}),),
                    ("intermediate_file", intermediate_file_id)))
            else:
                raise ConsistencyError("中间文件清理状态未决，不能确认清理成功")
        from camctl.outputs.cleanup_group import cleanup_group
        reads = _ProductReads(connection, self._state)
        paired_changes = []
        for paired_id in cleanup_group(reads, output_id):
            if paired_id == output_id:
                continue
            paired = reads.required("outputs", paired_id)
            paired_file = reads.required("device_files", paired["device_file_id"])
            if (paired["availability"] == int(_AVAILABILITY.CLEANED)
                    and paired_file["presence_state"] != int(_FILE_PRESENCE.ABSENT)):
                raise ConsistencyError("共同照片的清理完成与文件事实矛盾")
            if paired_file["presence_state"] != int(_FILE_PRESENCE.ABSENT):
                templates.append((17, 5, (_update("device_files", paired_file["id"],
                    {"presence_state": paired_file["presence_state"]},
                    {"presence_state": int(_FILE_PRESENCE.ABSENT)}),),
                    ("device_file", paired_file["id"])))
            desired = {"availability": int(_AVAILABILITY.CLEANED),
                       "cleanup_status": int(_OUTPUT_CLEANUP_STATUS.COMPLETED),
                       "cleanup_error_json": None}
            before = {name: paired[name] for name, value in desired.items()
                      if not json_equal(paired[name], value)}
            if before:
                paired_changes.append((paired_id, before, {name: desired[name] for name in before}))
        self._load_delete_basis(connection, command.item_id)
        paired_source = None
        if paired_changes:
            from camctl.outputs.cleanup_results import saved_cleanup_absence
            candidates = [command.item_id]
            for member_id in cleanup_group(reads, output_id):
                candidates.extend(identity for identity in reads.members("cleanup_items", "output_id", member_id)
                                  if identity != command.item_id)
            for identity in candidates:
                candidate = reads.required("cleanup_items", identity)
                if identity != command.item_id and candidate["status"] not in (4, 5, 6):
                    continue
                evidence = saved_cleanup_absence(connection, identity)
                if evidence is not None:
                    self._load_delete_basis(connection, identity)
                    paired_source = {"cleanup_result_source": {"item_id": identity,
                        "result_event_id": evidence.result_event_id}}
                    break
            if paired_source is None:
                raise ConsistencyError("共同照片缺席缺少可靠原删除结果")
        after_item = {
            "id": item["id"], "action_id": item["action_id"],
            "requested_output_id": item["requested_output_id"],
            "output_id": output_id,
            "status": int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
            "restriction_state": int(_CLEANUP_RESTRICTION.IRREVERSIBLE),
            "outcome": command.outcome.value,
            "final_event_id": 0,  # 本事务终态事件，错误选择时优先。
            "error_code": None, "error_details_json": None,
        }
        derived = self._derive_projection(connection, output_id, after_item)
        change = self._projection_change(output, derived)
        allocation = scope.allocate(
            len(templates) + 1 + (1 if change else 0) + len(paired_changes))
        next_event_id = allocation.first_event_id
        events = []
        for event_type, reason, rows, owner in templates:
            for (table, row_id) in [(rows[0].table, rows[0].row_id)]:
                self._owners[(table, row_id)] = owner
            events.append(_envelope(
                next_event_id, allocation.txn_id, event_type, reason, rows,
                command.occurred_at,
                evidence=({"cleanup_request": {"advance_cursor": False}}
                    if event_type == _INTERMEDIATE_FILE_EVENT
                    and reason == _CLEANUP_RESULT_REASON else None)))
            next_event_id += 1
        succeed_event_id = next_event_id
        before = {
            "status": item["status"],
            "restriction_state": item["restriction_state"],
            "outcome": None, "final_event_id": None,
            "error_code": None, "error_details_json": None,
        }
        succeed = _update(
            "cleanup_items", command.item_id, before,
            {"status": int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
             "restriction_state": int(_CLEANUP_RESTRICTION.IRREVERSIBLE),
             "outcome": command.outcome.value,
             "final_event_id": succeed_event_id,
             "error_code": None, "error_details_json": None},
        )
        self._claim(item)
        self._owners[("outputs", output_id)] = ("output", output_id)
        events.append(_envelope(
            succeed_event_id, allocation.txn_id, _CLEANUP_CHANGED_EVENT, 3,
            (succeed,), command.occurred_at))
        if change:
            events.append(_envelope(
                succeed_event_id + 1, allocation.txn_id,
                _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                (_update("outputs", output_id, change[0], change[1]),),
                command.occurred_at))
        for paired_id, before, after in paired_changes:
            self._owners[("outputs", paired_id)] = ("output", paired_id)
            events.append(_envelope(
                allocation.first_event_id + len(events), allocation.txn_id,
                _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                (_update("outputs", paired_id, before, after),), command.occurred_at,
                evidence=paired_source))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            read_coverage=reads.read_coverage(),
            result=CleanupItemSaved(
                CleanupItemDisposition.SAVED, command.item_id))

    def _verify_and_reuse(self, scope, saved, item):
        command = self._command
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds.count((24, 3)) != 1:
            raise TransactionError("原事务不是清理成功，不能作为重送核实")
        succeed_index = kinds.index((24, 3))
        if any(event["occurred_at"] != command.occurred_at for event in saved):
            raise TransactionError("清理成功的事实时刻与原事务不同")
        rows = saved[succeed_index]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "cleanup_items"
                or rows[0]["id"] != command.item_id):
            raise TransactionError("原清理成功属于其他成员")
        after = rows[0]["after"]["values"]
        if (after.get("status") != int(_CLEANUP_ITEM_STATUS.SUCCEEDED)
                or after.get("outcome") != command.outcome.value
                or after.get("final_event_id") != saved[succeed_index]["event_id"]):
            raise TransactionError("原清理成功的输入与原事务不同")
        # 先行文件缺席事实只指向本产物承载文件；投影事件只指向本产物。
        from camctl.outputs.cleanup_group import cleanup_group
        reads = _ProductReads(scope.connection, self._state)
        group = cleanup_group(reads, item["output_id"])
        output = self._state["outputs"].get(item["output_id"])
        carried = set()
        for member in group:
            target = reads.required("outputs", member)
            if target.get("device_file_id") is not None:
                carried.add(("device_files", target["device_file_id"]))
            if target.get("intermediate_file_id") is not None:
                carried.add(("intermediate_files", target["intermediate_file_id"]))
        for event in saved[:succeed_index]:
            covered = {
                (row["table"], row["id"]) for row in event["body"]["rows"]}
            if not covered <= carried:
                raise TransactionError("原清理成功的前置事件不是本产物文件事实")
        for event in saved[succeed_index + 1:]:
            projection = event["body"]["rows"]
            if ((event["type"], event["reason"])
                    != (_OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON)
                    or len(projection) != 1 or projection[0]["table"] != "outputs"
                    or projection[0]["id"] not in group):
                raise TransactionError("原清理成功的投影不属于本产物")
            if projection[0]["id"] != item["output_id"]:
                evidence = event["body"].get("evidence", {}).get("cleanup_result_source")
                if not isinstance(evidence, Mapping):
                    raise TransactionError("共同照片投影缺少原缺席结果")
                source_item = reads.required("cleanup_items", evidence.get("item_id"))
                if (source_item["output_id"] not in group
                        or reads.required("outputs", projection[0]["id"])["created_event_id"] >= saved[0]["event_id"]):
                    raise TransactionError("共同照片投影不属于原事务前的配对")
                from camctl.outputs.cleanup_results import _read_exact_cleanup_attempt
                from camctl.persistence.repositories.cleanup_absence import _presence
                from camctl.operations.models import AttemptTicket
                fact = scope.connection.execute(
                    "SELECT a.attempt_no,r.kind,r.id,r.responsibility_key FROM operation_attempts a"
                    " JOIN operation_runs r ON r.id=a.run_id WHERE a.result_event_id=? AND r.cleanup_item_id=?",
                    (evidence.get("result_event_id"), source_item["id"])).fetchone()
                if fact is None or fact[1] not in (4, 5):
                    raise TransactionError("共同照片投影的原缺席结果无法核实")
                operation = "delete" if fact[1] == 4 else "query"
                source = _read_exact_cleanup_attempt(scope.connection,
                    AttemptTicket(fact[0], operation, str(source_item["id"]), fact[3], fact[2]), source_item["id"])
                if _presence(source.actual, operation, source_item["id"]) is not False:
                    raise TransactionError("共同照片投影的原结果未确认缺席")
        # 按原完整事务边界核对成员结果，不用后来推进的当前阶段替代。
        boundary = _member_values_at_saved_boundary(scope, saved, item)
        for name, value in (
            ("status", int(_CLEANUP_ITEM_STATUS.SUCCEEDED)),
            ("restriction_state", int(_CLEANUP_RESTRICTION.IRREVERSIBLE)),
            ("outcome", command.outcome.value),
            ("final_event_id", saved[succeed_index]["event_id"]),
            ("error_code", None),
            ("error_details_json", None),
        ):
            if not json_equal(boundary.get(name), value):
                raise TransactionError("原清理成功的完整边界与成员结果不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, command.item_id))


class _FailCleanupItemCommand(_CleanupItemCommandMixin):
    """保存清理失败终态（CLEANUP_CHANGED.FAIL，携带公共错误）。"""

    def __init__(self, command: FailCleanupItem, key: OperationKey) -> None:
        if not isinstance(command, FailCleanupItem):
            raise TypeError("清理失败申请必须使用 FailCleanupItem")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        item = self._load_item(connection, command.item_id)
        if saved is not None:
            return self._verify_and_reuse(scope, saved, item)
        if item["status"] in (int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
                              int(_CLEANUP_ITEM_STATUS.FAILED),
                              int(_CLEANUP_ITEM_STATUS.CANCELED)):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupItemSaved(
                    CleanupItemDisposition.ALREADY, command.item_id))
        if item["status"] == int(_CLEANUP_ITEM_STATUS.UNRESOLVED):
            raise ConsistencyError("未建立限制的成员不进入删除失败")
        output_id = item["output_id"]
        output = self._state["outputs"].get(output_id)
        if output is None:
            raise ConsistencyError(f"清理失败要求确认产物身份: {command.item_id}")
        error_id = item_error_id("cleanup_items", command.code)
        validate_error_details(command.code, command.details)
        reads = _ProductReads(connection, self._state)
        owner = _cleanup_group_call_owner(reads, item)
        if owner is not None and owner["id"] != command.item_id:
            if self._state["actions"][item["action_id"]]["cancel_requested"]:
                return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
                    read_only=True, result=CleanupItemSaved(CleanupItemDisposition.WAITING, command.item_id))
            details = dict(owner.get("error_details_json") or {})
            if "output_id" in details:
                details["output_id"] = str(output_id)
            if (owner["status"] != int(_CLEANUP_ITEM_STATUS.FAILED)
                    or owner["error_code"] != error_id
                    or not json_equal(details, command.details)):
                raise TransactionError("共同照片成员的失败与原处理者不一致")
        fail_before = {
            "status": item["status"],
            "restriction_state": item["restriction_state"],
            "outcome": None, "final_event_id": None,
            "error_code": None, "error_details_json": None,
        }
        after_item = {
            "id": item["id"], "action_id": item["action_id"],
            "requested_output_id": item["requested_output_id"],
            "output_id": output_id,
            "status": int(_CLEANUP_ITEM_STATUS.FAILED),
            "restriction_state": item["restriction_state"],
            "outcome": None, "final_event_id": 0,  # 本事务终态事件。
            "error_code": error_id, "error_details_json": dict(command.details),
        }
        derived = self._derive_projection(connection, output_id, after_item)
        change = self._projection_change(output, derived)
        allocation = scope.allocate(1 + (1 if change else 0))
        fail = _update(
            "cleanup_items", command.item_id, fail_before,
            {"status": int(_CLEANUP_ITEM_STATUS.FAILED),
             "restriction_state": item["restriction_state"],
             "outcome": None, "final_event_id": allocation.first_event_id,
             "error_code": error_id, "error_details_json": dict(command.details)},
        )
        self._claim(item)
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _CLEANUP_CHANGED_EVENT, 4, (fail,), command.occurred_at)]
        if change:
            self._owners[("outputs", output_id)] = ("output", output_id)
            events.append(_envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                (_update("outputs", output_id, change[0], change[1]),),
                command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            read_coverage=reads.read_coverage(),
            result=CleanupItemSaved(
                CleanupItemDisposition.SAVED, command.item_id))

    def _verify_and_reuse(self, scope, saved, item):
        command = self._command
        if [(event["type"], event["reason"]) for event in saved][0] != (24, 4):
            raise TransactionError("原事务不是清理失败，不能作为重送核实")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("清理失败的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        error_id = item_error_id("cleanup_items", command.code)
        if (row["table"] != "cleanup_items" or row["id"] != command.item_id):
            raise TransactionError("原清理失败属于其他成员")
        after = row["after"]["values"]
        if (after.get("error_code") != error_id
                or not json_equal(after.get("error_details_json"),
                                  dict(command.details))
                or after.get("status") != int(_CLEANUP_ITEM_STATUS.FAILED)
                or after.get("final_event_id") != saved[0]["event_id"]):
            raise TransactionError("原清理失败的输入与原事务不同")
        for event in saved[1:]:
            projection = event["body"]["rows"]
            if ((event["type"], event["reason"])
                    != (_OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON)
                    or len(projection) != 1 or projection[0]["table"] != "outputs"
                    or projection[0]["id"] != item["output_id"]):
                raise TransactionError("原清理失败的投影不属于本产物")
        # 按原完整边界核对成员结果与完整错误，不只比较错误编号。
        boundary = _member_values_at_saved_boundary(scope, saved, item)
        for name, value in (
            ("status", int(_CLEANUP_ITEM_STATUS.FAILED)),
            ("outcome", None),
            ("final_event_id", saved[0]["event_id"]),
            ("error_code", error_id),
            ("error_details_json", dict(command.details)),
        ):
            if not json_equal(boundary.get(name), value):
                raise TransactionError("原清理失败的完整边界与成员结果不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, command.item_id))


class _CancelCleanupItemCommand(_CleanupItemCommandMixin):
    """保存清理取消终态（CLEANUP_CHANGED.CANCEL）。

    未发出删除的成员解除可撤销限制；删除中的收场取消必须携带
    delete_unconfirmed 或 file_delete_failed 并保留不可撤销限制。
    产物汇总投影与成员终态同事务派生保存。
    """

    _SETTLEMENT_CODES = ("delete_unconfirmed", "file_delete_failed")

    def __init__(self, command: CancelCleanupItem, key: OperationKey) -> None:
        if not isinstance(command, CancelCleanupItem):
            raise TypeError("清理取消申请必须使用 CancelCleanupItem")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        saved = saved_transaction_events(connection, self._key)
        item = self._load_item(connection, command.item_id)
        if saved is not None:
            return self._verify_and_reuse(scope, saved, item)
        action = self._state["actions"][item["action_id"]]
        if action["target_selection_state"] != _TARGET_FIXED:
            raise ConsistencyError("成员取消要求目标集合已固定")
        if not action["cancel_requested"]:
            raise TransactionError("成员取消要求所属动作的取消请求已生效")
        if item["status"] in (int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
                              int(_CLEANUP_ITEM_STATUS.FAILED),
                              int(_CLEANUP_ITEM_STATUS.CANCELED)):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=CleanupItemSaved(
                    CleanupItemDisposition.ALREADY, command.item_id))
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND (a.status=1 OR a.result_json IS NULL) LIMIT 1",
            (command.item_id,))) as cursor:
            if cursor.fetchone() is not None:
                raise TransactionError("原删除或查询没有可靠结束结果，不能结束取消成员")
        error_id = None
        details = None
        if item["status"] == int(_CLEANUP_ITEM_STATUS.DELETING):
            dispatched, unsettled = _member_delete_dispatch_state(
                connection, command.item_id)
            if unsettled:
                raise TransactionError(
                    "本项删除尝试尚未可靠结束，不能结束取消成员")
            if not dispatched:
                # 本项可靠从未派发删除：无删除错误取消并解除本项可撤
                # 销限制；另一成员的删除事实不进入本项分类。
                if command.code is not None:
                    raise TransactionError(
                        "可靠未派发删除的成员取消不携带收场错误")
                after_restriction = int(_CLEANUP_RESTRICTION.RELEASED)
            else:
                if command.code is None:
                    raise TransactionError(
                        "删除中的成员取消必须携带收场错误，不经直接取消")
                if command.code not in self._SETTLEMENT_CODES:
                    raise TransactionError(
                        f"取消收场错误不在允许集合: {command.code!r}")
                if item["restriction_state"] != int(_CLEANUP_RESTRICTION.IRREVERSIBLE):
                    raise ConsistencyError(
                        "已派发删除的成员取消要求不可撤销限制")
                error_id = item_error_id("cleanup_items", command.code)
                validate_error_details(command.code, command.details)
                details = dict(command.details)
                if details.get("output_id") != str(item["output_id"]):
                    raise TransactionError("取消收场错误必须属于原清理成员的产物")
                from camctl.outputs.cleanup_flow import _output_delete_facts
                facts = _output_delete_facts(connection, item["output_id"])
                expected = "file_delete_failed" if facts["confirmed_present_after"] else "delete_unconfirmed"
                if facts["file_absent"] or command.code != expected:
                    raise TransactionError("取消收场错误与原删除和查询的可靠事实不符")
                after_restriction = int(_CLEANUP_RESTRICTION.IRREVERSIBLE)
        elif item["status"] in (int(_CLEANUP_ITEM_STATUS.UNRESOLVED),
                                int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)):
            if command.code is not None:
                raise TransactionError("未发出删除的取消不携带收场错误")
            if self._delete_attempts_exist(connection):
                raise ConsistencyError(
                    "已发出删除调用的成员按取消收场规则处理，不直接解除限制")
            after_restriction = (
                int(_CLEANUP_RESTRICTION.RELEASED)
                if item["status"] == int(_CLEANUP_ITEM_STATUS.PENDING_DELETE)
                else int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED))
        else:
            raise TransactionError(
                f"清理成员状态不可解释: {command.item_id} {item['status']!r}")
        output_id = item["output_id"]
        change = None
        output = self._state["outputs"].get(output_id)
        if output is None:
            if output_id is not None:
                raise ConsistencyError(f"产物不存在: {output_id}")
        after_item = {
            "id": item["id"], "action_id": item["action_id"],
            "requested_output_id": item["requested_output_id"],
            "output_id": output_id,
            "status": int(_CLEANUP_ITEM_STATUS.CANCELED),
            "restriction_state": after_restriction,
            "outcome": None, "final_event_id": 0,  # 本事务终态事件。
            "error_code": error_id, "error_details_json": details,
        }
        if output is not None:
            derived = self._derive_projection(connection, output_id, after_item)
            change = self._projection_change(output, derived)
        allocation = scope.allocate(1 + (1 if change else 0))
        cancel = _update(
            "cleanup_items", command.item_id,
            {"status": item["status"],
             "restriction_state": item["restriction_state"],
             "outcome": None, "final_event_id": None,
             "error_code": None, "error_details_json": None},
            {"status": int(_CLEANUP_ITEM_STATUS.CANCELED),
             "restriction_state": after_restriction,
             "outcome": None, "final_event_id": allocation.first_event_id,
             "error_code": error_id, "error_details_json": details},
        )
        self._claim(item)
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id,
            _CLEANUP_CHANGED_EVENT, 5, (cancel,), command.occurred_at)]
        if change:
            self._owners[("outputs", output_id)] = ("output", output_id)
            events.append(_envelope(
                allocation.first_event_id + 1, allocation.txn_id,
                _OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON,
                (_update("outputs", output_id, change[0], change[1]),),
                command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=CleanupItemSaved(
                CleanupItemDisposition.SAVED, command.item_id))

    def _delete_attempts_exist(self, connection) -> bool:
        """本成员是否已有任何可能删除文件的操作（不只最后一次）。"""
        delete_kind = int(enum_for("operation_runs.kind").DELETE_FILE)
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts a"
            " JOIN operation_runs r ON a.run_id = r.id"
            " WHERE r.cleanup_item_id = ? AND r.kind = ?"
            " LIMIT 1", (self._command.item_id, delete_kind),
        )) as cursor:
            return cursor.fetchone() is not None

    def _verify_and_reuse(self, scope, saved, item):
        command = self._command
        kinds = [(event["type"], event["reason"]) for event in saved]
        if kinds not in ([(24, 5)], [(24, 5), (_OUTPUT_REGISTERED_EVENT, _OUTPUT_OBSERVATION_REASON)]):
            raise TransactionError("原事务不是清理取消，不能作为重送核实")
        if any(event["occurred_at"] != command.occurred_at for event in saved):
            raise TransactionError("清理取消的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "cleanup_items" or rows[0]["id"] != command.item_id
                or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]):
            raise TransactionError("原清理取消属于其他成员")
        row = rows[0]
        prior_status = row["before"]["values"].get("status")
        released_restrictions = {
            int(_CLEANUP_ITEM_STATUS.UNRESOLVED): int(_CLEANUP_RESTRICTION.NOT_ESTABLISHED),
            int(_CLEANUP_ITEM_STATUS.PENDING_DELETE): int(_CLEANUP_RESTRICTION.RELEASED),
        }
        if prior_status == int(_CLEANUP_ITEM_STATUS.DELETING):
            # 历史只编码变化列：限制列缺省表示保持不可撤销（已派发分
            # 支不改限制），解除分支必然编码 RELEASED。
            prior_restriction = row["after"]["values"].get("restriction_state")
            if prior_restriction is None:
                prior_restriction = int(_CLEANUP_RESTRICTION.IRREVERSIBLE)
            if prior_restriction not in (
                    int(_CLEANUP_RESTRICTION.RELEASED),
                    int(_CLEANUP_RESTRICTION.IRREVERSIBLE)):
                raise TransactionError("原删除收场的限制分支不可解释")
            released_restrictions[int(_CLEANUP_ITEM_STATUS.DELETING)] = prior_restriction
        restrictions = released_restrictions
        if prior_status not in restrictions:
            raise TransactionError("原清理取消的处理分支不可解释")
        error_id, details = None, None
        if (prior_status == int(_CLEANUP_ITEM_STATUS.DELETING)
                and restrictions[prior_status]
                == int(_CLEANUP_RESTRICTION.IRREVERSIBLE)):
            if command.code not in self._SETTLEMENT_CODES:
                raise TransactionError("原删除收场不能复用未发出删除的取消输入")
            validate_error_details(command.code, command.details)
            details = dict(command.details)
            if details.get("output_id") != str(item["output_id"]):
                raise TransactionError("原清理取消的产物输入不同")
            error_id = item_error_id("cleanup_items", command.code)
        elif command.code is not None or command.details is not None:
            raise TransactionError("原未发出删除的取消输入不携带错误")
        # 历史更新只记录变化列；不可逆限制和空 outcome 保持时不重复编码。
        expected_before = {"status": prior_status, "final_event_id": None}
        expected_after = {"status": int(_CLEANUP_ITEM_STATUS.CANCELED),
                          "final_event_id": saved[0]["event_id"]}
        if (prior_status == int(_CLEANUP_ITEM_STATUS.DELETING)
                and restrictions[prior_status]
                == int(_CLEANUP_RESTRICTION.IRREVERSIBLE)):
            expected_before.update(error_code=None, error_details_json=None)
            expected_after.update(error_code=error_id, error_details_json=details)
        elif prior_status in (int(_CLEANUP_ITEM_STATUS.PENDING_DELETE),
                              int(_CLEANUP_ITEM_STATUS.DELETING)):
            # 待删除或可靠未派发删除的删除中成员：可撤销限制解除。
            expected_before["restriction_state"] = int(_CLEANUP_RESTRICTION.ACTIVE)
            expected_after["restriction_state"] = int(_CLEANUP_RESTRICTION.RELEASED)
        if (not json_equal(row["before"]["values"], expected_before)
                or not json_equal(row["after"]["values"], expected_after)
                or item["status"] != int(_CLEANUP_ITEM_STATUS.CANCELED)
                or item["restriction_state"] != restrictions[prior_status]
                or item["outcome"] is not None or item["final_event_id"] != saved[0]["event_id"]
                or item["error_code"] != error_id or not json_equal(item["error_details_json"], details)):
            raise TransactionError("清理取消的原键输入与完整结果不同")
        # 按原完整边界核对成员结果；不用后来成员或文件观察改写原取消。
        boundary = _member_values_at_saved_boundary(scope, saved, item)
        for name, value in (
            ("status", int(_CLEANUP_ITEM_STATUS.CANCELED)),
            ("restriction_state", restrictions[prior_status]),
            ("outcome", None),
            ("final_event_id", saved[0]["event_id"]),
            ("error_code", error_id),
            ("error_details_json", details),
        ):
            if not json_equal(boundary.get(name), value):
                raise TransactionError("原清理取消的完整边界与成员结果不符")
        if len(saved) == 2:
            projection = saved[1]["body"]["rows"]
            if (len(projection) != 1 or projection[0]["table"] != "outputs"
                    or projection[0]["id"] != item["output_id"]
                    or not projection[0]["before"]["exists"] or not projection[0]["after"]["exists"]):
                raise TransactionError("原清理取消的产物投影属于其他目标")
            # 原边界的投影结果与事件一致；后来的产物事实不参与解释。
            output = row_facts(scope.connection, "outputs", item["output_id"])
            if output is None:
                raise ConsistencyError(f"产物不存在: {item['output_id']}")
            self._state.setdefault("outputs", {})[item["output_id"]] = output
            output_boundary = _values_at_saved_boundary(
                scope, saved, ("output", item["output_id"]), "outputs",
                item["output_id"], {
                    "availability": output["availability"],
                    "cleanup_status": output["cleanup_status"],
                    "cleanup_error_json": output["cleanup_error_json"],
                })
            for name, value in projection[0]["after"]["values"].items():
                if not json_equal(output_boundary.get(name), value):
                    raise TransactionError("原清理取消的投影与完整边界不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupItemSaved(
                CleanupItemDisposition.ALREADY, self._command.item_id))



class _CleanupBindingScope:
    """成员与伴随流程共享事务开始状态，按顺序切分事件编号。"""

    def __init__(self, parent):
        self.connection = parent.connection
        self.max_event_id, self.max_txn_id = parent.max_event_id, parent.max_txn_id
        self._next = parent.max_event_id + 1

    def allocate(self, count):
        first = self._next
        self._next += count
        return TransactionAllocations(self.max_txn_id + 1, first, self._next - 1)


def _merge_cleanup_binding_plans(plans, *, read_only=False, coverage=None, result=None):
    state, owners = {}, {}
    for plan in plans:
        owners.update(plan.owners)
        for table, rows in plan.state_rows.items():
            target = state.setdefault(table, {})
            for identity, facts in rows.items():
                previous = target.setdefault(identity, {})
                if any(column in previous and not json_equal(previous[column], value)
                        for column, value in facts.items()):
                    raise TransactionError("清理复合事务的同一原记录事实矛盾")
                target[identity] = {**previous, **facts}
    return CommandPlan(events=tuple(event for plan in plans for event in plan.events),
        owners=owners, state_rows=state,
        result=result if result is not None else plans[0].result, read_only=read_only,
        read_coverage=coverage or ReadCoverage())


class _CleanupBindingFailCommand(_CleanupItemCommandMixin):
    """原成员、删除限制、产物投影与未完成流程的共同绑定失败。"""

    def __init__(self, command: FailCleanupBinding, key: OperationKey):
        self._command, self._key = command, key
        self._state, self._owners = {}, {}

    def _facts(self, connection):
        command = self._command
        item = self._load_item(connection, command.item_id)
        output = self._state["outputs"].get(item["output_id"])
        if output is None or output["device_file_id"] != command.source_device_file_id:
            raise TransactionError("清理绑定失败必须指向原成员的设备源文件")
        details = _load_binding_facts(connection, command.source_device_file_id,
                                     command.binding_result, self._state)
        self._load_delete_basis(connection, command.item_id)
        return item, details

    def _member_command(self, item, details):
        command = self._command
        if command.canceled:
            return _CancelCleanupItemCommand(CancelCleanupItem(
                command.item_id, command.occurred_at, "delete_unconfirmed",
                {"output_id": str(item["output_id"])}), self._key)
        return _FailCleanupItemCommand(FailCleanupItem(
            command.item_id, "device_binding_unavailable", details, command.occurred_at), self._key)

    def _flow_command(self, details):
        return _FinishStaleRunsCommand(StaleRunFinish(
            self._command.responsibility_keys, RunOutcome.FAILED, self._command.occurred_at,
            ErrorValue("device_binding_unavailable", "execution", details)), self._key)

    def plan(self, scope):
        saved = saved_transaction_events(scope.connection, self._key)
        item, details = self._facts(scope.connection)
        if saved is not None:
            return self._reuse(scope, saved, item, details)
        if item["status"] in (4, 5, 6):
            return CommandPlan(events=(), owners=self._owners, state_rows=self._state, read_only=True,
                               result=CleanupItemSaved(CleanupItemDisposition.ALREADY, item["id"]))
        action = self._state["actions"][item["action_id"]]
        if bool(action["cancel_requested"]) != self._command.canceled:
            raise TransactionError("清理绑定失败的取消分支与原动作不同")
        runs = self._state["operation_runs"]
        open_keys = {run["responsibility_key"] for run in runs.values() if run["status"] in (1, 2)}
        if open_keys != set(self._command.responsibility_keys):
            raise TransactionError("原成员的未完成责任集合与绑定失败输入不同")
        if self._command.canceled and not open_keys:
            raise TransactionError("取消未知删除无开放责任时使用原取消输入，不采用绑定失败")
        if any(attempt["status"] == 1 or attempt["result_json"] is None
               for attempt in self._state["operation_attempts"].values()):
            raise TransactionError("原清理调用没有可靠结束结果，不能用绑定失败代替收场")
        sub = _CleanupBindingScope(scope)
        member = self._member_command(item, details).plan(sub)
        plans = [member]
        if open_keys:
            plans.append(self._flow_command(details).plan(sub))
        merged = _merge_cleanup_binding_plans(plans)
        allocation = scope.allocate(len(merged.events))
        if (merged.events[0].event_id != allocation.first_event_id
                or merged.events[-1].event_id != allocation.last_event_id):
            raise TransactionError("清理绑定失败的事件组编号不连续")
        merged_state = _merge_cleanup_binding_plans([
            CommandPlan(events=(), owners=self._owners, state_rows=self._state, result=member.result), merged])
        return replace(merged_state, events=merged.events)

    def _reuse(self, scope, saved, item, details):
        command = self._command
        reason = 5 if command.canceled else 4
        if (not saved or (saved[0]["type"], saved[0]["reason"]) != (24, reason)
                or any(event["occurred_at"] != command.occurred_at for event in saved)):
            raise TransactionError("原清理绑定失败的成员分支或时间不同")
        rows = saved[0]["body"]["rows"]
        expected_details = {"output_id": str(item["output_id"])} if command.canceled else details
        expected_code = item_error_id("cleanup_items", "delete_unconfirmed" if command.canceled else "device_binding_unavailable")
        if (len(rows) != 1 or rows[0]["table"] != "cleanup_items" or rows[0]["id"] != command.item_id
                or rows[0]["after"]["values"].get("error_code") != expected_code
                or not json_equal(rows[0]["after"]["values"].get("error_details_json"), expected_details)):
            raise TransactionError("原清理绑定失败的成员输入不同")
        split = 1
        if len(saved) > 1 and saved[1]["type"] == _OUTPUT_REGISTERED_EVENT:
            event = saved[1]
            rows = event["body"]["rows"]
            if (event["reason"] != _OUTPUT_OBSERVATION_REASON or len(rows) != 1
                    or rows[0]["table"] != "outputs" or rows[0]["id"] != item["output_id"]):
                raise TransactionError("原清理绑定失败的投影不属于本产物")
            split = 2
        member_command = self._member_command(item, details)
        member = member_command._verify_and_reuse(scope, saved[:split], item)
        flow_events = saved[split:]
        expected_error = {"code": "device_binding_unavailable", "stage": "execution", "details": details}
        keys = []
        for event in flow_events:
            rows = event["body"]["rows"]
            if (event["type"], event["reason"]) != (10, 3) or len(rows) != 1 or rows[0]["table"] != "operation_runs":
                raise TransactionError("原清理绑定失败包含其他流程事件")
            run = self._state["operation_runs"].get(rows[0]["id"])
            if run is None or run["cleanup_item_id"] != command.item_id:
                raise TransactionError("原绑定失败的流程不属于本成员")
            if not json_equal(rows[0]["after"]["values"], {
                    "status": 4, "retry_wait_required": 0, "error_json": expected_error}):
                raise TransactionError("原清理绑定失败的流程结果或采用输入不同")
            keys.append(run["responsibility_key"])
        if len(set(keys)) != len(keys) or set(keys) != set(command.responsibility_keys):
            raise TransactionError("原清理绑定失败的完整责任集合不同")
        plans = [member]
        if flow_events:
            plans.append(self._flow_command(details)._reuse(scope, flow_events))
        return _merge_cleanup_binding_plans(plans, read_only=True)


class _FinishCleanupActionCommand:
    """清理动作汇总终态：全部成员终态后保存动作结果与父计划状态。

    任一成员最终失败（含不可解析目标的直接终态）按公共错误
    cleanup_items_failed 汇总为失败；全部成功才成功。取消已生效的
    动作不保存普通终态（取消收场由取消流程负责）。
    """

    def __init__(self, command: FinishCleanupAction, key: OperationKey) -> None:
        if not isinstance(command, FinishCleanupAction):
            raise TypeError("清理汇总申请必须使用 FinishCleanupAction")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "actions": {}, "cleanup_items": {}, "plans": {},
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
        if action["type"] != _DELETE_ACTION_TYPE:
            raise TransactionError(f"动作不是清理动作: {command.action_id}")
        self._state["actions"][command.action_id] = action
        if action["status"] in _ACTION_TERMINAL:
            return self._recover(connection, action)
        if action["status"] != int(_ACTION_STATUS.RUNNING):
            raise TransactionError(
                f"清理动作不在执行中: {command.action_id}"
                f" status={action['status']}")
        if action["cancel_requested"]:
            raise TransactionError(
                f"取消请求已生效的清理动作不能保存普通终态: {command.action_id}")
        if action["target_selection_state"] != _TARGET_FIXED:
            raise TransactionError(
                f"清理动作目标集合尚未固定: {command.action_id}")
        items = self._load_items(connection)
        if not items:
            raise TransactionError(
                f"清理动作没有可汇总成员: {command.action_id}")
        for item in items.values():
            if item["status"] == int(_CLEANUP_ITEM_STATUS.CANCELED):
                raise ConsistencyError(
                    "未取消的清理动作不应有已取消成员"
                    f" {command.action_id}/{item['id']}")
            if item["status"] not in (
                    int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
                    int(_CLEANUP_ITEM_STATUS.FAILED)):
                raise TransactionError(
                    f"清理成员尚未全部终态，不能保存汇总: {item['id']}"
                    f" status={item['status']}")
        failed = sum(
            1 for item in items.values()
            if item["status"] == int(_CLEANUP_ITEM_STATUS.FAILED))
        succeeded = len(items) - failed
        if failed:
            action_status = int(_ACTION_STATUS.FAILED)
            reason = 2
            error_code = registered_error(_CLEANUP_ITEMS_FAILED)["action_error_id"]
        else:
            action_status = int(_ACTION_STATUS.SUCCEEDED)
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
        if plan_complete(siblings, command.action_id) \
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
            result=CleanupActionFinished(
                disposition=CleanupActionDisposition.SAVED,
                action_status=action_status, plan_status=plan_status,
                succeeded=succeeded, failed=failed),
        )

    def _load_items(self, connection) -> dict[int, dict[str, Any]]:
        with closing(connection.execute(
            "SELECT id FROM cleanup_items WHERE action_id = ? ORDER BY id",
            (self._command.action_id,),
        )) as cursor:
            ids = tuple(int(row[0]) for row in cursor.fetchall())
        items = {}
        for item_id in ids:
            item = row_facts(connection, "cleanup_items", item_id)
            if item is not None:
                items[item_id] = item
                self._owners[("cleanup_items", item_id)] = (
                    "action", self._command.action_id)
        self._state["cleanup_items"] = dict(items)
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
        if action["status"] not in (int(_ACTION_STATUS.SUCCEEDED),
                                    int(_ACTION_STATUS.FAILED)):
            raise TransactionError(
                f"清理动作终态不是汇总结果: {action['status']!r}")
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return self._result_from(
            action, plan["status"], CleanupActionDisposition.ALREADY)

    def _result_from(self, action, plan_status, disposition) -> CommandPlan:
        items = self._state["cleanup_items"]
        failed = sum(
            1 for item in items.values()
            if item["status"] == int(_CLEANUP_ITEM_STATUS.FAILED))
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupActionFinished(
                disposition=disposition,
                action_status=action["status"],
                plan_status=plan_status, succeeded=len(items) - failed,
                failed=failed),
        )

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实事务身份后按既有事实恢复首次响应。"""
        kinds = [(event["type"], event["reason"]) for event in saved]
        if not kinds or kinds[0][0] != _ACTION_FINISHED_EVENT \
                or kinds[0][1] not in (1, 2) \
                or kinds[1:] not in ([], [(_PLAN_STATUS_EVENT, 2)]):
            raise TransactionError("原事务不是清理汇总登记，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError("清理汇总的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" \
                or action_row["id"] != self._command.action_id:
            raise TransactionError("原清理汇总属于其他动作")
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return self._result_from(
            action, plan["status"], CleanupActionDisposition.ALREADY)


class _FinishCanceledCleanupCommand:
    """取消的清理动作终态化：成员全部终态后保存动作取消终态。

    取消已生效且成员全部终态（成功、失败或取消，含目标集合未固
    定的零成员）时由取消结算调用；兄弟齐终态时同事务保存父计划
    完成。已终态后重入按既有事实只读恢复。
    """

    def __init__(self, command: FinishCanceledCleanupAction,
                 key: OperationKey) -> None:
        if not isinstance(command, FinishCanceledCleanupAction):
            raise TypeError(
                "取消终态化申请必须使用 FinishCanceledCleanupAction")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {
            "actions": {}, "cleanup_items": {}, "plans": {},
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
        if action["type"] != _DELETE_ACTION_TYPE:
            raise TransactionError(f"动作不是清理动作: {command.action_id}")
        self._state["actions"][command.action_id] = action
        if action["status"] in _ACTION_TERMINAL:
            return self._recover(connection, action)
        if action["status"] != int(_ACTION_STATUS.RUNNING) \
                or not action["cancel_requested"]:
            raise TransactionError(
                f"取消终态化要求执行中且已请求取消的清理动作:"
                f" {command.action_id}"
                f" status={action['status']}"
                f" cancel_requested={action['cancel_requested']}")
        items = self._load_items(connection)
        for item in items.values():
            if item["status"] not in (
                    int(_CLEANUP_ITEM_STATUS.SUCCEEDED),
                    int(_CLEANUP_ITEM_STATUS.FAILED),
                    int(_CLEANUP_ITEM_STATUS.CANCELED)):
                raise TransactionError(
                    f"清理成员尚未全部终态，不能取消终态化: {item['id']}"
                    f" status={item['status']}")
        siblings = self._load_siblings(connection, action)
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id)
        templates = [(
            _ACTION_FINISHED_EVENT, 4,
            (_update("actions", command.action_id,
                     {"status": action["status"]},
                     {"status": int(_ACTION_STATUS.CANCELED)}),),
        )]
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        plan_status = plan["status"]
        if plan_complete(siblings, command.action_id) \
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
            result=CleanupActionFinished(
                disposition=CleanupActionDisposition.SAVED,
                action_status=int(_ACTION_STATUS.CANCELED),
                plan_status=plan_status,
                succeeded=self._succeeded(), failed=self._failed()),
        )

    def _load_items(self, connection) -> dict[int, dict[str, Any]]:
        with closing(connection.execute(
            "SELECT id FROM cleanup_items WHERE action_id = ? ORDER BY id",
            (self._command.action_id,),
        )) as cursor:
            ids = tuple(int(row[0]) for row in cursor.fetchall())
        items = {}
        for item_id in ids:
            item = row_facts(connection, "cleanup_items", item_id)
            if item is not None:
                items[item_id] = item
                self._owners[("cleanup_items", item_id)] = (
                    "action", self._command.action_id)
        self._state["cleanup_items"] = dict(items)
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

    def _succeeded(self) -> int:
        return sum(
            1 for item in self._state["cleanup_items"].values()
            if item["status"] == int(_CLEANUP_ITEM_STATUS.SUCCEEDED))

    def _failed(self) -> int:
        return sum(
            1 for item in self._state["cleanup_items"].values()
            if item["status"] == int(_CLEANUP_ITEM_STATUS.FAILED))

    def _recover(self, connection, action) -> CommandPlan:
        """终态后的新键：按既有事实恢复结果，不重新登记。"""
        if action["status"] != int(_ACTION_STATUS.CANCELED):
            raise TransactionError(
                f"取消终态化的动作终态不是取消: {action['status']!r}")
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupActionFinished(
                disposition=CleanupActionDisposition.ALREADY,
                action_status=action["status"],
                plan_status=plan["status"],
                succeeded=self._succeeded(), failed=self._failed()),
        )

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实事务身份后按既有事实恢复首次响应。"""
        kinds = [(event["type"], event["reason"]) for event in saved]
        if not kinds or kinds[0][0] != _ACTION_FINISHED_EVENT \
                or kinds[0][1] != 4 \
                or kinds[1:] not in ([], [(_PLAN_STATUS_EVENT, 2)]):
            raise TransactionError(
                "原事务不是清理取消终态化登记，不能作为重送核实")
        if saved[0]["occurred_at"] != self._command.occurred_at:
            raise TransactionError(
                "清理取消终态化的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" \
                or action_row["id"] != self._command.action_id:
            raise TransactionError("原清理取消终态化属于其他动作")
        action = row_facts(connection, "actions", self._command.action_id)
        assert action is not None
        self._load_items(connection)
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CleanupActionFinished(
                disposition=CleanupActionDisposition.ALREADY,
                action_status=action["status"],
                plan_status=plan["status"],
                succeeded=self._succeeded(), failed=self._failed()),
        )




@dataclass(frozen=True)
class EnsureWithdrawalWaiter:
    """为仍有效的取消目标登记一份原交付撤回明细。"""

    cancel_item_id: int
    delivery_id: int
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.cancel_item_id)
        ObjectId(self.delivery_id)
        UtcMicros(self.occurred_at)


class _EnsureWithdrawalWaiterCommand:
    """按原取消成员和交付登记唯一明细；已结束父责任不再创建。"""

    def __init__(self, command, key):
        if not isinstance(command, EnsureWithdrawalWaiter):
            raise TypeError("撤回等待明细申请必须使用 EnsureWithdrawalWaiter")
        self._command, self._key = command, key

    def _eligible(self, delivery, target, parent, origin):
        return (delivery["id"] == self._command.delivery_id
                and parent["id"] == self._command.cancel_item_id
                and delivery["action_id"] == target["id"] == parent["target_action_id"]
                and target["type"] == _OBTAIN_TYPE and target["cancel_requested"] == 1
                and (delivery["status"] in (int(_DELIVERY_STATUS.PUBLISHED), int(_DELIVERY_STATUS.WITHDRAWN))
                     or delivery["status"] in (int(_DELIVERY_STATUS.PUBLISHING), int(_DELIVERY_STATUS.CANCELED))
                     and delivery["withdrawal_state"] == int(_WITHDRAWAL.UNKNOWN))
                and origin["id"] == parent["action_id"] and origin["type"] == _CANCEL_ACTION_TYPE
                and origin["status"] == int(_ACTION_STATUS.RUNNING)
                and origin["cancel_requested"] == 0 and parent["status"] == 2)

    def plan(self, scope):
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        parent = row_facts(connection, "cancel_items", self._command.cancel_item_id)
        delivery = row_facts(connection, "deliveries", self._command.delivery_id)
        if parent is None or delivery is None:
            raise ConsistencyError("撤回等待明细缺少原取消成员或交付")
        target = row_facts(connection, "actions", delivery["action_id"])
        origin = row_facts(connection, "actions", parent["action_id"])
        if target is None or origin is None:
            raise ConsistencyError("撤回等待明细缺少原动作")
        if (delivery["action_id"] != parent["target_action_id"]
                or target["type"] != _OBTAIN_TYPE or origin["type"] != _CANCEL_ACTION_TYPE):
            raise ConsistencyError("撤回等待明细的原取消目标与交付不一致")
        if not self._eligible(delivery, target, parent, origin):
            return CommandPlan(events=(), owners={}, state_rows={}, read_only=True, result=False)
        with closing(connection.execute(
            "SELECT id FROM cancel_delivery_items WHERE cancel_item_id=? AND delivery_id=?",
            (parent["id"], delivery["id"]),
        )) as cursor:
            existing = cursor.fetchall()
        if len(existing) > 1:
            raise ConsistencyError("撤回等待明细的原关联不唯一")
        if existing:
            return CommandPlan(events=(), owners={}, state_rows={}, read_only=True, result=True)
        identity = next_row_id(connection, "cancel_delivery_items")
        values = {"cancel_item_id": parent["id"], "delivery_id": delivery["id"],
                  "status": 1, "error_code": None, "error_details_json": None}
        allocation = scope.allocate(1)
        event = _envelope(allocation.first_event_id, allocation.txn_id,
            _DELIVERY_CHANGED_EVENT, 7, (_row("cancel_delivery_items", identity, values),),
            self._command.occurred_at)
        state = {"actions": {target["id"]: target, origin["id"]: origin},
                 "cancel_items": {parent["id"]: parent}, "deliveries": {delivery["id"]: delivery},
                 "cancel_delivery_items": {}}
        return CommandPlan(events=(event,), state_rows=state,
            owners={("cancel_delivery_items", identity): ("action", origin["id"])}, result=True)

    def _reuse(self, scope, saved):
        if (len(saved) != 1 or saved[0]["type"] != _DELIVERY_CHANGED_EVENT
                or saved[0]["reason"] != 7 or saved[0]["occurred_at"] != self._command.occurred_at
                or saved[0]["body"]["evidence"] != {}):
            raise TransactionError("原操作不是相同的撤回等待明细申请")
        rows = saved[0]["body"]["rows"]
        expected = {"cancel_item_id": self._command.cancel_item_id,
                    "delivery_id": self._command.delivery_id, "status": 1,
                    "error_code": None, "error_details_json": None}
        if (len(rows) != 1 or rows[0]["table"] != "cancel_delivery_items"
                or rows[0]["before"] != {"exists": False, "values": {}}
                or not rows[0]["after"]["exists"] or rows[0]["after"]["values"] != expected):
            raise TransactionError("原撤回等待明细与本申请不同")
        connection = scope.connection
        created = row_facts(connection, "cancel_delivery_items", rows[0]["id"])
        parent = row_facts(connection, "cancel_items", self._command.cancel_item_id)
        delivery = row_facts(connection, "deliveries", self._command.delivery_id)
        if (created is None or parent is None or delivery is None
                or created["cancel_item_id"] != parent["id"]
                or created["delivery_id"] != delivery["id"]):
            raise ConsistencyError("原撤回等待明细的固定关联不一致")
        target = row_facts(connection, "actions", delivery["action_id"])
        origin = row_facts(connection, "actions", parent["action_id"])
        if target is None or origin is None:
            raise ConsistencyError("原撤回等待明细缺少原动作")
        transaction = saved[0]["transaction"]
        prior = read_transaction_range(connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        boundary = HistoryBoundary(0, 0) if prior is None else HistoryBoundary(prior.txn_id, prior.last_event_id)
        current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)
        for table, facts, owner, columns in (
                ("deliveries", delivery, ("delivery", delivery["id"]), ("status", "withdrawal_state")),
                ("actions", target, ("action", target["id"]), ("status", "cancel_requested")),
                ("cancel_items", parent, ("action", origin["id"]), ("status",)),
                ("actions", origin, ("action", origin["id"]), ("status", "cancel_requested"))):
            facts.update(read_row_values_at_boundary(connection, owner=owner, table=table,
                row_id=facts["id"], columns=frozenset(columns), current_values=facts,
                boundary=boundary, current_boundary=current))
        if not self._eligible(delivery, target, parent, origin):
            raise TransactionError("原撤回等待明细创建时父责任不符合资格")
        return CommandPlan(events=(), owners={}, state_rows={}, read_only=True, result=True)


class _AdvanceWithdrawalCommand:
    """交付独立撤回与全部必要等待明细在同一事实中结束。"""

    def __init__(self, command: AdvanceWithdrawal, key: OperationKey) -> None:
        if not isinstance(command, AdvanceWithdrawal):
            raise TypeError("撤回推进申请必须使用 AdvanceWithdrawal")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}
        self._items: list[dict] = []

    def _creation(self, scope, item, owner):
        """无独立元字段的明细沿实际原拥有者核实唯一创建事实。"""
        ref_type = load_enum_registry()["history_objects"][owner[0]]["id"]
        with closing(scope.connection.execute(
            "SELECT e.id,e.transaction_id,e.event_type,e.event_version,e.occurred_at,"
            " e.clock_status,e.change_seq,e.body_json FROM entity_event_links l"
            " JOIN history_events e ON e.id=l.event_id"
            " WHERE l.entity_type=? AND l.entity_id=? AND EXISTS ("
            " SELECT 1 FROM json_each(e.body_json,'$.rows') r"
            " WHERE json_extract(r.value,'$.table')='cancel_delivery_items'"
            " AND json_extract(r.value,'$.id')=?"
            " AND json_extract(r.value,'$.before.exists')=0"
            " AND json_extract(r.value,'$.after.exists')=1) ORDER BY e.id LIMIT 2",
            (ref_type, owner[1], item["id"]),
        )) as cursor:
            stored = cursor.fetchall()
        if len(stored) != 1:
            raise ConsistencyError("撤回明细缺少原拥有者的唯一创建事实")
        event = decode_event_row(stored[0])
        created = [row for row in event.rows if row.table == "cancel_delivery_items"
                   and row.row_id == item["id"] and not row.before.exists and row.after.exists]
        if (len(created) != 1 or any(created[0].after.values.get(name) != item[name]
                for name in ("cancel_item_id", "delivery_id"))):
            raise ConsistencyError("撤回明细创建事实与当前原关联不同")
        transaction = read_transaction_range(scope.connection, event.transaction_id)
        if not transaction.first_event_id <= event.event_id <= transaction.last_event_id <= scope.max_event_id:
            raise ConsistencyError("撤回明细创建事实不属于可靠完整事务")
        return transaction

    def _load(self, scope, boundary=None):
        connection = scope.connection
        delivery = row_facts(connection, "deliveries", self._command.delivery_id)
        if delivery is None:
            raise ConsistencyError(f"交付不存在: {self._command.delivery_id}")
        self._state = {"deliveries": {delivery["id"]: delivery},
                       "cancel_delivery_items": {}, "cancel_items": {}}
        self._owners[("deliveries", delivery["id"])] = ("delivery", delivery["id"])

        def original(table, facts, owner, columns):
            if boundary is not None:
                facts.update(read_row_values_at_boundary(connection,
                    owner=owner, table=table, row_id=facts["id"], columns=frozenset(columns),
                    current_values=facts, boundary=boundary,
                    current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id)))

        original("deliveries", delivery, ("delivery", delivery["id"]),
                 ("status", "withdrawal_state", "withdrawal_error_json"))
        with closing(connection.execute(
            "SELECT c.id,ci.action_id FROM cancel_delivery_items c"
            " JOIN cancel_items ci ON ci.id=c.cancel_item_id"
            " WHERE c.delivery_id=? ORDER BY c.id", (delivery["id"],),
        )) as cursor:
            identities = cursor.fetchall()
        self._items = []
        for item_id, owner_id in identities:
            item = row_facts(connection, "cancel_delivery_items", int(item_id))
            if item is None:
                raise ConsistencyError("原交付撤回明细缺失")
            owner = ("action", int(owner_id))
            if boundary is not None:
                creation = self._creation(scope, item, owner)
                if creation.last_event_id > boundary.last_event_id:
                    # 创建晚于原 H 是明确不存在的依据。仍沿当前 C
                    # 核实其目录及创建后的原值，不把历史错误当不存在。
                    initial = read_row_values_at_boundary(connection, owner=owner,
                        table="cancel_delivery_items", row_id=item["id"],
                        columns=frozenset(("status", "error_code", "error_details_json")),
                        current_values=item, boundary=HistoryBoundary(creation.txn_id, creation.last_event_id),
                        current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
                    if initial != {"status": 1, "error_code": None, "error_details_json": None}:
                        raise ConsistencyError("后到撤回明细的原创建状态不完整")
                    continue
            original("cancel_delivery_items", item, owner,
                     ("status", "error_code", "error_details_json"))
            if item["status"] != 1:
                continue
            parent = row_facts(connection, "cancel_items", item["cancel_item_id"])
            if parent is None or parent["target_action_id"] != delivery["action_id"]:
                raise ConsistencyError("原交付撤回明细与取消目标不一致")
            origin = row_facts(connection, "actions", parent["action_id"])
            if origin is None or origin["id"] != owner[1]:
                raise ConsistencyError("原交付撤回明细缺少所属取消动作")
            original("cancel_items", parent, owner, ("status",))
            original("actions", origin, owner, ("status", "cancel_requested"))
            if parent["status"] != 2 or origin["status"] != 2 or origin["cancel_requested"]:
                continue
            self._state.setdefault("actions", {})[origin["id"]] = origin
            self._state["cancel_items"][parent["id"]] = parent
            self._state["cancel_delivery_items"][item["id"]] = item
            self._owners[("cancel_delivery_items", item["id"])] = owner
            self._items.append(item)
        return delivery

    def _rows(self, delivery):
        command, state = self._command, delivery["withdrawal_state"]
        if command.choice is WithdrawalChoice.REQUESTED:
            if state != int(_WITHDRAWAL.NOT_REQUESTED):
                return ()
            return (_update("deliveries", delivery["id"], {"withdrawal_state": state},
                            {"withdrawal_state": int(_WITHDRAWAL.PENDING)}),)
        choice_states = {WithdrawalChoice.WITHDRAWN: _WITHDRAWAL.WITHDRAWN,
                         WithdrawalChoice.NOT_RETRACTABLE: _WITHDRAWAL.NOT_RETRACTABLE,
                         WithdrawalChoice.FAILED: _WITHDRAWAL.FAILED,
                         WithdrawalChoice.UNKNOWN: _WITHDRAWAL.UNKNOWN}
        result_state = int(choice_states[command.choice])
        failed = command.choice in (WithdrawalChoice.FAILED, WithdrawalChoice.UNKNOWN)
        rows = []
        if state == int(_WITHDRAWAL.PENDING):
            before, after = {"withdrawal_state": state}, {"withdrawal_state": result_state}
            if command.choice is WithdrawalChoice.WITHDRAWN:
                before["status"], after["status"] = delivery["status"], int(_DELIVERY_STATUS.WITHDRAWN)
            if failed:
                before["withdrawal_error_json"] = delivery["withdrawal_error_json"]
                after["withdrawal_error_json"] = dict(command.error)
            rows.append(_update("deliveries", delivery["id"], before, after))
        elif state == result_state:
            # 新请求采用原交付责任，不改写已结束的实际结果。
            if not json_equal(delivery["withdrawal_error_json"], command.error):
                raise TransactionError("后到撤回请求采用的原错误依据不同")
        else:
            return ()
        if command.choice is WithdrawalChoice.WITHDRAWN:
            item_status, code, details = 2, None, None
        elif command.choice is WithdrawalChoice.NOT_RETRACTABLE:
            item_status, code, details = 3, None, None
        else:
            name = ("delivery_withdrawal_failed" if command.choice is WithdrawalChoice.FAILED
                    else "delivery_position_unconfirmed")
            item_status, code = 4, item_error_id("cancel_delivery_items", name)
            details = {"delivery_id": str(delivery["id"])}
            validate_error_details(name, details)
        for item in self._items:
            rows.append(_update("cancel_delivery_items", item["id"],
                {"status": item["status"], "error_code": item["error_code"],
                 "error_details_json": item["error_details_json"]},
                {"status": item_status, "error_code": code, "error_details_json": details}))
        return tuple(rows)

    def plan(self, scope) -> CommandPlan:
        saved = saved_transaction_events(scope.connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        delivery = self._load(scope)
        rows = self._rows(delivery)
        if not rows:
            return self._already()
        allocation = scope.allocate(1)
        event = _envelope(allocation.first_event_id, allocation.txn_id,
            _DELIVERY_CHANGED_EVENT, 7, rows, self._command.occurred_at)
        return CommandPlan(events=(event,), owners=self._owners, state_rows=self._state,
            result=CleanupItemSaved(CleanupItemDisposition.SAVED, self._command.delivery_id))

    def _already(self) -> CommandPlan:
        return CommandPlan(events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=CleanupItemSaved(CleanupItemDisposition.ALREADY, self._command.delivery_id))

    def _reuse(self, scope, saved) -> CommandPlan:
        if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"]) != (_DELIVERY_CHANGED_EVENT, 7)
                or saved[0]["occurred_at"] != self._command.occurred_at
                or saved[0]["body"]["evidence"] != {}):
            raise TransactionError("原事务不是相同时刻的完整撤回推进")
        transaction = saved[0]["transaction"]
        prior = read_transaction_range(scope.connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        boundary = HistoryBoundary(0, 0) if prior is None else HistoryBoundary(prior.txn_id, prior.last_event_id)
        delivery = self._load(scope, boundary)
        rows = self._rows(delivery)
        expected = [{"table": row.table, "id": row.row_id,
            "before": {"exists": row.before.exists, "values": dict(row.before.values)},
            "after": {"exists": row.after.exists, "values": dict(row.after.values)}} for row in rows]
        if not expected or not json_equal(saved[0]["body"]["rows"], expected):
            raise TransactionError("原撤回事务的交付、分支、错误或完整等待集合不同")
        return self._already()


def register_outputs_guards() -> None:
    """注册来源、选择与读取资格事件的正式业务守卫（装配期调用）。"""
    register_guard("selection_initialization", _selection_initialization_guard)
    register_guard("source_selection", _source_selection_guard)
    register_guard("intermediate", _intermediate_guard)
    register_guard("cursor", _cursor_guard)
    register_guard("delivery", _delivery_guard)
    register_guard("copy", _copy_guard)
    from .read_end import read_end_guard
    register_guard("read_end", read_end_guard)
    register_guard("copy_links", _copy_links_guard)
    register_guard("read_slot", _read_slot_guard)
    register_guard("read_permission", _read_permission_guard)
    register_guard("processing", _processing_guard)
    register_guard("recording_source", _recording_source_guard)
    register_guard("obtain_member", _obtain_member_guard)
    register_guard("target_set", _target_set_guard)
    register_guard("cleanup_member", _cleanup_member_guard)
    register_guard("cleanup", _cleanup_guard)
    from camctl.persistence.repositories.cleanup_absence import cleanup_result_source_guard
    register_guard("cleanup_result_source", cleanup_result_source_guard)
    from camctl.persistence.repositories.segment_queries import (
        register_segment_query_guards,
    )

    register_segment_query_guards()
