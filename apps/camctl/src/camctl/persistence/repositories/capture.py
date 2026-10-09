"""录像完成终态与录像内部处理的事务。

经 P3 事务内核组织：动作成功终态、正式产物登记（含修复成品承载
文件的共同提升）及父计划状态在同一事务共同保存，任一写入失败整
组回滚；登记前经 X1 纯规则校验，事件守卫从当前文件事实复核来源、
原设备绑定、文件角色、修复成功依据、初始可用性组合与提升配对。
录像内部处理提供决定与结果保存，以及修复输出的登记与完成事务：
路径登记先于文件写入，完整字节与修复成功同事务固定。停止、活动
结束与产物、动作结果分别保存，不互相混同。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping

from camctl.capture.models import (
    ActivityConcludeSave,
    ActivityObservationSave,
    ActivityReleaseSave,
    ResultRunClose,
    ResultSetPhase,
    ResultSetSave,
)
from camctl.capture.files import (
    FileChecksumSave,
    FileCompletionSave,
    FileObservationSave,
    FilePresenceSave,
    ObservationDisposition,
    ObservationOutcome,
    OwnershipSave,
    file_identity_key,
)
from camctl.capture.media import (
    RecordingFailure, RecordingOutcomeFacts, RecordingResultKind,
    decide_recording_result,
)
from camctl.capture.result_inputs import files_from_outcome, saved_outcome
from camctl.capture.processing import (
    CheckDecisionSave,
    CheckReason,
    CheckResultSave,
    DiscardPhase,
    DiscardProgressSave,
    ProcessingDisposition,
    ProcessingOutcome,
    RepairDecisionSave,
    RepairOutcome,
    RepairOutputFile,
    RepairResultSave,
    RepairStart,
    RepairSuccess,
    SourceFileSave,
    saved_check_duration,
)
from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord, RecordStatus
from camctl.capture.results import (
    ActivityFacts, ActivityState, FileKind, OccupancyState, ReleaseDecision, decide_release,
)
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.contracts.workflow_errors import (
    registered_error,
    validate_error_details,
)
from camctl.history.reads import ReadCoverage
from camctl.history.events import business_columns
from camctl.history.validators import EventValidationError, register_guard
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import relative_file_path
from camctl.outputs.catalog import (
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
    RegistrationChanges,
    validate_output_registration,
)
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    FinishAttemptResult,
    RunOutcome,
    StaleRunFinish,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture_facts import (
    include_start_facts, load_start_facts, release_basis_holds,
    unstarted_events, verify_unstarted_final,
)
from camctl.persistence.repositories.operations import (
    FinishAttemptCommand, _FinishStaleRunsCommand, _RETRY_WAIT_EVENT,
)
from camctl.persistence.repositories.scheduling import (
    ExpireActionCommand, ExpireActionRequest, ExpireOutcome,
)
from camctl.operations.models import AttemptTicket, EffectState, ErrorValue
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionAllocations,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id as _next_id,
    row_change as _row,
    row_facts,
    read_transaction_range,
    saved_transaction_events,
    update_change as _update,
)

_ACTION_FINISHED_EVENT = 8
_OUTPUT_REGISTERED_EVENT = 20
_PLAN_STATUS_EVENT = 9
_EMERGENCY_RECORDED_EVENT = 33
_RECORDING_DECIDED_EVENT = 18
_RECORDING_PROCESSED_EVENT = 19
_INTERMEDIATE_FILE_EVENT = 26
_DEVICE_FILE_EVENT = 17

#: 尝试结果与流程收场事件（OPERATION_ATTEMPT_CONCLUDED / OPERATION_CONFIGURED.FINISH）。
_ATTEMPT_RESULT_EVENT = 12
_RUN_END_EVENT = 10

_RUN_STATUS = enum_for("operation_runs.status")
_RUN_KIND = enum_for("operation_runs.kind")
_QUERY_PURPOSE = enum_for("operation_runs.query_purpose")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_ACTIVITY_STATE = enum_for("device_activities.activity_state")

#: DEVICE_OBSERVED 的 OBSERVE 与 RELEASE 分支。
_ACTIVITY_OBSERVE_EVENT = 13
_ACTIVITY_OBSERVE_REASON = 2
_ACTIVITY_RELEASE_REASON = 3

#: RESULT_SET_CONFIRMED 的四个分支。
_RESULT_SET_EVENT = 16
_RESULT_COMPLETE_REASON = 1
_RESULT_UNSATISFIED_REASON = 2
_RESULT_UNCONFIRMED_REASON = 3
_RESULT_BEGIN_REASON = 4

#: 结论分支到（事件分支编号、目标核实状态、结果判定）的映射。
_RESULT_PHASE_TARGETS = {
    ResultSetPhase.COMPLETE: (_RESULT_COMPLETE_REASON, 3, 1),
    ResultSetPhase.UNSATISFIED: (_RESULT_UNSATISFIED_REASON, 3, 2),
    ResultSetPhase.UNCONFIRMED: (_RESULT_UNCONFIRMED_REASON, 4, 3),
    ResultSetPhase.BEGIN: (_RESULT_BEGIN_REASON, 2, None),
}

#: 核实状态的合法转换（登记状态模型；3 与 4 无出边）。
_RESULT_SET_NEXT = {
    1: frozenset({2, 3, 4}),
    2: frozenset({3, 4}),
}

#: 采集判定依据的方法标识与目标判定（operation-fields.md#设备活动字段）。
_TIME_AND_OUTPUTS_METHOD = "time_and_outputs"
_DEVICE_EVIDENCE_METHOD = "device_evidence"
_KNOWN_FAILURE_METHOD = "known_failure"

_ACTIVITY_DISPATCH = enum_for("device_activities.dispatch_state")
_ACTIVITY_STATE = enum_for("device_activities.activity_state")

#: 活动观察的合法状态转换（登记状态模型）。
_ACTIVITY_DISPATCH_NEXT = {
    1: frozenset({2}),
    2: frozenset({1, 3, 4}),
    4: frozenset({2}),
}
_ACTIVITY_STATE_NEXT = {
    1: frozenset({2}),
    2: frozenset({3}),
}

#: DEVICE_FILE_OBSERVED 的五个分支。
_FILE_CREATE_REASON = 1
_FILE_OWNERSHIP_REASON = 2
_FILE_COMPLETE_REASON = 3
_FILE_CHECKSUM_REASON = 4
_FILE_PRESENCE_REASON = 5

_FILE_PRESENCE = enum_for("device_files.presence_state")
_FILE_COMPLETION = enum_for("device_files.completion_state")
_FILE_CHECKSUM = enum_for("device_files.checksum_support")
_OWNERSHIP_METHOD = enum_for("device_files.ownership_evidence_json.method")
_COMPLETION_BASIS = enum_for("device_files.completion_evidence_json.basis")
_PAIRING_METHOD = enum_for("device_files.pairing_evidence_json.method")

#: 文件形成状态的合法转换（登记状态模型；COMPLETE 无出边）。
_FILE_COMPLETION_NEXT = {
    1: frozenset({2, 3, 4}),
    2: frozenset({3, 4}),
    4: frozenset({3}),
}

#: INTERMEDIATE_FILE_CHANGED.LIFECYCLE：保存完整字节及保留用途变化。
_LIFECYCLE_REASON = 2

_RETENTION = enum_for("intermediate_files.retention_state")
_PURPOSE = enum_for("intermediate_files.purpose")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")

#: RECORDING_DECIDED 的三个分支。
_DECIDED_CHECK_REASON = 1
_DECIDED_REPAIR_REASON = 2
_DECIDED_SOURCE_REASON = 3

#: RECORDING_PROCESSED 的三个分支。
_PROCESSED_CHECK_REASON = 1
_PROCESSED_REPAIR_REASON = 2
_PROCESSED_DISCARD_REASON = 3

_ACTION_RUNNING = 2
_ACTION_SUCCEEDED = 3
_ACTION_CANCELED = 6
_ACTION_FAILED = 4
_PLAN_COMPLETE = 3

#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

_OUTPUT_KIND = enum_for("outputs.kind")
_KIND_CODES = {kind: int(_OUTPUT_KIND[kind.name]) for kind in OutputKind}


def _unchecked_media() -> dict[str, Any]:
    """登记时产物文件尚无媒体检查：公共 media 结构表达未检查与未知时长。"""
    return {"check_status": "not_performed", "duration": {"status": "unknown"}}

#: device_files.role 与产物种类的对应；修复产物承载于中间文件。
_FILE_ROLE = enum_for("device_files.role")
_FILE_ROLE_FOR_KIND = {_OUTPUT_KIND.ORIGINAL: _FILE_ROLE.ORIGINAL, _OUTPUT_KIND.PREVIEW: _FILE_ROLE.PREVIEW}
_ACTION_TYPE = enum_for("actions.type")
_CAPTURE_TYPES = frozenset({
    _ACTION_TYPE.CAMERA_TAKE_PHOTO,
    _ACTION_TYPE.CAMERA_RECORD,
    _ACTION_TYPE.CAMERA_TIMELAPSE,
})


@dataclass(frozen=True)
class FinishCapture:
    """一次录像完成登记的完整输入：终态事实与全部适用产物。

    failure 为空保存成功终态；携带失败时按公共动作错误码保存执行
    失败终态，产物登记与失败事实同事务提交。
    """

    action_id: int
    drafts: tuple[OutputDraft, ...]
    catalog_facts: OutputCatalogFacts
    occurred_at: int
    failure: RecordingFailure | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)


@dataclass(frozen=True)
class FinishRecordingResults:
    """原录像文件核实已结束调用后的共同收场输入。

    capture 保存完整的动作与产物申请，run_id 固定原活动的唯一
    RESULTS 身份。原尝试已经保存，不再次提交或修改尝试结果。
    """

    capture: FinishCapture
    run_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.capture, FinishCapture):
            raise TypeError("录像核实收场必须携带完整 FinishCapture")
        ObjectId(self.run_id)


@dataclass(frozen=True)
class FinishCanceledCapture:
    """一次取消终态登记的输入：已拍完文件成为正式产物，其余放弃。

    要求取消标记已先保存（结束事务在事务内检查该标记）；只有已
    确认归属且写入完成的草稿登记为正式产物，与取消终态同事务提
    交。不提供草稿时本次取消不登记任何产物（如录像放弃内容）。
    """

    action_id: int
    occurred_at: int
    drafts: tuple[OutputDraft, ...] = ()
    catalog_facts: OutputCatalogFacts | None = None
    #: 可靠未启动的本地收场：事务内复核原尝试，与适用占用释放共同保存。
    unstarted: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        if not isinstance(self.unstarted, bool):
            raise TypeError("未启动收场标记必须是布尔值")
        if self.unstarted and (self.drafts or self.catalog_facts is not None):
            raise ValueError("未启动收场不登记拍摄产物")


@dataclass(frozen=True)
class FinishBindingFailure:
    """原设备绑定异常时，拍摄结果与必要流程共同结束的完整输入。"""

    action_id: int
    occurred_at: int
    failure: RecordingFailure
    canceled: bool = False
    stop_config: AttemptConfig | None = None
    responsibility_keys: tuple[str, ...] = ()
    check_config: AttemptConfig | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        if self.failure.code != "device_binding_unavailable":
            raise ValueError("绑定失败事务只保存设备绑定异常")
        validate_error_details(self.failure.code, self.failure.details)
        if not isinstance(self.canceled, bool):
            raise TypeError("取消分支必须是布尔值")
        if (not isinstance(self.responsibility_keys, tuple)
                or any(not isinstance(key, str) or not key for key in self.responsibility_keys)
                or len(set(self.responsibility_keys)) != len(self.responsibility_keys)):
            raise ValueError("绑定失败的责任集合必须是互不重复的非空责任键")


@dataclass(frozen=True)
class FinishResidualBindingFailure:
    """原残留绑定失败的固定责任，及适用的新触发动作失败。"""

    action_id: int | None
    activity_id: int
    occurred_at: int
    failure: RecordingFailure
    responsibility_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action_id is not None:
            ObjectId(self.action_id)
        ObjectId(self.activity_id)
        if self.failure.code != "device_binding_unavailable":
            raise ValueError("原残留绑定事务必须提供原设备绑定错误")
        validate_error_details(self.failure.code, self.failure.details)
        if (not isinstance(self.responsibility_keys, tuple) or not self.responsibility_keys
                or any(not isinstance(key, str) or not key for key in self.responsibility_keys)
                or len(set(self.responsibility_keys)) != len(self.responsibility_keys)):
            raise ValueError("原残留绑定事务必须固定互不重复的非空责任键")


class FinishDisposition(Enum):
    """完成申请的处理结果：新保存、复用原结果或可靠退役。"""

    SAVED = "saved"
    ALREADY = "already"
    RETIRED = "retired"


@dataclass(frozen=True)
class CaptureResult:
    """完成登记的已保存事实。"""

    action_status: int
    plan_status: int
    output_ids: tuple[int, ...]
    disposition: FinishDisposition = FinishDisposition.SAVED


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
    """从事件当前事实核对来源、原设备绑定、文件角色与提升配对。"""
    if event.event_type == _INTERMEDIATE_FILE_EVENT:
        _promotion_pairing(event, context)
        return
    for row in event.rows:
        if row.table == "outputs" and not row.before.exists:
            _output_file(row.after.values, context)
    _output_relationships(event, context)


def _promotion_pairing(event, context) -> None:
    """INTERMEDIATE_FILE_CHANGED.LIFECYCLE：提升只因同事务登记发生。

    推进到 PROMOTED 的中间文件要求先前事件已登记承载它的修复成
    品；释放与交接的保留变化不经本核对。
    """
    if event.reason != _LIFECYCLE_REASON:
        return
    for row in event.rows:
        if row.table != "intermediate_files":
            continue
        if row.after.values.get("retention_state") != int(_RETENTION.PROMOTED):
            continue
        file = context.state_rows.get("intermediate_files", {}).get(row.row_id)
        if file is None:
            raise EventValidationError(
                f"提升缺少当前文件事实: intermediate_files#{row.row_id}")
        owner = file.get("owner_action_id")
        for output in context.state_rows.get("outputs", {}).values():
            if (output.get("kind") == _KIND_CODES[OutputKind.REPAIRED]
                    and output.get("intermediate_file_id") == row.row_id
                    and output.get("source_action_id") == owner):
                break
        else:
            raise EventValidationError(
                "文件提升必须由同事务先行的修复成品登记授权:"
                f" intermediate_files#{row.row_id}")


def _output_file(values, context) -> None:
    """新登记产物及所引用的既有原片共用文件身份与来源校验。"""
    source_id = values["source_action_id"]
    source = _registration_facts(context, "actions", source_id)
    source_binding = _capture_binding(source)
    kind = values.get("kind")
    if not {"device_file_id", "intermediate_file_id"} <= values.keys():
        raise EventValidationError("产物承载文件类型尚未完整读取")
    device_file_id = values.get("device_file_id")
    if (device_file_id is None) == (values.get("intermediate_file_id") is None):
        raise EventValidationError("产物必须恰好由一种文件身份承载")
    if device_file_id is not None:
        facts = _registration_facts(context, "device_files", device_file_id)
        if facts.get("source_action_id") != source_id or facts.get("ownership_evidence_json") is None:
            raise EventValidationError("产物承载文件缺少该来源动作的可靠归属")
        observer = _registration_facts(context, "actions", facts.get("observer_action_id"))
        if _capture_binding(observer) != source_binding:
            raise EventValidationError("产物来源与文件观察者的原设备或驱动绑定不一致")
        expected_role = _FILE_ROLE_FOR_KIND.get(kind)
        if expected_role is None or facts.get("role") != expected_role:
            raise EventValidationError(f"产物种类与文件角色不符: kind={kind} role={facts.get('role')}")
        if facts.get("completion_state") != 3:
            raise EventValidationError(f"产物承载文件未完成: {device_file_id}")
        if kind == _OUTPUT_KIND.ORIGINAL and (
            "original_device_file_id" not in facts or "pairing_evidence_json" not in facts
            or facts["original_device_file_id"] is not None or facts["pairing_evidence_json"] is not None
        ):
            raise EventValidationError("原片文件必须明确没有预览配对")
    else:
        if kind != _OUTPUT_KIND.REPAIRED:
            raise EventValidationError("只有修复成品使用中间文件")
        facts = _registration_facts(context, "intermediate_files", values.get("intermediate_file_id"))
        if (facts.get("owner_action_id") != source_id
                or "owner_delivery_id" not in facts
                or facts["owner_delivery_id"] is not None):
            raise EventValidationError("产物中间文件不属于来源动作的文件责任")
        _require_promotable_repair_output(facts)
        _require_repair_success(context, source_id, values.get("intermediate_file_id"))


def _require_promotable_repair_output(facts) -> None:
    """修复成品的承载文件必须仍是未释放的完整修复输出。"""
    if facts.get("purpose") != int(_PURPOSE.REPAIR_OUTPUT):
        raise EventValidationError(
            f"修复成品承载文件不是修复输出用途: {facts.get('purpose')!r}")
    if facts.get("retention_state") != int(_RETENTION.REQUIRED):
        raise EventValidationError(
            "修复成品承载文件必须尚未释放、提升或交接:"
            f" {facts.get('retention_state')!r}")
    if facts.get("size_bytes") is None or facts.get("sha256") is None:
        raise EventValidationError("修复成品承载文件缺少完整字节事实")


def _require_repair_success(context, source_id: int, file_id) -> None:
    """登记以本动作的修复成功事实为前提；缺失或指向他处均拒绝。"""
    for processing in context.state_rows.get("recording_processing", {}).values():
        if (processing.get("action_id") == source_id
                and processing.get("repair_state") == RepairOutcome.SUCCEEDED.value
                and processing.get("repair_output_file_id") == file_id):
            return
    raise EventValidationError(
        f"产物登记缺少本动作的修复成功事实: intermediate_files#{file_id}")


def _output_relationships(event, context) -> None:
    """登记自己的关系与当前完整反向集合共同决定原片及派生唯一性。"""
    outputs = {row.row_id: row.after.values for row in event.rows
               if row.table == "outputs" and not row.before.exists}
    links: dict[int, list[Mapping[str, Any]]] = {}
    for row in event.rows:
        if row.table != "output_origins":
            continue
        values = row.after.values
        if values["output_id"] not in outputs:
            raise EventValidationError("关联必须属于本事件共同登记的产物")
        links.setdefault(values["output_id"], []).append(values)
    kinds_by_original: dict[int, set[int]] = {}
    for identity, output in outputs.items():
        if context.complete_rows("output_origins", "output_id", identity):
            raise EventValidationError("新产物不能已有原片关联")
        own_links = links.get(identity, ())
        kind = output["kind"]
        if kind == _OUTPUT_KIND.ORIGINAL:
            if own_links:
                raise EventValidationError("原片不能携带派生关联")
            continue
        if len(own_links) != 1:
            raise EventValidationError("派生产物必须共同登记唯一原片关联")
        original_id = own_links[0]["original_output_id"]
        if original_id == identity:
            raise EventValidationError("派生产物不能引用自身")
        original = _registration_facts(context, "outputs", original_id)
        if (original.get("kind") != _OUTPUT_KIND.ORIGINAL
                or original.get("source_action_id") != output["source_action_id"]
                or original.get("device_file_id") is None
                or original.get("intermediate_file_id") is not None):
            raise EventValidationError("派生关联必须指向同源的设备原片")
        if context.complete_rows("output_origins", "output_id", original_id):
            raise EventValidationError("引用的原片不能携带派生关联")
        _output_file(original, context)
        if kind == _OUTPUT_KIND.PREVIEW:
            file = _registration_facts(context, "device_files", output["device_file_id"])
            if (file.get("original_device_file_id") != original["device_file_id"]
                    or file.get("pairing_evidence_json") is None):
                raise EventValidationError("预览的产物关联与设备文件配对不一致")
        if original_id not in kinds_by_original:
            related = context.complete_rows("output_origins", "original_output_id", original_id)
            kinds: set[int] = set()
            for relation in related.values():
                previous = _registration_facts(context, "outputs", relation["output_id"])
                previous_kind = previous.get("kind")
                if (previous_kind not in {_OUTPUT_KIND.PREVIEW, _OUTPUT_KIND.REPAIRED}
                        or previous_kind in kinds
                        or previous.get("source_action_id") != output["source_action_id"]):
                    raise EventValidationError("已有原片派生集合的种类或来源矛盾")
                kinds.add(previous_kind)
            kinds_by_original[original_id] = kinds
        kinds = kinds_by_original[original_id]
        if kind in kinds:
            raise EventValidationError("同一原片至多登记一份预览和一份修复成品")
        kinds.add(kind)


def _registration_facts(context, table: str, identity: int | None) -> Mapping[str, Any]:
    facts = context.state_rows.get(table, {}).get(identity)
    if facts is None:
        raise EventValidationError(f"产物登记缺少当前关联记录: {table}#{identity}")
    return facts


def _capture_binding(action: Mapping[str, Any]) -> tuple[str, str]:
    if action.get("type") not in _CAPTURE_TYPES:
        raise EventValidationError("产物来源和设备文件观察者必须是拍摄动作")
    device, driver = action.get("device_id"), action.get("driver_id")
    if not isinstance(device, str) or not device or not isinstance(driver, str) or not driver:
        raise EventValidationError("拍摄动作缺少已保存的设备或驱动绑定")
    return device, driver


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
    register_guard("device_file", _device_file_guard)
    register_guard("emergency", _emergency_guard)
    register_guard("activity", _activity_guard)
    register_guard("release", _release_guard)
    register_guard("result_check", _result_check_guard)


class FinishCaptureCommand:
    """一次拍摄完成登记的完整事务命令。

    canceled 模式服务已生效取消的执行中动作：终态为取消，登记已
    拍完且确认完成的草稿（未提供草稿则不登记产物）。普通模式要
    求动作未取消，终态为成功或失败。
    """

    def __init__(self, command, key: OperationKey, *,
                 canceled: bool = False) -> None:
        self._canceled = canceled
        self._unstarted = canceled and command.unstarted
        self._failure = None if canceled else command.failure
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}
        self._ranges: dict[tuple[str, str], set[int]] = {}
        self._origin_members: dict[tuple[str, int], tuple[int, ...]] = {}

    def plan(self, scope, *, start_result_events=()) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)

        command = self._command
        action = row_facts(connection, "actions", command.action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {command.action_id}")
        if action["status"] in _ACTION_TERMINAL:
            if self._unstarted:
                verify_unstarted_final(connection, action)
            return self._recover(connection, action)
        siblings = self._sibling_actions(connection, action)
        self._state["actions"] = dict(siblings)
        self._state.setdefault("outputs", {})
        self._state.setdefault("device_files", {})
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        if action["status"] != _ACTION_RUNNING or (
                bool(action["cancel_requested"]) != self._canceled):
            raise TransactionError(
                f"执行中动作的取消标记与完成登记分支不符:"
                f" {command.action_id} status={action['status']}"
                f" cancel_requested={action['cancel_requested']}")
        unstarted = None
        if self._unstarted:
            original_start = load_start_facts(connection, action)
            unstarted = load_start_facts(connection, action, result_events=start_result_events)
            if not unstarted.not_started:
                raise ConsistencyError("本地取消收场缺少可靠未启动依据")
            if unstarted.run is not None and unstarted.run["status"] in (1, 2):
                raise ConsistencyError("本地取消收场前普通启动责任必须已随取消结束")
            include_start_facts(original_start, self._state, self._owners)
        action_status = _ACTION_SUCCEEDED
        error_id: int | None = None
        error_details: dict[str, Any] | None = None
        if self._canceled:
            action_status = _ACTION_CANCELED
        elif self._failure is not None:
            spec = registered_error(self._failure.code)
            if "action_error_id" not in spec:
                raise TransactionError(
                    f"失败错误码不是动作错误: {self._failure.code!r}")
            validate_error_details(self._failure.code, self._failure.details)
            action_status = _ACTION_FAILED
            error_id = spec["action_error_id"]
            error_details = dict(self._failure.details)

        # 登记规则在同一事务内校验：任一草稿不合法整组拒绝。
        _capture_binding(action)
        if command.catalog_facts is None:
            if not self._canceled or command.drafts:
                raise TransactionError("产物登记缺少目录上下文")
            changes = RegistrationChanges(outputs=())
        else:
            if command.catalog_facts.action_id != command.action_id:
                raise TransactionError("目录上下文与完成命令的动作身份不一致")
            changes = validate_output_registration(
                command.drafts, command.catalog_facts)
        for output in changes.outputs:
            self._load_file(connection, output.device_file_id, output.intermediate_file_id)
        self._verify_repaired_outputs(connection, changes)

        first_output_id = _next_id(connection, "outputs")
        numbered = tuple((first_output_id + index, output) for index, output in enumerate(changes.outputs))
        batch_originals = {output.device_file_id: identity for identity, output in numbered
                           if output.kind is OutputKind.ORIGINAL}
        original_ids = {}
        for identity, output in numbered:
            self._read_origins(connection, "output_id", identity)
            original_id = output.original_output_id
            if original_id is not None:
                original = self._required(connection, "outputs", original_id)
                self._load_file(connection, original["device_file_id"], original["intermediate_file_id"])
            elif output.original_batch_file_id is not None:
                original_id = batch_originals[output.original_batch_file_id]
            if original_id is not None:
                self._read_origins(connection, "output_id", original_id)
                for relation in self._read_origins(connection, "original_output_id", original_id):
                    self._required(connection, "outputs", relation["output_id"])
                original_ids[identity] = original_id

        if self._canceled:
            action_row = _update(
                "actions",
                command.action_id,
                {"status": action["status"]},
                {"status": _ACTION_CANCELED},
            )
            action_reason = 4
        elif self._failure is None:
            action_row = _update(
                "actions",
                command.action_id,
                {"status": action["status"]},
                {"status": _ACTION_SUCCEEDED},
            )
            action_reason = 1
        else:
            action_row = _update(
                "actions",
                command.action_id,
                {
                    "status": action["status"],
                    "error_code": action["error_code"],
                    "error_details_json": action["error_details_json"],
                },
                {
                    "status": _ACTION_FAILED,
                    "error_code": error_id,
                    "error_details_json": error_details,
                },
            )
            action_reason = 2
        templates = [
            _envelope(
                0, 0, _ACTION_FINISHED_EVENT, action_reason,
                (action_row,),
                command.occurred_at,
            )
        ]
        if unstarted is not None:
            templates.extend(unstarted_events(
                unstarted, command.occurred_at, run_status=None))
        next_origin_id = _next_id(connection, "output_origins")
        # 原片先进入当前事件事实，派生关系按显式引用解析；结果保持输入次序。
        promoted_files: list[int] = []
        for output_id, output in sorted(numbered, key=lambda item: item[1].kind is not OutputKind.ORIGINAL):
            name, media_type = self._readable_metadata(connection, output_id, output, original_ids)
            values = {
                "source_action_id": command.action_id,
                "kind": _KIND_CODES[output.kind],
                "device_file_id": output.device_file_id,
                "intermediate_file_id": output.intermediate_file_id,
                "original_name": name,
                "media_type": media_type,
                "availability": 1,
                "cleanup_status": 1,
                "cleanup_error_json": None,
                "media_json": _unchecked_media(),
                "error_json": None,
            }
            self._owners[("outputs", output_id)] = ("output", output_id)
            rows = (_row("outputs", output_id, values),)
            if output_id in original_ids:
                rows += (_row("output_origins", next_origin_id, {
                    "output_id": output_id, "original_output_id": original_ids[output_id],
                }),)
                self._owners[("output_origins", next_origin_id)] = ("output", output_id)
                next_origin_id += 1
            templates.append(
                _envelope(
                    0, 0, _OUTPUT_REGISTERED_EVENT, _KIND_CODES[output.kind],
                    rows, command.occurred_at,
                )
            )
            if output.kind is OutputKind.REPAIRED:
                promoted_files.append(output.intermediate_file_id)
        # 登记事件先行；承载文件在同事务提升为正式产物保留状态。
        for file_id in promoted_files:
            self._owners[("intermediate_files", file_id)] = ("intermediate_file", file_id)
            templates.append(
                _envelope(
                    0, 0, _INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON,
                    (_update(
                        "intermediate_files", file_id,
                        {"retention_state": int(_RETENTION.REQUIRED)},
                        {"retention_state": int(_RETENTION.PROMOTED)},
                    ),),
                    command.occurred_at,
                )
            )
        self._owners[("actions", command.action_id)] = (
            "action", command.action_id,
        )
        plan_complete = all(
            values.get("status") in _ACTION_TERMINAL
            or values["id"] == command.action_id
            for values in siblings.values()
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
            read_coverage=ReadCoverage(self._ranges),
            result=CaptureResult(
                action_status=action_status,
                plan_status=plan_status,
                output_ids=tuple(identity for identity, _ in numbered),
            ),
        )

    def _draft_identities(self) -> list[tuple[int, int | None, int | None]]:
        return [(_KIND_CODES[draft.kind], draft.file.device_file_id,
                 draft.file.intermediate_file_id)
                for draft in self._command.drafts]

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实原事务与输入一致，恢复首次结果与文件身份。

        事实时刻、终态分支、错误码、动作与产物集合任一不同都按操
        作身份冲突拒绝；不重新登记，也不改写既有终态。
        """
        command = self._command
        if self._unstarted:
            action = row_facts(connection, "actions", command.action_id)
            if action is None:
                raise ConsistencyError("原本地取消动作不存在")
            verify_unstarted_final(connection, action, saved)
        types = [(event["type"], event["reason"]) for event in saved]
        if not types or types[0][0] != _ACTION_FINISHED_EVENT:
            raise TransactionError("原事务不是完成登记，不能作为重送核实")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("完成登记的事实时刻与原事务不同")
        action_row = saved[0]["body"]["rows"][0]
        if action_row["table"] != "actions" or action_row["id"] != command.action_id:
            raise TransactionError("原完成登记属于其他动作")
        after = action_row["after"]["values"]
        if self._canceled:
            if types[0][1] != 4 or after.get("status") != _ACTION_CANCELED:
                raise TransactionError("原完成登记不是取消终态，与重送输入不同")
        elif self._failure is None:
            if types[0][1] != 1 or after.get("status") != _ACTION_SUCCEEDED:
                raise TransactionError("原完成登记不是成功终态，与重送输入不同")
        else:
            if types[0][1] != 2 or after.get("status") != _ACTION_FAILED:
                raise TransactionError("原完成登记不是失败终态，与重送输入不同")
            spec = registered_error(self._failure.code)
            if after.get("error_code") != spec["action_error_id"]:
                raise TransactionError("原完成登记的错误码与重送输入不同")
            if not json_equal(after.get("error_details_json"),
                              self._failure.details):
                raise TransactionError("原完成登记的错误详情与重送输入不同")
        registered: dict[tuple[int, int | None, int | None], int] = {}
        origins: set[tuple[int, int]] = set()
        for event, (event_type, reason) in zip(saved, types):
            if event_type != _OUTPUT_REGISTERED_EVENT:
                continue
            for row in event["body"]["rows"]:
                values = row["after"]["values"]
                if row["table"] == "outputs":
                    registered[(reason, values.get("device_file_id"),
                                values.get("intermediate_file_id"))] = row["id"]
                else:
                    origins.add((values["output_id"],
                                 values["original_output_id"]))
        expected = self._draft_identities()
        if sorted(registered) != sorted(expected):
            raise TransactionError("原完成登记的产物集合与重送输入不同")
        output_ids = tuple(registered[item] for item in expected)
        for draft, identity in zip(command.drafts, expected):
            if (draft.kind is not OutputKind.ORIGINAL
                    and draft.original_output_id is not None
                    and (registered[identity], draft.original_output_id)
                    not in origins):
                raise TransactionError("原完成登记的派生链接与重送输入不同")
        action = row_facts(connection, "actions", command.action_id)
        assert action is not None
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["actions"] = {action["id"]: action}
        self._state["plans"] = {plan["id"]: plan}
        self._owners[("actions", action["id"])] = (
            "action", action["id"])
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CaptureResult(
                action_status=after["status"], plan_status=plan["status"],
                output_ids=output_ids,
                disposition=FinishDisposition.ALREADY),
        )

    def _recover(self, connection, action) -> CommandPlan:
        """终态后的新键：不重新登记；输入与既有登记一致才恢复结果。

        终态分支或错误码不同、产物集合追加或改写都拒绝，既有终态
        与第一次保存的文件身份保持不变。
        """
        command = self._command
        if self._canceled:
            if action["status"] != _ACTION_CANCELED:
                raise TransactionError(
                    f"动作终态不是取消登记结果，不能按取消收场重送:"
                    f" {action['status']!r}")
        elif self._failure is None:
            if action["status"] != _ACTION_SUCCEEDED:
                raise TransactionError(
                    f"动作终态不是完成登记结果，不能按成功重送: {action['status']!r}")
        else:
            if action["status"] != _ACTION_FAILED:
                raise TransactionError(
                    f"动作终态不是失败登记结果，不能按失败重送: {action['status']!r}")
            spec = registered_error(self._failure.code)
            if action["error_code"] != spec["action_error_id"]:
                raise TransactionError("动作已保存的错误码与新键输入不同")
        with closing(connection.execute(
            "SELECT id, kind, device_file_id, intermediate_file_id"
            " FROM outputs WHERE source_action_id=? ORDER BY id",
            (command.action_id,),
        )) as cursor:
            registered = {
                (kind, device, intermediate): output_id
                for output_id, kind, device, intermediate in cursor}
        expected = self._draft_identities()
        if sorted(registered) != sorted(expected):
            raise TransactionError(
                "动作已终态，产物登记与新键输入不一致，不能追加或改写")
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["actions"] = {action["id"]: action}
        self._state["plans"] = {plan["id"]: plan}
        self._owners[("actions", action["id"])] = (
            "action", action["id"])
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=CaptureResult(
                action_status=action["status"], plan_status=plan["status"],
                output_ids=tuple(registered[item] for item in expected),
                disposition=FinishDisposition.ALREADY),
        )

    def _required(self, connection, table, identity):
        rows = self._state.setdefault(table, {})
        if identity not in rows:
            facts = row_facts(connection, table, identity)
            if facts is None:
                raise TransactionError(f"产物登记关联记录不存在: {table}#{identity}")
            rows[identity] = facts
        return rows[identity]

    def _verify_repaired_outputs(self, connection, changes) -> None:
        """核对修复成品登记资格：未释放的完整修复输出配修复成功事实。

        没有修复成品时不读取处理事实；守卫在事件层用同样事实复核。
        """
        repaired = [output for output in changes.outputs
                    if output.kind is OutputKind.REPAIRED]
        if not repaired:
            return
        with closing(connection.execute(
                "SELECT id FROM recording_processing WHERE action_id = ?",
                (self._command.action_id,))) as cursor:
            found = cursor.fetchone()
        processing = None
        if found is not None:
            processing = self._required(
                connection, "recording_processing", int(found[0]))
        for output in repaired:
            file = self._state["intermediate_files"][output.intermediate_file_id]
            if file.get("purpose") != int(_PURPOSE.REPAIR_OUTPUT):
                raise TransactionError(
                    "修复成品承载文件不是修复输出用途:"
                    f" {output.intermediate_file_id}")
            if file.get("retention_state") != int(_RETENTION.REQUIRED):
                raise TransactionError(
                    "修复成品承载文件必须尚未释放、提升或交接:"
                    f" {file.get('retention_state')!r}")
            if file.get("size_bytes") is None or file.get("sha256") is None:
                raise TransactionError(
                    f"修复成品承载文件缺少完整字节事实: {output.intermediate_file_id}")
            if (processing is None
                    or processing.get("repair_state") != RepairOutcome.SUCCEEDED.value
                    or processing.get("repair_output_file_id")
                    != output.intermediate_file_id):
                raise TransactionError(
                    "产物登记缺少本动作的修复成功事实:"
                    f" {output.intermediate_file_id}")

    def _readable_metadata(self, connection, output_id, output, original_ids):
        """登记时的可读元信息取自承载文件行；观察未保存时保留未知。

        修复成品沿用其关联原片设备文件的可读名称与类型：修复不重
        新编码，媒体类型与原片一致。
        """
        if output.device_file_id is not None:
            file = self._required(connection, "device_files", output.device_file_id)
        else:
            if output.original_batch_file_id is not None:
                original_file_id = output.original_batch_file_id
            else:
                original_file_id = self._state["outputs"][
                    original_ids[output_id]]["device_file_id"]
            file = self._required(connection, "device_files", original_file_id)
        return file.get("original_name"), file.get("media_type")

    def _load_file(self, connection, device_id, intermediate_id):
        table = "device_files" if device_id is not None else "intermediate_files"
        file = self._required(connection, table, device_id if device_id is not None else intermediate_id)
        if device_id is not None:
            self._required(connection, "actions", file["observer_action_id"])

    def _read_origins(self, connection, column, identity):
        covered = self._ranges.setdefault(("output_origins", column), set())
        rows = self._state.setdefault("output_origins", {})
        if identity not in covered:
            limit = 2 if column == "output_id" else 3
            with closing(connection.execute(
                f"SELECT id,output_id,original_output_id FROM output_origins WHERE {column}=? ORDER BY id LIMIT ?",
                (identity, limit),
            )) as cursor:
                found = cursor.fetchall()
            if len(found) == limit:
                raise TransactionError("产物原片关联数量超过允许范围")
            for row_id, output_id, original_id in found:
                rows[row_id] = {"id": row_id, "output_id": output_id, "original_output_id": original_id}
            self._origin_members[column, identity] = tuple(row[0] for row in found)
            covered.add(identity)
        return tuple(rows[row_id] for row_id in self._origin_members[column, identity])

    def _sibling_actions(self, connection, action) -> dict[int, dict[str, Any]]:
        rows = {}
        for row in connection.execute(
            "SELECT id FROM actions WHERE plan_id = ?", (action["plan_id"],)
        ).fetchall():
            facts = row_facts(connection, "actions", int(row[0]))
            if facts is not None:
                rows[int(row[0])] = facts
        return rows


