"""拍摄能力路由与处理器。

处理器是调度侧（Q5）登记的动作推进入口：按能力阶段推进一次执
行——授予启动机会、发起设备契约调用、把结果列举观察登记为设备
文件（归属按任务独立范围确认、完成按设备保证保存）、按 C6 核实
产物集合，可判定时保存终态与正式产物。延时核实按 CHECK_CAPTURE_
RESULTS 责任编排名额轮次：结论与尝试结束同事务提交，暂不齐备或
列举失败建立重试等待后作为新轮次，预算耗尽按无法确认收场。驱
动与结果列举由端口提供（契约替身与真实驱动同形）；录像中段事
实经能力状态端口装载，媒体链按媒体端口推进；时钟异常会话的保
守收场经 advance_winddown 接入，达到等待窗口后停止并保存等待
阶段，不启动媒体链。
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from enum import Enum
import json
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Protocol

if TYPE_CHECKING:
    from camctl.capture.software_recovery import SoftwareRecoveryDiagnostic

from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.diagnostic_controls import is_capture_control_error
from camctl.logging_runtime.diagnostics import DiagnosticState, record_call_outcome, record_error
from camctl.logging_runtime.events import measure_stage, record_event
from camctl.devices.start_entry import StartEntryCoordinator, EntryPhase, is_hasselblad_photo
from camctl.capture.finite_cancel import finite_cancel_policy
from camctl.capture.diagnostics import preparation_failure_diagnostic
from camctl.persistence.repositories.finite_cancel import (
    FiniteCancelEndSave, read_finite_cancel_assessment, save_finite_cancel_end,
)

from camctl.capture.models import (
    ActivityConcludeSave,
    ActivityObservationSave,
    ActivityReleaseSave,
    HostTimerStopSave,
    ResultRunClose,
    ResultSetPhase,
    ResultSetSave,
    WaitCompletedSave,
)
from camctl.capture.files import (
    FileChecksumSave,
    FileCompletionSave,
    FileObservationSave,
    FilePresenceSave,
    OwnershipSave,
    ObservationOutcome,
)
from camctl.capture.media import (
    RecordingFailure,
    RecordingOutcomeFacts,
    decide_recording_result,
)
from camctl.capture.media_flow import MediaFlow, run_recording_media
from camctl.capture.media_facts import saved_osv_input_issues
from camctl.capture.result_inputs import (
    ListedResult, ObservedFile, ResultPage, files_from_outcome, saved_outcome,
)
from camctl.capture.result_scans import (
    registered_observations, iter_result_entries, result_entry_count, assess_registered_files, max_result_value, any_result,
    PendingResultScan, SavedResultEntries, QualifiedResultEntries, SourceResultEntries,
    ObservedResultEntries, TerminalResultEntries, RegisteredSourceFiles, advance_result_scan,
    completed_wait_from_page, resume_result_page_saves,
)
from camctl.capture.result_pages import ResultPageRef, OutputSetFinalizationSave, page_from_outcome
from camctl.capture.photo import (
    CaptureAssessment,
    PhotoCompletion,
    PhotoDecision,
    PhotoState,
    decide_photo,
)
from camctl.capture.processing import (
    CheckBasis,
    CheckDecisionChoice,
    CheckDecisionSave,
    CheckReason,
    RepairBasis,
    RepairDecisionChoice,
    RepairDecisionSave,
    RepairReason,
    SourceFileSave,
    saved_check_duration,
)
from camctl.capture.recording import (
    CaptureContext,
    GrantDecision,
    ReconciliationFacts,
    ReconciliationPhase,
    RecordingFacts,
    RecordingPhase,
    RecordingState,
    StartDispatch,
    RecoveredControlDecision,
    RecoveredControlFacts,
    RecoveredControlReason,
    WinddownFacts,
    WinddownPhase,
    decide_conservative_winddown,
    decide_recording_next,
    decide_recording_reconciliation,
    decide_recovered_control,
    recording_stop_target,
    start_recording,
)
from camctl.capture.recovery import (
    RecoveryBlockedReason, RecoveryBoundary, RecoveryDiagnostic, recovery_block_reason,
)
from camctl.capture.results import (
    CaptureFile,
    CaptureFileSet,
    FileKind,
    ProductRequirements,
    ProductRule,
    assess_capture_files,
    is_optional_preview,
)
from camctl.capture.timelapse import (
    CaptureWaitConfig,
    EndControl,
    StartReturn,
    WaitKind,
    WaitPlan,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.bindings import BindingResult, DeviceBinding, binding_failure_details
from camctl.devices.capture_timing import CaptureTiming, WaitClockReference
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES
from camctl.devices.tasks import CompletionMode, VideoResultMode
from camctl.devices.capture_completion import is_stop_context
from camctl.persistence.repositories.result_sources import ResultSourceKind
from camctl.devices.directory import DirectoryReader
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.devices.evidence import EvidenceError
from camctl.devices.ports import CapturePreparationDriver, ControlRequest, DeviceCallResult
from camctl.devices.recording_completion import is_completed_file_wait
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    QueryPurpose,
    RetryWaitGate,
    RunFinish,
    RunOutcome,
    StaleRunFinish,
)
from camctl.operations.tool_diagnostics import (
    DiagnosticsValidationError, ResultEncodingError,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import OutcomeValidationError, validate_outcome
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.persistence.models import db_failure, db_outcome_for, DatabaseAccessError, DbOutcomeKind
from camctl.persistence.business import BusinessDatabase, SharedSave, settle_required
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishBindingFailure,
    FinishCanceledCapture,
    FinishCapture,
    FinishDisposition,
    FinishRecordingResults,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import (
    ExpireActionRequest, ExpireOutcome, GrantRequest, SchedulingRepository,
)
from camctl.persistence.repositories.timelapse import ScheduleWait, TimelapseRepository
from camctl.persistence.transaction import row_facts
from camctl.scheduling.rules import LaunchWindow
from camctl.scheduling.service import ActionHandler

__all__ = [
    "CaptureRuntime",
    "HandlerOutcome",
    "ObservedFile",
    "advance_winddown",
    "capture_handler",
    "route_completion",
]

#: 各能力要求的必需产物类别（第一版按动作类型固定）。
_REQUIRED_KINDS = {
    "camera_record": FileKind.VIDEO,
    "camera_take_photo": FileKind.PHOTO,
    "camera_timelapse": FileKind.VIDEO,
}

#: 归属证据方法与完成依据（file-fields.md#设备文件）。
_TASK_SCOPE = 1
_DEVICE_GUARANTEE = 1

#: 第一版任务范围列举的结果规则标识与采集判定方法。
_RESULT_CONTRACT = "task_scope_files"
_TIME_AND_OUTPUTS_METHOD = "time_and_outputs"
_DEVICE_EVIDENCE_METHOD = "device_evidence"
_KNOWN_FAILURE_METHOD = "known_failure"

_ACTION_TERMINAL = (3, 4, 5, 6)

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_ACTION_TYPE = enum_for("actions.type")
_RUN_STATUS = enum_for("operation_runs.status")
_ACTIVITY_STATE = enum_for("device_activities.activity_state")
_RESULT_SET_STATE = enum_for("device_activities.result_set_state")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")
_DISPATCH_STATE = enum_for("device_activities.dispatch_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_FILE_PRESENCE = enum_for("device_files.presence_state")
_FILE_CHECKSUM = enum_for("device_files.checksum_support")
_FILE_ROLE = enum_for("device_files.role")
_ORIGINAL_ROLE = int(_FILE_ROLE.ORIGINAL)
_PREVIEW_ROLE = int(_FILE_ROLE.PREVIEW)
_RUN_KIND = enum_for("operation_runs.kind")
_RECOVERABLE_OPERATIONS = {
    int(_RUN_KIND.START): "control", int(_RUN_KIND.STOP): "stop",
    int(_RUN_KIND.QUERY_ACTIVITY): "query", int(_RUN_KIND.CHECK_CAPTURE_RESULTS): "result",
    int(_RUN_KIND.STOP_RESIDUAL): "stop",
}
_QUERY_PURPOSE = enum_for("operation_runs.query_purpose")


@dataclass(frozen=True)
class HandlerOutcome:
    """一次处理器推进的结果分类。"""

    phase: str
    detail: str | None = None
    wakeup_required: bool = True


@dataclass(frozen=True)
class RecordingStartPreparation:
    """真实启动返回与本次上限；不持有数据库读取或设备回调。"""

    dispatch: StartDispatch
    max_attempts: int


@dataclass(frozen=True)
class StartConfirmationPreparation:
    """启动核实的原固定活动及本次上限。"""

    activity: Mapping[str, Any]
    max_attempts: int


@dataclass(frozen=True)
class UnvalidatedCallOutcome:
    """只持有原对象；不表示任何设备事实已经通过契约校验。"""

    outcome: CallOutcome


@dataclass(frozen=True)
class UnvalidatedAttemptFinish:
    """校验前冻结的原申请，不得传入仓储。"""

    ticket: AttemptTicket
    outcome: UnvalidatedCallOutcome
    occurred_at: int
    retry_wait: bool = False
    run_finish: RunFinish | None = None


@dataclass(frozen=True)
class PendingCallResult:
    """原执行者持有的调用结果；保存核实不改变身份、事实或时钟。"""

    key: OperationKey
    finish: AttemptFinish | UnvalidatedAttemptFinish
    observation: ActivityObservationSave | None
    start_finish: StaleRunFinish | None = None
    action_finish: FinishCapture | FinishCanceledCapture | None = None
    expiration: ExpireActionRequest | None = None
    confirmation_anchor_ns: int | None = None
    returned_ns: int = 0
    action_failure: RecordingFailure | None = None
    canceled_unstarted: bool = False
    preparation: RecordingStartPreparation | StartConfirmationPreparation | None = None
    result_listing: tuple[ObservedFile, ...] | SavedResultEntries | None = None
    result_disposition_ready: bool = True
    result_set: ResultSetSave | None = None
    completion_page: ResultPageRef | None = None
    result_registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None
    host_timer_stop: HostTimerStopSave | None = None
    finalized_page: ResultPageRef | None = None
    capture_stop: bool = False
    residual_conclude: int | None = None
    query_conclude: int | None = None
    query_conclude_stop_keys: tuple[str, ...] = ()
    diagnostics_check: Callable[[CallOutcome], None] | None = None
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)

    @property
    def validated(self) -> bool:
        return isinstance(self.finish, AttemptFinish)

    def retry_if_owned(self, pending_results, identity):
        current = pending_results.get(identity)
        if current is not None and current.shared is self.shared:
            self.shared.retry_failed()
            return current
        return self

    async def save(self, pending_results, identity, save_and_apply, *, retry=False):
        pending = self.retry_if_owned(pending_results, identity) if retry else self
        return await pending.shared.run(lambda: save_and_apply(pending))


# 启动装配和既有调用方使用同一个公共责任集合。
PendingStartResult = PendingCallResult


@dataclass(frozen=True)
class PendingCaptureInputReads:
    """拍摄终态可靠保存后，原输入读取流程的完整收尾申请。"""

    key: OperationKey
    request: StaleRunFinish


@dataclass(frozen=True)
class StartCloseRequest:
    """没有新调用结果时，原 START 与动作共同收场的完整申请。"""

    finish: StaleRunFinish
    action_finish: FinishCapture


@dataclass(frozen=True)
class StopCloseRequest:
    """原 STOP 收尾及适用普通动作结果，各自保留完整输入和原键。"""

    action_id: int
    finish: StaleRunFinish
    action_finish: FinishCapture | None = None
    action_key: OperationKey | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        if (self.action_finish is None) != (self.action_key is None):
            raise ConsistencyError("停止收尾的动作成员与操作键必须同时存在")
        if self.action_finish is not None and self.action_finish.action_id != self.action_id:
            raise ConsistencyError("停止收尾与动作成员的身份不一致")


CaptureCompletionRequest = (
    FinishCapture | FinishRecordingResults | FinishCanceledCapture | FinishBindingFailure
    | StartCloseRequest | StopCloseRequest | ExpireActionRequest
)


@dataclass(frozen=True)
class PendingCaptureCompletion:
    """原拍摄收场的完整输入；核实保存沿用原 key 和事实时刻。"""

    key: OperationKey
    request: CaptureCompletionRequest
    input_reads: PendingCaptureInputReads | None = None
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)

    @property
    def action_id(self) -> int:
        if isinstance(self.request, StartCloseRequest):
            return self.request.action_finish.action_id
        if isinstance(self.request, FinishRecordingResults):
            return self.request.capture.action_id
        if isinstance(self.request, (FinishCapture, FinishCanceledCapture, FinishBindingFailure, StopCloseRequest, ExpireActionRequest)):
            return self.request.action_id
        raise ConsistencyError("原拍摄完整申请类型无效")


@dataclass(frozen=True)
class PendingResultCheckClose:
    """结果核实收尾的完整申请；保存恢复沿用原 key 和时刻。"""

    key: OperationKey
    request: ResultSetSave | ResultRunClose | OutputSetFinalizationSave | FiniteCancelEndSave
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


async def resume_result_check_closes(
    database, *, pending_result_closes: dict[int, PendingResultCheckClose],
    action_id: int | None = None, capture: CaptureRepository | None = None,
    retry_gate: RetryWaitGate | None = None,
) -> None:
    """只核已形成的收尾申请，不取得新的设备或业务资格。"""
    repository = CaptureRepository() if capture is None else capture

    async def save_one(identity, pending):
        if pending.request.action_id != identity:
            raise ConsistencyError("原核实收尾申请与所属动作不一致")
        if isinstance(pending.request, ResultSetSave) and pending.request.error is not None:
            record_error(pending.request.error, stage="capture.result_check",
                context={"action_id": identity}, state=pending.diagnostic)
        if isinstance(pending.request, ResultSetSave):
            method = repository.close_result_check_unconfirmed
        elif isinstance(pending.request, ResultRunClose):
            method = repository.close_unconfirmed_result_run
        elif isinstance(pending.request, OutputSetFinalizationSave):
            method = repository.finalize_output_set
        elif isinstance(pending.request, FiniteCancelEndSave):
            method = save_finite_cancel_end
        else:
            raise ConsistencyError("原核实收尾申请类型无效")
        notification_error = None
        try:
            receipt = await database.write(lambda owned: method(pending.request, pending.key, owned), key=pending.key)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                raise
            notification_error = error
        complete = (receipt.kind is DbOutcomeKind.COMPLETED
            and (not isinstance(pending.request, ResultSetSave) or receipt.value is not None))
        if not complete:
            raise db_failure(ConsistencyError, f"原核实收尾申请未可靠保存，完整申请仍持有: {receipt.error}", receipt)
        if retry_gate is not None and not isinstance(pending.request, OutputSetFinalizationSave):
            activity = await database.read(lambda owned: _activity_id_of_connection(owned.connection, identity))
            retry_gate.cleared(f"results/{activity}")
        del pending_result_closes[identity]
        if notification_error is not None:
            raise notification_error

    async def save_and_apply():
        for identity, pending in tuple(pending_result_closes.items()):
            if action_id is not None and identity != action_id:
                continue
            if pending_result_closes.get(identity) is pending:
                pending.shared.retry_failed()
            await pending.shared.run(lambda: save_one(identity, pending))
    return await settle_required(save_and_apply())


async def _write_capture_completion(database, pending: PendingCaptureCompletion, repository: CaptureRepository, scheduling=None):
    """按申请类型入队原成员；STOP 与动作分别沿各自原键保存。"""
    async def save(callback, *, key, description):
        try:
            return await database.write(callback, key=key, description=description)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                raise
            return replace(receipt, handoff_error=error)

    request = pending.request
    if isinstance(request, ExpireActionRequest):
        method = (SchedulingRepository() if scheduling is None else scheduling).expire_action
        inputs = (request, pending.key)
    elif isinstance(request, StartCloseRequest):
        method, inputs = repository.close_start, (request.finish, request.action_finish, pending.key)
    elif isinstance(request, StopCloseRequest):
        receipt = await save(
            lambda owned: OperationRepository().finish_stale_runs(request.finish, pending.key, owned),
            key=pending.key, description="保存原停止收尾")
        if receipt.kind is not DbOutcomeKind.COMPLETED or request.action_finish is None:
            return receipt
        action_receipt = await save(
            lambda owned: repository.finish_capture(request.action_finish, request.action_key, owned),
            key=request.action_key, description="保存停止后的原动作结果")
        return (replace(action_receipt, handoff_error=receipt.handoff_error)
                if receipt.handoff_error is not None and action_receipt.handoff_error is None else action_receipt)
    else:
        methods = {FinishRecordingResults: repository.finish_recording_results,
                   FinishCanceledCapture: repository.finish_canceled_capture,
                   FinishBindingFailure: repository.finish_binding_failure,
                   FinishCapture: repository.finish_capture}
        method = methods.get(type(request))
        if method is None:
            raise ConsistencyError("原拍摄完整申请类型无效")
        inputs = (request, pending.key)
    return await save(lambda owned: method(*inputs, owned),
        key=pending.key, description="保存原拍摄收场")


async def resume_capture_completions(
    database, *, pending_capture_completions: dict[int, PendingCaptureCompletion],
    action_id: int | None = None, capture: CaptureRepository | None = None,
    retry_gate: RetryWaitGate | None = None,
    scheduling: SchedulingRepository | None = None,
) -> None:
    """核实已持有的完整拍摄申请，不取得时钟或设备端口。"""
    repository = CaptureRepository() if capture is None else capture

    async def save_one(identity, pending):
        if pending.action_id != identity:
            raise ConsistencyError("原拍摄完整申请与所属动作不一致")
        request = pending.request
        if isinstance(request, FinishRecordingResults):
            request = request.capture
        elif isinstance(request, (StartCloseRequest, StopCloseRequest)):
            request = request.action_finish
        if isinstance(request, (FinishCapture, FinishBindingFailure)) and request.failure is not None:
            record_error(request.failure, stage="capture.completion",
                context={"action_id": identity}, state=pending.diagnostic,
                details={"baseline_error": request.baseline_error}
                    if isinstance(request, FinishCapture) and request.baseline_error is not None else None)
        elif isinstance(pending.request, StopCloseRequest) and pending.request.finish.error is not None:
            record_error(pending.request.finish.error, stage="capture.stop_close",
                context={"action_id": identity}, state=pending.diagnostic)
        notification_error = None
        canceled = isinstance(pending.request, FinishCanceledCapture)
        start_close = isinstance(pending.request, StartCloseRequest)
        stop_close = isinstance(pending.request, StopCloseRequest)
        stop_only = stop_close and pending.request.action_finish is None
        if pending.input_reads is None:
            receipt = await _write_capture_completion(database, pending, repository, scheduling)
            if (receipt.kind in (DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED)
                    and type(pending.request) is FinishCapture
                    and pending.request.failure is None and pending.request.baseline_error is None):
                # 原普通完成确定未提交；同一已取得产物按新取消事实形成另一份申请。
                current = await database.read(lambda owned: row_facts(owned.connection, "actions", identity))
                if current is not None and current["status"] == 2 and current["cancel_requested"]:
                    command = pending.request
                    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(
                        action_id=command.action_id, occurred_at=command.occurred_at,
                        drafts=command.drafts, catalog_facts=command.catalog_facts))
                    pending_capture_completions[identity] = pending
                    return await pending.shared.run(lambda: save_one(identity, pending))
            notification_error = receipt.handoff_error
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"原拍摄收场未可靠保存，完整申请仍持有: {receipt.error}", receipt)
            if isinstance(pending.request, ExpireActionRequest):
                if receipt.value is None or receipt.value.outcome is not ExpireOutcome.EXPIRED:
                    raise ConsistencyError("原准备后过期申请未取得可靠过期结果")
                del pending_capture_completions[identity]
                if notification_error is not None:
                    raise notification_error
                return
            if not start_close and not stop_only:
                if receipt.value is None:
                    raise ConsistencyError("原拍摄收场缺少可靠处理结果")
                if receipt.value.disposition is FinishDisposition.RETIRED:
                    if stop_close and retry_gate is not None:
                        for responsibility in pending.request.finish.responsibility_keys:
                            retry_gate.cleared(responsibility)
                    del pending_capture_completions[identity]
                    if notification_error is not None:
                        raise notification_error
                    return
            if (start_close or stop_close) and retry_gate is not None:
                for responsibility in pending.request.finish.responsibility_keys:
                    retry_gate.cleared(responsibility)
            elif (retry_gate is not None
                    and isinstance(pending.request, (FinishRecordingResults, FinishCanceledCapture))):
                if canceled:
                    def read_responsibilities(owned):
                        with closing(owned.connection.execute(
                            "SELECT responsibility_key FROM operation_runs WHERE action_id=? AND kind=?",
                            (identity, int(_RUN_KIND.CHECK_CAPTURE_RESULTS)),
                        )) as cursor:
                            return tuple(row[0] for row in cursor.fetchall())
                    responsibilities = await database.read(read_responsibilities)
                else:
                    run = await database.read(lambda owned: row_facts(owned.connection, "operation_runs", pending.request.run_id))
                    if run is None:
                        raise ConsistencyError("已保存录像收场缺少原核实责任")
                    responsibilities = (run["responsibility_key"],)
                for responsibility in responsibilities:
                    retry_gate.cleared(responsibility)
            input_reads = await database.read(lambda owned: _capture_input_read_finish(owned, pending.request, identity))
            if input_reads is not None:
                pending = replace(pending, input_reads=input_reads)
                pending_capture_completions[identity] = pending
        if pending.input_reads is not None:
            try:
                result = await database.write(lambda owned: OperationRepository().finish_stale_runs(
                    pending.input_reads.request, pending.input_reads.key, owned), key=pending.input_reads.key)
            except BaseException as error:
                result = db_outcome_for(error)
                if result is None or result.kind is not DbOutcomeKind.COMPLETED:
                    raise
                notification_error = notification_error or error
            if result.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"拍摄终态后的原读取流程未可靠收尾，完整申请仍持有: {result.error}", result)
        del pending_capture_completions[identity]
        if not stop_only:
            record_event("capture.completion", event="settled", action_id=identity,
                request_type=type(pending.request).__name__)
        if notification_error is not None:
            raise notification_error

    async def save_and_apply():
        for identity, pending in tuple(pending_capture_completions.items()):
            if action_id is not None and identity != action_id:
                continue
            current = pending_capture_completions.get(identity)
            if current is not None and current.shared is pending.shared:
                # 原收场已经进入输入读取阶段时，核实必须继续使用该阶段的原申请。
                pending = current
                pending.shared.retry_failed()
            await pending.shared.run(lambda: save_one(identity, pending))
    return await settle_required(save_and_apply())


def _capture_input_read_finish(owned, request, action_id: int) -> PendingCaptureInputReads | None:
    """仅结束实际调用已返回的原输入读取责任，不修改读取尝试事实。"""
    if isinstance(request, (FinishBindingFailure, StartCloseRequest)):
        # 绑定失败已共同保存 READ；START 尚未确认时没有内部读取收尾。
        return None
    if isinstance(request, StopCloseRequest):
        if request.action_finish is None:
            return None
        request = request.action_finish
    with closing(owned.connection.execute(
        "SELECT r.id,r.kind,r.action_id,r.responsibility_key,fc.id,"
        " EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id AND t.status=?)"
        " FROM file_copies fc JOIN recording_processing p ON p.id=fc.processing_id"
        " JOIN operation_runs r ON r.copy_id=fc.id"
        " WHERE p.action_id=? AND fc.delivery_id IS NULL AND r.status IN (?,?) ORDER BY r.id",
        (int(_ATTEMPT_STATUS.RUNNING), action_id, int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)),
    )) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    for _run_id, kind, owner_id, key, copy_id, running in rows:
        if (kind != int(_RUN_KIND.READ_FILE) or owner_id != action_id or key != f"read/{copy_id}"):
            raise ConsistencyError("拍摄输入副本与原读取流程责任不一致")
        if running:
            raise ConsistencyError("原实际读取尚未结束，不能结束输入读取流程")
    keys = tuple(row[3] for row in rows)
    if len(set(keys)) != len(keys):
        raise ConsistencyError("拍摄输入副本的读取责任不唯一")
    command = request.capture if isinstance(request, FinishRecordingResults) else request
    failure = command.failure if isinstance(command, FinishCapture) else None
    status = (RunOutcome.CANCELED if isinstance(command, FinishCanceledCapture)
        else RunOutcome.SUCCEEDED if failure is None else RunOutcome.FAILED)
    error = (None if failure is None else ErrorValue(
        code=failure.code, stage=registered_error(failure.code)["stage"], details=dict(failure.details)))
    return PendingCaptureInputReads(new_operation_key(), StaleRunFinish(
        responsibility_keys=keys, status=status, error=error, occurred_at=command.occurred_at))


async def resume_canceled_recording_results(
    database, *, pending_capture_completions: dict[int, PendingCaptureCompletion],
    action_id: int | None = None, capture: CaptureRepository | None = None,
    retry_gate: RetryWaitGate | None = None,
) -> None:
    """原申请核实后，补存持久取消事实留下的原核实用途收尾。"""
    async def save_and_apply():
        repository = CaptureRepository() if capture is None else capture
        for request in await database.read(lambda owned: tuple(repository.canceled_recording_results(owned, action_id))):
            if request.action_id in pending_capture_completions:
                raise ConsistencyError("原录像完整申请尚未核实，不能形成取消用途申请")
            pending_capture_completions[request.action_id] = PendingCaptureCompletion(
                new_operation_key(), request)
            await resume_capture_completions(database, pending_capture_completions=pending_capture_completions,
                action_id=request.action_id, capture=repository, retry_gate=retry_gate)
    return await settle_required(save_and_apply())


@dataclass(frozen=True)
class PendingFileFact:
    """一个在场、归属、完成或摘要事实的原申请及可靠保存阶段。"""

    command: FilePresenceSave | OwnershipSave | FileCompletionSave | FileChecksumSave
    key: OperationKey
    response: ObservationOutcome | None = None
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


class FileFactStage(Enum):
    PRESENCE = "presence"
    OWNERSHIP = "ownership"
    COMPLETION = "completion"
    CHECKSUM = "checksum"


@dataclass(frozen=True)
class FileRegistration:
    """本批派生事实的原依据，独立于后来装配的运行配置。"""

    pairing_files: Mapping[str, tuple[ObservedFile, int]] = field(default_factory=dict)
    ownership_method: int = _TASK_SCOPE
    activity_id: int | None = None
    completion_basis: int = _DEVICE_GUARANTEE
    completion_activity_id: int | None = None
    wait_completed_event_id: int | None = None
    result_page_event_id: int | None = None
    stop_result_event_id: int | None = None
    start_result_event_id: int | None = None
    captured_checksums: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultPageFileInput:
    """已保存结果页中首次发现的文件与原事实时刻。"""

    page: ResultPageRef
    file_id: int
    identity: str
    occurred_at: int


@dataclass(frozen=True)
class ResultPageFileRef:
    file_id: int


@dataclass(frozen=True)
class PendingFileObservation:
    """独立于 RESULTS 收场的原文件发现请求及其可靠首次响应。"""

    command: FileObservationSave | ResultPageFileInput
    key: OperationKey
    entries: tuple[ObservedFile, ...]
    registered_files: Mapping[str, tuple[CaptureFile, int]]
    response: ObservationOutcome | ResultPageFileRef | None = None
    facts: dict[FileFactStage, PendingFileFact] = field(default_factory=dict)
    registration: FileRegistration = field(default_factory=FileRegistration)
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    registration_shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)


def _same_file_request(left, right) -> bool:
    """文件保存申请的标量保持原类型契约，JSON 按精确值核对。"""
    if type(left) is not type(right):
        return False
    if isinstance(left, ResultPageFileInput):
        return left == right
    if isinstance(left, FileObservationSave):
        return (replace(left, locator=right.locator) == right
                and json_equal(left.locator, right.locator))
    if isinstance(left, OwnershipSave):
        return (replace(left, observation=right.observation,
                        pairing_observation=right.pairing_observation) == right
                and json_equal(left.observation, right.observation)
                and json_equal(left.pairing_observation, right.pairing_observation))
    if isinstance(left, FileCompletionSave):
        return (replace(left, observation=right.observation, error=right.error,
                        locator=right.locator) == right
                and json_equal(left.observation, right.observation)
                and json_equal(left.error, right.error)
                and json_equal(left.locator, right.locator))
    if isinstance(left, FilePresenceSave):
        return (replace(left, error=right.error, locator=right.locator) == right
                and json_equal(left.error, right.error)
                and json_equal(left.locator, right.locator))
    if isinstance(left, FileChecksumSave):
        return left == right
    raise TypeError("文件申请必须属于发现、在场、归属、完成或摘要阶段")


def _same_observed_files(left: tuple[ObservedFile, ...] | None,
                         right: tuple[ObservedFile, ...]) -> bool:
    """同一原列举保留条目顺序、全部元数据及精确 JSON 观察。"""
    return left is not None and len(left) == len(right) and all(
        replace(original, locator=current.locator, evidence=current.evidence) == current
        and json_equal(original.locator, current.locator)
        and json_equal(original.evidence, current.evidence)
        for original, current in zip(left, right))


class _FileObservationSaves:
    """只保存已取得的原文件事实，不持有设备端口或当前业务资格。"""

    def __init__(self, database, pending_file_observations, pending_call_results):
        self.database = database
        self.capture = CaptureRepository()
        self.pending_file_observations = pending_file_observations
        self.pending_start_results = pending_call_results
        def original_time_required():
            raise ConsistencyError("原文件保存必须使用已持有的事实时刻")
        self.wall_us = original_time_required

    async def resume_file_observations(self, action_id: int) -> None:
        """业务筛选前核实原发现；后续文件事实可靠保存前保持首次响应。"""
        for identity, pending in tuple(self.pending_file_observations.items()):
            if identity[0] == action_id and identity in self.pending_file_observations:
                pending.registration_shared.retry_failed()
                await pending.registration_shared.run(lambda: _apply_observed(
                    self, action_id, pending.entries, pending.registered_files, pending.registration))

    async def _save_file_observations(self, action_id: int) -> None:
        """仅核实持有的发现请求；整批事实保存负责释放生命周期。"""
        for identity, pending in tuple(self.pending_file_observations.items()):
            if identity[0] != action_id or pending.response is not None:
                continue
            async def save_and_apply():
                _delivery_error = None
                error = None
                for _ in range(2):
                    try:
                        receipt = await self.database.write(
                            lambda owned: self.capture.save_file_observation(pending.command, pending.key, owned),
                            key=pending.key, description="保存原文件发现")
                    except BaseException as failure:
                        receipt = db_outcome_for(failure)
                        if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                            raise
                        _delivery_error = failure
                    if receipt.kind is DbOutcomeKind.COMPLETED:
                        self.pending_file_observations[identity] = replace(pending, response=receipt.value)
                        if _delivery_error is not None:
                            raise _delivery_error
                        return
                    error = receipt.error
                raise db_failure(ConsistencyError, f"原文件发现未可靠保存，原请求仍持有: {error}", receipt)
            pending.shared.retry_failed()
            await pending.shared.run(save_and_apply)

    async def _save_file_fact(self, identity: tuple[int, str], stage: FileFactStage,
                        command: FilePresenceSave | OwnershipSave | FileCompletionSave | FileChecksumSave) -> None:
        """子阶段首次确定原请求和 key，未知提交先核实该身份。"""
        observation = self.pending_file_observations[identity]
        pending = observation.facts.get(stage)
        if pending is None:
            pending = PendingFileFact(command, new_operation_key())
            observation.facts[stage] = pending
        elif not _same_file_request(pending.command, command):
            raise ConsistencyError("原文件事实子阶段的完整申请不可替换")
        if pending.response is not None:
            return
        if isinstance(command, (FilePresenceSave, FileCompletionSave)) and command.error is not None:
            record_error(command.error, stage="capture.file_fact",
                context={"action_id": identity[0], "file_id": command.file_id,
                         "fact_stage": stage.value}, state=pending.diagnostic)
        save = {FileFactStage.PRESENCE: self.capture.save_file_presence,
                FileFactStage.OWNERSHIP: self.capture.save_file_ownership,
                FileFactStage.COMPLETION: self.capture.save_file_completion,
                FileFactStage.CHECKSUM: self.capture.save_file_checksum}[stage]
        async def save_and_apply():
            _delivery_error = None
            error = None
            for _ in range(2):
                try:
                    receipt = await self.database.write(
                        lambda owned: save(pending.command, pending.key, owned),
                        key=pending.key, description="保存原文件事实")
                except BaseException as failure:
                    receipt = db_outcome_for(failure)
                    if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                        raise
                    _delivery_error = failure
                if receipt.kind is DbOutcomeKind.COMPLETED:
                    observation.facts[stage] = replace(pending, response=receipt.value)
                    if _delivery_error is not None:
                        raise _delivery_error
                    return
                error = receipt.error
            raise db_failure(ConsistencyError, f"原文件事实未可靠保存，原申请仍持有: {error}", receipt)
        pending.shared.retry_failed()
        await pending.shared.run(save_and_apply)

    async def action_id_of_ticket(self, ticket: AttemptTicket) -> int:
        run = await self.database.read(lambda owned: row_facts(owned.connection, "operation_runs", ticket.run_id))
        if run is None:
            raise ConsistencyError("原票据缺少所属流程")
        return run["action_id"]


async def resume_file_observations(database, *, pending_file_observations, pending_call_results) -> None:
    """先接手同会话文件责任，再取得设备、时钟和业务资格。"""
    saves = _FileObservationSaves(database, pending_file_observations, pending_call_results)
    for action_id in dict.fromkeys(identity[0] for identity in tuple(pending_file_observations)):
        await saves.resume_file_observations(action_id)


class DeviceControlPort(Protocol):
    """设备控制调用端口；契约替身与真实驱动同形。"""

    async def control(self, request: ControlRequest) -> DeviceCallResult: ...


class DeviceStopPort(Protocol):
    """设备停止操作端口；录像停止调用经此发出。"""

    async def stop(self, request: ControlRequest) -> DeviceCallResult: ...


class ResultFilesPort(Protocol):
    """结果列举端口：返回本任务观察到的候选产物文件。"""

    async def list_round(self, ticket: AttemptTicket, *, timeout_s: Decimal) -> ListedResult: ...

    async def list_page(self, ticket: AttemptTicket, *, cursor, timeout_s: Decimal,
                        output_scope: Mapping | None = None,
                        completion_context: Mapping | None = None,
                        video_result_mode: str | None = None,
                        reading_context=None, scope_only: bool = False) -> ResultPage: ...


class RecordingStatePort(Protocol):
    """录像中段事实端口；活动事实生产者接入前由装配层提供。"""

    def recording_state(self, action_id: int) -> RecordingState: ...


@dataclass(frozen=True)
class AppliedNaturalWait:
    """可靠保存的安排；逻辑截止保留原参考对的有符号结果。"""

    expected_check_at: int
    extra_wait_ms: int
    logical_deadline_ns: int


@dataclass
class PendingNaturalWait:
    """未决保存持有原键及完整申请，核实前不采用后续配置。"""

    command: ScheduleWait | WaitCompletedSave
    key: OperationKey
    arrangement: AppliedNaturalWait
    reconfigure: bool = False
    verified_absent: bool = False


@dataclass
class NaturalWaitState:
    """同一原活动在一个会话内的不可变映射与可靠／未决事实。"""

    activity_id: int
    reference: WaitClockReference
    applied: AppliedNaturalWait | None = None
    pending: PendingNaturalWait | None = None


@dataclass
class CaptureRuntime(_FileObservationSaves):
    """处理器组合的真实仓储端口与设备替身注入点。

    window_of 从动作行取得启动窗口；wait_config 从动作行取得延时
    等待配置（目标时长与余量读首次固定的执行定义）；monotonic_ns
    提供会话单调钟；evidence 登记驱动观察契约供尝试结果校验。
    """

    database: BusinessDatabase
    scheduling: SchedulingRepository
    operations: OperationRepository
    capture: CaptureRepository
    timelapse: TimelapseRepository
    driver: DeviceControlPort
    results: ResultFilesPort | None
    evidence: Any
    wall_us: Callable[[], int]
    monotonic_ns: Callable[[], int]
    window_of: Callable[[Mapping[str, Any]], LaunchWindow]
    wait_config: Callable[[Mapping[str, Any]], CaptureWaitConfig]
    scope_closure_supported: bool = False
    #: 核对已保存设备身份与本次固定配置；未提供时由调用方保证绑定匹配。
    binding_check: Callable[[DeviceBinding], BindingResult] | None = None
    #: 本次会话的预期机身，用于首次建立活动；不是实际设备观察。
    fixed_camera_identity: Any = None
    recording_state: RecordingStatePort | None = None
    #: 只发布本会话普通录像的原停止截止；None 清除对应动作的提示。
    on_recording_deadline: Callable[[int, int | None], None] | None = None
    #: 录像停止调用端口；未装配时录像不能停止。
    stopper: DeviceStopPort | None = None
    #: 录像媒体链端口；未装配时需要检查的录像不推进，等待装配会话。
    media: MediaFlow | None = None
    #: 异常多录修复门槛的本次余量秒数（configuration.md#配置归属）。
    repair_margin_s: Decimal = Decimal("10")
    #: 录像启动使用本次设备配置；正常与恢复保持原责任累计次数。
    start_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 停止尝试的本次预算；默认 3 次、单次 10 秒、重试间隔 3 秒
    #:（configuration.md#通信重试间隔）。
    stop_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 结果核实轮次的本次预算；默认 3 轮、每次查询 10 秒、重试间隔 3 秒
    #:（configuration.md#状态查询与产物核实的配置）。
    check_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 重试间隔的会话内时间门槛；装配层闭包共享，跨推进轮次保留。
    retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)
    #: 会话内已观察的结果列举缓存（动作到列举与登记事实）；装配层
    #: 闭包共享，跨推进轮次保留，避免等待中的重复列举消耗核实名额。
    listing_cache: dict[int, tuple[tuple, tuple]] | None = None
    #: 本次会话未完成延时等待的单调截止；装配层跨推进共享，完成即释放。
    timelapse_deadlines: dict[int, int] = field(default_factory=dict)
    #: 每个原活动的会话映射、可靠安排和未决原申请；装配层共享。
    natural_wait_states: dict[int, NaturalWaitState] = field(default_factory=dict)
    #: 设备状态查询端口；未装配时执行前检查与残留收场确认查询不
    #: 推进，触发动作按自身窗口与取消规则收尾。
    state_query: Any = None
    #: 执行前检查与确认查询的本次预算；默认 3 次、单次 10 秒、重试
    #: 间隔 3 秒（configuration.md#状态查询与产物核实的配置）。
    query_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 残留收场停止的本次预算；默认 3 次包含第一次、单次 10 秒、
    #: 重试间隔 3 秒（camera-recovery.md#后续动作触发的残留收场）。
    software_recovery_driver: Any = None
    software_recovery_config: Any = None
    residual_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 仅恢复固定启动边界之前的旧尝试，不用于本会话新派发调用。
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    recovery_max_event_id: int | None = None
    recovery_evidence_for: Callable[[DeviceBinding, str], Any] | None = None
    #: 会话装配共享，未核实的实际结果不能由 UNKNOWN 恢复覆盖。
    pending_start_results: dict[tuple[int, int], PendingCallResult] = field(default_factory=dict)
    #: 原拍摄完整收场申请独立于原调用尝试和当前设备资格。
    pending_capture_completions: dict[int, PendingCaptureCompletion] = field(default_factory=dict)
    #: 主机单张文件责任及完整字节请求；UNKNOWN 时保留原请求与键。
    pending_host_photo_prepares: dict[int, Any] = field(default_factory=dict)
    pending_host_photo_completions: dict[int, Any] = field(default_factory=dict)
    #: 主机正式文件归档使用已固定 staging 根目录和专用仓储。
    roots: BoundDirectories | None = None
    host_capture: Any = None
    #: 未知主机拍摄保存回执的独立只读核实端口；连接由装配层创建和关闭。
    #: 没有新尝试承载的有限耗尽决定，由三个拍摄工厂共用。
    pending_result_closes: dict[int, PendingResultCheckClose] = field(default_factory=dict)
    pending_read_results: dict = field(default_factory=dict)
    pending_read_business: dict = field(default_factory=dict)
    pending_read_ends: dict = field(default_factory=dict)
    continuing_read_tickets: dict = field(default_factory=dict)
    #: 无恢复依据保留责任与诊断；成功核实后清除，仅供本地消费。
    last_recovery_diagnostic: RecoveryDiagnostic | SoftwareRecoveryDiagnostic | None = None
    #: 诊断进入本地运行日志，不影响原业务责任；相同诊断不重复投递。
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic | SoftwareRecoveryDiagnostic], None] | None = None
    #: 已取得的媒体原申请由会话共享，保存资格独立于驱动和动作终态。
    pending_media_results: dict = field(default_factory=dict)
    file_executor: Any = None
    #: 原文件发现写入前持有，普通、残留与受限工厂共用。
    pending_file_observations: dict[tuple[int, str], PendingFileObservation] = field(default_factory=dict)
    #: 目录准备与原保存申请跨推进轮次保留，独立于驱动资格。
    pending_baselines: dict = field(default_factory=dict)
    #: 同一 RESULTS 尝试的原页交接；保存未知时不读下一页。
    pending_result_scans: dict[tuple[int, int], PendingResultScan] = field(default_factory=dict)
    #: 分段模块持有每个动作至多一份未决有限申请，与单原片媒体责任独立。
    pending_segment_writes: dict = field(default_factory=dict)
    pending_segment_inspection_results: dict = field(default_factory=dict)
    baseline_directory: DirectoryReader | None = None
    start_entries: Any = field(default_factory=StartEntryCoordinator)
    capture_preparation_supported: bool = False
    preparation_driver: CapturePreparationDriver | None = None
    preparation_cache: Any = None
    session_key: str | None = None
    prepare_lead_s: Decimal = Decimal(0)
    #: 当前会话尚需执行的设备配置预算；仅查询驱动内存，不访问设备。
    session_setup_budget: Callable[[DeviceBinding], Decimal] | None = None

    @property
    def baseline_timeout_s(self):
        return self.check_config.timeout_s

    async def prepare_baseline(self, action_id):
        """通过共同准备边界取得原基准，完成后仍由授予事务核验。"""
        from camctl.logging_runtime.performance import measure_performance

        action = (await self.action(action_id))
        with measure_performance("baseline_preparation", action_id=action_id,
                                 device_id=action["device_id"]):
            if action["execution_spec_json"].get("result_policy") is not None:
                from camctl.capture.segment_baseline import prepare_segment_baseline

                return await prepare_segment_baseline(
                    action_id, (await _activity_id_of(self, action_id)), self
                )
            from camctl.capture.baseline import prepare_baseline
            return await prepare_baseline(action_id, (await _activity_id_of(self, action_id)), runtime=self)

    async def resume_baselines(self, action_id):
        from camctl.capture.baseline import PreparationPhase, resume_baseline_settlement
        result = (await resume_baseline_settlement(action_id, runtime=self))
        if result is not None and result.phase is PreparationPhase.PENDING:
            raise ConsistencyError(f"原基准保存未可靠完成: {result.database_error}") from result.database_error
        return result

    def record_recovery_diagnostic(self, diagnostic: RecoveryDiagnostic | SoftwareRecoveryDiagnostic) -> None:
        if diagnostic == self.last_recovery_diagnostic:
            return
        self.last_recovery_diagnostic = diagnostic
        if self.on_recovery_diagnostic is not None:
            self.on_recovery_diagnostic(diagnostic)

    async def capture_canceled(self, action_id: int) -> bool:
        return bool((await self.action(action_id))["cancel_requested"])

    async def action(self, action_id: int) -> Mapping[str, Any]:
        facts = await self.database.read(lambda owned: row_facts(owned.connection, "actions", action_id),
                                         description="读取拍摄动作")
        if facts is None:
            raise LookupError(f"动作不存在: {action_id}")
        return facts

    async def resume_result_check_closes(self, action_id: int) -> None:
        """新业务判断之前核原耗尽事务，保存失败时保留原完整申请。"""
        (await resume_result_check_closes(self.database,
            pending_result_closes=self.pending_result_closes, action_id=action_id,
            capture=self.capture, retry_gate=self.retry_gate))

    async def resume_capture_completions(self, action_id: int) -> None:
        """先核原完整收场申请，动作已有终态也不跳过原事务。"""
        if (
            (await self.action(action_id))["execution_spec_json"].get("result_policy")
            is not None
        ):
            from camctl.capture.segment_completion import resume_segment_writes

            (await resume_segment_writes(self.database, self.pending_segment_writes, action_id))
        (await resume_capture_completions(self.database,
            pending_capture_completions=self.pending_capture_completions,
            action_id=action_id, capture=self.capture, retry_gate=self.retry_gate,
            scheduling=self.scheduling))
        (await resume_canceled_recording_results(self.database,
            pending_capture_completions=self.pending_capture_completions,
            action_id=action_id, capture=self.capture, retry_gate=self.retry_gate))

    async def save_capture_completion(
        self, request: CaptureCompletionRequest,
    ) -> None:
        """首次保存前交接完整申请；未知和回滚后继续由会话持有。"""
        pending = PendingCaptureCompletion(new_operation_key(), request)
        command = request.capture if isinstance(request, FinishRecordingResults) else (
            request.action_finish if isinstance(request, (StartCloseRequest, StopCloseRequest)) else request)
        if isinstance(command, FinishCapture):
            diagnostic = preparation_failure_diagnostic(self, command.action_id, command.failure)
            if diagnostic is not None:
                pending = replace(pending, diagnostic=diagnostic)
        if pending.action_id in self.pending_capture_completions:
            raise ConsistencyError("原拍摄完整申请尚未核实，不能替换")
        self.pending_capture_completions[pending.action_id] = pending
        (await self.resume_capture_completions(pending.action_id))

    async def last_attempt(self, responsibility: str):
        """读取该责任的最近尝试：状态、效果与意图事实时刻。"""
        from contextlib import closing

        def read(owned):
            with closing(owned.connection.execute(
                "SELECT a.status, a.effect_state, e.occurred_at FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " JOIN history_events e ON e.id = a.intent_event_id"
                " WHERE r.responsibility_key = ? ORDER BY a.id DESC LIMIT 1",
                (responsibility,),
            )) as cursor:
                return cursor.fetchone()
        return await self.database.read(read, description="读取拍摄责任最近尝试")

    async def grant(self, action: Mapping[str, Any]):
        request = GrantRequest(
            device_id=action["device_id"],
            action_id=action["id"],
            window=self.window_of(action),
            trusted_wall_now=self.wall_us(),
            config=(self.start_config if action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
                    else AttemptConfig(max_attempts=1, timeout_s=_start_call_timeout(action))),
            occurred_at=self.wall_us(),
            current_identity=self.fixed_camera_identity,
        )
        permit = None
        if is_hasselblad_photo(action):
            existing = self.start_entries.get(self.database.metadata.instance_id, action["id"])
            if existing is not None:
                pending = self.start_entries.pending_grants.get((existing.database_id, existing.action_id))
                if pending is not None:
                    return await pending()
                if existing.phase in (EntryPhase.RESERVED, EntryPhase.BLOCKED):
                    return None, "canceled"
                if existing.phase is EntryPhase.READY and existing.ticket is not None:
                    return existing.ticket, None
                if existing.phase is not EntryPhase.READY:
                    raise ConsistencyError("原 START 入口许可仍承担责任")
                if existing.cancel_origins:
                    await _refresh_photo_cancel_origins(self, existing)
                    if existing.phase is not EntryPhase.READY or existing.cancel_origins:
                        return None, "canceled"
                permit = existing
            else:
                permit = self.start_entries.prepare(self.database.metadata.instance_id, action["id"])
            permit.phase = EntryPhase.GRANTING
        key = new_operation_key()
        shared = SharedSave()
        async def save_and_apply():
            _delivery_error = None
            try:
                try:
                    outcome = await self.database.write(
                        lambda owned: self.scheduling.grant_start(request, key, owned), key=key,
                        description="授予拍摄启动")
                except BaseException as failure:
                    outcome = db_outcome_for(failure)
                    if outcome is None or outcome.kind is not DbOutcomeKind.COMPLETED:
                        raise
                    _delivery_error = failure
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise db_failure(ConsistencyError, f"启动授予事务未可靠完成: {outcome.error}", outcome)
            except BaseException:
                if permit is not None:
                    permit.phase = EntryPhase.UNKNOWN
                raise
            result = outcome.value
            if result.outcome.value != "granted":
                if permit is not None:
                    del self.start_entries.permits[(permit.database_id, permit.action_id)]
                    self.start_entries.pending_grants.pop((permit.database_id, permit.action_id), None)
                if _delivery_error is not None:
                    raise _delivery_error
                return None, result.reason
            if permit is not None:
                permit.bind(self.database.metadata.instance_id, result.ticket)
                self.start_entries.pending_grants.pop((permit.database_id, permit.action_id), None)
            if _delivery_error is not None:
                raise _delivery_error
            return result.ticket, None
        async def resume():
            shared.retry_failed()
            return await shared.run(save_and_apply)
        if permit is not None:
            self.start_entries.pending_grants[(permit.database_id, permit.action_id)] = resume
        return await resume()

    async def finish(self, ticket, outcome: CallOutcome, *,
               end_run: RunOutcome | None = None,
               run_error: ErrorValue | None = None,
               retry_wait: bool = False,
               activity: ActivityObservationSave | None = None,
               start_finish: StaleRunFinish | None = None,
               action_failure: RecordingFailure | None = None,
               occurred_at: int | None = None,
               expiration: ExpireActionRequest | None = None,
               confirmation_anchor_ns: int | None = None,
               returned_ns: int | None = None,
               canceled_unstarted: bool = False,
               host_timer_stop: HostTimerStopSave | None = None,
               capture_stop: bool = False,
               residual_conclude: int | None = None,
               query_conclude: int | None = None,
               query_conclude_stop_keys: tuple[str, ...] = (),
               diagnostics_check: Callable[[CallOutcome], None] | None = None) -> None:
        """先持有完整实际结果，再保存原尝试及其适用的伴随事实。

        保存重试等待使用调用返回时的单调读数；流程结束清除。
        """
        attempt = UnvalidatedAttemptFinish(
            ticket=ticket,
            outcome=UnvalidatedCallOutcome(outcome),
            occurred_at=self.wall_us() if occurred_at is None else occurred_at,
            run_finish=None if end_run is None else RunFinish(
                status=end_run, error=run_error),
            retry_wait=retry_wait,
        )
        identity = (ticket.run_id, ticket.attempt_id)
        if identity in self.pending_start_results:
            raise ConsistencyError("原调用结果仍待核实，不能替换实际结果")
        pending = PendingCallResult(
            new_operation_key(), attempt, activity, start_finish,
            expiration=expiration, confirmation_anchor_ns=confirmation_anchor_ns,
            returned_ns=self.monotonic_ns() if returned_ns is None else returned_ns,
            action_failure=action_failure, canceled_unstarted=canceled_unstarted,
            host_timer_stop=host_timer_stop, capture_stop=capture_stop,
            residual_conclude=residual_conclude, query_conclude=query_conclude,
            query_conclude_stop_keys=query_conclude_stop_keys,
            diagnostics_check=diagnostics_check)
        self.pending_start_results[identity] = pending
        (await self._save_call_result(identity, pending))

    def _validate_call_result(self, identity, pending: PendingCallResult) -> PendingCallResult:
        """原申请已持有后只附加校验视图；诊断错误保留原申请。"""
        if pending.validated:
            return pending
        original = pending.finish
        try:
            if pending.diagnostics_check is not None:
                try:
                    pending.diagnostics_check(original.outcome.outcome)
                except Exception as failure:
                    raise DiagnosticsValidationError("原诊断构造检查失败") from failure
            validated = validate_outcome(original.ticket, original.outcome.outcome, self.evidence)
        except DiagnosticsValidationError as failure:
            error = ConsistencyError("原诊断校验失败，完整结果与字节仍持有")
            error.__cause__ = failure
            record_error(error, stage="capture.result_validation",
                context={"ticket": original.ticket}, state=pending.diagnostic,
                details={"outcome": original.outcome.outcome})
            raise error
        except OutcomeValidationError:
            # 一般无效观察仍由既有业务分支拒绝，不保留可保存的原申请。
            del self.pending_start_results[identity]
            raise
        pending = replace(pending, finish=AttemptFinish(
            original.ticket, validated, original.occurred_at,
            retry_wait=original.retry_wait, run_finish=original.run_finish))
        self.pending_start_results[identity] = pending
        return pending

    async def _write_call_result(self, pending: PendingCallResult):
        """原请求的固定派生输入由各仓储在事务内核对可靠资格。"""
        if not pending.validated:
            raise ConsistencyError("未校验原结果不能提交仓储")
        ticket = pending.finish.ticket
        run = (await self.database.read(lambda owned, inputs=("operation_runs", ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if run is None:
            raise ConsistencyError("原调用结果缺少所属流程")
        if not pending.result_disposition_ready:
            raise ConsistencyError("原 RESULTS 仍持有，所属消费者尚未确定结果处置")
        if pending.host_timer_stop is not None:
            _save_inputs = (pending.finish, pending.host_timer_stop, pending.key)
            _save_options = dict()
            return (await self.database.write(lambda owned: self.capture.finish_host_timer_stop(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if pending.capture_stop:
            _save_inputs = (pending.finish, pending.key)
            _save_options = dict()
            return (await self.database.write(lambda owned: self.capture.finish_capture_stop(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if pending.residual_conclude is not None:
            _save_inputs = (pending.finish, pending.residual_conclude, pending.key)
            _save_options = dict()
            return (await self.database.write(lambda owned: self.capture.finish_residual_stop(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if pending.query_conclude is not None:
            _save_inputs = (pending.finish, pending.query_conclude, pending.query_conclude_stop_keys, pending.key)
            _save_options = dict()
            return (await self.database.write(lambda owned: self.capture.finish_query_conclude(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if (pending.result_set is not None or pending.completion_page is not None
                or pending.finalized_page is not None):
            if run["kind"] not in (
                    int(_RUN_KIND.CHECK_CAPTURE_RESULTS), int(_RUN_KIND.RESOLVE_OUTPUT_SCOPE)):
                raise ConsistencyError("结果复合保存必须使用结果核实或范围收尾责任")
            options = {"completion_page": pending.completion_page}
            if pending.finalized_page is not None:
                options["finalized_page"] = pending.finalized_page
            _save_inputs = (pending.finish, pending.result_set, pending.key)
            _save_options = dict(**options)
            return (await self.database.write(lambda owned: self.capture.finish_result_check(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        is_start = run["kind"] == int(_RUN_KIND.START) or (
            run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
            and run["query_purpose"] == int(_QUERY_PURPOSE.START_CONFIRMATION))
        if not is_start:
            if (pending.observation is not None or pending.start_finish is not None
                    or pending.action_failure is not None or pending.expiration is not None
                    or pending.canceled_unstarted):
                raise ConsistencyError("普通调用结果不能携带启动专属派生请求")
            _save_inputs = (pending.finish, pending.key)
            _save_options = dict()
            return (await self.database.write(lambda owned: self.operations.finish_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        action_finish = pending.action_finish
        if pending.action_failure is not None:
            action_finish = FinishCapture(
                run["action_id"], (), OutputCatalogFacts(run["action_id"], True),
                pending.finish.occurred_at, failure=pending.action_failure)
        if pending.canceled_unstarted:
            action_finish = FinishCanceledCapture(
                run["action_id"], pending.finish.occurred_at, unstarted=True)
        segmented = (
            (await self.action(run["action_id"]))["execution_spec_json"].get("result_policy")
            is not None
        )
        _save_inputs = (pending.finish, pending.observation, pending.key)
        _save_options = dict(start_finish=pending.start_finish, action_finish=None if segmented else action_finish, expiration=pending.expiration)
        receipt = (await self.database.write(lambda owned: self.capture.finish_start_result(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if (
            segmented
            and action_finish is not None
            and receipt.kind is DbOutcomeKind.COMPLETED
        ):
            if (
                pending.canceled_unstarted
                or (await self.action(run["action_id"]))["cancel_requested"]
            ):
                (await _finish_canceled_capture(self, run["action_id"], unstarted=True))
            else:
                from camctl.capture.segment_completion import (
                    finish_segmented_start_failure,
                )

                (await finish_segmented_start_failure(self, run["action_id"]))
        return receipt

    def hold_call_result(
        self, ticket: AttemptTicket, outcome: CallOutcome, *,
        occurred_at: int, returned_ns: int,
        preparation: RecordingStartPreparation | StartConfirmationPreparation | None = None,
        result_listing: tuple[ObservedFile, ...] | SavedResultEntries | None = None,
        diagnostics_check: Callable[[CallOutcome], None] | None = None,
    ) -> None:
        """在实际 await 返回后登记原结果，派生读取之前建立内存责任。"""
        identity = (ticket.run_id, ticket.attempt_id)
        if identity in self.pending_start_results:
            raise ConsistencyError("原调用结果仍待核实，不能替换实际结果")
        self.pending_start_results[identity] = PendingCallResult(
            new_operation_key(), UnvalidatedAttemptFinish(
                ticket, UnvalidatedCallOutcome(outcome), occurred_at),
            None, returned_ns=returned_ns, preparation=preparation,
            result_listing=result_listing, diagnostics_check=diagnostics_check,
            result_disposition_ready=result_listing is None)
        self._validate_call_result(identity, self.pending_start_results[identity])

    async def save_held_result(self, ticket: AttemptTicket) -> None:
        """原执行者与恢复入口使用同一个已持有结果。"""
        identity = (ticket.run_id, ticket.attempt_id)
        pending = self.pending_start_results.get(identity)
        if pending is None or pending.finish.ticket != ticket:
            raise ConsistencyError("原调用结果尚未持有或票据不符")
        (await self._save_call_result(identity, pending, retry=True))

    async def _save_call_result(self, identity, pending: PendingCallResult, *, retry=False) -> None:
        """最多重送一次原事务；可靠回滚与提交后错误使用同一身份。"""
        # 校验不进入有限 DB 重送循环；本次失败不自动重复校验。
        async def save_and_apply(pending):
            pending = self._validate_call_result(identity, pending)
            for _ in range(2):
                try:
                    if pending.preparation is not None:
                        pending = (await _prepare_held_start_result(self, pending))
                        self.pending_start_results[identity] = pending
                    actual = pending.finish.outcome.outcome
                    run_error = None if pending.finish.run_finish is None else pending.finish.run_finish.error
                    if (not is_capture_control_error(actual.error)
                            or run_error is not None and not is_capture_control_error(run_error)):
                        scan = self.pending_result_scans.get(identity)
                        record_call_outcome(actual, ticket=pending.finish.ticket,
                            run_error=run_error,
                            context={"stage": "capture.call_result"},
                            state=pending.diagnostic if scan is None else scan.diagnostic)
                    delivery_error = None
                    try:
                        receipt = await self._write_call_result(pending)
                    except BaseException as failure:
                        receipt = db_outcome_for(failure)
                        if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                            raise
                        delivery_error = failure
                    error = receipt.error
                    if isinstance(error, ResultEncodingError):
                        raise error
                except ResultEncodingError as failure:
                    raise ConsistencyError("原结果编码失败，完整结果与字节仍持有") from failure
                except (DatabaseAccessError, ConsistencyError) as failure:
                    receipt = None
                    error = failure
                if receipt is not None and receipt.kind is DbOutcomeKind.COMPLETED:
                    ticket = pending.finish.ticket
                    if pending.finish.retry_wait:
                        self.retry_gate.established(ticket.responsibility_key, pending.returned_ns)
                    elif pending.finish.run_finish is not None or pending.expiration is not None:
                        self.retry_gate.cleared(ticket.responsibility_key)
                    if pending.confirmation_anchor_ns is not None:
                        action_id = (await self.action_id_of_ticket(ticket))
                        action = (await self.action(action_id))
                        if action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE):
                            if action["execution_spec_json"]["end_control"] == int(EndControl.HOST_TIMER):
                                timing = CaptureTiming(_target_duration_ms(action), action["execution_spec_json"].get("startup_margin_ms", 0))
                                self.timelapse_deadlines[action_id] = timing.deadline_ns(pending.confirmation_anchor_ns)
                            else:
                                activity_id = (await _activity_id_of(self, action_id))
                                state = self.natural_wait_states.get(action_id)
                                if state is None:
                                    self.natural_wait_states[action_id] = NaturalWaitState(
                                        activity_id, WaitClockReference(
                                            pending.finish.occurred_at, pending.confirmation_anchor_ns))
                                elif state.activity_id != activity_id:
                                    raise ConsistencyError("原延时等待属于其他活动")
                        else:
                            _recording_port(self).anchor_confirmed(
                                action_id, pending.confirmation_anchor_ns,
                                recording_stop_target(pending.confirmation_anchor_ns,
                                                      _target_duration_ms(action),
                                                      action["execution_spec_json"].get("startup_margin_ms", 0)))
                    del self.pending_start_results[identity]
                    scan = self.pending_result_scans.get(identity)
                    if scan is not None:
                        scan.close()
                        self.pending_result_scans.pop(identity)
                        scan.log_return("returned", scan_complete=False,
                            page_count=0 if scan.last_ref is None else scan.last_ref.page_no,
                            reason="result_disposition_saved")
                    if delivery_error is not None:
                        raise delivery_error
                    return
            if receipt is not None:
                raise db_failure(ConsistencyError, f"原调用结果未可靠保存，原结果仍持有: {error}", receipt)
            raise ConsistencyError(f"原调用结果未可靠保存，原结果仍持有: {error}") from error
        return await pending.save(self.pending_start_results, identity, save_and_apply, retry=retry)

    async def resume_start_results(self, action_id: int) -> bool:
        """同会话原真实结果优先保存，包括动作已经终态的分区。"""
        changed = False
        for identity, pending in tuple(self.pending_start_results.items()):
            if not pending.result_disposition_ready:
                continue
            if (await self.action_id_of_ticket(pending.finish.ticket)) == action_id:
                (await self._save_call_result(identity, pending, retry=True))
                changed |= self.pending_start_results.get(identity) is not pending
        return changed

    async def retry_wait_remaining(self, responsibility: str,
                             interval_s: Decimal | None, *,
                             maximum: int | None = None) -> Decimal | None:
        """责任当前重试等待的剩余秒数；可开始下一次尝试时为 None。

        以流程行的等待标志与累计次数为权威（首次尝试不预先等待、
        预算耗尽即时交还意图事务），跨会话恢复的等待按本次间隔从
        本会话首次观察重新计时。
        """
        def read(owned):
            with closing(owned.connection.execute(
                "SELECT attempts_used, retry_wait_required, max_attempts_used"
                " FROM operation_runs WHERE responsibility_key = ?",
                (responsibility,),
            )) as cursor:
                return cursor.fetchone()
        row = await self.database.read(read, description="读取拍摄重试等待")
        if row is None:
            return None
        return self.retry_gate.remaining(
            responsibility,
            attempts_used=int(row[0]),
            retry_wait_required=int(row[1]) == 1,
            max_attempts_used=int(row[2]) if maximum is None else maximum,
            interval_s=interval_s,
            now_ns=self.monotonic_ns())

    async def recover_attempt(self, ticket: AttemptTicket, *, retry_wait: bool = False) -> bool:
        """可靠旧会话边界结束原前台发令；未知设备活动另行核实。"""
        if ticket.operation == "result":
            from camctl.host_files.downloads import download_owned
            if download_owned(ticket):
                return False
        identity = (ticket.run_id, ticket.attempt_id)
        pending = self.pending_start_results.get(identity)
        if pending is not None:
            pending = self._validate_call_result(identity, pending)
            if not pending.result_disposition_ready:
                pending = (await _settle_recovered_listing(self, pending))
                self.pending_start_results[identity] = pending
            (await self._save_call_result(identity, pending, retry=True))
            self.last_recovery_diagnostic = None
            return True
        def read(owned, parameters=(ticket.run_id, ticket.attempt_id)):
            with closing(owned.connection.execute("SELECT id FROM operation_attempts WHERE run_id = ? AND attempt_no = ?", parameters)) as cursor:
                return cursor.fetchone()
        found = await self.database.read(read, description="读取拍摄当前责任")
        if found is None:
            raise ConsistencyError("原恢复尝试不存在")
        attempt = (await self.database.read(lambda owned, inputs=("operation_attempts", found[0]): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if attempt["status"] != int(_ATTEMPT_STATUS.RUNNING):
            self.last_recovery_diagnostic = None
            return True
        evidence = (await self._attempt_recovery_evidence(ticket, attempt))
        if evidence is None:
            return False
        # 可靠末页已经承载实际返回；部分页仍与整轮未知结果分开。
        if ticket.operation == "result":
            last = (await self.database.read(lambda owned, inputs=(ticket,), options=dict(): self.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
            if last is not None and _result_page_returned(last):
                _hold_recovered_result_page(self, last, evidence)
                pending = (await _settle_recovered_listing(self, self.pending_start_results[identity]))
            else:
                entries = None if last is None else SavedResultEntries(self.capture, self.database, last.ref)
                action = await self.action(await self.action_id_of_ticket(ticket))
                recovered_type = "local_result_recovery" if is_hasselblad_photo(action) else "adb_foreground_recovery"
                pending = PendingCallResult(new_operation_key(), AttemptFinish(
                    ticket, validate_outcome(ticket, _recovered_call_outcome(recovered_type), evidence), self.wall_us(),
                    retry_wait=retry_wait),
                    None, returned_ns=self.monotonic_ns(), result_listing=entries)
                pending = (await _settle_recovered_listing(self, pending))
            self.pending_start_results[identity] = pending
            (await self._save_call_result(identity, pending))
        else:
            action = (await self.action((await self.action_id_of_ticket(ticket))))
            recovered = _recovered_call_outcome("local_control_recovery"
                if ticket.operation == "control" and is_hasselblad_photo(action)
                else "adb_foreground_recovery")
            finite_cancel = (finite_cancel_policy(action) and action["cancel_requested"]
                             and ticket.operation in ("stop", "control"))
            if finite_cancel:
                pending = PendingCallResult(new_operation_key(), AttemptFinish(ticket,
                    validate_outcome(ticket, recovered, evidence), self.wall_us(),
                    run_finish=RunFinish(RunOutcome.FAILED if ticket.operation == "stop"
                        else RunOutcome.UNCONFIRMED, recovered.error)),
                    None, returned_ns=self.monotonic_ns())
                self.pending_start_results[identity] = pending
                (await self._save_call_result(identity, pending))
                self.last_recovery_diagnostic = None
                return True
            if ticket.operation == "control" and is_hasselblad_photo(action):
                pending = PendingCallResult(new_operation_key(), AttemptFinish(ticket,
                    validate_outcome(ticket, recovered, evidence), self.wall_us(),
                    retry_wait=retry_wait), None, returned_ns=self.monotonic_ns())
                self.pending_start_results[identity] = pending
                (await self._save_call_result(identity, pending))
                self.last_recovery_diagnostic = None
                return True
            _save_inputs = (AttemptFinish(ticket, validate_outcome(ticket, recovered, evidence), self.wall_us(),
                              retry_wait=retry_wait), new_operation_key())
            _save_options = dict()
            receipt = (await self.database.write(lambda owned: self.operations.finish_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"原调用恢复结果未可靠保存: {receipt.error}", receipt)
        self.last_recovery_diagnostic = None
        return True

    async def _attempt_recovery_evidence(self, ticket: AttemptTicket, attempt=None):
        """核对原意图与旧会话收场资格，不保存或推导设备结果。"""
        if ticket.operation == "result":
            from camctl.host_files.downloads import download_owned
            if download_owned(ticket):
                return None
        if attempt is None:
            def read(owned, parameters=(ticket.run_id, ticket.attempt_id)):
                with closing(owned.connection.execute("SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?", parameters)) as cursor:
                    return cursor.fetchone()
            found = await self.database.read(read, description="读取拍摄当前责任")
            if found is None:
                raise ConsistencyError("原恢复尝试不存在")
            attempt = (await self.database.read(lambda owned, inputs=("operation_attempts", found[0]): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        def blocked_reason(*, active_local_control=False, evidence_available=True):
            return recovery_block_reason(self.recovery_boundary, self.recovery_max_event_id,
                attempt["intent_event_id"], evidence_lookup_available=self.recovery_evidence_for is not None,
                active_local_control=active_local_control, evidence_available=evidence_available)

        blocked = blocked_reason()
        if blocked is not None:
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                blocked, ticket.run_id, ticket.attempt_id))
            return None
        run = (await self.database.read(lambda owned, inputs=("operation_runs", ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if run is None:
            raise ConsistencyError("原恢复尝试缺少流程")
        owner_id = run["action_id"]
        local = self.start_entries.get(self.database.metadata.instance_id, owner_id)
        blocked = blocked_reason(active_local_control=ticket.operation == "control" and local is not None and local.active)
        if blocked is not None:
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                blocked, ticket.run_id, ticket.attempt_id))
            return None
        if (run["kind"] in (int(_RUN_KIND.STOP_RESIDUAL), int(_RUN_KIND.EMERGENCY_STOP))
                or (run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
                    and run["query_purpose"] == int(_QUERY_PURPOSE.RESIDUAL_STOP_CONFIRMATION))):
            activity = (await self.database.read(lambda owned, inputs=("device_activities", run["activity_id"]): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
            if activity is None:
                raise ConsistencyError("原停止恢复责任缺少活动")
            owner_id = activity["action_id"]
        evidence = self.recovery_evidence_for(_binding((await self.action(owner_id))), ticket.operation)
        blocked = blocked_reason(evidence_available=evidence is not None)
        if blocked is not None:
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                blocked, ticket.run_id, ticket.attempt_id))
            return None
        self.last_recovery_diagnostic = None
        return evidence


def _query_confirmed_idle(connection, action_id: int, activity_id: int) -> bool:
    """停止核实或活动核实是否已可靠确认设备不再执行本活动。

    只有成功终态且最近观察没有进行中活动的核实流程构成停止事
    实；归属确认（仍在录观察）或未确认终态不构成。
    """
    for purpose in ("stop", "activity"):
        responsibility = f"query/{purpose}/{action_id}/{activity_id}"
        with closing(connection.execute(
            "SELECT r.status, (SELECT a.result_json FROM operation_attempts a"
            " WHERE a.run_id = r.id ORDER BY a.id DESC LIMIT 1)"
            " FROM operation_runs r WHERE r.responsibility_key = ?",
            (responsibility,),
        )) as cursor:
            row = cursor.fetchone()
        if row is None or int(row[0]) != 3 or not isinstance(row[1], str):
            continue
        observations = json.loads(row[1]).get("observations")
        if observations == []:
            return True
    return False


class SessionRecordingState:
    """录像中段事实的生产装载器。

    启动确认与停止确认取自 start/stop 责任的最近尝试；停止确认也
    接受停止核实或活动核实的可靠空闲确认；停止次数与在途取自停
    止流程行，上限取本次运行的停止配置。计时锚点是本进程会话的
    单调钟读数（规格：重启后旧读数不能与新会话组合，须先对账），
    由装配层在启动确认后登记；未登记时按跨会话处理进入对账分区。
    推进循环每轮重建运行时时，装配层传入同一会话共享的锚点表，
    锚点跨轮保留。
    """

    def __init__(self, runtime: "CaptureRuntime",
                 anchors: dict[int, tuple[int, int]] | None = None) -> None:
        self._runtime = runtime
        self._anchors: dict[int, tuple[int, int]] = (
            {} if anchors is None else anchors)

    def anchor_confirmed(self, action_id: int, anchor_ns: int,
                         stop_target_ns: int) -> None:
        self._anchors[action_id] = (anchor_ns, stop_target_ns)
        if self._runtime.on_recording_deadline is not None:
            self._runtime.on_recording_deadline(action_id, stop_target_ns)

    def has_session_anchor(self, action_id: int) -> bool:
        """本会话是否持有该动作的启动确认锚点。"""
        return action_id in self._anchors

    async def recording_state(self, action_id: int) -> RecordingState:
        runtime = self._runtime
        action = (await runtime.action(action_id))
        start = (await runtime.last_attempt(f"start/{action_id}"))
        started = (start is not None
                   and start[1] == int(_EFFECT_STATE.CONFIRMED))
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(runtime, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        started = started or activity["started_at"] is not None
        def read(owned, parameters=(f"stop/{action_id}",)):
            with closing(owned.connection.execute("SELECT r.id, r.attempts_used, r.max_attempts_used,"
            " (SELECT a.status FROM operation_attempts a WHERE a.run_id = r.id"
            "  ORDER BY a.id DESC LIMIT 1),"
            " (SELECT a.effect_state FROM operation_attempts a WHERE a.run_id = r.id"
            "  ORDER BY a.id DESC LIMIT 1)"
            " FROM operation_runs r WHERE r.responsibility_key = ?", parameters)) as cursor:
                return cursor.fetchone()
        stop_run = await runtime.database.read(read, description="读取拍摄当前责任")
        used, maximum, in_flight = 0, runtime.stop_config.max_attempts, False
        stop_confirmed = False
        if stop_run is not None:
            used = int(stop_run[1])
            in_flight = stop_run[3] == int(_ATTEMPT_STATUS.RUNNING)
            stop_confirmed = (
                stop_run[3] != int(_ATTEMPT_STATUS.RUNNING)
                and stop_run[4] == int(_EFFECT_STATE.CONFIRMED))
        if not stop_confirmed:
            # 停止核实或活动核实的可靠空闲确认同样证明停止责任完成；
            # 归属确认（仍在录观察）不构成停止事实。
            stop_confirmed = (await runtime.database.read(lambda owned, inputs=(action_id, int(activity["id"])): _query_confirmed_idle(owned.connection, *inputs), description="读取拍摄直接事实"))
        anchor = self._anchors.get(action_id)
        stop_target = anchor[1] if anchor is not None else None
        file_complete = stop_confirmed
        if "file_completion_wait_ms" in action["execution_spec_json"]:
            completion = (await _record_file_completion_context(runtime, action_id))
            file_complete = (stop_confirmed and completion is not None
                             and completion["prior_completion"] is not None)
        return RecordingState(
            action_terminal=action["status"] in _ACTION_TERMINAL,
            started_confirmed=started,
            anchor_from_current_session=anchor is not None,
            stop_target_ns=stop_target,
            monotonic_now_ns=self._runtime.monotonic_ns(),
            stop_confirmed=stop_confirmed,
            file_complete_guaranteed=file_complete,
            stop_attempts_used=used,
            stop_max_attempts=maximum,
            stop_in_flight=in_flight,
        )


def _binding(action: Mapping[str, Any]) -> DeviceBinding:
    return DeviceBinding(device_id=action["device_id"], driver_id=action["driver_id"])


def _operation_outcome(result: DeviceCallResult, confirmed_observation: str,
                       evidence_type: str = "operation_returned"):
    """按契约调用结果构造尝试结局：可靠观察与调用错误并存。"""
    confirmed = any(
        observation.type == confirmed_observation
        for observation in result.observations
    )
    if result.outcome is not None:
        return result.outcome, confirmed
    error = None
    if result.error is not None:
        code = result.error.get("code") if isinstance(result.error, Mapping) else None
        error = ErrorValue(
            code=code if isinstance(code, str) and code else "device_error",
            stage="device")
        status = AttemptStatus.FAILED
        effect = EffectState.CONFIRMED if confirmed else EffectState.UNKNOWN
    elif confirmed:
        status, effect = AttemptStatus.SUCCEEDED, EffectState.CONFIRMED
    else:
        status, effect = AttemptStatus.SUCCEEDED, EffectState.UNKNOWN
    outcome = CallOutcome(
        status=status,
        error=error,
        effect=effect,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type=evidence_type, version=1, data={}),
        ),
        observations=result.observations,
    )
    return outcome, confirmed


async def _save_activity(runtime: CaptureRuntime, action_id: int, **facts) -> None:
    """把发送或启动观察交活动观察边界落库；被拒按一致性错误上抛。"""
    _save_inputs = (ActivityObservationSave(
            action_id=action_id, occurred_at=runtime.wall_us(), **facts), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.save_activity_observation(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)


def _preflight_capture_timing(runtime, action):
    """在任何物理 START 前核对完整等待与持久时钟范围。"""
    spec = action["execution_spec_json"]
    duration = spec.get("target_duration_ms")
    if duration is None:
        return
    extra = 0
    if action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE) and spec["wait_after_send"]:
        extra = runtime.wait_config(action).extra_wait_ms
    timing = CaptureTiming(duration, spec.get("startup_margin_ms", 0),
        spec.get("result_wait_margin_ms", 0), extra)
    # 包括启动调用最多还可能占用的时间，响应返回后不再出现新溢出。
    timeout = (runtime.start_config.timeout_s if action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
               else _start_call_timeout(action))
    timeout_us = (Fraction(timeout) * 1000000).__ceil__()
    timing.deadline_us(runtime.wall_us() + timeout_us)
    timing.deadline_ns(runtime.monotonic_ns() + timeout_us * 1000)


async def _capture_dispatch_check(runtime, action_id):
    """每次新设备派发前重新读取当前取消和原动作窗口。"""
    from camctl.scheduling.resources import recheck_dispatch

    current = (await runtime.action(action_id))
    _preflight_capture_timing(runtime, current)
    now = runtime.wall_us()
    if current["cancel_requested"]:
        return "canceled"
    if now < current["scheduled_at"]:
        return "before_start"
    return recheck_dispatch(canceled=bool(current["cancel_requested"]),
        window=runtime.window_of(current), trusted_wall_now=now).reason


def _prepared_start_ticket(runtime, action):
    if (getattr(runtime, "capture_preparation_supported", False) is not True
            or action["type"] not in CAPTURE_ACTION_TYPES):
        return None
    from camctl.capture.preparation import prepared_by
    ticket = prepared_by(runtime, action)
    if ticket is None:
        raise ConsistencyError("实际 START 缺少本会话可靠且匹配的准备凭据")
    return ticket


async def _refresh_photo_cancel_origins(runtime, permit):
    """只保留仍在固定目标的取消发起者；新的并发申请不会被清除。"""
    origins = tuple(permit.cancel_origins)
    if not origins:
        return
    def read(owned):
        inactive = []
        for origin in origins:
            with closing(owned.connection.execute(
                    "SELECT type,status,cancel_requested,target_selection_state FROM actions WHERE id=?",
                    (origin,))) as cursor:
                row = cursor.fetchone()
            if (row is None or row[0] != 6 or row[1] not in (1, 2, 3, 4, 5, 6)
                    or row[2] not in (0, 1) or row[3] not in (1, 2, 3)):
                raise ConsistencyError("START 等待的取消发起者缺失或类型不符")
            if row[1] not in (1, 2) or row[2] or row[3] != 1:
                inactive.append(origin)
        return tuple(inactive)
    inactive = await runtime.database.read(read, description="读取 START 等待的取消发起者")
    permit.cancel_origins.difference_update(inactive)


async def _expire_unentered_photo(runtime, action, ticket, permit):
    """原票据未进入驱动时，按实际窗口保存未派发与动作过期。"""
    from camctl.devices.start_entry import PREVENTED
    if permit.phase is not EntryPhase.READY or permit.ticket != ticket or permit.active:
        raise ConsistencyError("过期单张缺少原未进入许可")
    permit.phase = EntryPhase.BLOCKED
    now = runtime.wall_us()
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue("window_ended", "dispatch"),
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue.of(PREVENTED)))
    await runtime.finish(ticket, outcome, end_run=RunOutcome.EXPIRED,
        activity=ActivityObservationSave(action["id"], now, dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED)),
        expiration=ExpireActionRequest(action["id"], now, now), occurred_at=now,
        returned_ns=runtime.monotonic_ns())


def _require_result_port(runtime, message: str) -> None:
    """只在旧拍摄已通过本轮启动资格检查后拒绝缺少结果端口。"""
    if runtime.results is None:
        raise CapabilityNotDeclaredError(message)


@owned_tool_call
async def _control_call(runtime: CaptureRuntime, action, operation: str,
                        confirmed_observation: str,
                        activity_facts=None) -> HandlerOutcome:
    """授予启动机会后发起一次设备控制调用并保存尝试与活动观察。

    调用错误或可靠确认效果时同时结束启动流程（额度一次用尽）；
    未确认的发送保持流程执行中，等待后续效果核实。驱动结果不符
    合登记契约时不可采纳：按调用失败收场本次尝试，不遗留执行中
    的启动流程。
    """
    if operation == "start_recording":
        return await _record_start_once(runtime, action)
    from camctl.capture.host_photo import is_saved_capture_photo

    direct_photo = is_saved_capture_photo(action)
    if not direct_photo and (await runtime.last_attempt(f"start/{action['id']}")) is None:
        from camctl.capture.residual import pass_residual_gate

        if not await pass_residual_gate(runtime, action):
            return HandlerOutcome("not_granted")
    if not direct_photo:
        _require_result_port(runtime,
            "旧拍摄启动与后续产物核实需要驱动声明 result 能力")
        _preflight_capture_timing(runtime, action)
        prepared = await _baseline_before_control(runtime, action)
        if prepared is not None:
            return prepared
    prepared_ticket = _prepared_start_ticket(runtime, action)
    ticket, reason = (await runtime.grant(action))
    if ticket is None:
        return HandlerOutcome("not_granted", reason)
    permit = (runtime.start_entries.get(runtime.database.metadata.instance_id, action["id"])
              if is_hasselblad_photo(action) else None)
    if is_hasselblad_photo(action):
        if permit is None or permit.ticket != ticket:
            raise ConsistencyError("原 START 缺少匹配的会话入口许可，不能调用驱动")
        await _refresh_photo_cancel_origins(runtime, permit)
        if permit.cancel_origins or permit.phase in (EntryPhase.RESERVED, EntryPhase.BLOCKED):
            return HandlerOutcome("not_granted", "canceled")
        if permit.phase is not EntryPhase.READY:
            raise ConsistencyError("原 START 入口许可尚未核实或已使用，不能调用驱动")
        from camctl.persistence.repositories.preparation import recheck_start_availability
        blocked = await runtime.database.read(lambda owned: recheck_start_availability(owned, ticket),
                                              description="复核启动设备检查占用")
        if permit.cancel_origins or permit.phase is not EntryPhase.READY:
            return HandlerOutcome("not_granted", "canceled")
        if blocked is not None:
            from camctl.devices.start_entry import PREVENTED
            permit.phase = EntryPhase.BLOCKED
            occurred = runtime.wall_us()
            outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                error=ErrorValue("device_error", "dispatch", {"reason": blocked}),
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue.of(PREVENTED)))
            await runtime.finish(ticket, outcome, end_run=RunOutcome.FAILED, run_error=outcome.error,
                activity=ActivityObservationSave(action["id"], occurred,
                    dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED)),
                occurred_at=occurred, returned_ns=runtime.monotonic_ns())
            return HandlerOutcome("call_failed", "device_error")
        reason = await _capture_dispatch_check(runtime, action["id"])
        if permit.cancel_origins or permit.phase is not EntryPhase.READY:
            return HandlerOutcome("not_granted", "canceled")
        if reason == "window_ended":
            await _expire_unentered_photo(runtime, action, ticket, permit)
            return HandlerOutcome("not_granted", reason)
        if reason is not None:
            return HandlerOutcome("not_granted", reason)
    try:
        with measure_stage("capture.start", action_id=action["id"],
                device_id=action["device_id"], activity_id=ticket.target_id,
                run_id=ticket.run_id, attempt_id=ticket.attempt_id, operation=operation):
            result = await runtime.driver.control(ControlRequest(
                operation=operation,
                binding=_binding(action),
                params=action["effective_params_json"],
                ticket=ticket, timeout_s=_start_call_timeout(action),
                dispatch_check=lambda: _capture_dispatch_check(runtime, action["id"]),
                start_entry=permit,
                prepared_by=prepared_ticket,
                output_storage=(action["execution_spec_json"]["output_storage"]
                                if is_hasselblad_photo(action) else None),
            ))
            returned_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
    finally:
        if permit is not None:
            permit.active = False
    outcome, confirmed = _operation_outcome(result, confirmed_observation)
    if result.outcome is not None:
        confirmed = outcome.effect is EffectState.CONFIRMED
    # 返回时取得事实时间；结果事务和日志耗时不能改变设备返回锚点。
    captured_facts = None
    anchor = None
    if (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            and outcome.status is AttemptStatus.SUCCEEDED and outcome.error is None and confirmed):
        meaning = action["execution_spec_json"]["start_return_meaning"]
        captured_facts = {}
        if meaning == int(StartReturn.SENT):
            captured_facts["sent_at"] = returned_at
            if (action["execution_spec_json"]["wait_after_send"]
                    or action["execution_spec_json"]["end_control"] == int(EndControl.HOST_TIMER)):
                anchor = returned_ns
        elif meaning == int(StartReturn.STARTED):
            captured_facts.update(started_at=returned_at, activity_state=int(_ACTIVITY_STATE.ACTIVE))
            if action["execution_spec_json"]["end_control"] == int(EndControl.HOST_TIMER):
                anchor = returned_ns
    elif activity_facts is not None and result.error is None:
        captured_facts = (activity_facts(confirmed) if callable(activity_facts)
                          else activity_facts)
    observation = None if captured_facts is None else ActivityObservationSave(
        action_id=action["id"], occurred_at=returned_at,
        dispatch_state=int(_DISPATCH_STATE.SUCCESS_RETURNED), **captured_facts)
    if outcome.effect is EffectState.NO_EFFECT:
        observation = ActivityObservationSave(action["id"], returned_at,
            dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED
                if outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
                else _DISPATCH_STATE.REJECTED_WITHOUT_EFFECT))
    try:
        if result.error is not None:
            (await runtime.finish(
                ticket, outcome, end_run=RunOutcome.FAILED,
                diagnostics_check=result.diagnostics_check,
                run_error=outcome.error, activity=observation,
                occurred_at=returned_at, returned_ns=returned_ns))
        elif confirmed:
            (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                diagnostics_check=result.diagnostics_check,
                activity=observation, occurred_at=returned_at, returned_ns=returned_ns,
                confirmation_anchor_ns=anchor))
        else:
            (await runtime.finish(ticket, outcome, activity=observation,
                diagnostics_check=result.diagnostics_check,
                occurred_at=returned_at, returned_ns=returned_ns))
    except OutcomeValidationError:
        # 观察与收场依据不符合登记契约：该结果整体不可采纳，不落
        # 库、不保存派发成功事实，按调用失败保存尝试终局。
        rejected, _ = _operation_outcome(
            DeviceCallResult(observations=(), error={
                "code": "invalid_device_result",
                "message": "驱动结果不符合登记契约"}),
            confirmed_observation)
        (await runtime.finish(
            ticket, rejected, end_run=RunOutcome.FAILED,
            run_error=rejected.error, occurred_at=returned_at, returned_ns=returned_ns))
        return HandlerOutcome("call_failed", "invalid_device_result")
    if result.error is not None:
        return HandlerOutcome("call_failed", "device_error")
    return HandlerOutcome("confirmed" if confirmed else "sent")


async def _prepare_recording_start_result(runtime, pending, preparation):
    """用原返回事实形成启动派生请求；保存重送保持该请求。"""
    dispatch = preparation.dispatch
    ticket, outcome = pending.finish.ticket, pending.finish.outcome.outcome
    action_id = (await runtime.action_id_of_ticket(ticket))
    current = (await runtime.action(action_id))
    observation = None
    if dispatch.confirmed or dispatch.sent_only:
        facts = {"dispatch_state": int(_DISPATCH_STATE.SUCCESS_RETURNED)}
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(runtime, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity["sent_at"] is None and (dispatch.confirmed or "startup_margin_ms" not in current["execution_spec_json"]):
            facts["sent_at"] = dispatch.confirmed_at
        if dispatch.confirmed and current["execution_spec_json"].get("start_return_meaning", int(StartReturn.STARTED)) == int(StartReturn.STARTED):
            if activity["started_at"] is None:
                facts["started_at"] = dispatch.confirmed_at
            if activity["activity_state"] != 2:
                facts["activity_state"] = 2
        observation = ActivityObservationSave(
            action_id, dispatch.confirmed_at, **facts)
    elif outcome.effect is EffectState.NO_EFFECT:
        observation = ActivityObservationSave(
            action_id, dispatch.confirmed_at,
            dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED
                if outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
                else _DISPATCH_STATE.REJECTED_WITHOUT_EFFECT))
    failure = None
    expiration = None
    end_run = RunOutcome.SUCCEEDED if dispatch.confirmed else None
    run_error = None
    if outcome.effect is EffectState.NO_EFFECT and not current["cancel_requested"]:
        from camctl.scheduling.rules import WindowPhase, window_phase

        run = (await runtime.database.read(lambda owned, inputs=("operation_runs", ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        phase = window_phase(runtime.window_of(current), pending.finish.occurred_at)
        if phase is WindowPhase.AFTER_WINDOW:
            expiration = ExpireActionRequest(
                action_id, pending.finish.occurred_at, dispatch.confirmed_at)
        elif outcome.error is not None and outcome.error.code == "device_start_failed":
            failure = RecordingFailure("device_start_failed", dict(outcome.error.details))
            end_run = RunOutcome.FAILED
            run_error = outcome.error
        elif (phase is WindowPhase.IN_WINDOW
              and run["attempts_used"] >= preparation.max_attempts):
            failure = RecordingFailure("start_attempts_exhausted", {
                "max_attempts": preparation.max_attempts,
                "attempts_used": run["attempts_used"]})
            end_run = RunOutcome.FAILED
            run_error = ErrorValue(failure.code, "device_start", failure.details)
    return replace(
        pending,
        finish=replace(pending.finish,
            run_finish=None if end_run is None else RunFinish(end_run, run_error),
            retry_wait=(outcome.effect is EffectState.NO_EFFECT
                        and not current["cancel_requested"] and end_run is None
                        and expiration is None)),
        observation=observation, action_failure=failure, expiration=expiration,
        confirmation_anchor_ns=dispatch.anchor_ns,
        canceled_unstarted=(outcome.effect is EffectState.NO_EFFECT
                            and bool(current["cancel_requested"])), preparation=None)


async def _prepare_start_confirmation_result(runtime, pending, preparation):
    """启动核实派生只使用原活动、实际观察时刻与原单调读数。"""
    activity = preparation.activity
    ticket, call = pending.finish.ticket, pending.finish.outcome.outcome
    observed_at, anchor = pending.finish.occurred_at, pending.returned_ns
    action_id = (await runtime.action_id_of_ticket(ticket))
    confirmed = any(observation.type == "activity_status"
                    and observation.data.get("activity_id") == str(activity["id"])
                    for observation in call.observations)
    current = (await runtime.action(action_id))
    def read(owned, parameters=(ticket.run_id,)):
        with closing(owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id = ?", parameters)) as cursor:
            return cursor.fetchone()[0]
    used = await runtime.database.read(read, description="读取拍摄直接事实")
    observation = None
    failure = None
    start_finish = None
    final = None
    run_error = None
    if confirmed:
        new_facts = {}
        if activity["dispatch_state"] != int(_DISPATCH_STATE.SUCCESS_RETURNED):
            new_facts["dispatch_state"] = int(_DISPATCH_STATE.SUCCESS_RETURNED)
        if activity["started_at"] is None:
            new_facts["started_at"] = observed_at
        if activity["activity_state"] != 2:
            new_facts["activity_state"] = 2
        if new_facts:
            observation = ActivityObservationSave(action_id, observed_at, **new_facts)
        final = RunOutcome.SUCCEEDED
        start_finish = StaleRunFinish(responsibility_keys=(f"start/{action_id}",),
                                     status=RunOutcome.SUCCEEDED, occurred_at=observed_at)
    if current["cancel_requested"] or current["status"] in _ACTION_TERMINAL:
        return replace(
            pending,
            finish=replace(pending.finish, run_finish=RunFinish(
                RunOutcome.CANCELED if current["cancel_requested"] else (
                    RunOutcome.SUCCEEDED if confirmed else RunOutcome.UNCONFIRMED))),
            observation=observation, preparation=None)
    if not confirmed and used >= preparation.max_attempts:
        failure = RecordingFailure("capture_result_unconfirmed", {
            "activity_id": str(activity["id"]), "reason": "start_unknown"})
        final = RunOutcome.UNCONFIRMED
        run_error = ErrorValue(failure.code, "execution", failure.details)
        start_finish = StaleRunFinish(responsibility_keys=(f"start/{action_id}",),
                                     status=final, occurred_at=observed_at, error=run_error)
    return replace(
        pending,
        finish=replace(pending.finish,
            run_finish=None if final is None else RunFinish(final, run_error),
            retry_wait=final is None),
        observation=observation, start_finish=start_finish, action_failure=failure,
        confirmation_anchor_ns=anchor if confirmed else None, preparation=None)


async def _prepare_held_start_result(runtime, pending):
    preparation = pending.preparation
    if isinstance(preparation, RecordingStartPreparation):
        return (await _prepare_recording_start_result(runtime, pending, preparation))
    if isinstance(preparation, StartConfirmationPreparation):
        return (await _prepare_start_confirmation_result(runtime, pending, preparation))
    raise ConsistencyError("原调用结果缺少合法的启动准备责任")


@owned_tool_call
async def _baseline_before_control(runtime: CaptureRuntime, action) -> HandlerOutcome | None:
    """拍摄入口共用准备；准备结束后重新检查取消与原窗口。"""
    from camctl.capture.baseline import PreparationPhase
    from camctl.scheduling.rules import WindowPhase, window_phase

    if (getattr(runtime, "capture_preparation_supported", False) is True
            and action["type"] in CAPTURE_ACTION_TYPES):
        from camctl.capture.preparation import advance_preparation
        result = await advance_preparation(runtime, action["id"])
    else:
        result = await runtime.prepare_baseline(action["id"])
    if result.phase is PreparationPhase.WAITING:
        return HandlerOutcome("not_granted", "baseline_pending")
    if result.phase is PreparationPhase.PENDING:
        raise ConsistencyError(f"基准保存未可靠完成: {result.database_error}") from result.database_error
    current = (await runtime.action(action["id"]))
    if current["status"] in _ACTION_TERMINAL:
        (await runtime.resume_baselines(action["id"]))
        return HandlerOutcome("not_granted", "terminal")
    if current["cancel_requested"]:
        (await _finish_canceled_capture(runtime, action["id"], unstarted=True))
        return HandlerOutcome("not_granted", "canceled")
    now = runtime.wall_us()
    if window_phase(runtime.window_of(current), now) is WindowPhase.AFTER_WINDOW:
        (await runtime.save_capture_completion(ExpireActionRequest(action["id"], now, now,
            preparation_resolved=result.phase is not PreparationPhase.FAILED)))
        (await runtime.resume_baselines(action["id"]))
        return HandlerOutcome("not_granted", "window_ended")
    if result.phase is PreparationPhase.FAILED:
        activity_id = (await _activity_id_of(runtime, action["id"]))
        from camctl.devices.tasks import CaptureResultPolicy

        if action["execution_spec_json"].get("result_policy") in {
            policy.value for policy in CaptureResultPolicy
        }:
            from camctl.capture.segment_completion import (
                finish_segmented_baseline_failure,
            )

            (await finish_segmented_baseline_failure(runtime, action["id"]))
        else:
            (await runtime.save_capture_completion(
                FinishCapture(
                    action["id"],
                    (),
                    OutputCatalogFacts(action["id"], True),
                    runtime.wall_us(),
                    RecordingFailure(
                        "capture_failed",
                        {
                            "activity_id": str(activity_id),
                            "reason": "baseline_read_failed",
                        },
                    ),
                    baseline_error=result.error,
                )
            ))
        (await runtime.resume_baselines(action["id"]))
        return HandlerOutcome("call_failed", "baseline_read_failed")
    return None


@owned_tool_call
async def _record_start_once(runtime: CaptureRuntime, action) -> HandlerOutcome:
    """将真实控制端口适配到一次启动骨架，事实时间先于保存。"""
    _preflight_capture_timing(runtime, action)
    if (await runtime.last_attempt(f"start/{action['id']}")) is None:
        from camctl.capture.residual import pass_residual_gate

        if not await pass_residual_gate(runtime, action):
            return HandlerOutcome("not_granted")

    _require_result_port(runtime, "录像启动与后续产物核实需要驱动声明 result 能力")
    prepared = await _baseline_before_control(runtime, action)
    if prepared is not None:
        return prepared

    class Grants:
        async def grant(self, request):
            ticket, reason = (await runtime.grant((await runtime.action(action["id"]))))
            return GrantDecision("granted" if ticket is not None else "rejected",
                                 ticket=ticket, reason=reason)

    class Wall:
        def now_us(self):
            return runtime.wall_us()

    class Driver:
        dispatch = None

        async def start(self, ticket):
            with measure_stage("capture.start", action_id=action["id"],
                    device_id=action["device_id"], activity_id=ticket.target_id,
                    run_id=ticket.run_id, attempt_id=ticket.attempt_id,
                    operation="start_recording"):
                result = await runtime.driver.control(ControlRequest(
                    "start_recording", _binding(action), action["effective_params_json"],
                    ticket=ticket, timeout_s=runtime.start_config.timeout_s,
                    start_return_meaning=action["execution_spec_json"].get("start_return_meaning", int(StartReturn.STARTED)),
                    dispatch_check=lambda: _capture_dispatch_check(runtime, action["id"]),
                    prepared_by=_prepared_start_ticket(runtime, action)))
                received_at, anchor_ns = runtime.wall_us(), runtime.monotonic_ns()
            meaning = action["execution_spec_json"].get("start_return_meaning", int(StartReturn.STARTED))
            outcome, confirmed = _operation_outcome(result,
                "recording_sent" if meaning == int(StartReturn.SENT) else "start_confirmed")
            confirmed = confirmed and outcome.effect is EffectState.CONFIRMED
            self.dispatch = StartDispatch(
                confirmed=confirmed,
                anchor_ns=anchor_ns if confirmed else None,
                confirmed_at=received_at,
                sent_only=(not confirmed and outcome.status is AttemptStatus.SUCCEEDED
                           and outcome.effect is EffectState.UNKNOWN),
                rejected_no_effect=outcome.effect is EffectState.NO_EFFECT,
                error=outcome.error, observations=outcome.observations, outcome=outcome)
            runtime.hold_call_result(
                ticket, outcome, occurred_at=received_at, returned_ns=anchor_ns,
                preparation=RecordingStartPreparation(self.dispatch, runtime.start_config.max_attempts),
                diagnostics_check=result.diagnostics_check)
            return self.dispatch

    driver = Driver()

    class Finishes:
        async def finish(self, ticket, outcome):
            dispatch = driver.dispatch
            assert dispatch is not None
            (await runtime.save_held_result(ticket))

        async def finish_prevented(self, ticket, reason):
            current = (await runtime.action(action["id"]))
            now = runtime.wall_us()
            prevented = CallOutcome(
                status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                error=ErrorValue(reason, "dispatch"),
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                      EvidenceValue("dispatch_prevented", 1, {})))
            (await runtime.finish(ticket, prevented, activity=ActivityObservationSave(
                action["id"], now,
                dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED)), occurred_at=now,
                expiration=None if current["cancel_requested"] else ExpireActionRequest(
                    action["id"], now, now),
                canceled_unstarted=bool(current["cancel_requested"])))

    step = await start_recording(CaptureContext(
        device_id=action["device_id"], action_id=action["id"],
        window=runtime.window_of(action), config=runtime.start_config,
        duration=_target_duration_ms(action), trusted_wall_now=runtime.wall_us(),
        startup_margin_ms=action["execution_spec_json"].get("startup_margin_ms", 0),
        wall=Wall(), grants=Grants(), driver=driver, finishes=Finishes(),
        canceled_now=lambda: runtime.capture_canceled(action["id"])))
    if step.phase in (RecordingPhase.START_CONFIRMED, RecordingPhase.START_CONFIRMED_WITH_ERROR):
        return HandlerOutcome("confirmed")
    if step.phase is RecordingPhase.REJECTED_NO_EFFECT:
        return HandlerOutcome("no_effect")
    return HandlerOutcome(step.phase.value, step.reason)


def validate_observed_pairings(entries: tuple[ObservedFile, ...]) -> None:
    """预览条目的配对必须指向同批的另一条原片条目。

    配对目标缺失、自指或指向另一个预览时无法证明配对可靠，明确
    拒绝整批登记，不猜测关联。
    """
    by_identity = {entry.identity: entry for entry in entries}
    for entry in entries:
        if entry.paired_identity is None:
            continue
        paired = by_identity.get(entry.paired_identity)
        if (entry.paired_identity == entry.identity or paired is None
                or paired.paired_identity is not None):
            raise ConsistencyError(
                "预览配对必须指向同批原片条目: "
                f"{entry.identity!r} -> {entry.paired_identity!r}")


async def _register_observed(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
    *, occurred_at: int | None = None,
    registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None,
    registration: FileRegistration | None = None,
) -> tuple[tuple[CaptureFile, int], ...]:
    """把结果列举观察落库：发现、任务归属与完成事实一次登记。

    预览条目按驱动配对关联登记预览角色并保留配对证据；配对由
    validate_observed_pairings 先行校验。
    """
    # 第一阶段按列举顺序保存发现与在场事实，并建立身份到文件主
    # 键的映射供配对解析。
    registration = FileRegistration() if registration is None else registration
    saved_files = {} if registered_files is None else registered_files
    shared = next((pending.registration_shared
        for entry in entries
        if (pending := runtime.pending_file_observations.get((action_id, entry.identity))) is not None), SharedSave())
    for entry in entries:
        if entry.identity in saved_files:
            continue
        occurred = runtime.wall_us() if occurred_at is None else occurred_at
        identity = (action_id, entry.identity)
        command = FileObservationSave(
                observer_action_id=action_id,
                file_identity=entry.identity,
                locator=entry.locator,
                occurred_at=occurred,
                original_name=entry.original_name,
                media_type=entry.media_type,
            )
        pending = runtime.pending_file_observations.get(identity)
        if pending is None:
            runtime.pending_file_observations[identity] = PendingFileObservation(
                command, new_operation_key(), entries, saved_files, registration=registration,
                registration_shared=shared)
        elif (not _same_file_request(pending.command, command)
                or not _same_observed_files(pending.entries, entries)
                or pending.registered_files != saved_files or pending.registration != registration):
            raise ConsistencyError("待存文件发现的原完整请求不可替换")
    shared.retry_failed()
    return await shared.run(lambda: _apply_observed(runtime, action_id, entries, saved_files, registration))


async def _register_photo_result_page(runtime, action_id: int, page_ref: ResultPageRef) -> None:
    """先消费可靠角色页的文件子责任，再允许同轮读取下一角色。"""
    from camctl.capture.files import file_identity_key
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, DownloadVariant

    action = await runtime.action(action_id)
    if not is_hasselblad_photo(action) or action["execution_spec_json"]["output_storage"] != "device":
        raise ConsistencyError("照片角色页不属于冻结的 DEVICE 单张动作")
    saved = await runtime.database.read(
        lambda owned: runtime.capture.read_result_page(page_ref, owned), description="读取已保存照片角色页")
    observations = [item for item in saved.page.outcome.observations
                    if item.type == "result_files_listed" and item.version == 3]
    if not observations:
        return
    if len(observations) != 1 or len(saved.page.entries) != 1 or len(saved.file_ids) != 1:
        raise ConsistencyError("照片角色页缺少唯一原文件及可靠引用")
    entry = saved.page.entries[0]
    identity, file_id = saved.file_ids[0]
    if identity != entry.identity:
        raise ConsistencyError("照片角色页的文件引用改变原身份")
    source = CameraFileIdentity.from_source(
        file_identity_key(action["device_id"], action["driver_id"], identity), entry.locator)
    if source.variant is DownloadVariant.THUMBNAIL and entry.paired_identity is None:
        # 没有原片时保留实际页和发现事实，不虚构独立预览配对。
        return
    pairing = {}
    if entry.paired_identity is not None:
        async for original, file, original_id in SourceResultEntries(
                runtime.capture, runtime.database, page_ref.ticket).records():
            if original.identity == entry.paired_identity:
                if not file.complete:
                    raise ConsistencyError("照片预览配对的原片尚未可靠完成")
                pairing[original.identity] = (original, original_id)
                break
        if not pairing:
            raise ConsistencyError("照片预览缺少原可靠角色来源")
    def read_start_event(owned):
        with closing(owned.connection.execute(
                "SELECT a.result_event_id FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
                " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 2",
                (f"start/{action_id}",))) as reader:
            rows = reader.fetchall()
        if len(rows) != 1 or rows[0][0] is None:
            raise ConsistencyError("照片角色文件缺少唯一原 START 结果引用")
        return rows[0][0]
    start_event = await runtime.database.read(read_start_event, description="读取照片原 START 结果引用")
    entry = replace(entry, evidence={**entry.evidence, "photo_source": {
        "start_result_event_id": start_event, "result_page_event_id": page_ref.event_id,
        "role": source.variant.value}})
    registration = FileRegistration(pairing_files=pairing,
        ownership_method=int(enum_for("device_files.ownership_evidence_json.method").DRIVER_TASK_ASSOCIATION),
        captured_checksums={identity: source.sha256})
    original = ResultPageFileInput(page_ref, file_id, identity, saved.occurred_at)
    key = (action_id, identity)
    pending = runtime.pending_file_observations.get(key)
    if pending is None:
        pending = PendingFileObservation(original, new_operation_key(), (entry,), {},
            response=ResultPageFileRef(file_id), registration=registration)
        runtime.pending_file_observations[key] = pending
    elif (not _same_file_request(pending.command, original)
            or not _same_observed_files(pending.entries, (entry,))
            or pending.registration != registration):
        raise ConsistencyError("照片结果页文件子责任的原申请不可替换")
    pending.registration_shared.retry_failed()
    await pending.registration_shared.run(lambda: _apply_observed(
        runtime, action_id, pending.entries, pending.registered_files, pending.registration))


async def _resume_saved_photo_sources(runtime, action_id, ticket):
    """先接回所有原角色页的子责任，不能以未完成状态授权再次下载。"""
    action = await runtime.action(action_id)
    if not is_hasselblad_photo(action) or action["execution_spec_json"].get("output_storage") != "device":
        return
    from camctl.capture.photo_result_recovery import saved_photo_page_refs
    refs = await runtime.database.read(lambda owned: saved_photo_page_refs(owned.connection,
        action_id=action_id, activity_id=int(ticket.target_id), run_id=ticket.run_id),
        description="读取原照片角色页子责任")
    for ref in refs:
        await _register_photo_result_page(runtime, action_id, ref)


async def _apply_observed(runtime, action_id, entries, saved_files, registration):
    """沿原发现请求完成整批派生事实与调用结果应用。"""
    file_ids = {identity: item[1] for identity, item in registration.pairing_files.items()}
    await runtime._save_file_observations(action_id)
    for entry in entries:
        if entry.identity in saved_files:
            file_ids[entry.identity] = saved_files[entry.identity][1]
            continue
        identity = (action_id, entry.identity)
        occurred = runtime.pending_file_observations[identity].command.occurred_at
        observed = runtime.pending_file_observations[identity].response
        file_ids[entry.identity] = observed.file_id
        # 原 presence 申请必须先核实；没有原申请时才判断是否需要状态变化。
        pending = runtime.pending_file_observations[identity]
        if (FileFactStage.PRESENCE in pending.facts
                or (await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", observed.file_id)))["presence_state"]
                   != int(_FILE_PRESENCE.PRESENT)):
            await runtime._save_file_fact(identity, FileFactStage.PRESENCE, FilePresenceSave(
                file_id=observed.file_id, state=int(_FILE_PRESENCE.PRESENT), occurred_at=occurred))
    validate_observed_pairings(tuple(entries) + tuple(item[0] for item in registration.pairing_files.values()
                                                    if item[0].identity not in {entry.identity for entry in entries}))
    # 第二阶段保存归属与完成事实。
    registered: list[tuple[CaptureFile, int]] = []
    for entry in entries:
        if entry.identity in saved_files:
            registered.append(saved_files[entry.identity])
            continue
        identity = (action_id, entry.identity)
        occurred = runtime.pending_file_observations[identity].command.occurred_at
        file_id = file_ids[entry.identity]
        if entry.paired_identity is None:
            ownership = OwnershipSave(
                    file_id=file_id,
                    source_action_id=action_id,
                    method=registration.ownership_method,
                    activity_id=registration.activity_id,
                    role=_ORIGINAL_ROLE,
                    observation=entry.evidence,
                    occurred_at=occurred,
                )
        else:
            ownership = OwnershipSave(
                    file_id=file_id,
                    source_action_id=action_id,
                    method=registration.ownership_method,
                    activity_id=registration.activity_id,
                    role=_PREVIEW_ROLE,
                    observation=entry.evidence,
                    occurred_at=occurred,
                    paired_device_file_id=file_ids[entry.paired_identity],
                    pairing_observation={
                        "paired_identity": entry.paired_identity},
                )
        await runtime._save_file_fact(identity, FileFactStage.OWNERSHIP, ownership)
        known = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", file_id))
        pending = runtime.pending_file_observations[identity]
        if entry.complete and (FileFactStage.COMPLETION in pending.facts or known["completion_state"] != 3):
            await runtime._save_file_fact(identity, FileFactStage.COMPLETION, FileCompletionSave(
                    file_id=file_id,
                    state=3,
                    occurred_at=occurred,
                    basis=registration.completion_basis,
                    activity_id=registration.completion_activity_id,
                    wait_completed_event_id=registration.wait_completed_event_id,
                    result_page_event_id=registration.result_page_event_id,
                    stop_result_event_id=registration.stop_result_event_id,
                    start_result_event_id=registration.start_result_event_id,
                    observation=entry.evidence,
                    size_bytes=entry.size_bytes,
                ))
        if entry.identity in registration.captured_checksums:
            digest = registration.captured_checksums[entry.identity]
            pending = runtime.pending_file_observations[identity]
            facts = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", file_id))
            if (FileFactStage.CHECKSUM in pending.facts
                    or facts["checksum_support"] == int(_FILE_CHECKSUM.UNDETERMINED)):
                await runtime._save_file_fact(identity, FileFactStage.CHECKSUM, FileChecksumSave(
                    file_id, int(_FILE_CHECKSUM.SUPPORTED), occurred, sha256=digest))
            elif facts["checksum_support"] != int(_FILE_CHECKSUM.SUPPORTED) or facts["sha256"] != digest:
                raise ConsistencyError("捕获源摘要与已经固定的设备文件事实不一致")
        fixed = await runtime.database.read(lambda owned: row_facts(owned.connection, "device_files", file_id))
        complete = (entry.complete and fixed["completion_state"] == 3
                    and entry.size_bytes == fixed["size_bytes"])
        registered.append((
            CaptureFile(
                file_id=entry.identity, kind=entry.kind, complete=complete,
                ownership_confirmed=True, format_id=entry.format_id,
                pairing_confirmed=True if entry.paired_identity is not None else None,
                size_bytes=entry.size_bytes if complete else None),
            file_id,
        ))
    result_files = {file.file_id: (file, file_id) for file, file_id in registered}
    for identity, pending in tuple(runtime.pending_start_results.items()):
        if (not isinstance(pending.result_listing, SavedResultEntries)
                and _same_observed_files(pending.result_listing, entries)
                and await runtime.action_id_of_ticket(pending.finish.ticket) == action_id):
            runtime.pending_start_results[identity] = replace(pending, result_registered_files=result_files)
    for entry in entries:
        runtime.pending_file_observations.pop((action_id, entry.identity), None)
    return tuple(registered)


async def _conclude_activity(runtime: CaptureRuntime, action_id: int) -> None:
    """成功链收场活动：结束观察与占用释放同事务，幂等可重入。"""
    _save_inputs = (ActivityConcludeSave(
            action_id=action_id, occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.conclude_activity(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"活动收场事务未完成（{receipt.kind.value}）: {receipt.error}", receipt)


async def _settle_open_start(runtime: CaptureRuntime, action_id: int, status: RunOutcome,
                       error: ErrorValue | None = None) -> None:
    """动作终态后收场仍开放的启动流程，幂等可重入。

    启动尝试在途（进程中断或结果不可保存）时流程行保持执行中，
    但动作终态后不再有后续启动尝试；仍开放的流程按动作的最终
    结果结束并清除重试等待，否则会话的流程收尾计数无法归零。
    """
    _save_inputs = (StaleRunFinish(
            responsibility_keys=(f"start/{action_id}",),
            status=status,
            error=error,
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.operations.finish_stale_runs(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)


async def _settle_start_without_sent_at(
        runtime: CaptureRuntime, action, attempt) -> bool:
    """可能派发但没有可靠发送时间的启动按无法核实收场。

    启动尝试已存在而活动没有发送时间，说明调用可能在途或结果未
    保存：录像与延时的时间及产物完成判定都依赖可靠发送时间，无
    法核实原任务。按恢复规则不重复启动、不补造时间——调用已有
    明确失败结果时按设备失败收场，否则按无法确认收场；活动占用
    保持未知，不伪造释放依据。已收场返回 True。
    """
    activity_id = (await _activity_id_of(runtime, action["id"]))
    def read(owned, parameters=(activity_id,)):
        with closing(owned.connection.execute("SELECT sent_at FROM device_activities WHERE id = ?", parameters)) as cursor:
            return cursor.fetchone()[0]
    sent_at = await runtime.database.read(read, description="读取拍摄直接事实")
    if sent_at is not None:
        return False
    if attempt[0] == int(_ATTEMPT_STATUS.FAILED):
        (await _finish_capture(
            runtime, action["id"], (), FileKind.VIDEO,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(activity_id),
                         "reason": "device_failed"})))
        return True
    (await _settle_open_start(
        runtime, action["id"], RunOutcome.UNCONFIRMED,
        error=ErrorValue(code="result_unconfirmed", stage="device")))
    (await _finish_capture(
        runtime, action["id"], (), FileKind.VIDEO,
        failure=RecordingFailure(
            code="capture_result_unconfirmed",
            details={"activity_id": str(activity_id),
                     "reason": "start_unknown"})))
    return True


async def _stop_run_id(runtime: CaptureRuntime, action_id: int) -> int:
    def read(owned):
        """读取动作停止流程的主键；预算耗尽收场前流程必然存在。"""
        with closing(owned.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (f"stop/{action_id}",),
        )) as cursor:
            found = cursor.fetchone()
        if found is None:
            raise LookupError(f"停止流程不存在: {action_id}")
        return int(found[0])
    return await runtime.database.read(read, description="读取拍摄当前责任")


async def _close_stop_exhausted(
    runtime: CaptureRuntime, action, *, finish_action: bool = False,
) -> RecordingFailure:
    """保存停止预算耗尽的原流程结果；活动效果及占用保持原事实。"""
    activity_id = (await _activity_id_of(runtime, action["id"]))
    error = ErrorValue(
        code="recording_stop_failed", stage="device_stop",
        details={"activity_id": str(activity_id),
                 "operation_run_id": str((await _stop_run_id(runtime, action["id"])))})
    occurred_at = runtime.wall_us()
    failure = RecordingFailure(code=error.code, details=dict(error.details))
    action_finish = (FinishCapture(
        action["id"], (), OutputCatalogFacts(action["id"], ownership_confirmed=True),
        occurred_at, failure=failure) if finish_action else None)
    (await runtime.save_capture_completion(StopCloseRequest(
        action["id"], StaleRunFinish(
            responsibility_keys=(f"stop/{action['id']}",),
            status=RunOutcome.UNCONFIRMED, error=error, occurred_at=occurred_at),
        action_finish, new_operation_key() if action_finish is not None else None)))
    return failure


async def _settle_stop_exhausted(runtime: CaptureRuntime, action) -> None:
    """停止预算耗尽的收场：停止流程与动作按未确认失败终态化。

    原停止预算内没有取得可靠停止确认时，停止流程按未确认收场并
    携带 recording_stop_failed，动作以零产物登记失败终态。设备活
    动缺少结束与释放依据，执行中事实与占用原样保留，由后续动作
    触发的残留收场或取消收场处理（camera-recovery.md#停止预算
    耗尽后的收场责任）。幂等：动作终态后重入走处理器终态分支。
    """
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        action["execution_spec_json"].get("result_policy")
        in {
            CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
            CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
        }
    ):
        failure = (await _close_stop_exhausted(runtime, action))
        from camctl.capture.segment_completion import finish_segmented_external_failure
        from camctl.capture.segment_models import ResultCheckEndReason

        (anchor,) = (await runtime.database.read(lambda owned, inputs=("SELECT e.id FROM history_events e JOIN json_each(e.body_json,'$.rows') r "
            "WHERE json_extract(r.value,'$.table')='operation_runs' AND json_extract(r.value,'$.id')=? "
            "AND json_extract(r.value,'$.after.values.status')=? ORDER BY e.id DESC LIMIT 1", ((await _stop_run_id(runtime, action["id"])), int(_RUN_STATUS.UNCONFIRMED))): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
        (await finish_segmented_external_failure(
            runtime,
            action["id"],
            ResultCheckEndReason.STOP_UNCONFIRMED_CLOSED,
            failure,
            anchor,
        ))
    else:
        (await _close_stop_exhausted(runtime, action, finish_action=True))


def _recording_port(context: CaptureRuntime) -> RecordingStatePort:
    """会话内缓存录像中段事实装载器；计时锚点跨推进保留。"""
    if context.recording_state is None:
        context.recording_state = SessionRecordingState(context)
    return context.recording_state


async def _activity_id_of(runtime: CaptureRuntime, action_id: int) -> int:
    """查询动作的唯一设备活动主键；动作与活动的主键不重合。"""
    return await runtime.database.read(lambda owned: _activity_id_of_connection(owned.connection, action_id),
                                       description="读取拍摄所属活动")


def _activity_id_of_connection(connection, action_id: int) -> int:
    """原保存恢复只从状态库读取所属活动，不装配设备端口。"""
    with closing(connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action_id,),
    )) as cursor:
        found = cursor.fetchone()
    if found is None:
        raise LookupError(f"设备活动不存在: {action_id}")
    return int(found[0])


@owned_tool_call
async def _stop_call(runtime: CaptureRuntime, action,
                     operation: str = "stop_recording", *, normal_recording=False) -> HandlerOutcome:
    """按原停止预算发起一次设备停止调用并保存尝试结果。

    意图先提交才派发；可靠确认结束停止流程，调用错误单独保存；
    未确认保持流程执行中并建立重试等待，预算沿原流程累计不刷新。
    停止操作字面量由调用方按任务类型提供。重试等待的间隔未到时
    不提交新意图，由推进循环下一轮再判。
    """
    if (action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
            and runtime.on_recording_deadline is not None):
        runtime.on_recording_deadline(action["id"], None)
    if runtime.stopper is None:
        raise LookupError("设备停止端口未装配")
    remaining = (await runtime.retry_wait_remaining(
        f"stop/{action['id']}", runtime.stop_config.retry_interval_s,
        maximum=runtime.stop_config.max_attempts))
    if remaining is not None:
        return HandlerOutcome("stop_retry_wait", f"{remaining}s")
    from camctl.devices.tasks import CaptureResultPolicy

    segmented_recording = (
        action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
        and action["execution_spec_json"].get("result_policy")
        in (
            CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
            CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
        )
    )
    # 只使用 START 固定的会话锚点；STOP 返回后先持有实际结果再等待。
    stop_state = (await _recording_port(runtime).recording_state(action["id"])) \
        if segmented_recording else None
    intent = AttemptIntent(
        operation="stop",
        action_id=action["id"],
        kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=(await _activity_id_of(runtime, action["id"]))),
        query_purpose=None,
        config=runtime.stop_config,
        occurred_at=runtime.wall_us(),
    )
    _save_inputs = (intent, new_operation_key())
    _save_options = dict()
    outcome = (await runtime.database.write(lambda owned: runtime.operations.begin_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"停止意图事务未完成（{outcome.kind.value}）: {outcome.error}", outcome)
    if outcome.value.disposition is not BeginDisposition.GRANTED:
        return HandlerOutcome("stop_not_granted", outcome.value.reason)
    ticket = outcome.value.ticket
    normal_source = None
    if (normal_recording and segmented_recording
            and action["execution_spec_json"].get("result_policy")
            == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
            and stop_state.anchor_from_current_session and stop_state.stop_target_ns is not None
            and stop_state.stop_attempts_used == 0 and ticket.attempt_id == 1):
        def read_start(owned):
            with closing(owned.connection.execute(
                "SELECT a.result_event_id FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id "
                "WHERE r.responsibility_key=? AND r.activity_id=? AND a.status=? "
                "AND a.effect_state=? AND a.error_json IS NULL AND a.result_event_id IS NOT NULL "
                "ORDER BY a.attempt_no DESC LIMIT 1",
                (f"start/{action['id']}", int(ticket.target_id), int(_ATTEMPT_STATUS.SUCCEEDED),
                 int(_EFFECT_STATE.CONFIRMED)),
            )) as cursor:
                return cursor.fetchone()
        start = await runtime.database.read(read_start, description="读取正常录像原启动结果")
        canceled_before_stop = (await runtime.action(action["id"]))["cancel_requested"]
        elapsed = runtime.monotonic_ns() - (stop_state.stop_target_ns - (
            _target_duration_ms(action) + action["execution_spec_json"].get("startup_margin_ms", 0)
        ) * 1_000_000)
        if start is not None and not canceled_before_stop and elapsed >= (
            _target_duration_ms(action) + action["execution_spec_json"].get("startup_margin_ms", 0)
        ) * 1_000_000:
            from camctl.capture.models import NormalRecordingStop

            normal_source = NormalRecordingStop(start[0], elapsed)
    with measure_stage("capture.stop", action_id=action["id"],
            device_id=action["device_id"], activity_id=ticket.target_id,
            run_id=ticket.run_id, attempt_id=ticket.attempt_id, operation=operation):
        response = await runtime.stopper.stop(ControlRequest(
            operation=operation,
            binding=_binding(action),
            params=action["effective_params_json"],
            ticket=ticket,
            timeout_s=runtime.stop_config.timeout_s,
        ))
        returned_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
    call, confirmed = _operation_outcome(
        response, "stop_confirmed", evidence_type="stop_returned")
    if response.outcome is not None:
        confirmed = call.effect is EffectState.CONFIRMED
    try:
        if confirmed:
            host_stop = None
            if segmented_recording:
                state = stop_state
                elapsed = None
                if (
                    state.anchor_from_current_session
                    and state.stop_target_ns is not None
                ):
                    duration_ns = (
                        _target_duration_ms(action)
                        + action["execution_spec_json"].get("startup_margin_ms", 0)
                    ) * 1_000_000
                    elapsed = returned_ns - (state.stop_target_ns - duration_ns)
                host_stop = HostTimerStopSave(elapsed, normal_source if (
                    call.status is AttemptStatus.SUCCEEDED and call.error is None
                ) else None)
            if (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
                    and action["execution_spec_json"]["end_control"] == int(EndControl.HOST_TIMER)):
                deadline = runtime.timelapse_deadlines.get(action["id"])
                elapsed = None if deadline is None else returned_ns - (
                    deadline - (_target_duration_ms(action)
                        + action["execution_spec_json"].get("startup_margin_ms", 0)) * 1_000_000)
                host_stop = HostTimerStopSave(elapsed)
            (await runtime.finish(ticket, call, end_run=RunOutcome.SUCCEEDED,
                           diagnostics_check=response.diagnostics_check,
                           occurred_at=returned_at, returned_ns=returned_ns,
                           host_timer_stop=host_stop, capture_stop=True))
        else:
            finite_cancel = bool(action["cancel_requested"]) and finite_cancel_policy(action)
            stop_error = (call.error or ErrorValue("recording_stop_failed", "device_stop", {
                "activity_id": ticket.target_id, "operation_run_id": str(ticket.run_id)}))
            (await runtime.finish(ticket, call, retry_wait=not finite_cancel,
                           diagnostics_check=response.diagnostics_check,
                           end_run=RunOutcome.FAILED if finite_cancel else None,
                           run_error=stop_error if finite_cancel else None,
                           occurred_at=returned_at, returned_ns=returned_ns))
    except OutcomeValidationError:
        # 观察与收场依据不符合登记契约：该结果整体不可采纳，不落
        # 库，按调用失败保存尝试终局（与启动调用同规则），不遗留
        # 执行中的停止流程。
        rejected, _ = _operation_outcome(
            DeviceCallResult(observations=(), error={
                "code": "invalid_device_result",
                "message": "驱动结果不符合登记契约"}),
            "stop_confirmed", evidence_type="stop_returned")
        (await runtime.finish(
            ticket, rejected, end_run=RunOutcome.FAILED,
            run_error=rejected.error, occurred_at=returned_at, returned_ns=returned_ns))
        return HandlerOutcome("call_failed", "invalid_device_result")
    if confirmed:
        return HandlerOutcome("confirmed")
    if response.error is not None:
        return HandlerOutcome("stop_failed", "device_error")
    return HandlerOutcome("sent")


async def _confirm_stop_by_query(runtime: CaptureRuntime, action) -> bool:
    """普通停止核实消费者：未确认停止先经原停止核实责任查询。

    停止命令已发送、没有调用错误且未取得确认时，先用本次 query 配
    置经 `query/stop` 责任查询。可靠空闲即按停止事实同事务结束查
    询、原停止流程并收场活动，返回 True；仍在录制、其他活动占用
    或调用错误保存观察与重试等待并交回停止链继续。直接停止已确认
    时不进入本消费者。
    """

    from camctl.capture.residual import (
        _begin_query_attempt, _last_attempt, _observed_activity, _run_row,
    )

    if runtime.state_query is None:
        return False
    action_id = action["id"]
    stop_attempt = (await runtime.last_attempt(f"stop/{action_id}"))
    if (stop_attempt is None
            or int(stop_attempt[0]) != int(_ATTEMPT_STATUS.SUCCEEDED)
            or int(stop_attempt[1]) != int(_EFFECT_STATE.UNKNOWN)):
        # 只有没有调用错误的未确认发送进入停止核实。
        return False
    responsibility = (
        f"query/stop/{action_id}/{(await _activity_id_of(runtime, action_id))}")
    row = (await runtime.database.read(lambda owned, inputs=(responsibility,): _run_row(owned.connection, *inputs), description="读取拍摄直接事实"))
    if row is not None and int(row[1]) not in (1, 2):
        # 核实责任已终态：预算耗尽按 UNCONFIRMED，停止链继续。
        return False
    attempt = (await runtime.database.read(lambda owned, inputs=(responsibility,): _last_attempt(owned.connection, *inputs), description="读取拍摄直接事实"))
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        # 在途查询等待实际返回；停止链本轮不再发令。
        return True
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.FAILED):
        # 查询调用失败：不反复查询，按停止预算继续停止。
        return False
    if (await runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s)) is not None:
        return False
    ticket = (await _begin_query_attempt(
        runtime, responsibility, action_id,
        AttemptTarget(activity_id=(await _activity_id_of(runtime, action_id))),
        QueryPurpose.STOP_CONFIRMATION))
    if ticket is None:
        return False
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(action),
        params=action["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    returned_at = runtime.wall_us()
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    if response.error is None and _observed_activity(response) is None:
        # 可靠空闲：停止已经生效，结果、原停止流程与活动收场同事务。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                       occurred_at=returned_at, query_conclude=action_id,
                       query_conclude_stop_keys=(f"stop/{action_id}",)))
        return True
    # 仍在录制、其他活动或调用错误：核实继续，停止链按原预算推进。
    (await runtime.finish(ticket, outcome, retry_wait=True, occurred_at=returned_at))
    return False


async def _observe_activity_for_reconcile(runtime: CaptureRuntime, action) -> bool:
    """对账活动核实消费者：墙钟成功分支前的归属与连续性确认。

    跨会话对账计时满足后，先用本次 query 配置经 `query/activity`
    责任确认当前仍是同一活动在录：归属确认即继续停止；可靠空闲表
    示活动已结束，保存观察并同事务收场活动，不重新执行停止；归属
    或连续性不足时不进入墙钟成功分支。驱动未声明查询能力时保持仅
    计时对账。
    """

    from camctl.capture.residual import (
        _begin_query_attempt, _last_attempt, _last_observations,
        _observed_activity, _run_row,
    )

    if runtime.state_query is None:
        return True
    action_id = action["id"]
    activity_id = (await _activity_id_of(runtime, action_id))
    responsibility = f"query/activity/{action_id}/{activity_id}"
    row = (await runtime.database.read(lambda owned, inputs=(responsibility,): _run_row(owned.connection, *inputs), description="读取拍摄直接事实"))
    if row is not None and int(row[1]) not in (1, 2):
        # 核实责任已终态：归属确认过即可停止；空闲已另行收场。
        if int(row[1]) == 3 and not (await runtime.database.read(lambda owned, inputs=(responsibility,): _last_observations(owned.connection, *inputs), description="读取拍摄直接事实")):
            return False
        return True
    attempt = (await runtime.database.read(lambda owned, inputs=(responsibility,): _last_attempt(owned.connection, *inputs), description="读取拍摄直接事实"))
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        return False
    if (await runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s)) is not None:
        # 调用失败或归属不明按间隔重查；预算耗尽前不进入停止。
        return False
    ticket = (await _begin_query_attempt(
        runtime, responsibility, action_id,
        AttemptTarget(activity_id=activity_id),
        QueryPurpose.ACTIVITY_OBSERVATION))
    if ticket is None:
        # 预算耗尽按 UNCONFIRMED：不再阻塞计时对账的停止链。
        return True
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(action),
        params=action["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    returned_at = runtime.wall_us()
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    if response.error is None and _observed_activity(response) == activity_id:
        # 同一活动仍在录：归属与连续性已确认，继续停止。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                       occurred_at=returned_at))
        return True
    if response.error is None and _observed_activity(response) is None:
        # 可靠空闲：活动已结束，观察与活动收场同事务，不重新停止。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                       occurred_at=returned_at, query_conclude=action_id))
        return False
    # 其他活动占用或调用错误：归属不明，不进入墙钟成功分支。
    (await runtime.finish(ticket, outcome, retry_wait=True, occurred_at=returned_at))
    return False


async def _finish_canceled_capture(runtime: CaptureRuntime, action_id: int, *,
                             unstarted: bool = False) -> None:
    """取消终态：放弃内容，不登记正式产物。"""
    from camctl.devices.tasks import CaptureResultPolicy

    pending = runtime.pending_baselines.get(action_id)
    if pending is not None and pending.read_owner is not None:
        if pending.in_call:
            return
        from camctl.capture.baseline_reads import abort_unstarted_baseline_read

        (await abort_unstarted_baseline_read(
            pending.read_owner, capture=runtime.capture, database=runtime.database,
            wall_us=runtime.wall_us,
        ))
        (await runtime.resume_baselines(action_id))
        pending = runtime.pending_baselines.get(action_id)
        if pending is not None and pending.read_owner is not None:
            return
    action = (await runtime.action(action_id))
    from camctl.capture.canceled_internal_reads import settle_canceled_processing_read
    if not await settle_canceled_processing_read(_read_result_scope(runtime), action_id):
        return
    policy = action["execution_spec_json"].get("result_policy")
    if policy == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value:
        from camctl.capture.action6_recovery import finalize_pending_action6_inspections

        if not (await finalize_pending_action6_inspections(
            runtime, action_id, reason="source_canceled"
        )):
            return
    if (
        policy == CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value
        or policy == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
        and action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
        and action["execution_started"]
        or unstarted
        and policy == CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value
    ) and (await runtime.action(action_id))["execution_started"]:
        from camctl.capture.segment_completion import finish_segmented_cancellation

        (await finish_segmented_cancellation(runtime, action_id))
        if unstarted:
            (await runtime.resume_baselines(action_id))
        return
    pending = runtime.pending_baselines.get(action_id)
    preparation_resolved = (unstarted and (pending is None
                            or (not pending.in_call and pending.saves.pending is None and pending.error is None)))
    (await runtime.save_capture_completion(
        FinishCanceledCapture(
            action_id=action_id, occurred_at=runtime.wall_us(), unstarted=unstarted,
            preparation_resolved=preparation_resolved)))
    if unstarted:
        (await runtime.resume_baselines(action_id))


async def _settle_unstarted_attempt(runtime: CaptureRuntime, action, attempt) -> bool:
    """全部原尝试已可靠无效果时，按当前取消与启动窗口本地结束。"""
    if (attempt is None or attempt[0] == int(_ATTEMPT_STATUS.RUNNING)
            or attempt[1] != int(_EFFECT_STATE.NO_EFFECT)):
        return False
    from camctl.persistence.repositories.capture_facts import load_start_facts
    from camctl.persistence.repositories.scheduling import ExpireActionRequest, ExpireOutcome
    from camctl.scheduling.rules import WindowPhase, window_phase

    facts = (await runtime.database.read(lambda owned, inputs=(action,): load_start_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if not facts.not_started:
        return False
    if action["cancel_requested"]:
        (await _finish_canceled_capture(runtime, action["id"], unstarted=True))
        return True
    now = runtime.wall_us()
    if window_phase(runtime.window_of(action), now) is not WindowPhase.AFTER_WINDOW:
        return False
    _save_inputs = (ExpireActionRequest(action["id"], now, now), new_operation_key())
    _save_options = dict()
    result = (await runtime.database.write(lambda owned: runtime.scheduling.expire_action(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"可靠无效果启动的过期事务未完成: {result.error}", result)
    return result.value.outcome is ExpireOutcome.EXPIRED


async def _catalog_drafts(
    registered: tuple[tuple[CaptureFile, int], ...],
    entries: tuple[ObservedFile, ...], *, product_rules: tuple[ProductRule, ...] = (),
) -> tuple[OutputDraft, ...]:
    """按登记结果构造产物目录草稿：原片与预览按配对分别登记。"""
    if isinstance(registered, RegisteredSourceFiles):
        entries = registered.entries
    def eligible(entry):
        return all(rule.min_size_bytes is None or (
            entry.size_bytes is not None and entry.size_bytes >= rule.min_size_bytes)
            for rule in product_rules if entry.kind is rule.kind
            and (rule.format_id is None or entry.format_id == rule.format_id))

    file_ids, complete_originals = {}, set()
    async for (_, file_id), entry in registered_observations(registered, entries):
        file_ids[entry.identity] = file_id
        if entry.complete and entry.paired_identity is None and eligible(entry):
            complete_originals.add(entry.identity)
    drafts = []
    async for (_, file_id), entry in registered_observations(registered, entries):
        if not entry.complete or not eligible(entry):
            continue
        if entry.paired_identity is None:
            drafts.append(OutputDraft(
                kind=OutputKind.ORIGINAL,
                file=FileReference(device_file_id=file_id),
                file_complete=True))
        else:
            if entry.paired_identity not in complete_originals:
                continue
            drafts.append(OutputDraft(
                kind=OutputKind.PREVIEW,
                file=FileReference(device_file_id=file_id),
                file_complete=True,
                original_batch_file_id=file_ids[entry.paired_identity]))
    return tuple(drafts)


async def _finish_capture(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
    required: FileKind, *, failure: RecordingFailure | None = None,
    registered=None, repair_file_id: int | None = None,
    results_run_id: int | None = None,
    set_finalized: bool = False,
    terminal_registered=None,
    collection_confirmed: bool = False,
) -> HandlerOutcome:
    """C6 核实产物集合并保存终态；失败保留完整且归属明确的文件。

    registered 传入已登记的观察结果避免重复登记；repair_file_id
    携带修复成功的成品时与原片同事务登记为 REPAIRED 产物。
    """
    if (
        (await runtime.action(action_id))["execution_spec_json"].get("result_policy")
        is not None
    ):
        if failure is None:
            raise ConsistencyError("结果策略任务的正式产物须由其独立结果模块发布")
        from camctl.capture.segment_completion import (
            finish_segmented_start_failure,
            _save,
        )
        from camctl.persistence.transaction import row_facts

        run_id = (await runtime.database.read(lambda owned, inputs=("SELECT id FROM operation_runs WHERE action_id=? AND kind=?", (action_id, int(_RUN_KIND.START))): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
        if run_id is not None and (await runtime.database.read(lambda owned, inputs=("operation_runs", run_id[0]): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["status"] in (1, 2):
            (await _save(
                runtime,
                action_id,
                runtime.operations,
                "finish_stale_runs",
                StaleRunFinish(
                    (f"start/{action_id}",),
                    RunOutcome.FAILED,
                    runtime.wall_us(),
                    ErrorValue(
                        failure.code,
                        registered_error(failure.code)["stage"],
                        failure.details,
                    ),
                ),
            ))
        (await finish_segmented_start_failure(runtime, action_id))
        return HandlerOutcome("terminal", "failed")
    if isinstance(registered, RegisteredSourceFiles):
        entries = registered.entries
    if registered is None:
        registered = (await _register_observed(runtime, action_id, entries))
    if failure is None and not collection_confirmed:
        assessment = (await assess_registered_files(registered, _product_requirements((await runtime.action(action_id)), required), set_finalized=set_finalized))
        if not assessment.is_complete:
            return HandlerOutcome("files_incomplete", str(assessment.missing_kinds))
    if terminal_registered is None:
        terminal_registered = (RegisteredSourceFiles(TerminalResultEntries(
            runtime.capture, runtime.database, registered.entries.ticket,
            current_entries=registered.entries.current_entries))
            if isinstance(registered, RegisteredSourceFiles) else registered)
    drafts = (await _catalog_drafts(terminal_registered, entries,
        product_rules=_product_requirements((await runtime.action(action_id)), required).rules))
    if repair_file_id is not None:
        original_file_ids = [
            file_id async for (_, file_id), entry in registered_observations(registered, entries)
            if entry.complete and entry.paired_identity is None]
        if original_file_ids:
            drafts = drafts + (OutputDraft(
                kind=OutputKind.REPAIRED,
                file=FileReference(intermediate_file_id=repair_file_id),
                file_complete=True,
                original_batch_file_id=original_file_ids[0],
            ),)
    command = FinishCapture(
        action_id=action_id,
        drafts=drafts,
        catalog_facts=OutputCatalogFacts(
            action_id=action_id, ownership_confirmed=True),
        occurred_at=runtime.wall_us(),
        failure=failure,
    )
    (await runtime.save_capture_completion(command if results_run_id is None else
        FinishRecordingResults(command, results_run_id)))
    return HandlerOutcome(
        "terminal", "failed" if failure is not None else "succeeded")


def _product_requirements(action, required):
    spec = action["execution_spec_json"]
    rules = tuple(ProductRule.from_json(value) for value in spec.get("product_rules", ()))
    return ProductRequirements(frozenset({required}), rules)


def _listing_finalized(listing: ListingRound | None) -> bool:
    return (listing is not None and listing.time_qualified and listing.scan_complete and listing.set_finalized
            and listing.outcome is not None and listing.outcome.error is None)


async def _output_failure(runtime, action_id, assessment):
    if not assessment.explicitly_unmet:
        return None
    return RecordingFailure("capture_failed", {
        "activity_id": str((await _activity_id_of(runtime, action_id))),
        "reason": "no_outputs" if assessment.file_count == 0 else "invalid_outputs"})


async def _release_occupancy(runtime: CaptureRuntime, action_id: int) -> None:
    """按统一释放判定解除本活动占用；条件不满足保持原状。"""
    _save_inputs = (ActivityReleaseSave(
            action_id=action_id, occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.release_occupancy(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)


def _target_duration_ms(action: Mapping[str, Any]) -> int:
    """从首次固定的执行定义读取录像目标时长（毫秒）。

    定义缺失或非法属于状态库错误；本会话不猜测时长继续执行。
    """
    spec = action["execution_spec_json"]
    if not isinstance(spec, Mapping):
        raise ConsistencyError("录像执行定义缺失，目标时长不可读")
    duration = spec.get("target_duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        raise ConsistencyError(f"录像目标时长缺失或非法: {duration!r}")
    return duration


def _start_call_timeout(action: Mapping[str, Any]) -> Decimal:
    """完整原生调用使用首次固定期限，其余启动为短控制调用。"""
    if (action["type"] == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO)
            and action["execution_spec_json"].get("output_storage") is not None):
        timeout = action["execution_spec_json"].get("start_call_timeout_s")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, Decimal))
                or timeout <= 0):
            raise ConsistencyError("单张固定调用期限缺失或非法")
        return timeout if isinstance(timeout, Decimal) else Decimal(timeout)
    if (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            and action["execution_spec_json"]["start_return_meaning"] == int(StartReturn.COMPLETED)):
        return action["execution_spec_json"]["start_call_timeout_s"]
    return Decimal("30")


async def _closed_capture_is_local(
    runtime: CaptureRuntime, action: Mapping[str, Any],
) -> bool:
    def read(owned):
        """原有限核实已结束且无需设备操作时，按原事实继续业务收尾。"""
        if action["type"] not in (
                int(_ACTION_TYPE.CAMERA_TAKE_PHOTO), int(_ACTION_TYPE.CAMERA_TIMELAPSE)):
            return False
        with closing(owned.connection.execute(
            "SELECT id,result_set_state,activity_state,sent_at,wait_completed_event_id"
            " FROM device_activities WHERE action_id=?", (action["id"],),
        )) as cursor:
            activity = cursor.fetchone()
        if activity is None or activity[1] != int(_RESULT_SET_STATE.UNCONFIRMED):
            return False
        with closing(owned.connection.execute(
            "SELECT action_id,activity_id,kind,status FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity[0]}",),
        )) as cursor:
            results = cursor.fetchone()
        if (results is None or results[:3] != (
                action["id"], activity[0], int(_RUN_KIND.CHECK_CAPTURE_RESULTS))):
            raise ConsistencyError("已保存无法确认结论缺少原 RESULTS 责任")
        if results[3] != int(_RUN_STATUS.UNCONFIRMED):
            return False

        from camctl.persistence.repositories.capture_facts import load_start_facts

        start = load_start_facts(owned.connection, action)
        if start.run is None or start.attempts_used == 0 or start.not_started:
            raise ConsistencyError("已结束 RESULTS 缺少原已执行启动事实")
        with closing(owned.connection.execute(
            "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND t.status=? LIMIT 1",
            (action["id"], int(_ATTEMPT_STATUS.RUNNING)),
        )) as cursor:
            if cursor.fetchone() is not None:
                return False
        with closing(owned.connection.execute(
            "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND t.result_event_id IS NULL LIMIT 1", (action["id"],),
        )) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("本地拍摄收尾缺少原实际调用结果")
        if action["type"] == int(_ACTION_TYPE.CAMERA_TAKE_PHOTO):
            return True
        if not action["cancel_requested"]:
            if activity[3] is None or activity[4] is None:
                raise ConsistencyError("已结束延时核实缺少原发送或等待完成事实")
            return True

        with closing(owned.connection.execute(
            "SELECT action_id,activity_id,kind,status FROM operation_runs"
            " WHERE responsibility_key=?", (f"stop/{action['id']}",),
        )) as cursor:
            stop = cursor.fetchone()
        ended = activity[2] == int(_ACTIVITY_STATE.ENDED)
        if stop is None:
            return ended
        if stop[:3] != (action["id"], activity[0], int(_RUN_KIND.STOP)):
            raise ConsistencyError("取消延时收尾的原 STOP 身份不符")
        if ended:
            return True
        if stop[3] in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)):
            return False
        if stop[3] in (int(_RUN_STATUS.SUCCEEDED), int(_RUN_STATUS.FAILED),
                       int(_RUN_STATUS.UNCONFIRMED)):
            return True
        raise ConsistencyError("取消延时收尾缺少适用的原 STOP 结束结果")
    return await runtime.database.read(read, description="读取拍摄当前责任")


async def _resume_bound_local_read(runtime, action, copy_id, ticket, saved_end):
    """绑定异常下只推进已结束的本机读取，其他设备责任仍由动作处理。"""
    if saved_end[0] != ticket:
        raise ConsistencyError("本机恢复的读取结束与原票据不一致")
    if runtime.recovery_evidence_for is None:
        return
    source = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT c.processing_id,c.inspection_id,c.source_device_file_id,ob.device_id,ob.driver_id"
        " FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id"
        " JOIN actions ob ON ob.id=f.observer_action_id WHERE c.id=?", (copy_id,)).fetchone())
    if source is None:
        raise ConsistencyError("本机恢复缺少内部拷贝的固定来源")
    processing_id, inspection_id, source_id, device_id, driver_id = source
    evidence = runtime.recovery_evidence_for(DeviceBinding(device_id, driver_id), "read")
    if evidence is None:
        return
    from camctl.outputs.read_attempts import (HeldReadEnd, canceled_complete_read_result,
                                              current_local_read_end, run_owned_read)
    from camctl.capture.media_flow import _save_internal_read_and_settle

    held = runtime.pending_read_ends.get(copy_id)
    if held is not None and (held.ticket != ticket or held.end != saved_end[1]):
        raise ConsistencyError("本机恢复与原拥有者持有的读取结束不一致")
    if held is None:
        held = HeldReadEnd(ticket, saved_end[1], saved_end[2], evidence=evidence)
        runtime.pending_read_ends[copy_id] = held
    if runtime.media is None:
        if runtime.roots is None:
            raise ConsistencyError("本机恢复缺少固定工作目录")
        flow = MediaFlow(database=runtime.database, roots=runtime.roots, sessions=None,
            tools=None, policy=None, occurred_at=runtime.wall_us, digest_supported=False,
            max_recopies=None, evidence=evidence, file_executor=runtime.file_executor,
            operations=runtime.operations, retry_gate=runtime.retry_gate,
            pending_read_results=runtime.pending_read_results,
            pending_read_business=runtime.pending_read_business,
            pending_read_ends=runtime.pending_read_ends,
            continuing_read_tickets=runtime.continuing_read_tickets,
            pending_segment_inspection_results=runtime.pending_segment_inspection_results)
    else:
        flow = replace(runtime.media, evidence=evidence)
    if action["cancel_requested"]:
        async def settle_canceled(_control):
            if not await current_local_read_end(flow, copy_id, held):
                return
            pending = await canceled_complete_read_result(flow, ticket, evidence)
            if pending is not None:
                await _save_internal_read_and_settle(flow, pending)
        if held.resume is None:
            held = replace(held, resume=lambda current, value:
                _resume_bound_local_read(replace(runtime, database=current.database), action,
                                         copy_id, ticket, saved_end))
            runtime.pending_read_ends[copy_id] = held
        await run_owned_read(flow, copy_id, settle_canceled)
    else:
        from camctl.capture.input_copy import InputContext, OperationConfig
        from camctl.capture.media_flow import _resume_local_internal_end

        input_context = InputContext(
            action_id=action["id"], processing_id=processing_id,
            inspection_id=inspection_id, source_device_file_id=source_id,
            target_extension=None,
            config=OperationConfig(flow.max_read_attempts, flow.read_idle_timeout_s,
                                   flow.retry_interval_s),
            segment_size=flow.segment_size, staging=flow.roots.staging,
            copies=flow.copies(), sessions=flow.sessions,
            occurred_at=runtime.wall_us(), read_end=saved_end[1])
        async def finish_missing_config(current_flow, value):
            from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result

            binding_result = runtime.binding_check(DeviceBinding(device_id, driver_id))
            details = binding_failure_details(binding_result)
            if details is None or details["reason"] != "missing":
                raise ConsistencyError("读取摘要不一致缺少原设备配置缺失依据")
            request = await _binding_failure_request(runtime, action, details, excluded_run_id=ticket.run_id)
            business = PendingReadBusiness(runtime.capture.finish_binding_failure, request, new_operation_key())
            pending = await held_binding_read_result(current_flow, copy_id, binding_result, business)
            if pending is not None:
                await _save_internal_read_and_settle(current_flow, pending)

        if held.resume is None:
            held = replace(held, resume=lambda current, value:
                _resume_local_internal_end(current, value, flow, input_context,
                                           on_missing_config=finish_missing_config))
            runtime.pending_read_ends[copy_id] = held
        await _resume_local_internal_end(flow, held, flow, input_context,
                                         on_missing_config=finish_missing_config)


async def _saved_internal_binding_failure(runtime, action):
    """恢复内部 READ 已保存的原绑定失败，不查询本次设备配置。"""
    from camctl.persistence.repositories.binding_read_cancel import load_saved_binding_read_failure

    def read(owned):
        with closing(owned.connection.execute(
            "SELECT c.id,r.error_json,c.processing_id,c.inspection_id FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
            " WHERE r.action_id=? AND r.kind=3 AND r.status=4 AND c.delivery_id IS NULL"
            " AND json_extract(r.error_json,'$.code')='device_binding_unavailable' LIMIT 2",
            (action["id"],),
        )) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ConsistencyError("拍摄存在多个未收场的原读取绑定失败")
        copy_id, stored, processing_id, inspection_id = rows[0]
        if (processing_id is None) == (inspection_id is None):
            raise ConsistencyError("原内部读取绑定失败缺少唯一处理或检查归属")
        error = parse_exact_json(stored)
        key, identity, occurred_at = load_saved_binding_read_failure(
            owned.connection, action["id"], copy_id, error)
        return copy_id, error["details"], key, identity, occurred_at, inspection_id

    return await runtime.database.read(read, description="读取原内部读取绑定失败")


async def _handle_binding_failure(
    runtime: CaptureRuntime, action: Mapping[str, Any],
) -> bool:
    """异常绑定不产生新调用；原结果可靠保存后共同保存业务收场。"""
    saved_read = await _saved_internal_binding_failure(runtime, action)
    if saved_read is None:
        if (await _closed_capture_is_local(runtime, action)):
            return False
        if runtime.binding_check is None:
            return False
        binding_result = runtime.binding_check(_binding(action))
        details = binding_failure_details(binding_result)
    else:
        from camctl.devices.bindings import BindingStatus
        copy_id, details, _key, identity, _occurred_at, _inspection_id = saved_read
        statuses = {"missing": BindingStatus.DEVICE_MISSING, "mismatch": BindingStatus.DRIVER_MISMATCH,
                    "physical_identity_mismatch": BindingStatus.PHYSICAL_IDENTITY_MISMATCH,
                    "physical_identity_conflict": BindingStatus.PHYSICAL_IDENTITY_CONFLICT}
        status = statuses.get(details.get("reason"))
        if status is None:
            raise ConsistencyError("原内部读取绑定失败原因不属于已登记分区")
        binding_result = BindingResult(status, _binding(action), details.get("actual_driver_id"), identity)
    if details is None:
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        if action.get("driver_id") == DRIVER_ID and action.get("type") == 1:
            from camctl.devices.bindings import BindingStatus
            from camctl.persistence.repositories.physical_alias import action_physical_identity, physical_alias_conflict
            def read_alias(owned):
                fixed = action_physical_identity(owned.connection, action["id"])
                if fixed != runtime.fixed_camera_identity:
                    return False
                return physical_alias_conflict(owned.connection, action["device_id"], fixed)
            if await runtime.database.read(read_alias, description="读取原机身的未完成责任"):
                binding_result = BindingResult(BindingStatus.PHYSICAL_IDENTITY_CONFLICT,
                                               _binding(action), None)
                details = binding_failure_details(binding_result)
    if details is None:
        return False
    if saved_read is None and (await _local_recording_listing(runtime, action)) is not None:
        return False
    def read(owned, parameters=(action["id"], int(_ATTEMPT_STATUS.RUNNING))):
        with closing(owned.connection.execute("SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
        " WHERE r.action_id = ? AND t.status = ? LIMIT 1", parameters)) as cursor:
            return cursor.fetchone() is not None
    has_unfinished_attempt = await runtime.database.read(read, description="读取拍摄直接事实")
    if has_unfinished_attempt:
        def read(owned, parameters=(action["id"], int(_ATTEMPT_STATUS.RUNNING))):
            with closing(owned.connection.execute("SELECT DISTINCT r.responsibility_key, r.kind FROM operation_attempts t"
            " JOIN operation_runs r ON r.id = t.run_id"
            " WHERE r.action_id = ? AND t.status = ? ORDER BY r.id", parameters)) as cursor:
                return cursor.fetchall()
        unfinished = await runtime.database.read(read, description="读取拍摄当前责任")
        for responsibility, kind in unfinished:
            if kind == int(_RUN_KIND.READ_FILE):
                from types import SimpleNamespace
                from camctl.outputs.read_attempts import read_host_boundary, record_read_diagnostic, running_read_ticket

                copy_id = (await runtime.database.read(lambda owned, inputs=("SELECT copy_id FROM operation_runs WHERE responsibility_key=?", (responsibility,)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))[0]
                original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
                if original is None:
                    raise ConsistencyError("内部读取绑定失败缺少原未结束尝试")
                ticket, intent_id = original
                read_scope = _read_result_scope(runtime)
                read_scope.recovery_boundary = runtime.recovery_boundary
                read_scope.recovery_max_event_id = runtime.recovery_max_event_id
                read_scope.last_recovery_diagnostic = runtime.last_recovery_diagnostic
                read_scope.recovery_evidence_for = runtime.recovery_evidence_for
                read_scope.on_recovery_diagnostic = runtime.record_recovery_diagnostic
                from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result
                from camctl.capture.media_flow import _save_internal_read_and_settle

                from camctl.persistence.repositories.read_end import load_read_end
                saved_end = await runtime.database.read(lambda owned: load_read_end(copy_id, owned))
                if saved_end is not None and saved_end[3] is None:
                    await _resume_bound_local_read(runtime, action, copy_id, ticket, saved_end)
                    return True
                if copy_id in read_scope.pending_read_ends:
                    request = (await _binding_failure_request(runtime, action, details, excluded_run_id=ticket.run_id))
                    business = PendingReadBusiness(runtime.capture.finish_binding_failure, request, new_operation_key())
                    actual = (await held_binding_read_result(read_scope, copy_id, binding_result, business))
                    if actual is not None:
                        (await _save_internal_read_and_settle(read_scope, actual))
                        runtime.timelapse_deadlines.pop(action["id"], None)
                        runtime.natural_wait_states.pop(action["id"], None)
                        return True
                if not read_host_boundary(read_scope, ticket, intent_id, continuing=False):
                    return True
                if runtime.recovery_evidence_for is None:
                    record_read_diagnostic(read_scope, RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP, ticket)
                    return True
                source = (await runtime.database.read(lambda owned, inputs=("SELECT a.device_id,a.driver_id FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id"
                    " JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (copy_id,)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
                if source is None:
                    raise ConsistencyError("内部读取绑定失败缺少原文件观察者")
                if runtime.recovery_evidence_for(DeviceBinding(*source), "read") is None:
                    record_read_diagnostic(read_scope, RecoveryBlockedReason.EVIDENCE_UNAVAILABLE, ticket)
                    return True
                from camctl.outputs.read_attempts import recover_unavailable_read

                if not (await recover_unavailable_read(read_scope, copy_id, DeviceBinding(*source), runtime.wall_us())):
                    return True
                continue
            operation = _RECOVERABLE_OPERATIONS.get(kind)
            if operation is None:
                return True
            ticket = (await _original_ticket(runtime, responsibility, operation))
            if ticket is None:
                raise ConsistencyError("绑定失败的未完成责任缺少原尝试票据")
            if not (await runtime.recover_attempt(ticket)):
                return True
    if action["cancel_requested"]:
        from camctl.persistence.repositories.capture_facts import load_start_facts

        facts = (await runtime.database.read(lambda owned, inputs=(action,): load_start_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if facts.not_started:
            return False
        if facts.activity is not None and facts.activity["stop_supported"] == 0:
            return False
        def read(owned, parameters=(f"stop/{action['id']}",)):
            with closing(owned.connection.execute("SELECT status FROM operation_runs WHERE responsibility_key = ?", parameters)) as cursor:
                return cursor.fetchone()
        stop = await runtime.database.read(read, description="读取拍摄当前责任")
        if action["type"] == 2 and (
                facts.activity is not None and facts.activity["activity_state"] == 3
                or stop is not None and stop[0] not in (1, 2)):
            return False
    from camctl.outputs.read_attempts import save_read_business

    if saved_read is not None:
        copy_id, details, key, _identity, occurred_at, inspection_id = saved_read
        if inspection_id is not None:
            from types import SimpleNamespace
            from camctl.capture.segment_inspections import (
                settle_inspection_read_failure, settle_terminal_inspection_read_slot,
            )
            flow = runtime.media or SimpleNamespace(
                database=runtime.database, occurred_at=runtime.wall_us,
                pending_segment_inspection_results=runtime.pending_segment_inspection_results,
                pending_read_business=runtime.pending_read_business,
                pending_read_results=runtime.pending_read_results, pending_read_ends=runtime.pending_read_ends,
                continuing_read_tickets=runtime.continuing_read_tickets,
                operations=runtime.operations, evidence=runtime.evidence,
                file_executor=runtime.file_executor,
            )
            if not await settle_inspection_read_failure(flow, copy_id, occurred_at):
                current_action = await runtime.action(action["id"])
                return not bool(current_action["cancel_requested"])
            if not await settle_terminal_inspection_read_slot(flow, copy_id, runtime.wall_us()):
                return True
            return False
        request = replace(await _binding_failure_request(runtime, action, details),
                          read_result_key=key, read_copy_id=copy_id)
        receipt = await save_read_business(_read_result_scope(runtime), action["id"],
            "capture_binding_failure", request, runtime.capture.finish_binding_failure)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"原内部读取绑定失败收场未可靠保存: {receipt.error}", receipt)
        return receipt.value.disposition is not FinishDisposition.RETIRED

    from camctl.devices.tasks import CaptureResultPolicy

    if action["execution_spec_json"].get("result_policy") in {
        policy.value for policy in CaptureResultPolicy
    }:
        from camctl.capture.segment_completion import (
            finish_segmented_external_failure,
            _save,
            resume_segment_writes,
        )
        from camctl.capture.segment_models import ResultCheckEndReason

        (await resume_segment_writes(
            runtime.database, runtime.pending_segment_writes, action["id"]
        ))
        responsibilities = await runtime.database.read(lambda owned: tuple(row[0] for row in owned.connection.execute(
            "SELECT responsibility_key FROM operation_runs WHERE action_id=? AND kind!=? AND status IN (1,2) ORDER BY id",
            (action["id"], int(_RUN_KIND.CHECK_CAPTURE_RESULTS)))))
        failure = RecordingFailure("device_binding_unavailable", details)
        if responsibilities:
            (await _save(
                runtime,
                action["id"],
                runtime.operations,
                "finish_stale_runs",
                StaleRunFinish(
                    responsibilities,
                    RunOutcome.FAILED,
                    runtime.wall_us(),
                    ErrorValue(failure.code, "execution", details),
                ),
            ))
        if action["cancel_requested"]:
            (await _finish_canceled_capture(runtime, action["id"]))
        else:
            (await finish_segmented_external_failure(
                runtime, action["id"], ResultCheckEndReason.BINDING_UNAVAILABLE, failure
            ))
        return True

    request = (await _binding_failure_request(runtime, action, details))
    internal_read = (await runtime.database.read(lambda owned, inputs=("SELECT 1 FROM file_copies c JOIN recording_processing p ON p.id=c.processing_id"
        " WHERE p.action_id=? LIMIT 1", (action["id"],)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
    if internal_read is not None:
        receipt = (await save_read_business(_read_result_scope(runtime), action["id"], "capture_binding_failure", request,
                                    runtime.capture.finish_binding_failure))
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"拍摄绑定失败的完整事务未完成（{receipt.kind.value}）: {receipt.error}", receipt)
    else:
        (await runtime.save_capture_completion(request))
    if details["reason"] == "physical_identity_conflict" and (await runtime.action(action["id"]))["status"] == 2:
        return False
    (await runtime.resume_baselines(action["id"]))
    runtime.timelapse_deadlines.pop(action["id"], None)
    runtime.natural_wait_states.pop(action["id"], None)
    return True


async def _binding_failure_request(runtime, action, details, *, excluded_run_id=None):
    def read(owned, parameters=(action["id"],)):
        with closing(owned.connection.execute("SELECT id,responsibility_key FROM operation_runs WHERE action_id = ?"
        " AND kind IN (1, 2, 3, 6, 7) AND status IN (1, 2)"
        " AND (kind != 6 OR query_purpose != 5) ORDER BY id", parameters)) as cursor:
            return tuple(row[1] for row in cursor.fetchall() if row[0] != excluded_run_id)
    responsibilities = await runtime.database.read(read, description="读取拍摄直接事实")
    drafts = (await _binding_failure_files(runtime, action))
    return FinishBindingFailure(
        action_id=action["id"], occurred_at=runtime.wall_us(),
        failure=RecordingFailure(code="device_binding_unavailable", details=details),
        canceled=bool(action["cancel_requested"]), stop_config=runtime.stop_config,
        responsibility_keys=responsibilities, check_config=runtime.check_config,
        drafts=drafts, catalog_facts=(OutputCatalogFacts(action["id"], True) if drafts else None))


async def _binding_failure_files(runtime: CaptureRuntime, action) -> tuple[OutputDraft, ...]:
    """拍摄按原实际 RESULTS 保留文件，当前绑定不解释原观察。"""
    if action["type"] not in (int(_ACTION_TYPE.CAMERA_TAKE_PHOTO), int(_ACTION_TYPE.CAMERA_RECORD),
                              int(_ACTION_TYPE.CAMERA_TIMELAPSE)):
        return ()
    if action["type"] == int(_ACTION_TYPE.CAMERA_RECORD) and action["cancel_requested"]:
        return ()
    def read(owned):
        with closing(owned.connection.execute(
        "SELECT id,activity_id,responsibility_key FROM operation_runs"
        " WHERE action_id=? AND kind=? ORDER BY id",
        (action["id"], int(_RUN_KIND.CHECK_CAPTURE_RESULTS)),
    )) as cursor:
            return cursor.fetchall()
    runs = await runtime.database.read(read)
    def read(owned):
        with closing(owned.connection.execute(
        "SELECT 1 FROM device_files WHERE source_action_id=? LIMIT 1", (action["id"],),
    )) as cursor:
            return cursor.fetchone() is not None
    has_files = await runtime.database.read(read)
    if not runs:
        if has_files:
            raise ConsistencyError("原拍摄文件缺少可靠 RESULTS 责任，不能按空产物结束")
        return ()
    if len(runs) != 1:
        raise ConsistencyError("原拍摄 RESULTS 责任不唯一")
    run_id, activity_id, responsibility = runs[0]
    if (activity_id != (await _activity_id_of(runtime, action["id"]))
            or responsibility != f"results/{activity_id}"):
        raise ConsistencyError("原 RESULTS 责任与拍摄活动身份不符")
    def read(owned):
        with closing(owned.connection.execute(
        "SELECT 1 FROM operation_attempts WHERE run_id=? LIMIT 1", (run_id,),
    )) as cursor:
            return cursor.fetchone() is not None
    has_attempts = await runtime.database.read(read)
    if not has_attempts:
        if has_files:
            raise ConsistencyError("原拍摄文件缺少可靠 RESULTS 实际输入，不能按空产物结束")
        return ()
    listing = (await _saved_result_listing(runtime, action["id"]))
    registered = _retained_result_files(runtime, listing, (await _register_listing(runtime, action["id"], listing)))
    return (await _catalog_drafts(registered, listing.entries,
        product_rules=_product_requirements(action, FileKind.VIDEO).rules))


async def _photo_handler(action_id: int, context: CaptureRuntime) -> None:
    (await context.resume_capture_completions(action_id))
    (await context.resume_result_check_closes(action_id))
    (await context.resume_file_observations(action_id))
    (await context.resume_start_results(action_id))
    action = (await context.action(action_id))
    if action["status"] in _ACTION_TERMINAL:
        return
    from camctl.capture.host_photo import advance_host_photo, is_saved_capture_photo

    if is_saved_capture_photo(action):
        await advance_host_photo(context, action_id)
        return
    if (await _handle_binding_failure(context, action)):
        return
    attempt = (await context.last_attempt(f"start/{action_id}"))
    if attempt is None:
        if action["cancel_requested"]:
            (await _finish_canceled_capture(context, action_id, unstarted=True))
            return
        step = await _control_call(
            context, action, "take_photo", "photo_taken",
            activity_facts=lambda confirmed: (
                {"sent_at": context.wall_us()}
                | ({"started_at": context.wall_us(), "activity_state": 2}
                   if confirmed else {})))
        if step.phase not in ("confirmed", "call_failed"):
            # 仅发送或未授予：没有可靠响应事实，等待下次推进。
            return
        attempt = (await context.last_attempt(f"start/{action_id}"))
        action = (await context.action(action_id))
    if (await _settle_unstarted_attempt(context, action, attempt)):
        return
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        # 在途或本轮列举失败：已保存实际结果与重试等待，下一轮重新核实。
        return
    action = (await context.action(action_id))
    if action["cancel_requested"]:
        # 照片的公开取消只适用于可靠未启动；原核实返回仍真实保存。
        if listing.phase is ListingPhase.LISTED:
            (await _finish_listing_result(context, listing, end_run=RunOutcome.CANCELED))
        if (await _settle_unstarted_attempt(context, action, attempt)):
            return
        raise ConsistencyError("照片取消缺少可靠未启动依据")
    unconfirmed = (listing.phase is ListingPhase.CLOSED
        and (await context.database.read(lambda owned, inputs=("operation_runs", listing.ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["status"]
            == int(enum_for("operation_runs.status").UNCONFIRMED))
    if listing.phase is ListingPhase.EXHAUSTED or unconfirmed:
        # 原无法确认结论保持；仅继续未完成的文件和业务收场。
        saved = listing if unconfirmed else (await _saved_result_listing(context, action_id))
        registered = (await _register_listing(context, action_id, saved))
        if not unconfirmed:
            (await _close_check_unconfirmed(context, action_id))
        (await _settle_open_start(
            context, action_id, RunOutcome.UNCONFIRMED,
            error=ErrorValue(code="result_unconfirmed", stage="device")))
        (await _finish_capture(context, action_id, saved.entries, FileKind.PHOTO, registered=registered,
                        failure=(await _unconfirmed_failure(context, action_id))))
        return
    entries = listing.entries
    ticket = None if listing.already_saved else listing.ticket
    registered = (await _register_listing(context, action_id, listing))
    if isinstance(registered, RegisteredSourceFiles):
        entries = registered.entries
    files = (await assess_registered_files(registered, ProductRequirements(required_kinds=frozenset({FileKind.PHOTO})), set_finalized=_listing_finalized(listing)))
    assessment = CaptureAssessment(
        complete=files.is_complete,
        explicitly_unmet=files.explicitly_unmet,
        read_error=bool(files.read_errors))
    # 启动尝试在途（RUNNING）或结果未知（UNKNOWN）时没有可采纳的
    # 响应结论：不折叠为失败，按只发送契约由产物核实证明终局；
    # 效果未知输入留给发送未确认的保守分区，不在这里短路核实。
    response_unresolved = attempt[0] in (
        int(_ATTEMPT_STATUS.RUNNING), int(_ATTEMPT_STATUS.UNKNOWN))
    decision = decide_photo(
        PhotoState(
            action_terminal=False,
            canceled=bool(action["cancel_requested"]),
            dispatched=True,
            response_completed=not response_unresolved
            and attempt[1] == int(_EFFECT_STATE.CONFIRMED),
            response_failed=attempt[0] == int(_ATTEMPT_STATUS.FAILED),
            effect_unknown=not response_unresolved
            and attempt[1] == int(_EFFECT_STATE.UNKNOWN),
            stop_supported=False,
        ),
        assessment,
        (PhotoCompletion.SENT_ONLY if response_unresolved
         else PhotoCompletion.COMPLETED_ON_RETURN),
    )
    if decision is PhotoDecision.REGISTER_SUCCESS:
        if ticket is not None:
            (await _finish_listing_result(context, listing,
                           end_run=RunOutcome.SUCCEEDED))
        (await _settle_open_start(context, action_id, RunOutcome.SUCCEEDED))
        (await _conclude_activity(context, action_id))
        (await _finish_capture(context, action_id, entries, FileKind.PHOTO, registered=registered,
                        set_finalized=_listing_finalized(listing)))
    elif decision is PhotoDecision.FAILED_KEEP_FILES:
        if ticket is not None:
            (await _finish_listing_result(context, listing,
                           end_run=RunOutcome.SUCCEEDED))
        if attempt[0] == int(_ATTEMPT_STATUS.FAILED):
            failure = RecordingFailure("capture_failed", {
                "activity_id": str((await _activity_id_of(context, action_id))), "reason": "device_failed"})
            (await _settle_open_start(context, action_id, RunOutcome.FAILED,
                              error=ErrorValue(code="device_failed", stage="device")))
        else:
            failure = (await _output_failure(context, action_id, files))
            (await _settle_open_start(context, action_id, RunOutcome.SUCCEEDED))
            (await _conclude_activity(context, action_id))
        if failure is None:
            raise ConsistencyError("单张拍摄明确失败缺少实际失败分区")
        (await _finish_capture(
            context, action_id, entries, FileKind.PHOTO, registered=registered,
            failure=failure))
    elif ticket is not None:
        # 其余分区（等待响应、取消保留、未知无停止）：本轮成功结果与
        # 重试等待共同保存，等待下次推进或取消收场。
        (await _finish_listing_result(context, listing, retry_wait=True))
    elif listing.already_saved and not action["cancel_requested"]:
        # 原核实责任已结束，未知集合不能重新取得额度，也不能成为
        # 成功；保存无法确认的业务结果，保留独立完整文件。
        (await _settle_open_start(context, action_id, RunOutcome.UNCONFIRMED,
                          error=ErrorValue(code="result_unconfirmed", stage="device")))
        (await _finish_capture(context, action_id, entries, FileKind.PHOTO, registered=registered,
                        failure=(await _unconfirmed_failure(context, action_id))))


async def _original_ticket(runtime: CaptureRuntime, responsibility: str, operation: str):
    def read(owned):
        with closing(owned.connection.execute(
            "SELECT r.id, a.attempt_no, r.activity_id FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id = r.id"
            " WHERE r.responsibility_key = ? ORDER BY a.attempt_no DESC LIMIT 1",
            (responsibility,))) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return AttemptTicket(row[1], operation, None if row[2] is None else str(row[2]),
                             responsibility, row[0])
    return await runtime.database.read(read, description="读取拍摄当前责任")


async def _close_record_start(runtime, action, *, exhausted: bool, query_key: str | None = None):
    from camctl.persistence.repositories.capture_facts import load_start_facts

    facts = (await runtime.database.read(lambda owned, inputs=(action,): load_start_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if exhausted:
        failure = RecordingFailure("start_attempts_exhausted", {
            "max_attempts": runtime.start_config.max_attempts,
            "attempts_used": facts.attempts_used})
        final = RunOutcome.FAILED
    else:
        failure = RecordingFailure("capture_result_unconfirmed", {
            "activity_id": str(facts.activity["id"]), "reason": "start_unknown"})
        final = RunOutcome.UNCONFIRMED
    keys = (f"start/{action['id']}",) + (() if query_key is None else (query_key,))
    now = runtime.wall_us()
    if action["execution_spec_json"].get("result_policy") is not None:
        from camctl.capture.segment_completion import (
            finish_segmented_start_failure,
            _save,
        )

        (await _save(
            runtime,
            action["id"],
            runtime.operations,
            "finish_stale_runs",
            StaleRunFinish(
                keys,
                final,
                now,
                ErrorValue(
                    failure.code,
                    registered_error(failure.code)["stage"],
                    failure.details,
                ),
            ),
        ))
        (await finish_segmented_start_failure(runtime, action["id"]))
        return
    (await runtime.save_capture_completion(StartCloseRequest(
        StaleRunFinish(responsibility_keys=keys, status=final, occurred_at=now, error=ErrorValue(
            failure.code, registered_error(failure.code)["stage"], failure.details)),
        FinishCapture(action["id"], (), OutputCatalogFacts(action["id"], True), now,
                      failure=failure))))


@owned_tool_call
async def _confirm_record_start(runtime, action, facts):
    """未知启动沿同一独立查询责任有限核实；空闲不证明从未启动。"""
    activity = facts.activity
    key = f"query/start/{action['id']}/{activity['id']}"
    latest = (await runtime.last_attempt(key))
    if latest is not None and latest[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = (await _original_ticket(runtime, key, "query"))
        if not (await runtime.recover_attempt(ticket)):
            return
    def read(owned, parameters=(key,)):
        with closing(owned.connection.execute("SELECT status, attempts_used FROM operation_runs WHERE responsibility_key = ?", parameters)) as cursor:
            return cursor.fetchone()
    query = await runtime.database.read(read, description="读取拍摄当前责任")
    if runtime.state_query is None or not activity["state_query_supported"]:
        (await _close_record_start(runtime, action, exhausted=False, query_key=key))
        return
    if query is not None and (query[0] not in (1, 2)
                              or query[1] >= runtime.query_config.max_attempts):
        (await _close_record_start(runtime, action, exhausted=False, query_key=key))
        return
    if (await runtime.retry_wait_remaining(
            key, runtime.query_config.retry_interval_s,
            maximum=runtime.query_config.max_attempts)) is not None:
        return
    intent = AttemptIntent(
        operation="query", action_id=action["id"], kind=OperationKind.QUERY_ACTIVITY,
        target=AttemptTarget(activity_id=activity["id"]), config=runtime.query_config,
        occurred_at=runtime.wall_us(), query_purpose=QueryPurpose.START_CONFIRMATION)
    _save_inputs = (intent, new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.operations.begin_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"启动核实意图未可靠提交: {receipt.error}", receipt)
    if receipt.value.disposition is not BeginDisposition.GRANTED:
        return
    ticket = receipt.value.ticket
    response = await runtime.state_query.query_state(ControlRequest(
        "query", _binding(action), {"activity_id": str(activity["id"])},
        ticket=ticket, timeout_s=runtime.query_config.timeout_s))
    observed_at, anchor = runtime.wall_us(), runtime.monotonic_ns()
    call, _ = _operation_outcome(response, "activity_status", evidence_type="query_returned")
    runtime.hold_call_result(
        ticket, call, occurred_at=observed_at, returned_ns=anchor,
        preparation=StartConfirmationPreparation(dict(activity), runtime.query_config.max_attempts))
    (await runtime.save_held_result(ticket))


async def _advance_record_start(runtime, action, attempt) -> bool:
    """返回 True 表示启动责任仍占本轮；False 表示进入已确认录像。"""
    from camctl.persistence.repositories.capture_facts import load_start_facts

    if attempt[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = (await _original_ticket(runtime, f"start/{action['id']}", "control"))
        if not (await runtime.recover_attempt(ticket)):
            return True
        attempt = (await runtime.last_attempt(f"start/{action['id']}"))
    facts = (await runtime.database.read(lambda owned, inputs=(action,): load_start_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if action["cancel_requested"]:
        if facts.activity is not None:
            query_key = f"query/start/{action['id']}/{facts.activity['id']}"
            query_attempt = (await runtime.last_attempt(query_key))
            if query_attempt is not None and query_attempt[0] == int(_ATTEMPT_STATUS.RUNNING):
                if not (await runtime.recover_attempt((await _original_ticket(runtime, query_key, "query")))):
                    return True
        if facts.not_started:
            (await _finish_canceled_capture(runtime, action["id"], unstarted=True))
        else:
            await _advance_canceled_capture(runtime, action, attempt)
        return True
    if facts.activity["started_at"] is not None or attempt[1] == int(_EFFECT_STATE.CONFIRMED):
        return False
    if facts.not_started:
        if (await _settle_unstarted_attempt(runtime, action, attempt)):
            return True
        if facts.attempts_used >= runtime.start_config.max_attempts:
            (await _close_record_start(runtime, action, exhausted=True))
            return True
        if (await runtime.retry_wait_remaining(
                f"start/{action['id']}", runtime.start_config.retry_interval_s,
                maximum=runtime.start_config.max_attempts)) is not None:
            return True
        await _record_start_once(runtime, action)
        return True
    await _confirm_record_start(runtime, action, facts)
    return True


def _read_result_scope(runtime):
    from camctl.capture.media_flow import internal_read_scope

    return internal_read_scope(runtime.database, runtime.operations, occurred_at=runtime.wall_us,
        pending_read_results=runtime.pending_read_results, pending_read_business=runtime.pending_read_business,
        pending_read_ends=runtime.pending_read_ends, continuing_read_tickets=runtime.continuing_read_tickets,
        pending_segment_inspection_results=runtime.pending_segment_inspection_results,
        file_executor=runtime.file_executor, evidence=runtime.evidence)


async def _resume_internal_read_results(runtime):
    pending_maps = (runtime.pending_read_business, runtime.pending_read_results, runtime.pending_read_ends,
                    runtime.pending_segment_inspection_results)
    original = tuple(tuple(pending.items()) for pending in pending_maps)
    if not any(original):
        return False
    continuations = dict(runtime.continuing_read_tickets)
    from camctl.capture.media_flow import _save_internal_read_and_settle
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    scope = _read_result_scope(runtime)
    (await retry_read_business(scope))
    for pending in tuple(scope.pending_read_results.values()):
        (await _save_internal_read_and_settle(scope, pending))
    from camctl.capture.segment_inspections import resume_pending_inspection_results
    await resume_pending_inspection_results(scope)
    await retry_read_ends(scope)
    # 原申请被消费或替换才交付进展；保留原对象的强引用以比较实际拥有权。
    if any(len(pending) != len(items)
           or any(pending.get(identity) is not value for identity, value in items)
           for pending, items in zip(pending_maps, original)):
        return True
    current = runtime.continuing_read_tickets
    if current.keys() != continuations.keys():
        return True
    # 同一续读资格仅重建形成时刻时，仍等待原实际读取推进。
    return any(replace(value, occurred_at=continuations[identity].occurred_at) != continuations[identity]
               for identity, value in current.items())


async def _record_handler(action_id: int, context: CaptureRuntime) -> HandlerOutcome | None:
    pending_at_entry = any((
        action_id in context.pending_result_closes, action_id in context.pending_capture_completions,
        action_id in context.pending_baselines, action_id in context.pending_segment_writes,
        any(identity[0] == action_id for identity in context.pending_file_observations),
    ))
    (await context.resume_result_check_closes(action_id))
    (await context.resume_capture_completions(action_id))
    (await context.resume_baselines(action_id))
    (await context.resume_file_observations(action_id))
    read_progress = await _resume_internal_read_results(context)
    start_progress = (await context.resume_start_results(action_id)) is True
    action = (await context.action(action_id))
    if action["status"] in _ACTION_TERMINAL:
        if context.on_recording_deadline is not None:
            context.on_recording_deadline(action_id, None)
        return
    if action["execution_spec_json"].get("result_policy") is not None:
        from camctl.capture.segment_completion import resume_closed_start

        if (await resume_closed_start(context, action)):
            return
    if (await _handle_binding_failure(context, action)):
        return
    open_start = (await context.last_attempt(f"start/{action_id}"))
    if open_start is None:
        if action["cancel_requested"]:
            (await _finish_canceled_capture(context, action_id, unstarted=True))
            return
        await _control_call(context, action, "start_recording", "start_confirmed")
        return
    if await _advance_record_start(context, action, open_start):
        return
    port = _recording_port(context)
    canceled = bool(action["cancel_requested"])
    state = (await port.recording_state(action_id))
    decision = decide_recording_next(
        state,
        RecordingFacts(canceled=canceled),
    )
    if context.on_recording_deadline is not None:
        context.on_recording_deadline(
            action_id, state.stop_target_ns if decision.phase is RecordingPhase.WAIT_RECORD else None)
    if decision.phase is RecordingPhase.NOT_RUNNING:
        if canceled:
            # 启动未确认时取消：不重新启动，收场归启动核实链。
            return
        # 已保存确认不得转入新的 START。缺少中段一致事实须暴露。
        raise ConsistencyError("已确认录像缺少可继续的录像状态")
        return
    if decision.phase is RecordingPhase.READY_TO_STOP:
        if await _confirm_stop_by_query(context, action):
            # 停止核实本轮已收场或占用：重入按已保存事实推进。
            return
        step = await _stop_call(context, action, normal_recording=not canceled)
        if step.phase == "confirmed":
            # 停止确认：活动以可靠停止事实收场，重入进入终态分支。
            (await _conclude_activity(context, action_id))
            return await _record_handler(action_id, context)
        if step.phase == "stop_not_granted" and step.detail == "budget_exhausted" and not canceled:
            (await _settle_stop_exhausted(context, action))
        return
    if decision.phase is RecordingPhase.RECONCILE_REQUIRED:
        # 锚点随既往会话失效：按已保存启动墙钟与当前可信墙钟对账。
        await _reconcile_recording(context, action)
        return
    if decision.phase is RecordingPhase.STOP_EXHAUSTED:
        if canceled:
            # 取消触发的停止预算耗尽归取消收场链，不在本分支收场。
            return
        (await _settle_stop_exhausted(context, action))
        return
    if decision.phase not in (RecordingPhase.CONTROL_COMPLETE,
                              RecordingPhase.VERIFY_FILE_COMPLETE):
        # 等计时；在途停止由尝试收场后重入推进。
        if decision.phase is RecordingPhase.WAIT_RECORD:
            return HandlerOutcome("waiting", wakeup_required=(
                pending_at_entry or read_progress or start_progress
                or open_start[0] == int(_ATTEMPT_STATUS.RUNNING)))
        return
    (await _conclude_activity(context, action_id))
    if canceled:
        # 停止已确认后取消生效：终止后续核验，放弃本次录像内容。
        (await _finish_canceled_capture(context, action_id))
        return
    await _advance_recording_outcome(context, action, decision)
    # 判定待定（必要处理未结束）时等待媒体链下次推进。


async def _load_processing_row(runtime: CaptureRuntime, action_id: int):
    def read(owned):
        """装载录像处理行的决定、进度与原片归属；无责任行返回 None。"""
        with closing(owned.connection.execute(
            "SELECT id, check_decision, check_state, media_json, repair_state,"
            " repair_output_file_id, source_device_file_id"
            " FROM recording_processing WHERE action_id = ?",
            (action_id,),
        )) as cursor:
            return cursor.fetchone()
    return await runtime.database.read(read, description="读取拍摄当前责任")


async def _local_recording_listing(runtime: CaptureRuntime, action):
    """原控制和读取均可靠结束时，装载单次录像的完整本地输入。"""
    if (action["type"] != int(_ACTION_TYPE.CAMERA_RECORD)
            or action["cancel_requested"] or action["status"] in _ACTION_TERMINAL):
        return None
    def read(owned):
        with closing(owned.connection.execute(
        "SELECT f.identity_key,f.completion_state,f.source_action_id,c.verification_state,"
        " c.committed_bytes,c.source_size,c.target_sha256,h.owner_action_id,h.size_bytes,h.sha256,"
        " h.retention_state,h.cleanup_state,r.id,r.status,a.activity_state,a.id"
        " FROM recording_processing p JOIN device_files f ON f.id=p.source_device_file_id"
        " JOIN file_copies c ON c.processing_id=p.id AND c.source_device_file_id=f.id"
        " JOIN intermediate_files h ON h.id=c.target_file_id"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " JOIN device_activities a ON a.action_id=p.action_id WHERE p.action_id=?",
        (int(_RUN_KIND.READ_FILE), action["id"]),
    )) as cursor:
            return cursor.fetchone()
    row = await runtime.database.read(read)
    if row is None:
        return None
    (identity, completion, owner, verification, committed, source_size, target_sha,
     host_owner, host_size, host_sha, retention, cleanup, run_id, run_status, activity,
     activity_id) = row
    # 集合完成依据仍不充分且核实预算未耗尽时，重试等待到期后必须
    # 重新观察设备文件（继续有限核实）；等待期内的推进沿用已保存
    # 观察，媒体链可继续推进而不消耗核实名额。
    if (await runtime.retry_wait_remaining(
            f"results/{activity_id}", runtime.check_config.retry_interval_s,
            maximum=runtime.check_config.max_attempts)) is None:
        return None
    ready = enum_for("file_copies.verification_state")
    if (completion != int(enum_for("device_files.completion_state").COMPLETE)
            or owner != action["id"] or host_owner != action["id"]
            or verification not in (int(ready.MATCHED), int(ready.SOURCE_CHECKSUM_UNAVAILABLE))
            or committed != source_size or host_size != source_size or target_sha is None or host_sha != target_sha
            or retention != int(enum_for("intermediate_files.retention_state").REQUIRED)
            or cleanup != int(enum_for("intermediate_files.cleanup_state").NOT_NEEDED)
            or run_status != int(enum_for("operation_runs.status").SUCCEEDED)
            or activity != int(enum_for("device_activities.activity_state").ENDED)):
        return None
    # START、STOP 或 QUERY 仍需设备时，原绑定规则继续负责这些调用。
    required = (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT 1 FROM operation_runs WHERE action_id=? AND kind IN (?,?,?) AND status IN (1,2) LIMIT 1",
        (action["id"], int(_RUN_KIND.START), int(_RUN_KIND.STOP), int(_RUN_KIND.QUERY_ACTIVITY))).fetchone()))
    unfinished = (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND (t.status=? OR t.result_json IS NULL) LIMIT 1",
        (action["id"], int(_ATTEMPT_STATUS.RUNNING))).fetchone()))
    actual = (await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT status,result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (run_id,)).fetchone()))
    if (required is not None or unfinished is not None or actual is None
            or actual[0] != int(_ATTEMPT_STATUS.SUCCEEDED) or actual[1] is None):
        return None
    listing = (await _saved_result_listing(runtime, action["id"]))
    from camctl.capture.files import file_identity_key

    if not await any_result(entry.complete and entry.kind is FileKind.VIDEO and entry.paired_identity is None
               and file_identity_key(action["device_id"], action["driver_id"], entry.identity) == identity
               async for entry in iter_result_entries(listing.entries)):
        return None
    return listing


async def _save_source_file(
    runtime: CaptureRuntime, processing_id: int, source_file_id: int) -> None:
    """首次把可靠原片归属到处理行；媒体链读取资格以此对齐。"""
    _save_inputs = (SourceFileSave(
            processing_id=processing_id,
            source_device_file_id=source_file_id,
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.save_source_file(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)


async def _save_not_needed_decision(
    runtime: CaptureRuntime, processing_id: int, target_duration_ms: int) -> None:
    """连续控制完成：固定无需检查决定，控制依据即成功依据。"""
    _save_inputs = (CheckDecisionSave(
            processing_id=processing_id,
            decision=CheckDecisionChoice.NOT_NEEDED,
            basis=CheckBasis(
                reason=CheckReason.CONTINUOUS_CONTROL_COMPLETE,
                target_duration_ms=target_duration_ms),
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.save_check_decision(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)


async def _save_excess_decisions(
    runtime: CaptureRuntime, processing_id: int, target_duration_ms: int,
    control: RecoveredControlDecision) -> None:
    """异常多录：无需检查但达到修复门槛，两决定一次固定。"""
    elapsed = control.control_elapsed_ns
    _save_inputs = (CheckDecisionSave(
            processing_id=processing_id,
            decision=CheckDecisionChoice.NOT_NEEDED,
            basis=CheckBasis(
                reason=CheckReason.EXCESS_DURATION_CHECK,
                target_duration_ms=target_duration_ms,
                control_elapsed_ns=elapsed),
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.capture.save_check_decision(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)
    _save_inputs = (RepairDecisionSave(
            processing_id=processing_id,
            decision=RepairDecisionChoice.PENDING,
            basis=RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=target_duration_ms,
                # 门槛秒数保存实际比较门槛（目标时长加修复余量），
                # 与恢复停止判定使用的门槛一致（operation-fields.md
                # threshold_s 字段语义）。
                threshold_s=(Decimal(target_duration_ms) / Decimal(1_000)
                             + runtime.repair_margin_s),
                control_elapsed_ns=elapsed),
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    repair = (await runtime.database.write(lambda owned: runtime.capture.save_repair_decision(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if repair.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(AssertionError, "拍摄业务保存未可靠完成", repair)


def _media_responsibility_open(row) -> bool:
    """需要媒体链推进的责任：待执行的检查（仅需要检查时）或修复。

    无需检查且未达修复门槛的处理行没有媒体责任，控制完成即按结
    果集合核实收场。
    """
    return ((row[1] == int(_CHECK_DECISION.REQUIRED)
             and row[2] in (int(_CHECK_STATE.NOT_PERFORMED),
                            int(_CHECK_STATE.RUNNING)))
            or row[4] in (int(_REPAIR_STATE.PENDING),
                          int(_REPAIR_STATE.RUNNING)))


async def _recording_confirmation_at(runtime: CaptureRuntime, action_id: int) -> int | None:
    """按原冻结返回含义读取确认墙钟，不把 sent_at 当作实际开始观察。"""
    action = (await runtime.action(action_id))
    column = ("sent_at" if action["execution_spec_json"].get("start_return_meaning") == int(StartReturn.SENT)
              else "started_at")
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute(f"SELECT {column} FROM device_activities WHERE action_id = ?", parameters)) as cursor:
            return cursor.fetchone()
    row = await runtime.database.read(read, description="读取拍摄当前责任")
    return None if row is None else row[0]


async def _stop_confirmed_at(runtime: CaptureRuntime, action_id: int) -> int | None:
    def read(owned):
        """读取停止责任最近可靠确认尝试的结果事件墙钟。"""
        with closing(owned.connection.execute(
            "SELECT e.occurred_at FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id = r.id"
            " JOIN history_events e ON e.id = a.result_event_id"
            " WHERE r.responsibility_key = ? AND a.status <> ? AND a.effect_state = ?"
            " ORDER BY a.id DESC LIMIT 1",
            (f"stop/{action_id}", int(_ATTEMPT_STATUS.RUNNING),
             int(_EFFECT_STATE.CONFIRMED)),
        )) as cursor:
            row = cursor.fetchone()
        return None if row is None else int(row[0])
    return await runtime.database.read(read, description="读取拍摄当前责任")


async def _record_media_source(registered, entries, *, source_file_id=None):
    """只保留一个完整原片及其原格式，拒绝由目录顺序选择修复输入。"""
    selected, count = None, 0
    async for (file, identity), entry in registered_observations(registered, entries):
        if file.file_id != entry.identity:
            raise ConsistencyError("录像来源与原结果条目顺序不一致")
        if (entry.complete and entry.kind is FileKind.VIDEO
                and entry.size_bytes is not None and entry.size_bytes > 0
                and entry.paired_identity is None and entry.format_id != "lrf"):
            count += 1
            if count == 1:
                selected = (identity, entry.format_id)
    if count != 1:
        return None
    if source_file_id is not None and selected[0] != source_file_id:
        raise ConsistencyError("录像处理不能改选已经固定的原片身份")
    return selected


async def _record_file_completion_context(runtime: CaptureRuntime, action_id: int) -> dict | None:
    """原 START/STOP 均可靠时，冻结 RESULT 采用的停止与等待依据。"""
    action = (await runtime.action(action_id))
    wait_ms = action["execution_spec_json"].get("file_completion_wait_ms")
    if wait_ms is None:
        return None
    start = (await runtime.last_attempt(f"start/{action_id}"))
    if (start is None or start[0] == int(_ATTEMPT_STATUS.RUNNING)
            or start[1] != int(_EFFECT_STATE.CONFIRMED)):
        return None
    activity_id = (await _activity_id_of(runtime, action_id))
    def read(owned, parameters=(f"stop/{action_id}",)):
        with closing(owned.connection.execute("SELECT r.kind,r.action_id,r.activity_id,a.status,a.effect_state,"
        " a.result_event_id,a.error_json,a.result_json FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.responsibility_key=? ORDER BY a.id DESC LIMIT 1", parameters)) as cursor:
            return cursor.fetchone()
    stopped = await runtime.database.read(read, description="读取拍摄当前责任")
    if (stopped is None or stopped[3] == int(_ATTEMPT_STATUS.RUNNING)
            or stopped[4] != int(_EFFECT_STATE.CONFIRMED)):
        return (await _natural_file_completion_context(runtime, action_id))
    if (stopped[0] != int(_RUN_KIND.STOP) or stopped[1] != action_id
            or stopped[2] != activity_id or stopped[5] is None):
        raise ConsistencyError("原停止结果与录像动作、活动或停止责任不符")
    original, occurred_at = (await runtime.database.read(lambda owned, inputs=(stopped[5],), options=dict(): runtime.capture.read_confirmed_stop(*inputs, owned, **options), description="读取已保存拍摄事实"))
    if original["id"] != activity_id or original["action_id"] != action_id:
        raise ConsistencyError("原停止确认改变了录像活动或固定设备绑定")
    context = {
        "activity_id": str(activity_id), "stop_result_event_id": int(stopped[5]),
        "stop_returned_at_us": int(occurred_at),
        "stop_response": {"status": int(stopped[3]), "effect_state": int(stopped[4]),
            "error": None if stopped[6] is None else parse_exact_json(stopped[6]),
            "result": parse_exact_json(stopped[7])},
        "file_completion_wait_ms": wait_ms, "prior_completion": None,
    }
    if action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE):
        context["kind"] = "timelapse_stop"
    context["prior_completion"] = (await _prior_record_file_completion(runtime, action_id, context))
    return context


async def _natural_file_completion_context(runtime: CaptureRuntime, action_id: int) -> dict | None:
    """只从原可靠 SENT、固定目标和已保存等待完成事件建立自然上下文。"""
    from camctl.devices.capture_completion import CompletionContextKind, validate_completion_context

    action = (await runtime.action(action_id))
    spec = action["execution_spec_json"]
    if (action["type"] != int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            or not spec["wait_after_send"]
            or spec["completion_mode"] != int(CompletionMode.TIME_AND_OUTPUTS)):
        return None
    activity = (await runtime.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(runtime, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    wait_event = activity["wait_completed_event_id"]
    if activity["sent_at"] is None or wait_event is None:
        return None
    def read(owned, parameters=(f"start/{action_id}", action_id, activity["id"])):
        with closing(owned.connection.execute("SELECT a.status,a.effect_state,a.result_event_id,a.error_json,a.result_json"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=?"
        " ORDER BY a.id DESC LIMIT 1", parameters)) as cursor:
            return cursor.fetchone()
    start = await runtime.database.read(read, description="读取拍摄当前责任")
    if start is None or start[0] == int(_ATTEMPT_STATUS.RUNNING) or start[1] != int(_EFFECT_STATE.CONFIRMED):
        return None
    def read(owned, parameters=(wait_event,)):
        with closing(owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=?", parameters)) as cursor:
            return cursor.fetchone()
    waited = await runtime.database.read(read, description="读取拍摄当前责任")
    if waited is None:
        raise ConsistencyError("延时文件完成缺少原可靠等待事件")
    if activity["result_wait_margin_ms"] != spec["result_wait_margin_ms"]:
        raise ConsistencyError("自然文件完成使用的余量与原冻结规则不符")
    context = {"kind": CompletionContextKind.TIMELAPSE_NATURAL.value,
        "activity_id": str(activity["id"]), "start_result_event_id": int(start[2]),
        "start_response": {"status": int(start[0]), "effect_state": int(start[1]),
            "error": None if start[3] is None else parse_exact_json(start[3]),
            "result": parse_exact_json(start[4])},
        "sent_at_us": activity["sent_at"], "wait_completed_event_id": wait_event,
        "wait_completed_at_us": waited[0], "target_duration_ms": spec["target_duration_ms"],
        "result_wait_margin_ms": activity["result_wait_margin_ms"],
        "extra_wait_ms": activity["extra_wait_ms_used"],
        "expected_check_at_us": activity["expected_check_at"],
        **({"startup_margin_ms": spec["startup_margin_ms"]} if "startup_margin_ms" in spec else {})}
    return validate_completion_context(context, activity_id=str(activity["id"]))


async def _prior_record_file_completion(runtime: CaptureRuntime, action_id: int, context: dict) -> dict | None:
    """沿同活动可靠 RESULT 页读取完成等待；每次只保留一批和一个依据。"""
    activity_id = int(context["activity_id"])
    responsibility = f"results/{activity_id}"
    before_attempt = None
    while True:
        def read_attempts(owned):
            with closing(owned.connection.execute(
                "SELECT r.id,a.attempt_no FROM operation_runs r"
                " JOIN operation_attempts a ON a.run_id=r.id"
                " WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=?"
                " AND r.kind=? AND a.result_last_page_event_id IS NOT NULL"
                " AND (? IS NULL OR a.attempt_no<?) ORDER BY a.attempt_no DESC LIMIT 128",
                (responsibility, action_id, activity_id, int(_RUN_KIND.CHECK_CAPTURE_RESULTS), before_attempt, before_attempt),
            )) as cursor:
                return tuple(cursor.fetchall())
        attempts = await runtime.database.read(read_attempts)
        if not attempts:
            break
        for run_id, attempt_no in attempts:
            ticket = AttemptTicket(attempt_no, "result", str(activity_id), responsibility, run_id)
            cursor, completion = None, None
            while True:
                pages = (await runtime.database.read(lambda owned, inputs=(ticket, cursor, 32), options=dict(): runtime.capture.read_result_pages(*inputs, owned, **options), description="读取已保存拍摄事实"))
                for saved in pages.items:
                    actual_wait = await runtime.database.read(lambda owned: completed_wait_from_page(
                        saved, context, repository=runtime.capture, owned=owned))
                    if actual_wait is not None:
                        completion = actual_wait
                if pages.next_cursor is None:
                    break
                cursor = pages.next_cursor
            if completion is not None:
                return completion
        before_attempt = attempts[-1][1]
    return None


async def _recovered_control_facts(
    runtime: CaptureRuntime, action) -> RecoveredControlFacts:
    """恢复停止后控制完成依据判定的事实装载。"""
    action_id = action["id"]
    return RecoveredControlFacts(
        session_anchor=_recording_port(runtime).has_session_anchor(action_id),
        started_at_us=(await _recording_confirmation_at(runtime, action_id)),
        stop_confirmed_at_us=(await _stop_confirmed_at(runtime, action_id)),
        target_duration_ms=_target_duration_ms(action),
        repair_margin_s=runtime.repair_margin_s,
        startup_margin_ms=action["execution_spec_json"].get("startup_margin_ms", 0))


async def _reconcile_recording(
    context: CaptureRuntime, action) -> None:
    """跨会话对账：可信计时证明满足即停止，未满足等待剩余时长。

    计时不可靠（启动确认墙钟缺失）归时钟异常会话的保守收场接线，
    本处不推测已经录够；停止确认后重入处理器进入终态判定。
    """
    phase = decide_recording_reconciliation(ReconciliationFacts(
        started_at_us=(await _recording_confirmation_at(context, action["id"])),
        target_duration_ms=_target_duration_ms(action),
        trusted_now_us=context.wall_us(),
        startup_margin_ms=action["execution_spec_json"].get("startup_margin_ms", 0)))
    if phase is not ReconciliationPhase.TIMING_SATISFIED:
        return
    if not await _observe_activity_for_reconcile(context, action):
        # 归属或连续性未确认、活动已空闲收场或核实占本轮：不进入
        # 墙钟成功分支。
        return
    if await _confirm_stop_by_query(context, action):
        # 对账会话的未确认停止先经原停止核实责任查询收场。
        return
    step = await _stop_call(context, action)
    if step.phase == "confirmed":
        # 对账满足的停止确认：活动以可靠停止事实收场，重入终态判定。
        (await _conclude_activity(context, action["id"]))
        await _record_handler(action["id"], context)
    elif step.phase == "stop_not_granted" and step.detail == "budget_exhausted":
        (await _settle_stop_exhausted(context, action))


async def _save_winddown_progress(
    runtime: CaptureRuntime, action) -> bool:
    """保存保守停止后的等待阶段；列举不可靠时保持未定等待后续会话。

    登记结果观察（归属与写完事实）、固定计时证据不足的检查决定并
    关联源文件；不启动媒体链、不登记正式产物，动作保持执行中表
    达“已停止，等待正常会话处理”。
    """
    action_id = action["id"]
    (await runtime.resume_file_observations(action_id))
    row = (await _load_processing_row(runtime, action_id))
    if row is None:
        return False
    listing = await _listing_round(runtime, action_id)
    if listing.phase not in (ListingPhase.LISTED, ListingPhase.CLOSED):
        return False
    entries = listing.entries
    registered = (await _register_listing(runtime, action_id, listing))
    if isinstance(registered, RegisteredSourceFiles):
        entries = registered.entries
    # 保守收场只保存待检查进度；核实与处理尚未终局，沿原责任保留等待。
    (await _finish_listing_result(runtime, listing, retry_wait=True))
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value
    ):
        return True
    media_source = await _record_media_source(registered, entries, source_file_id=row[6])
    source_file_id = None if media_source is None else media_source[0]
    if source_file_id is None:
        # 尚无写完的完整原片可归属：不能用等待阶段代替文件证据。
        return False
    if row[1] == int(_CHECK_DECISION.UNDETERMINED):
        _save_inputs = (CheckDecisionSave(
                processing_id=int(row[0]),
                decision=CheckDecisionChoice.REQUIRED,
                basis=CheckBasis(
                    reason=CheckReason.INSUFFICIENT_TIMING,
                    target_duration_ms=_target_duration_ms(action)),
                occurred_at=runtime.wall_us()), new_operation_key())
        _save_options = dict()
        receipt = (await runtime.database.write(lambda owned: runtime.capture.save_check_decision(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)
    if row[6] is None:
        (await _save_source_file(runtime, int(row[0]), source_file_id))
    return True


async def advance_winddown(
    action_id: int, context: CaptureRuntime, first_seen: dict[int, int],
    *, wait_cap_s: Decimal,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> HandlerOutcome:
    """时钟异常会话的保守收场推进入口。

    前提是受限会话：墙钟不可信，录像由既往会话确认启动且锚点随
    会话失效。未取消的录像额外等待 min(目标时长, wait_cap_s)（本
    会话单调钟，first_seen 登记首次观察读数）后放弃计时，按原停
    止预算停止并保存等待阶段；停止尝试失败按重试间隔在预算内重
    试。取消已生效的录像不经计时立即停止并按取消规则收场。停止
    已确认时不重复停止：取消则收终态，否则保存等待阶段；启动未
    确认、在途停止、预算耗尽及本会话锚点分别归各自责任链。
    """
    (await context.resume_result_check_closes(action_id))
    (await context.resume_capture_completions(action_id))
    (await context.resume_file_observations(action_id))
    action = (await context.action(action_id))
    if action["status"] in _ACTION_TERMINAL:
        return HandlerOutcome("already_terminal")
    if action["cancel_requested"]:
        start = (await context.last_attempt(f"start/{action_id}"))
        if start is None:
            (await _finish_canceled_capture(context, action_id, unstarted=True))
        elif not (await _settle_unstarted_attempt(context, action, start)):
            await _advance_canceled_capture(context, action, start)
        return HandlerOutcome("canceled_final" if (await context.action(action_id))["status"]
                              in _ACTION_TERMINAL else "cancel_pending")
    port = _recording_port(context)
    canceled = bool(action["cancel_requested"])
    decision = decide_recording_next(
        (await port.recording_state(action_id)), RecordingFacts(canceled=canceled))
    if decision.phase is RecordingPhase.NOT_RUNNING:
        return HandlerOutcome("not_running")
    if decision.phase is RecordingPhase.STOP_EXHAUSTED:
        (await _settle_stop_exhausted(context, action))
        return HandlerOutcome(decision.phase.value)
    if decision.phase in (RecordingPhase.STOP_IN_FLIGHT,
                          RecordingPhase.WAIT_RECORD):
        # 在途停止等待实际结束；本会话锚点属于正常
        # 计时，不满足保守收场前提。
        return HandlerOutcome(decision.phase.value)
    if decision.phase in (RecordingPhase.CONTROL_COMPLETE,
                          RecordingPhase.VERIFY_FILE_COMPLETE):
        # 停止已确认（既往或本流程）：不重复停止，不启动后处理。
        (await _conclude_activity(context, action_id))
        if canceled:
            (await _finish_canceled_capture(context, action_id))
            return HandlerOutcome("canceled_final")
        saved = await _save_winddown_progress(context, action)
        return HandlerOutcome(
            "progress_saved" if saved else "progress_deferred")
    if not canceled:
        # 保守等待只用本会话单调钟，不接续既往会话读数。
        anchor_ns = first_seen.setdefault(action_id, context.monotonic_ns())
        plan = decide_conservative_winddown(WinddownFacts(
            first_seen_ns=anchor_ns,
            monotonic_now_ns=(await port.recording_state(action_id)).monotonic_now_ns,
            target_duration_ms=_target_duration_ms(action),
            recovery_wait_cap_s=wait_cap_s))
        if plan.phase is WinddownPhase.WAIT:
            await sleep(plan.remaining_s or 0.0)
    retried = False
    while True:
        decision = decide_recording_next(
            (await port.recording_state(action_id)),
            RecordingFacts(canceled=canceled, timing_waived=True))
        if decision.phase is not RecordingPhase.READY_TO_STOP:
            if decision.phase is RecordingPhase.STOP_EXHAUSTED:
                (await _settle_stop_exhausted(context, action))
            return HandlerOutcome(decision.phase.value)
        if retried:
            # 上一次尝试失败：按重试间隔等待后再试，预算内有限重试。
            await sleep(float(context.stop_config.retry_interval_s))
        retried = True
        step = await _stop_call(context, action)
        if step.phase == "stop_not_granted" and step.detail == "budget_exhausted":
            (await _settle_stop_exhausted(context, action))
            return HandlerOutcome(RecordingPhase.STOP_EXHAUSTED.value)
        if step.phase == "confirmed":
            # 保守停止确认：活动以可靠停止事实收场，保存等待阶段
            # 或取消终态，交给后续正常会话。
            (await _conclude_activity(context, action_id))
            if canceled:
                (await _finish_canceled_capture(context, action_id))
                return HandlerOutcome("canceled_final")
            saved = await _save_winddown_progress(context, action)
            return HandlerOutcome(
                "progress_saved" if saved else "progress_deferred")
        # 失败或仅发送：重入循环按预算判定是否重试。


async def _advance_recording_outcome(
    context: CaptureRuntime, action, decision) -> None:
    """建立检查决定、推进媒体链并按录像成功标准收场。

    控制完成固定无需检查决定；计时证据不足的检查决定由跨会话对
    账收场建立，此前保持待定。结果列举按 results 责任的有限轮次
    推进，会话内已观察的列举事实直接复用；判定装载已保存的检查
    时长与媒体问题，修复成功的成品与原片同事务登记。
    """
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    ):
        from camctl.capture.action6_completion import advance_action6_segmented_recording

        await advance_action6_segmented_recording(context, action, decision)
        return
    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value
    ):
        from camctl.capture.segment_completion import advance_segmented_recording

        await advance_segmented_recording(context, action, decision)
        return
    action_id = action["id"]
    row = (await _load_processing_row(context, action_id))
    if row is None:
        # 处理责任尚未建立；终态等待责任建立后的推进。
        return
    control_complete = decision.phase is RecordingPhase.CONTROL_COMPLETE
    if row[1] == int(_CHECK_DECISION.UNDETERMINED):
        if decision.phase not in (RecordingPhase.CONTROL_COMPLETE,
                                  RecordingPhase.VERIFY_FILE_COMPLETE):
            # 只有可靠停止后，原 START/STOP 的时刻才决定控制时长。
            return
        target_ms = _target_duration_ms(action)
        control = decide_recovered_control((await _recovered_control_facts(context, action)))
        if control is None:
            # 计时证据缺失：不折叠为连续控制完成，保持未定等待对账。
            return
        if control.reason is RecoveredControlReason.EXCESS_DURATION:
            (await _save_excess_decisions(context, int(row[0]), target_ms, control))
        elif control.reason is RecoveredControlReason.DURATION_UNCERTAIN:
            _save_inputs = (CheckDecisionSave(
                processing_id=int(row[0]), decision=CheckDecisionChoice.REQUIRED,
                basis=CheckBasis(reason=CheckReason.INSUFFICIENT_TIMING,
                    target_duration_ms=target_ms), occurred_at=context.wall_us()), new_operation_key())
            _save_options = dict()
            receipt = (await context.database.write(lambda owned: context.capture.save_check_decision(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(AssertionError, "拍摄业务保存未可靠完成", receipt)
        else:
            (await _save_not_needed_decision(context, int(row[0]), target_ms))
        row = (await _load_processing_row(context, action_id))
    ticket = None
    cached = (None if context.listing_cache is None
              else context.listing_cache.get(action_id))
    if cached is not None:
        # 读取已保存的列举事实不构成新轮次；等待中的推进不消耗名额。
        entries, registered = cached
        original = (await _original_ticket(context, f"results/{(await _activity_id_of(context, action_id))}", "result"))
        if original is None:
            raise ConsistencyError("缓存结果缺少原核实责任")
        if isinstance(registered, RegisteredSourceFiles) and isinstance(registered.entries, SourceResultEntries):
            current = registered.entries.current_entries
            if current is not None:
                fresh = SavedResultEntries(context.capture, context.database, current.last_ref)
                current = (QualifiedResultEntries(fresh, current.lower_bound, source_qualified=current.source_qualified)
                    if isinstance(current, QualifiedResultEntries) else fresh)
            entries = SourceResultEntries(context.capture, context.database, original, current_entries=current)
            registered = RegisteredSourceFiles(entries)
        listing = None if original is None else (await _held_listing(context, original))
        if listing is not None:
            ticket = listing.ticket
        else:
            listing = (await _saved_result_listing(context, action_id))
    else:
        listing = (await _local_recording_listing(context, action))
        if listing is None:
            listing = await _listing_round(context, action_id)
        action = (await context.action(action_id))
        if action["cancel_requested"]:
            # 原实际返回仍沿原票据保存，取消只终止后续核验及内容登记。
            if listing.phase is ListingPhase.LISTED:
                (await _finish_listing_result(context, listing))
            (await _finish_canceled_capture(context, action_id))
            return
        if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
            # 在途或本轮列举失败：已保存实际结果与重试等待。
            return
        unconfirmed = (listing.phase is ListingPhase.CLOSED
            and (await context.database.read(lambda owned, inputs=("operation_runs", listing.ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["status"]
                == int(enum_for("operation_runs.status").UNCONFIRMED))
        if listing.phase is ListingPhase.EXHAUSTED or unconfirmed:
            saved = listing if unconfirmed else (await _saved_result_listing(context, action_id))
            registered = (await _register_listing(context, action_id, saved))
            if not unconfirmed:
                # 录像仅收场核实责任，不生成延时摄影的集合结论。
                if action_id not in context.pending_result_closes:
                    context.pending_result_closes[action_id] = PendingResultCheckClose(
                        new_operation_key(), ResultRunClose(
                            action_id=action_id, occurred_at=context.wall_us()))
                (await context.resume_result_check_closes(action_id))
            (await _finish_capture(context, action_id, saved.entries, FileKind.VIDEO,
                            registered=registered, failure=(await _unconfirmed_failure(context, action_id))))
            return
        entries = listing.entries
        ticket = None if listing.already_saved else listing.ticket
        registered = (await _register_listing(context, action_id, listing))
        if isinstance(registered, RegisteredSourceFiles):
            entries = registered.entries
    if "file_completion_wait_ms" in action["execution_spec_json"]:
        state = (await _recording_port(context).recording_state(action_id))
        control_complete = state.stop_confirmed and state.file_complete_guaranteed
    files = (await assess_registered_files(registered, _product_requirements(action, FileKind.VIDEO), set_finalized=_listing_finalized(listing)))
    media_source = await _record_media_source(registered, entries, source_file_id=row[6])
    source_file_id = None if media_source is None else media_source[0]
    if cached is None and files.is_complete and context.listing_cache is not None and (
            row[6] is not None or source_file_id is not None):
        # 完整原片已归属且媒体链推进中：已保存观察足以继续装载，等
        # 待装配不再重复列举；产物未齐的列举每轮重新观察文件到达。
        context.listing_cache[action_id] = (entries, registered)
    if _media_responsibility_open(row):
        # 检查或修复责任未终局时经媒体端口推进；媒体端口未装配时
        # 等待装配会话。已归属原片沿用归属事实，未归属时以本次观
        # 察的完整原片首次关联。
        source = row[6] if row[6] is not None else source_file_id
        if source is not None and context.media is not None:
            if row[6] is None:
                (await _save_source_file(context, int(row[0]), source))
            await run_recording_media(
                context.media, action_id, int(row[0]), source,
                target_extension="osv" if media_source is not None and media_source[1] == "osv" else "mp4")
            row = (await _load_processing_row(context, action_id))
            action = (await context.action(action_id))
            if action["cancel_requested"]:
                (await _finish_listing_result(context, listing))
                (await _finish_canceled_capture(context, action_id))
                return
    media_json = parse_exact_json(row[3]) if row[3] else None
    facts = RecordingOutcomeFacts(
        processing_id=int(row[0]),
        control_complete=control_complete,
        check_decision=int(row[1]),
        check_state=int(row[2]),
        check_duration_s=(saved_check_duration(media_json)
                          if row[2] == int(_CHECK_STATE.COMPLETED) else None),
        check_issues=(bool(media_json.get("issues")) if media_json else False) or bool(
            (await context.database.read(lambda owned, inputs=(int(row[0]), _target_duration_ms(action)): saved_osv_input_issues(owned.connection, *inputs), description="读取拍摄直接事实"))),
        input_unavailable=(
            _media_responsibility_open(row) and row[6] is None
            and source_file_id is None),
        repair_state=int(row[4]),
        target_duration_ms=_target_duration_ms(action),
    )
    result = decide_recording_result(facts)
    output_failure = (await _output_failure(context, action_id, files))
    if result.kind.value == "failed" or (
            result.kind.value == "succeeded" and (files.is_complete or output_failure is not None)):
        results_run_id = None
        if ticket is None and files.is_complete:
            original = (await _original_ticket(
                context, f"results/{(await _activity_id_of(context, action_id))}", "result"))
            if original is None:
                raise ConsistencyError("完整录像文件缺少原结果核实责任")
            results_run_id = original.run_id
        # 原调用与文件范围独立闭合；CLOSED 沿原末页补齐范围事实。
        (await _finish_listing_result(context, listing, end_run=RunOutcome.SUCCEEDED,
                               finalize_output_set=True))
        if (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["activity_state"] == 3:
            (await _release_occupancy(context, action_id))
        if result.kind.value == "succeeded" and output_failure is None:
            (await _finish_capture(
                context, action_id, entries, FileKind.VIDEO,
                registered=registered,
                repair_file_id=int(row[5]) if row[4] == int(
                    _REPAIR_STATE.SUCCEEDED) and row[5] is not None else None,
                results_run_id=results_run_id, set_finalized=_listing_finalized(listing)))
        else:
            (await _finish_capture(context, action_id, entries, FileKind.VIDEO,
                            registered=registered, failure=result.failure if result.kind.value == "failed" else output_failure,
                            results_run_id=results_run_id))
    elif ticket is not None:
        # 终局依据尚不齐备：本轮成功结果与重试等待共同保存。
        (await _finish_listing_result(context, listing, retry_wait=True))
    elif (result.kind.value == "succeeded" and listing.already_saved
            and (await context.database.read(lambda owned, inputs=("operation_runs", listing.ticket.run_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["status"]
                not in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE))):
        (await _finish_capture(context, action_id, entries, FileKind.VIDEO, registered=registered,
                        failure=(await _unconfirmed_failure(context, action_id))))


class HostTimerPhase(Enum):
    """主机计时推进结果，目标完成与实际停止分别判断。"""

    WAITING = "waiting"
    CONFIRMED = "confirmed"
    DURATION_UNKNOWN = "duration_unknown"
    COMPLETION_UNKNOWN = "completion_unknown"


async def _advance_host_timer(context: CaptureRuntime, action, attempt) -> HostTimerPhase:
    """原计时到期停止；缺少连续依据时及时停止且目标保持无法确认。"""
    action_id = action["id"]
    if attempt[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = (await _original_ticket(context, f"start/{action_id}", "control"))
        if not (await context.recover_attempt(ticket)):
            return HostTimerPhase.WAITING
        attempt = (await context.last_attempt(f"start/{action_id}"))
    if attempt[1] == int(_EFFECT_STATE.NO_EFFECT):
        # 可靠未启动继续原窗口收场，不能发出不适用的停止命令。
        return HostTimerPhase.WAITING
    activity = (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if activity["activity_state"] == 3:
        return (HostTimerPhase.CONFIRMED if (await context.database.read(lambda owned, inputs=(action_id,), options=dict(): context.capture.read_result_completion(*inputs, owned, **options), description="读取已保存拍摄事实")) is not None else HostTimerPhase.DURATION_UNKNOWN)
    deadline = context.timelapse_deadlines.get(action_id)
    reason = (HostTimerPhase.DURATION_UNKNOWN if deadline is None
              else HostTimerPhase.COMPLETION_UNKNOWN)
    stop = (await context.last_attempt(f"stop/{action_id}"))
    if stop is not None and stop[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = (await _original_ticket(context, f"stop/{action_id}", "stop"))
        if not (await context.recover_attempt(ticket, retry_wait=True)):
            return HostTimerPhase.WAITING
    def read(owned, parameters=(f"stop/{action_id}",)):
        with closing(owned.connection.execute("SELECT status FROM operation_runs WHERE responsibility_key=?", parameters)) as cursor:
            return cursor.fetchone()
    run = await context.database.read(read, description="读取拍摄当前责任")
    if run is not None and run[0] not in (1, 2):
        return reason
    if deadline is not None and context.monotonic_ns() < deadline:
        return HostTimerPhase.WAITING
    step = await _stop_call(context, action, "stop_timelapse")
    if step.phase == "confirmed":
        return (HostTimerPhase.CONFIRMED if (await context.database.read(lambda owned, inputs=(action_id,), options=dict(): context.capture.read_result_completion(*inputs, owned, **options), description="读取已保存拍摄事实")) is not None else HostTimerPhase.DURATION_UNKNOWN)
    if step.phase == "stop_not_granted" and step.detail == "budget_exhausted":
        (await _close_stop_exhausted(context, action))
        return reason
    return HostTimerPhase.WAITING


async def _unconfirmed_result_set(runtime, action_id, reason, occurred_at):
    code = "capture_result_unconfirmed"
    error = {"code": code, "stage": registered_error(code)["stage"],
        "details": {"activity_id": str((await _activity_id_of(runtime, action_id))), "reason": reason}}
    return ResultSetSave(action_id, occurred_at, ResultSetPhase.UNCONFIRMED,
        contract=_RESULT_CONTRACT, observation={"reason": reason},
        capture={"status": "unconfirmed", "error": error}, error=error)


async def _close_host_timer_failed(context, action_id, reason):
    """目标无法确认时有限核实文件，再沿原结论保存失败和完整产物。"""
    listing = await _listing_round(context, action_id)
    if (await context.action(action_id))["cancel_requested"]:
        await _timelapse_handler(action_id, context)
        return
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        (await _close_check_unconfirmed(context, action_id, reason=reason))
        await _finish_timelapse_conclusion(context, action_id)
        return
    if listing.phase is ListingPhase.CLOSED:
        await _finish_timelapse_conclusion(context, action_id)
        return
    registered = (await _register_listing(context, action_id, listing))
    assessment = (await assess_registered_files(registered, _product_requirements((await context.action(action_id)), FileKind.VIDEO), set_finalized=_listing_finalized(listing)))
    if listing.completion_evidence is not None and (assessment.is_complete or assessment.explicitly_unmet):
        (await _confirm_timelapse_results(context, action_id, assessment, listing.entries, None, listing))
        await _finish_timelapse_conclusion(context, action_id)
    elif listing.outcome.error is None and (assessment.is_complete or assessment.explicitly_unmet):
        command = (await _unconfirmed_result_set(context, action_id, reason, listing.occurred_at))
        (await _finish_listing_result(context, listing, end_run=RunOutcome.SUCCEEDED, result_set=command,
                               finalize_output_set=True))
        await _finish_timelapse_conclusion(context, action_id)
    else:
        (await _finish_listing_result(context, listing, retry_wait=True))


def _publish_natural_wait(context, action_id, state, arrangement):
    state.applied = arrangement
    context.timelapse_deadlines[action_id] = arrangement.logical_deadline_ns


async def _save_natural_wait(context, action_id, state):
    """一次保存只采用原完整申请；UNKNOWN 留待原键核实。"""
    async def save_and_apply():
        pending = state.pending
        if isinstance(pending.command, WaitCompletedSave):
            save = context.timelapse.complete_wait
        else:
            save = (context.timelapse.reconfigure_wait if pending.reconfigure
                    else context.timelapse.schedule_wait)
        pending.verified_absent = False
        receipt = await context.database.write(lambda owned: save(pending.command, pending.key, owned), key=pending.key)
        if receipt.kind is DbOutcomeKind.COMPLETED:
            if isinstance(pending.command, ScheduleWait):
                _publish_natural_wait(context, action_id, state, pending.arrangement)
            state.pending = None
            return receipt.value
        if receipt.kind is not DbOutcomeKind.UNKNOWN:
            state.pending = None
        raise db_failure(ConsistencyError, f"延时等待保存未可靠完成（{receipt.kind.value}）: {receipt.error}", receipt)
    return await settle_required(save_and_apply())


async def _resolve_natural_wait(context, action_id):
    """任何依赖推进前，结束未决事务并在合法事务内核实原键。"""
    async def save_and_apply():
        state = context.natural_wait_states.get(action_id)
        if state is None or state.pending is None or state.pending.verified_absent:
            return
        pending = state.pending
        _save_inputs = (pending.command, pending.key)
        _save_options = dict(reconfigure=pending.reconfigure)
        receipt = (await context.database.write(lambda owned: context.timelapse.verify_wait(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"原延时等待申请尚未可靠核实: {receipt.error}", receipt)
        if receipt.value is not None:
            if isinstance(pending.command, ScheduleWait):
                _publish_natural_wait(context, action_id, state, pending.arrangement)
            state.pending = None
        elif isinstance(pending.command, ScheduleWait):
            # 安排申请已可靠确认未提交，原样重送后才处理最新 E。
            (await _save_natural_wait(context, action_id, state))
        else:
            # 完成尚不存在。先重判当前 E；仍到期且安排未变才原样重送。
            pending.verified_absent = True
    return await settle_required(save_and_apply())


async def _advance_timelapse_natural_wait(context: CaptureRuntime, action) -> int | None:
    """正常与有限取消共用原 SENT 的映射、保存和完成责任。"""
    action_id = action["id"]
    (await _resolve_natural_wait(context, action_id))
    activity = (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if activity["sent_at"] is None:
        return None
    if activity["wait_completed_event_id"] is not None:
        return activity["wait_completed_event_id"]
    config = replace(context.wait_config(action),
        startup_margin_ms=action["execution_spec_json"].get("startup_margin_ms", 0))
    timing = CaptureTiming(config.target_duration_ms, config.startup_margin_ms,
        config.driver_margin_ms, config.extra_wait_ms)
    check_at = timing.deadline_us(activity["sent_at"])
    original_check = activity["expected_check_at"]
    if original_check is not None:
        if (activity["result_wait_margin_ms"] != config.driver_margin_ms
                or activity["extra_wait_ms_used"] is None
                or original_check != CaptureTiming(config.target_duration_ms, config.startup_margin_ms,
                    activity["result_wait_margin_ms"], activity["extra_wait_ms_used"]).deadline_us(activity["sent_at"])):
            raise ConsistencyError("原延时等待与固定执行定义或发送锚点不符")
    state = context.natural_wait_states.get(action_id)
    if state is None:
        state = NaturalWaitState(activity["id"], WaitClockReference(context.wall_us(), context.monotonic_ns()))
        context.natural_wait_states[action_id] = state
    elif state.activity_id != activity["id"]:
        raise ConsistencyError("原延时等待属于其他活动")
    if state.applied is None and original_check is not None:
        _publish_natural_wait(context, action_id, state, AppliedNaturalWait(
            original_check, activity["extra_wait_ms_used"],
            state.reference.logical_deadline_ns(original_check)))
    candidate = AppliedNaturalWait(check_at, config.extra_wait_ms,
        state.reference.logical_deadline_ns(check_at))
    if state.pending is not None:
        # 这里只能剩下已核实未提交的原完成申请。
        if state.pending.arrangement != candidate:
            state.pending = None
    if original_check != check_at or activity["extra_wait_ms_used"] != config.extra_wait_ms:
        state.pending = PendingNaturalWait(
            ScheduleWait(action_id, WaitPlan(WaitKind.WAIT_THEN_CHECK,
                check_at_utc=check_at, monotonic_deadline_ns=candidate.logical_deadline_ns),
                config.driver_margin_ms, config.extra_wait_ms, context.wall_us()),
            new_operation_key(), candidate, reconfigure=original_check is not None)
        (await _save_natural_wait(context, action_id, state))
    if context.monotonic_ns() < state.applied.logical_deadline_ns:
        return None
    return (await _complete_timelapse_wait(context, action_id))


async def _timelapse_handler(action_id: int, context: CaptureRuntime) -> None:
    try:
        await _timelapse_handler_once(action_id, context)
    except _NaturalTimeDeferred:
        # 当前来源资格不足；正常、取消和恢复共同交回会话正等待。
        return


async def _timelapse_handler_once(action_id: int, context: CaptureRuntime) -> None:
    (await _resolve_natural_wait(context, action_id))
    # 归属查询会让出执行；其他设备可以同时登记或移除自己的扫描。
    (await resume_result_page_saves(context.database, pending_scans={identity: scan
        for identity, scan in tuple(context.pending_result_scans.items())
        if (await context.action_id_of_ticket(scan.ticket)) == action_id}, repository=context.capture))
    (await context.resume_capture_completions(action_id))
    (await context.resume_baselines(action_id))
    (await context.resume_result_check_closes(action_id))
    (await context.resume_file_observations(action_id))
    (await context.resume_start_results(action_id))
    action = (await context.action(action_id))
    if action["status"] in _ACTION_TERMINAL:
        context.timelapse_deadlines.pop(action_id, None)
        context.natural_wait_states.pop(action_id, None)
        return
    if action["execution_spec_json"].get("result_policy") is not None:
        from camctl.capture.segment_completion import resume_closed_start

        if (await resume_closed_start(context, action)):
            return
    if (await _handle_binding_failure(context, action)):
        return
    attempt = (await context.last_attempt(f"start/{action_id}"))
    if attempt is None:
        if action["cancel_requested"]:
            (await _finish_canceled_capture(context, action_id, unstarted=True))
            context.timelapse_deadlines.pop(action_id, None)
            context.natural_wait_states.pop(action_id, None)
            return
        step = await _control_call(
            context, action, "start_timelapse", "timelapse_sent")
        if step.phase not in ("confirmed", "sent", "call_failed"):
            return
        attempt = (await context.last_attempt(f"start/{action_id}"))
        if attempt is None:
            return
        action = (await context.action(action_id))
    if (await _settle_unstarted_attempt(context, action, attempt)):
        context.timelapse_deadlines.pop(action_id, None)
        context.natural_wait_states.pop(action_id, None)
        return
    if action["cancel_requested"]:
        await _advance_canceled_capture(context, action, attempt)
        if (await context.action(action_id))["status"] in _ACTION_TERMINAL:
            context.timelapse_deadlines.pop(action_id, None)
            context.natural_wait_states.pop(action_id, None)
        return
    if action["execution_spec_json"]["end_control"] == int(EndControl.HOST_TIMER):
        activity = (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity["result_set_state"] in (3, 4):
            await _finish_timelapse_conclusion(context, action_id)
            return
        step = await _advance_host_timer(context, action, attempt)
        action = (await context.action(action_id))
        if action["cancel_requested"]:
            await _advance_canceled_capture(context, action, attempt)
            return
        if step is HostTimerPhase.WAITING:
            return
        if step is not HostTimerPhase.CONFIRMED:
            await _close_host_timer_failed(context, action_id, step.value)
            if (await context.action(action_id))["status"] in _ACTION_TERMINAL:
                context.timelapse_deadlines.pop(action_id, None)
                context.natural_wait_states.pop(action_id, None)
            return
    wait_after_send = action["execution_spec_json"]["wait_after_send"]
    if wait_after_send and (await _settle_start_without_sent_at(context, action, attempt)):
        # 可能派发但没有可靠发送时间：无法计算等待锚点，不重复启
        # 动、不补造时间，按无法核实收场。
        context.timelapse_deadlines.pop(action_id, None)
        context.natural_wait_states.pop(action_id, None)
        return
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute("SELECT sent_at, expected_check_at, result_wait_margin_ms, extra_wait_ms_used,"
        " wait_completed_event_id, result_set_state"
        " FROM device_activities WHERE action_id = ?", parameters)) as cursor:
            return cursor.fetchone()
    activity = await context.database.read(read, description="读取拍摄当前责任")
    if activity is None or (wait_after_send and activity[0] is None):
        # 发送事实（sent_at）由活动观察边界保存；尚未保存时等待。
        return
    wait_event_id = ((await _advance_timelapse_natural_wait(context, action))
                     if wait_after_send else None)
    if wait_after_send and wait_event_id is None:
        return
    context.timelapse_deadlines.pop(action_id, None)
    context.natural_wait_states.pop(action_id, None)
    concluded = activity[5] in (3, 4)
    if concluded:
        # 中断后已有可靠结论：用原结果完成收尾，不重开核实责任。
        await _finish_timelapse_conclusion(context, action_id)
        return
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value
    ):
        from camctl.capture.native_completion import advance_native_result

        await advance_native_result(context, action_id)
        return
    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    ):
        from camctl.capture.action6_completion import advance_action6_native_result

        await advance_action6_native_result(context, action_id)
        return
    listing = await _listing_round(context, action_id)
    if (await context.action(action_id))["cancel_requested"]:
        # 原页及调用结果由 listing 拥有者保留，取消收场复用原轮次。
        await _timelapse_handler(action_id, context)
        return
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT,
                         ListingPhase.CLOSED):
        # 在途、本轮列举失败或责任已闭合：等待收尾轮次，不提交新意图。
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        # Action6 分段由公共结果来源事务接管耗尽，不先单独关闭原 RESULTS。
        # 其他合同沿用原结论收场。
        if ((await context.action(action_id))["execution_spec_json"].get("result_policy")
                != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
            (await _close_check_unconfirmed(context, action_id))
        await _finish_timelapse_conclusion(context, action_id)
        return
    registered = (await _register_listing(context, action_id, listing))
    assessment = (await assess_registered_files(registered, _product_requirements(action, FileKind.VIDEO), set_finalized=_listing_finalized(listing)))
    completion_ready = (wait_after_send and wait_event_id is not None
        and action["execution_spec_json"]["completion_mode"] == int(
            enum_for("device_activities.completion_mode").TIME_AND_OUTPUTS))
    completion_ready = (completion_ready or listing.completion_evidence is not None
        or (await context.database.read(lambda owned, inputs=(action_id,), options=dict(): context.capture.read_result_completion(*inputs, owned, **options), description="读取已保存拍摄事实")) is not None)
    if completion_ready and (assessment.is_complete or assessment.explicitly_unmet):
        (await _confirm_timelapse_results(context, action_id, assessment, listing.entries, wait_event_id, listing))
        await _finish_timelapse_conclusion(context, action_id)
    else:
        (await _finish_listing_result(context, listing, retry_wait=True))


async def _advance_canceled_capture(context: CaptureRuntime, action, start) -> None:
    """取消优先推进原停止责任，不依赖启动确认或计时锚点。

    原启动及停止调用在途时等待实际结果；可能启动的活动沿原 STOP
    的身份、预算和间隔收场。结束未知不释放占用，也不补造启动时间。
    """
    (await _resolve_natural_wait(context, action["id"]))
    action_id = action["id"]
    if start is not None and start[0] == int(_ATTEMPT_STATUS.RUNNING):
        if not finite_cancel_policy(action) or not (await context.recover_attempt(
                (await _original_ticket(context, f"start/{action_id}", "control")))):
            return
        start = (await context.last_attempt(f"start/{action_id}"))
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute("SELECT activity_state, stop_supported FROM device_activities WHERE action_id = ?", parameters)) as cursor:
            return cursor.fetchone()
    activity = await context.database.read(read, description="读取拍摄当前责任")
    if activity is None:
        raise ConsistencyError("取消拍摄缺少设备活动")
    stop = (await context.last_attempt(f"stop/{action_id}"))
    finite_cancel = finite_cancel_policy(action)
    if finite_cancel and stop is not None and stop[0] == int(_ATTEMPT_STATUS.RUNNING):
        if not (await context.recover_attempt((await _original_ticket(context, f"stop/{action_id}", "stop")))):
            return
        stop = (await context.last_attempt(f"stop/{action_id}"))
    confirmed = (stop is not None and stop[0] != int(_ATTEMPT_STATUS.RUNNING)
                 and stop[1] == int(_EFFECT_STATE.CONFIRMED))
    ended = activity[0] == 3
    if not ended and not confirmed:
        if stop is not None and stop[0] == int(_ATTEMPT_STATUS.RUNNING):
            return
        def read(owned, parameters=(f"stop/{action_id}",)):
            with closing(owned.connection.execute("SELECT status FROM operation_runs WHERE responsibility_key = ?", parameters)) as cursor:
                return cursor.fetchone()
        run = await context.database.read(read, description="读取拍摄当前责任")
        if run is None or run[0] in (1, 2):
            if not activity[1]:
                raise ConsistencyError("可能启动的取消拍摄缺少原停止能力")
            if not finite_cancel and await _confirm_stop_by_query(context, action):
                # 未确认停止由停止核实收场：不重复发令，按停止事实
                # 继续取消收场。
                confirmed = True
            else:
                step = await _stop_call(context, action,
                                        "stop_timelapse" if action["type"] == 3 else "stop_recording")
                if step.phase == "confirmed":
                    confirmed = True
                elif step.phase == "stop_not_granted" and step.detail == "budget_exhausted":
                    (await _close_stop_exhausted(context, action))
                elif step.phase != "call_failed" and not finite_cancel:
                    return
    if finite_cancel and not confirmed and not ended:
        activity_facts = (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity_facts["sent_at"] is not None:
            if (await _advance_timelapse_natural_wait(context, action)) is None:
                return
            context.timelapse_deadlines.pop(action_id, None)
            context.natural_wait_states.pop(action_id, None)
        # 缺少可靠 SENT 时不制造等待，有限核实仍按原未知活动收场。
    if confirmed and (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["activity_state"] != 3:
        (await _conclude_activity(context, action_id))
    if action["type"] == 3:
        await _close_canceled_timelapse(context, action_id)
    else:
        if (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))["activity_state"] == 3:
            (await _release_occupancy(context, action_id))
        (await _finish_canceled_capture(context, action_id))


async def _close_canceled_timelapse(
    context: CaptureRuntime, action_id: int) -> None:
    """取消延时收尾：已拍完文件与取消终态同事务登记为正式产物。

    收尾列举按 results 责任的有限轮次推进：本轮失败保存实际结果
    与重试等待并保留取消待收场事实；预算耗尽时集合结论按无法确
    认收场。已经可靠归属且写入完成的文件仍与取消终态共同登记。
    """
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        (await context.action(action_id))["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    ):
        from camctl.capture.action6_completion import advance_action6_native_result

        await advance_action6_native_result(context, action_id, canceled=True)
        return
    if (
        (await context.action(action_id))["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value
    ):
        from camctl.capture.native_completion import advance_native_result

        await advance_native_result(context, action_id, canceled=True)
        return
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        # 在途或本轮列举失败：取消待收场事实保持，下一轮重新核实。
        return
    if listing.phase is ListingPhase.LISTED and listing.outcome.error is not None:
        # 已取得的文件事实先保存；读取错误按原有限预算重试，不能
        # 因分页已返回而提前结束取消所需的文件核实。
        (await _register_listing(context, action_id, listing))
        (await _finish_listing_result(context, listing, retry_wait=True))
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        (await _close_check_unconfirmed(context, action_id))
        listing = (await _saved_result_listing(context, action_id))
    entries = listing.entries
    registered = (await _register_listing(context, action_id, listing))
    activity = (await context.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(context, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    end_request = None
    if (finite_cancel_policy((await context.action(action_id))) and activity["activity_state"] != 3
            and isinstance(entries, SavedResultEntries)):
        if listing.phase is ListingPhase.LISTED and not _listing_finalized(listing):
            (await _finish_listing_result(context, listing, retry_wait=True))
            return
        if _listing_finalized(listing):
            request = FiniteCancelEndSave(action_id, entries.last_ref, context.wall_us())
            assessment = (await context.database.read(lambda owned, inputs=(request,), options=dict(): read_finite_cancel_assessment(inputs[0], owned, **options), description="读取拍摄直接事实"))
            if (listing.phase is ListingPhase.LISTED and not assessment.is_complete
                    and not assessment.explicitly_unmet):
                (await _finish_listing_result(context, listing, retry_wait=True))
                return
            if (assessment.is_complete and activity["sent_at"] is not None
                    and activity["wait_completed_event_id"] is not None
                    and (await _result_source(context, listing.ticket)).kind is ResultSourceKind.NATURAL):
                end_request = request
    (await _finish_listing_result(context, listing, end_run=RunOutcome.SUCCEEDED,
                           finalize_output_set=True))
    retained = _retained_result_files(context, listing, registered)
    drafts = (await _catalog_drafts(retained, entries,
        product_rules=_product_requirements((await context.action(action_id)), FileKind.VIDEO).rules))
    if end_request is not None:
        context.pending_result_closes[action_id] = PendingResultCheckClose(
            new_operation_key(), end_request)
        (await context.resume_result_check_closes(action_id))
        activity = (await context.database.read(lambda owned, inputs=("device_activities", activity["id"]): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
    if activity["activity_state"] == 3:
        (await _release_occupancy(context, action_id))
    (await context.save_capture_completion(
        FinishCanceledCapture(
            action_id=action_id,
            occurred_at=context.wall_us(),
            drafts=drafts,
            catalog_facts=OutputCatalogFacts(
                action_id=action_id, ownership_confirmed=True))))


async def _begin_check_round(runtime: CaptureRuntime, action_id: int):
    """为一轮结果核实提交意图并返回授予结果；预算沿原流程累计。"""
    _save_inputs = (AttemptIntent(
            operation="result", action_id=action_id,
            kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(
                activity_id=(await _activity_id_of(runtime, action_id))),
            query_purpose=None, config=runtime.check_config,
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    outcome = (await runtime.database.write(lambda owned: runtime.operations.begin_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"核实意图事务未完成（{outcome.kind.value}）: {outcome.error}", outcome)
    return outcome.value


async def _close_check_unconfirmed(runtime: CaptureRuntime, action_id: int, *,
                             reason: str = "outputs_unknown") -> None:
    """预算耗尽：核实流程与无法确认的集合结论同事务收场。"""
    if action_id in runtime.pending_result_closes:
        (await runtime.resume_result_check_closes(action_id))
        return
    code = "capture_result_unconfirmed"
    error = {"code": code, "stage": registered_error(code)["stage"],
             "details": {"activity_id": str((await _activity_id_of(runtime, action_id))),
                         "reason": reason}}
    runtime.pending_result_closes[action_id] = PendingResultCheckClose(
        new_operation_key(), ResultSetSave(
            action_id=action_id,
            occurred_at=runtime.wall_us(),
            phase=ResultSetPhase.UNCONFIRMED,
            contract=_RESULT_CONTRACT,
            observation={"reason": "attempts_exhausted"},
            capture={"status": "unconfirmed", "error": error},
            error=error,
        ))
    (await runtime.resume_result_check_closes(action_id))


class ListingPhase(Enum):
    """一轮结果列举推进的阶段分区。"""

    LISTED = "listed"
    IN_FLIGHT = "in_flight"
    RETRY_WAIT = "retry_wait"
    EXHAUSTED = "exhausted"
    CLOSED = "closed"


@dataclass(frozen=True)
class ListingRound:
    """一轮结果列举的推进结果：阶段、列举事实与承载它的票据。"""

    phase: ListingPhase
    entries: Iterable[ObservedFile] = ()
    ticket: Any = None
    outcome: CallOutcome | None = None
    occurred_at: int | None = None
    returned_ns: int | None = None
    already_saved: bool = False
    registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None
    scan_complete: bool = False
    set_finalized: bool = False
    time_qualified: bool = True
    completion_evidence: Any = None
    completion_page: ResultPageRef | None = None


class _NaturalTimeDeferred(Exception):
    """保留原申请并交还调度；当前真实读数尚不足以承载原来源时刻。"""


async def _result_source(runtime, ticket):
    source = (await runtime.database.read(lambda owned, inputs=(ticket,), options=dict(): runtime.capture.read_result_source(*inputs, owned, **options), description="读取已保存拍摄事实"))
    if source.context is not None and is_stop_context(source.context):
        context = {**source.context, "prior_completion": (await _prior_record_file_completion(
            runtime, (await runtime.action_id_of_ticket(ticket)), source.context))}
        source = replace(source, context=context)
    return source


async def _natural_source_lower_bound(runtime: CaptureRuntime, action_id: int, ticket=None) -> int | None:
    """从可靠原等待读取来源下界；明确 STOP 分支继续使用自己的完成依据。"""
    action = (await runtime.action(action_id))
    spec = action["execution_spec_json"]
    if (action["type"] != int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            or not spec.get("wait_after_send")
            or spec.get("end_control") != int(EndControl.DEVICE)):
        return None
    if ticket is not None:
        return (await _result_source(runtime, ticket)).natural_lower_bound
    stopped = (await runtime.last_attempt(f"stop/{action_id}"))
    if stopped is not None and stopped[1] == int(_EFFECT_STATE.CONFIRMED):
        return None
    context = (await _natural_file_completion_context(runtime, action_id))
    if context is None:
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", await _activity_id_of(runtime, action_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity["wait_completed_event_id"] is not None:
            raise ConsistencyError("可靠自然完成缺少原等待或发送来源")
        return None
    return max(context["expected_check_at_us"], context["wait_completed_at_us"])


async def _natural_listing_time(runtime, action_id, listing):
    """页资格只由原页实际时刻决定；当前时钟恢复不能改变坏页资格。"""
    if listing.ticket is not None:
        source = (await _result_source(runtime, listing.ticket))
        if source.kind is ResultSourceKind.UNCONFIRMED and (await runtime.action(action_id))["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE):
            return False, None
    bound = (await _natural_source_lower_bound(runtime, action_id, listing.ticket))
    if bound is None:
        return True, None
    if not listing.time_qualified:
        return False, bound
    latest = bound
    if isinstance(listing.entries, SavedResultEntries):
        async for saved in listing.entries.pages():
            if saved.occurred_at < bound:
                return False, bound
            latest = max(latest, saved.occurred_at)
    elif listing.occurred_at is not None:
        if listing.occurred_at < bound:
            return False, bound
        latest = max(latest, listing.occurred_at)
    return True, latest


async def _natural_read_ready(runtime, action_id, entries=None, *, ticket=None):
    bound = (await _natural_source_lower_bound(runtime, action_id, ticket))
    if bound is None:
        return True
    if entries is not None:
        async for saved in (entries.pages()):
            # 坏页只保留实际输入，不成为后续事实的完成来源。
            if saved.occurred_at >= bound:
                bound = max(bound, saved.occurred_at)
    return runtime.wall_us() >= bound


async def _qualify_listing_collection(runtime, action_id, listing):
    valid, _ = (await _natural_listing_time(runtime, action_id, listing))
    if valid:
        return listing
    # 集合不足不撤销逐页文件事实；原 entries 仍承载真实全部页。
    return replace(listing, time_qualified=False, completion_evidence=None, completion_page=None)


def _retained_result_files(runtime, listing, registered):
    """终态合并原可靠文件及合格当前观察；此视图不参与当前集合判断。"""
    if listing.ticket is not None:
        current = (registered.entries.current_entries if isinstance(registered, RegisteredSourceFiles)
            else listing.entries if listing.time_qualified else None)
        return RegisteredSourceFiles(TerminalResultEntries(runtime.capture, runtime.database,
            listing.ticket, current_entries=current))
    return registered


async def _register_listing(runtime: CaptureRuntime, action_id: int, listing: ListingRound):
    """已登记事实直接消费；只有尚未登记的原观察产生文件保存责任。"""
    from camctl.devices.tasks import CaptureResultPolicy

    action = await runtime.action(action_id)
    if is_hasselblad_photo(action) and action["execution_spec_json"]["output_storage"] == "device":
        if isinstance(listing.entries, SavedResultEntries):
            async for saved in listing.entries.pages():
                await _register_photo_result_page(runtime, action_id, saved.ref)
        if listing.ticket is None:
            return ()
        return RegisteredSourceFiles(SourceResultEntries(runtime.capture, runtime.database, listing.ticket))

    if action["execution_spec_json"].get("result_policy") in (
        CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
        CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
    ):
        from camctl.capture.segment_completion import register_segment_observations

        (await register_segment_observations(runtime, action_id, listing))
        return RegisteredSourceFiles(
            SourceResultEntries(
                runtime.capture,
                runtime.database,
                listing.ticket,
                current_entries=listing.entries,
            )
        )
    collection_valid, _ = (await _natural_listing_time(runtime, action_id, listing))
    source = None if listing.ticket is None else (await _result_source(runtime, listing.ticket))
    bound = (await _natural_source_lower_bound(runtime, action_id, listing.ticket))
    entries = listing.entries
    if isinstance(entries, SavedResultEntries) and source is not None and source.kind in (ResultSourceKind.UNCONFIRMED, ResultSourceKind.UNANCHORED) and source.requires_file_wait:
        return RegisteredSourceFiles(SourceResultEntries(runtime.capture, runtime.database, listing.ticket, current_entries=QualifiedResultEntries(entries, 0, source_qualified=False)))
    if isinstance(entries, SavedResultEntries) and bound is not None:
        entries = QualifiedResultEntries(entries, bound)
        latest = await max_result_value((saved.occurred_at async for saved in entries.pages()), default=None)
    else:
        valid, latest = (await _natural_listing_time(runtime, action_id, listing))
        if not valid and (bound is not None or source is None or source.requires_file_wait):
            return ()
    if latest is not None and runtime.wall_us() < latest:
        raise _NaturalTimeDeferred("当前真实时间早于原来源时刻")
    if isinstance(entries, SavedResultEntries):
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", int(listing.ticket.target_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        baseline = activity["ownership_mode"] == int(enum_for("device_activities.ownership_mode").BASELINE_COMPARISON)
        method = int(enum_for("device_files.ownership_evidence_json.method").BASELINE_DIFFERENCE) if baseline else _TASK_SCOPE
        action = (await runtime.action(action_id))
        completion_context = ((await _record_file_completion_context(runtime, action_id))
            if source is None else source.context)
        record_wait = is_stop_context(completion_context)
        stop_context = completion_context if record_wait else None
        uses_wait = (action["type"] == int(_ACTION_TYPE.CAMERA_TIMELAPSE)
            and not record_wait
            and (source is None or source.kind is ResultSourceKind.NATURAL)
            and (not action["cancel_requested"] or completion_context is not None)
            and activity["completion_mode"] == int(enum_for("device_activities.completion_mode").TIME_AND_OUTPUTS))
        wait_event = (activity["wait_completed_event_id"] if uses_wait else None)
        if uses_wait and wait_event is None:
            raise ConsistencyError("等待与产物契约的文件完成仍缺少原等待完成事实")
        # 原片归属先保存，随后处理预览；先出现的预览也能指向后页原片。
        for previews in (False, True):
            async for saved in (entries.pages()):
                identities = dict(saved.file_ids)
                batch = tuple(entry for entry in saved.page.entries if entry.identity in identities
                    and (entry.paired_identity is not None) == previews)
                if not batch:
                    continue
                pairs = {entry.paired_identity: (await entries.find(entry.paired_identity,
                         allow_missing=True))
                         for entry in batch if entry.paired_identity is not None}
                rules = _product_requirements(action, FileKind.VIDEO).rules
                if collection_valid and listing.scan_complete and any(entry.paired_identity is not None
                        and pairs[entry.paired_identity] is None and not is_optional_preview(
                            entry.kind, entry.format_id, entry.paired_identity, rules)
                        for entry in batch):
                    raise ConsistencyError("必要预览的配对原片不属于原任务的完整差集")
                # 原片在后页时等待；完整差集仍没有原片的可选预览保留发现事实而不登记。
                batch = tuple(entry for entry in batch if entry.paired_identity is None
                              or pairs[entry.paired_identity] is not None)
                pairs = {identity: pair for identity, pair in pairs.items() if pair is not None}
                if not batch:
                    continue
                if record_wait and any(entry.complete for entry in batch):
                    settlement = saved.page.outcome.settlement
                    completion = None if settlement is None else settlement.evidence.data.get("file_completion")
                    if stop_context is None or not is_completed_file_wait(completion,
                            activity_id=stop_context["activity_id"],
                            stop_result_event_id=stop_context["stop_result_event_id"],
                            required_wait_ms=stop_context["file_completion_wait_ms"]):
                        raise ConsistencyError("完整录像文件缺少原可靠停止及已完成等待依据")
                (await _register_observed(runtime, action_id, batch, occurred_at=saved.occurred_at,
                    registration=FileRegistration(pairing_files=pairs, ownership_method=method,
                    activity_id=activity["id"] if baseline else None,
                    completion_basis=int(enum_for("device_files.completion_evidence_json.basis").STOP_RETURN_AND_WAIT)
                        if record_wait else int(enum_for("device_files.completion_evidence_json.basis").TIME_AND_OUTPUTS)
                        if wait_event is not None else _DEVICE_GUARANTEE,
                    completion_activity_id=activity["id"] if record_wait or wait_event is not None else None,
                    wait_completed_event_id=wait_event,
                    result_page_event_id=saved.ref.event_id if record_wait or (
                        wait_event is not None and completion_context is not None) else None,
                    stop_result_event_id=None if stop_context is None else stop_context["stop_result_event_id"],
                    start_result_event_id=completion_context["start_result_event_id"]
                        if wait_event is not None and completion_context is not None else None)))
        return RegisteredSourceFiles(SourceResultEntries(runtime.capture, runtime.database, listing.ticket,
            current_entries=entries))
    registered = (await _register_observed(runtime, action_id, listing.entries, occurred_at=listing.occurred_at,
                                   registered_files=listing.registered_files))
    return RegisteredSourceFiles(ObservedResultEntries(listing.ticket, listing.entries, registered))


async def _unconfirmed_failure(runtime: CaptureRuntime, action_id: int) -> RecordingFailure:
    """有限核实耗尽后的动作失败：产物结果无法确认。"""
    return RecordingFailure(
        code="capture_result_unconfirmed",
        details={"activity_id": str((await _activity_id_of(runtime, action_id))), "reason": "outputs_unknown"})


async def _held_listing(runtime: CaptureRuntime, ticket: AttemptTicket) -> ListingRound | None:
    pending = runtime.pending_start_results.get((ticket.run_id, ticket.attempt_id))
    if pending is None or pending.result_listing is None:
        return None
    if pending.finish.ticket != ticket:
        raise ConsistencyError("待存 RESULTS 原票据不符")
    entries = pending.result_listing
    if isinstance(entries, SavedResultEntries):
        entries = SavedResultEntries(runtime.capture, runtime.database, entries.last_ref)
        page = await entries.last_page()
        completion = await entries.completion_page()
    else:
        page = None
        completion = None
    return ListingRound(ListingPhase.LISTED, entries, ticket,
                        pending.finish.outcome.outcome, pending.finish.occurred_at,
                        pending.returned_ns, registered_files=pending.result_registered_files,
                        scan_complete=False if page is None else page.scan_complete,
                        set_finalized=False if page is None else page.set_finalized,
                        completion_evidence=None if completion is None else completion.page.completion_evidence,
                        completion_page=None if completion is None else completion.ref)


def _uses_result_pages(runtime: CaptureRuntime) -> bool:
    """分页能力由驱动正式证据登记决定。"""
    for version in (2, 3):
        try:
            contract = runtime.evidence.contract("result_files_listed", version)
        except EvidenceError:
            continue
        if contract.operation == "result":
            return True
    return False


def _recovered_call_outcome(evidence_type="adb_foreground_recovery") -> CallOutcome:
    """旧前台已收场但整轮返回未保存；不生成退出或设备观察。"""
    return CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("result_not_saved", "recovery"),
        settlement=Settlement(SettlementBasis.ASSUMED,
                              EvidenceValue(evidence_type, 1, {})))


def _result_page_returned(saved) -> bool:
    return saved.page.next_cursor is None or saved.page.outcome.error is not None


def _hold_recovered_result_page(runtime: CaptureRuntime, saved, evidence) -> None:
    """原末页实际返回沿原驱动登记验证，时刻和页范围保持。"""
    ticket = saved.ref.ticket
    identity = (ticket.run_id, ticket.attempt_id)
    if identity in runtime.pending_start_results:
        raise ConsistencyError("原 RESULTS 结果尚未交接，不能替换")
    runtime.pending_start_results[identity] = PendingCallResult(new_operation_key(), AttemptFinish(
        ticket, validate_outcome(ticket, saved.page.outcome, evidence), saved.occurred_at), None,
        returned_ns=runtime.monotonic_ns(),
        result_listing=SavedResultEntries(runtime.capture, runtime.database, saved.ref),
        result_disposition_ready=False)


async def _settle_recovered_listing(runtime: CaptureRuntime, pending: PendingCallResult) -> PendingCallResult:
    """独立保存原真实结束依据；绑定失败不判定正常集合结论。"""
    completion = None
    if isinstance(pending.result_listing, SavedResultEntries):
        action_id = await runtime.action_id_of_ticket(pending.finish.ticket)
        action = await runtime.action(action_id)
        if is_hasselblad_photo(action) and action["execution_spec_json"]["output_storage"] == "device":
            async for page in pending.result_listing.pages():
                await _register_photo_result_page(runtime, action_id, page.ref)
            actual = pending.finish.outcome.outcome
            if actual.error is None:
                finish = replace(pending.finish, retry_wait=False, run_finish=RunFinish(RunOutcome.SUCCEEDED))
            elif (actual.error.details.get("classification") == "invalid"
                  or actual.error.code == "device_binding_unavailable"):
                finish = replace(pending.finish, retry_wait=False, run_finish=RunFinish(RunOutcome.FAILED, actual.error))
            else:
                finish = replace(pending.finish, retry_wait=True, run_finish=None)
            return replace(pending, finish=finish, result_disposition_ready=True)
        saved = await pending.result_listing.completion_page()
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", int(pending.finish.ticket.target_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if saved is not None and activity["activity_state"] != 3:
            completion = saved.ref
    return replace(pending, completion_page=completion, result_disposition_ready=True)


async def _continue_result_scan(runtime: CaptureRuntime, pending: PendingResultScan, *,
                                read_allowed=None) -> ListingRound:
    if runtime.results is None and not pending.finished:
        if pending.saves.pending is None:
            raise CapabilityNotDeclaredError(
                "继续未完成的拍摄结果分页需要驱动声明 result 能力")
        original = pending.saves.pending.request
        available_page = page_from_outcome(
            original.ticket, original.outcome.outcome, cursor=original.cursor)
        if available_page.next_cursor is not None and available_page.outcome.error is None:
            raise CapabilityNotDeclaredError(
                "继续未完成的拍摄结果分页需要驱动声明 result 能力")
    action_id = (await runtime.action_id_of_ticket(pending.ticket))
    source = (await _result_source(runtime, pending.ticket))
    pending.completion_context = deepcopy(source.context)
    async def can_read():
        if read_allowed is not None and not await read_allowed():
            return False
        await _resume_saved_photo_sources(runtime, action_id, pending.ticket)
        action = (await runtime.action(action_id))
        if (not pending.scope_only and source.kind is ResultSourceKind.NATURAL and action["cancel_requested"]
                and (await runtime.last_attempt(f"stop/{action_id}")) is None):
            return False
        entries = (None if pending.last_ref is None else
            SavedResultEntries(runtime.capture, runtime.database, pending.last_ref))
        return (await _natural_read_ready(runtime, action_id, entries, ticket=pending.ticket))
    async def page_qualified(saved):
        action = await runtime.action(action_id)
        if (is_hasselblad_photo(action)
                and action["execution_spec_json"]["output_storage"] == "device"):
            await _register_photo_result_page(runtime, action_id, saved.ref)
            return True
        bound = (await _natural_source_lower_bound(runtime, action_id, pending.ticket))
        return bound is None or saved.occurred_at >= bound
    if not await advance_result_scan(pending, runtime=runtime,
            can_read=can_read, page_qualified=page_qualified):
        return ListingRound(ListingPhase.IN_FLIGHT)
    if pending.last_ref is None or pending.last_outcome is None or pending.occurred_at is None:
        raise ConsistencyError("结果扫描缺少可靠末页及原返回")
    entries = SavedResultEntries(runtime.capture, runtime.database, pending.last_ref)
    runtime.hold_call_result(pending.ticket, pending.last_outcome.outcome,
        occurred_at=pending.occurred_at, returned_ns=pending.returned_ns, result_listing=entries)
    return (await _held_listing(runtime, pending.ticket))


async def _saved_result_listing(runtime: CaptureRuntime, action_id: int, *, responsibility=None) -> ListingRound:
    return (await _qualify_listing_collection(runtime, action_id, (await _saved_result_listing_input(
        runtime, action_id, responsibility=responsibility))))


async def _saved_result_listing_input(runtime: CaptureRuntime, action_id: int, *, responsibility=None) -> ListingRound:
    """CLOSED 消费原已保存输入；不取得当前驱动端口或新的时钟。"""
    activity_id = (await _activity_id_of(runtime, action_id))
    responsibility = f"results/{activity_id}" if responsibility is None else responsibility
    def read(owned, parameters=(responsibility,)):
        with closing(owned.connection.execute("SELECT r.id,r.action_id,r.activity_id,r.kind,t.attempt_no,t.status,t.effect_state,"
        " t.result_json,t.error_json,e.occurred_at FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN history_events e ON e.id=t.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY t.attempt_no DESC LIMIT 1", parameters)) as cursor:
            return cursor.fetchone()
    row = await runtime.database.read(read, description="读取拍摄当前责任")
    if (row is None or row[1] != action_id or row[2] != activity_id
            or row[3] not in (int(_RUN_KIND.CHECK_CAPTURE_RESULTS),
                              int(_RUN_KIND.RESOLVE_OUTPUT_SCOPE))):
        raise ConsistencyError("RESULTS 责任缺少可核对的完整本地输入")
    ticket = AttemptTicket(row[4], "result", str(activity_id), responsibility, row[0])
    actual = saved_outcome(row[5], row[6], parse_exact_json(row[7]),
                           None if row[8] is None else parse_exact_json(row[8]))
    last = (await runtime.database.read(lambda owned, inputs=(ticket,), options=dict(): runtime.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
    if last is not None:
        partial_recovery = actual in (_recovered_call_outcome(), _recovered_call_outcome("local_result_recovery")) and not _result_page_returned(last)
        if not partial_recovery and (last.page.outcome != actual or last.occurred_at != row[9]):
            raise ConsistencyError("RESULTS 结束记录改变原末页实际返回或时刻")
        entries = SavedResultEntries(runtime.capture, runtime.database, last.ref)
        completion = await entries.completion_page()
        return ListingRound(ListingPhase.CLOSED, entries, ticket, actual, row[9], None, True,
            scan_complete=False if partial_recovery else last.page.scan_complete,
            set_finalized=False if partial_recovery else last.page.set_finalized,
            completion_evidence=None if completion is None else completion.page.completion_evidence,
            completion_page=None if completion is None else completion.ref)
    if not actual.observations:
        # 新轮首批尚未返回时，原文件子责任仍属于前轮可靠页。
        # 这些页只用于文件事实，不构成本轮完整扫描或集合保证。
        def read(owned, parameters=(ticket.run_id, ticket.attempt_id)):
            with closing(owned.connection.execute("SELECT attempt_no FROM operation_attempts WHERE run_id=? AND attempt_no<?"
            " AND result_last_page_event_id IS NOT NULL ORDER BY attempt_no DESC LIMIT 1", parameters)) as cursor:
                return cursor.fetchone()
        previous = await runtime.database.read(read, description="读取拍摄当前责任")
        if previous is not None:
            previous_ticket = replace(ticket, attempt_id=previous[0])
            source = (await runtime.database.read(lambda owned, inputs=(previous_ticket,), options=dict(): runtime.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
            if source is None:
                raise ConsistencyError("原文件责任缺少已保存的前轮结果页")
            return ListingRound(ListingPhase.CLOSED,
                SavedResultEntries(runtime.capture, runtime.database, source.ref),
                ticket, actual, row[9], None, True)
    has_observation = any(value.type == "result_files_listed" for value in actual.observations)
    if (not has_observation and actual.error is None
            and (await runtime.action(action_id))["type"] != int(_ACTION_TYPE.CAMERA_TIMELAPSE)):
        found = await runtime.database.read(lambda owned: owned.connection.execute(
            "SELECT 1 FROM device_files WHERE source_action_id=? LIMIT 1", (action_id,)).fetchone())
        if found is None:
            raise ConsistencyError("已保存 RESULTS 缺少完整本地文件输入")
    entries = files_from_outcome(ticket, actual) if has_observation else ()
    return ListingRound(ListingPhase.CLOSED, entries, ticket, actual, row[9], None, True)


async def _finish_listing_result(runtime: CaptureRuntime, listing: ListingRound, *,
                           retry_wait: bool = False, end_run: RunOutcome | None = None,
                           run_error: ErrorValue | None = None,
                           result_set: ResultSetSave | None = None,
                           finalize_output_set: bool = False) -> None:
    """首次固定真实处置后沿原 key 保存；已保存的 CLOSED 输入不再改写。"""
    if listing.already_saved:
        if finalize_output_set and _listing_finalized(listing):
            ticket = listing.ticket
            activity = (await runtime.database.read(lambda owned, inputs=("device_activities", int(ticket.target_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
            if activity["output_set_finalized_event_id"] is None:
                last = (await runtime.database.read(lambda owned, inputs=(ticket,), options=dict(): runtime.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
                if last is None:
                    raise ConsistencyError("原完整集合缺少可靠末页")
                action_id = activity["action_id"]
                if action_id not in runtime.pending_result_closes:
                    runtime.pending_result_closes[action_id] = PendingResultCheckClose(
                        new_operation_key(), OutputSetFinalizationSave(action_id, last.ref, last.occurred_at))
                (await runtime.resume_result_check_closes(action_id))
        return
    ticket = listing.ticket
    identity = (ticket.run_id, ticket.attempt_id)
    pending = runtime.pending_start_results.get(identity)
    if pending is None or pending.finish.ticket != ticket or pending.finish.outcome.outcome is not listing.outcome:
        raise ConsistencyError("RESULTS 原返回及其保存责任不可替换")
    finish = replace(pending.finish, retry_wait=retry_wait,
                     run_finish=None if end_run is None else RunFinish(end_run, run_error))
    completion_page = pending.completion_page
    finalized_page = pending.finalized_page
    if finalize_output_set and _listing_finalized(listing):
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", int(ticket.target_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity["output_set_finalized_event_id"] is None:
            last = (await runtime.database.read(lambda owned, inputs=(ticket,), options=dict(): runtime.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
            if last is None:
                raise ConsistencyError("完整集合缺少原可靠末页")
            finalized_page = last.ref
    if not pending.result_disposition_ready and listing.completion_page is not None:
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", int(ticket.target_id)): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        if activity["activity_state"] != 3:
            completion_page = listing.completion_page
    if pending.result_disposition_ready and (pending.finish != finish or pending.result_set != result_set
            or pending.finalized_page != finalized_page):
        raise ConsistencyError("RESULTS 原 key 的已确定处置不可改变")
    pending = replace(pending, finish=finish, result_set=result_set,
                      completion_page=completion_page, finalized_page=finalized_page,
                      result_disposition_ready=True)
    runtime.pending_start_results[identity] = pending
    (await runtime.save_held_result(ticket))


@owned_tool_call
async def _listing_round(runtime: CaptureRuntime, action_id: int) -> ListingRound:
    """先交接原申请，分别核对调用前与原页实际返回的时间资格。"""
    (await runtime.resume_file_observations(action_id))
    activity_id = (await _activity_id_of(runtime, action_id))
    responsibility = f"results/{activity_id}"
    ticket = (await _original_ticket(runtime, responsibility, "result"))
    if ticket is None and not (await _natural_read_ready(runtime, action_id)):
        return ListingRound(ListingPhase.RETRY_WAIT)
    try:
        listing = await _listing_round_input(runtime, action_id)
    except _NaturalTimeDeferred:
        return ListingRound(ListingPhase.RETRY_WAIT)
    if listing.phase not in (ListingPhase.LISTED, ListingPhase.CLOSED):
        return listing
    valid, bound = (await _natural_listing_time(runtime, action_id, listing))
    if not valid:
        try:
            (await _register_listing(runtime, action_id, listing))
        except _NaturalTimeDeferred:
            return ListingRound(ListingPhase.RETRY_WAIT)
        if not listing.already_saved:
            # 实际调用仍成功；只保存其业务完成不足和原责任重试等待。
            (await _finish_listing_result(runtime, replace(listing, completion_page=None), retry_wait=True))
            return ListingRound(ListingPhase.RETRY_WAIT)
        return (await _qualify_listing_collection(runtime, action_id, listing))
    if bound is not None and runtime.wall_us() < bound:
        return ListingRound(ListingPhase.RETRY_WAIT)
    return listing


async def _listing_round_input(runtime: CaptureRuntime, action_id: int) -> ListingRound:
    """同一活动的有限核实；实际返回先持有，消费者随后确定真实处置。"""
    (await runtime.resume_file_observations(action_id))
    activity_id = (await _activity_id_of(runtime, action_id))
    responsibility = f"results/{activity_id}"
    in_flight = (await runtime.last_attempt(responsibility))
    if in_flight is not None and in_flight[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = (await _original_ticket(runtime, responsibility, "result"))
        held = (await _held_listing(runtime, ticket))
        if held is not None:
            return held
        pending = runtime.pending_result_scans.get((ticket.run_id, ticket.attempt_id))
        if pending is not None:
            return await _continue_result_scan(runtime, pending)
        evidence = (await runtime._attempt_recovery_evidence(ticket))
        if evidence is None:
            return ListingRound(ListingPhase.IN_FLIGHT)
        last = (await runtime.database.read(lambda owned, inputs=(ticket,), options=dict(): runtime.capture.read_last_result_page(*inputs, owned, **options), description="读取已保存拍摄事实"))
        if last is not None and _result_page_returned(last):
            _hold_recovered_result_page(runtime, last, evidence)
            return (await _held_listing(runtime, ticket))
        if not (await runtime.recover_attempt(ticket, retry_wait=True)):
            return ListingRound(ListingPhase.IN_FLIGHT)
        # 原页发现的文件可能尚未保存归属或完成事实，必须先消费。
        (await _register_listing(runtime, action_id, (await _saved_result_listing(runtime, action_id))))
    elif in_flight is not None:
        # 原尝试已经结束时，分页文件子责任仍可能尚未可靠保存。
        # 先恢复这些原事实，再按原重试要求区分新轮与结果收尾。
        ticket = (await _original_ticket(runtime, responsibility, "result"))
        def read(owned, parameters=(ticket.run_id,)):
            with closing(owned.connection.execute("SELECT retry_wait_required, EXISTS("
            "SELECT 1 FROM operation_attempts WHERE run_id=r.id"
            " AND result_last_page_event_id IS NOT NULL) FROM operation_runs r WHERE r.id=?",
            parameters)) as cursor:
                return cursor.fetchone()
        saved_state = await runtime.database.read(read, description="读取拍摄直接事实")
        if saved_state is None:
            raise ConsistencyError("原 RESULTS 尝试缺少所属流程")
        saved_listing = None
        if saved_state[1]:
            saved_listing = await _saved_result_listing(runtime, action_id)
            await _register_listing(runtime, action_id, saved_listing)
        if saved_state[0] == 0:
            # 原结果的处置已确定；集合等后续保存尚未完成不授予新尝试。
            return saved_listing if saved_listing is not None else await _saved_result_listing(runtime, action_id)
    if (await runtime.retry_wait_remaining(responsibility, runtime.check_config.retry_interval_s,
                                    maximum=runtime.check_config.max_attempts)) is not None:
        return ListingRound(ListingPhase.RETRY_WAIT)
    original_ticket = await _original_ticket(runtime, responsibility, "result")
    if original_ticket is not None:
        await _resume_saved_photo_sources(runtime, action_id, original_ticket)
    if runtime.results is None:
        def read(owned, parameters=(responsibility,)):
            with closing(owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key=?", parameters)) as cursor:
                return cursor.fetchone()
        run = await runtime.database.read(read, description="读取拍摄当前责任")
        if run is None:
            raise CapabilityNotDeclaredError(
                "拍摄结果核实需要驱动声明 result 能力")
        if run[0] not in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)):
            return (await _saved_result_listing(runtime, action_id))
        if run[1] >= runtime.check_config.max_attempts:
            return ListingRound(ListingPhase.EXHAUSTED)
        raise CapabilityNotDeclaredError(
            "拍摄结果核实需要驱动声明 result 能力")
    if not (await _natural_read_ready(runtime, action_id)):
        return ListingRound(ListingPhase.RETRY_WAIT)
    begin = (await _begin_check_round(runtime, action_id))
    if begin.disposition is not BeginDisposition.GRANTED:
        if begin.reason == "budget_exhausted":
            return ListingRound(ListingPhase.EXHAUSTED)
        return (await _saved_result_listing(runtime, action_id))
    ticket = begin.ticket
    if _uses_result_pages(runtime):
        activity = (await runtime.database.read(lambda owned, inputs=("device_activities", activity_id): row_facts(owned.connection, *inputs), description="读取拍摄直接事实"))
        action = (await runtime.action(action_id))
        execution_spec = action["execution_spec_json"]
        video_result_mode = None
        if action["type"] in (int(_ACTION_TYPE.CAMERA_RECORD), int(_ACTION_TYPE.CAMERA_TIMELAPSE)):
            try:
                video_result_mode = VideoResultMode(execution_spec["video_result_mode"]).value
            except (KeyError, TypeError, ValueError) as error:
                raise ConsistencyError("视频结果列举缺少已冻结的有效 video_result_mode") from error
        pending = PendingResultScan(ticket, activity["output_scope_json"],
                                    runtime.check_config.timeout_s,
                                    completion_context=(await _result_source(runtime, ticket)).context,
                                    video_result_mode=video_result_mode,
                                    action_id=action_id, device_id=action["device_id"])
        runtime.pending_result_scans[(ticket.run_id, ticket.attempt_id)] = pending
        return await _continue_result_scan(runtime, pending)
    with measure_stage("capture.result_scan", action_id=action_id,
            activity_id=ticket.target_id, run_id=ticket.run_id, attempt_id=ticket.attempt_id):
        result = await runtime.results.list_round(ticket, timeout_s=runtime.check_config.timeout_s)
        observed_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
    runtime.hold_call_result(ticket, result.outcome, occurred_at=observed_at,
                             returned_ns=returned_ns, result_listing=result.entries)
    listing = (await _held_listing(runtime, ticket))
    has_file_observation = any(value.type == "result_files_listed" for value in result.outcome.observations)
    if not has_file_observation and result.outcome.error is not None:
        (await _finish_listing_result(runtime, listing, retry_wait=True))
        return ListingRound(ListingPhase.RETRY_WAIT, outcome=result.outcome,
                            occurred_at=observed_at, returned_ns=returned_ns)
    return listing


async def _finish_timelapse_conclusion(
    runtime: CaptureRuntime, action_id: int) -> None:
    """集合已有结论后的收尾：按保存的判定登记产物与终态。

    核实责任终态保持，不重开轮次；产物登记只消费原保存输入，
    输入缺失或不可解释时保留诊断，不访问设备补写。
    """
    from camctl.devices.tasks import CaptureResultPolicy

    if (
        (await runtime.action(action_id))["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    ):
        from camctl.capture.action6_completion import advance_action6_native_result

        await advance_action6_native_result(runtime, action_id)
        return
    if (
        (await runtime.action(action_id))["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value
    ):
        from camctl.capture.native_completion import advance_native_result

        await advance_native_result(
            runtime,
            action_id,
            canceled=bool((await runtime.action(action_id))["cancel_requested"]),
        )
        return
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute("SELECT result_set_state, completion_basis, capture_json, activity_state FROM device_activities"
        " WHERE action_id = ?", parameters)) as cursor:
            return cursor.fetchone()
    state, basis, capture_json, activity_state = await runtime.database.read(read, description="读取拍摄当前责任")
    listing = (await _saved_result_listing(runtime, action_id))
    valid, bound = (await _natural_listing_time(runtime, action_id, listing))
    if valid and bound is not None and runtime.wall_us() < bound:
        return
    entries = listing.entries
    registered = (await _register_listing(runtime, action_id, listing))
    terminal = _retained_result_files(runtime, listing, registered)
    capture = None if capture_json is None else parse_exact_json(capture_json)
    if activity_state == 3 or (state == 3 and basis == 3):
        (await _release_occupancy(runtime, action_id))
    if state == 3 and basis in (2, 3):
        # 原 RESULT_SET_CONFIRMED 已经可靠保存完整且满足的集合；
        # 恢复沿该结论收尾，不要求旧列举响应再次提供集合保证。
        (await _finish_capture(runtime, action_id, entries, FileKind.VIDEO, registered=registered,
                        set_finalized=True, collection_confirmed=True, terminal_registered=terminal))
    elif state == 3:
        error = None if capture is None else capture.get("error")
        if not isinstance(error, Mapping) or set(error) != {"code", "stage", "details"}:
            raise ConsistencyError("已保存的明确采集失败缺少完整公共错误")
        (await _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO, registered=registered,
            failure=RecordingFailure(code=error["code"], details=error["details"]), terminal_registered=terminal))
    elif capture is not None and isinstance(capture.get("error"), Mapping):
        error = capture["error"]
        (await _finish_capture(runtime, action_id, entries, FileKind.VIDEO, registered=registered,
            failure=RecordingFailure(code=error["code"], details=error["details"]), terminal_registered=terminal))
    else:
        (await _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO, registered=registered,
            failure=(await _unconfirmed_failure(runtime, action_id)), terminal_registered=terminal))


async def _complete_timelapse_wait(runtime: CaptureRuntime, action_id: int) -> int | None:
    """到期后保留原完成申请，可靠提交才返回完成事件。"""
    state = runtime.natural_wait_states[action_id]
    if state.pending is None:
        occurred_at = runtime.wall_us()
        if occurred_at < state.applied.expected_check_at:
            return None
        state.pending = PendingNaturalWait(
            WaitCompletedSave(action_id=action_id, occurred_at=occurred_at),
            new_operation_key(), state.applied)
    return (await _save_natural_wait(runtime, action_id, state)).event_id


async def _confirm_timelapse_results(
    runtime: CaptureRuntime, action_id: int, assessment, entries,
    wait_event_id: int | None, listing: ListingRound,
) -> None:
    """把本轮结果集合核实结论与尝试结束、流程收场共同保存。

    完整集合满足时按时间与产物完成判定，依据引用已保存的等待完
    成事实；明确不满足保存已知失败。观察只记录实际列举到的文件
    事实，不填理论张数。结论与承载它的列举轮次原子提交。
    """
    contract = _RESULT_CONTRACT
    if isinstance(entries, SavedResultEntries):
        first = (await anext(entries.pages())).ref
        observation = {"first_page_event_id": first.event_id,
                       "last_page_event_id": entries.last_ref.event_id,
                       "file_count": assessment.file_count}
        contract = "result_files_listed/v2"
    else:
        observation = {"files": sorted(entry.identity for entry in entries)}
    if assessment.is_complete:
        if listing.completion_evidence is not None:
            actual = listing.completion_evidence
            completion = {"method": _DEVICE_EVIDENCE_METHOD,
                "observation": {"result_page_event_id": listing.completion_page.event_id,
                    "completion_evidence": {"type": actual.type, "version": actual.version,
                                            "data": dict(actual.data)}}}
        else:
            completion = (await runtime.database.read(lambda owned, inputs=(action_id,), options=dict(): runtime.capture.read_result_completion(*inputs, owned, **options), description="读取已保存拍摄事实"))
            if completion is None:
                if ((await _result_source(runtime, listing.ticket)).kind is not ResultSourceKind.NATURAL
                        or not (await _natural_listing_time(runtime, action_id, listing))[0]):
                    raise ConsistencyError("原结果请求没有自然完成资格，不能采用后来的等待")
                completion = {"method": _TIME_AND_OUTPUTS_METHOD,
                    "wait_completed_event_id": wait_event_id, "observation": observation}
        command = ResultSetSave(
            action_id=action_id,
            occurred_at=listing.occurred_at,
            phase=ResultSetPhase.COMPLETE,
            contract=contract,
            observation=observation,
            capture={"status": "completed"},
            evidence=completion)
    else:
        missing = sorted(str(kind.value) for kind in assessment.missing_kinds)
        failure = (await _output_failure(runtime, action_id, assessment))
        if failure is None:
            raise ConsistencyError("明确不满足必须具备确定集合及产物失败分区")
        error = {"code": failure.code, "stage": registered_error(failure.code)["stage"],
                 "details": dict(failure.details)}
        command = ResultSetSave(
            action_id=action_id,
            occurred_at=listing.occurred_at,
            phase=ResultSetPhase.UNSATISFIED,
            contract=contract,
            observation={**observation, "missing": missing},
            capture={"status": "failed", "error": error},
            evidence={
                "method": _KNOWN_FAILURE_METHOD,
                "observation": {**observation, "missing": missing},
            }, error=error)
    (await _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED, result_set=command))


_HANDLERS: dict[str, ActionHandler] = {
    "camera_record": _record_handler,
    "camera_take_photo": _photo_handler,
    "camera_timelapse": _timelapse_handler,
}


def capture_handler(action_type: str) -> ActionHandler:
    """取得拍摄类型的处理器；未支持类型拒绝（不默认录像）。"""
    return _HANDLERS[action_type]


def route_completion(action_type: str, declared):
    """核对完成声明属于该动作类型；返回原声明。"""
    if action_type not in _HANDLERS:
        raise ValueError(f"未支持的拍摄类型: {action_type!r}")
    return declared
