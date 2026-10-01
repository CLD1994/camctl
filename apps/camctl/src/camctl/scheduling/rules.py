"""公共时间资格与启动窗口分类。

窗口判断精确比较可信墙钟（UTC 微秒），两端包含；窗口结束后的处
理按已可靠取得的启动事实独立分区，不能统一判过期。本模块是纯规
则：不查询外部资源、不读时钟，等待计时由调用方按单调钟执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ExpirationReason",
    "LaunchFact",
    "LaunchWindow",
    "ScheduleDecision",
    "ScheduleFacts",
    "WindowPhase",
    "decide",
    "expiration_reason",
    "window_phase",
]


@dataclass(frozen=True)
class LaunchWindow:
    """启动窗口：scheduled_at 与 window_end 均为主机发起调用的时间边界。"""

    scheduled_at: int
    window_end: int

    def __post_init__(self) -> None:
        if self.window_end < self.scheduled_at:
            raise ValueError(
                f"窗口终点早于计划时间: {self.window_end} < {self.scheduled_at}"
            )


class WindowPhase(Enum):
    """可信时间相对窗口的分区；两端包含，0 宽窗口单点有效。"""

    BEFORE_START = "before_start"
    IN_WINDOW = "in_window"
    AFTER_WINDOW = "after_window"


def window_phase(window: LaunchWindow, trusted_wall_now: int) -> WindowPhase:
    """按 scheduled_at <= now <= window_end 判定时间资格。"""
    if trusted_wall_now < window.scheduled_at:
        return WindowPhase.BEFORE_START
    if trusted_wall_now > window.window_end:
        return WindowPhase.AFTER_WINDOW
    return WindowPhase.IN_WINDOW


class LaunchFact(Enum):
    """已可靠取得的启动事实；决定窗口结束后各自的处理。"""

    START_CONFIRMED = "start_confirmed"
    NOT_ATTEMPTED_OR_NO_EFFECT = "not_attempted_or_no_effect"
    CALL_IN_FLIGHT = "call_in_flight"
    NEEDS_FINITE_VERIFICATION = "needs_finite_verification"
    VERIFICATION_EXHAUSTED_UNKNOWN = "verification_exhausted_unknown"


class ScheduleDecision(Enum):
    """普通启动责任的调度决定分区。"""

    WAIT_UNTIL_START = "wait_until_start"
    WAIT_CONDITIONS = "wait_conditions"
    ELIGIBLE = "eligible"
    EXPIRED = "expired"
    CONTINUE_ORIGINAL = "continue_original"
    CONTINUE_CAPTURE = "continue_capture"
    UNCONFIRMED_FAILED = "unconfirmed_failed"


@dataclass(frozen=True)
class ScheduleFacts:
    """一次普通启动判断的完整输入。

    终态与生效取消由调用方先行处理；window 为空表示无时效动作，
    不受时间窗口阻挡。conditions_ready 汇总准备、预算及资源条件，
    由各自事实共同决定。
    """

    trusted_wall_now: int
    launch_fact: LaunchFact
    conditions_ready: bool
    window: LaunchWindow | None = None


def decide(facts: ScheduleFacts) -> ScheduleDecision:
    """按启动事实优先、时间其次的顺序决定普通启动处理。

    已确认成功、在途调用、待核实意图及核实耗尽各给独立预期：窗口
    结束不改变这些分区，也不得统一判过期。
    """
    if facts.launch_fact is LaunchFact.START_CONFIRMED:
        return ScheduleDecision.CONTINUE_CAPTURE
    if facts.launch_fact is LaunchFact.CALL_IN_FLIGHT:
        return ScheduleDecision.CONTINUE_ORIGINAL
    if facts.launch_fact is LaunchFact.NEEDS_FINITE_VERIFICATION:
        return ScheduleDecision.CONTINUE_ORIGINAL
    if facts.launch_fact is LaunchFact.VERIFICATION_EXHAUSTED_UNKNOWN:
        return ScheduleDecision.UNCONFIRMED_FAILED
    if facts.window is None:
        return (
            ScheduleDecision.ELIGIBLE
            if facts.conditions_ready
            else ScheduleDecision.WAIT_CONDITIONS
        )
    phase = window_phase(facts.window, facts.trusted_wall_now)
    if phase is WindowPhase.BEFORE_START:
        return ScheduleDecision.WAIT_UNTIL_START
    if phase is WindowPhase.AFTER_WINDOW:
        return ScheduleDecision.EXPIRED
    return (
        ScheduleDecision.ELIGIBLE
        if facts.conditions_ready
        else ScheduleDecision.WAIT_CONDITIONS
    )


class ExpirationReason(Enum):
    """过期原因；只依据持久化观察事实。"""

    WINDOW_MISSED = "window_missed"
    WINDOW_EXHAUSTED = "window_exhausted"


def expiration_reason(first_window_observed_at: int | None) -> ExpirationReason:
    """按首次窗口内有效观察是否已持久化区分错过与耗尽。"""
    if first_window_observed_at is None:
        return ExpirationReason.WINDOW_MISSED
    return ExpirationReason.WINDOW_EXHAUSTED
