"""原尝试恢复与取消结果接手。

恢复只依据已保存的可靠事实：只有意图且主机收场已完成的尝试按原
责任核实，不重发；已确认失败、可续传与可靠未派发分别处理不混
用。恢复依据只保存实际掌握的事实，不补造原超时、退出、时刻或配
置。取消触发的终止不丢弃已经取得的可靠结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CancelOutcome",
    "RecoveryClass",
    "RecoveryDecision",
    "RecoveryFacts",
    "RecoveryOutcome",
    "SettledCall",
    "recover_attempt",
    "settle_cancelled_call",
]


class RecoveryClass(Enum):
    """原尝试的恢复分类；未知、已终、可续传与未派发不混用。"""

    UNKNOWN_NEEDS_VERIFICATION = "unknown_needs_verification"
    ALREADY_SETTLED = "already_settled"
    WAIT_HOST_SETTLEMENT = "wait_host_settlement"
    NO_INTENT_RECORDED = "no_intent_recorded"


@dataclass(frozen=True)
class RecoveryFacts:
    """恢复判定的事实输入，全部来自已保存记录。

    不包含从日志或当前配置猜测的数据；host_settlement_complete 表
    示原本地调用责任已经可靠收场（含重启后确认原进程退出）。
    """

    has_intent: bool
    has_saved_result: bool
    host_settlement_complete: bool
    result_failed_confirmed: bool = False
    read_progress_saved: bool = False
    target_length_known: bool = False
    dispatch_prevented_confirmed: bool = False
    read_error_confirmed: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    """恢复判定：分类与明确的不重发保证。

    redispatch 恒为 False——重发资格由业务规则按剩余额度另行判
    断，恢复从不因额度剩余直接发令。
    """

    recovery: RecoveryClass
    redispatch: bool = False
    verify_original: bool = False
    save_basis: bool = False


def recover_attempt(facts: RecoveryFacts) -> RecoveryDecision:
    """按已保存事实分类原尝试的恢复处理。"""
    if not facts.has_intent:
        return RecoveryDecision(recovery=RecoveryClass.NO_INTENT_RECORDED)
    if facts.has_saved_result:
        # 已保存结束结果（失败、错误或可靠未派发）保留原事实。
        return RecoveryDecision(recovery=RecoveryClass.ALREADY_SETTLED)
    if not facts.host_settlement_complete:
        return RecoveryDecision(recovery=RecoveryClass.WAIT_HOST_SETTLEMENT)
    return RecoveryDecision(
        recovery=RecoveryClass.UNKNOWN_NEEDS_VERIFICATION,
        redispatch=False,
        verify_original=True,
        save_basis=True,
    )


@dataclass(frozen=True)
class RecoveryOutcome:
    """接手时已取得的实际退出事实；只保存真实掌握的部分。"""

    local_exit_code: int | None = None
    local_signal: int | None = None

    def __post_init__(self) -> None:
        if self.local_exit_code is None and self.local_signal is None:
            raise ValueError("退出事实至少包含退出码或终止信号之一")


@dataclass(frozen=True)
class CancelOutcome:
    """一次被取消的调用的最终掌握事实。"""

    cancelled: bool
    final_outcome: RecoveryOutcome | None


@dataclass(frozen=True)
class SettledCall:
    """取消接手的保存结果：可靠结果与未知分开表达。"""

    saved: bool
    outcome: RecoveryOutcome | None


async def settle_cancelled_call(outcome: CancelOutcome) -> SettledCall:
    """保存被取消调用的实际结果。

    等待者取消不改变保存责任：已经取得的可靠退出事实原样保存；
    终止后仍无可靠结果时保存未知，不补造退出码或时刻。
    """
    return SettledCall(saved=True, outcome=outcome.final_outcome)
