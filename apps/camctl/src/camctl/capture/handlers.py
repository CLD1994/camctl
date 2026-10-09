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
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from camctl.capture.models import (
    ActivityConcludeSave,
    ActivityObservationSave,
    ActivityReleaseSave,
    ResultRunClose,
    ResultSetPhase,
    ResultSetSave,
    WaitCompletedSave,
)
from camctl.capture.files import (
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
from camctl.capture.result_inputs import (
    ListedResult, ObservedFile, files_from_outcome, saved_outcome,
)
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
    RecoveryBlockedReason, RecoveryBoundary, RecoveryDiagnostic,
)
from camctl.capture.results import (
    CaptureFile,
    CaptureFileSet,
    FileKind,
    ProductRequirements,
    assess_capture_files,
)
from camctl.capture.timelapse import (
    CaptureWaitConfig,
    ClockReading,
    EndControl,
    StartReturn,
    TimelapseState,
    WaitKind,
    WaitPlan,
    plan_capture_wait,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.bindings import BindingResult, DeviceBinding, binding_failure_details
from camctl.devices.ports import ControlRequest, DeviceCallResult
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
from camctl.persistence.models import DatabaseAccessError, DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishBindingFailure,
    FinishCanceledCapture,
    FinishCapture,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import (
    ExpireActionRequest, GrantRequest, SchedulingRepository,
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
_KNOWN_FAILURE_METHOD = "known_failure"

_ACTION_TERMINAL = (3, 4, 5, 6)

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_ACTION_TYPE = enum_for("actions.type")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")
_DISPATCH_STATE = enum_for("device_activities.dispatch_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_FILE_PRESENCE = enum_for("device_files.presence_state")
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
class PendingCallResult:
    """原执行者持有的调用结果；保存核实不改变身份、事实或时钟。"""

    key: OperationKey
    finish: AttemptFinish
    observation: ActivityObservationSave | None
    start_finish: StaleRunFinish | None = None
    action_finish: FinishCapture | FinishCanceledCapture | None = None
    expiration: ExpireActionRequest | None = None
    confirmation_anchor_ns: int | None = None
    returned_ns: int = 0
    action_failure: RecordingFailure | None = None
    canceled_unstarted: bool = False
    preparation: RecordingStartPreparation | StartConfirmationPreparation | None = None
    result_listing: tuple[ObservedFile, ...] | None = None
    result_disposition_ready: bool = True
    result_set: ResultSetSave | None = None
    result_registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None


# 启动装配和既有调用方使用同一个公共责任集合。
PendingStartResult = PendingCallResult


@dataclass(frozen=True)
class PendingFileFact:
    """一个在场、归属或完成事实的原申请及可靠保存阶段。"""

    command: FilePresenceSave | OwnershipSave | FileCompletionSave
    key: OperationKey
    response: ObservationOutcome | None = None


class FileFactStage(Enum):
    PRESENCE = "presence"
    OWNERSHIP = "ownership"
    COMPLETION = "completion"


@dataclass(frozen=True)
class PendingFileObservation:
    """独立于 RESULTS 收场的原文件发现请求及其可靠首次响应。"""

    command: FileObservationSave
    key: OperationKey
    entries: tuple[ObservedFile, ...]
    registered_files: Mapping[str, tuple[CaptureFile, int]]
    response: ObservationOutcome | None = None
    facts: dict[FileFactStage, PendingFileFact] = field(default_factory=dict)


def _same_file_request(left, right) -> bool:
    """文件保存申请的标量保持原类型契约，JSON 按精确值核对。"""
    if type(left) is not type(right):
        return False
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
    raise TypeError("文件申请必须属于发现、在场、归属或完成阶段")


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

    def __init__(self, owned, pending_file_observations, pending_call_results):
        self.owned = owned
        self.capture = CaptureRepository()
        self.pending_file_observations = pending_file_observations
        self.pending_start_results = pending_call_results
        def original_time_required():
            raise ConsistencyError("原文件保存必须使用已持有的事实时刻")
        self.wall_us = original_time_required

    def resume_file_observations(self, action_id: int) -> None:
        """业务筛选前核实原发现；后续文件事实可靠保存前保持首次响应。"""
        for identity, pending in tuple(self.pending_file_observations.items()):
            if identity[0] == action_id and identity in self.pending_file_observations:
                _register_observed(self, action_id, pending.entries,
                    occurred_at=pending.command.occurred_at, registered_files=pending.registered_files)

    def _save_file_observations(self, action_id: int) -> None:
        """仅核实持有的发现请求；整批事实保存负责释放生命周期。"""
        for identity, pending in tuple(self.pending_file_observations.items()):
            if identity[0] != action_id or pending.response is not None:
                continue
            error = None
            for _ in range(2):
                receipt = self.capture.save_file_observation(pending.command, pending.key, self.owned)
                if receipt.kind is DbOutcomeKind.COMPLETED:
                    self.pending_file_observations[identity] = replace(pending, response=receipt.value)
                    break
                error = receipt.error
                if self.owned.connection.in_transaction:
                    try:
                        self.owned.connection.execute("ROLLBACK")
                    except (DatabaseAccessError, ConsistencyError) as failure:
                        raise ConsistencyError("原文件发现事务无法可靠结束，原请求仍持有") from failure
            else:
                raise ConsistencyError(f"原文件发现未可靠保存，原请求仍持有: {error}")

    def _save_file_fact(self, identity: tuple[int, str], stage: FileFactStage,
                        command: FilePresenceSave | OwnershipSave | FileCompletionSave) -> None:
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
        save = {FileFactStage.PRESENCE: self.capture.save_file_presence,
                FileFactStage.OWNERSHIP: self.capture.save_file_ownership,
                FileFactStage.COMPLETION: self.capture.save_file_completion}[stage]
        error = None
        for _ in range(2):
            receipt = save(pending.command, pending.key, self.owned)
            if receipt.kind is DbOutcomeKind.COMPLETED:
                observation.facts[stage] = replace(pending, response=receipt.value)
                return
            error = receipt.error
            if self.owned.connection.in_transaction:
                try:
                    self.owned.connection.execute("ROLLBACK")
                except (DatabaseAccessError, ConsistencyError) as failure:
                    raise ConsistencyError("原文件事实事务无法可靠结束，原申请仍持有") from failure
        raise ConsistencyError(f"原文件事实未可靠保存，原申请仍持有: {error}")

    def action_id_of_ticket(self, ticket: AttemptTicket) -> int:
        run = row_facts(self.owned.connection, "operation_runs", ticket.run_id)
        if run is None:
            raise ConsistencyError("原票据缺少所属流程")
        return run["action_id"]


def resume_file_observations(owned, *, pending_file_observations, pending_call_results) -> None:
    """fresh Owned 接手同会话文件责任，先于设备、时钟和业务筛选。"""
    saves = _FileObservationSaves(owned, pending_file_observations, pending_call_results)
    for action_id in dict.fromkeys(identity[0] for identity in tuple(pending_file_observations)):
        saves.resume_file_observations(action_id)


class DeviceControlPort(Protocol):
    """设备控制调用端口；契约替身与真实驱动同形。"""

    async def control(self, request: ControlRequest) -> DeviceCallResult: ...


class DeviceStopPort(Protocol):
    """设备停止操作端口；录像停止调用经此发出。"""

    async def stop(self, request: ControlRequest) -> DeviceCallResult: ...


class ResultFilesPort(Protocol):
    """结果列举端口：返回本任务观察到的候选产物文件。"""

    async def list_round(self, ticket: AttemptTicket, *, timeout_s: Decimal) -> ListedResult: ...


class RecordingStatePort(Protocol):
    """录像中段事实端口；活动事实生产者接入前由装配层提供。"""

    def recording_state(self, action_id: int) -> RecordingState: ...


@dataclass
class CaptureRuntime(_FileObservationSaves):
    """处理器组合的真实仓储端口与设备替身注入点。

    window_of 从动作行取得启动窗口；wait_config 从动作行取得延时
    等待配置（目标时长与余量读首次固定的执行定义）；monotonic_ns
    提供会话单调钟；evidence 登记驱动观察契约供尝试结果校验。
    """

    owned: Any
    scheduling: SchedulingRepository
    operations: OperationRepository
    capture: CaptureRepository
    timelapse: TimelapseRepository
    driver: DeviceControlPort
    results: ResultFilesPort
    evidence: Any
    wall_us: Callable[[], int]
    monotonic_ns: Callable[[], int]
    window_of: Callable[[Mapping[str, Any]], LaunchWindow]
    wait_config: Callable[[Mapping[str, Any]], CaptureWaitConfig]
    #: 核对已保存设备身份与本次固定配置；未提供时由调用方保证绑定匹配。
    binding_check: Callable[[DeviceBinding], BindingResult] | None = None
    recording_state: RecordingStatePort | None = None
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
    #: 设备状态查询端口；未装配时执行前检查与残留收场确认查询不
    #: 推进，触发动作按自身窗口与取消规则收尾。
    state_query: Any = None
    #: 执行前检查与确认查询的本次预算；默认 3 次、单次 10 秒、重试
    #: 间隔 3 秒（configuration.md#状态查询与产物核实的配置）。
    query_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 残留收场停止的本次预算；默认 3 次包含第一次、单次 10 秒、
    #: 重试间隔 3 秒（camera-recovery.md#后续动作触发的残留收场）。
    residual_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 仅恢复固定启动边界之前的旧尝试，不用于本会话新派发调用。
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    recovery_max_event_id: int | None = None
    recovery_evidence_for: Callable[[DeviceBinding, str], Any] | None = None
    #: 会话装配共享，未核实的实际结果不能由 UNKNOWN 恢复覆盖。
    pending_start_results: dict[tuple[int, int], PendingCallResult] = field(default_factory=dict)
    pending_read_results: dict = field(default_factory=dict)
    pending_read_business: dict = field(default_factory=dict)
    pending_read_ends: dict = field(default_factory=dict)
    continuing_read_tickets: dict = field(default_factory=dict)
    #: 无恢复依据保留责任与诊断；成功核实后清除，仅供本地消费。
    last_recovery_diagnostic: RecoveryDiagnostic | None = None
    #: 诊断进入本地运行日志，不影响原业务责任；相同诊断不重复投递。
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None
    #: 已取得的媒体原申请由会话共享，保存资格独立于驱动和动作终态。
    pending_media_results: dict = field(default_factory=dict)
    file_executor: Any = None
    #: 原文件发现写入前持有，普通、残留与受限工厂共用。
    pending_file_observations: dict[tuple[int, str], PendingFileObservation] = field(default_factory=dict)

    def record_recovery_diagnostic(self, diagnostic: RecoveryDiagnostic) -> None:
        if diagnostic == self.last_recovery_diagnostic:
            return
        self.last_recovery_diagnostic = diagnostic
        if self.on_recovery_diagnostic is not None:
            self.on_recovery_diagnostic(diagnostic)

    def action(self, action_id: int) -> Mapping[str, Any]:
        facts = row_facts(self.owned.connection, "actions", action_id)
        if facts is None:
            raise LookupError(f"动作不存在: {action_id}")
        return facts

    def last_attempt(self, responsibility: str):
        """读取该责任的最近尝试：状态、效果与意图事实时刻。"""
        from contextlib import closing

        with closing(self.owned.connection.execute(
            "SELECT a.status, a.effect_state, e.occurred_at FROM operation_attempts a"
            " JOIN operation_runs r ON a.run_id = r.id"
            " JOIN history_events e ON e.id = a.intent_event_id"
            " WHERE r.responsibility_key = ? ORDER BY a.id DESC LIMIT 1",
            (responsibility,),
        )) as cursor:
            return cursor.fetchone()

    def grant(self, action: Mapping[str, Any]):
        request = GrantRequest(
            device_id=action["device_id"],
            action_id=action["id"],
            window=self.window_of(action),
            trusted_wall_now=self.wall_us(),
            config=(self.start_config if action["type"] == int(_ACTION_TYPE.CAMERA_RECORD)
                    else AttemptConfig(max_attempts=1, timeout_s=Decimal("30"))),
            occurred_at=self.wall_us(),
        )
        outcome = self.scheduling.grant_start(
            request, new_operation_key(), self.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"启动授予事务未可靠完成: {outcome.error}")
        result = outcome.value
        if result.outcome.value != "granted":
            return None, result.reason
        return result.ticket, None

    def finish(self, ticket, outcome: CallOutcome, *,
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
               canceled_unstarted: bool = False) -> None:
        """先持有完整实际结果，再保存原尝试及其适用的伴随事实。

        保存重试等待使用调用返回时的单调读数；流程结束清除。
        """
        attempt = AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, self.evidence),
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
            action_failure=action_failure, canceled_unstarted=canceled_unstarted)
        self.pending_start_results[identity] = pending
        self._save_call_result(identity, pending)

    def _write_call_result(self, pending: PendingCallResult):
        """原请求的固定派生输入由各仓储在事务内核对可靠资格。"""
        ticket = pending.finish.ticket
        run = row_facts(self.owned.connection, "operation_runs", ticket.run_id)
        if run is None:
            raise ConsistencyError("原调用结果缺少所属流程")
        if not pending.result_disposition_ready:
            raise ConsistencyError("原 RESULTS 仍持有，所属消费者尚未确定结果处置")
        if pending.result_set is not None:
            if run["kind"] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS):
                raise ConsistencyError("集合结论必须使用原 RESULTS 责任")
            return self.capture.finish_result_check(
                pending.finish, pending.result_set, pending.key, self.owned)
        is_start = run["kind"] == int(_RUN_KIND.START) or (
            run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
            and run["query_purpose"] == int(_QUERY_PURPOSE.START_CONFIRMATION))
        if not is_start:
            if (pending.observation is not None or pending.start_finish is not None
                    or pending.action_failure is not None or pending.expiration is not None
                    or pending.canceled_unstarted):
                raise ConsistencyError("普通调用结果不能携带启动专属派生请求")
            return self.operations.finish_attempt(pending.finish, pending.key, self.owned)
        action_finish = pending.action_finish
        if pending.action_failure is not None:
            action_finish = FinishCapture(
                run["action_id"], (), OutputCatalogFacts(run["action_id"], True),
                pending.finish.occurred_at, failure=pending.action_failure)
        if pending.canceled_unstarted:
            action_finish = FinishCanceledCapture(
                run["action_id"], pending.finish.occurred_at, unstarted=True)
        return self.capture.finish_start_result(
            pending.finish, pending.observation, pending.key, self.owned,
            start_finish=pending.start_finish, action_finish=action_finish,
            expiration=pending.expiration)

    def hold_call_result(
        self, ticket: AttemptTicket, outcome: CallOutcome, *,
        occurred_at: int, returned_ns: int,
        preparation: RecordingStartPreparation | StartConfirmationPreparation | None = None,
        result_listing: tuple[ObservedFile, ...] | None = None,
    ) -> None:
        """在实际 await 返回后登记原结果，派生读取之前建立内存责任。"""
        identity = (ticket.run_id, ticket.attempt_id)
        if identity in self.pending_start_results:
            raise ConsistencyError("原调用结果仍待核实，不能替换实际结果")
        self.pending_start_results[identity] = PendingCallResult(
            new_operation_key(), AttemptFinish(
                ticket, validate_outcome(ticket, outcome, self.evidence), occurred_at),
            None, returned_ns=returned_ns, preparation=preparation,
            result_listing=result_listing,
            result_disposition_ready=result_listing is None)

    def save_held_result(self, ticket: AttemptTicket) -> None:
        """原执行者与恢复入口使用同一个已持有结果。"""
        identity = (ticket.run_id, ticket.attempt_id)
        pending = self.pending_start_results.get(identity)
        if pending is None or pending.finish.ticket != ticket:
            raise ConsistencyError("原调用结果尚未持有或票据不符")
        self._save_call_result(identity, pending)

    def _save_call_result(self, identity, pending: PendingCallResult) -> None:
        """最多重送一次原事务；可靠回滚与提交后错误使用同一身份。"""
        for _ in range(2):
            try:
                if pending.preparation is not None:
                    pending = _prepare_held_start_result(self, pending)
                    self.pending_start_results[identity] = pending
                receipt = self._write_call_result(pending)
                error = receipt.error
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
                    action_id = self.action_id_of_ticket(ticket)
                    action = self.action(action_id)
                    _recording_port(self).anchor_confirmed(
                        action_id, pending.confirmation_anchor_ns,
                        recording_stop_target(pending.confirmation_anchor_ns,
                                              _target_duration_ms(action)))
                del self.pending_start_results[identity]
                return
            if self.owned.connection.in_transaction:
                # 当前连接内的新投影不是可靠提交。结束事务后才能重送原键。
                try:
                    self.owned.connection.execute("ROLLBACK")
                except (DatabaseAccessError, ConsistencyError) as error:
                    raise ConsistencyError("原调用结果事务无法可靠结束，原结果仍持有") from error
        raise ConsistencyError(f"原调用结果未可靠保存，原结果仍持有: {error}")

    def resume_start_results(self, action_id: int) -> None:
        """同会话原真实结果优先保存，包括动作已经终态的分区。"""
        for identity, pending in tuple(self.pending_start_results.items()):
            if not pending.result_disposition_ready:
                continue
            if self.action_id_of_ticket(pending.finish.ticket) == action_id:
                self._save_call_result(identity, pending)

    def retry_wait_remaining(self, responsibility: str,
                             interval_s: Decimal | None, *,
                             maximum: int | None = None) -> Decimal | None:
        """责任当前重试等待的剩余秒数；可开始下一次尝试时为 None。

        以流程行的等待标志与累计次数为权威（首次尝试不预先等待、
        预算耗尽即时交还意图事务），跨会话恢复的等待按本次间隔从
        本会话首次观察重新计时。
        """
        with closing(self.owned.connection.execute(
            "SELECT attempts_used, retry_wait_required, max_attempts_used"
            " FROM operation_runs WHERE responsibility_key = ?",
            (responsibility,),
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return self.retry_gate.remaining(
            responsibility,
            attempts_used=int(row[0]),
            retry_wait_required=int(row[1]) == 1,
            max_attempts_used=int(row[2]) if maximum is None else maximum,
            interval_s=interval_s,
            now_ns=self.monotonic_ns())

    def recover_attempt(self, ticket: AttemptTicket) -> bool:
        """可靠旧会话边界结束原前台发令；未知设备活动另行核实。"""
        identity = (ticket.run_id, ticket.attempt_id)
        pending = self.pending_start_results.get(identity)
        if pending is not None:
            self._save_call_result(identity, pending)
            self.last_recovery_diagnostic = None
            return True
        with closing(self.owned.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ? AND attempt_no = ?",
            (ticket.run_id, ticket.attempt_id))) as cursor:
            found = cursor.fetchone()
        if found is None:
            raise ConsistencyError("原恢复尝试不存在")
        attempt = row_facts(self.owned.connection, "operation_attempts", found[0])
        if attempt["status"] != int(_ATTEMPT_STATUS.RUNNING):
            self.last_recovery_diagnostic = None
            return True
        blocked = None
        if self.recovery_boundary is RecoveryBoundary.UNCONFIRMED:
            blocked = RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
        elif self.recovery_max_event_id is None:
            blocked = RecoveryBlockedReason.MISSING_HORIZON
        elif self.recovery_evidence_for is None:
            blocked = RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP
        if blocked is not None:
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                blocked, ticket.run_id, ticket.attempt_id))
            return False
        if (attempt["intent_event_id"] is None
                or attempt["intent_event_id"] > self.recovery_max_event_id):
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON,
                ticket.run_id, ticket.attempt_id))
            return False
        run = row_facts(self.owned.connection, "operation_runs", ticket.run_id)
        if run is None:
            raise ConsistencyError("原恢复尝试缺少流程")
        owner_id = run["action_id"]
        if (run["kind"] in (int(_RUN_KIND.STOP_RESIDUAL), int(_RUN_KIND.EMERGENCY_STOP))
                or (run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
                    and run["query_purpose"] == int(_QUERY_PURPOSE.RESIDUAL_STOP_CONFIRMATION))):
            activity = row_facts(self.owned.connection, "device_activities", run["activity_id"])
            if activity is None:
                raise ConsistencyError("原停止恢复责任缺少活动")
            owner_id = activity["action_id"]
        evidence = self.recovery_evidence_for(_binding(self.action(owner_id)), ticket.operation)
        if evidence is None:
            self.record_recovery_diagnostic(RecoveryDiagnostic(
                RecoveryBlockedReason.EVIDENCE_UNAVAILABLE,
                ticket.run_id, ticket.attempt_id))
            return False
        # RUNNING 意图不携带原调用结果。活动已有确认在其原记录中保持，
        # 本次恢复不为原发令补造退出信息、观察或发生时刻。
        recovered = CallOutcome(
            status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
            error=ErrorValue("result_not_saved", "recovery"),
            settlement=Settlement(SettlementBasis.ASSUMED,
                                  EvidenceValue("adb_foreground_recovery", 1, {})))
        receipt = self.operations.finish_attempt(
            AttemptFinish(ticket, validate_outcome(ticket, recovered, evidence), self.wall_us()),
            new_operation_key(), self.owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原调用恢复结果未可靠保存: {receipt.error}")
        self.last_recovery_diagnostic = None
        return True


class SessionRecordingState:
    """录像中段事实的生产装载器。

    启动确认与停止确认取自 start/stop 责任的最近尝试；停止次数与
    在途取自停止流程行。计时锚点是本进程会话的单调钟读数（规格：
    重启后旧读数不能与新会话组合，须先对账），由装配层在启动确认
    后登记；未登记时按跨会话处理进入对账分区。推进循环每轮重建运
    行时时，装配层传入同一会话共享的锚点表，锚点跨轮保留。
    """

    def __init__(self, runtime: "CaptureRuntime",
                 anchors: dict[int, tuple[int, int]] | None = None) -> None:
        self._runtime = runtime
        self._anchors: dict[int, tuple[int, int]] = (
            {} if anchors is None else anchors)

    def anchor_confirmed(self, action_id: int, anchor_ns: int,
                         stop_target_ns: int) -> None:
        self._anchors[action_id] = (anchor_ns, stop_target_ns)

    def has_session_anchor(self, action_id: int) -> bool:
        """本会话是否持有该动作的启动确认锚点。"""
        return action_id in self._anchors

    def recording_state(self, action_id: int) -> RecordingState:
        runtime = self._runtime
        action = runtime.action(action_id)
        start = runtime.last_attempt(f"start/{action_id}")
        started = (start is not None
                   and start[1] == int(_EFFECT_STATE.CONFIRMED))
        activity = row_facts(runtime.owned.connection, "device_activities",
                             _activity_id_of(runtime, action_id))
        started = started or activity["started_at"] is not None
        with closing(runtime.owned.connection.execute(
            "SELECT r.id, r.attempts_used, r.max_attempts_used,"
            " (SELECT a.status FROM operation_attempts a WHERE a.run_id = r.id"
            "  ORDER BY a.id DESC LIMIT 1),"
            " (SELECT a.effect_state FROM operation_attempts a WHERE a.run_id = r.id"
            "  ORDER BY a.id DESC LIMIT 1)"
            " FROM operation_runs r WHERE r.responsibility_key = ?",
            (f"stop/{action_id}",),
        )) as cursor:
            stop_run = cursor.fetchone()
        used, maximum, in_flight = 0, 3, False
        stop_confirmed = False
        if stop_run is not None:
            used, maximum = int(stop_run[1]), int(stop_run[2])
            in_flight = stop_run[3] == int(_ATTEMPT_STATUS.RUNNING)
            stop_confirmed = (
                stop_run[3] == int(_ATTEMPT_STATUS.SUCCEEDED)
                and stop_run[4] == int(_EFFECT_STATE.CONFIRMED))
        anchor = self._anchors.get(action_id)
        stop_target = anchor[1] if anchor is not None else None
        return RecordingState(
            action_terminal=action["status"] in _ACTION_TERMINAL,
            started_confirmed=started,
            anchor_from_current_session=anchor is not None,
            stop_target_ns=stop_target,
            monotonic_now_ns=self._runtime.monotonic_ns(),
            stop_confirmed=stop_confirmed,
            file_complete_guaranteed=stop_confirmed,
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


def _save_activity(runtime: CaptureRuntime, action_id: int, **facts) -> None:
    """把发送或启动观察交活动观察边界落库；被拒按一致性错误上抛。"""
    receipt = runtime.capture.save_activity_observation(
        ActivityObservationSave(
            action_id=action_id, occurred_at=runtime.wall_us(), **facts),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


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
    if runtime.last_attempt(f"start/{action['id']}") is None:
        from camctl.capture.residual import pass_residual_gate

        if not await pass_residual_gate(runtime, action):
            return HandlerOutcome("not_granted")
    ticket, reason = runtime.grant(action)
    if ticket is None:
        return HandlerOutcome("not_granted", reason)
    result = await runtime.driver.control(ControlRequest(
        operation=operation,
        binding=_binding(action),
        params=action["effective_params_json"],
    ))
    outcome, confirmed = _operation_outcome(result, confirmed_observation)
    # 返回时取得事实时间；结果事务和日志耗时不能改变设备返回锚点。
    captured_facts = None
    if activity_facts is not None and result.error is None:
        captured_facts = (activity_facts(confirmed) if callable(activity_facts)
                          else activity_facts)
    try:
        if result.error is not None:
            runtime.finish(
                ticket, outcome, end_run=RunOutcome.FAILED,
                run_error=outcome.error)
        elif confirmed:
            runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
        else:
            runtime.finish(ticket, outcome)
    except OutcomeValidationError:
        # 观察与收场依据不符合登记契约：该结果整体不可采纳，不落
        # 库、不保存派发成功事实，按调用失败保存尝试终局。
        rejected, _ = _operation_outcome(
            DeviceCallResult(observations=(), error={
                "code": "invalid_device_result",
                "message": "驱动结果不符合登记契约"}),
            confirmed_observation)
        runtime.finish(
            ticket, rejected, end_run=RunOutcome.FAILED,
            run_error=rejected.error)
        return HandlerOutcome("call_failed", "invalid_device_result")
    if captured_facts is not None:
        # 调用已可靠返回：派发状态推进到成功返回。
        facts = {"dispatch_state": int(_DISPATCH_STATE.SUCCESS_RETURNED),
                 **captured_facts}
        _save_activity(runtime, action["id"], **facts)
    if result.error is not None:
        return HandlerOutcome("call_failed", "device_error")
    return HandlerOutcome("confirmed" if confirmed else "sent")


def _prepare_recording_start_result(runtime, pending, preparation):
    """用原返回事实形成启动派生请求；保存重送保持该请求。"""
    dispatch = preparation.dispatch
    ticket, outcome = pending.finish.ticket, pending.finish.outcome.outcome
    action_id = runtime.action_id_of_ticket(ticket)
    current = runtime.action(action_id)
    observation = None
    if dispatch.confirmed or dispatch.sent_only:
        facts = {"dispatch_state": int(_DISPATCH_STATE.SUCCESS_RETURNED)}
        activity = row_facts(runtime.owned.connection, "device_activities",
                             _activity_id_of(runtime, action_id))
        if activity["sent_at"] is None:
            facts["sent_at"] = dispatch.confirmed_at
        if dispatch.confirmed:
            if activity["started_at"] is None:
                facts["started_at"] = dispatch.confirmed_at
            if activity["activity_state"] != 2:
                facts["activity_state"] = 2
        observation = ActivityObservationSave(
            action_id, dispatch.confirmed_at, **facts)
    elif outcome.effect is EffectState.NO_EFFECT:
        observation = ActivityObservationSave(
            action_id, dispatch.confirmed_at,
            dispatch_state=int(_DISPATCH_STATE.REJECTED_WITHOUT_EFFECT))
    failure = None
    expiration = None
    end_run = RunOutcome.SUCCEEDED if dispatch.confirmed else None
    run_error = None
    if outcome.effect is EffectState.NO_EFFECT and not current["cancel_requested"]:
        from camctl.scheduling.rules import WindowPhase, window_phase

        run = row_facts(runtime.owned.connection, "operation_runs", ticket.run_id)
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


def _prepare_start_confirmation_result(runtime, pending, preparation):
    """启动核实派生只使用原活动、实际观察时刻与原单调读数。"""
    activity = preparation.activity
    ticket, call = pending.finish.ticket, pending.finish.outcome.outcome
    observed_at, anchor = pending.finish.occurred_at, pending.returned_ns
    action_id = runtime.action_id_of_ticket(ticket)
    confirmed = any(observation.type == "activity_status"
                    and observation.data.get("activity_id") == str(activity["id"])
                    for observation in call.observations)
    current = runtime.action(action_id)
    with closing(runtime.owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id = ?", (ticket.run_id,))) as cursor:
        used = cursor.fetchone()[0]
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


def _prepare_held_start_result(runtime, pending):
    preparation = pending.preparation
    if isinstance(preparation, RecordingStartPreparation):
        return _prepare_recording_start_result(runtime, pending, preparation)
    if isinstance(preparation, StartConfirmationPreparation):
        return _prepare_start_confirmation_result(runtime, pending, preparation)
    raise ConsistencyError("原调用结果缺少合法的启动准备责任")


async def _record_start_once(runtime: CaptureRuntime, action) -> HandlerOutcome:
    """将真实控制端口适配到一次启动骨架，事实时间先于保存。"""
    if runtime.last_attempt(f"start/{action['id']}") is None:
        from camctl.capture.residual import pass_residual_gate

        if not await pass_residual_gate(runtime, action):
            return HandlerOutcome("not_granted")

    class Grants:
        def grant(self, request):
            ticket, reason = runtime.grant(runtime.action(action["id"]))
            return GrantDecision("granted" if ticket is not None else "rejected",
                                 ticket=ticket, reason=reason)

    class Wall:
        def now_us(self):
            return runtime.wall_us()

    class Driver:
        dispatch = None

        async def start(self, ticket):
            result = await runtime.driver.control(ControlRequest(
                "start_recording", _binding(action), action["effective_params_json"],
                ticket=ticket, timeout_s=runtime.start_config.timeout_s))
            received_at, anchor_ns = runtime.wall_us(), runtime.monotonic_ns()
            outcome, confirmed = _operation_outcome(result, "start_confirmed")
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
                preparation=RecordingStartPreparation(self.dispatch, runtime.start_config.max_attempts))
            return self.dispatch

    driver = Driver()

    class Finishes:
        def finish(self, ticket, outcome):
            dispatch = driver.dispatch
            assert dispatch is not None
            runtime.save_held_result(ticket)

        def finish_prevented(self, ticket, reason):
            current = runtime.action(action["id"])
            now = runtime.wall_us()
            prevented = CallOutcome(
                status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                error=ErrorValue(reason, "dispatch"),
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                      EvidenceValue("dispatch_prevented", 1, {})))
            runtime.finish(ticket, prevented, activity=ActivityObservationSave(
                action["id"], now,
                dispatch_state=int(_DISPATCH_STATE.NOT_DISPATCHED)), occurred_at=now,
                expiration=None if current["cancel_requested"] else ExpireActionRequest(
                    action["id"], now, now),
                canceled_unstarted=bool(current["cancel_requested"]))

    step = await start_recording(CaptureContext(
        device_id=action["device_id"], action_id=action["id"],
        window=runtime.window_of(action), config=runtime.start_config,
        duration=_target_duration_ms(action), trusted_wall_now=runtime.wall_us(),
        wall=Wall(), grants=Grants(), driver=driver, finishes=Finishes(),
        canceled_now=lambda: bool(runtime.action(action["id"])["cancel_requested"])))
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


def _register_observed(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
    *, occurred_at: int | None = None,
    registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None,
) -> tuple[tuple[CaptureFile, int], ...]:
    """把结果列举观察落库：发现、任务归属与完成事实一次登记。

    预览条目按驱动配对关联登记预览角色并保留配对证据；配对由
    validate_observed_pairings 先行校验。
    """
    # 第一阶段按列举顺序保存发现与在场事实，并建立身份到文件主
    # 键的映射供配对解析。
    file_ids: dict[str, int] = {}
    saved_files = {} if registered_files is None else registered_files
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
                command, new_operation_key(), entries, saved_files)
        elif (not _same_file_request(pending.command, command)
                or not _same_observed_files(pending.entries, entries)
                or pending.registered_files != saved_files):
            raise ConsistencyError("待存文件发现的原完整请求不可替换")
    runtime._save_file_observations(action_id)
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
                or row_facts(runtime.owned.connection, "device_files", observed.file_id)["presence_state"]
                   != int(_FILE_PRESENCE.PRESENT)):
            runtime._save_file_fact(identity, FileFactStage.PRESENCE, FilePresenceSave(
                file_id=observed.file_id, state=int(_FILE_PRESENCE.PRESENT), occurred_at=occurred))
    validate_observed_pairings(entries)
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
                    method=_TASK_SCOPE,
                    role=_ORIGINAL_ROLE,
                    observation=entry.evidence,
                    occurred_at=occurred,
                )
        else:
            ownership = OwnershipSave(
                    file_id=file_id,
                    source_action_id=action_id,
                    method=_TASK_SCOPE,
                    role=_PREVIEW_ROLE,
                    observation=entry.evidence,
                    occurred_at=occurred,
                    paired_device_file_id=file_ids[entry.paired_identity],
                    pairing_observation={
                        "paired_identity": entry.paired_identity},
                )
        runtime._save_file_fact(identity, FileFactStage.OWNERSHIP, ownership)
        if entry.complete:
            runtime._save_file_fact(identity, FileFactStage.COMPLETION, FileCompletionSave(
                    file_id=file_id,
                    state=3,
                    occurred_at=occurred,
                    basis=_DEVICE_GUARANTEE,
                    observation=entry.evidence,
                    size_bytes=entry.size_bytes,
                ))
        registered.append((
            CaptureFile(
                file_id=entry.identity, kind=entry.kind, complete=entry.complete,
                ownership_confirmed=True),
            file_id,
        ))
    result_files = {file.file_id: (file, file_id) for file, file_id in registered}
    for identity, pending in tuple(runtime.pending_start_results.items()):
        if (_same_observed_files(pending.result_listing, entries)
                and runtime.action_id_of_ticket(pending.finish.ticket) == action_id):
            runtime.pending_start_results[identity] = replace(pending, result_registered_files=result_files)
    for entry in entries:
        runtime.pending_file_observations.pop((action_id, entry.identity), None)
    return tuple(registered)


