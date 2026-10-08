"""电机通知的资格分类；持久化未知优先于时间与取消。"""
from dataclasses import dataclass
from enum import Enum

from camctl.contracts.values import ConsistencyError
from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase


class MotorDecision(Enum):
    KEEP_TERMINAL = "keep_terminal"
    RECOVER_UNKNOWN = "recover_unknown"
    WAIT = "wait"
    EXPIRE = "expire"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    PREPARE = "prepare"
    SEND = "send"


@dataclass(frozen=True)
class MotorFacts:
    terminal: bool
    has_intent: bool
    owns_intent: bool
    now: int
    available: bool
    window: LaunchWindow


def decide_motor(facts: MotorFacts) -> MotorDecision:
    """消费可靠事实和已检查的墙钟，不把既有意图当作新发送许可。"""
    if facts.owns_intent and not facts.has_intent:
        raise ConsistencyError("本次发送许可缺少已保存意图")
    if facts.terminal:
        return MotorDecision.KEEP_TERMINAL
    if facts.has_intent and not facts.owns_intent:
        return MotorDecision.RECOVER_UNKNOWN
    phase = window_phase(facts.window, facts.now)
    if phase is WindowPhase.BEFORE_START:
        return MotorDecision.WAIT
    if phase is WindowPhase.AFTER_WINDOW:
        return MotorDecision.EXPIRE
    if not facts.available:
        return MotorDecision.CHANNEL_UNAVAILABLE
    return MotorDecision.SEND if facts.owns_intent else MotorDecision.PREPARE


def owns_send_permit(facts, permit) -> bool:
    """本地未使用许可必须精确对应已保存且未决的原发送意图。"""
    from camctl.motor.models import SendOutcome

    notice = facts.notification
    return (permit is not None and notice is not None
            and notice.outcome is SendOutcome.PENDING
            and notice.action_id == permit.action_id == facts.action["id"]
            and notice.notification_id == permit.notification_id
            and notice.intent_operation_key == permit.operation_key)