# -- 录像内部处理的决定与结果 ---------------------------------------


#: 取消收场进度的合法转换；与 RECORDING_PROCESSED.DISCARD 登记一致。
_DISCARD_NEXT = {
    1: frozenset({DiscardPhase.PENDING.value}),
    2: frozenset({
        DiscardPhase.RUNNING.value, DiscardPhase.COMPLETED.value,
        DiscardPhase.FAILED.value, DiscardPhase.UNKNOWN.value}),
    3: frozenset({
        DiscardPhase.COMPLETED.value, DiscardPhase.FAILED.value,
        DiscardPhase.UNKNOWN.value}),
}

_FILE_PURPOSE = enum_for("intermediate_files.purpose")
_FILE_COMPLETION = enum_for("device_files.completion_state")


class _ProcessingCommand:
    """录像内部处理事务的共同装载：处理行、核对行与只读决定。"""

    def __init__(self) -> None:
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def _load_processing(self, connection, processing_id: int) -> dict[str, Any]:
        rows = self._state.setdefault("recording_processing", {})
        if processing_id not in rows:
            facts = row_facts(connection, "recording_processing", processing_id)
            if facts is None:
                raise ConsistencyError(f"录像处理记录不存在: {processing_id}")
            rows[processing_id] = facts
        return rows[processing_id]

    def _load_row(self, connection, table: str, row_id: int) -> dict[str, Any]:
        rows = self._state.setdefault(table, {})
        if row_id not in rows:
            facts = row_facts(connection, table, row_id)
            if facts is None:
                raise ConsistencyError(f"处理关联记录不存在: {table}#{row_id}")
            rows[row_id] = facts
        return rows[row_id]

    def _claim(self, processing) -> None:
        self._owners[("recording_processing", processing["id"])] = (
            "action", processing["action_id"])

    def _decision(
        self, disposition: ProcessingDisposition, *, read_only: bool = False,
    ) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(disposition), read_only=read_only)

    def _emit(
        self, scope, event_type: int, reason: int, row, occurred_at: int,
    ) -> CommandPlan:
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            event_type, reason, (row,), occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))

    def _reuse(self, saved, event_type: int, reason: int, occurred_at: int) -> CommandPlan:
        """原键恢复首次响应；承载其他阶段或事实时按身份冲突拒绝。"""
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(event_type, reason)]:
            raise TransactionError("操作身份已用于其他阶段，不能作为本事务重送")
        if saved[0]["occurred_at"] != occurred_at:
            raise TransactionError("处理事实的时刻与原事务不同")
        return self._decision(ProcessingDisposition.ALREADY, read_only=True)


class _MediaProcessingCommand(_ProcessingCommand):
    """媒体申请在原完整边界上生成并核实同一事实组。"""

    def _saved_processing(self, scope, saved, kinds):
        if [(event["type"], event["reason"]) for event in saved] != kinds:
            raise TransactionError("操作身份已用于不同媒体阶段")
        if any(event["occurred_at"] != self._request.occurred_at for event in saved):
            raise TransactionError("媒体申请的原事实时刻不同")
        rows = [row for event in saved for row in event["body"]["rows"]
                if row["table"] == "recording_processing"]
        if len(rows) != 1 or rows[0]["id"] != self._request.processing_id:
            raise TransactionError("原媒体申请属于不同处理记录")
        transaction = saved[0]["transaction"]
        previous = read_transaction_range(scope.connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        self._original_boundary = HistoryBoundary(previous.txn_id, previous.last_event_id) if previous else HistoryBoundary(0, 0)
        processing = self._load_processing(scope.connection, self._request.processing_id)
        self._claim(processing)
        return self._original_row(scope, "recording_processing", processing["id"],
                                  ("action", processing["action_id"]))

    def _original_row(self, scope, table, row_id, owner, *, boundary=None):
        facts = self._load_row(scope.connection, table, row_id)
        facts.update(read_row_values_at_boundary(scope.connection, owner=owner,
            table=table, row_id=row_id, columns=business_columns(table), current_values=facts,
            boundary=self._original_boundary if boundary is None else boundary,
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id)))
        return facts

    def _save_specs(self, scope, specs, result=None):
        allocation = scope.allocate(len(specs))
        events = tuple(_envelope(allocation.first_event_id + index, allocation.txn_id,
            kind, reason, rows, self._request.occurred_at)
            for index, (kind, reason, rows) in enumerate(specs))
        return CommandPlan(events=events, owners=self._owners, state_rows=self._state,
            result=result if result is not None else ProcessingOutcome(ProcessingDisposition.SAVED))

    def _reuse_specs(self, scope, saved, specs, result=None):
        if len(saved) != len(specs):
            raise TransactionError("原媒体事务没有完整阶段事实")
        registry = load_enum_registry()["history_objects"]
        for event, (kind, reason, rows) in zip(saved, specs):
            expected = [{"table": row.table, "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)}} for row in rows]
            if (event["type"] != kind or event["reason"] != reason
                    or event["body"]["evidence"] != {}
                    or not json_equal(event["body"]["rows"], expected)):
                raise TransactionError("原媒体事务的完整申请、阶段或文件身份不同")
            expected_owners = {(registry[owner[0]]["id"], owner[1])
                for row in rows for owner in (self._owners[(row.table, row.row_id)],)}
            with closing(scope.connection.execute(
                "SELECT entity_type,entity_id FROM entity_event_links WHERE event_id=?",
                (event["event_id"],))) as cursor:
                actual_owners = set(cursor.fetchall())
            if actual_owners != expected_owners:
                raise ConsistencyError("原媒体事务的对象目录归属不完整")
        return CommandPlan(events=(), owners=self._owners, state_rows=self._state,
            read_only=True, result=result if result is not None else
            ProcessingOutcome(ProcessingDisposition.ALREADY))


class _CheckDecisionCommand(_ProcessingCommand):
    """固定原片检查决定：UNDETERMINED 一次固定，不因重送重算。"""

    def __init__(self, request: CheckDecisionSave, key: OperationKey) -> None:
        if not isinstance(request, CheckDecisionSave):
            raise TypeError("检查决定申请必须使用 CheckDecisionSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_DECIDED_EVENT, _DECIDED_CHECK_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["check_decision"] != 1:
            raise ConsistencyError(
                f"检查决定已固定，不重新判断: {processing['check_decision']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"check_decision": processing["check_decision"],
             "check_basis_json": processing["check_basis_json"]},
            {"check_decision": request.decision.value,
             "check_basis_json": request.basis.as_json()},
        )
        return self._emit(
            scope, _RECORDING_DECIDED_EVENT, _DECIDED_CHECK_REASON,
            row, request.occurred_at)


class _RepairDecisionCommand(_MediaProcessingCommand):
    """固定修复决定：UNDETERMINED 一次固定，保存后配置变化不重算。"""

    def __init__(self, request: RepairDecisionSave, key: OperationKey) -> None:
        if not isinstance(request, RepairDecisionSave):
            raise TypeError("修复决定申请必须使用 RepairDecisionSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            processing = self._saved_processing(scope, saved,
                [(_RECORDING_DECIDED_EVENT, _DECIDED_REPAIR_REASON)])
            return self._reuse_specs(scope, saved, self._specs(processing))
        processing = self._load_processing(connection, request.processing_id)
        self._claim(processing)
        return self._save_specs(scope, self._specs(processing))

    def _specs(self, processing):
        request = self._request
        if processing["repair_state"] != 1:
            raise ConsistencyError(
                f"修复决定已固定，不重新判断: {processing['repair_state']!r}")
        row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"],
             "repair_basis_json": processing["repair_basis_json"]},
            {"repair_state": request.decision.value,
             "repair_basis_json": request.basis.as_json()},
        )
        return ((_RECORDING_DECIDED_EVENT, _DECIDED_REPAIR_REASON, (row,)),)