def _conclude_activity(runtime: CaptureRuntime, action_id: int) -> None:
    """成功链收场活动：结束观察与占用释放同事务，幂等可重入。"""
    receipt = runtime.capture.conclude_activity(
        ActivityConcludeSave(
            action_id=action_id, occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _settle_open_start(runtime: CaptureRuntime, action_id: int, status: RunOutcome,
                       error: ErrorValue | None = None) -> None:
    """动作终态后收场仍开放的启动流程，幂等可重入。

    启动尝试在途（进程中断或结果不可保存）时流程行保持执行中，
    但动作终态后不再有后续启动尝试；仍开放的流程按动作的最终
    结果结束并清除重试等待，否则会话的流程收尾计数无法归零。
    """
    receipt = runtime.operations.finish_stale_runs(
        StaleRunFinish(
            responsibility_keys=(f"start/{action_id}",),
            status=status,
            error=error,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _settle_input_read_runs(runtime: CaptureRuntime, action_id: int,
                            status: RunOutcome,
                            failure: RecordingFailure | None) -> None:
    """动作终态后收场录像输入副本的读取流程，幂等可重入。

    授予输入副本时同步建立其读取流程行（读取机会排序依赖），但
    输入链的分段推进不驱动该流程行；动作终态后输入读取不再有后
    续工作，仍开放的流程按动作的最终结果结束，否则会话的流程收
    尾计数无法归零。失败结果携带动作最终错误，阶段取公共登记。
    """
    with closing(runtime.owned.connection.execute(
        "SELECT fc.id FROM file_copies fc"
        " JOIN recording_processing rp ON rp.id = fc.processing_id"
        " WHERE rp.action_id = ? AND fc.delivery_id IS NULL",
        (action_id,),
    )) as cursor:
        keys = tuple(f"read/{int(row[0])}" for row in cursor.fetchall())
    if not keys:
        return
    error = None
    if failure is not None:
        error = ErrorValue(
            code=failure.code,
            stage=registered_error(failure.code)["stage"],
            details=dict(failure.details))
    receipt = runtime.operations.finish_stale_runs(
        StaleRunFinish(
            responsibility_keys=keys,
            status=status,
            error=error,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _settle_start_without_sent_at(
        runtime: CaptureRuntime, action, attempt) -> bool:
    """可能派发但没有可靠发送时间的启动按无法核实收场。

    启动尝试已存在而活动没有发送时间，说明调用可能在途或结果未
    保存：录像与延时的时间及产物完成判定都依赖可靠发送时间，无
    法核实原任务。按恢复规则不重复启动、不补造时间——调用已有
    明确失败结果时按设备失败收场，否则按无法确认收场；活动占用
    保持未知，不伪造释放依据。已收场返回 True。
    """
    activity_id = _activity_id_of(runtime, action["id"])
    with closing(runtime.owned.connection.execute(
        "SELECT sent_at FROM device_activities WHERE id = ?", (activity_id,),
    )) as cursor:
        sent_at = cursor.fetchone()[0]
    if sent_at is not None:
        return False
    if attempt[0] == int(_ATTEMPT_STATUS.FAILED):
        _finish_capture(
            runtime, action["id"], (), FileKind.VIDEO,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action["id"]),
                         "reason": "device_failed"}))
        return True
    _settle_open_start(
        runtime, action["id"], RunOutcome.UNCONFIRMED,
        error=ErrorValue(code="result_unconfirmed", stage="device"))
    _finish_capture(
        runtime, action["id"], (), FileKind.VIDEO,
        failure=RecordingFailure(
            code="capture_result_unconfirmed",
            details={"activity_id": str(action["id"]),
                     "reason": "start_unknown"}))
    return True


def _stop_run_id(runtime: CaptureRuntime, action_id: int) -> int:
    """读取动作停止流程的主键；预算耗尽收场前流程必然存在。"""
    with closing(runtime.owned.connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ?",
        (f"stop/{action_id}",),
    )) as cursor:
        found = cursor.fetchone()
    if found is None:
        raise LookupError(f"停止流程不存在: {action_id}")
    return int(found[0])


def _close_stop_exhausted(runtime: CaptureRuntime, action) -> RecordingFailure:
    """保存停止预算耗尽的原流程结果；活动效果及占用保持原事实。"""
    activity_id = _activity_id_of(runtime, action["id"])
    error = ErrorValue(
        code="recording_stop_failed", stage="device_stop",
        details={"activity_id": str(activity_id),
                 "operation_run_id": str(_stop_run_id(runtime, action["id"]))})
    receipt = runtime.operations.finish_stale_runs(
        StaleRunFinish(
            responsibility_keys=(f"stop/{action['id']}",),
            status=RunOutcome.UNCONFIRMED,
            error=error,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return RecordingFailure(code=error.code, details=dict(error.details))


def _settle_stop_exhausted(runtime: CaptureRuntime, action) -> None:
    """停止预算耗尽的收场：停止流程与动作按未确认失败终态化。

    原停止预算内没有取得可靠停止确认时，停止流程按未确认收场并
    携带 recording_stop_failed，动作以零产物登记失败终态。设备活
    动缺少结束与释放依据，执行中事实与占用原样保留，由后续动作
    触发的残留收场或取消收场处理（camera-recovery.md#停止预算
    耗尽后的收场责任）。幂等：动作终态后重入走处理器终态分支。
    """
    failure = _close_stop_exhausted(runtime, action)
    _finish_capture(
        runtime, action["id"], (), FileKind.VIDEO,
        failure=failure)


def _recording_port(context: CaptureRuntime) -> RecordingStatePort:
    """会话内缓存录像中段事实装载器；计时锚点跨推进保留。"""
    if context.recording_state is None:
        context.recording_state = SessionRecordingState(context)
    return context.recording_state


def _activity_id_of(runtime: CaptureRuntime, action_id: int) -> int:
    """查询动作的唯一设备活动主键；动作与活动的主键不重合。"""
    from contextlib import closing

    with closing(runtime.owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action_id,),
    )) as cursor:
        found = cursor.fetchone()
    if found is None:
        raise LookupError(f"设备活动不存在: {action_id}")
    return int(found[0])


async def _stop_call(runtime: CaptureRuntime, action,
                     operation: str = "stop_recording") -> HandlerOutcome:
    """按原停止预算发起一次设备停止调用并保存尝试结果。

    意图先提交才派发；可靠确认结束停止流程，错误或未确认保持流
    程执行中并建立重试等待，预算沿原流程累计不刷新。停止操作字
    面量由调用方按任务类型提供。重试等待的间隔未到时不提交新意
    图，由推进循环下一轮再判。
    """
    if runtime.stopper is None:
        raise LookupError("设备停止端口未装配")
    remaining = runtime.retry_wait_remaining(
        f"stop/{action['id']}", runtime.stop_config.retry_interval_s)
    if remaining is not None:
        return HandlerOutcome("stop_retry_wait", f"{remaining}s")
    intent = AttemptIntent(
        operation="stop",
        action_id=action["id"],
        kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=_activity_id_of(runtime, action["id"])),
        query_purpose=None,
        config=runtime.stop_config,
        occurred_at=runtime.wall_us(),
    )
    outcome = runtime.operations.begin_attempt(
        intent, new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"停止意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    if outcome.value.disposition is not BeginDisposition.GRANTED:
        return HandlerOutcome("stop_not_granted", outcome.value.reason)
    ticket = outcome.value.ticket
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
    try:
        if confirmed:
            runtime.finish(ticket, call, end_run=RunOutcome.SUCCEEDED,
                           occurred_at=returned_at, returned_ns=returned_ns)
        else:
            runtime.finish(ticket, call, retry_wait=True,
                           occurred_at=returned_at, returned_ns=returned_ns)
    except OutcomeValidationError:
        # 观察与收场依据不符合登记契约：该结果整体不可采纳，不落
        # 库，按调用失败保存尝试终局（与启动调用同规则），不遗留
        # 执行中的停止流程。
        rejected, _ = _operation_outcome(
            DeviceCallResult(observations=(), error={
                "code": "invalid_device_result",
                "message": "驱动结果不符合登记契约"}),
            "stop_confirmed", evidence_type="stop_returned")
        runtime.finish(
            ticket, rejected, end_run=RunOutcome.FAILED,
            run_error=rejected.error, occurred_at=returned_at, returned_ns=returned_ns)
        return HandlerOutcome("call_failed", "invalid_device_result")
    if response.error is not None:
        return HandlerOutcome("stop_failed", "device_error")
    return HandlerOutcome("confirmed" if confirmed else "sent")


def _finish_canceled_capture(runtime: CaptureRuntime, action_id: int, *,
                             unstarted: bool = False) -> None:
    """取消终态：放弃内容，不登记正式产物。"""
    receipt = runtime.capture.finish_canceled_capture(
        FinishCanceledCapture(
            action_id=action_id, occurred_at=runtime.wall_us(), unstarted=unstarted),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    _settle_input_read_runs(
        runtime, action_id, RunOutcome.CANCELED, None)


def _settle_unstarted_attempt(runtime: CaptureRuntime, action, attempt) -> bool:
    """全部原尝试已可靠无效果时，按当前取消与启动窗口本地结束。"""
    if (attempt is None or attempt[0] == int(_ATTEMPT_STATUS.RUNNING)
            or attempt[1] != int(_EFFECT_STATE.NO_EFFECT)):
        return False
    from camctl.persistence.repositories.capture_facts import load_start_facts
    from camctl.persistence.repositories.scheduling import ExpireActionRequest, ExpireOutcome
    from camctl.scheduling.rules import WindowPhase, window_phase

    facts = load_start_facts(runtime.owned.connection, action)
    if not facts.not_started:
        return False
    if action["cancel_requested"]:
        _finish_canceled_capture(runtime, action["id"], unstarted=True)
        return True
    now = runtime.wall_us()
    if window_phase(runtime.window_of(action), now) is not WindowPhase.AFTER_WINDOW:
        return False
    result = runtime.scheduling.expire_action(
        ExpireActionRequest(action["id"], now, now), new_operation_key(), runtime.owned)
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"可靠无效果启动的过期事务未完成: {result.error}")
    return result.value.outcome is ExpireOutcome.EXPIRED


def _catalog_drafts(
    registered: tuple[tuple[CaptureFile, int], ...],
    entries: tuple[ObservedFile, ...],
) -> tuple[OutputDraft, ...]:
    """按登记结果构造产物目录草稿：原片与预览按配对分别登记。"""
    file_ids = {
        entry.identity: file_id
        for (_, file_id), entry in zip(registered, entries)}
    drafts = []
    for (_, file_id), entry in zip(registered, entries):
        if not entry.complete:
            continue
        if entry.paired_identity is None:
            drafts.append(OutputDraft(
                kind=OutputKind.ORIGINAL,
                file=FileReference(device_file_id=file_id),
                file_complete=True))
        else:
            drafts.append(OutputDraft(
                kind=OutputKind.PREVIEW,
                file=FileReference(device_file_id=file_id),
                file_complete=True,
                original_batch_file_id=file_ids[entry.paired_identity]))
    return tuple(drafts)


def _finish_capture(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
    required: FileKind, *, failure: RecordingFailure | None = None,
    registered=None, repair_file_id: int | None = None,
) -> HandlerOutcome:
    """C6 核实产物集合并保存终态；失败保留完整且归属明确的文件。

    registered 传入已登记的观察结果避免重复登记；repair_file_id
    携带修复成功的成品时与原片同事务登记为 REPAIRED 产物。
    """
    if registered is None:
        registered = _register_observed(runtime, action_id, entries)
    assessment = assess_capture_files(
        CaptureFileSet(files=tuple(file for file, _ in registered), set_finalized=False),
        ProductRequirements(required_kinds=frozenset({required})),
    )
    drafts = _catalog_drafts(registered, entries)
    if repair_file_id is not None:
        original_file_ids = [
            file_id for (_, file_id), entry in zip(registered, entries)
            if entry.complete and entry.paired_identity is None]
        if original_file_ids:
            drafts = drafts + (OutputDraft(
                kind=OutputKind.REPAIRED,
                file=FileReference(intermediate_file_id=repair_file_id),
                file_complete=True,
                original_batch_file_id=original_file_ids[0],
            ),)
    if failure is None and not assessment.is_complete:
        return HandlerOutcome("files_incomplete", str(assessment.missing_kinds))
    receipt = runtime.capture.finish_capture(
        FinishCapture(
            action_id=action_id,
            drafts=drafts,
            catalog_facts=OutputCatalogFacts(
                action_id=action_id, ownership_confirmed=True),
            occurred_at=runtime.wall_us(),
            failure=failure,
        ), new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    _settle_input_read_runs(
        runtime, action_id,
        RunOutcome.SUCCEEDED if failure is None else RunOutcome.FAILED,
        failure)
    return HandlerOutcome(
        "terminal", "failed" if failure is not None else "succeeded")


def _release_occupancy(runtime: CaptureRuntime, action_id: int) -> None:
    """按统一释放判定解除本活动占用；条件不满足保持原状。"""
    receipt = runtime.capture.release_occupancy(
        ActivityReleaseSave(
            action_id=action_id, occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


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


def _handle_binding_failure(
    runtime: CaptureRuntime, action: Mapping[str, Any],
) -> bool:
    """异常绑定不产生新调用；原结果可靠保存后共同保存业务收场。"""
    if runtime.binding_check is None:
        return False
    binding_result = runtime.binding_check(_binding(action))
    details = binding_failure_details(binding_result)
    if details is None:
        return False
    if _local_recording_listing(runtime, action) is not None:
        return False
    with closing(runtime.owned.connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
        " WHERE r.action_id = ? AND t.status = ? LIMIT 1",
        (action["id"], int(_ATTEMPT_STATUS.RUNNING)),
    )) as cursor:
        has_unfinished_attempt = cursor.fetchone() is not None
    if has_unfinished_attempt:
        with closing(runtime.owned.connection.execute(
            "SELECT DISTINCT r.responsibility_key, r.kind FROM operation_attempts t"
            " JOIN operation_runs r ON r.id = t.run_id"
            " WHERE r.action_id = ? AND t.status = ? ORDER BY r.id",
            (action["id"], int(_ATTEMPT_STATUS.RUNNING)),
        )) as cursor:
            unfinished = cursor.fetchall()
        for responsibility, kind in unfinished:
            if kind == int(_RUN_KIND.READ_FILE):
                from types import SimpleNamespace
                from camctl.outputs.read_attempts import read_host_boundary, record_read_diagnostic, running_read_ticket

                copy_id = runtime.owned.connection.execute(
                    "SELECT copy_id FROM operation_runs WHERE responsibility_key=?", (responsibility,)).fetchone()[0]
                original = running_read_ticket(runtime.owned, copy_id)
                if original is None:
                    raise ConsistencyError("内部读取绑定失败缺少原未结束尝试")
                ticket, intent_id = original
                read_scope = SimpleNamespace(
                    recovery_boundary=runtime.recovery_boundary, recovery_max_event_id=runtime.recovery_max_event_id,
                    continuing_read_tickets=runtime.continuing_read_tickets,
                    pending_read_results=runtime.pending_read_results, pending_read_ends=runtime.pending_read_ends,
                    pending_read_business=runtime.pending_read_business, owned=runtime.owned, operations=runtime.operations,
                    occurred_at=runtime.wall_us, last_recovery_diagnostic=runtime.last_recovery_diagnostic,
                    recovery_evidence_for=runtime.recovery_evidence_for,
                    on_recovery_diagnostic=runtime.record_recovery_diagnostic)
                from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result
                from camctl.capture.media_flow import _save_internal_read_and_settle

                if copy_id in read_scope.pending_read_ends:
                    request = _binding_failure_request(runtime, action, details, excluded_run_id=ticket.run_id)
                    business = PendingReadBusiness(runtime.capture.finish_binding_failure, request, new_operation_key())
                    actual = held_binding_read_result(read_scope, copy_id, binding_result, business)
                    if actual is not None:
                        _save_internal_read_and_settle(read_scope, actual)
                        runtime.timelapse_deadlines.pop(action["id"], None)
                        return True
                if not read_host_boundary(read_scope, ticket, intent_id, continuing=False):
                    return True
                if runtime.recovery_evidence_for is None:
                    record_read_diagnostic(read_scope, RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP, ticket)
                    return True
                source = runtime.owned.connection.execute(
                    "SELECT a.device_id,a.driver_id FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id"
                    " JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (copy_id,)).fetchone()
                if source is None:
                    raise ConsistencyError("内部读取绑定失败缺少原文件观察者")
                if runtime.recovery_evidence_for(DeviceBinding(*source), "read") is None:
                    record_read_diagnostic(read_scope, RecoveryBlockedReason.EVIDENCE_UNAVAILABLE, ticket)
                    return True
                from camctl.outputs.read_attempts import recover_unavailable_read

                if not recover_unavailable_read(read_scope, copy_id, DeviceBinding(*source), runtime.wall_us()):
                    return True
                continue
            operation = _RECOVERABLE_OPERATIONS.get(kind)
            if operation is None:
                return True
            ticket = _original_ticket(runtime, responsibility, operation)
            if ticket is None:
                raise ConsistencyError("绑定失败的未完成责任缺少原尝试票据")
            if not runtime.recover_attempt(ticket):
                return True
    if action["cancel_requested"]:
        from camctl.persistence.repositories.capture_facts import load_start_facts

        facts = load_start_facts(runtime.owned.connection, action)
        if facts.not_started:
            return False
        if facts.activity is not None and facts.activity["stop_supported"] == 0:
            return False
        with closing(runtime.owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key = ?",
            (f"stop/{action['id']}",),
        )) as cursor:
            stop = cursor.fetchone()
        if action["type"] == 2 and (
                facts.activity is not None and facts.activity["activity_state"] == 3
                or stop is not None and stop[0] not in (1, 2)):
            return False
    from camctl.outputs.read_attempts import save_read_business

    request = _binding_failure_request(runtime, action, details)
    internal_read = runtime.owned.connection.execute(
        "SELECT 1 FROM file_copies c JOIN recording_processing p ON p.id=c.processing_id"
        " WHERE p.action_id=? LIMIT 1", (action["id"],)).fetchone()
    receipt = (save_read_business(_read_result_scope(runtime), action["id"], "capture_binding_failure", request,
                                  runtime.capture.finish_binding_failure) if internal_read is not None
               else runtime.capture.finish_binding_failure(request, new_operation_key(), runtime.owned))
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"拍摄绑定失败的完整事务未完成（{receipt.kind.value}）: {receipt.error}")
    runtime.timelapse_deadlines.pop(action["id"], None)
    return True


def _binding_failure_request(runtime, action, details, *, excluded_run_id=None):
    with closing(runtime.owned.connection.execute(
        "SELECT id,responsibility_key FROM operation_runs WHERE action_id = ?"
        " AND kind IN (1, 2, 3, 6, 7) AND status IN (1, 2)"
        " AND (kind != 6 OR query_purpose != 5) ORDER BY id", (action["id"],),
    )) as cursor:
        responsibilities = tuple(row[1] for row in cursor.fetchall() if row[0] != excluded_run_id)
    return FinishBindingFailure(
        action_id=action["id"], occurred_at=runtime.wall_us(),
        failure=RecordingFailure(code="device_binding_unavailable", details=details),
        canceled=bool(action["cancel_requested"]), stop_config=runtime.stop_config,
        responsibility_keys=responsibilities, check_config=runtime.check_config)


async def _photo_handler(action_id: int, context: CaptureRuntime) -> None:
    context.resume_file_observations(action_id)
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    if _handle_binding_failure(context, action):
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            _finish_canceled_capture(context, action_id, unstarted=True)
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
        attempt = context.last_attempt(f"start/{action_id}")
    if _settle_unstarted_attempt(context, action, attempt):
        return
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        # 在途或本轮列举失败：已保存实际结果与重试等待，下一轮重新核实。
        return
    unconfirmed = (listing.phase is ListingPhase.CLOSED
        and row_facts(context.owned.connection, "operation_runs", listing.ticket.run_id)["status"]
            == int(enum_for("operation_runs.status").UNCONFIRMED))
    if listing.phase is ListingPhase.EXHAUSTED or unconfirmed:
        # 原无法确认结论保持；仅继续未完成的文件和业务收场。
        saved = listing if unconfirmed else _saved_result_listing(context, action_id)
        registered = _register_listing(context, action_id, saved)
        if not unconfirmed:
            _close_check_unconfirmed(context, action_id)
        _settle_open_start(
            context, action_id, RunOutcome.UNCONFIRMED,
            error=ErrorValue(code="result_unconfirmed", stage="device"))
        _finish_capture(context, action_id, saved.entries, FileKind.PHOTO, registered=registered,
                        failure=_unconfirmed_failure(context, action_id))
        return
    entries = listing.entries
    ticket = None if listing.already_saved else listing.ticket
    registered = _register_listing(context, action_id, listing)
    metadata = _result_file_metadata(context, listing.ticket)
    metadata.update((entry.identity, entry) for entry in listing.entries)
    entries, registered_files = _registered_result_files(context, action_id, metadata)
    registered = tuple(registered_files[entry.identity] for entry in entries)
    files = assess_capture_files(
        CaptureFileSet(files=tuple(file for file, _ in registered), set_finalized=False),
        ProductRequirements(required_kinds=frozenset({FileKind.PHOTO})),
    )
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
            _finish_listing_result(context, listing,
                           end_run=RunOutcome.SUCCEEDED)
        _settle_open_start(context, action_id, RunOutcome.SUCCEEDED)
        _conclude_activity(context, action_id)
        _finish_capture(context, action_id, entries, FileKind.PHOTO, registered=registered)
    elif decision is PhotoDecision.FAILED_KEEP_FILES:
        if ticket is not None:
            _finish_listing_result(context, listing,
                           end_run=RunOutcome.SUCCEEDED)
        _settle_open_start(
            context, action_id, RunOutcome.FAILED,
            error=ErrorValue(code="device_failed", stage="device"))
        _finish_capture(
            context, action_id, entries, FileKind.PHOTO, registered=registered,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action_id), "reason": "device_failed"}))
    elif ticket is not None:
        # 其余分区（等待响应、取消保留、未知无停止）：本轮成功结果与
        # 重试等待共同保存，等待下次推进或取消收场。
        _finish_listing_result(context, listing, retry_wait=True)


