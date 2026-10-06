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
from dataclasses import dataclass, field
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
)
from camctl.capture.media import (
    RecordingFailure,
    RecordingOutcomeFacts,
    decide_recording_result,
)
from camctl.capture.media_flow import MediaFlow, run_recording_media
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
    ReconciliationFacts,
    ReconciliationPhase,
    RecordingFacts,
    RecordingPhase,
    RecordingState,
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
    plan_capture_wait,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import ControlRequest, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    RetryWaitGate,
    RunFinish,
    RunOutcome,
)
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCanceledCapture,
    FinishCapture,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import GrantRequest, SchedulingRepository
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

#: 结果核实轮次收场依据的证据类型（驱动登记 operation="result"）。
_RESULTS_RETURNED = "results_returned"

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")
_DISPATCH_STATE = enum_for("device_activities.dispatch_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_FILE_PRESENCE = enum_for("device_files.presence_state")


@dataclass(frozen=True)
class ObservedFile:
    """结果列举取得的一份候选产物文件。

    evidence 是驱动结构化依据，原样进入归属与完成证据；kind 由列
    举方按驱动声明给出，未知时为 OTHER。
    """

    identity: str
    locator: Mapping[str, Any]
    evidence: Mapping[str, Any]
    complete: bool
    size_bytes: int | None
    kind: FileKind = FileKind.OTHER
    original_name: str | None = None
    media_type: str | None = None


@dataclass(frozen=True)
class HandlerOutcome:
    """一次处理器推进的结果分类。"""

    phase: str
    detail: str | None = None


class DeviceControlPort(Protocol):
    """设备控制调用端口；契约替身与真实驱动同形。"""

    async def control(self, request: ControlRequest) -> DeviceCallResult: ...


class DeviceStopPort(Protocol):
    """设备停止操作端口；录像停止调用经此发出。"""

    async def stop(self, request: ControlRequest) -> DeviceCallResult: ...


class ResultFilesPort(Protocol):
    """结果列举端口：返回本任务观察到的候选产物文件。"""

    async def list_files(self, action_id: int) -> tuple[ObservedFile, ...]: ...


class RecordingStatePort(Protocol):
    """录像中段事实端口；活动事实生产者接入前由装配层提供。"""

    def recording_state(self, action_id: int) -> RecordingState: ...


@dataclass
class CaptureRuntime:
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
    recording_state: RecordingStatePort | None = None
    #: 录像停止调用端口；未装配时录像不能停止。
    stopper: DeviceStopPort | None = None
    #: 录像媒体链端口；未装配时需要检查的录像不推进，等待装配会话。
    media: MediaFlow | None = None
    #: 异常多录修复门槛的本次余量秒数（configuration.md#配置归属）。
    repair_margin_s: Decimal = Decimal("10")
    #: 停止尝试的本次预算；默认 3 次、单次 10 秒、重试间隔 3 秒
    #:（configuration.md#通信重试间隔）。
    stop_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 结果核实轮次的本次预算；默认 3 轮、单轮 10 秒、重试间隔 3 秒
    #:（configuration.md#状态查询与产物核实的配置）。
    check_config: AttemptConfig = field(default_factory=lambda: AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3")))
    #: 重试间隔的会话内时间门槛；装配层闭包共享，跨推进轮次保留。
    retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)
    #: 会话内已观察的结果列举缓存（动作到列举与登记事实）；装配层
    #: 闭包共享，跨推进轮次保留，避免等待中的重复列举消耗核实名额。
    listing_cache: dict[int, tuple[tuple, tuple]] | None = None

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
            config=AttemptConfig(max_attempts=1, timeout_s=Decimal("30")),
            occurred_at=self.wall_us(),
        )
        outcome = self.scheduling.grant_start(
            request, new_operation_key(), self.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            return None, f"grant_{outcome.kind.value}"
        result = outcome.value
        if result.outcome.value != "granted":
            return None, result.reason
        return result.ticket, None

    def finish(self, ticket, outcome: CallOutcome, *,
               end_run: RunOutcome | None = None,
               run_error: ErrorValue | None = None,
               retry_wait: bool = False) -> None:
        """保存尝试结果；调用收场后同时结束流程（启动责任闭合）。

        保存重试等待时以当前单调读数登记间隔锚点；流程结束清除。
        """
        attempt = AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, self.evidence),
            occurred_at=self.wall_us(),
            run_finish=None if end_run is None else RunFinish(
                status=end_run, error=run_error),
            retry_wait=retry_wait,
        )
        receipt = self.operations.finish_attempt(
            attempt, new_operation_key(), self.owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        if retry_wait:
            self.retry_gate.established(
                ticket.responsibility_key, self.monotonic_ns())
        elif end_run is not None:
            self.retry_gate.cleared(ticket.responsibility_key)

    def retry_wait_remaining(self, responsibility: str,
                             interval_s: Decimal | None) -> Decimal | None:
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
            max_attempts_used=int(row[2]),
            interval_s=interval_s,
            now_ns=self.monotonic_ns())


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
                   and start[0] == int(_ATTEMPT_STATUS.SUCCEEDED)
                   and start[1] == int(_EFFECT_STATE.CONFIRMED))
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
    未确认的发送保持流程执行中，等待后续效果核实。
    """
    ticket, reason = runtime.grant(action)
    if ticket is None:
        return HandlerOutcome("not_granted", reason)
    result = await runtime.driver.control(ControlRequest(
        operation=operation,
        binding=_binding(action),
        params=action["effective_params_json"],
    ))
    outcome, confirmed = _operation_outcome(result, confirmed_observation)
    if result.error is not None:
        runtime.finish(
            ticket, outcome, end_run=RunOutcome.FAILED,
            run_error=outcome.error)
    elif confirmed:
        runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
    else:
        runtime.finish(ticket, outcome)
    if activity_facts is not None and result.error is None:
        facts = (activity_facts(confirmed) if callable(activity_facts)
                 else activity_facts)
        # 调用已可靠返回：派发状态推进到成功返回。
        facts = {"dispatch_state": int(_DISPATCH_STATE.SUCCESS_RETURNED),
                 **facts}
        _save_activity(runtime, action["id"], **facts)
    if result.error is not None:
        return HandlerOutcome("call_failed", "device_error")
    return HandlerOutcome("confirmed" if confirmed else "sent")


def _register_observed(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
) -> tuple[tuple[CaptureFile, int], ...]:
    """把结果列举观察落库：发现、任务归属与完成事实一次登记。"""
    registered: list[tuple[CaptureFile, int]] = []
    for entry in entries:
        occurred = runtime.wall_us()
        observed = runtime.capture.save_file_observation(
            FileObservationSave(
                observer_action_id=action_id,
                file_identity=entry.identity,
                locator=entry.locator,
                occurred_at=occurred,
                original_name=entry.original_name,
                media_type=entry.media_type,
            ), new_operation_key(), runtime.owned)
        assert observed.kind is DbOutcomeKind.COMPLETED, observed.error
        file_id = observed.value.file_id
        if observed.value.created:
            # 同一次列举确认文件在场；重复发现沿用已保存的存在事实。
            present = runtime.capture.save_file_presence(
                FilePresenceSave(
                    file_id=file_id,
                    state=int(_FILE_PRESENCE.PRESENT),
                    occurred_at=occurred),
                new_operation_key(), runtime.owned)
            assert present.kind is DbOutcomeKind.COMPLETED, present.error
        owned = runtime.capture.save_file_ownership(
            OwnershipSave(
                file_id=file_id,
                source_action_id=action_id,
                method=_TASK_SCOPE,
                role=2,
                observation=entry.evidence,
                occurred_at=occurred,
            ), new_operation_key(), runtime.owned)
        assert owned.kind is DbOutcomeKind.COMPLETED, owned.error
        if entry.complete:
            finished = runtime.capture.save_file_completion(
                FileCompletionSave(
                    file_id=file_id,
                    state=3,
                    occurred_at=occurred,
                    basis=_DEVICE_GUARANTEE,
                    observation=entry.evidence,
                    size_bytes=entry.size_bytes,
                ), new_operation_key(), runtime.owned)
            assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        registered.append((
            CaptureFile(
                file_id=entry.identity, kind=entry.kind, complete=entry.complete,
                ownership_confirmed=True),
            file_id,
        ))
    return tuple(registered)


def _conclude_activity(runtime: CaptureRuntime, action_id: int) -> None:
    """成功链收场活动：结束观察与占用释放同事务，幂等可重入。"""
    receipt = runtime.capture.conclude_activity(
        ActivityConcludeSave(
            action_id=action_id, occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _recording_port(context: CaptureRuntime) -> RecordingStatePort:
    """会话内缓存录像中段事实装载器；计时锚点跨推进保留。"""
    if context.recording_state is None:
        context.recording_state = SessionRecordingState(context)
    return context.recording_state


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
        target=AttemptTarget(activity_id=action["id"]),
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
    ))
    call, confirmed = _operation_outcome(
        response, "stop_confirmed", evidence_type="stop_returned")
    if confirmed:
        runtime.finish(ticket, call, end_run=RunOutcome.SUCCEEDED)
    else:
        runtime.finish(ticket, call, retry_wait=True)
    if response.error is not None:
        return HandlerOutcome("stop_failed", "device_error")
    return HandlerOutcome("confirmed" if confirmed else "sent")


def _finish_canceled_capture(runtime: CaptureRuntime, action_id: int) -> None:
    """取消终态：放弃内容，不登记正式产物。"""
    receipt = runtime.capture.finish_canceled_capture(
        FinishCanceledCapture(
            action_id=action_id, occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


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
        CaptureFileSet(files=tuple(file for file, _ in registered), set_finalized=True),
        ProductRequirements(required_kinds=frozenset({required})),
    )
    drafts = tuple(
        OutputDraft(
            kind=OutputKind.ORIGINAL,
            file=FileReference(device_file_id=file_id),
            file_complete=True,
        )
        for (file, file_id), entry in zip(registered, entries)
        if entry.complete
    )
    if repair_file_id is not None:
        original_file_ids = [
            file_id for (file, file_id), entry in zip(registered, entries)
            if entry.complete]
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


async def _photo_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            # 未派发取消不创建尝试；终态由取消收场处理。
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
    listing = await _listing_round(context, action_id)
    if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
        # 在途或本轮列举失败：已保存实际结果与重试等待，下一轮重新核实。
        return
    if listing.phase is ListingPhase.EXHAUSTED:
        # 有限轮次用尽：核实责任与无法确认结论同事务收场。
        _close_check_unconfirmed(context, action_id)
        _finish_capture(context, action_id, (), FileKind.PHOTO,
                        failure=_unconfirmed_failure(action_id))
        return
    if listing.phase is ListingPhase.LISTED:
        entries = listing.entries
        ticket = listing.ticket
    else:
        # 核实责任已闭合：按直接列举回退，不再保存轮次事实。
        entries = await context.results.list_files(action_id)
        ticket = None
    assessment = CaptureAssessment(
        complete=bool(entries) and all(entry.complete for entry in entries))
    decision = decide_photo(
        PhotoState(
            action_terminal=False,
            canceled=bool(action["cancel_requested"]),
            dispatched=True,
            response_completed=attempt[1] == int(_EFFECT_STATE.CONFIRMED),
            response_failed=attempt[0] != int(_ATTEMPT_STATUS.SUCCEEDED),
            effect_unknown=attempt[1] == int(_EFFECT_STATE.UNKNOWN),
            stop_supported=False,
        ),
        assessment,
        PhotoCompletion.COMPLETED_ON_RETURN,
    )
    if decision is PhotoDecision.REGISTER_SUCCESS:
        if ticket is not None:
            context.finish(ticket, _round_outcome(),
                           end_run=RunOutcome.SUCCEEDED)
        _conclude_activity(context, action_id)
        _finish_capture(context, action_id, entries, FileKind.PHOTO)
    elif decision is PhotoDecision.FAILED_KEEP_FILES:
        if ticket is not None:
            context.finish(ticket, _round_outcome(),
                           end_run=RunOutcome.SUCCEEDED)
        _finish_capture(
            context, action_id, entries, FileKind.PHOTO,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action_id), "reason": "device_failed"}))
    elif ticket is not None:
        # 其余分区（等待响应、取消保留、未知无停止）：本轮成功结果与
        # 重试等待共同保存，等待下次推进或取消收场。
        context.finish(ticket, _round_outcome(), retry_wait=True)


async def _record_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    if context.last_attempt(f"start/{action_id}") is None:
        # 尚未发起启动：首次授予并调用，不依赖中段事实端口。
        if action["cancel_requested"]:
            return
        step = await _control_call(
            context, action, "start_recording", "start_confirmed",
            activity_facts=lambda confirmed: (
                {"sent_at": context.wall_us()}
                | ({"started_at": context.wall_us(), "activity_state": 2}
                   if confirmed else {})))
        if step.phase == "confirmed":
            # 启动确认即取本会话单调锚点；停止目标 = 锚点 + 目标时长。
            port = _recording_port(context)
            anchor_ns = context.monotonic_ns()
            port.anchor_confirmed(
                action_id, anchor_ns,
                recording_stop_target(anchor_ns, _target_duration_ms(action)))
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
        await _control_call(context, action, "start_recording", "start_confirmed")
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
    if decision.phase not in (RecordingPhase.CONTROL_COMPLETE,
                              RecordingPhase.VERIFY_FILE_COMPLETE):
        # 等待计时与预算耗尽的收场随后续接线推进。
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
                threshold_s=runtime.repair_margin_s,
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
    row = _load_processing_row(runtime, action_id)
    if row is None:
        return False
    try:
        entries = await runtime.results.list_files(action_id)
    except Exception:
        # 停止依据已可靠保存：等待阶段未取得文件证据前保持未定，
        # 后续会话按已保存事实重新推进。
        return False
    registered = _register_observed(runtime, action_id, entries)
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
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return HandlerOutcome("already_terminal")
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
    else:
        listing = await _listing_round(context, action_id)
        if listing.phase in (ListingPhase.IN_FLIGHT, ListingPhase.RETRY_WAIT):
            # 在途或本轮列举失败：已保存实际结果与重试等待。
            return
        if listing.phase is ListingPhase.EXHAUSTED:
            # 录像活动不适用集合结论：仅核实流程按公共错误结构收场。
            receipt = context.capture.close_unconfirmed_result_run(
                ResultRunClose(
                    action_id=action_id, occurred_at=context.wall_us()),
                new_operation_key(), context.owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            _finish_capture(context, action_id, (), FileKind.VIDEO,
                            failure=_unconfirmed_failure(action_id))
            return
        if listing.phase is ListingPhase.LISTED:
            registered = _register_observed(context, action_id, listing.entries)
            entries = listing.entries
            ticket = listing.ticket
        else:
            # 核实责任已闭合：按直接列举回退，不再保存轮次事实。
            entries = await context.results.list_files(action_id)
            registered = _register_observed(context, action_id, entries)
    source_file_id = next(
        (file_id for (file, file_id), entry in zip(registered, entries)
         if entry.complete and entry.kind is FileKind.VIDEO), None)
    if cached is None and context.listing_cache is not None and (
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
    if result.kind.value in ("succeeded", "failed"):
        if ticket is not None:
            # 承载结论的轮次以可靠结果收场核实责任。
            context.finish(ticket, _round_outcome(),
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
        context.finish(ticket, _round_outcome(), retry_wait=True)


async def _timelapse_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            return
        step = await _control_call(
            context, action, "start_timelapse", "timelapse_sent",
            activity_facts={"sent_at": context.wall_us()})
        if step.phase not in ("confirmed", "sent", "call_failed"):
            return
    with closing(context.owned.connection.execute(
        "SELECT sent_at, expected_check_at FROM device_activities WHERE id = ?",
        (action_id,),
    )) as cursor:
        activity = cursor.fetchone()
    if activity is None or activity[0] is None:
        # 发送事实（sent_at）由活动观察边界保存；尚未保存时等待。
        return
    if action["cancel_requested"]:
        # 可停止延时的取消已生效：不再等待计时，立即按停止预算收场。
        await _cancel_timelapse_stop(context, action)
        return
    config = context.wait_config(action)
    if activity[1] is None:
        plan = plan_capture_wait(
            TimelapseState(
                clock_trusted=True,
                start_return=StartReturn.SENT,
                end_control=EndControl.DEVICE,
                sent_at_utc=activity[0],
                anchor_monotonic_ns=context.monotonic_ns(),
            ),
            config,
            ClockReading(
                utc_us=context.wall_us(), monotonic_ns=context.monotonic_ns()),
        )
        receipt = context.timelapse.schedule_wait(
            ScheduleWait(
                action_id=action_id,
                plan=plan,
                driver_margin_ms=config.driver_margin_ms,
                extra_wait_ms=config.extra_wait_ms,
                occurred_at=context.wall_us(),
            ), new_operation_key(), context.owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return
    if context.wall_us() < activity[1]:
        return
    wait_event_id = _complete_timelapse_wait(context, action_id)
    with closing(context.owned.connection.execute(
        "SELECT result_set_state FROM device_activities WHERE id = ?",
        (action_id,),
    )) as cursor:
        concluded = cursor.fetchone()[0] in (3, 4)
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
                        failure=_unconfirmed_failure(action_id))
        return
    entries = listing.entries
    ticket = listing.ticket
    assessment = assess_capture_files(
        CaptureFileSet(
            files=tuple(
                CaptureFile(
                    file_id=entry.identity, kind=entry.kind,
                    complete=entry.complete, ownership_confirmed=True)
                for entry in entries),
            set_finalized=True),
        ProductRequirements(required_kinds=frozenset({FileKind.VIDEO})))
    if assessment.is_complete:
        _confirm_timelapse_results(
            context, action_id, assessment, entries, wait_event_id, ticket)
        # 时间与产物完成依据成立后解除占用；收尾处理不再阻塞同设备。
        _release_occupancy(context, action_id)
        _finish_capture(context, action_id, entries, FileKind.VIDEO)
    elif assessment.explicitly_unmet:
        _confirm_timelapse_results(
            context, action_id, assessment, entries, wait_event_id, ticket)
        _finish_capture(
            context, action_id, entries, FileKind.VIDEO,
            failure=RecordingFailure(
                code="capture_failed",
                details={
                    "activity_id": str(action_id),
                    "reason": "no_outputs"}))
    else:
        # 暂不齐备：本轮成功结果与重试等待共同保存，下一轮作为新轮次。
        context.finish(ticket, _round_outcome(), retry_wait=True)


async def _cancel_timelapse_stop(context: CaptureRuntime, action) -> None:
    """等待中取消的可停止延时收场：立即停止并保留已拍完文件。

    停止决策与录像共用一套阶段规则：取消不经计时立即按剩余预算
    停止。停止确认后活动以可靠停止事实收场并释放占用，收尾核实
    把已拍完且确认完成的文件登记为正式产物。启动未确认的取消归
    启动核实链；在途停止与预算耗尽等待停止结果或残留收场接线。
    """
    port = _recording_port(context)
    decision = decide_recording_next(
        port.recording_state(action["id"]),
        RecordingFacts(canceled=True),
    )
    if decision.phase is not RecordingPhase.READY_TO_STOP:
        if decision.phase is RecordingPhase.CONTROL_COMPLETE:
            # 停止已确认（中断恢复）：直接进入取消收尾。
            _conclude_activity(context, action["id"])
            await _close_canceled_timelapse(context, action["id"])
        return
    step = await _stop_call(context, action, "stop_timelapse")
    if step.phase == "confirmed":
        _conclude_activity(context, action["id"])
        await _close_canceled_timelapse(context, action["id"])


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
    if listing.phase is ListingPhase.LISTED:
        entries = listing.entries
        context.finish(listing.ticket, _round_outcome(),
                       end_run=RunOutcome.SUCCEEDED)
    else:
        # 核实责任已闭合：按直接列举回退，不再保存轮次事实。
        try:
            entries = await context.results.list_files(action_id)
        except Exception:
            entries = ()
    registered = _register_observed(context, action_id, entries)
    drafts = tuple(
        OutputDraft(
            kind=OutputKind.ORIGINAL,
            file=FileReference(device_file_id=file_id),
            file_complete=True)
        for (file, file_id), entry in zip(registered, entries)
        if entry.complete)
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
            target=AttemptTarget(activity_id=action_id),
            query_purpose=None, config=runtime.check_config,
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"核实意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    return outcome.value


def _round_outcome(error: ErrorValue | None = None) -> CallOutcome:
    """一轮结果列举的尝试结局：可靠返回，效果未知。"""
    return CallOutcome(
        status=(AttemptStatus.FAILED if error is not None
                else AttemptStatus.SUCCEEDED),
        error=error,
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type=_RESULTS_RETURNED, version=1, data={}),
        ),
    )


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


def _unconfirmed_failure(action_id: int) -> RecordingFailure:
    """有限核实耗尽后的动作失败：产物结果无法确认。"""
    return RecordingFailure(
        code="capture_result_unconfirmed",
        details={"activity_id": str(action_id), "reason": "outputs_unknown"})


async def _listing_round(
    runtime: CaptureRuntime, action_id: int) -> ListingRound:
    """按 results 责任的有限轮次推进一次结果列举。

    在途轮次跨会话恢复前停等；上一轮的重试等待按核实间隔到时才
    开始新轮次，未到时不提交新意图、不消耗名额；本轮列举失败保
    存实际结果与重试等待；预算耗尽交由调用方按所属拍摄及收场规
    则结束；其余拒绝说明核实责任已闭合，调用方按直接列举回退。
    """
    in_flight = runtime.last_attempt(f"results/{action_id}")
    if in_flight is not None and in_flight[0] == int(_ATTEMPT_STATUS.RUNNING):
        return ListingRound(ListingPhase.IN_FLIGHT)
    if runtime.retry_wait_remaining(
            f"results/{action_id}",
            runtime.check_config.retry_interval_s) is not None:
        return ListingRound(ListingPhase.RETRY_WAIT)
    begin = _begin_check_round(runtime, action_id)
    if begin.disposition is not BeginDisposition.GRANTED:
        if begin.reason == "budget_exhausted":
            return ListingRound(ListingPhase.EXHAUSTED)
        return ListingRound(ListingPhase.CLOSED)
    ticket = begin.ticket
    try:
        entries = await runtime.results.list_files(action_id)
    except Exception:
        runtime.finish(ticket, _round_outcome(
            ErrorValue(code="device_error", stage="device")), retry_wait=True)
        return ListingRound(ListingPhase.RETRY_WAIT)
    return ListingRound(ListingPhase.LISTED, entries, ticket)


async def _finish_timelapse_conclusion(
    runtime: CaptureRuntime, action_id: int) -> None:
    """集合已有结论后的收尾：按保存的判定登记产物与终态。

    核实责任终态保持，不重开轮次；产物登记经收尾列举进行，列举
    事实与已保存结论不一致时保持等待。
    """
    with closing(runtime.owned.connection.execute(
        "SELECT result_set_state, completion_basis FROM device_activities"
        " WHERE id = ?", (action_id,),
    )) as cursor:
        state, basis = cursor.fetchone()
    entries = await runtime.results.list_files(action_id)
    if state == 3 and basis == 3:
        _release_occupancy(runtime, action_id)
        _finish_capture(runtime, action_id, entries, FileKind.VIDEO)
    elif state == 3:
        _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action_id), "reason": "no_outputs"}))
    else:
        _finish_capture(
            runtime, action_id, entries, FileKind.VIDEO,
            failure=RecordingFailure(
                code="capture_result_unconfirmed",
                details={
                    "activity_id": str(action_id),
                    "reason": "outputs_unknown"}))


def _complete_timelapse_wait(runtime: CaptureRuntime, action_id: int) -> int:
    """到期的等待先保存一次完成事实，返回其事件引用。"""
    with closing(runtime.owned.connection.execute(
        "SELECT wait_completed_event_id FROM device_activities WHERE id = ?",
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
    wait_event_id: int, ticket,
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
            occurred_at=runtime.wall_us(),
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
            occurred_at=runtime.wall_us(),
            phase=ResultSetPhase.UNSATISFIED,
            contract=_RESULT_CONTRACT,
            observation={"files": identities, "missing": missing},
            capture={"status": "failed",
                     "error": {"code": "capture_unsatisfied"}},
            evidence={
                "method": _KNOWN_FAILURE_METHOD,
                "observation": {"files": identities, "missing": missing},
            })
    finish = AttemptFinish(
        ticket=ticket,
        outcome=validate_outcome(ticket, _round_outcome(), runtime.evidence),
        occurred_at=runtime.wall_us(),
        run_finish=RunFinish(status=RunOutcome.SUCCEEDED),
    )
    receipt = runtime.capture.finish_result_check(
        finish, command, new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


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