class _SourceFileCommand(_ProcessingCommand):
    """首次关联可靠原片：属于本次动作、角色为原片且已确认写完。"""

    def __init__(self, request: SourceFileSave, key: OperationKey) -> None:
        if not isinstance(request, SourceFileSave):
            raise TypeError("原片关联申请必须使用 SourceFileSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_DECIDED_EVENT, _DECIDED_SOURCE_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        if processing["source_device_file_id"] is not None:
            raise ConsistencyError("可靠原片已关联，不重复关联")
        file = self._load_row(
            connection, "device_files", request.source_device_file_id)
        if file["role"] != int(_FILE_ROLE.ORIGINAL):
            raise ConsistencyError(
                f"关联文件不是原片角色: {file['role']!r}")
        if file["completion_state"] != int(_FILE_COMPLETION.COMPLETE):
            raise ConsistencyError(
                f"关联文件尚未确认写完: {file['completion_state']!r}")
        if file["source_action_id"] != processing["action_id"]:
            raise ConsistencyError(
                f"关联文件属于其他动作: {file['source_action_id']!r}")
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"],
            {"source_device_file_id": processing["source_device_file_id"]},
            {"source_device_file_id": request.source_device_file_id},
        )
        return self._emit(
            scope, _RECORDING_DECIDED_EVENT, _DECIDED_SOURCE_REASON,
            row, request.occurred_at)


class _CheckResultCommand(_MediaProcessingCommand):
    """保存检查执行阶段及公共媒体观察；只有 REQUIRED 决定可执行。"""

    def __init__(self, request: CheckResultSave, key: OperationKey) -> None:
        if not isinstance(request, CheckResultSave):
            raise TypeError("检查结果申请必须使用 CheckResultSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            processing = self._saved_processing(scope, saved,
                [(_RECORDING_PROCESSED_EVENT, _PROCESSED_CHECK_REASON)])
            return self._reuse_specs(scope, saved, self._specs(processing))
        processing = self._load_processing(connection, request.processing_id)
        self._claim(processing)
        return self._save_specs(scope, self._specs(processing))

    def _specs(self, processing):
        request = self._request
        if processing["check_decision"] != 3:
            raise ConsistencyError(
                f"检查决定未固定为需要检查: {processing['check_decision']!r}")
        if processing["check_state"] not in (1, 2):
            raise ConsistencyError(
                f"检查阶段已终结，不再保存结果: {processing['check_state']!r}")
        row = _update(
            "recording_processing", processing["id"],
            {"check_state": processing["check_state"],
             "media_json": processing["media_json"]},
            {"check_state": request.media.phase.value,
             "media_json": request.media.as_json()},
        )
        return ((_RECORDING_PROCESSED_EVENT, _PROCESSED_CHECK_REASON, (row,)),)


class _RepairResultCommand(_MediaProcessingCommand):
    """保存修复执行阶段及结果；成功必须指向完整修复输出。"""

    def __init__(self, request: RepairResultSave, key: OperationKey) -> None:
        if not isinstance(request, RepairResultSave):
            raise TypeError("修复结果申请必须使用 RepairResultSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            processing = self._saved_processing(scope, saved,
                [(_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON)])
            plan = self._reuse_specs(scope, saved, self._specs(processing))
            if request.phase is RepairOutcome.SUCCEEDED:
                output = self._original_row(scope, "intermediate_files", request.output_file_id,
                    ("intermediate_file", request.output_file_id))
                self._verify_output(processing, output)
            return plan
        processing = self._load_processing(connection, request.processing_id)
        if request.phase is RepairOutcome.SUCCEEDED:
            self._verify_output(processing,
                self._load_row(connection, "intermediate_files", request.output_file_id))
        self._claim(processing)
        return self._save_specs(scope, self._specs(processing))

    @staticmethod
    def _verify_output(processing, output):
        if output["purpose"] != int(_FILE_PURPOSE.REPAIR_OUTPUT):
            raise ConsistencyError(f"修复成功必须指向修复输出文件: {output['purpose']!r}")
        if output["owner_action_id"] != processing["action_id"]:
            raise ConsistencyError("修复输出属于其他动作")
        if output["size_bytes"] is None or output["sha256"] is None:
            raise ConsistencyError("修复输出缺少完整字节事实")

    def _specs(self, processing):
        request = self._request
        if processing["repair_state"] not in (3, 4):
            raise ConsistencyError(
                f"修复尚未取得执行决定或已终结: {processing['repair_state']!r}")
        before: dict[str, Any] = {"repair_state": processing["repair_state"]}
        after: dict[str, Any] = {"repair_state": request.phase.value}
        if request.phase is RepairOutcome.SUCCEEDED:
            before["repair_output_file_id"] = processing["repair_output_file_id"]
            after["repair_output_file_id"] = request.output_file_id
        elif request.phase is RepairOutcome.FAILED:
            before["repair_error_json"] = processing["repair_error_json"]
            after["repair_error_json"] = request.error.as_json()
        row = _update(
            "recording_processing", processing["id"], before, after)
        return ((_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON, (row,)),)


class _DiscardProgressCommand(_ProcessingCommand):
    """保存取消后文件处理收场进度；失败或未知保留实际错误。"""

    def __init__(self, request: DiscardProgressSave, key: OperationKey) -> None:
        if not isinstance(request, DiscardProgressSave):
            raise TypeError("收场进度申请必须使用 DiscardProgressSave")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(
                saved, _RECORDING_PROCESSED_EVENT, _PROCESSED_DISCARD_REASON,
                request.occurred_at)
        processing = self._load_processing(connection, request.processing_id)
        current = processing["discard_state"]
        allowed = _DISCARD_NEXT.get(current, frozenset())
        if request.phase.value not in allowed:
            raise ConsistencyError(
                f"收场进度不能从 {current!r} 推进到 {request.phase.value!r}")
        before: dict[str, Any] = {"discard_state": current}
        after: dict[str, Any] = {"discard_state": request.phase.value}
        if request.error is not None:
            before["discard_error_json"] = processing["discard_error_json"]
            after["discard_error_json"] = request.error.as_json()
        self._claim(processing)
        row = _update(
            "recording_processing", processing["id"], before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RECORDING_PROCESSED_EVENT, _PROCESSED_DISCARD_REASON, (row,),
            request.occurred_at,
        )
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ProcessingOutcome(ProcessingDisposition.SAVED))


class _RepairStartCommand(_MediaProcessingCommand):
    """登记修复输出文件并进入修复执行；路径登记与运行阶段同事务。

    创建文件前先登记路径和责任；修复输出自创建起使用 derived/
    正式身份，保持同一文件身份和位置直到登记为正式产物。
    """

    def __init__(self, request: RepairStart, key: OperationKey) -> None:
        if not isinstance(request, RepairStart):
            raise TypeError("修复输出登记申请必须使用 RepairStart")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            processing = self._saved_processing(scope, saved,
                [(_INTERMEDIATE_FILE_EVENT, 1),
                 (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON)])
            created = saved[0]["body"]["rows"]
            if len(created) != 1 or created[0]["table"] != "intermediate_files":
                raise TransactionError("原媒体登记没有唯一修复输出")
            file_id = created[0]["id"]
            transaction = saved[0]["transaction"]
            output = self._original_row(scope, "intermediate_files", file_id,
                ("intermediate_file", file_id),
                boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id))
            specs, relative_path = self._specs(processing, file_id)
            self._owners[("intermediate_files", file_id)] = ("intermediate_file", file_id)
            plan = self._reuse_specs(scope, saved, specs, RepairOutputFile(
                ProcessingDisposition.ALREADY, file_id, relative_path))
            if any(not json_equal(output[name], value)
                   for name, value in specs[0][2][0].after.values.items()):
                raise ConsistencyError("原修复输出的登记事实与可靠历史不符")
            return plan
        processing = self._load_processing(connection, request.processing_id)
        self._claim(processing)
        file_id = _next_id(connection, "intermediate_files")
        specs, relative_path = self._specs(processing, file_id)
        self._owners[("intermediate_files", file_id)] = ("intermediate_file", file_id)
        return self._save_specs(scope, specs, RepairOutputFile(
            ProcessingDisposition.SAVED, file_id, relative_path))

    def _specs(self, processing, file_id):
        request = self._request
        if processing["repair_state"] != int(_REPAIR_STATE.PENDING):
            raise ConsistencyError(
                f"修复尚未取得待执行决定: {processing['repair_state']!r}")
        relative_path = relative_file_path(
            FilePurpose.REPAIR_OUTPUT, file_id, request.extension)
        file_row = _row(
            "intermediate_files",
            file_id,
            {
                "owner_action_id": processing["action_id"],
                "owner_delivery_id": None,
                "purpose": int(_PURPOSE.REPAIR_OUTPUT),
                "relative_path": relative_path,
                "retention_state": int(_RETENTION.REQUIRED),
                "cleanup_state": int(_FILE_CLEANUP.NOT_NEEDED),
                "size_bytes": None,
                "sha256": None,
                "last_error_json": None,
            },
        )
        processing_row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"]},
            {"repair_state": int(_REPAIR_STATE.RUNNING)},
        )
        return ((_INTERMEDIATE_FILE_EVENT, 1, (file_row,)),
                (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON, (processing_row,))), relative_path


class _RepairSuccessCommand(_MediaProcessingCommand):
    """保存修复输出完整字节并固定修复成功；字节与终态同事务。

    事件先保存字节事实再保存成功终态，守卫从同事务先行事件复核
    输出用途、归属与完整字节；重复提交按原键恢复首次响应。
    """

    def __init__(self, request: RepairSuccess, key: OperationKey) -> None:
        if not isinstance(request, RepairSuccess):
            raise TypeError("修复成功申请必须使用 RepairSuccess")
        self._request = request
        self._key = key
        super().__init__()

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            processing = self._saved_processing(scope, saved,
                [(_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON),
                 (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON)])
            rows = saved[0]["body"]["rows"]
            if (len(rows) != 1 or rows[0]["table"] != "intermediate_files"
                    or rows[0]["id"] != request.output_file_id):
                raise TransactionError("原修复成功属于不同输出文件")
            output = self._original_row(scope, "intermediate_files", request.output_file_id,
                ("intermediate_file", request.output_file_id))
            self._owners[("intermediate_files", output["id"])] = ("intermediate_file", output["id"])
            return self._reuse_specs(scope, saved, self._specs(processing, output))
        processing = self._load_processing(connection, request.processing_id)
        output = self._load_row(connection, "intermediate_files", request.output_file_id)
        self._claim(processing)
        self._owners[("intermediate_files", output["id"])] = ("intermediate_file", output["id"])
        return self._save_specs(scope, self._specs(processing, output))

    def _specs(self, processing, output):
        request = self._request
        if processing["repair_state"] != int(_REPAIR_STATE.RUNNING):
            raise ConsistencyError(
                f"修复不在执行中，不能固定成功: {processing['repair_state']!r}")
        if output["purpose"] != int(_PURPOSE.REPAIR_OUTPUT):
            raise ConsistencyError(
                f"修复成功必须指向修复输出文件: {output['purpose']!r}")
        if output["owner_action_id"] != processing["action_id"]:
            raise ConsistencyError("修复输出属于其他动作")
        if output["size_bytes"] is not None or output["sha256"] is not None:
            raise ConsistencyError("修复输出已保存完整字节事实")
        file_row = _update(
            "intermediate_files", output["id"],
            {"size_bytes": output["size_bytes"], "sha256": output["sha256"]},
            {"size_bytes": request.size_bytes, "sha256": request.sha256},
        )
        processing_row = _update(
            "recording_processing", processing["id"],
            {"repair_state": processing["repair_state"],
             "repair_output_file_id": processing["repair_output_file_id"]},
            {"repair_state": int(_REPAIR_STATE.SUCCEEDED),
             "repair_output_file_id": request.output_file_id},
        )
        return ((_INTERMEDIATE_FILE_EVENT, _LIFECYCLE_REASON, (file_row,)),
                (_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON, (processing_row,)))