def _original_ticket(runtime: CaptureRuntime, responsibility: str, operation: str):
    with closing(runtime.owned.connection.execute(
        "SELECT r.id, a.attempt_no, r.activity_id FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id = r.id"
        " WHERE r.responsibility_key = ? ORDER BY a.attempt_no DESC LIMIT 1",
        (responsibility,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    return AttemptTicket(row[1], operation, None if row[2] is None else str(row[2]),
                         responsibility, row[0])


def _close_record_start(runtime, action, *, exhausted: bool, query_key: str | None = None):
    from camctl.persistence.repositories.capture_facts import load_start_facts

    facts = load_start_facts(runtime.owned.connection, action)
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
    receipt = runtime.capture.close_start(
        StaleRunFinish(responsibility_keys=keys, status=final, occurred_at=now, error=ErrorValue(
            failure.code, registered_error(failure.code)["stage"], failure.details)),
        FinishCapture(action["id"], (), OutputCatalogFacts(action["id"], True), now,
                      failure=failure), new_operation_key(), runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"启动收场未可靠保存: {receipt.error}")
    for key in keys:
        runtime.retry_gate.cleared(key)


async def _confirm_record_start(runtime, action, facts):
    """未知启动沿同一独立查询责任有限核实；空闲不证明从未启动。"""
    activity = facts.activity
    key = f"query/start/{action['id']}/{activity['id']}"
    latest = runtime.last_attempt(key)
    if latest is not None and latest[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = _original_ticket(runtime, key, "query")
        if not runtime.recover_attempt(ticket):
            return
    with closing(runtime.owned.connection.execute(
        "SELECT status, attempts_used FROM operation_runs WHERE responsibility_key = ?",
        (key,))) as cursor:
        query = cursor.fetchone()
    if runtime.state_query is None or not activity["state_query_supported"]:
        _close_record_start(runtime, action, exhausted=False, query_key=key)
        return
    if query is not None and (query[0] not in (1, 2)
                              or query[1] >= runtime.query_config.max_attempts):
        _close_record_start(runtime, action, exhausted=False, query_key=key)
        return
    if runtime.retry_wait_remaining(
            key, runtime.query_config.retry_interval_s,
            maximum=runtime.query_config.max_attempts) is not None:
        return
    intent = AttemptIntent(
        operation="query", action_id=action["id"], kind=OperationKind.QUERY_ACTIVITY,
        target=AttemptTarget(activity_id=activity["id"]), config=runtime.query_config,
        occurred_at=runtime.wall_us(), query_purpose=QueryPurpose.START_CONFIRMATION)
    receipt = runtime.operations.begin_attempt(intent, new_operation_key(), runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"启动核实意图未可靠提交: {receipt.error}")
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
    runtime.save_held_result(ticket)


async def _advance_record_start(runtime, action, attempt) -> bool:
    """返回 True 表示启动责任仍占本轮；False 表示进入已确认录像。"""
    from camctl.persistence.repositories.capture_facts import load_start_facts

    if attempt[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = _original_ticket(runtime, f"start/{action['id']}", "control")
        if not runtime.recover_attempt(ticket):
            return True
        attempt = runtime.last_attempt(f"start/{action['id']}")
    facts = load_start_facts(runtime.owned.connection, action)
    if action["cancel_requested"]:
        if facts.activity is not None:
            query_key = f"query/start/{action['id']}/{facts.activity['id']}"
            query_attempt = runtime.last_attempt(query_key)
            if query_attempt is not None and query_attempt[0] == int(_ATTEMPT_STATUS.RUNNING):
                if not runtime.recover_attempt(_original_ticket(runtime, query_key, "query")):
                    return True
        if facts.not_started:
            _finish_canceled_capture(runtime, action["id"], unstarted=True)
        else:
            await _advance_canceled_capture(runtime, action, attempt)
        return True
    if facts.activity["started_at"] is not None or attempt[1] == int(_EFFECT_STATE.CONFIRMED):
        return False
    if facts.not_started:
        if _settle_unstarted_attempt(runtime, action, attempt):
            return True
        if facts.attempts_used >= runtime.start_config.max_attempts:
            _close_record_start(runtime, action, exhausted=True)
            return True
        if runtime.retry_wait_remaining(
                f"start/{action['id']}", runtime.start_config.retry_interval_s,
                maximum=runtime.start_config.max_attempts) is not None:
            return True
        await _record_start_once(runtime, action)
        return True
    await _confirm_record_start(runtime, action, facts)
    return True


def _read_result_scope(runtime):
    from types import SimpleNamespace

    return SimpleNamespace(owned=runtime.owned, operations=runtime.operations, occurred_at=runtime.wall_us,
        pending_read_results=runtime.pending_read_results, pending_read_business=runtime.pending_read_business,
        pending_read_ends=runtime.pending_read_ends, continuing_read_tickets=runtime.continuing_read_tickets)


async def _resume_internal_read_results(runtime):
    if not runtime.pending_read_business and not runtime.pending_read_results and not runtime.pending_read_ends:
        return
    from camctl.capture.media_flow import _save_internal_read_and_settle
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    scope = _read_result_scope(runtime)
    retry_read_business(scope)
    for pending in tuple(scope.pending_read_results.values()):
        _save_internal_read_and_settle(scope, pending)
    await retry_read_ends(scope)


async def _record_handler(action_id: int, context: CaptureRuntime) -> None:
    context.resume_file_observations(action_id)
    await _resume_internal_read_results(context)
    context.resume_start_results(action_id)
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    if _handle_binding_failure(context, action):
        return
    open_start = context.last_attempt(f"start/{action_id}")
    if open_start is None:
        # 尚未发起启动：首次授予并调用，不依赖中段事实端口。
        if action["cancel_requested"]:
            _finish_canceled_capture(context, action_id, unstarted=True)
            return
        await _control_call(context, action, "start_recording", "start_confirmed")
        return
    if await _advance_record_start(context, action, open_start):
        return
    port = _recording_port(context)
    canceled = bool(action["cancel_requested"])
    decision = decide_recording_next(
        port.recording_state(action_id),
        RecordingFacts(canceled=canceled),
    )
    if decision.phase is RecordingPhase.NOT_RUNNING:
        if canceled:
            # 启动未确认时取消：不重新启动，收场归启动核实链。
            return
        # 已保存确认不得转入新的 START。缺少中段一致事实须暴露。
        raise ConsistencyError("已确认录像缺少可继续的录像状态")
        return
    if decision.phase is RecordingPhase.READY_TO_STOP:
        step = await _stop_call(context, action)
        if step.phase == "confirmed":
            # 停止确认：活动以可靠停止事实收场，重入进入终态分支。
            _conclude_activity(context, action_id)
            return await _record_handler(action_id, context)
        return
    if decision.phase is RecordingPhase.RECONCILE_REQUIRED:
        # 锚点随既往会话失效：按已保存启动墙钟与当前可信墙钟对账。
        await _reconcile_recording(context, action)
        return
    if decision.phase is RecordingPhase.STOP_EXHAUSTED:
        if canceled:
            # 取消触发的停止预算耗尽归取消收场链，不在本分支收场。
            return
        _settle_stop_exhausted(context, action)
        return
    if decision.phase not in (RecordingPhase.CONTROL_COMPLETE,
                              RecordingPhase.VERIFY_FILE_COMPLETE):
        # 等计时；在途停止由尝试收场后重入推进。
        return
    _conclude_activity(context, action_id)
    if canceled:
        # 停止已确认后取消生效：终止后续核验，放弃本次录像内容。
        _finish_canceled_capture(context, action_id)
        return
    await _advance_recording_outcome(context, action, decision)
    # 判定待定（必要处理未结束）时等待媒体链下次推进。


def _load_processing_row(runtime: CaptureRuntime, action_id: int):
    """装载录像处理行的决定、进度与原片归属；无责任行返回 None。"""
    with closing(runtime.owned.connection.execute(
        "SELECT id, check_decision, check_state, media_json, repair_state,"
        " repair_output_file_id, source_device_file_id"
        " FROM recording_processing WHERE action_id = ?",
        (action_id,),
    )) as cursor:
        return cursor.fetchone()


def _local_recording_listing(runtime: CaptureRuntime, action):
    """原控制和读取均可靠结束时，装载单次录像的完整本地输入。"""
    if (action["type"] != int(_ACTION_TYPE.CAMERA_RECORD)
            or action["cancel_requested"] or action["status"] in _ACTION_TERMINAL):
        return None
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT f.identity_key,f.completion_state,f.source_action_id,c.verification_state,"
        " c.committed_bytes,c.source_size,c.target_sha256,h.owner_action_id,h.size_bytes,h.sha256,"
        " h.retention_state,h.cleanup_state,r.id,r.status,a.activity_state"
        " FROM recording_processing p JOIN device_files f ON f.id=p.source_device_file_id"
        " JOIN file_copies c ON c.processing_id=p.id AND c.source_device_file_id=f.id"
        " JOIN intermediate_files h ON h.id=c.target_file_id"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " JOIN device_activities a ON a.action_id=p.action_id WHERE p.action_id=?",
        (int(_RUN_KIND.READ_FILE), action["id"]),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    (identity, completion, owner, verification, committed, source_size, target_sha,
     host_owner, host_size, host_sha, retention, cleanup, run_id, run_status, activity) = row
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
    required = connection.execute(
        "SELECT 1 FROM operation_runs WHERE action_id=? AND kind IN (?,?,?) AND status IN (1,2) LIMIT 1",
        (action["id"], int(_RUN_KIND.START), int(_RUN_KIND.STOP), int(_RUN_KIND.QUERY_ACTIVITY))).fetchone()
    unfinished = connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND (t.status=? OR t.result_json IS NULL) LIMIT 1",
        (action["id"], int(_ATTEMPT_STATUS.RUNNING))).fetchone()
    actual = connection.execute(
        "SELECT status,result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (run_id,)).fetchone()
    if (required is not None or unfinished is not None or actual is None
            or actual[0] != int(_ATTEMPT_STATUS.SUCCEEDED) or actual[1] is None):
        return None
    listing = _saved_result_listing(runtime, action["id"])
    from camctl.capture.files import file_identity_key

    if not any(entry.complete and entry.kind is FileKind.VIDEO and entry.paired_identity is None
               and file_identity_key(action["device_id"], action["driver_id"], entry.identity) == identity
               for entry in listing.entries):
        return None
    return listing


def _save_source_file(
    runtime: CaptureRuntime, processing_id: int, source_file_id: int) -> None:
    """首次把可靠原片归属到处理行；媒体链读取资格以此对齐。"""
    receipt = runtime.capture.save_source_file(
        SourceFileSave(
            processing_id=processing_id,
            source_device_file_id=source_file_id,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _save_not_needed_decision(
    runtime: CaptureRuntime, processing_id: int, target_duration_ms: int) -> None:
    """连续控制完成：固定无需检查决定，控制依据即成功依据。"""
    receipt = runtime.capture.save_check_decision(
        CheckDecisionSave(
            processing_id=processing_id,
            decision=CheckDecisionChoice.NOT_NEEDED,
            basis=CheckBasis(
                reason=CheckReason.CONTINUOUS_CONTROL_COMPLETE,
                target_duration_ms=target_duration_ms),
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _save_excess_decisions(
    runtime: CaptureRuntime, processing_id: int, target_duration_ms: int,
    control: RecoveredControlDecision) -> None:
    """异常多录：无需检查但达到修复门槛，两决定一次固定。"""
    elapsed = control.control_elapsed_ns
    receipt = runtime.capture.save_check_decision(
        CheckDecisionSave(
            processing_id=processing_id,
            decision=CheckDecisionChoice.NOT_NEEDED,
            basis=CheckBasis(
                reason=CheckReason.EXCESS_DURATION_CHECK,
                target_duration_ms=target_duration_ms,
                control_elapsed_ns=elapsed),
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    repair = runtime.capture.save_repair_decision(
        RepairDecisionSave(
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
                actual_duration_s=(
                    None if elapsed is None
                    else Decimal(elapsed) / Decimal(1_000_000_000)),
                control_elapsed_ns=elapsed),
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert repair.kind is DbOutcomeKind.COMPLETED, repair.error


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


def _activity_started_at(runtime: CaptureRuntime, action_id: int) -> int | None:
    """读取启动确认保存的墙钟；跨会话对账的历史计时依据。"""
    with closing(runtime.owned.connection.execute(
        "SELECT started_at FROM device_activities WHERE action_id = ?",
        (action_id,),
    )) as cursor:
        row = cursor.fetchone()
    return None if row is None else row[0]


def _stop_confirmed_at(runtime: CaptureRuntime, action_id: int) -> int | None:
    """读取停止责任最近可靠确认尝试的结果事件墙钟。"""
    with closing(runtime.owned.connection.execute(
        "SELECT e.occurred_at FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id = r.id"
        " JOIN history_events e ON e.id = a.result_event_id"
        " WHERE r.responsibility_key = ? AND a.status = ? AND a.effect_state = ?"
        " ORDER BY a.id DESC LIMIT 1",
        (f"stop/{action_id}", int(_ATTEMPT_STATUS.SUCCEEDED),
         int(_EFFECT_STATE.CONFIRMED)),
    )) as cursor:
        row = cursor.fetchone()
    return None if row is None else int(row[0])


def _recovered_control_facts(
    runtime: CaptureRuntime, action) -> RecoveredControlFacts:
    """恢复停止后控制完成依据判定的事实装载。"""
    action_id = action["id"]
    return RecoveredControlFacts(
        session_anchor=_recording_port(runtime).has_session_anchor(action_id),
        started_at_us=_activity_started_at(runtime, action_id),
        stop_confirmed_at_us=_stop_confirmed_at(runtime, action_id),
        target_duration_ms=_target_duration_ms(action),
        repair_margin_s=runtime.repair_margin_s)


async def _reconcile_recording(
    context: CaptureRuntime, action) -> None:
    """跨会话对账：可信计时证明满足即停止，未满足等待剩余时长。

    计时不可靠（启动确认墙钟缺失）归时钟异常会话的保守收场接线，
    本处不推测已经录够；停止确认后重入处理器进入终态判定。
    """
    phase = decide_recording_reconciliation(ReconciliationFacts(
        started_at_us=_activity_started_at(context, action["id"]),
        target_duration_ms=_target_duration_ms(action),
        trusted_now_us=context.wall_us()))
    if phase is not ReconciliationPhase.TIMING_SATISFIED:
        return
    step = await _stop_call(context, action)
    if step.phase == "confirmed":
        # 对账满足的停止确认：活动以可靠停止事实收场，重入终态判定。
        _conclude_activity(context, action["id"])
        await _record_handler(action["id"], context)


async def _save_winddown_progress(
    runtime: CaptureRuntime, action) -> bool:
    """保存保守停止后的等待阶段；列举不可靠时保持未定等待后续会话。

    登记结果观察（归属与写完事实）、固定计时证据不足的检查决定并
    关联源文件；不启动媒体链、不登记正式产物，动作保持执行中表
    达“已停止，等待正常会话处理”。
    """
    action_id = action["id"]
    runtime.resume_file_observations(action_id)
    row = _load_processing_row(runtime, action_id)
    if row is None:
        return False
    listing = await _listing_round(runtime, action_id)
    if listing.phase not in (ListingPhase.LISTED, ListingPhase.CLOSED):
        return False
    entries = listing.entries
    registered = _register_listing(runtime, action_id, listing)
    # 保守收场只保存待检查进度；核实与处理尚未终局，沿原责任保留等待。
    _finish_listing_result(runtime, listing, retry_wait=True)
    source_file_id = next(
        (file_id for (file, file_id), entry in zip(registered, entries)
         if entry.complete and entry.kind is FileKind.VIDEO), None)
    if source_file_id is None:
        # 尚无写完的完整原片可归属：不能用等待阶段代替文件证据。
        return False
    if row[1] == int(_CHECK_DECISION.UNDETERMINED):
        receipt = runtime.capture.save_check_decision(
            CheckDecisionSave(
                processing_id=int(row[0]),
                decision=CheckDecisionChoice.REQUIRED,
                basis=CheckBasis(
                    reason=CheckReason.INSUFFICIENT_TIMING,
                    target_duration_ms=_target_duration_ms(action)),
                occurred_at=runtime.wall_us()),
            new_operation_key(), runtime.owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    if row[6] is None:
        _save_source_file(runtime, int(row[0]), source_file_id)
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
    context.resume_file_observations(action_id)
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return HandlerOutcome("already_terminal")
    if action["cancel_requested"]:
        start = context.last_attempt(f"start/{action_id}")
        if start is None:
            _finish_canceled_capture(context, action_id, unstarted=True)
        elif not _settle_unstarted_attempt(context, action, start):
            await _advance_canceled_capture(context, action, start)
        return HandlerOutcome("canceled_final" if context.action(action_id)["status"]
                              in _ACTION_TERMINAL else "cancel_pending")
    port = _recording_port(context)
    canceled = bool(action["cancel_requested"])
    decision = decide_recording_next(
        port.recording_state(action_id), RecordingFacts(canceled=canceled))
    if decision.phase is RecordingPhase.NOT_RUNNING:
        return HandlerOutcome("not_running")
    if decision.phase in (RecordingPhase.STOP_IN_FLIGHT,
                          RecordingPhase.STOP_EXHAUSTED,
                          RecordingPhase.WAIT_RECORD):
        # 在途停止与预算耗尽按既有规则等待；本会话锚点属于正常
        # 计时，不满足保守收场前提。
        return HandlerOutcome(decision.phase.value)
    if decision.phase in (RecordingPhase.CONTROL_COMPLETE,
                          RecordingPhase.VERIFY_FILE_COMPLETE):
        # 停止已确认（既往或本流程）：不重复停止，不启动后处理。
        _conclude_activity(context, action_id)
        if canceled:
            _finish_canceled_capture(context, action_id)
            return HandlerOutcome("canceled_final")
        saved = await _save_winddown_progress(context, action)
        return HandlerOutcome(
            "progress_saved" if saved else "progress_deferred")
    if not canceled:
        # 保守等待只用本会话单调钟，不接续既往会话读数。
        anchor_ns = first_seen.setdefault(action_id, context.monotonic_ns())
        plan = decide_conservative_winddown(WinddownFacts(
            first_seen_ns=anchor_ns,
            monotonic_now_ns=port.recording_state(action_id).monotonic_now_ns,
            target_duration_ms=_target_duration_ms(action),
            recovery_wait_cap_s=wait_cap_s))
        if plan.phase is WinddownPhase.WAIT:
            await sleep(plan.remaining_s or 0.0)
    retried = False
    while True:
        decision = decide_recording_next(
            port.recording_state(action_id),
            RecordingFacts(canceled=canceled, timing_waived=True))
        if decision.phase is not RecordingPhase.READY_TO_STOP:
            return HandlerOutcome(decision.phase.value)
        if retried:
            # 上一次尝试失败：按重试间隔等待后再试，预算内有限重试。
            await sleep(float(context.stop_config.retry_interval_s))
        retried = True
        step = await _stop_call(context, action)
        if step.phase == "confirmed":
            # 保守停止确认：活动以可靠停止事实收场，保存等待阶段
            # 或取消终态，交给后续正常会话。
            _conclude_activity(context, action_id)
            if canceled:
                _finish_canceled_capture(context, action_id)
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
    action_id = action["id"]
    row = _load_processing_row(context, action_id)
    if row is None:
        # 处理责任尚未建立；终态等待责任建立后的推进。
        return
    control_complete = decision.phase is RecordingPhase.CONTROL_COMPLETE
    if row[1] == int(_CHECK_DECISION.UNDETERMINED):
        if not control_complete:
            # 停止确认但文件完成未保证：需要检查的依据归跨会话对账
            # 收场固定，不在此猜测计时证据不足。
            return
        target_ms = _target_duration_ms(action)
        control = decide_recovered_control(_recovered_control_facts(context, action))
        if control is None:
            # 计时证据缺失：不折叠为连续控制完成，保持未定等待对账。
            return
        if control.reason is RecoveredControlReason.EXCESS_DURATION:
            _save_excess_decisions(context, int(row[0]), target_ms, control)
        else:
            _save_not_needed_decision(context, int(row[0]), target_ms)
        row = _load_processing_row(context, action_id)
    ticket = None
    cached = (None if context.listing_cache is None
              else context.listing_cache.get(action_id))
    if cached is not None:
        # 读取已保存的列举事实不构成新轮次；等待中的推进不消耗名额。
        entries, registered = cached
        original = _original_ticket(context, f"results/{_activity_id_of(context, action_id)}", "result")
        listing = None if original is None else _held_listing(context, original)
        if listing is not None:
            ticket = listing.ticket
    else:
        listing = _local_recording_listing(context, action)
        if listing is None:
            listing = await _listing_round(context, action_id)
        if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
            # 在途或本轮列举失败：已保存实际结果与重试等待。
            return
        unconfirmed = (listing.phase is ListingPhase.CLOSED
            and row_facts(context.owned.connection, "operation_runs", listing.ticket.run_id)["status"]
                == int(enum_for("operation_runs.status").UNCONFIRMED))
        if listing.phase is ListingPhase.EXHAUSTED or unconfirmed:
            saved = listing if unconfirmed else _saved_result_listing(context, action_id)
            registered = _register_listing(context, action_id, saved)
            if not unconfirmed:
                # 录像仅收场核实责任，不生成延时摄影的集合结论。
                receipt = context.capture.close_unconfirmed_result_run(
                    ResultRunClose(
                        action_id=action_id, occurred_at=context.wall_us()),
                    new_operation_key(), context.owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            _finish_capture(context, action_id, saved.entries, FileKind.VIDEO,
                            registered=registered, failure=_unconfirmed_failure(context, action_id))
            return
        entries = listing.entries
        ticket = None if listing.already_saved else listing.ticket
        registered = _register_listing(context, action_id, listing)
        metadata = _result_file_metadata(context, listing.ticket)
        metadata.update((entry.identity, entry) for entry in listing.entries)
        entries, registered_files = _registered_result_files(context, action_id, metadata)
        registered = tuple(registered_files[entry.identity] for entry in entries)
    files = assess_capture_files(
        CaptureFileSet(files=tuple(file for file, _ in registered), set_finalized=False),
        ProductRequirements(required_kinds=frozenset({FileKind.VIDEO})),
    )
    source_file_id = next(
        (file_id for (file, file_id), entry in zip(registered, entries)
         if entry.complete and entry.kind is FileKind.VIDEO), None)
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
                _save_source_file(context, int(row[0]), source)
            await run_recording_media(
                context.media, action_id, int(row[0]), source)
            row = _load_processing_row(context, action_id)
    media_json = parse_exact_json(row[3]) if row[3] else None
    facts = RecordingOutcomeFacts(
        processing_id=int(row[0]),
        control_complete=control_complete,
        check_decision=int(row[1]),
        check_state=int(row[2]),
        check_duration_s=(saved_check_duration(media_json)
                          if row[2] == int(_CHECK_STATE.COMPLETED) else None),
        check_issues=bool(media_json.get("issues")) if media_json else False,
        input_unavailable=(
            _media_responsibility_open(row) and row[6] is None
            and source_file_id is None),
        repair_state=int(row[4]),
        target_duration_ms=_target_duration_ms(action),
    )
    result = decide_recording_result(facts)
    if result.kind.value == "failed" or (
            result.kind.value == "succeeded" and files.is_complete):
        if ticket is not None:
            # 承载结论的轮次以可靠结果收场核实责任。
            _finish_listing_result(context, listing,
                           end_run=RunOutcome.SUCCEEDED)
        if result.kind.value == "succeeded":
            _finish_capture(
                context, action_id, entries, FileKind.VIDEO,
                registered=registered,
                repair_file_id=int(row[5]) if row[4] == int(
                    _REPAIR_STATE.SUCCEEDED) and row[5] is not None else None)
        else:
            _finish_capture(context, action_id, entries, FileKind.VIDEO,
                            registered=registered, failure=result.failure)
    elif ticket is not None:
        # 终局依据尚不齐备：本轮成功结果与重试等待共同保存。
        _finish_listing_result(context, listing, retry_wait=True)


async def _timelapse_handler(action_id: int, context: CaptureRuntime) -> None:
    context.resume_file_observations(action_id)
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        context.timelapse_deadlines.pop(action_id, None)
        return
    if _handle_binding_failure(context, action):
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            _finish_canceled_capture(context, action_id, unstarted=True)
            context.timelapse_deadlines.pop(action_id, None)
            return
        send_anchor = None

        def sent_facts(confirmed):
            nonlocal send_anchor
            if not confirmed:
                return {}
            send_anchor = context.monotonic_ns()
            return {"sent_at": context.wall_us()}

        step = await _control_call(
            context, action, "start_timelapse", "timelapse_sent",
            activity_facts=sent_facts)
        if step.phase not in ("confirmed", "sent", "call_failed"):
            return
        attempt = context.last_attempt(f"start/{action_id}")
        if attempt is None:
            return
        if send_anchor is not None:
            config = context.wait_config(action)
            context.timelapse_deadlines[action_id] = send_anchor + (
                config.target_duration_ms + config.driver_margin_ms
                + config.extra_wait_ms) * 1_000_000
    if _settle_unstarted_attempt(context, action, attempt):
        context.timelapse_deadlines.pop(action_id, None)
        return
    if action["cancel_requested"]:
        await _advance_canceled_capture(context, action, attempt)
        if context.action(action_id)["status"] in _ACTION_TERMINAL:
            context.timelapse_deadlines.pop(action_id, None)
        return
    if _settle_start_without_sent_at(context, action, attempt):
        # 可能派发但没有可靠发送时间：无法计算等待锚点，不重复启
        # 动、不补造时间，按无法核实收场。
        context.timelapse_deadlines.pop(action_id, None)
        return
    with closing(context.owned.connection.execute(
        "SELECT sent_at, expected_check_at, result_wait_margin_ms, extra_wait_ms_used,"
        " wait_completed_event_id, result_set_state"
        " FROM device_activities WHERE action_id = ?",
        (action_id,),
    )) as cursor:
        activity = cursor.fetchone()
    if activity is None or activity[0] is None:
        # 发送事实（sent_at）由活动观察边界保存；尚未保存时等待。
        return
    if activity[4] is None:
        config = context.wait_config(action)
        check_at = activity[0] + (
            config.target_duration_ms + config.driver_margin_ms
            + config.extra_wait_ms) * 1000
        if activity[1] is not None:
            if (activity[2] != config.driver_margin_ms
                    or activity[3] is None
                    or activity[1] != activity[0] + (
                        config.target_duration_ms + activity[2] + activity[3]) * 1000):
                raise ConsistencyError("原延时等待与固定执行定义或发送锚点不符")
        if action_id not in context.timelapse_deadlines:
            plan = plan_capture_wait(
                TimelapseState(
                    clock_trusted=True, start_return=StartReturn.SENT,
                    end_control=EndControl.DEVICE, sent_at_utc=activity[0],
                    anchor_monotonic_ns=None, restart=True),
                config, ClockReading(
                    utc_us=context.wall_us(), monotonic_ns=context.monotonic_ns()))
            context.timelapse_deadlines[action_id] = (
                plan.monotonic_deadline_ns if plan.monotonic_deadline_ns is not None
                else context.monotonic_ns())
        plan = WaitPlan(
            kind=WaitKind.WAIT_THEN_CHECK, check_at_utc=check_at,
            monotonic_deadline_ns=context.timelapse_deadlines[action_id])
        if activity[1] is None or activity[1] != check_at or activity[3] != config.extra_wait_ms:
            save = (context.timelapse.schedule_wait if activity[1] is None
                    else context.timelapse.reconfigure_wait)
            receipt = save(
                ScheduleWait(
                    action_id=action_id, plan=plan,
                    driver_margin_ms=config.driver_margin_ms,
                    extra_wait_ms=config.extra_wait_ms, occurred_at=context.wall_us()),
                new_operation_key(), context.owned)
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(
                    f"延时等待安排事务未完成（{receipt.kind.value}）: {receipt.error}")
        if context.monotonic_ns() < context.timelapse_deadlines[action_id]:
            return
    wait_event_id = _complete_timelapse_wait(context, action_id)
    context.timelapse_deadlines.pop(action_id, None)
    concluded = activity[5] in (3, 4)
    if concluded:
        # 中断后已有可靠结论：用原结果完成收尾，不重开核实责任。
        await _finish_timelapse_conclusion(context, action_id)
        return
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT,
                         ListingPhase.CLOSED):
        # 在途、本轮列举失败或责任已闭合：等待收尾轮次，不提交新意图。
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        # 有限轮次用尽：核实责任与无法确认结论同事务收场。
        _close_check_unconfirmed(context, action_id)
        _finish_capture(context, action_id, (), FileKind.VIDEO,
                        failure=_unconfirmed_failure(context, action_id))
        return
    # v1 文件观察分别证明归属与单文件事实，不提供集合确定依据。
    # 真实未确定分区保存原本轮与已取得文件，下一检查仍沿原有限责任。
    _register_listing(context, action_id, listing)
    _finish_listing_result(context, listing, retry_wait=True)


async def _advance_canceled_capture(context: CaptureRuntime, action, start) -> None:
    """取消优先推进原停止责任，不依赖启动确认或计时锚点。

    原启动及停止调用在途时等待实际结果；可能启动的活动沿原 STOP
    的身份、预算和间隔收场。结束未知不释放占用，也不补造启动时间。
    """
    action_id = action["id"]
    if start is not None and start[0] == int(_ATTEMPT_STATUS.RUNNING):
        return
    with closing(context.owned.connection.execute(
        "SELECT activity_state, stop_supported FROM device_activities WHERE action_id = ?",
        (action_id,),
    )) as cursor:
        activity = cursor.fetchone()
    if activity is None:
        raise ConsistencyError("取消拍摄缺少设备活动")
    stop = context.last_attempt(f"stop/{action_id}")
    confirmed = (stop is not None and stop[0] != int(_ATTEMPT_STATUS.RUNNING)
                 and stop[1] == int(_EFFECT_STATE.CONFIRMED))
    ended = activity[0] == 3
    if not ended and not confirmed:
        if stop is not None and stop[0] == int(_ATTEMPT_STATUS.RUNNING):
            return
        with closing(context.owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key = ?",
            (f"stop/{action_id}",),
        )) as cursor:
            run = cursor.fetchone()
        if run is None or run[0] in (1, 2):
            if not activity[1]:
                raise ConsistencyError("可能启动的取消拍摄缺少原停止能力")
            step = await _stop_call(context, action,
                                    "stop_timelapse" if action["type"] == 3 else "stop_recording")
            if step.phase == "confirmed":
                confirmed = True
            elif step.phase == "stop_not_granted" and step.detail == "budget_exhausted":
                _close_stop_exhausted(context, action)
            elif step.phase != "call_failed":
                return
    if confirmed:
        _conclude_activity(context, action_id)
    if action["type"] == 3:
        await _close_canceled_timelapse(context, action_id)
    else:
        _finish_canceled_capture(context, action_id)


async def _close_canceled_timelapse(
    context: CaptureRuntime, action_id: int) -> None:
    """取消延时收尾：已拍完文件与取消终态同事务登记为正式产物。

    收尾列举按 results 责任的有限轮次推进：本轮失败保存实际结果
    与重试等待并保留取消待收场事实；预算耗尽时集合结论按无法确
    认收场，取消终态优先，不登记产物。
    """
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        # 在途或本轮列举失败：取消待收场事实保持，下一轮重新核实。
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        _close_check_unconfirmed(context, action_id)
        _finish_canceled_capture(context, action_id)
        return
    entries = listing.entries
    _finish_listing_result(context, listing, end_run=RunOutcome.SUCCEEDED)
    registered = _register_listing(context, action_id, listing)
    drafts = _catalog_drafts(registered, entries)
    receipt = context.capture.finish_canceled_capture(
        FinishCanceledCapture(
            action_id=action_id,
            occurred_at=context.wall_us(),
            drafts=drafts,
            catalog_facts=OutputCatalogFacts(
                action_id=action_id, ownership_confirmed=True)),
        new_operation_key(), context.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _begin_check_round(runtime: CaptureRuntime, action_id: int):
    """为一轮结果核实提交意图并返回授予结果；预算沿原流程累计。"""
    outcome = runtime.operations.begin_attempt(
        AttemptIntent(
            operation="result", action_id=action_id,
            kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(
                activity_id=_activity_id_of(runtime, action_id)),
            query_purpose=None, config=runtime.check_config,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"核实意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    return outcome.value


def _close_check_unconfirmed(runtime: CaptureRuntime, action_id: int) -> None:
    """预算耗尽：核实流程与无法确认的集合结论同事务收场。"""
    receipt = runtime.capture.close_result_check_unconfirmed(
        ResultSetSave(
            action_id=action_id,
            occurred_at=runtime.wall_us(),
            phase=ResultSetPhase.UNCONFIRMED,
            contract=_RESULT_CONTRACT,
            observation={"reason": "attempts_exhausted"},
            capture={"status": "unconfirmed",
                     "error": {"code": "result_unconfirmed"}},
            error={"code": "result_unconfirmed"},
        ), new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


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
    entries: tuple[ObservedFile, ...] = ()
    ticket: Any = None
    outcome: CallOutcome | None = None
    occurred_at: int | None = None
    returned_ns: int | None = None
    already_saved: bool = False
    registered_files: Mapping[str, tuple[CaptureFile, int]] | None = None


def _registered_result_files(runtime: CaptureRuntime, action_id: int,
                             metadata: Mapping[str, ObservedFile]):
    """原文件绑定、归属、配对与完成事实仍是本地文件输入的权威。"""
    with closing(runtime.owned.connection.execute(
        "SELECT id FROM device_files WHERE source_action_id=? ORDER BY id", (action_id,))) as cursor:
        identifiers = tuple(row[0] for row in cursor.fetchall())
    entries, registered = [], {}
    for file_id in identifiers:
        facts = row_facts(runtime.owned.connection, "device_files", file_id)
        observer = runtime.action(facts["observer_action_id"])
        identity = parse_exact_json(facts["identity_key"])
        binding = _binding(observer)
        if (not isinstance(identity, list) or len(identity) != 3
                or identity[:2] != [binding.device_id, binding.driver_id]
                or not isinstance(identity[2], str) or not identity[2]):
            raise ConsistencyError("已登记结果文件的原身份与观察者绑定不符")
        ownership = facts["ownership_evidence_json"]
        if not isinstance(ownership, Mapping) or not isinstance(ownership.get("observation"), Mapping):
            raise ConsistencyError("已登记结果文件缺少可靠归属依据")
        original_input = metadata.get(identity[2])
        if original_input is None:
            raise ConsistencyError("已登记结果文件缺少原已保存 RESULTS 元数据")
        if not json_equal(original_input.locator, facts["locator_json"]):
            raise ConsistencyError("原 RESULTS 定位与已登记文件事实矛盾")
        complete = facts["completion_state"] == 3
        if complete and not isinstance(facts["completion_evidence_json"], Mapping):
            raise ConsistencyError("已登记完整结果文件缺少完成依据")
        paired = facts["original_device_file_id"]
        if paired is not None:
            original = row_facts(runtime.owned.connection, "device_files", paired)
            if (original is None or original["source_action_id"] != action_id
                    or original["role"] != _ORIGINAL_ROLE or facts["role"] != _PREVIEW_ROLE):
                raise ConsistencyError("已登记预览文件缺少同源原片配对")
            original_identity = parse_exact_json(original["identity_key"])
            if original_input.paired_identity != original_identity[2]:
                raise ConsistencyError("原 RESULTS 配对与已登记文件事实矛盾")
        elif original_input.paired_identity is not None:
            raise ConsistencyError("原 RESULTS 预览输入缺少已登记配对事实")
        # 原 v1 承载类别、名称、媒体类型及配对元数据；文件行只覆盖明确保存的形成状态。
        entry = replace(original_input, complete=complete,
                        size_bytes=facts["size_bytes"] if complete else None)
        if entry.identity in registered:
            raise ConsistencyError("同一结果来源有重复驱动文件身份")
        entries.append(entry)
        registered[entry.identity] = (CaptureFile(entry.identity, entry.kind, entry.complete,
                                                 ownership_confirmed=True), file_id)
    validate_observed_pairings(tuple(entries))
    return tuple(entries), registered


def _register_listing(runtime: CaptureRuntime, action_id: int, listing: ListingRound):
    """已登记事实直接消费；只有尚未登记的原观察产生文件保存责任。"""
    return _register_observed(runtime, action_id, listing.entries, occurred_at=listing.occurred_at,
                              registered_files=listing.registered_files)


def _unconfirmed_failure(runtime: CaptureRuntime, action_id: int) -> RecordingFailure:
    """有限核实耗尽后的动作失败：产物结果无法确认。"""
    return RecordingFailure(
        code="capture_result_unconfirmed",
        details={"activity_id": str(_activity_id_of(runtime, action_id)), "reason": "outputs_unknown"})


def _held_listing(runtime: CaptureRuntime, ticket: AttemptTicket) -> ListingRound | None:
    pending = runtime.pending_start_results.get((ticket.run_id, ticket.attempt_id))
    if pending is None or pending.result_listing is None:
        return None
    if pending.finish.ticket != ticket:
        raise ConsistencyError("待存 RESULTS 原票据不符")
    return ListingRound(ListingPhase.LISTED, pending.result_listing, ticket,
                        pending.finish.outcome.outcome, pending.finish.occurred_at,
                        pending.returned_ns, registered_files=pending.result_registered_files)


def _result_file_metadata(runtime: CaptureRuntime, ticket: AttemptTicket) -> dict[str, ObservedFile]:
    """沿原 RESULTS 流程装载已结束尝试提供的文件元数据。"""
    metadata: dict[str, ObservedFile] = {}
    with closing(runtime.owned.connection.execute(
        "SELECT attempt_no,status,effect_state,result_json,error_json FROM operation_attempts"
        " WHERE run_id=? AND result_event_id IS NOT NULL ORDER BY attempt_no", (ticket.run_id,))) as cursor:
        for attempt_no, status, effect, result_json, error_json in cursor:
            previous_outcome = saved_outcome(status, effect, parse_exact_json(result_json),
                None if error_json is None else parse_exact_json(error_json))
            if any(value.type == "result_files_listed" for value in previous_outcome.observations):
                original_ticket = AttemptTicket(attempt_no, "result", ticket.target_id,
                                                 ticket.responsibility_key, ticket.run_id)
                for entry in files_from_outcome(original_ticket, previous_outcome):
                    metadata[entry.identity] = entry
    return metadata


def _saved_result_listing(runtime: CaptureRuntime, action_id: int) -> ListingRound:
    """CLOSED 消费原已保存输入；不取得当前驱动端口或新的时钟。"""
    activity_id = _activity_id_of(runtime, action_id)
    responsibility = f"results/{activity_id}"
    with closing(runtime.owned.connection.execute(
        "SELECT r.id,r.action_id,r.activity_id,r.kind,t.attempt_no,t.status,t.effect_state,"
        " t.result_json,t.error_json,e.occurred_at FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN history_events e ON e.id=t.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY t.attempt_no DESC LIMIT 1",
        (responsibility,))) as cursor:
        row = cursor.fetchone()
    if (row is None or row[1] != action_id or row[2] != activity_id
            or row[3] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS)):
        raise ConsistencyError("RESULTS 责任缺少可核对的完整本地输入")
    ticket = AttemptTicket(row[4], "result", str(activity_id), responsibility, row[0])
    actual = saved_outcome(row[5], row[6], parse_exact_json(row[7]),
                           None if row[8] is None else parse_exact_json(row[8]))
    metadata = _result_file_metadata(runtime, ticket)
    previous, registered = _registered_result_files(runtime, action_id, metadata)
    previous_by_identity = {entry.identity: entry for entry in previous}
    has_observation = any(value.type == "result_files_listed" for value in actual.observations)
    if not has_observation and not previous and actual.error is None:
        raise ConsistencyError("已保存 RESULTS 缺少完整本地文件输入")
    latest = files_from_outcome(ticket, actual) if has_observation else ()
    entries = []
    seen = set()
    for entry in latest:
        saved = previous_by_identity.get(entry.identity)
        if saved is not None and not json_equal(saved.locator, entry.locator):
            raise ConsistencyError("原 RESULTS 定位与已登记文件事实矛盾")
        entries.append(entry if saved is None else saved)
        seen.add(entry.identity)
    entries.extend(entry for entry in previous if entry.identity not in seen)
    validate_observed_pairings(tuple(entries))
    return ListingRound(ListingPhase.CLOSED, tuple(entries), ticket, actual, row[9], None, True,
                        registered_files=registered)


def _finish_listing_result(runtime: CaptureRuntime, listing: ListingRound, *,
                           retry_wait: bool = False, end_run: RunOutcome | None = None,
                           result_set: ResultSetSave | None = None) -> None:
    """首次固定真实处置后沿原 key 保存；已保存的 CLOSED 输入不再改写。"""
    if listing.already_saved:
        return
    ticket = listing.ticket
    identity = (ticket.run_id, ticket.attempt_id)
    pending = runtime.pending_start_results.get(identity)
    if pending is None or pending.finish.ticket != ticket or pending.finish.outcome.outcome is not listing.outcome:
        raise ConsistencyError("RESULTS 原返回及其保存责任不可替换")
    finish = replace(pending.finish, retry_wait=retry_wait,
                     run_finish=None if end_run is None else RunFinish(end_run))
    if pending.result_disposition_ready and (pending.finish != finish or pending.result_set != result_set):
        raise ConsistencyError("RESULTS 原 key 的已确定处置不可改变")
    pending = replace(pending, finish=finish, result_set=result_set, result_disposition_ready=True)
    runtime.pending_start_results[identity] = pending
    runtime.save_held_result(ticket)


async def _listing_round(runtime: CaptureRuntime, action_id: int) -> ListingRound:
    """同一活动的有限核实；实际返回先持有，消费者随后确定真实处置。"""
    runtime.resume_file_observations(action_id)
    activity_id = _activity_id_of(runtime, action_id)
    responsibility = f"results/{activity_id}"
    in_flight = runtime.last_attempt(responsibility)
    if in_flight is not None and in_flight[0] == int(_ATTEMPT_STATUS.RUNNING):
        ticket = _original_ticket(runtime, responsibility, "result")
        held = _held_listing(runtime, ticket)
        if held is not None:
            return held
        return ListingRound(ListingPhase.IN_FLIGHT)
    if runtime.retry_wait_remaining(responsibility, runtime.check_config.retry_interval_s,
                                    maximum=runtime.check_config.max_attempts) is not None:
        return ListingRound(ListingPhase.RETRY_WAIT)
    begin = _begin_check_round(runtime, action_id)
    if begin.disposition is not BeginDisposition.GRANTED:
        if begin.reason == "budget_exhausted":
            return ListingRound(ListingPhase.EXHAUSTED)
        return _saved_result_listing(runtime, action_id)
    ticket = begin.ticket
    result = await runtime.results.list_round(ticket, timeout_s=runtime.check_config.timeout_s)
    observed_at, returned_ns = runtime.wall_us(), runtime.monotonic_ns()
    runtime.hold_call_result(ticket, result.outcome, occurred_at=observed_at,
                             returned_ns=returned_ns, result_listing=result.entries)
    listing = _held_listing(runtime, ticket)
    has_file_observation = any(value.type == "result_files_listed" for value in result.outcome.observations)
    if not has_file_observation and result.outcome.error is not None:
        _finish_listing_result(runtime, listing, retry_wait=True)
        return ListingRound(ListingPhase.RETRY_WAIT, outcome=result.outcome,
                            occurred_at=observed_at, returned_ns=returned_ns)
    return listing


async def _finish_timelapse_conclusion(
    runtime: CaptureRuntime, action_id: int) -> None:
    """集合已有结论后的收尾：按保存的判定登记产物与终态。

    核实责任终态保持，不重开轮次；产物登记只消费原保存输入，
    输入缺失或不可解释时保留诊断，不访问设备补写。
    """
    with closing(runtime.owned.connection.execute(
        "SELECT result_set_state, completion_basis FROM device_activities"
        " WHERE action_id = ?", (action_id,),
    )) as cursor:
        state, basis = cursor.fetchone()
    listing = _saved_result_listing(runtime, action_id)
    entries = listing.entries
    registered = _register_listing(runtime, action_id, listing)
    if state == 3 and basis == 3:
        _release_occupancy(runtime, action_id)
        _finish_capture(runtime, action_id, entries, FileKind.VIDEO, registered=registered)
    elif state == 3:
        _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO, registered=registered,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action_id), "reason": "no_outputs"}))
    else:
        _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO, registered=registered,
            failure=_unconfirmed_failure(runtime, action_id))


def _complete_timelapse_wait(runtime: CaptureRuntime, action_id: int) -> int:
    """到期的等待先保存一次完成事实，返回其事件引用。"""
    with closing(runtime.owned.connection.execute(
        "SELECT wait_completed_event_id FROM device_activities WHERE action_id = ?",
        (action_id,),
    )) as cursor:
        saved = cursor.fetchone()
    if saved is not None and saved[0] is not None:
        return saved[0]
    receipt = runtime.timelapse.complete_wait(
        WaitCompletedSave(
            action_id=action_id, occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return receipt.value.event_id


def _confirm_timelapse_results(
    runtime: CaptureRuntime, action_id: int, assessment, entries,
    wait_event_id: int, listing: ListingRound,
) -> None:
    """把本轮结果集合核实结论与尝试结束、流程收场共同保存。

    完整集合满足时按时间与产物完成判定，依据引用已保存的等待完
    成事实；明确不满足保存已知失败。观察只记录实际列举到的文件
    事实，不填理论张数。结论与承载它的列举轮次原子提交。
    """
    identities = sorted(entry.identity for entry in entries)
    if assessment.is_complete:
        command = ResultSetSave(
            action_id=action_id,
            occurred_at=listing.occurred_at,
            phase=ResultSetPhase.COMPLETE,
            contract=_RESULT_CONTRACT,
            observation={"files": identities},
            capture={"status": "completed"},
            evidence={
                "method": _TIME_AND_OUTPUTS_METHOD,
                "wait_completed_event_id": wait_event_id,
                "observation": {"files": identities},
            })
    else:
        missing = sorted(str(kind.value) for kind in assessment.missing_kinds)
        command = ResultSetSave(
            action_id=action_id,
            occurred_at=listing.occurred_at,
            phase=ResultSetPhase.UNSATISFIED,
            contract=_RESULT_CONTRACT,
            observation={"files": identities, "missing": missing},
            capture={"status": "failed",
                     "error": {"code": "capture_unsatisfied"}},
            evidence={
                "method": _KNOWN_FAILURE_METHOD,
                "observation": {"files": identities, "missing": missing},
            })
    _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED, result_set=command)


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
