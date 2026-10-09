"""单张拍摄独立流程。

照片按自身完成声明执行：完成后返回契约使用响应完成证据，只发
送契约等待并核实产物。单张任务没有录像计时或停止目标，不采用
录像流程的默认假设；未启动取消不创建尝试，可能启动且无停止能
力时保留未知效果；取消后到达的合法完成文件保留。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)

__all__ = [
    "CaptureAssessment",
    "PhotoCompletion",
    "PhotoDecision",
    "PhotoDispatch",
    "PhotoState",
    "PhotoStep",
    "decide_photo",
    "run_photo",
]


class PhotoCompletion(Enum):
    """照片任务的完成声明。"""

    COMPLETED_ON_RETURN = "completed_on_return"
    SENT_ONLY = "sent_only"


class PhotoDecision(Enum):
    """照片处理的决定分区。"""

    WAIT_RESPONSE = "wait_response"
    VERIFY_RESULTS = "verify_results"
    REGISTER_SUCCESS = "register_success"
    FAILED_KEEP_FILES = "failed_keep_files"
    KEEP_FILES_UNDER_CANCEL = "keep_files_under_cancel"
    NOT_DISPATCHED_CANCELED = "not_dispatched_canceled"
    UNKNOWN_NO_STOP = "unknown_no_stop"
    ALREADY_TERMINAL = "already_terminal"


@dataclass(frozen=True)
class PhotoState:
    """照片判定的已保存事实；不含录像计时或停止目标。"""

    action_terminal: bool
    canceled: bool
    dispatched: bool
    response_completed: bool
    response_failed: bool
    effect_unknown: bool
    stop_supported: bool


@dataclass(frozen=True)
class CaptureAssessment:
    """产物核实的最小结果端口；完整评估随 C6 对齐。"""

    complete: bool
    explicitly_unmet: bool = False
    read_error: bool = False


def decide_photo(
    state: PhotoState, result: CaptureAssessment, completion: PhotoCompletion
) -> PhotoDecision:
    """按照片自身完成声明决定处理。

    终态、取消与明确失败证据优先；完成后返回契约使用响应完成证据，
    只发送契约依赖产物核实；读取错误不解释为产物缺失。
    """
    if state.action_terminal:
        return PhotoDecision.ALREADY_TERMINAL
    if state.canceled and not state.dispatched:
        return PhotoDecision.NOT_DISPATCHED_CANCELED
    if state.response_failed:
        return PhotoDecision.FAILED_KEEP_FILES
    if state.effect_unknown and not state.stop_supported:
        return PhotoDecision.UNKNOWN_NO_STOP
    if state.canceled:
        # 取消已生效：合法完成文件保留事实，不用迟到结果覆盖取消。
        if result.complete:
            return PhotoDecision.KEEP_FILES_UNDER_CANCEL
        return PhotoDecision.NOT_DISPATCHED_CANCELED
    if completion is PhotoCompletion.COMPLETED_ON_RETURN:
        if state.response_completed:
            return (PhotoDecision.REGISTER_SUCCESS if result.complete
                    else PhotoDecision.VERIFY_RESULTS)
        return PhotoDecision.WAIT_RESPONSE
    if result.complete:
        return PhotoDecision.REGISTER_SUCCESS
    return PhotoDecision.VERIFY_RESULTS


@dataclass(frozen=True)
class PhotoDispatch:
    """驱动照片响应：按任务完成声明携带证据。"""

    completion: PhotoCompletion
    completed: bool
    error: ErrorValue | None = None


class PhotoDriverPort(Protocol):
    """照片驱动端口。"""

    async def shoot(self, ticket: Any) -> PhotoDispatch: ...


class PhotoFinishPort(Protocol):
    """照片调用结果保存端口。"""

    def finish(self, ticket: Any, outcome: CallOutcome) -> None: ...


@dataclass(frozen=True)
class PhotoPorts:
    """照片编排依赖的端口集合。"""

    driver: PhotoDriverPort
    finishes: PhotoFinishPort


@dataclass(frozen=True)
class PhotoContext:
    """一次照片编排的输入：状态、核实结果、完成声明与端口。"""

    state: PhotoState
    assessment: CaptureAssessment
    completion: PhotoCompletion
    ports: PhotoPorts


@dataclass(frozen=True)
class PhotoStep:
    """照片编排结果；单张任务没有停止目标。"""

    phase: PhotoDecision
    ticket: Any = None
    stop_target_ns: None = None


def _outcome(dispatch: PhotoDispatch) -> CallOutcome:
    if dispatch.completion is PhotoCompletion.COMPLETED_ON_RETURN and dispatch.completed:
        status = AttemptStatus.SUCCEEDED
        effect = EffectState.CONFIRMED
    elif dispatch.completed:
        status = AttemptStatus.SUCCEEDED
        effect = EffectState.UNKNOWN
    else:
        status = AttemptStatus.FAILED if dispatch.error else AttemptStatus.SUCCEEDED
        effect = EffectState.UNKNOWN
    return CallOutcome(
        status=status,
        error=dispatch.error,
        effect=effect,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type="operation_returned", version=1, data={}),
        ),
        observations=(),
    )


async def run_photo(context: PhotoContext) -> PhotoStep:
    """执行一次照片调用并按完成声明分流保存。

    单张任务不产生录像计时或停止；结果分区由 decide_photo 决定，
    本编排只负责调用与保存，不引入录像假设。
    """
    decision = decide_photo(context.state, context.assessment, context.completion)
    if decision in (
        PhotoDecision.ALREADY_TERMINAL,
        PhotoDecision.NOT_DISPATCHED_CANCELED,
    ):
        return PhotoStep(phase=decision)
    dispatch = await context.ports.driver.shoot(None)
    context.ports.finishes.finish(None, _outcome(dispatch))
    final = decide_photo(
        PhotoState(
            action_terminal=False,
            canceled=context.state.canceled,
            dispatched=True,
            response_completed=dispatch.completed
            and dispatch.completion is PhotoCompletion.COMPLETED_ON_RETURN,
            response_failed=dispatch.error is not None,
            effect_unknown=context.state.effect_unknown,
            stop_supported=context.state.stop_supported,
        ),
        context.assessment,
        context.completion,
    )
    return PhotoStep(phase=final)
