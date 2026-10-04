"""拍摄能力路由与处理器。

处理器是调度侧（Q5）登记的动作推进入口：按能力阶段推进一次执
行——授予启动机会、发起设备契约调用、把结果列举观察登记为设备
文件（归属按任务独立范围确认、完成按设备保证保存）、按 C6 核实
产物集合，可判定时保存终态与正式产物。驱动与结果列举由端口提供
（契约替身与真实驱动同形）。设备活动与处理决定的生产者随调度接
线接入前，录像中段与延时等待事实经能力状态端口装载；录像媒体链
与 D4 读取会话工厂属后续分段。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from camctl.capture.files import (
    FileCompletionSave,
    FileObservationSave,
    OwnershipSave,
)
from camctl.capture.media import (
    RecordingFailure,
    RecordingOutcomeFacts,
    decide_recording_result,
)
from camctl.capture.photo import (
    CaptureAssessment,
    PhotoCompletion,
    PhotoDecision,
    PhotoState,
    decide_photo,
)
from camctl.capture.recording import (
    RecordingFacts,
    RecordingPhase,
    RecordingState,
    decide_recording_next,
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
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import ControlRequest, DeviceCallResult
from camctl.operations.attempts import AttemptConfig, AttemptFinish
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
from camctl.persistence.repositories.capture import CaptureRepository, FinishCapture
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

_ACTION_TERMINAL = (3, 4, 5, 6)

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")


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


class ResultFilesPort(Protocol):
    """结果列举端口：返回本任务观察到的候选产物文件。"""

    async def list_files(self, action_id: int) -> tuple[ObservedFile, ...]: ...


class RecordingStatePort(Protocol):
    """录像中段事实端口；活动事实生产者接入前由装配层提供。"""

    def recording_state(self, action_id: int) -> RecordingState: ...


@dataclass
class CaptureRuntime:
    """处理器组合的真实仓储端口与设备替身注入点。

    window_of 从动作行取得启动窗口；wait_config 从生效参数取得延时
    等待配置；monotonic_ns 提供会话单调钟；evidence 登记驱动观察契
    约供尝试结果校验。
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

    def finish(self, ticket, outcome: CallOutcome) -> None:
        attempt = AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, self.evidence),
            occurred_at=self.wall_us(),
        )
        receipt = self.operations.finish_attempt(
            attempt, new_operation_key(), self.owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _binding(action: Mapping[str, Any]) -> DeviceBinding:
    return DeviceBinding(device_id=action["device_id"], driver_id=action["driver_id"])


def _operation_outcome(result: DeviceCallResult, confirmed_observation: str):
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
            evidence=EvidenceValue(type="operation_returned", version=1, data={}),
        ),
        observations=result.observations,
    )
    return outcome, confirmed


async def _control_call(runtime: CaptureRuntime, action, operation: str,
                        confirmed_observation: str) -> HandlerOutcome:
    """授予启动机会后发起一次设备控制调用并保存尝试结局。"""
    ticket, reason = runtime.grant(action)
    if ticket is None:
        return HandlerOutcome("not_granted", reason)
    result = await runtime.driver.control(ControlRequest(
        operation=operation,
        binding=_binding(action),
        params=action["effective_params_json"],
    ))
    outcome, confirmed = _operation_outcome(result, confirmed_observation)
    runtime.finish(ticket, outcome)
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


def _finish_capture(
    runtime: CaptureRuntime, action_id: int, entries: tuple[ObservedFile, ...],
    required: FileKind, *, failure: RecordingFailure | None = None,
) -> HandlerOutcome:
    """C6 核实产物集合并保存终态；失败保留完整且归属明确的文件。"""
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


def _target_duration_ms(action: Mapping[str, Any]) -> int:
    params = action["effective_params_json"]
    if not isinstance(params, Mapping):
        raise ConsistencyError("录像生效参数不是对象")
    duration = params.get("target_duration_s")
    if isinstance(duration, bool) or not isinstance(duration, (int, Decimal, float)):
        raise ConsistencyError(f"录像缺少可解释的目标时长: {duration!r}")
    milliseconds = int(Decimal(str(duration)) * 1000)
    if milliseconds <= 0:
        raise ConsistencyError(f"录像目标时长必须是正数: {duration!r}")
    return milliseconds


