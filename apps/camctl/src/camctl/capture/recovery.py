"""录像有限安全收场与应急停止编排。

仅在会话致命错误、可靠归属、排他资格、驱动安全重复停止能力及
可运行条件成立时执行有限应急停止；额度在发令前占用且不退还，
同一会话同一录像唯一流程。停止确认即结束流程不补发；停止结果
与持久化结果分别表达，保存失败不阻止有限停止。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Protocol

from camctl.devices.evidence import EvidenceError, EvidenceRegistry

__all__ = [
    "EmergencyBudget",
    "EmergencyDecision",
    "EmergencyFacts",
    "EmergencyOutcome",
    "EmergencyRecord",
    "EmergencyStopPort",
    "RecordStatus",
    "RecoveryBoundary",
    "RecoveryBlockedReason",
    "RecoveryDiagnostic",
    "emergency_eligibility",
    "emergency_stop",
    "recovery_registry",
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
    if entry is None or operation not in entry.declaration.adb_foreground_recovery_operations:
        return None
    try:
        template = entry.evidence.contract("adb_foreground_recovery", 1)
    except EvidenceError:
        return None
    if template.fields or template.identity_field is not None:
        raise ValueError("原驱动恢复模板必须是正式 v1 空正文契约")
    return entry.evidence.with_contract(replace(template, operation=operation))


class EmergencyDecision(Enum):
    """应急停止的资格分区。"""

    ELIGIBLE = "eligible"
    ALREADY_CONFIRMED_SKIP = "already_confirmed_skip"
    BUDGET_EXHAUSTED_END = "budget_exhausted_end"
    INELIGIBLE_KEEP_DIAGNOSIS = "ineligible_keep_diagnosis"


class EmergencyOutcome(Enum):
    """应急处理的结果分区；只证明停止事实，不宣告动作成功。"""

    STOPPED = "stopped"
    UNCONFIRMED = "unconfirmed"
    NOT_ATTEMPTED = "not_attempted"


class RecordStatus(Enum):
    """补记保存结果的分区；与实际停止结果分开表达。"""

    RECORDED = "recorded"
    NOT_RECORDED = "not_recorded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EmergencyFacts:
    """应急资格判定的事实输入。"""

    process_can_handle: bool
    session_fatal_error: bool
    stop_confirmed: bool
    ownership_confirmed: bool
    exclusive_eligibility: bool
    driver_safe_repeat_stop: bool
    config_known: bool
    budget_available: bool
    stop_observation: Mapping[str, Any] | None = None


def emergency_eligibility(facts: EmergencyFacts) -> EmergencyDecision:
    """判定是否执行有限应急停止。

    普通业务失败、报告失败及日志失败不取得应急资格；执行条件任
    一不成立或未知时保留诊断，不依据猜测操作设备。
    """
    if facts.stop_confirmed:
        return EmergencyDecision.ALREADY_CONFIRMED_SKIP
    if not facts.process_can_handle or not facts.session_fatal_error:
        return EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS
    if not (
        facts.ownership_confirmed
        and facts.exclusive_eligibility
        and facts.driver_safe_repeat_stop
        and facts.config_known
    ):
        return EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS
    if not facts.budget_available:
        return EmergencyDecision.BUDGET_EXHAUSTED_END
    return EmergencyDecision.ELIGIBLE


class EmergencyBudget:
    """本次会话对同一录像的应急停止限额（内存计数）。

    每次发令前占用一次额度；明确失败、超时或结果未知均不退还。
    内存计数只限制当前进程，不承诺跨进程准确恢复。
    """

    def __init__(self, max_attempts: int) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"应急限额必须是正整数: {max_attempts!r}")
        self._max_attempts = max_attempts
        self._used = 0

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def attempts_used(self) -> int:
        return self._used

    def take(self) -> bool:
        """发令前占用一次额度；无额度返回 False。"""
        if self._used >= self._max_attempts:
            return False
        self._used += 1
        return True

    def report_failure(self) -> None:
        """失败不退还已占用额度。"""

    def report_unknown(self) -> None:
        """结果未知不退还已占用额度。"""


class EmergencyStopPort(Protocol):
    """应急停止端口：一次停止调用及其确认。"""

    async def stop(self) -> Any: ...


@dataclass(frozen=True)
class EmergencyRecord:
    """一次应急处理的最终事实：结果与保存状态分别表达。"""

    outcome: EmergencyOutcome
    attempts_used: int
    max_attempts: int | None
    record_status: RecordStatus = RecordStatus.NOT_RECORDED
    stop_observation: Mapping[str, Any] | None = None
    reason: str | None = None


async def emergency_stop(
    facts: EmergencyFacts, budget: EmergencyBudget, port: EmergencyStopPort
) -> EmergencyRecord:
    """执行有限应急停止并形成最终记录。

    已确认停止不补发；资格不成立时不发令如实记录未尝试；额度内
    循环调用至确认或耗尽。记录初始为未保存；持久化由补记事务另
    行执行并更新保存状态。
    """
    decision = emergency_eligibility(facts)
    if decision is EmergencyDecision.ALREADY_CONFIRMED_SKIP:
        if facts.stop_observation is None:
            # 零尝试停止的补记必须保存停止依据；没有可保存的结构化
            # 观察时不能宣称已确认停止。
            raise ValueError("已确认停止的应急资格必须携带可靠停止依据")
        return EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED,
            attempts_used=budget.attempts_used,
            max_attempts=budget.max_attempts,
            stop_observation=facts.stop_observation,
        )
    if decision is EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS:
        conditions = (
            (facts.process_can_handle, "进程无法处理应急错误"),
            (facts.session_fatal_error, "本次没有会话致命错误"),
            (facts.ownership_confirmed, "目标活动归属未确认"),
            (facts.exclusive_eligibility, "目标设备排他资格未确认"),
            (facts.driver_safe_repeat_stop, "驱动安全重复停止能力未确认"),
            (facts.config_known, "停止配置未知"),
        )
        return EmergencyRecord(
            outcome=(EmergencyOutcome.UNCONFIRMED if budget.attempts_used
                     else EmergencyOutcome.NOT_ATTEMPTED),
            attempts_used=budget.attempts_used,
            max_attempts=budget.max_attempts,
            reason="；".join(reason for confirmed, reason in conditions if not confirmed),
        )
    if decision is EmergencyDecision.BUDGET_EXHAUSTED_END:
        return EmergencyRecord(
            outcome=(EmergencyOutcome.UNCONFIRMED if budget.attempts_used
                     else EmergencyOutcome.NOT_ATTEMPTED),
            attempts_used=budget.attempts_used,
            max_attempts=budget.max_attempts,
            reason=f"本次应急停止额度不可用（已尝试 {budget.attempts_used}/{budget.max_attempts} 次）",
        )
    while True:
        if not budget.take():
            return EmergencyRecord(
                outcome=EmergencyOutcome.UNCONFIRMED,
                attempts_used=budget.attempts_used,
                max_attempts=budget.max_attempts,
                reason=f"本次应急停止额度已耗尽（{budget.attempts_used}/{budget.max_attempts} 次），仍未确认停止",
            )
        response = await port.stop()
        if getattr(response, "confirmed", False):
            return EmergencyRecord(
                outcome=EmergencyOutcome.STOPPED,
                attempts_used=budget.attempts_used,
                max_attempts=budget.max_attempts,
            )