class CaptureRepository:
    """采集完成终态事务的 SQLite 仓储。"""

    def finish_start_result(
        self, finish: AttemptFinish, observation: ActivityObservationSave | None,
        key: OperationKey, owned: OwnedConnection,
        *, start_finish: StaleRunFinish | None = None,
        action_finish: FinishCapture | FinishCanceledCapture | None = None,
        expiration: ExpireActionRequest | None = None,
    ) -> DbOutcome[FinishAttemptResult]:
        """原启动或启动核实结果与派生活动、START 收场共同保存。"""
        receipt = commit_operation(
            _FinishStartResultCommand(
                finish, observation, start_finish, action_finish, expiration, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def close_start(
        self, finish: StaleRunFinish, action_finish: FinishCapture,
        key: OperationKey, owned: OwnedConnection,
    ) -> DbOutcome[None]:
        """没有新调用结果时，共同结束原 START 责任与动作。"""
        receipt = commit_operation(_CloseStartCommand(finish, action_finish, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=None)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_emergency(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        owned: OwnedConnection,
        timeout_s=None,
        retry_interval_s=None,
    ) -> DbOutcome[EmergencySave]:
        command = SaveEmergencyCommand(
            session_key=session_key,
            action_id=action_id,
            activity_id=activity_id,
            record=record,
            attempts=attempts,
            occurred_at=occurred_at,
            key=key,
            timeout_s=timeout_s,
            retry_interval_s=retry_interval_s,
        )
        receipt = commit_operation(command, key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_capture(
        self, command: FinishCapture, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[CaptureResult]:
        receipt = commit_operation(FinishCaptureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_recording_results(
        self, request: FinishRecordingResults, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CaptureResult]:
        """已保存原轮次后，共同结束录像核实责任、动作与产物。"""
        receipt = commit_operation(
            _FinishRecordingResultsCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_canceled_capture(
        self, command: FinishCanceledCapture, key: OperationKey,
        owned: OwnedConnection
    ) -> DbOutcome[CaptureResult]:
        receipt = commit_operation(
            FinishCaptureCommand(command, key, canceled=True), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_binding_failure(
        self, command: FinishBindingFailure, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CaptureResult]:
        """同事务保存绑定失败、适用流程收场和拍摄／计划终态。"""
        receipt = commit_operation(_FinishBindingFailureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_residual_binding_failure(
        self, command: FinishResidualBindingFailure, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CaptureResult | None]:
        """共同保存原残留流程失败及适用的匹配触发动作失败。"""
        receipt = commit_operation(_FinishResidualBindingFailureCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_check_decision(
        self, command: CheckDecisionSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_CheckDecisionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_repair_decision(
        self, command: RepairDecisionSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairDecisionCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_source_file(
        self, command: SourceFileSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_SourceFileCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_check_result(
        self, command: CheckResultSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_CheckResultCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_repair_result(
        self, command: RepairResultSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairResultCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_discard_progress(
        self, command: DiscardProgressSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_DiscardProgressCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def start_repair_output(
        self, command: RepairStart, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[RepairOutputFile]:
        receipt = commit_operation(_RepairStartCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def complete_repair_output(
        self, command: RepairSuccess, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ProcessingOutcome]:
        receipt = commit_operation(_RepairSuccessCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_file_observation(
        self, command: FileObservationSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_FileCreateCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_file_ownership(
        self, command: OwnershipSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_FileOwnershipCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_file_completion(
        self, command: FileCompletionSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_FileCompleteCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_file_presence(
        self, command: FilePresenceSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_FilePresenceCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_file_checksum(
        self, command: FileChecksumSave, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_FileChecksumCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def save_activity_observation(
        self, command: ActivityObservationSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ObservationOutcome]:
        receipt = commit_operation(_ActivityObserveCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def release_occupancy(
        self, command: ActivityReleaseSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ActivityReleaseResult]:
        receipt = commit_operation(
            _ActivityReleaseCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def conclude_activity(
        self, command: ActivityConcludeSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ActivityConclusion]:
        receipt = commit_operation(
            _ActivityConcludeCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def confirm_result_set(
        self, command: ResultSetSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ResultSetOutcome]:
        receipt = commit_operation(
            _ResultSetConfirmCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def finish_result_check(
        self, finish: AttemptFinish, confirm: ResultSetSave,
        key: OperationKey, owned: OwnedConnection,
    ) -> DbOutcome[ResultCheckOutcome]:
        receipt = commit_operation(
            _FinishResultCheckCommand(finish, confirm, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def close_result_check_unconfirmed(
        self, command: ResultSetSave, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ResultSetOutcome]:
        receipt = commit_operation(
            _CloseResultCheckCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def close_unconfirmed_result_run(
        self, command: ResultRunClose, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[None]:
        """录像活动预算耗尽：仅收场核实流程，不携带集合结论。"""
        receipt = commit_operation(
            _ResultRunCloseCommand(command, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)



class _FilePresenceCommand:
    """保存一次文件存在性观察（DEVICE_FILE_OBSERVED.PRESENCE）。

    存在性与清理成功分别保存；必须是实际状态变化。
    """

    def __init__(self, command: FilePresenceSave, key: OperationKey) -> None:
        if not isinstance(command, FilePresenceSave):
            raise TypeError("存在性观察申请必须使用 FilePresenceSave")
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
        facts = self._load(connection, command.file_id)
        current = facts["presence_state"]
        if current == command.state:
            raise ConsistencyError(
                f"存在性观察必须是实际状态变化: {command.file_id} {command.state!r}")
        before: dict[str, Any] = {"presence_state": current}
        after: dict[str, Any] = {"presence_state": command.state}
        if command.locator is not None:
            before["locator_json"] = facts["locator_json"]
            after["locator_json"] = dict(command.locator)
        if command.error is not None:
            before["last_error_json"] = facts["last_error_json"]
            after["last_error_json"] = dict(command.error)
        row = _update("device_files", command.file_id, before, after)
        self._owners[("device_files", command.file_id)] = (
            "device_file", command.file_id)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DEVICE_FILE_EVENT, _FILE_PRESENCE_REASON,
            (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(
                ObservationDisposition.SAVED, command.file_id))

    def _load(self, connection, file_id: int) -> dict[str, Any]:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None:
            raise ConsistencyError(f"设备文件不存在: {file_id}")
        self._state.setdefault("device_files", {})[file_id] = facts
        return facts

    def _reuse(self, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DEVICE_FILE_EVENT, _FILE_PRESENCE_REASON)]:
            raise TransactionError(
                "操作身份已用于其他文件事务，不能作为存在性观察重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("存在性观察的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "device_files" or row["id"] != command.file_id:
            raise TransactionError("原存在性观察属于其他文件")
        after = row["after"]["values"]
        if (after.get("presence_state") != command.state
                or not json_equal(after.get("locator_json"),
                                  None if command.locator is None
                                  else dict(command.locator))
                or not json_equal(after.get("last_error_json"),
                                  None if command.error is None
                                  else dict(command.error))):
            raise TransactionError("存在性观察的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, command.file_id))


class _FileChecksumCommand:
    """保存一次设备源摘要能力声明（DEVICE_FILE_OBSERVED.CHECKSUM）。

    能力只能从未判定一次决定；实际摘要与查询错误另行保存。
    """

    def __init__(self, command: FileChecksumSave, key: OperationKey) -> None:
        if not isinstance(command, FileChecksumSave):
            raise TypeError("摘要能力声明申请必须使用 FileChecksumSave")
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
        facts = self._load(connection, command.file_id)
        if facts["checksum_support"] != int(_FILE_CHECKSUM.UNDETERMINED):
            raise ConsistencyError(
                f"摘要能力已决定，不重复声明: {command.file_id}"
                f" {facts['checksum_support']!r}")
        row = _update(
            "device_files", command.file_id,
            {"checksum_support": facts["checksum_support"]},
            {"checksum_support": command.support},
        )
        self._owners[("device_files", command.file_id)] = (
            "device_file", command.file_id)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DEVICE_FILE_EVENT, _FILE_CHECKSUM_REASON, (row,),
            command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(
                ObservationDisposition.SAVED, command.file_id))

    def _load(self, connection, file_id: int) -> dict[str, Any]:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None:
            raise ConsistencyError(f"设备文件不存在: {file_id}")
        self._state.setdefault("device_files", {})[file_id] = facts
        return facts

    def _reuse(self, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DEVICE_FILE_EVENT, _FILE_CHECKSUM_REASON)]:
            raise TransactionError(
                "操作身份已用于其他文件事务，不能作为摘要能力声明重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("摘要能力声明的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "device_files" or row["id"] != command.file_id:
            raise TransactionError("原摘要能力声明属于其他文件")
        after = row["after"]["values"]
        if after.get("checksum_support") != command.support:
            raise TransactionError("摘要能力声明的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, command.file_id))

# -- 设备活动观察 -----------------------------------------------------


def load_activity_of_action(connection, action_id: int) -> dict:
    """装载动作的唯一设备活动行；动作与活动的主键不重合。

    活动按动作建立（一动作一活动），查询按 action_id 定位后以真实
    主键返回行事实，供观察、释放、收场与核实命令共用。
    """
    from contextlib import closing

    with closing(connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action_id,),
    )) as cursor:
        found = cursor.fetchone()
    if found is None:
        raise ConsistencyError(f"设备活动不存在: {action_id}")
    facts = row_facts(connection, "device_activities", int(found[0]))
    if facts is None:
        raise ConsistencyError(f"设备活动记录缺失: {found[0]}")
    return facts


class _ActivityObserveCommand:
    """保存一次设备活动观察（DEVICE_OBSERVED.OBSERVE）。

    发送与启动时刻只能从空值一次保存；状态按登记转换推进；活动
    结束不经本命令补造（须由可靠停止事实承载，见 activity 守卫）。
    """

    def __init__(self, command: ActivityObservationSave, key: OperationKey) -> None:
        if not isinstance(command, ActivityObservationSave):
            raise TypeError("活动观察申请必须使用 ActivityObservationSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        facts = load_activity_of_action(connection, command.action_id)
        activity_id = facts["id"]
        self._activity_id = activity_id
        self._state["device_activities"] = {activity_id: facts}
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for column, current, value in (
            ("sent_at", facts["sent_at"], command.sent_at),
            ("started_at", facts["started_at"], command.started_at),
            ("dispatch_state", facts["dispatch_state"], command.dispatch_state),
            ("activity_state", facts["activity_state"], command.activity_state),
        ):
            if value is None:
                continue
            if column in ("sent_at", "started_at"):
                if current is not None:
                    raise ConsistencyError(f"{column} 已保存，不因新观察改写")
            else:
                table = _ACTIVITY_DISPATCH_NEXT if column == "dispatch_state" else _ACTIVITY_STATE_NEXT
                if value not in table.get(current, frozenset()):
                    raise ConsistencyError(
                        f"{column} 不能从 {current!r} 推进到 {value!r}")
            before[column] = current
            after[column] = value
        if not after:
            raise ConsistencyError("活动观察必须携带至少一项新事实")
        row = _update("device_activities", activity_id, before, after)
        self._owners[("device_activities", activity_id)] = (
            "action", facts["action_id"])
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_OBSERVE_REASON,
            (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(
                ObservationDisposition.SAVED, command.action_id))

    def _reuse(self, saved) -> CommandPlan:
        """原键重送：核实原观察分支与输入后恢复首次响应。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_ACTIVITY_OBSERVE_EVENT, _ACTIVITY_OBSERVE_REASON)]:
            raise TransactionError("操作身份已用于其他事务，不能作为活动观察重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("活动观察的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if (row["table"] != "device_activities"
                or row["id"] != self._activity_id):
            raise TransactionError("原活动观察属于其他活动")
        after = row["after"]["values"]
        for column, value in (
            ("sent_at", command.sent_at),
            ("started_at", command.started_at),
            ("dispatch_state", command.dispatch_state),
            ("activity_state", command.activity_state),
        ):
            if not json_equal(after.get(column), value):
                raise TransactionError("活动观察的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, command.action_id))


# -- 设备活动占用释放与收场 -------------------------------------------


class ReleaseOutcome(Enum):
    """占用释放事务的可靠结果分区。"""

    RELEASED = "released"
    ALREADY = "already"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ActivityReleaseResult:
    """释放结果：保存或已有释放时成功，拒绝时携带可靠原因。"""

    outcome: ReleaseOutcome
    reason: str | None = None


class ConcludeOutcome(Enum):
    """活动收场事务的可靠结果分区。"""

    CONCLUDED = "concluded"
    ALREADY = "already"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ActivityConclusion:
    """收场结果：结束观察与占用释放在同一事务共同保存。"""

    outcome: ConcludeOutcome
    reason: str | None = None


class _ActivityReleaseCommand:
    """释放本活动冲突占用的事务命令（DEVICE_OBSERVED.RELEASE）。

    正常完成、停止、可靠未派发、无效果拒绝、恢复对账、残留收场
    及应急补记共用本判定：释放依据三者居一，且输出范围归属限制
    已经解除；只触发候选重新判断，不自动授予下一动作。
    """

    def __init__(self, command: ActivityReleaseSave, key: OperationKey) -> None:
        if not isinstance(command, ActivityReleaseSave):
            raise TypeError("占用释放申请必须使用 ActivityReleaseSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope, *, activity_result_events=()) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        command = self._command
        facts = load_activity_of_action(connection, command.action_id)
        activity_id = facts["id"]
        self._state["device_activities"] = {activity_id: facts}
        projected = dict(facts)
        concluded = set()
        for event in activity_result_events:
            for change in event.rows:
                if change.table == "device_activities" and change.row_id == activity_id:
                    projected.update(change.after.values)
                elif (change.table == "operation_attempts"
                      and change.after.values.get("status") in {
                          int(member) for member in _ATTEMPT_STATUS
                          if member is not _ATTEMPT_STATUS.RUNNING}):
                    concluded.add(change.row_id)
        if facts["occupancy_state"] == 2:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ActivityReleaseResult(outcome=ReleaseOutcome.ALREADY))
        query = ("SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                 " WHERE t.status=? AND r.activity_id=?")
        params = (int(_ATTEMPT_STATUS.RUNNING), activity_id)
        if concluded:
            query += f" AND t.id NOT IN ({','.join('?' for _ in concluded)})"
            params += tuple(concluded)
        with closing(connection.execute(query + " LIMIT 1", params)) as cursor:
            unresolved = cursor.fetchone() is not None
        decision = decide_release(ActivityFacts(
            activity_state=ActivityState[
                _ACTIVITY_STATE(projected["activity_state"]).name],
            occupancy_state=OccupancyState.HELD,
            completion_evidence=release_basis_holds(projected),
            unresolved_calls=unresolved,
            file_ownership_resolved=(projected["ownership_mode"] != 2
                                     or projected["baseline_state"] == 3)))
        if decision is not ReleaseDecision.RELEASE:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ActivityReleaseResult(
                    outcome=ReleaseOutcome.REJECTED,
                    reason=("scope_limited" if decision is ReleaseDecision.KEEP_HELD_OWNERSHIP
                            else "calls_unsettled" if decision is ReleaseDecision.KEEP_HELD_CALLS
                            else "conditions_unmet")))

        allocation = scope.allocate(1)
        self._owners[("device_activities", activity_id)] = (
            "action", facts["action_id"])
        row = _update(
            "device_activities", activity_id,
            {"occupancy_state": 1}, {"occupancy_state": 2})
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_RELEASE_REASON,
            (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ActivityReleaseResult(outcome=ReleaseOutcome.RELEASED))

    def _reuse(self, scope, saved) -> CommandPlan:
        """原键重送：核实原释放分支与输入后恢复首次响应。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_ACTIVITY_OBSERVE_EVENT, _ACTIVITY_RELEASE_REASON)]:
            raise TransactionError("操作身份已用于其他事务，不能作为占用释放重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("占用释放的事实时刻与原事务不同")
        facts = load_activity_of_action(scope.connection, command.action_id)
        row = saved[0]["body"]["rows"][0]
        if (row["table"] != "device_activities"
                or row["id"] != facts["id"]):
            raise TransactionError("原占用释放属于其他活动")
        if facts["occupancy_state"] != 2:
            raise TransactionError("原占用释放的可靠记录与输入不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ActivityReleaseResult(outcome=ReleaseOutcome.RELEASED))


class _ActivityConcludeCommand:
    """活动收场事务命令：结束观察与占用释放共同保存。

    活动结束的可靠停止事实按动作类型选择：照片整次活动随启动调
    用完成，使用 start 责任；录像与延时活动随停止调用结束，使用
    stop 责任的成功终态流程行，由本事务装载核验；结束观察
    （DEVICE_OBSERVED.OBSERVE）与占用释放（RELEASE）在同一事务，
    释放条件不满足时整组拒绝。
    """

    def __init__(self, command: ActivityConcludeSave, key: OperationKey) -> None:
        if not isinstance(command, ActivityConcludeSave):
            raise TypeError("活动收场申请必须使用 ActivityConcludeSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        command = self._command
        facts = load_activity_of_action(connection, command.action_id)
        activity_id = facts["id"]
        self._state["device_activities"] = {activity_id: facts}
        if facts["occupancy_state"] == 2:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ActivityConclusion(outcome=ConcludeOutcome.ALREADY))
        if facts["activity_state"] == 3:
            needs_ended = False
        elif facts["activity_state"] == 2:
            needs_ended = True
        else:
            # 活动状态未知但启动已生效或可能生效（如延时发送后等待）：
            # 可靠停止事实同样证明活动存在并结束；可靠确认没有启动
            # 效果的活动不能凭空结束。
            if facts["dispatch_state"] in (1, 4):
                return self._rejected("not_active")
            needs_ended = True
        if needs_ended and not self._load_stop_fact(
                connection, command.action_id, activity_id):
            raise ConsistencyError(
                "活动结束缺少可靠停止事实: "
                f"{command.action_id}")
        combined = dict(facts)
        if needs_ended:
            combined["activity_state"] = 3
        if not release_basis_holds(combined):
            return self._rejected("conditions_unmet")
        if combined["ownership_mode"] == 2 and combined["baseline_state"] != 3:
            return self._rejected("scope_limited")

        allocation = scope.allocate(2 if needs_ended else 1)
        owner = ("action", facts["action_id"])
        self._owners[("device_activities", activity_id)] = owner
        events = []
        next_event = allocation.first_event_id
        if needs_ended:
            events.append(_envelope(
                next_event, allocation.txn_id,
                _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_OBSERVE_REASON,
                (_update(
                    "device_activities", activity_id,
                    {"activity_state": facts["activity_state"]},
                    {"activity_state": 3}),),
                command.occurred_at))
            next_event += 1
        events.append(_envelope(
            next_event, allocation.txn_id,
            _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_RELEASE_REASON,
            (_update(
                "device_activities", activity_id,
                {"occupancy_state": 1}, {"occupancy_state": 2}),),
            command.occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=ActivityConclusion(outcome=ConcludeOutcome.CONCLUDED))

    def _load_stop_fact(
            self, connection, action_id: int, activity_id: int) -> bool:
        """按动作类型装载成功终态流程行作为活动结束证据。

        照片整次活动随启动调用完成，使用 start 责任；录像与延时活
        动随停止调用结束，使用 stop 责任。后续动作建立的残留收场流
        程可靠确认停止时同样证明活动结束，按活动引用采纳任一成功
        的收场流程。启动调用的成功不证明采集已经结束，正常延时按
        等待与产物判定解除占用，不经本命令。
        """
        action_row = connection.execute(
            "SELECT type FROM actions WHERE id = ?", (action_id,)).fetchone()
        responsibility = (
            f"stop/{action_id}"
            if action_row is not None and int(action_row[0]) in (2, 3)
            else f"start/{action_id}")
        rows = {}
        found = False
        for row in connection.execute(
            "SELECT id, status FROM operation_runs"
            " WHERE responsibility_key = ? AND activity_id = ?",
            (responsibility, activity_id),
        ).fetchall():
            facts = {"id": int(row[0]), "status": int(row[1])}
            rows[int(row[0])] = facts
            if facts["status"] == 3:
                found = True
        for row in connection.execute(
            "SELECT id, status FROM operation_runs"
            " WHERE kind = 8 AND activity_id = ?",
            (activity_id,),
        ).fetchall():
            facts = {"id": int(row[0]), "status": int(row[1])}
            rows[int(row[0])] = facts
            if facts["status"] == 3:
                found = True
        if rows:
            self._state["operation_runs"] = rows
        return found

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ActivityConclusion(
                outcome=ConcludeOutcome.REJECTED, reason=reason))

    def _reuse(self, scope, saved) -> CommandPlan:
        """原键重送：核实原收场组成后恢复首次响应。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if not types or any(
                event_type != _ACTIVITY_OBSERVE_EVENT for event_type, _ in types):
            raise TransactionError("操作身份已用于其他事务，不能作为活动收场重送")
        for event in saved:
            if event["occurred_at"] != command.occurred_at:
                raise TransactionError("活动收场的事实时刻与原事务不同")
        facts = load_activity_of_action(scope.connection, command.action_id)
        for event in saved:
            for row in event["body"]["rows"]:
                if (row["table"] != "device_activities"
                        or row["id"] != facts["id"]):
                    raise TransactionError("原活动收场属于其他活动")
        if facts["occupancy_state"] != 2:
            raise TransactionError("原活动收场的可靠记录与输入不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ActivityConclusion(outcome=ConcludeOutcome.CONCLUDED))


# -- 结果集合核实 -----------------------------------------------------


class ResultSetDisposition(Enum):
    """结果集合核实事务的可靠结果分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class ResultSetOutcome:
    """核实结果：保存后的核实状态与采集判定。"""

    disposition: ResultSetDisposition
    result_set_state: int
    completion_basis: int | None


class _ResultSetConfirmCommand:
    """保存一次结果集合核实事实的事务命令（RESULT_SET_CONFIRMED）。

    BEGIN 只把未核实的集合推进到核实中；结论分支保存驱动规则标
    识、结构化依据与采集结果。时间与产物完成要求固定完成方式、
    可靠发送、已保存的等待完成与一致的引用共同成立；设备证据要
    求活动已经结束。录像活动不适用本命令。
    """

    def __init__(self, command: ResultSetSave, key: OperationKey) -> None:
        if not isinstance(command, ResultSetSave):
            raise TypeError("结果集合核实申请必须使用 ResultSetSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        command = self._command
        facts = load_activity_of_action(connection, command.action_id)
        activity_id = facts["id"]
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        action = row_facts(connection, "actions", facts["action_id"])
        if action is None:
            raise ConsistencyError(f"活动所属动作不存在: {facts['action_id']}")
        self._state["device_activities"] = {activity_id: facts}
        self._state["actions"] = {facts["action_id"]: action}
        self._owners[("device_activities", activity_id)] = (
            "action", facts["action_id"])
        if action["type"] == 2:
            raise ConsistencyError(
                "录像活动不适用结果集合核实，采集判定列保持为空")
        reason, target, outcome = _RESULT_PHASE_TARGETS[command.phase]
        current = facts["result_set_state"]
        if target not in _RESULT_SET_NEXT.get(current, frozenset()):
            raise ConsistencyError(
                f"结果集合状态不能从 {current!r} 推进到 {target!r}")
        before: dict[str, Any] = {"result_set_state": current}
        after: dict[str, Any] = {"result_set_state": target}
        if outcome is not None:
            before["result_check_json"] = facts["result_check_json"]
            after["result_check_json"] = {
                "contract": command.contract,
                "outcome": outcome,
                "observation": dict(command.observation),
            }
        basis, evidence = self._basis_after(facts)
        if basis != facts["completion_basis"]:
            before["completion_basis"] = facts["completion_basis"]
            after["completion_basis"] = basis
            before["completion_evidence_json"] = facts["completion_evidence_json"]
            after["completion_evidence_json"] = evidence
        if command.capture is not None:
            before["capture_json"] = facts["capture_json"]
            after["capture_json"] = dict(command.capture)
        error_after = self._error_after(facts)
        if error_after != facts["last_error_json"]:
            before["last_error_json"] = facts["last_error_json"]
            after["last_error_json"] = error_after
        row = _update("device_activities", activity_id, before, after)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RESULT_SET_EVENT, reason, (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ResultSetOutcome(
                ResultSetDisposition.SAVED, target, basis))

    def _basis_after(self, facts) -> tuple[int | None, dict | None]:
        """按分支与已保存事实确定采集判定；只从待定初始化一次。"""
        command = self._command
        current = facts["completion_basis"]
        if command.phase is ResultSetPhase.BEGIN:
            return current, None
        if command.phase is ResultSetPhase.UNSATISFIED:
            if current not in (None, 1):
                raise ConsistencyError("采集判定已确定，不能再保存明确不满足")
            return 4, dict(command.evidence)
        if command.phase is ResultSetPhase.UNCONFIRMED:
            if current not in (None, 1):
                raise ConsistencyError("采集判定已确定，不能再变为无法确认")
            if command.capture is None or current == 1:
                return current, None
            # 保存未知采集事实要求判定列脱离空值；待定是唯一合法落点。
            return 1, None
        method = command.evidence["method"]
        if method == _TIME_AND_OUTPUTS_METHOD:
            if facts["completion_mode"] != 2:
                raise ConsistencyError("固定完成方式不是时间与产物，判定不适用")
            if (facts["dispatch_state"] != 3 or facts["sent_at"] is None
                    or facts["expected_check_at"] is None
                    or facts["wait_completed_event_id"] is None):
                raise ConsistencyError("时间与产物判定缺少可靠发送与等待完成事实")
            if (command.evidence.get("wait_completed_event_id")
                    != facts["wait_completed_event_id"]):
                raise ConsistencyError("完成依据引用的等待事件与本活动不符")
            if facts["activity_state"] == 2:
                raise ConsistencyError("不能以时间与产物判定覆盖进行中的实际观察")
            return 3, dict(command.evidence)
        if method == _DEVICE_EVIDENCE_METHOD:
            if facts["activity_state"] != 3:
                raise ConsistencyError("设备证据判定要求原活动已经结束")
            return 2, dict(command.evidence)
        raise ConsistencyError(f"未登记的采集判定方法: {method!r}")

    def _error_after(self, facts):
        command = self._command
        if command.phase is ResultSetPhase.COMPLETE:
            return None
        return None if command.error is None else dict(command.error)

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实原分支与输入后恢复首次结果。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        reason = _RESULT_PHASE_TARGETS[command.phase][0]
        if types != [(_RESULT_SET_EVENT, reason)]:
            raise TransactionError("操作身份已用于其他事务，不能作为结果核实重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("结果核实的事实时刻与原事务不同")
        activity_id = load_activity_of_action(
            connection, command.action_id)["id"]
        row = saved[0]["body"]["rows"][0]
        if (row["table"] != "device_activities"
                or row["id"] != activity_id):
            raise TransactionError("原结果核实属于其他活动")
        values = row["after"]["values"]
        expected_check = None if command.phase is ResultSetPhase.BEGIN else {
            "contract": command.contract,
            "outcome": _RESULT_PHASE_TARGETS[command.phase][2],
            "observation": dict(command.observation),
        }
        pairs = (
            ("result_check_json", expected_check),
            ("capture_json", None if command.capture is None else dict(command.capture)),
            ("last_error_json",
             None if command.error is None else dict(command.error)),
        )
        for column, value in pairs:
            if column in values and not json_equal(values.get(column), value):
                raise TransactionError("结果核实的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ResultSetOutcome(
                ResultSetDisposition.ALREADY,
                values["result_set_state"],
                values.get("completion_basis")))


@dataclass(frozen=True)
class ResultCheckOutcome:
    """一轮核实结论事务的结果：尝试结束与集合结论。"""

    finish: FinishAttemptResult
    result_set: ResultSetOutcome


def _merged_state_rows(*states) -> dict[str, dict[int, dict[str, Any]]]:
    """合并子计划的关联事实；同一行的事实必须一致。"""
    merged: dict[str, dict[int, dict[str, Any]]] = {}
    for state in states:
        for table, rows in state.items():
            target = merged.setdefault(table, {})
            for row_id, facts in rows.items():
                known = target.get(row_id)
                if known is None:
                    target[row_id] = facts
                    continue
                for column, value in known.items():
                    if column in facts and not json_equal(value, facts[column]):
                        raise TransactionError("复合事务的关联事实读取不一致")
                target[row_id] = {**known, **facts}
    return merged


class _CompositeScope:
    """复合命令的子范围：共享连接与边界，事件段按调用顺序切分。"""

    def __init__(self, parent, first_event_id: int) -> None:
        self._parent = parent
        self._next_event_id = first_event_id
        self._txn_id = parent.max_txn_id + 1

    @property
    def connection(self):
        return self._parent.connection

    @property
    def max_txn_id(self) -> int:
        return self._parent.max_txn_id

    @property
    def max_event_id(self) -> int:
        return self._parent.max_event_id

    def allocate(self, event_count: int) -> TransactionAllocations:
        first = self._next_event_id
        self._next_event_id += event_count
        return TransactionAllocations(
            txn_id=self._txn_id,
            first_event_id=first,
            last_event_id=first + event_count - 1,
        )


class _FinishStartResultCommand:
    """沿原尝试原子保存启动事实，不生成新的调用或业务观察。"""

    def __init__(self, finish, observation, start_finish, action_finish, expiration, key) -> None:
        self._finish = finish
        self._observation = observation
        self._start_finish = start_finish
        self._action_finish = action_finish
        self._expiration = expiration
        self._key = key

    def _verify(self, connection):
        run = row_facts(connection, "operation_runs", self._finish.ticket.run_id)
        if run is None or not (run["kind"] == int(_RUN_KIND.START) or (
                run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
                and run["query_purpose"] == int(_QUERY_PURPOSE.START_CONFIRMATION))):
            raise TransactionError("启动结果只接受 START 或 START_CONFIRMATION 原尝试")
        if self._observation is not None:
            if self._observation.action_id != run["action_id"]:
                raise TransactionError("启动结果与活动观察必须归同一动作")
            outcome = self._finish.outcome.outcome
            if self._observation.started_at is not None and outcome.effect.value != "confirmed":
                raise TransactionError("启动确认时刻必须由可靠确认结果承载")
        if self._start_finish is not None and (
                run["kind"] != int(_RUN_KIND.QUERY_ACTIVITY)
                or self._start_finish.responsibility_keys != (f"start/{run['action_id']}",)):
            raise TransactionError("启动核实只共同收场其原 START 责任")
        if self._action_finish is not None and self._action_finish.action_id != run["action_id"]:
            raise TransactionError("启动结果与动作终态必须归同一动作")
        if self._expiration is not None and (
                self._expiration.action_id != run["action_id"]
                or run["kind"] != int(_RUN_KIND.START)
                or self._action_finish is not None):
            raise TransactionError("启动结果的过期责任必须属于原 START 且不含另一动作终态")
        return run

    def _action_command(self):
        return FinishCaptureCommand(
            self._action_finish, self._key,
            canceled=isinstance(self._action_finish, FinishCanceledCapture))

    def plan(self, scope):
        self._verify(scope.connection)
        saved = saved_transaction_events(scope.connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        finish = FinishAttemptCommand(self._finish, self._key).plan(sub)
        if finish.read_only:
            return finish
        plans = [finish]
        if self._observation is not None:
            plans.append(_ActivityObserveCommand(self._observation, self._key).plan(sub))
        if self._start_finish is not None:
            plans.append(_FinishStaleRunsCommand(self._start_finish, self._key).plan(sub))
        if self._action_finish is not None:
            prior_events = tuple(event for plan in plans for event in plan.events)
            plans.append(self._action_command().plan(sub, start_result_events=prior_events))
        if self._expiration is not None:
            prior_events = tuple(event for plan in plans for event in plan.events)
            expire = ExpireActionCommand(self._expiration, self._key).plan(
                sub, start_result_events=prior_events)
            if expire.result.outcome is not ExpireOutcome.EXPIRED:
                raise TransactionError("原启动结果不满足共同过期的可靠资格")
            plans.append(expire)
        if (isinstance(self._action_finish, FinishCapture)
                and self._action_finish.failure is not None
                and self._finish.outcome.outcome.effect is EffectState.NO_EFFECT):
            prior_events = tuple(event for plan in plans for event in plan.events)
            action = row_facts(scope.connection, "actions", self._action_finish.action_id)
            if load_start_facts(scope.connection, action, result_events=prior_events).not_started:
                plans.append(_ActivityReleaseCommand(ActivityReleaseSave(
                    action["id"], self._finish.occurred_at), self._key).plan(
                        sub, activity_result_events=prior_events))
        events = tuple(event for plan in plans for event in plan.events)
        scope.allocate(len(events))
        return CommandPlan(
            events=events,
            owners={member: owner for plan in plans for member, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            result=finish.result)

    def _reuse(self, scope, saved):
        # 原结果段始终在前；后续活动和 START 段按明确责任分开核对。
        cut = 1
        if len(saved) > 1 and saved[1]["type"] in (_RETRY_WAIT_EVENT, _RUN_END_EVENT):
            cut = 2
        plans = [FinishAttemptCommand(self._finish, self._key)._reuse(scope, saved[:cut])]
        if self._observation is not None:
            if cut >= len(saved) or saved[cut]["type"] != _ACTIVITY_OBSERVE_EVENT:
                raise TransactionError("原启动结果缺少对应活动观察")
            observe = _ActivityObserveCommand(self._observation, self._key)
            observe._activity_id = load_activity_of_action(
                scope.connection, self._observation.action_id)["id"]
            plans.append(observe._reuse(saved[cut:cut + 1]))
            cut += 1
        if self._start_finish is not None:
            end = len(saved)
            if self._action_finish is not None:
                end = next((index for index in range(cut, len(saved))
                            if saved[index]["type"] == _ACTION_FINISHED_EVENT), len(saved))
            plans.append(_FinishStaleRunsCommand(
                self._start_finish, self._key)._reuse(scope, saved[cut:end]))
            cut = end
        if self._action_finish is not None:
            release = (isinstance(self._action_finish, FinishCapture)
                       and self._action_finish.failure is not None
                       and self._finish.outcome.outcome.effect is EffectState.NO_EFFECT
                       and (saved[-1]["type"], saved[-1]["reason"]) == (
                           _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_RELEASE_REASON))
            end = len(saved) - int(release)
            plans.append(self._action_command()._reuse(scope.connection, saved[cut:end]))
            if release:
                plans.append(_ActivityReleaseCommand(ActivityReleaseSave(
                    self._action_finish.action_id, self._finish.occurred_at), self._key)._reuse(
                        scope, saved[end:]))
            cut = len(saved)
        if self._expiration is not None:
            plans.append(ExpireActionCommand(self._expiration, self._key)._reuse(scope, saved[cut:]))
            cut = len(saved)
        if cut != len(saved):
            raise TransactionError("原启动结果包含与本次输入无关的事实")
        return CommandPlan(
            events=(), owners={member: owner for plan in plans for member, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            read_only=True, result=plans[0].result)


class _CloseStartCommand:
    def __init__(self, finish, action_finish, key):
        self._finish, self._action_finish, self._key = finish, action_finish, key

    def plan(self, scope):
        action = row_facts(scope.connection, "actions", self._action_finish.action_id)
        if action is None or f"start/{action['id']}" not in self._finish.responsibility_keys:
            raise TransactionError("启动收场必须包含原动作 START 责任")
        if self._finish.status is RunOutcome.FAILED:
            if not load_start_facts(scope.connection, action).not_started:
                raise TransactionError("确定启动失败需要全部原尝试可靠无效果")
        saved = saved_transaction_events(scope.connection, self._key)
        end = _FinishStaleRunsCommand(self._finish, self._key)
        finish = FinishCaptureCommand(self._action_finish, self._key)
        if saved is not None:
            cut = next((index for index, event in enumerate(saved)
                        if event["type"] == _ACTION_FINISHED_EVENT), len(saved))
            release = (self._finish.status is RunOutcome.FAILED
                       and (saved[-1]["type"], saved[-1]["reason"]) == (
                           _ACTIVITY_OBSERVE_EVENT, _ACTIVITY_RELEASE_REASON))
            finish_end = len(saved) - int(release)
            plans = [end._reuse(scope, saved[:cut]),
                     finish._reuse(scope.connection, saved[cut:finish_end])]
            if release:
                plans.append(_ActivityReleaseCommand(ActivityReleaseSave(
                    action["id"], self._finish.occurred_at), self._key)._reuse(
                        scope, saved[finish_end:]))
        else:
            sub = _CompositeScope(scope, scope.max_event_id + 1)
            plans = [end.plan(sub), finish.plan(sub)]
            if self._finish.status is RunOutcome.FAILED:
                prior_events = tuple(event for plan in plans for event in plan.events)
                plans.append(_ActivityReleaseCommand(ActivityReleaseSave(
                    action["id"], self._finish.occurred_at), self._key).plan(
                        sub, activity_result_events=prior_events))
            scope.allocate(sum(len(plan.events) for plan in plans))
        return CommandPlan(
            events=tuple(event for plan in plans for event in plan.events),
            owners={member: owner for plan in plans for member, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            read_only=saved is not None, result=None)


class _FinishBindingFailureCommand:
    """保存拍摄绑定异常的动作结果与相关设备责任，不改写调用事实。"""

    def __init__(self, request: FinishBindingFailure, key: OperationKey) -> None:
        self._request = request
        self._key = key

    def _capture_command(self) -> FinishCaptureCommand:
        request = self._request
        if request.canceled:
            return FinishCaptureCommand(
                FinishCanceledCapture(request.action_id, request.occurred_at),
                self._key, canceled=True)
        return FinishCaptureCommand(FinishCapture(
            request.action_id, (), OutputCatalogFacts(
                action_id=request.action_id, ownership_confirmed=True),
            request.occurred_at, failure=request.failure), self._key)

    def _error(self) -> ErrorValue:
        return ErrorValue(
            "device_binding_unavailable", "execution", self._request.failure.details)

    def _verify_binding(self, action) -> None:
        details = self._request.failure.details
        if (details["device_id"] != action["device_id"]
                or details["expected_driver_id"] != action["driver_id"]):
            raise TransactionError("绑定失败输入与原动作设备身份不符")

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        request = self._request
        action = row_facts(connection, "actions", request.action_id)
        if action is None:
            raise TransactionError("绑定失败动作不存在")
        self._verify_binding(action)
        if action["status"] in _ACTION_TERMINAL:
            return self._capture_command()._recover(connection, action)
        if bool(action["cancel_requested"]) != request.canceled:
            raise TransactionError("绑定失败的取消分支与原动作事实不符")
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
            " WHERE r.action_id = ? AND t.status = 1 LIMIT 1", (request.action_id,),
        )) as cursor:
            if cursor.fetchone() is not None:
                raise TransactionError("原调用尚未保存可靠结束结果，不能用绑定失败代替调用收场")
        facts = load_start_facts(connection, action)
        with closing(connection.execute(
            "SELECT responsibility_key FROM operation_runs WHERE action_id = ?"
            " AND kind IN (1, 2, 3, 6, 7) AND status IN (1, 2)"
            " AND (kind != 6 OR query_purpose != 5) ORDER BY id", (request.action_id,),
        )) as cursor:
            responsibilities = tuple(row[0] for row in cursor.fetchall())
        if set(responsibilities) != set(request.responsibility_keys):
            raise TransactionError("绑定失败的固定责任集合与事务内未完成责任不符")
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        capture_plan = self._capture_command().plan(sub)
        if capture_plan.read_only:
            return capture_plan
        plans = [capture_plan]
        if responsibilities:
            plans.append(_FinishStaleRunsCommand(StaleRunFinish(
                responsibilities, RunOutcome.FAILED, request.occurred_at,
                self._error()), self._key).plan(sub))
        plans.extend(self._read_business_plans(sub, action))
        if request.canceled and facts.activity is not None:
            with closing(connection.execute(
                "SELECT id FROM operation_runs WHERE responsibility_key = ?",
                (f"stop/{request.action_id}",),
            )) as cursor:
                has_stop = cursor.fetchone() is not None
            next_run_id = _next_id(connection, "operation_runs")
            if (not has_stop and not facts.not_started
                    and facts.activity["activity_state"] != 3
                    and facts.activity["stop_supported"] == 1):
                plans.append(self._new_failed_run(
                    sub, action, facts.activity, next_run_id, int(_RUN_KIND.STOP)))
                next_run_id += 1
            if action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE) and not facts.not_started:
                with closing(connection.execute(
                    "SELECT id FROM operation_runs WHERE responsibility_key = ?",
                    (f"results/{facts.activity['id']}",),
                )) as cursor:
                    has_results = cursor.fetchone() is not None
                if not has_results:
                    plans.append(self._new_failed_run(
                        sub, action, facts.activity, next_run_id, int(_RUN_KIND.CHECK_CAPTURE_RESULTS)))
        events = tuple(event for plan in plans for event in plan.events)
        allocation = scope.allocate(len(events))
        if (events[0].event_id != allocation.first_event_id
                or events[-1].event_id != allocation.last_event_id):
            raise TransactionError("绑定失败复合事务的事件范围不连续")
        ranges = {}
        for plan in plans:
            for identity, values in plan.read_coverage.ranges.items():
                ranges.setdefault(identity, set()).update(values)
        return CommandPlan(
            events=events,
            owners={owner: target for plan in plans for owner, target in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            read_coverage=ReadCoverage(ranges), result=capture_plan.result)

    def _read_business_plans(self, scope, action):
        """原读取实际结果可靠后，绑定失败共同结束内部处理与机会。"""
        from camctl.outputs.slots import SlotOutcome, SlotRequest
        from camctl.persistence.repositories.outputs import _SlotChangeCommand, _SLOT_RELEASE

        request = self._request
        rows = scope.connection.execute(
            "SELECT c.id,c.processing_id FROM file_copies c JOIN recording_processing p ON p.id=c.processing_id"
            " JOIN operation_runs r ON r.copy_id=c.id WHERE p.action_id=? AND r.kind=3 ORDER BY c.id",
            (request.action_id,)).fetchall()
        plans = []
        for copy_id, processing_id in rows:
            unfinished = scope.connection.execute(
                "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.copy_id=? AND (t.status=1 OR t.result_json IS NULL) LIMIT 1", (copy_id,)).fetchone()
            if unfinished is not None:
                raise TransactionError("原内部读取未可靠结束，不能解除保护")
            processing = row_facts(scope.connection, "recording_processing", processing_id)
            processing_command = _ProcessingCommand()
            processing_command._state = {"recording_processing": {processing_id: processing}}
            processing_command._claim(processing)
            for event_type, reason, row in self._read_binding_changes(processing, action):
                plans.append(processing_command._emit(scope, event_type, reason, row, request.occurred_at))
            slot = _SlotChangeCommand(SlotRequest(copy_id, request.occurred_at), _SLOT_RELEASE, self._key)
            copy = slot._load_copy(scope.connection)
            source = slot._load_source(scope.connection, copy)
            device_id = slot._source_device(scope.connection, source)
            run = slot._load_run(scope.connection, copy_id)
            slot._load_attempts(scope.connection, run["id"])
            if copy["slot_device_id"] is not None:
                if copy["slot_device_id"] != device_id:
                    raise TransactionError("原内部读取机会不属于原绑定设备")
                plans.append(slot._save(scope, copy, device_id, None, SlotOutcome.RELEASED))
        return plans

    def _read_binding_changes(self, processing, action):
        """以原处理状态确定绑定失败负责的全部检查、修复变化。"""
        from camctl.capture.processing import (
            CheckPhase, MediaObservation, ProcessingError, RepairBasis,
            RepairDecisionChoice, RepairReason,
        )
        error = ProcessingError("device_binding_unavailable", "execution", self._request.failure.details)
        changes = []
        if processing["check_decision"] == 3 and processing["check_state"] in (1, 2):
            changes.append((_RECORDING_PROCESSED_EVENT, _PROCESSED_CHECK_REASON,
                _update("recording_processing", processing["id"],
                    {"check_state": processing["check_state"], "media_json": processing["media_json"]},
                    {"check_state": CheckPhase.FAILED.value,
                     "media_json": MediaObservation(CheckPhase.FAILED, error=error).as_json()})))
        if processing["repair_state"] == 1:
            changes.append((_RECORDING_DECIDED_EVENT, _DECIDED_REPAIR_REASON,
                _update("recording_processing", processing["id"],
                    {"repair_state": processing["repair_state"], "repair_basis_json": processing["repair_basis_json"]},
                    {"repair_state": RepairDecisionChoice.NOT_NEEDED.value,
                     "repair_basis_json": RepairBasis(RepairReason.NO_USABLE_INPUT,
                         action["execution_spec_json"]["target_duration_ms"]).as_json()})))
        elif processing["repair_state"] in (3, 4):
            changes.append((_RECORDING_PROCESSED_EVENT, _PROCESSED_REPAIR_REASON,
                _update("recording_processing", processing["id"],
                    {"repair_state": processing["repair_state"], "repair_error_json": processing["repair_error_json"]},
                    {"repair_state": RepairOutcome.FAILED.value, "repair_error_json": error.as_json()})))
        return changes

    def _reuse_read_business(self, scope, saved, events, action):
        """从原事务前完整 H 重建 READ 变化组并精确核实全部正文。"""
        from camctl.outputs.slots import SlotRequest
        from camctl.persistence.repositories.outputs import _SlotChangeCommand, _SLOT_RELEASE, _SLOT_REASON

        connection = scope.connection
        transaction = saved[0]["transaction"]
        previous = read_transaction_range(connection, transaction.txn_id - 1) if transaction.txn_id > 1 else None
        boundary = HistoryBoundary(previous.txn_id, previous.last_event_id) if previous else HistoryBoundary(0, 0)
        current = HistoryBoundary(scope.max_txn_id, scope.max_event_id)

        def original(table, facts, owner):
            if facts is None:
                raise ConsistencyError("原内部读取关联记录缺失")
            columns = business_columns(table)
            return {**facts, **read_row_values_at_boundary(connection, owner=owner, table=table,
                row_id=facts["id"], columns=columns, current_values=facts,
                boundary=boundary, current_boundary=current)}

        owner = ("action", action["id"])
        original_action = original("actions", action, owner)
        if bool(original_action["cancel_requested"]) != self._request.canceled:
            raise TransactionError("原绑定失败的取消输入与原 H 不同")
        copies = connection.execute(
            "SELECT c.id FROM file_copies c JOIN recording_processing p ON p.id=c.processing_id"
            " JOIN operation_runs r ON r.copy_id=c.id WHERE p.action_id=? AND r.kind=3 ORDER BY c.id",
            (action["id"],)).fetchall()
        expected = []
        for (copy_id,) in copies:
            slot = _SlotChangeCommand(SlotRequest(copy_id, self._request.occurred_at), _SLOT_RELEASE, self._key)
            copy = original("file_copies", slot._load_copy(connection), owner)
            processing = original("recording_processing",
                row_facts(connection, "recording_processing", copy["processing_id"]), owner)
            source = original("device_files", slot._load_source(connection, copy),
                ("device_file", copy["source_device_file_id"]))
            observer = original("actions", row_facts(connection, "actions", source["observer_action_id"]),
                ("action", source["observer_action_id"]))
            origin = original("actions", row_facts(connection, "actions", source["source_action_id"]),
                ("action", source["source_action_id"]))
            target = original("intermediate_files", row_facts(connection, "intermediate_files", copy["target_file_id"]),
                ("intermediate_file", copy["target_file_id"]))
            run = original("operation_runs", slot._load_run(connection, copy_id), owner)
            if (copy["delivery_id"] is not None or processing["action_id"] != action["id"]
                    or run["action_id"] != action["id"] or run["kind"] != 3 or run["copy_id"] != copy_id
                    or run["responsibility_key"] != f"read/{copy_id}"
                    or source["id"] != processing["source_device_file_id"]
                    or target["owner_action_id"] != action["id"] or target["owner_delivery_id"] is not None
                    or (observer["device_id"], observer["driver_id"]) != (origin["device_id"], origin["driver_id"])
                    or (origin["device_id"], origin["driver_id"]) != (action["device_id"], action["driver_id"])):
                raise TransactionError("原 H 的内部读取归属、文件或设备身份不符")
            slot._load_attempts(connection, run["id"])
            for attempt in slot._state["operation_attempts"].values():
                attempt = original("operation_attempts", attempt, owner)
                if attempt["run_id"] != run["id"] or attempt["status"] == 1 or attempt["result_json"] is None:
                    raise TransactionError("原 H 的内部读取实际调用未可靠结束")
            expected.extend(self._read_binding_changes(processing, original_action))
            if copy["slot_device_id"] is not None:
                if copy["slot_device_id"] != origin["device_id"]:
                    raise TransactionError("原 H 的读取机会不属于原来源设备")
                expected.append((22, _SLOT_REASON, _update("file_copies", copy_id,
                    {"slot_device_id": copy["slot_device_id"]}, {"slot_device_id": None})))
        if len(events) != len(expected):
            raise TransactionError("原绑定失败的内部读取事件组不完整")
        for event, (event_type, reason, row) in zip(events, expected):
            body = {"reason": reason, "evidence": {}, "rows": [{
                "table": row.table, "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)}}]}
            if (event["type"] != event_type or event["reason"] != reason
                    or event["occurred_at"] != self._request.occurred_at or not json_equal(event["body"], body)):
                raise TransactionError("原绑定失败的内部读取完整事实与原 H 或重送输入不同")

    def _new_run_values(self, action, activity, kind) -> dict:
        request = self._request
        if kind == int(_RUN_KIND.STOP):
            config = request.stop_config
            responsibility = f"stop/{action['id']}"
        elif kind == int(_RUN_KIND.CHECK_CAPTURE_RESULTS):
            config = request.check_config
            responsibility = f"results/{activity['id']}"
        else:
            raise TransactionError("绑定失败只能新建必要停止或结果核实责任")
        if config is None:
            raise TransactionError("零尝试必要责任缺少本次采用配置")
        return {
            "action_id": action["id"], "delivery_id": None,
            "kind": kind, "query_purpose": None,
            "responsibility_key": responsibility,
            "activity_id": activity["id"], "copy_id": None,
            "cleanup_item_id": None, "session_key": None,
            "status": 1, "attempts_used": 0,
            "max_attempts_used": config.max_attempts,
            "timeout_s_json": config.timeout_s,
            "retry_interval_s_json": config.retry_interval_s,
            "retry_wait_required": 0, "error_json": None,
        }

    def _new_failed_run(self, scope, action, activity, run_id, kind) -> CommandPlan:
        request = self._request
        values = self._new_run_values(action, activity, kind)
        allocation = scope.allocate(2)
        return CommandPlan(events=(
            _envelope(allocation.first_event_id, allocation.txn_id, 10, 1,
                      (_row("operation_runs", run_id, values),), request.occurred_at),
            _envelope(allocation.first_event_id + 1, allocation.txn_id, 10, 3,
                      (_update("operation_runs", run_id,
                               {"status": 1, "error_json": None},
                               {"status": 4, "error_json": {
                                   "code": self._error().code, "stage": self._error().stage,
                                   "details": dict(self._error().details)}}),), request.occurred_at),
        ), owners={("operation_runs", run_id): ("action", action["id"])},
            state_rows={"actions": {action["id"]: action},
                        "device_activities": {activity["id"]: activity}})

    def _reuse(self, scope, saved) -> CommandPlan:
        request = self._request
        connection = scope.connection
        action = row_facts(connection, "actions", request.action_id)
        if action is None:
            raise ConsistencyError("原绑定失败动作不存在")
        self._verify_binding(action)
        split = next((index for index, event in enumerate(saved)
                      if event["type"] not in (8, 9)), len(saved))
        capture_events, responsibility_events = saved[:split], saved[split:]
        flow_events = [event for event in responsibility_events if event["type"] == 10]
        read_events = [event for event in responsibility_events if event["type"] != 10]
        # 检查、修复和机会释放必须连续，且位于原流程结束之后、新必要流程之前。
        if read_events:
            first = responsibility_events.index(read_events[0])
            last = responsibility_events.index(read_events[-1])
            if (responsibility_events[first:last + 1] != read_events
                    or any(event["reason"] != 3 for event in responsibility_events[:first])):
                raise TransactionError("原绑定失败的内部读取事件顺序不符")
        self._reuse_read_business(scope, saved, read_events, action)
        if [event["type"] for event in capture_events] not in ([8], [8, 9]):
            raise TransactionError("原绑定失败事务的动作和计划事件组不符")
        capture_plan = self._capture_command()._reuse(connection, capture_events)
        plans = [capture_plan]
        finished_keys = set()
        created_keys = set()
        for event in flow_events:
            if (event["type"] != 10 or event["reason"] not in (1, 3)
                    or event["occurred_at"] != request.occurred_at):
                raise TransactionError("原绑定失败事务包含不同责任或事实时刻")
            rows = event["body"]["rows"]
            if len(rows) != 1 or rows[0]["table"] != "operation_runs":
                raise TransactionError("原绑定失败事务的流程事件必须恰更新一条流程")
            run = row_facts(connection, "operation_runs", rows[0]["id"])
            if (run is None or run["action_id"] != request.action_id
                    or run["kind"] not in (1, 2, 3, 6, 7)
                    or (run["kind"] == 6 and run["query_purpose"] == 5)):
                raise TransactionError("原绑定失败流程的实际责任与输入不符")
            if event["reason"] == 1:
                if (not request.canceled or run["kind"] not in (
                        int(_RUN_KIND.STOP), int(_RUN_KIND.CHECK_CAPTURE_RESULTS))
                        or (run["kind"] == int(_RUN_KIND.CHECK_CAPTURE_RESULTS)
                            and action["type"] != int(_ACTION_TYPE.CAMERA_TIMELAPSE))):
                    raise TransactionError("原零尝试必要责任与取消分支不符")
                activity = row_facts(connection, "device_activities", run["activity_id"])
                if activity is None or activity["action_id"] != request.action_id:
                    raise TransactionError("原零尝试必要责任与目标活动不符")
                values = rows[0]["after"]["values"]
                if (rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]
                        or not json_equal(values, self._new_run_values(action, activity, run["kind"]))):
                    raise TransactionError("原零尝试必要责任或配置与重送输入不同")
                if run["responsibility_key"] in created_keys:
                    raise TransactionError("绑定失败事务不能重复建立同一必要责任")
                created_keys.add(run["responsibility_key"])
                continue
            key = run["responsibility_key"]
            if key in finished_keys:
                raise TransactionError("绑定失败事务不能重复结束同一责任")
            expected_error = {
                "code": self._error().code, "stage": self._error().stage,
                "details": dict(self._error().details),
            }
            if not json_equal(rows[0]["after"]["values"].get("error_json"), expected_error):
                raise TransactionError("原绑定失败的流程错误与重送输入不同")
            finished_keys.add(key)
            plans.append(_FinishStaleRunsCommand(StaleRunFinish(
                (run["responsibility_key"],), RunOutcome.FAILED,
                request.occurred_at, self._error()), self._key)._reuse(scope, [event]))
        expected_keys = set(request.responsibility_keys)
        if created_keys & expected_keys:
            raise TransactionError("原事务新建的必要责任不属于此前固定责任集合")
        expected_keys.update(created_keys)
        if finished_keys != expected_keys:
            raise TransactionError("原绑定失败事务的完整责任集合与重送输入不同")
        if request.canceled and not flow_events:
            raise TransactionError("绑定异常的取消终态缺少必要流程失败事实")
        return CommandPlan(events=(),
            owners={owner: target for plan in plans for owner, target in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            read_only=True, result=capture_plan.result)


class _FinishResidualBindingFailureCommand:
    """残留流程归原活动，新触发动作的冲突失败归自身，整组提交。"""

    def __init__(self, request: FinishResidualBindingFailure, key: OperationKey) -> None:
        self._request = request
        self._key = key

    def _target(self, connection):
        request = self._request
        activity = row_facts(connection, "device_activities", request.activity_id)
        owner = None if activity is None else row_facts(connection, "actions", activity["action_id"])
        details = request.failure.details
        if (owner is None or details["device_id"] != owner["device_id"]
                or details["expected_driver_id"] != owner["driver_id"]):
            raise TransactionError("原残留绑定错误与活动所属设备及驱动不符")
        return activity, owner

    def _capture_command(self, owner):
        request = self._request
        return FinishCaptureCommand(FinishCapture(
            request.action_id, (), OutputCatalogFacts(request.action_id, True),
            request.occurred_at, RecordingFailure("device_activity_unresolved", {
                "activity_id": str(request.activity_id), "device_id": owner["device_id"]})), self._key)

    def _error(self):
        return ErrorValue("device_binding_unavailable", "execution", self._request.failure.details)

    def _run(self, connection, run_id):
        run = row_facts(connection, "operation_runs", run_id)
        if (run is None or run["activity_id"] != self._request.activity_id
                or not (run["kind"] == 8 or (run["kind"] == 6 and run["query_purpose"] == 5))):
            raise TransactionError("原残留绑定事务包含不同活动或不适用的流程责任")
        return run

    def plan(self, scope):
        request = self._request
        connection = scope.connection
        activity, owner = self._target(connection)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved, owner)
        if (owner["status"] not in _ACTION_TERMINAL
                or activity["activity_state"] != 2 or activity["occupancy_state"] != 1):
            raise TransactionError("原残留绑定失败要求已结束动作仍保持执行中占用")
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE activity_id = ? AND status IN (1, 2)"
            " AND (kind = 8 OR (kind = 6 AND query_purpose = 5)) ORDER BY id",
            (request.activity_id,),
        )) as cursor:
            runs = [self._run(connection, int(row[0])) for row in cursor.fetchall()]
        if (not any(run["kind"] == 8 for run in runs)
                or {run["responsibility_key"] for run in runs} != set(request.responsibility_keys)):
            raise TransactionError("原残留绑定失败的完整责任集合与事务内事实不符")
        ids = tuple(run["id"] for run in runs)
        marks = ",".join("?" for _ in ids)
        with closing(connection.execute(
            f"SELECT 1 FROM operation_attempts WHERE run_id IN ({marks}) AND status = 1 LIMIT 1", ids,
        )) as cursor:
            if cursor.fetchone() is not None:
                raise TransactionError("原残留调用未取得结束结果，不能提前保存绑定失败")
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        plans = []
        if request.action_id is not None:
            trigger = row_facts(connection, "actions", request.action_id)
            if (trigger is None or trigger["device_id"] != owner["device_id"]
                    or trigger["status"] != _ACTION_RUNNING or trigger["cancel_requested"]):
                raise TransactionError("原残留占用失败要求同设备仍有效的执行中触发动作")
            plans.append(self._capture_command(owner).plan(sub))
        plans.append(_FinishStaleRunsCommand(StaleRunFinish(
            request.responsibility_keys, RunOutcome.FAILED, request.occurred_at, self._error()),
            self._key).plan(sub))
        events = tuple(event for plan in plans for event in plan.events)
        allocation = scope.allocate(len(events))
        if (events[0].event_id != allocation.first_event_id
                or events[-1].event_id != allocation.last_event_id):
            raise TransactionError("原残留绑定复合事务的事件范围不连续")
        return CommandPlan(
            events=events, owners={row: owner for plan in plans for row, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            result=plans[0].result if request.action_id is not None else None)

    def _reuse(self, scope, saved, owner):
        request = self._request
        plans = []
        if request.action_id is None:
            flow_events = saved
        else:
            cut = next((i for i, event in enumerate(saved) if event["type"] == 10), len(saved))
            action_events, flow_events = saved[:cut], saved[cut:]
            if [event["type"] for event in action_events] not in ([8], [8, 9]):
                raise TransactionError("原残留绑定失败的动作及计划段与请求不同")
            plans.append(self._capture_command(owner)._reuse(scope.connection, action_events))
        keys = set()
        expected_error = {"code": self._error().code, "stage": self._error().stage,
                          "details": dict(self._error().details)}
        for event in flow_events:
            if (event["type"] != 10 or event["reason"] != 3
                    or event["occurred_at"] != request.occurred_at):
                raise TransactionError("原残留绑定失败包含不同事件或事实时刻")
            rows = event["body"]["rows"]
            if len(rows) != 1 or rows[0]["table"] != "operation_runs":
                raise TransactionError("原残留流程结束必须恰更新一项固定责任")
            run = self._run(scope.connection, rows[0]["id"])
            if (run["responsibility_key"] in keys
                    or not json_equal(rows[0]["after"]["values"].get("error_json"), expected_error)):
                raise TransactionError("原残留责任重复或绑定错误与重送输入不同")
            keys.add(run["responsibility_key"])
            plans.append(_FinishStaleRunsCommand(StaleRunFinish(
                (run["responsibility_key"],), RunOutcome.FAILED,
                request.occurred_at, self._error()), self._key)._reuse(scope, [event]))
        if keys != set(request.responsibility_keys):
            raise TransactionError("原残留绑定失败的完整责任集合与重送输入不同")
        return CommandPlan(
            events=(), owners={row: owner for plan in plans for row, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)), read_only=True,
            result=plans[0].result if request.action_id is not None else None)


class _FinishResultCheckCommand:
    """一轮核实结论与尝试结束、流程收场同事务提交的命令。

    结论依据与承载它的列举轮次原子保存：任一侧输入被拒整组回滚，
    不留下已结束而无结论的轮次；重送按原事务分段恢复。
    """

    def __init__(self, finish: AttemptFinish, confirm: ResultSetSave,
                 key: OperationKey) -> None:
        self._finish = finish
        self._confirm = confirm
        self._key = key

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        # 一次总分配覆盖尝试结果、流程收场与集合结论三个事件。
        allocation = scope.allocate(3)
        sub = _CompositeScope(scope, allocation.first_event_id)
        finish_plan = FinishAttemptCommand(self._finish, self._key).plan(sub)
        if finish_plan.read_only:
            raise TransactionError("结论轮次的尝试已结束，不能再次携带结论提交")
        confirm_plan = _ResultSetConfirmCommand(self._confirm, self._key).plan(sub)
        return CommandPlan(
            events=(*finish_plan.events, *confirm_plan.events),
            owners={**finish_plan.owners, **confirm_plan.owners},
            state_rows=_merged_state_rows(
                finish_plan.state_rows, confirm_plan.state_rows),
            result=ResultCheckOutcome(
                finish=finish_plan.result, result_set=confirm_plan.result),
        )

    def _reuse(self, scope, saved) -> CommandPlan:
        types = [(event["type"], event["reason"]) for event in saved]
        if (len(types) != 3 or types[0][0] != _ATTEMPT_RESULT_EVENT
                or types[1] != (_RUN_END_EVENT, 3)
                or types[2][0] != _RESULT_SET_EVENT):
            raise TransactionError("操作身份已用于其他事务，不能作为核实结论重送")
        finish_plan = FinishAttemptCommand(
            self._finish, self._key)._reuse(scope, saved[:2])
        confirm_plan = _ResultSetConfirmCommand(
            self._confirm, self._key)._reuse(scope.connection, saved[2:])
        return CommandPlan(
            events=(),
            owners={**finish_plan.owners, **confirm_plan.owners},
            state_rows=_merged_state_rows(
                finish_plan.state_rows, confirm_plan.state_rows),
            read_only=True,
            result=ResultCheckOutcome(
                finish=finish_plan.result, result_set=confirm_plan.result),
        )


class _CloseResultCheckCommand:
    """预算耗尽时结束核实流程并保存无法确认结论的同事务命令。

    没有在途尝试可承载结论：流程行按结果核实责任定位，与集合结
    论原子收场；流程错误按登记的公共错误结构保存。
    """

    def __init__(self, command: ResultSetSave, key: OperationKey) -> None:
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        command = self._command
        if command.phase is not ResultSetPhase.UNCONFIRMED:
            raise TransactionError("耗尽收场只保存无法确认的集合结论")
        run_facts = _result_run_of_action(connection, command.action_id)
        if run_facts["status"] not in (1, 2):
            raise ConsistencyError("已结束的核实流程不能再次收场")
        self._state["operation_runs"] = {run_facts["id"]: run_facts}
        error = self._run_error(connection)
        # 流程收场与集合结论共用一次总分配的两个事件段。
        allocation = scope.allocate(2)
        sub = _CompositeScope(scope, allocation.first_event_id)
        run_allocation = sub.allocate(1)
        run_row = _update(
            "operation_runs",
            run_facts["id"],
            {
                "status": run_facts["status"],
                "retry_wait_required": run_facts["retry_wait_required"],
                "error_json": run_facts["error_json"],
            },
            {
                "status": int(_RUN_STATUS.UNCONFIRMED),
                "retry_wait_required": 0,
                "error_json": error,
            },
        )
        self._owners[("operation_runs", run_facts["id"])] = (
            "action", run_facts["action_id"])
        run_event = _envelope(
            run_allocation.first_event_id, run_allocation.txn_id,
            _RUN_END_EVENT, 3, (run_row,), command.occurred_at)
        confirm_plan = _ResultSetConfirmCommand(command, self._key).plan(sub)
        return CommandPlan(
            events=(run_event, *confirm_plan.events),
            owners={**self._owners, **confirm_plan.owners},
            state_rows=_merged_state_rows(self._state, confirm_plan.state_rows),
            result=confirm_plan.result,
        )

    def _run_error(self, connection) -> dict[str, Any]:
        """按登记的公共错误结构构造流程错误（有限核实后结果未知）。"""
        return _unconfirmed_run_error(
            load_activity_of_action(connection, self._command.action_id)["id"])

    def _reuse(self, connection, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_RUN_END_EVENT, 3),
                     (_RESULT_SET_EVENT, _RESULT_PHASE_TARGETS[command.phase][0])]:
            raise TransactionError("操作身份已用于其他事务，不能作为耗尽收场重送")
        if any(event["occurred_at"] != command.occurred_at for event in saved):
            raise TransactionError("耗尽收场的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "operation_runs"
                or not rows[0]["after"]["exists"]):
            raise TransactionError("原收场缺少流程事实")
        values = rows[0]["after"]["values"]
        if (values.get("status") != int(_RUN_STATUS.UNCONFIRMED)
                or values.get("retry_wait_required") != 0
                or not json_equal(values.get("error_json"), self._run_error(connection))):
            raise TransactionError("耗尽收场的重送输入与原事务不同")
        confirm_plan = _ResultSetConfirmCommand(
            command, self._key)._reuse(connection, saved[1:])
        return CommandPlan(
            events=(),
            owners=confirm_plan.owners,
            state_rows=_merged_state_rows(self._state, confirm_plan.state_rows),
            read_only=True,
            result=confirm_plan.result,
        )


def _result_run_of_action(connection, action_id: int) -> dict[str, Any]:
    """以动作关联的实际活动定位原核实责任，不混用两者的编号。"""
    activity = load_activity_of_action(connection, action_id)
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key=?",
        (f"results/{activity['id']}",),
    )) as cursor:
        found = cursor.fetchone()
    facts = None if found is None else row_facts(connection, "operation_runs", int(found[0]))
    if (facts is None or facts["action_id"] != action_id
            or facts["activity_id"] != activity["id"]
            or facts["kind"] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS)):
        raise ConsistencyError(f"动作 {action_id} 缺少所属活动的原结果核实流程")
    return facts


def _unconfirmed_run_error(activity_id: int) -> dict[str, Any]:
    """按登记的公共错误结构构造核实流程错误（有限轮次后结果未知）。"""
    code = "capture_result_unconfirmed"
    spec = registered_error(code)
    details = {
        "activity_id": str(activity_id),
        "reason": "outputs_unknown",
    }
    validate_error_details(code, details)
    return {"code": code, "stage": spec["stage"], "details": details}


class _FinishRecordingResultsCommand:
    """已保存真实轮次后的原核实责任与录像终态共同提交。"""

    def __init__(self, request: FinishRecordingResults, key: OperationKey) -> None:
        if not isinstance(request, FinishRecordingResults):
            raise TypeError("录像核实收场申请必须使用 FinishRecordingResults")
        self._request, self._key = request, key

    def _identity(self, connection):
        capture = self._request.capture
        action = row_facts(connection, "actions", capture.action_id)
        if action is None or action["type"] != int(_ACTION_TYPE.CAMERA_RECORD):
            raise TransactionError("录像核实收场必须属于原录像动作")
        run = _result_run_of_action(connection, capture.action_id)
        if run["id"] != self._request.run_id:
            raise TransactionError("录像核实收场的原流程与活动身份不符")
        if (not isinstance(capture.catalog_facts, OutputCatalogFacts)
                or capture.catalog_facts.action_id != capture.action_id
                or capture.catalog_facts.ownership_confirmed is not True
                or any(draft.file_complete is not True or draft.sha256 is not None
                       for draft in capture.drafts)):
            raise TransactionError("录像核实收场要求原完整文件登记申请")
        validate_output_registration(capture.drafts, capture.catalog_facts)
        return action, run

    def _ready(self, connection, action, run) -> None:
        with closing(connection.execute(
            "SELECT 1 FROM operation_attempts WHERE run_id=? AND status=? LIMIT 1",
            (run["id"], int(_ATTEMPT_STATUS.RUNNING)),
        )) as cursor:
            if cursor.fetchone() is not None:
                raise TransactionError("原录像核实调用尚未保存实际结束，不能收场")
        metadata = {}
        with closing(connection.execute(
            "SELECT attempt_no,status,effect_state,result_json,error_json"
            " FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (run["id"],),
        )) as cursor:
            for number, status, effect, result_json, error_json in cursor:
                if result_json is None:
                    raise TransactionError("原录像核实尝试缺少已保存实际结果")
                actual = saved_outcome(status, effect, parse_exact_json(result_json),
                    None if error_json is None else parse_exact_json(error_json))
                if any(value.type == "result_files_listed" for value in actual.observations):
                    ticket = AttemptTicket(number, "result", str(run["activity_id"]),
                                           run["responsibility_key"], run["id"])
                    metadata.update((entry.identity, entry) for entry in files_from_outcome(ticket, actual))
        with closing(connection.execute(
            "SELECT id FROM device_files WHERE source_action_id=? ORDER BY id", (action["id"],),
        )) as cursor:
            files = [row_facts(connection, "device_files", row[0]) for row in cursor.fetchall()]
        identities = set()
        for file in files:
            identity = parse_exact_json(file["identity_key"])
            entry = metadata.get(identity[2]) if isinstance(identity, list) and len(identity) == 3 else None
            if (entry is None or identity[:2] != [action["device_id"], action["driver_id"]]
                    or not json_equal(entry.locator, file["locator_json"])
                    or file["completion_state"] != int(_FILE_COMPLETION.COMPLETE)
                    or file["ownership_evidence_json"] is None
                    or file["completion_evidence_json"] is None):
                raise TransactionError("录像核实收场缺少原可靠完整文件依据")
            identities.add(entry.identity)
        if (identities != set(metadata)
                or not any(entry.kind is FileKind.VIDEO for entry in metadata.values())
                or {file["id"] for file in files} != {
                    draft.file.device_file_id for draft in self._request.capture.drafts
                    if draft.file.device_file_id is not None}):
            raise TransactionError("录像核实收场的文件输入尚未满足全部登记要求")
        with closing(connection.execute(
            "SELECT id FROM recording_processing WHERE action_id=?", (action["id"],),
        )) as cursor:
            found = cursor.fetchone()
        processing = None if found is None else row_facts(connection, "recording_processing", found[0])
        if (processing is None or processing["check_decision"] == int(_CHECK_DECISION.UNDETERMINED)
                or (processing["check_decision"] == int(_CHECK_DECISION.REQUIRED)
                    and processing["check_state"] in (
                        int(_CHECK_STATE.NOT_PERFORMED), int(_CHECK_STATE.RUNNING)))
                or processing["repair_state"] in (int(_REPAIR_STATE.PENDING), int(_REPAIR_STATE.RUNNING))):
            raise TransactionError("录像核实收场前适用媒体处理必须已经结束")
        spec = action["execution_spec_json"]
        target = spec.get("target_duration_ms") if isinstance(spec, Mapping) else None
        basis = processing["check_basis_json"]
        if (isinstance(target, bool) or not isinstance(target, int) or target <= 0
                or not isinstance(basis, Mapping) or basis.get("target_duration_ms") != target):
            raise ConsistencyError("原录像检查依据与固定目标时长不一致")
        source_id = processing["source_device_file_id"]
        if processing["check_decision"] == int(_CHECK_DECISION.REQUIRED):
            source = next((file for file in files if file["id"] == source_id), None)
            if (source is None or source["role"] != int(_FILE_ROLE.ORIGINAL)
                    or metadata[parse_exact_json(source["identity_key"])[2]].kind is not FileKind.VIDEO):
                raise TransactionError("原录像媒体结果缺少同动作的完整原片来源")
        media = processing["media_json"]
        result = decide_recording_result(RecordingOutcomeFacts(
            processing_id=processing["id"],
            control_complete=(processing["check_decision"] == int(_CHECK_DECISION.NOT_NEEDED)
                and basis.get("reason") in (
                    CheckReason.CONTINUOUS_CONTROL_COMPLETE.value,
                    CheckReason.EXCESS_DURATION_CHECK.value)),
            check_decision=processing["check_decision"],
            check_state=processing["check_state"],
            check_duration_s=(saved_check_duration(media)
                if processing["check_state"] == int(_CHECK_STATE.COMPLETED) else None),
            check_issues=bool(media.get("issues")) if isinstance(media, Mapping) else False,
            input_unavailable=False,
            repair_state=processing["repair_state"], target_duration_ms=target))
        failure = self._request.capture.failure
        if (result.kind is RecordingResultKind.PENDING
                or (result.failure is None) != (failure is None)
                or (failure is not None and (
                    result.failure.code != failure.code
                    or not json_equal(result.failure.details, failure.details)))):
            raise TransactionError("录像收场申请与原已保存媒体和控制结果不一致")

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        action, run = self._identity(connection)
        if saved is not None:
            return self._reuse(scope, saved, run)
        if action["status"] in _ACTION_TERMINAL or (
                action["status"] == _ACTION_RUNNING and action["cancel_requested"]):
            # 原申请确实未提交；当前取消或终态结束其普通登记资格。
            # 已保存的尝试与原 run 独立保留，退役不产生业务终态。
            plan = row_facts(connection, "plans", action["plan_id"])
            if plan is None:
                raise ConsistencyError("原录像终态申请缺少所属计划")
            with closing(connection.execute(
                "SELECT id FROM outputs WHERE source_action_id=? ORDER BY id", (action["id"],),
            )) as cursor:
                output_ids = tuple(row[0] for row in cursor)
            return CommandPlan(events=(), owners={}, read_only=True,
                state_rows={"actions": {action["id"]: action},
                    "plans": {plan["id"]: plan}, "operation_runs": {run["id"]: run}},
                result=CaptureResult(action["status"], plan["status"], output_ids,
                    FinishDisposition.RETIRED))
        capture = FinishCaptureCommand(self._request.capture, self._key)
        self._ready(connection, action, run)
        sub = _CompositeScope(scope, scope.max_event_id + 1)
        plans = [capture.plan(sub)]
        if run["status"] in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)):
            plans.append(_FinishStaleRunsCommand(StaleRunFinish(
                (run["responsibility_key"],), RunOutcome.SUCCEEDED,
                self._request.capture.occurred_at), self._key).plan(sub))
        events = tuple(event for plan in plans for event in plan.events)
        allocation = scope.allocate(len(events))
        if (events[0].event_id != allocation.first_event_id
                or events[-1].event_id != allocation.last_event_id):
            raise TransactionError("录像核实共同收场的事件范围不连续")
        ranges = {}
        for plan in plans:
            for identity, values in plan.read_coverage.ranges.items():
                ranges.setdefault(identity, set()).update(values)
        return CommandPlan(
            events=events,
            owners={row: owner for plan in plans for row, owner in plan.owners.items()},
            state_rows=_merged_state_rows(*(plan.state_rows for plan in plans)),
            read_coverage=ReadCoverage(ranges), result=plans[0].result)

    def _reuse(self, scope, saved, run) -> CommandPlan:
        capture = self._request.capture
        flow_events = [event for event in saved if event["type"] == _RUN_END_EVENT]
        capture_events = saved[:-1] if flow_events else saved
        if (len(flow_events) > 1 or (flow_events and saved[-1] is not flow_events[0])
                or any(event["occurred_at"] != capture.occurred_at for event in saved)
                or any(event["type"] not in (
                    _ACTION_FINISHED_EVENT, _OUTPUT_REGISTERED_EVENT,
                    _INTERMEDIATE_FILE_EVENT, _PLAN_STATUS_EVENT,
                ) for event in capture_events)):
            raise TransactionError("原录像核实共同收场的完整事件段与申请不同")
        capture_plan = FinishCaptureCommand(capture, self._key)._reuse(scope.connection, capture_events)
        state = {"operation_runs": {run["id"]: run}}
        if flow_events:
            event = flow_events[0]
            rows = event["body"]["rows"]
            if (event["reason"] != 3 or len(rows) != 1
                    or rows[0]["table"] != "operation_runs" or rows[0]["id"] != run["id"]
                    or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]
                    or rows[0]["after"]["values"].get("status") != int(_RUN_STATUS.SUCCEEDED)
                    or rows[0]["after"]["values"].get("retry_wait_required") != 0
                    or rows[0]["after"]["values"].get("error_json") is not None
                    or run["status"] != int(_RUN_STATUS.SUCCEEDED)
                    or run["retry_wait_required"] != 0 or run["error_json"] is not None):
                raise TransactionError("原录像核实收场的责任或最终结果与申请不同")
        elif run["status"] in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)):
            raise TransactionError("原录像核实共同收场缺少流程结束事件")
        originals = {}
        origins = set()
        outputs = {}
        for event in capture_events:
            if event["type"] != _OUTPUT_REGISTERED_EVENT:
                continue
            for row in event["body"]["rows"]:
                values = row["after"]["values"]
                if row["table"] == "outputs":
                    outputs[(values["kind"], values["device_file_id"], values["intermediate_file_id"])] = row["id"]
                    if values["kind"] == _KIND_CODES[OutputKind.ORIGINAL]:
                        originals[values["device_file_id"]] = row["id"]
                elif row["table"] == "output_origins":
                    origins.add((values["output_id"], values["original_output_id"]))
        expected_origins = set()
        for draft in capture.drafts:
            if draft.kind is OutputKind.ORIGINAL:
                continue
            original = draft.original_output_id if draft.original_output_id is not None else originals[draft.original_batch_file_id]
            identity = (_KIND_CODES[draft.kind], draft.file.device_file_id, draft.file.intermediate_file_id)
            expected_origins.add((outputs[identity], original))
        if origins != expected_origins:
            raise TransactionError("原录像核实登记的全部原片关联与申请不同")
        return CommandPlan(
            events=(), owners=capture_plan.owners,
            state_rows=_merged_state_rows(capture_plan.state_rows, state),
            read_only=True, result=capture_plan.result)


class _ResultRunCloseCommand:
    """预算耗尽时仅收场结果核实流程的事务命令（不携带集合结论）。

    录像活动不适用结果集合核实，采集判定列保持为空；没有在途尝
    试可承载结论时，流程行按结果核实责任定位，与集合结论通道分
    别收场，流程错误按登记的公共错误结构保存。
    """

    def __init__(self, command: ResultRunClose, key: OperationKey) -> None:
        if not isinstance(command, ResultRunClose):
            raise TypeError("核实流程收场申请必须使用 ResultRunClose")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        command = self._command
        run_facts = _result_run_of_action(connection, command.action_id)
        if run_facts["status"] not in (1, 2):
            raise ConsistencyError("已结束的核实流程不能再次收场")
        self._state["operation_runs"] = {run_facts["id"]: run_facts}
        run_row = _update(
            "operation_runs",
            run_facts["id"],
            {
                "status": run_facts["status"],
                "retry_wait_required": run_facts["retry_wait_required"],
                "error_json": run_facts["error_json"],
            },
            {
                "status": int(_RUN_STATUS.UNCONFIRMED),
                "retry_wait_required": 0,
                "error_json": _unconfirmed_run_error(run_facts["activity_id"]),
            },
        )
        self._owners[("operation_runs", run_facts["id"])] = (
            "action", run_facts["action_id"])
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _RUN_END_EVENT, 3, (run_row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state)

    def _reuse(self, connection, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_RUN_END_EVENT, 3)]:
            raise TransactionError("操作身份已用于其他事务，不能作为核实收场重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("核实收场的事实时刻与原事务不同")
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "operation_runs"
                or not rows[0]["after"]["exists"]):
            raise TransactionError("原收场缺少流程事实")
        values = rows[0]["after"]["values"]
        run = _result_run_of_action(connection, command.action_id)
        if rows[0]["id"] != run["id"]:
            raise TransactionError("核实收场的原流程与所属活动不同")
        if (values.get("status") != int(_RUN_STATUS.UNCONFIRMED)
                or values.get("retry_wait_required") != 0
                or not json_equal(
                    values.get("error_json"),
                    _unconfirmed_run_error(run["activity_id"]))):
            raise TransactionError("核实收场的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True)


# -- 设备文件观察登记 -------------------------------------------------


def _observer_binding(action) -> tuple[str, str]:
    """观察者动作必须属于拍摄类型并携带固定设备与驱动绑定。"""
    if action is None or action.get("type") not in _CAPTURE_TYPES:
        raise ConsistencyError("设备文件观察者必须是拍摄动作")
    device, driver = action.get("device_id"), action.get("driver_id")
    if not isinstance(device, str) or not device or not isinstance(driver, str) or not driver:
        raise ConsistencyError("设备文件观察者缺少已保存的设备或驱动绑定")
    return device, driver


class _FileCreateCommand:
    """登记一次设备文件发现（DEVICE_FILE_OBSERVED.CREATE）。

    身份键由观察者动作的固定设备与驱动绑定及驱动文件身份确定编
    码构成；重复发现复用原行并核对定位，不创建第二份身份。
    """

    def __init__(self, command: FileObservationSave, key: OperationKey) -> None:
        if not isinstance(command, FileObservationSave):
            raise TypeError("文件发现申请必须使用 FileObservationSave")
        self._command = command
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)
        command = self._command
        action = row_facts(connection, "actions", command.observer_action_id)
        if action is None:
            raise ConsistencyError(f"观察动作不存在: {command.observer_action_id}")
        self._state["actions"] = {command.observer_action_id: action}
        device, driver = _observer_binding(action)
        identity_key = file_identity_key(device, driver, command.file_identity)
        with closing(connection.execute(
            "SELECT id, locator_json FROM device_files WHERE identity_key = ?",
            (identity_key,),
        )) as cursor:
            existing = cursor.fetchone()
        if existing is not None:
            file_id = int(existing[0])
            if not json_equal(parse_exact_json(existing[1]), dict(command.locator)):
                raise ConsistencyError(
                    f"重复发现与已登记定位矛盾，保留原依据: {identity_key}")
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ObservationOutcome(ObservationDisposition.ALREADY, file_id),
            )
        file_id = _next_id(connection, "device_files")
        row = _row("device_files", file_id, {
            "observer_action_id": command.observer_action_id,
            "source_action_id": None,
            "identity_key": identity_key,
            "locator_json": dict(command.locator),
            "ownership_evidence_json": None,
            "original_name": command.original_name,
            "media_type": command.media_type,
            "role": int(_FILE_ROLE.UNDETERMINED),
            "original_device_file_id": None,
            "pairing_evidence_json": None,
            "presence_state": int(_FILE_PRESENCE.UNKNOWN),
            "completion_state": int(_FILE_COMPLETION.UNKNOWN),
            "completion_evidence_json": None,
            "size_bytes": None,
            "checksum_support": int(_FILE_CHECKSUM.UNDETERMINED),
            "sha256": None,
            "last_error_json": None,
        })
        self._owners[("device_files", file_id)] = ("device_file", file_id)
        # 公开投影路由从文件行走到产物表；新发现尚无产物，装配空范围。
        self._state.setdefault("device_files", {})[file_id] = dict(row.after.values)
        self._state.setdefault("outputs", {})
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DEVICE_FILE_EVENT, _FILE_CREATE_REASON, (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(ObservationDisposition.SAVED, file_id, created=True))

    def _reuse(self, connection, saved) -> CommandPlan:
        """原键重送：核实原事务为同一发现的创建后恢复首次响应。"""
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DEVICE_FILE_EVENT, _FILE_CREATE_REASON)]:
            raise TransactionError("操作身份已用于其他文件事务，不能作为发现重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("文件发现的事实时刻与原事务不同")
        created = saved[0]["body"]["rows"][0]
        if created["table"] != "device_files" or created.get("before", {}).get("exists"):
            raise TransactionError("原事务不是该文件的首次发现登记")
        values = created["after"]["values"]
        action = row_facts(connection, "actions", command.observer_action_id)
        if action is None:
            raise ConsistencyError(f"观察动作不存在: {command.observer_action_id}")
        self._state["actions"] = {command.observer_action_id: action}
        device, driver = _observer_binding(action)
        expected = file_identity_key(device, driver, command.file_identity)
        if (values.get("observer_action_id") != command.observer_action_id
                or values.get("identity_key") != expected
                or not json_equal(values.get("locator_json"), dict(command.locator))
                or not json_equal(values.get("original_name"), command.original_name)
                or not json_equal(values.get("media_type"), command.media_type)):
            raise TransactionError("文件发现的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, int(created["id"]), created=True))


class _FileOwnershipCommand:
    """保存一次归属确认（DEVICE_FILE_OBSERVED.OWNERSHIP）。

    来源只能从未知一次确认；预览配对原片必须已确认归属且属于同一
    来源任务。
    """

    def __init__(self, command: OwnershipSave, key: OperationKey) -> None:
        if not isinstance(command, OwnershipSave):
            raise TypeError("归属确认申请必须使用 OwnershipSave")
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
        facts = self._load(connection, command.file_id)
        if (facts["source_action_id"] is not None
                or facts["ownership_evidence_json"] is not None):
            if (facts["source_action_id"] == command.source_action_id
                    and facts["ownership_evidence_json"] is not None
                    and facts["role"] == command.role
                    and facts["original_device_file_id"]
                    == command.paired_device_file_id):
                # 重复列举再次观察到同一归属事实：按已确认处理，
                # 保持已保存依据，不产生重复事件。
                return CommandPlan(
                    events=(), owners=self._owners, state_rows=self._state,
                    read_only=True,
                    result=ObservationOutcome(
                        ObservationDisposition.ALREADY, command.file_id))
            raise ConsistencyError("文件来源已确认，不能再次确认或改指")
        if facts["role"] != int(_FILE_ROLE.UNDETERMINED):
            raise ConsistencyError("已确认用途的文件不能再次确认归属")
        if command.role == int(_FILE_ROLE.PREVIEW):
            if command.paired_device_file_id == command.file_id:
                raise ConsistencyError("预览不能与自身配对")
            paired = self._load(connection, command.paired_device_file_id)
            if (paired["role"] != int(_FILE_ROLE.ORIGINAL)
                    or paired["source_action_id"] != command.source_action_id):
                raise ConsistencyError(
                    "配对原片必须已确认归属且属于同一来源任务")
        row = _update(
            "device_files", command.file_id,
            {
                "source_action_id": facts["source_action_id"],
                "ownership_evidence_json": facts["ownership_evidence_json"],
                "role": facts["role"],
                "original_device_file_id": facts["original_device_file_id"],
                "pairing_evidence_json": facts["pairing_evidence_json"],
            },
            {
                "source_action_id": command.source_action_id,
                "ownership_evidence_json": command.ownership_evidence(),
                "role": command.role,
                "original_device_file_id": command.paired_device_file_id,
                "pairing_evidence_json": command.pairing_evidence(),
            },
        )
        self._owners[("device_files", command.file_id)] = (
            "device_file", command.file_id)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DEVICE_FILE_EVENT, _FILE_OWNERSHIP_REASON, (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(
                ObservationDisposition.SAVED, command.file_id))

    def _load(self, connection, file_id: int) -> dict[str, Any]:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None:
            raise ConsistencyError(f"设备文件不存在: {file_id}")
        self._state.setdefault("device_files", {})[file_id] = facts
        _load_related_outputs(connection, self._state, file_id)
        return facts

    def _reuse(self, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DEVICE_FILE_EVENT, _FILE_OWNERSHIP_REASON)]:
            raise TransactionError("操作身份已用于其他文件事务，不能作为归属确认重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("归属确认的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "device_files" or row["id"] != command.file_id:
            raise TransactionError("原归属确认属于其他文件")
        after = row["after"]["values"]
        if (after.get("source_action_id") != command.source_action_id
                or after.get("role") != command.role
                or not json_equal(after.get("ownership_evidence_json"),
                                  command.ownership_evidence())
                or not json_equal(after.get("original_device_file_id"),
                                  command.paired_device_file_id)
                or not json_equal(after.get("pairing_evidence_json"),
                                  command.pairing_evidence())):
            raise TransactionError("归属确认的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, command.file_id))


class _FileCompleteCommand:
    """保存一次文件形成状态（DEVICE_FILE_OBSERVED.COMPLETE）。

    状态按登记的转换表推进，已完成不倒退；等待与产物契约依据引用
    已保存的等待完成事实；可靠观察可清除此前的失败证据。
    """

    def __init__(self, command: FileCompletionSave, key: OperationKey) -> None:
        if not isinstance(command, FileCompletionSave):
            raise TypeError("文件形成状态申请必须使用 FileCompletionSave")
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
        facts = self._load(connection, command.file_id)
        current = facts["completion_state"]
        if (command.state == current
                and command.state == int(_FILE_COMPLETION.COMPLETE)):
            # 重复列举再次观察到同一完成事实：长度一致按已确认处
            # 理，保持已保存依据；已完成文件的长度固定，不一致是
            # 设备观察矛盾。
            if command.size_bytes != facts["size_bytes"]:
                raise ConsistencyError(
                    "已完成文件的完整大小与既往确认不一致:"
                    f" {facts['size_bytes']!r} -> {command.size_bytes!r}")
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ObservationOutcome(
                    ObservationDisposition.ALREADY, command.file_id))
        allowed = _FILE_COMPLETION_NEXT.get(current, frozenset())
        if command.state not in allowed:
            raise ConsistencyError(
                f"文件形成状态不能从 {current!r} 推进到 {command.state!r}")
        if command.state == int(_FILE_COMPLETION.COMPLETE) and command.basis == int(
                _COMPLETION_BASIS.TIME_AND_OUTPUTS):
            activity = row_facts(connection, "device_activities", command.activity_id)
            if (activity is None or activity.get("wait_completed_event_id")
                    != command.wait_completed_event_id):
                raise ConsistencyError(
                    "等待与产物契约依据必须引用已保存的等待完成事实")
            self._state["device_activities"] = {
                command.activity_id: activity}
        before: dict[str, Any] = {
            "completion_state": current,
            "completion_evidence_json": facts["completion_evidence_json"],
            "size_bytes": facts["size_bytes"],
            "last_error_json": facts["last_error_json"],
        }
        after: dict[str, Any] = {
            "completion_state": command.state,
            "completion_evidence_json": command.completion_evidence(),
            "size_bytes": command.size_bytes,
            "last_error_json": None if command.error is None else dict(command.error),
        }
        for column, value in (
            ("locator_json", command.locator),
            ("original_name", command.original_name),
            ("media_type", command.media_type),
        ):
            if value is not None:
                before[column] = facts[column]
                after[column] = dict(value) if column == "locator_json" else value
        row = _update("device_files", command.file_id, before, after)
        self._owners[("device_files", command.file_id)] = (
            "device_file", command.file_id)
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _DEVICE_FILE_EVENT, _FILE_COMPLETE_REASON, (row,), command.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObservationOutcome(
                ObservationDisposition.SAVED, command.file_id))

    def _load(self, connection, file_id: int) -> dict[str, Any]:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None:
            raise ConsistencyError(f"设备文件不存在: {file_id}")
        self._state.setdefault("device_files", {})[file_id] = facts
        _load_related_outputs(connection, self._state, file_id)
        return facts

    def _reuse(self, saved) -> CommandPlan:
        command = self._command
        types = [(event["type"], event["reason"]) for event in saved]
        if types != [(_DEVICE_FILE_EVENT, _FILE_COMPLETE_REASON)]:
            raise TransactionError("操作身份已用于其他文件事务，不能作为形成状态重送")
        if saved[0]["occurred_at"] != command.occurred_at:
            raise TransactionError("形成状态的事实时刻与原事务不同")
        row = saved[0]["body"]["rows"][0]
        if row["table"] != "device_files" or row["id"] != command.file_id:
            raise TransactionError("原形成状态属于其他文件")
        after = row["after"]["values"]
        if (after.get("completion_state") != command.state
                or not json_equal(after.get("size_bytes"), command.size_bytes)
                or not json_equal(after.get("completion_evidence_json"),
                                  command.completion_evidence())
                or not json_equal(after.get("last_error_json"),
                                  None if command.error is None else dict(command.error))):
            raise TransactionError("形成状态的重送输入与原事务不同")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=ObservationOutcome(
                ObservationDisposition.ALREADY, command.file_id))


def _load_related_outputs(connection, state: dict, file_id: int) -> None:
    """装入引用该文件的正式产物行，供公开投影路由解析报告目标。"""
    outputs = state.setdefault("outputs", {})
    with closing(connection.execute(
        "SELECT id FROM outputs WHERE device_file_id = ?", (file_id,)
    )) as cursor:
        for (output_id,) in cursor.fetchall():
            facts = row_facts(connection, "outputs", output_id)
            if facts is not None:
                outputs[output_id] = facts


def _current_file_facts(context, row) -> Mapping[str, Any]:
    facts = context.state_rows.get("device_files", {}).get(row.row_id)
    if facts is None:
        raise EventValidationError(f"设备文件事件缺少当前事实: device_files#{row.row_id}")
    return facts


def _device_file_guard(event, context) -> None:
    """DEVICE_FILE_OBSERVED 五分支：身份、来源、配对与完成事实核对。

    身份键编码观察者的固定绑定；来源只能从未知一次确认，预览配对
    两端属于同一来源任务；形成状态按登记转换推进且已完成不倒退；
    摘要与支持声明、存在性变化各自满足登记约束。
    """
    if event.event_type != _DEVICE_FILE_EVENT:
        return
    for row in event.rows:
        if row.table != "device_files":
            continue
        _device_file_row_guard(event.reason, row, context)


def _device_file_row_guard(reason: int, row, context) -> None:
    if reason == _FILE_CREATE_REASON:
        if row.before.exists:
            raise EventValidationError("文件发现必须是创建行")
        values = row.after.values
        action = context.state_rows.get("actions", {}).get(
            values.get("observer_action_id"))
        if action is None:
            raise EventValidationError("文件发现缺少观察者动作事实")
        device, driver = _capture_binding(action)
        key = values.get("identity_key")
        try:
            decoded = parse_exact_json(key)
        except ValueError as error:
            raise EventValidationError(f"文件身份键不是有效精确 JSON: {key!r}") from error
        if (not isinstance(decoded, list) or len(decoded) != 3
                or decoded[0] != device or decoded[1] != driver
                or not isinstance(decoded[2], str) or not decoded[2]):
            raise EventValidationError("文件身份键与观察者的设备或驱动绑定不一致")
        if not isinstance(values.get("locator_json"), dict):
            raise EventValidationError("文件定位结构必须是对象")
        for column in ("role", "presence_state", "completion_state", "checksum_support"):
            if values.get(column) != 1:
                raise EventValidationError(f"新发现文件的 {column} 必须是未知初始值")
        for column in ("source_action_id", "ownership_evidence_json",
                       "original_device_file_id", "pairing_evidence_json",
                       "completion_evidence_json", "size_bytes", "sha256",
                       "last_error_json"):
            if values.get(column) is not None:
                raise EventValidationError(f"新发现文件不能携带 {column}")
        return

    if not row.before.exists:
        raise EventValidationError("文件归属、形成状态、摘要及存在性必须是更新行")
    if reason == _FILE_OWNERSHIP_REASON:
        before, after = row.before.values, row.after.values
        if (before.get("source_action_id") is not None
                or before.get("ownership_evidence_json") is not None):
            raise EventValidationError("文件来源只能从未知一次确认")
        if before.get("role") != int(_FILE_ROLE.UNDETERMINED):
            raise EventValidationError("已确认用途的文件不能再次确认归属")
        source = after.get("source_action_id")
        if not is_json_integer(source) or source < 1:
            raise EventValidationError(f"确认来源必须是合法动作身份: {source!r}")
        role = after.get("role")
        if role not in (int(_FILE_ROLE.ORIGINAL), int(_FILE_ROLE.PREVIEW)):
            raise EventValidationError(f"归属确认的用途必须是原片或预览: {role!r}")
        evidence = after.get("ownership_evidence_json")
        if not isinstance(evidence, dict) or evidence.get("method") not in (
                int(member) for member in _OWNERSHIP_METHOD):
            raise EventValidationError("归属证据缺少登记的方法编号")
        if not isinstance(evidence.get("observation"), dict):
            raise EventValidationError("归属证据缺少驱动观察依据")
        if (evidence.get("method") == int(_OWNERSHIP_METHOD.BASELINE_DIFFERENCE)
                and not is_json_integer(evidence.get("activity_id"))):
            raise EventValidationError("固定基准差集证据必须填写活动身份")
        paired = after.get("original_device_file_id")
        pairing = after.get("pairing_evidence_json")
        if role == int(_FILE_ROLE.PREVIEW):
            if not is_json_integer(paired) or paired < 1 or paired == row.row_id:
                raise EventValidationError(f"预览配对必须是其他文件身份: {paired!r}")
            if (not isinstance(pairing, dict)
                    or pairing.get("method") != int(_PAIRING_METHOD.DRIVER_PAIRING)
                    or not isinstance(pairing.get("observation"), dict)):
                raise EventValidationError("预览配对缺少驱动配对证据")
            original = context.state_rows.get("device_files", {}).get(paired)
            if original is None:
                raise EventValidationError("预览配对缺少原片当前事实")
            if (original.get("role") != int(_FILE_ROLE.ORIGINAL)
                    or original.get("source_action_id") != source):
                raise EventValidationError("配对两端必须属于同一来源任务且原片用途为原片")
        elif paired is not None or pairing is not None:
            raise EventValidationError("原片不携带配对")
        return

    facts = _current_file_facts(context, row)
    after = row.after.values
    if reason == _FILE_COMPLETE_REASON:
        current = facts.get("completion_state")
        state = after.get("completion_state")
        if state not in _FILE_COMPLETION_NEXT.get(current, frozenset()):
            raise EventValidationError(
                f"文件形成状态不能从 {current!r} 推进到 {state!r}")
        if state == int(_FILE_COMPLETION.COMPLETE):
            size = after.get("size_bytes")
            if not is_json_integer(size) or size < 0:
                raise EventValidationError(f"完成状态必须携带非负完整大小: {size!r}")
            evidence = after.get("completion_evidence_json")
            if (not isinstance(evidence, dict)
                    or evidence.get("basis") not in (
                        int(member) for member in _COMPLETION_BASIS)):
                raise EventValidationError("完成状态缺少登记依据")
            if not isinstance(evidence.get("observation"), dict):
                raise EventValidationError("完成状态缺少驱动观察依据")
            if (evidence.get("basis") == int(_COMPLETION_BASIS.TIME_AND_OUTPUTS)
                    and (not is_json_integer(evidence.get("activity_id"))
                         or not is_json_integer(evidence.get("wait_completed_event_id")))):
                raise EventValidationError("等待与产物契约依据必须引用任务及等待完成事件")
        elif after.get("size_bytes") is not None:
            raise EventValidationError("只有完成状态携带完整大小")
        if state == int(_FILE_COMPLETION.UNCONFIRMED):
            if not isinstance(after.get("last_error_json"), dict):
                raise EventValidationError("未确认状态必须携带实际失败证据")
        return

    if reason == _FILE_CHECKSUM_REASON:
        support = after.get("checksum_support")
        if (facts.get("checksum_support") != int(_FILE_CHECKSUM.UNDETERMINED)
                or support not in (int(_FILE_CHECKSUM.SUPPORTED),
                                   int(_FILE_CHECKSUM.UNSUPPORTED))):
            raise EventValidationError("摘要能力只能从未判定一次决定")
        sha256 = after.get("sha256")
        if sha256 is not None:
            if (support != int(_FILE_CHECKSUM.SUPPORTED)
                    or facts.get("completion_state") != int(_FILE_COMPLETION.COMPLETE)):
                raise EventValidationError("摘要只能在已完成的受支持文件上保存")
            if (not isinstance(sha256, str) or len(sha256) != 64
                    or any(char not in "0123456789abcdef" for char in sha256)):
                raise EventValidationError(f"摘要必须是 64 位小写十六进制: {sha256!r}")
        return

    if reason == _FILE_PRESENCE_REASON:
        state = after.get("presence_state")
        if state not in (int(member) for member in _FILE_PRESENCE) or state == facts.get(
                "presence_state"):
            raise EventValidationError("存在性观察必须是实际状态变化")
        return
    raise EventValidationError(f"未登记的设备文件观察分支: {reason!r}")



# -- 应急停止最终补记 -------------------------------------------------


@dataclass(frozen=True)
class EmergencySave:
    """应急补记的保存结果：持久化状态与原流程事实。"""

    record_status: RecordStatus
    run_id: int
    attempts_saved: int


class SaveEmergencyCommand:
    """一次应急停止最终补记的完整事务命令。

    一个目标的最终流程（kind=9）与全部实际尝试、适用设备活动变
    化在一个事务共同创建；进行中补记、普通意图引用、超限与缺项
    均拒绝。
    """

    def __init__(
        self,
        *,
        session_key: str,
        action_id: int,
        activity_id: int,
        record: EmergencyRecord,
        attempts: tuple[dict, ...],
        occurred_at: int,
        key: OperationKey,
        timeout_s=None,
        retry_interval_s=None,
    ) -> None:
        self._session_key = session_key
        self._timeout_s = timeout_s
        self._retry_interval_s = retry_interval_s
        self._action_id = action_id
        self._activity_id = activity_id
        self._record = record
        self._attempts = attempts
        self._occurred_at = occurred_at
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        if saved_transaction_events(connection, self._key) is not None:
            raise TransactionError("应急补记的重送须按原事务核实")
        action = row_facts(connection, "actions", self._action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {self._action_id}")
        activity = row_facts(connection, "device_activities", self._activity_id)
        if activity is None:
            raise TransactionError(f"设备活动不存在: {self._activity_id}")
        self._state["device_activities"] = {self._activity_id: activity}
        self._state["actions"] = {self._action_id: action}
        self._state.setdefault("operation_runs", {})
        self._state.setdefault("operation_attempts", {})

        record = self._record
        if len(self._attempts) != record.attempts_used:
            raise TransactionError(
                f"补记尝试行数与实际次数不符: {len(self._attempts)}"
                f" != {record.attempts_used}"
            )
        if record.attempts_used > record.max_attempts:
            raise TransactionError("实际次数超过本会话固定限额")
        if record.attempts_used > 0 and not self._complete_config():
            raise TransactionError("已有尝试的补记要求完整配置")
        stop_observation = record.stop_observation
        if record.outcome is EmergencyOutcome.STOPPED:
            if record.attempts_used == 0 and stop_observation is None:
                raise TransactionError("零尝试停止的补记必须携带可靠停止依据")
        elif stop_observation is not None:
            raise TransactionError("停止依据只伴随停止成功的补记")

        responsibility_key = (
            f"emergency/{self._session_key}/{self._activity_id}"
        )
        existed = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (responsibility_key,),
        ).fetchone()
        if existed is not None:
            raise TransactionError("同一会话对同一活动只有一条应急流程")

        if record.outcome is EmergencyOutcome.STOPPED:
            run_status = 3
            run_error = None
            activity_after = 3
            activity_error = None
        elif record.outcome is EmergencyOutcome.UNCONFIRMED:
            if record.attempts_used == 0:
                raise TransactionError("停止未确认且有尝试的补记要求次数大于 0")
            run_status = 6
            run_error = {"code": "emergency_stop_unconfirmed", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error
        else:
            if record.attempts_used != 0:
                raise TransactionError("未能尝试的补记不创建尝试行")
            run_status = 4
            run_error = {"code": "emergency_not_attempted", "stage": "emergency"}
            activity_after = activity["activity_state"]
            activity_error = run_error

        allocation = scope.allocate(1)
        first_id = allocation.first_event_id
        run_id = _next_id(connection, "operation_runs")
        owner = ("action", action["id"])
        self._owners[("operation_runs", run_id)] = owner

        run_values = {
            "action_id": self._action_id,
            "delivery_id": None,
            "kind": 9,
            "query_purpose": None,
            "responsibility_key": responsibility_key,
            "activity_id": self._activity_id,
            "copy_id": None,
            "cleanup_item_id": None,
            "session_key": self._session_key,
            "status": run_status,
            "attempts_used": record.attempts_used,
            "max_attempts_used": record.max_attempts,
            "timeout_s_json": self._timeout_s,
            "retry_interval_s_json": self._retry_interval_s,
            "retry_wait_required": 0,
            "error_json": run_error,
        }
        rows = [_row("operation_runs", run_id, run_values)]
        attempt_ids: list[int] = []
        next_attempt_id = _next_id(connection, "operation_attempts")
        for index, attempt in enumerate(self._attempts):
            attempt_id = next_attempt_id + index
            attempt_ids.append(attempt_id)
            self._owners[("operation_attempts", attempt_id)] = owner
            values = dict(attempt)
            values.update(
                {
                    "run_id": run_id,
                    "attempt_no": index + 1,
                    "copy_round": None,
                    "intent_event_id": None,
                    "result_event_id": first_id,
                    "max_attempts_used": record.max_attempts,
                    "timeout_s_json": self._timeout_s,
                    "retry_interval_s_json": self._retry_interval_s,
                }
            )
            rows.append(_row("operation_attempts", attempt_id, values))
        before = {"activity_state": activity["activity_state"],
                  "last_error_json": activity["last_error_json"]}
        after = {"activity_state": activity_after, "last_error_json": activity_error}
        if not json_equal(before, after):
            self._owners[("device_activities", self._activity_id)] = owner
            rows.append(_update("device_activities", self._activity_id, before, after))
        event = _envelope(
            first_id,
            allocation.txn_id,
            _EMERGENCY_RECORDED_EVENT,
            1,
            tuple(rows),
            self._occurred_at,
            evidence=({"observation": dict(stop_observation)}
                      if stop_observation is not None else None),
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=EmergencySave(
                record_status=RecordStatus.RECORDED,
                run_id=run_id,
                attempts_saved=record.attempts_used,
            ),
        )

    def _complete_config(self) -> bool:
        return self._timeout_s is not None and self._retry_interval_s is not None


def _emergency_guard(event, context) -> None:
    """应急补记的组合守卫：责任键、尝试归属、次数与结果组合。

    会话身份由本事件创建的最终流程行承载；停止成功的补记必须具
    有可靠停止证据——零尝试或依据来自尝试结果之外时经事件依据成
    员保存，其余保存在实际尝试结果的观察中；两种依据都须指向目
    标活动。停止依据不伴随其他结果分类。
    """
    run_values = None
    run_id = None
    attempts: list[dict] = []
    for row in event.rows:
        if row.table == "operation_runs" and not row.before.exists:
            if run_values is not None:
                raise EventValidationError("应急补记只共同创建一个最终流程")
            run_values = row.after.values
            run_id = row.row_id
        elif row.table == "operation_attempts" and not row.before.exists:
            attempts.append(row.after.values)
    if run_values is None:
        raise EventValidationError("应急补记缺少最终流程行")
    session_key = run_values.get("session_key")
    if not isinstance(session_key, str) or len(session_key) != 32:
        raise EventValidationError("应急流程行必须携带本会话身份")
    expected_key = (
        f"emergency/{session_key}/{run_values.get('activity_id')}"
    )
    if run_values.get("responsibility_key") != expected_key:
        raise EventValidationError("应急责任键与会话及活动不符")
    if run_values.get("kind") != 9 or run_values.get("retry_wait_required") != 0:
        raise EventValidationError("应急流程必须是补记终态")
    status = run_values.get("status")
    if status not in (3, 4, 6):
        raise EventValidationError("应急补记不保存进行中状态")
    if status == 3 and run_values.get("error_json") is not None:
        raise EventValidationError("应急成功不携带流程错误")
    if status in (4, 6) and run_values.get("error_json") is None:
        raise EventValidationError("应急失败或未确认必须携带原因")
    if status == 6 and not attempts:
        raise EventValidationError("停止未确认的补记必须有实际尝试")
    if status == 4 and attempts:
        raise EventValidationError("未能尝试的补记不创建尝试行")
    observation = event.evidence.get("observation")
    if observation is not None and status != 3:
        raise EventValidationError("停止依据只伴随停止成功的补记")
    if observation is not None or (status == 3 and not attempts):
        _verify_stop_observation(observation, run_values)
    if status == 3 and attempts and observation is None:
        if not any(
            _result_confirms_stop(attempt, run_values.get("activity_id"))
            for attempt in attempts
        ):
            raise EventValidationError(
                "停止成功的补记必须在尝试结果中保存指向目标活动的停止观察"
            )
    numbers = [a.get("attempt_no") for a in attempts]
    if numbers != list(range(1, len(attempts) + 1)):
        raise EventValidationError("应急尝试必须从 1 连续编号")
    if run_values.get("attempts_used") != len(attempts):
        raise EventValidationError("累计次数与尝试行数不符")
    maximum = run_values.get("max_attempts_used")
    if maximum is not None and len(attempts) > maximum:
        raise EventValidationError("尝试次数超过本会话固定限额")
    for attempt in attempts:
        if attempt.get("run_id") != run_id:
            raise EventValidationError("应急尝试必须归属本次流程")
        if attempt.get("intent_event_id") is not None:
            raise EventValidationError("应急尝试不携带普通意图引用")
        if attempt.get("result_event_id") != event.event_id:
            raise EventValidationError("应急尝试结果必须指向本事件")
        if attempt.get("status") == 1:
            raise EventValidationError("应急补记不保存运行中尝试")
        if attempt.get("copy_round") is not None:
            raise EventValidationError("应急尝试没有拷贝轮次")


def _structured_observation(observation) -> bool:
    """观察的结构边界：非空类型、正整数版本与结构化数据。

    证据类型与 data 成员是否符合驱动契约由装配层按证据登记核对；
    本结构边界只排除不能解释为观察的形状。
    """
    return (
        isinstance(observation, Mapping)
        and isinstance(observation.get("type"), str)
        and bool(observation.get("type"))
        and is_json_integer(observation.get("version"))
        and observation["version"] > 0
        and isinstance(observation.get("data"), Mapping)
    )


def _result_confirms_stop(attempt_values, activity_id) -> bool:
    """尝试结果是否携带指向目标活动的可靠停止观察。"""
    result = attempt_values.get("result_json")
    if not isinstance(result, Mapping):
        return False
    observations = result.get("observations")
    if not isinstance(observations, list):
        return False
    return any(
        _structured_observation(item)
        and item["data"].get("activity_id") == str(activity_id)
        for item in observations
    )


def _verify_stop_observation(observation, run_values: Mapping[str, Any]) -> None:
    """核对事件依据成员中的停止依据的结构与目标指向。"""
    if not _structured_observation(observation):
        raise EventValidationError("停止的补记必须携带结构化停止依据")
    if observation["data"].get("activity_id") != str(run_values.get("activity_id")):
        raise EventValidationError("停止依据必须指向本补记的目标活动")


def _activity_guard(event, context) -> None:
    """活动状态守卫：结束不由路径、超时或本地退出补造。

    采集判定列只适用于照片与延时：录像活动不得携带采集结果或判
    定依据；确定判定只能从待定初始化一次并同事务携带依据。
    """
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        before_state = row.before.values.get("activity_state")
        after_state = row.after.values.get("activity_state")
        if after_state == 3 and before_state != 3:
            # 活动结束必须由本事务的可靠停止事实承载；同事件创建的
            # 最终流程行即为该事实。
            stopped = any(
                row.table == "operation_runs"
                and not row.before.exists
                and row.after.values.get("status") == 3
                for row in event.rows
            )
            runs = context.state_rows.get("operation_runs", {})
            stopped = stopped or any(
                values.get("status") == 3 for values in runs.values()
            )
            if not stopped:
                raise EventValidationError("活动结束缺少可靠停止事实")
        basis_before = row.before.values.get("completion_basis")
        basis_after = row.after.values.get("completion_basis")
        capture_columns = (
            "capture_json", "completion_basis", "completion_evidence_json")
        if any(row.after.values.get(column) is not None
               for column in capture_columns):
            action_id = row.before.values.get(
                "action_id", context.state_rows.get(
                    "device_activities", {}).get(row.row_id, {}).get("action_id"))
            action = context.state_rows.get("actions", {}).get(action_id)
            if action is None:
                raise EventValidationError(
                    f"采集判定列要求动作事实: actions#{action_id}")
            if action.get("type") == 2:
                raise EventValidationError("录像活动不得携带采集结果或判定依据")
        if basis_after != basis_before and basis_after in (2, 3, 4):
            if basis_before not in (None, 1):
                raise EventValidationError("采集判定已确定，不能再改判")
            if not row.after.values.get("completion_evidence_json"):
                raise EventValidationError("确定的采集判定缺少可靠依据")


def _release_guard(event, context) -> None:
    """占用释放守卫：统一释放判定与输出范围限制。

    ENDED 仍可保持 HELD；保存 RELEASED 要求活动已结束、可靠未派
    发或无效果拒绝、适用的等待与产物完成依据三者居一，且基准比
    较的输出范围归属已经固定。
    """
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        if not (
            row.after.values.get("occupancy_state") == 2
            and row.before.values.get("occupancy_state") == 1
        ):
            continue
        facts = dict(
            context.state_rows.get("device_activities", {})
            .get(row.row_id, {})
        )
        facts.update(row.after.values)
        if not release_basis_holds(facts):
            raise EventValidationError(
                "占用释放缺少活动结束、未派发或完成依据")
        if facts.get("ownership_mode") == 2 and facts.get("baseline_state") != 3:
            raise EventValidationError("输出范围归属未固定不得释放占用")


def _result_check_guard(event, context) -> None:
    """结果集合核实守卫：分支、判定编码与轮次状态一致。

    一轮只消耗一次：开始分支只推进状态，结论分支必须携带规则标
    识与结构化依据，判定编码与分支对应；取消或耗尽不补造集合。
    """
    if event.event_type != _RESULT_SET_EVENT:
        return
    rows = [
        row for row in event.rows
        if row.table == "device_activities" and row.before.exists
    ]
    if len(rows) != 1:
        raise EventValidationError("结果集合核实恰好修改一个设备活动")
    row = rows[0]
    state_after = row.after.values.get("result_set_state")
    state_before = row.before.values.get("result_set_state")
    if event.reason == _RESULT_BEGIN_REASON:
        if (state_before, state_after) != (1, 2):
            raise EventValidationError("开始分支只把未核实集合推进到核实中")
        extra = set(row.after.values) - {"result_set_state"}
        if extra:
            raise EventValidationError(f"开始分支不携带结论事实: {sorted(extra)}")
        return
    outcome_by_reason = {
        _RESULT_COMPLETE_REASON: (3, 1),
        _RESULT_UNSATISFIED_REASON: (3, 2),
        _RESULT_UNCONFIRMED_REASON: (4, 3),
    }
    expected = outcome_by_reason.get(event.reason)
    if expected is None or state_before not in (1, 2) or state_after != expected[0]:
        raise EventValidationError("结论分支与核实状态转换不符")
    check = row.after.values.get("result_check_json")
    if not isinstance(check, Mapping):
        raise EventValidationError("结论分支必须保存结果集合核实依据")
    if (not isinstance(check.get("contract"), str) or not check["contract"]
            or check.get("outcome") != expected[1]
            or not isinstance(check.get("observation"), Mapping)):
        raise EventValidationError("结果集合核实依据的结构或判定编码非法")
