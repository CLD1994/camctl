"""录像启动编排与计时锚点。

驱动可靠识别启动成功响应时读取当前会话单调钟作为计时锚点；后续
持久化、日志或源文件查询不重新设置锚点。停止目标为锚点加目标时
长，正常录像不主动少录。意图与派发分别核对窗口；窗口内派发而窗
口后取得的确认仍被接受。仅发送、拒绝无效果、未知及可靠启动后的
调用错误分别保留，不互相覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from camctl.contracts.values import DurationMillis
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.scheduling.rules import LaunchWindow
from camctl.scheduling.resources import recheck_dispatch

__all__ = [
    "CaptureContext",
    "GrantDecision",
    "ReconciliationFacts",
    "ReconciliationPhase",
    "RecordingDecision",
    "RecordingFacts",
    "RecordingPhase",
    "RecordingState",
    "RecoveredControlDecision",
    "RecoveredControlFacts",
    "RecoveredControlReason",
    "StartDispatch",
    "decide_recording_next",
    "decide_recording_reconciliation",
    "decide_recovered_control",
    "recording_stop_target",
    "start_recording",
]

#: 毫秒到纳秒的精确换算。
_MS_TO_NS = 1_000_000


def recording_stop_target(anchor_ns: int, duration: DurationMillis) -> int:
    """停止目标 = 启动确认锚点 + 目标时长（毫秒精确换算为纳秒）。"""
    return anchor_ns + int(duration) * _MS_TO_NS


@dataclass(frozen=True)
class RecordingState:
    """一次录像停止判定的已保存事实。

    anchor_from_current_session 表示计时锚点属于本进程会话；重启后
    旧单调钟读数不能与新会话时钟组合计时，须先对账。
    """

    action_terminal: bool
    started_confirmed: bool
    anchor_from_current_session: bool
    stop_target_ns: int | None
    monotonic_now_ns: int | None
    stop_confirmed: bool
    file_complete_guaranteed: bool
    stop_attempts_used: int
    stop_max_attempts: int
    stop_in_flight: bool


@dataclass(frozen=True)
class RecordingFacts:
    """停止判定的补充事实；取消共用原停止预算，不单独刷新。"""

    canceled: bool = False


@dataclass(frozen=True)
class RecordingDecision:
    """停止判定结果：阶段与是否登记新的停止尝试。"""

    phase: RecordingPhase
    new_stop_attempt: bool = False
    stop_attempts_used: int = 0


def decide_recording_next(
    state: RecordingState, facts: RecordingFacts
) -> RecordingDecision:
    """按录像成功标准决定停止与核实。

    正常录像到达锚点加完整时长才停止，不主动少录；取消生效后不经
    计时立即按剩余预算停止，也不依赖本会话锚点。停止成功与文件
    完成保证分别核对；调用尚未结束保持资源；停止预算沿原流程累计，
    取消与恢复不刷新。
    """
    used = state.stop_attempts_used
    if state.action_terminal:
        return RecordingDecision(phase=RecordingPhase.ALREADY_TERMINAL, stop_attempts_used=used)
    if not state.started_confirmed:
        return RecordingDecision(phase=RecordingPhase.NOT_RUNNING, stop_attempts_used=used)
    if state.stop_confirmed:
        if state.file_complete_guaranteed:
            return RecordingDecision(
                phase=RecordingPhase.CONTROL_COMPLETE, stop_attempts_used=used
            )
        return RecordingDecision(
            phase=RecordingPhase.VERIFY_FILE_COMPLETE, stop_attempts_used=used
        )
    if state.stop_in_flight:
        return RecordingDecision(
            phase=RecordingPhase.STOP_IN_FLIGHT, stop_attempts_used=used
        )
    if not facts.canceled:
        if not state.anchor_from_current_session:
            return RecordingDecision(
                phase=RecordingPhase.RECONCILE_REQUIRED, stop_attempts_used=used
            )
        assert (state.stop_target_ns is not None
                and state.monotonic_now_ns is not None)
        if state.monotonic_now_ns < state.stop_target_ns:
            return RecordingDecision(
                phase=RecordingPhase.WAIT_RECORD, stop_attempts_used=used
            )
    if used >= state.stop_max_attempts:
        return RecordingDecision(
            phase=RecordingPhase.STOP_EXHAUSTED, stop_attempts_used=used
        )
    return RecordingDecision(
        phase=RecordingPhase.READY_TO_STOP,
        new_stop_attempt=True,
        stop_attempts_used=used + 1,
    )


class RecordingPhase(Enum):
    """录像执行的阶段分区：启动编排与停止推进共用。"""

    START_CONFIRMED = "start_confirmed"
    START_CONFIRMED_WITH_ERROR = "start_confirmed_with_error"
    SENT_ONLY = "sent_only"
    REJECTED_NO_EFFECT = "rejected_no_effect"
    START_UNKNOWN = "start_unknown"
    DISPATCH_PREVENTED = "dispatch_prevented"
    NOT_GRANTED = "not_granted"
    NOT_RUNNING = "not_running"
    WAIT_RECORD = "wait_record"
    READY_TO_STOP = "ready_to_stop"
    STOP_IN_FLIGHT = "stop_in_flight"
    VERIFY_FILE_COMPLETE = "verify_file_complete"
    CONTROL_COMPLETE = "control_complete"
    STOP_EXHAUSTED = "stop_exhausted"
    RECONCILE_REQUIRED = "reconcile_required"
    ALREADY_TERMINAL = "already_terminal"


class ReconciliationPhase(Enum):
    """跨会话对账按可信计时的判定分区。"""

    TIMING_SATISFIED = "timing_satisfied"
    TIMING_WAIT_REMAINDER = "timing_wait_remainder"
    TIMING_UNRELIABLE = "timing_unreliable"


@dataclass(frozen=True)
class ReconciliationFacts:
    """一次跨会话对账判定的输入事实。

    启动确认墙钟与当前可信墙钟同源；目标时长毫秒精确换算比较。
    """

    started_at_us: int | None
    target_duration_ms: int
    trusted_now_us: int


def decide_recording_reconciliation(
    facts: ReconciliationFacts,
) -> ReconciliationPhase:
    """以已保存的启动墙钟与当前可信墙钟对账目标时长。

    启动确认墙钟缺失时不能组合计时，按计时不可靠交保守收场规则
    处理，不推测已经录够；恰好到达目标即视为满足。
    """
    if facts.started_at_us is None:
        return ReconciliationPhase.TIMING_UNRELIABLE
    target_us = facts.started_at_us + facts.target_duration_ms * 1_000
    if facts.trusted_now_us >= target_us:
        return ReconciliationPhase.TIMING_SATISFIED
    return ReconciliationPhase.TIMING_WAIT_REMAINDER


class RecoveredControlReason(Enum):
    """恢复停止后控制完成依据的判定理由。"""

    CONTINUOUS = "continuous"
    EXCESS_DURATION = "excess_duration"


@dataclass(frozen=True)
class RecoveredControlDecision:
    """恢复停止后控制完成依据的判定结果；耗时随多录判定携带。"""

    reason: RecoveredControlReason
    control_elapsed_ns: int | None = None


@dataclass(frozen=True)
class RecoveredControlFacts:
    """恢复停止后控制完成依据判定的输入事实。

    本会话锚点的正常停止以锚点加目标为停止目标，不超门槛；恢复
    停止以启动墙钟到停止确认墙钟的控制耗时判定多录。
    """

    session_anchor: bool
    started_at_us: int | None
    stop_confirmed_at_us: int | None
    target_duration_ms: int
    repair_margin_s: Decimal


def decide_recovered_control(
    facts: RecoveredControlFacts,
) -> RecoveredControlDecision | None:
    """判定恢复停止的控制完成依据与异常多录门槛。

    控制耗时超过目标加余量即达到多录修复条件，恰好相等不触发。
    计时证据缺失时不折叠为连续控制完成，返回 None 由调用方保持
    未定决定等待对账。
    """
    if facts.session_anchor:
        return RecoveredControlDecision(RecoveredControlReason.CONTINUOUS)
    if (facts.started_at_us is None or facts.stop_confirmed_at_us is None
            or facts.stop_confirmed_at_us < facts.started_at_us):
        return None
    elapsed_ns = (facts.stop_confirmed_at_us - facts.started_at_us) * 1_000
    threshold_ns = int(
        (Decimal(facts.target_duration_ms) / Decimal(1_000)
         + facts.repair_margin_s) * Decimal(1_000_000_000))
    if elapsed_ns > threshold_ns:
        return RecoveredControlDecision(
            RecoveredControlReason.EXCESS_DURATION,
            control_elapsed_ns=elapsed_ns)
    return RecoveredControlDecision(RecoveredControlReason.CONTINUOUS)


@dataclass(frozen=True)
class GrantDecision:
    """授予端口的可靠结果。"""

    outcome: str
    ticket: Any = None
    reason: str | None = None

    @property
    def granted(self) -> bool:
        return self.outcome == "granted"


@dataclass(frozen=True)
class StartDispatch:
    """驱动启动响应：锚点在确认时由驱动取得。

    confirmed 表示驱动可靠识别了启动成功响应，anchor_ns 是响应
    时刻的单调钟读数；sent_only 表示只确认发送；rejected_no_effect
    表示设备明确拒绝且无效果；error 与可靠观察可以并存。
    """

    confirmed: bool = False
    anchor_ns: int | None = None
    sent_only: bool = False
    rejected_no_effect: bool = False
    error: ErrorValue | None = None
    observations: tuple[DeviceObservation, ...] = ()


class GrantPort(Protocol):
    """首次启动机会授予端口（Q4 完整事务的入口）。"""

    def grant(self, request: Any) -> GrantDecision: ...


class FinishPort(Protocol):
    """尝试结束结果保存端口（O2 完整结果事务的入口）。"""

    def finish(self, ticket: Any, outcome: CallOutcome) -> None: ...

    def finish_prevented(self, ticket: Any, reason: str) -> None: ...


class StartDriverPort(Protocol):
    """驱动启动端口：确认时立即取得单调钟锚点。"""

    async def start(self, ticket: Any) -> StartDispatch: ...


class WallClockPort(Protocol):
    """可信墙钟读取端口（微秒），仅用于派发再检查。"""

    def now_us(self) -> int: ...


@dataclass(frozen=True)
class CaptureContext:
    """一次录像启动编排的完整输入与端口。"""

    device_id: str
    action_id: int
    window: LaunchWindow
    config: AttemptConfig
    duration: DurationMillis
    trusted_wall_now: int
    wall: WallClockPort
    grants: GrantPort
    driver: StartDriverPort
    finishes: FinishPort
    canceled: bool = False


@dataclass(frozen=True)
class CaptureStep:
    """启动编排的结果：已保存事实与下步责任。"""

    phase: RecordingPhase
    stop_target_ns: int | None = None
    ticket: Any = None
    reason: str | None = None


def _outcome(
    *,
    status: AttemptStatus,
    effect: EffectState,
    basis: SettlementBasis,
    evidence_type: str,
    error: ErrorValue | None,
    observations: tuple[DeviceObservation, ...],
) -> CallOutcome:
    return CallOutcome(
        status=status,
        error=error,
        effect=effect,
        settlement=Settlement(
            basis=basis,
            evidence=EvidenceValue(type=evidence_type, version=1, data={}),
        ),
        observations=observations,
    )


async def start_recording(context: CaptureContext) -> CaptureStep:
    """执行一次录像启动：授予、派发再检查、调用与结束保存。

    窗口在授予事务内与派发前分别核对；窗口内派发后取得的确认
    不再受窗口约束。所有结束结果按各自分区保存，次数不退还。
    """
    decision = context.grants.grant(context)
    if not decision.granted:
        return CaptureStep(
            phase=RecordingPhase.NOT_GRANTED,
            ticket=decision.ticket,
            reason=decision.reason,
        )
    ticket = decision.ticket

    check = recheck_dispatch(
        window=context.window,
        trusted_wall_now=context.wall.now_us(),
        canceled=context.canceled,
    )
    if not check.allowed:
        context.finishes.finish_prevented(ticket, check.reason or "prevented")
        return CaptureStep(
            phase=RecordingPhase.DISPATCH_PREVENTED, ticket=ticket, reason=check.reason
        )

    dispatch = await context.driver.start(ticket)
    if dispatch.confirmed:
        stop_target = (
            recording_stop_target(dispatch.anchor_ns or 0, context.duration)
            if dispatch.anchor_ns is not None
            else None
        )
        outcome = _outcome(
            status=(
                AttemptStatus.SUCCEEDED
                if dispatch.error is None
                else AttemptStatus.FAILED
            ),
            effect=EffectState.CONFIRMED,
            basis=SettlementBasis.OBSERVED,
            evidence_type="operation_returned",
            error=dispatch.error,
            observations=dispatch.observations,
        )
        context.finishes.finish(ticket, outcome)
        phase = (
            RecordingPhase.START_CONFIRMED
            if dispatch.error is None
            else RecordingPhase.START_CONFIRMED_WITH_ERROR
        )
        return CaptureStep(
            phase=phase, stop_target_ns=stop_target, ticket=ticket
        )
    if dispatch.sent_only:
        outcome = _outcome(
            status=AttemptStatus.SUCCEEDED,
            effect=EffectState.UNKNOWN,
            basis=SettlementBasis.OBSERVED,
            evidence_type="operation_returned",
            error=None,
            observations=(),
        )
        context.finishes.finish(ticket, outcome)
        return CaptureStep(phase=RecordingPhase.SENT_ONLY, ticket=ticket)
    if dispatch.rejected_no_effect:
        outcome = _outcome(
            status=AttemptStatus.FAILED,
            effect=EffectState.NO_EFFECT,
            basis=SettlementBasis.OBSERVED,
            evidence_type="operation_returned",
            error=dispatch.error,
            observations=(),
        )
        context.finishes.finish(ticket, outcome)
        return CaptureStep(
            phase=RecordingPhase.REJECTED_NO_EFFECT, ticket=ticket
        )
    outcome = _outcome(
        status=AttemptStatus.FAILED,
        effect=EffectState.UNKNOWN,
        basis=SettlementBasis.OBSERVED,
        evidence_type="operation_returned",
        error=dispatch.error,
        observations=dispatch.observations,
    )
    context.finishes.finish(ticket, outcome)
    return CaptureStep(phase=RecordingPhase.START_UNKNOWN, ticket=ticket)
