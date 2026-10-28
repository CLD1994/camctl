"""原尝试恢复与取消结果接手。

恢复只依据已保存的可靠事实：只有意图且主机收场已完成的尝试按原
责任核实，不重发；已确认失败、可续传与可靠未派发分别处理不混
用。恢复依据只保存实际掌握的事实，不补造原超时、退出、时刻或配
置。取消触发的终止不丢弃已经取得的可靠结果。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from camctl.devices.evidence import EvidenceError, EvidenceRegistry

__all__ = [
    "RecoveryBoundary", "RecoveryBlockedReason", "RecoveryDiagnostic", "recovery_registry",
    "CancelOutcome",
    "RecoveryClass",
    "RecoveryDecision",
    "RecoveryFacts",
    "RecoveryOutcome",
    "SettledCall",
    "recover_attempt",
    "settle_cancelled_call",
]


class RecoveryBoundary(Enum):
    """调用方保证的旧本地执行收场边界；普通构造默认不确认。

    正式 run 的部署调用方保证 host 已完成旧工作收场时使用
    HOST_LOCAL_SETTLED；确认重新上电的嵌入式调用方可使用
    HOST_POWER_CYCLE。此值不证明独立设备活动结束。
    """

    UNCONFIRMED = "unconfirmed"
    HOST_LOCAL_SETTLED = "host_local_settled"
    HOST_POWER_CYCLE = "host_power_cycle"


class RecoveryBlockedReason(Enum):
    """原调用仍承担责任时，阻止无依据恢复的输入分区。"""

    UNCONFIRMED_BOUNDARY = "unconfirmed_boundary"
    MISSING_HORIZON = "missing_horizon"
    MISSING_EVIDENCE_LOOKUP = "missing_evidence_lookup"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    INTENT_OUTSIDE_HORIZON = "intent_outside_horizon"
    ACTIVE_LOCAL_READ = "active_local_read"
    ACTIVE_LOCAL_CONTROL = "active_local_control"


@dataclass(frozen=True)
class RecoveryDiagnostic:
    """单份恢复诊断，标明原流程及尝试；不生成数据库或机器协议。"""

    reason: RecoveryBlockedReason
    run_id: int
    attempt_id: int


def recovery_registry(entry: Any, operation: str) -> EvidenceRegistry | None:
    """从原驱动的适用声明取得保留实际返回契约的恢复登记。

    恢复类型与版本共用空正文规则；原驱动必须明确声明本操作适用。
    仅把模板的操作类别绑定原票据；其他契约保持原驱动声明，
    以便可靠页已保存实际返回时仍能验证原结果。
    """
    if entry is None:
        return None
    if operation == "availability" and getattr(entry.declaration, "local_availability_recovery_supported", False):
        evidence_type = "local_availability_recovery"
    elif operation == "control" and getattr(entry.declaration, "local_control_recovery_supported", False):
        evidence_type = "local_control_recovery"
    elif operation in getattr(entry.declaration, "local_file_recovery_operations", ()):
        evidence_type = "local_file_recovery"
    elif operation == "read" and getattr(entry.declaration, "local_read_recovery_supported", False):
        evidence_type = "local_read_recovery"
    elif operation == "result" and getattr(entry.declaration, "local_result_recovery_supported", False):
        evidence_type = "local_result_recovery"
    elif operation in getattr(entry.declaration, "adb_foreground_recovery_operations", ()):
        evidence_type = "adb_foreground_recovery"
    else:
        return None
    try:
        template = entry.evidence.contract(evidence_type, 1)
    except EvidenceError:
        return None
    if template.fields or template.identity_field is not None:
        raise ValueError("原驱动恢复模板必须是正式 v1 空正文契约")
    return entry.evidence.with_contract(replace(template, operation=operation))


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