async def _photo_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            # 未派发取消不创建尝试；终态由取消收场处理。
            return
        step = await _control_call(context, action, "take_photo", "photo_taken")
        if step.phase not in ("confirmed", "call_failed"):
            # 仅发送或未授予：没有可靠响应事实，等待下次推进。
            return
        attempt = context.last_attempt(f"start/{action_id}")
    entries = await context.results.list_files(action_id)
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
        _finish_capture(context, action_id, entries, FileKind.PHOTO)
    elif decision is PhotoDecision.FAILED_KEEP_FILES:
        _finish_capture(
            context, action_id, entries, FileKind.PHOTO,
            failure=RecordingFailure(
                code="capture_failed",
                details={"activity_id": str(action_id), "reason": "device_failed"}))
    # 其余分区（等待响应、取消保留、未知无停止）等待下次推进或取消收场。


async def _record_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    if context.last_attempt(f"start/{action_id}") is None:
        # 尚未发起启动：首次授予并调用，不依赖中段事实端口。
        if action["cancel_requested"]:
            return
        await _control_call(context, action, "start_recording", "start_confirmed")
        return
    if context.recording_state is None:
        raise LookupError("录像中段事实端口未装配")
    decision = decide_recording_next(
        context.recording_state.recording_state(action_id),
        RecordingFacts(canceled=bool(action["cancel_requested"])),
    )
    if decision.phase is RecordingPhase.NOT_RUNNING:
        await _control_call(context, action, "start_recording", "start_confirmed")
        return
    if decision.phase not in (RecordingPhase.CONTROL_COMPLETE,
                              RecordingPhase.VERIFY_FILE_COMPLETE):
        # 等待计时、停止推进与跨会话对账随中段端口接线后由调度推进。
        return
    processing = context.owned.connection.execute(
        "SELECT id, check_decision, check_state, repair_state"
        " FROM recording_processing WHERE action_id = ?", (action_id,)).fetchone()
    if processing is None:
        # 处理责任尚未建立；正常录像的无需检查决定随媒体链接线保存。
        return
    facts = RecordingOutcomeFacts(
        processing_id=processing[0],
        control_complete=decision.phase is RecordingPhase.CONTROL_COMPLETE,
        check_decision=processing[1],
        check_state=processing[2],
        check_duration_s=None,
        check_issues=False,
        input_unavailable=False,
        repair_state=processing[3],
        target_duration_ms=_target_duration_ms(action),
    )
    result = decide_recording_result(facts)
    entries = await context.results.list_files(action_id)
    if result.kind.value == "succeeded":
        _finish_capture(context, action_id, entries, FileKind.VIDEO)
    elif result.kind.value == "failed":
        _finish_capture(context, action_id, entries, FileKind.VIDEO,
                        failure=result.failure)
    # 待定（必要处理未结束）等待媒体链推进。


async def _timelapse_handler(action_id: int, context: CaptureRuntime) -> None:
    action = context.action(action_id)
    if action["status"] in _ACTION_TERMINAL:
        return
    attempt = context.last_attempt(f"start/{action_id}")
    if attempt is None:
        if action["cancel_requested"]:
            return
        step = await _control_call(context, action, "start_timelapse", "timelapse_sent")
        if step.phase not in ("confirmed", "sent"):
            return
    from contextlib import closing

    with closing(context.owned.connection.execute(
        "SELECT sent_at, expected_check_at FROM device_activities WHERE id = ?",
        (action_id,),
    )) as cursor:
        activity = cursor.fetchone()
    if activity is None or activity[0] is None:
        # 发送事实（sent_at）由活动观察边界保存；尚未保存时等待。
        return
    config = context.wait_config(action["effective_params_json"])
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
    entries = await context.results.list_files(action_id)
    _finish_capture(context, action_id, entries, FileKind.VIDEO)


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
