"""电机通知的资格分类；持久化未知优先于时间与取消。"""
from dataclasses import dataclass
from enum import Enum

from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.values import ConsistencyError, UtcMicros
from camctl.contracts.workflow_errors import action_error_id, validate_error_details
from camctl.motor.models import MotorFinalKind, SendOutcome, SendPermit
from camctl.scheduling.rules import (ExpirationReason, LaunchWindow, WindowPhase,
                                     expiration_reason, window_phase)


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


class MotorRequestRuleError(ValueError):
    """电机发送或结束申请与实际事实不一致。"""


def validate_motor_combination(action, notice):
    if action["type"] != 8:
        raise ConsistencyError("电机仓储只接受电机动作")
    read_action_spec(action)
    started = action["execution_started"]
    status = action["status"]
    if notice is None:
        if started != 0 or status not in (1, 4, 5, 6):
            raise ConsistencyError("电机动作已开始但缺少必要发送记录")
        return
    if notice.action_id != action["id"] or started != 1:
        raise ConsistencyError("电机发送记录与动作身份或开始事实矛盾")
    if notice.outcome is SendOutcome.PENDING and action["cancel_requested"]:
        raise ConsistencyError("未决发送意图不得独立携带取消标记")
    expected = {
        SendOutcome.PENDING: 2,
        SendOutcome.WRITTEN: 3,
        SendOutcome.FAILED: 4,
        SendOutcome.UNCONFIRMED: 4,
    }
    if notice.outcome is SendOutcome.NOT_SENT:
        if status not in (4, 5, 6):
            raise ConsistencyError("可靠未发送结论必须与终态共同保存")
        if status == 4 and action["error_code"] != action_error_id(
            "motor_channel_unavailable"
        ):
            raise ConsistencyError("未发送失败缺少通道不可用依据")
    elif status != expected[notice.outcome]:
        raise ConsistencyError("电机发送结果与动作状态矛盾")
    code = {
        SendOutcome.FAILED: "motor_notification_failed",
        SendOutcome.UNCONFIRMED: "motor_notification_unconfirmed",
    }.get(notice.outcome)
    if code:
        if action["error_code"] != action_error_id(code):
            raise ConsistencyError("电机发送结果与错误原因矛盾")
        details = action["error_details_json"]
        validate_error_details(code, details)
        if notice.outcome is SendOutcome.FAILED and (
            details["written_bytes"] != notice.written_bytes
            or details.get("errno") != notice.errno
        ):
            raise ConsistencyError("电机写入事实与动作错误详情矛盾")
    if notice.outcome is SendOutcome.NOT_SENT and status == 4:
        validate_error_details(
            "motor_channel_unavailable", action["error_details_json"]
        )


def derive_finish_values(request, facts):
    kind = request.kind
    if not isinstance(kind, MotorFinalKind):
        raise MotorRequestRuleError("结束分类不是受约束的电机结果")
    action = facts.action
    notice = facts.notification
    needs_permission = kind in (
        MotorFinalKind.WRITTEN,
        MotorFinalKind.FAILED,
    )
    needs_permission = needs_permission or (
        notice is not None
        and kind in (MotorFinalKind.EXPIRED, MotorFinalKind.CHANNEL_UNAVAILABLE)
    )
    if needs_permission and not (isinstance(request.permit, SendPermit) and owns_send_permit(facts, request.permit)):
        raise MotorRequestRuleError("该结束事实要求本地流程持有原发送许可")
    if kind is MotorFinalKind.UNCONFIRMED and (
        notice is None
        or notice.outcome is not SendOutcome.PENDING
        or request.permit is not None
    ):
        raise MotorRequestRuleError("未知恢复只解释既有未决意图，不持有本地许可")
    if (
        kind
        in (MotorFinalKind.WRITTEN, MotorFinalKind.FAILED, MotorFinalKind.UNCONFIRMED)
        and notice is None
    ):
        raise MotorRequestRuleError("写入或未知结果要求已有发送意图")
    if action["cancel_requested"]:
        raise MotorRequestRuleError("取消结果由共同取消事务保存，不能再次结束电机发送")
    details = {}
    error_code = None
    expiry = None
    if kind is MotorFinalKind.WRITTEN:
        status = 3
        outcome = SendOutcome.WRITTEN
        if request.reason is not None or request.errno is not None:
            raise MotorRequestRuleError("完整写入不能携带失败原因")
    elif kind is MotorFinalKind.FAILED:
        status = 4
        outcome = SendOutcome.FAILED
        details = {"reason": request.reason, "written_bytes": request.written_bytes}
        if request.errno is not None:
            details["errno"] = request.errno
        validate_error_details("motor_notification_failed", details)
        error_code = action_error_id("motor_notification_failed")
    elif kind is MotorFinalKind.UNCONFIRMED:
        status = 4
        outcome = SendOutcome.UNCONFIRMED
        error_code = action_error_id("motor_notification_unconfirmed")
    elif kind is MotorFinalKind.CHANNEL_UNAVAILABLE:
        status = 4
        outcome = SendOutcome.NOT_SENT
        details = {"reason": request.reason}
        if request.errno is not None:
            details["errno"] = request.errno
        validate_error_details("motor_channel_unavailable", details)
        error_code = action_error_id("motor_channel_unavailable")
    elif kind is MotorFinalKind.EXPIRED:
        status = 5
        outcome = SendOutcome.NOT_SENT
        window = LaunchWindow(
            action["scheduled_at"],
            action["scheduled_at"] + action["max_delay_ms"] * 1000,
        )
        if window_phase(window, request.occurred_at) is not WindowPhase.AFTER_WINDOW:
            raise MotorRequestRuleError("未超过窗口不能保存过期")
        expected = expiration_reason(action["first_window_observed_at"])
        if request.expiration_reason is not expected:
            raise MotorRequestRuleError("过期原因与首次窗口观察矛盾")
        expiry = 1 if expected is ExpirationReason.WINDOW_MISSED else 2
    else:
        raise MotorRequestRuleError("电机结束分类未定义")
    if kind not in (MotorFinalKind.WRITTEN, MotorFinalKind.FAILED):
        if request.written_bytes is not None or (
            request.errno is not None and kind is not MotorFinalKind.CHANNEL_UNAVAILABLE
        ):
            raise MotorRequestRuleError("该结果不能补造写入字节或系统错误")
    if (
        kind not in (MotorFinalKind.FAILED, MotorFinalKind.CHANNEL_UNAVAILABLE)
        and request.reason is not None
    ):
        raise MotorRequestRuleError("该结果不能携带发送失败原因")
    if kind is not MotorFinalKind.EXPIRED and request.expiration_reason is not None:
        raise MotorRequestRuleError("非过期结果不能携带过期原因")
    return status, outcome, error_code, details, expiry


def expected_motor_plan_status(action_id, action, members):
    """用已读取的本计划成员判断本次动作后的计划状态。"""
    if action_id not in members:
        raise ValueError("电机动作不在父计划完整集合中")
    final_members = {
        identity: action if identity == action_id else values
        for identity, values in members.items()
    }
    return (3 if all(v["status"] in (3, 4, 5, 6) for v in final_members.values())
            else 2 if any(v["execution_started"] for v in final_members.values()) else 1)


def validate_motor_plan_change(expected, plan, changes):
    """校验本次已经物化的计划变化，不读取其他成员。"""
    if expected == plan["status"]:
        if changes:
            raise ValueError("电机事务不能写入无变化的父计划状态")
    elif (len(changes) != 1 or changes[0][0] != plan["id"]
          or changes[0][1] != {"status": expected}):
        raise ValueError("电机结果与父计划汇总未共同保存")


def validate_motor_send_intent(request, action_id, occurred_at, old, action,
                               old_notice, final_notice, notice_count):
    UtcMicros(request.trusted_wall_now)
    if request.action_id != action_id or request.occurred_at != occurred_at:
        raise ValueError("发送意图输入与历史身份或时刻不符")
    if notice_count != 1 or final_notice is None or final_notice.outcome is not SendOutcome.PENDING:
        raise ValueError("发送意图必须共同建立唯一未决记录")
    if old_notice is not None or old["status"] != 1 or old["cancel_requested"]:
        raise ValueError("发送意图不能覆盖已开始或取消的动作")
    window = LaunchWindow(old["scheduled_at"], old["scheduled_at"] + old["max_delay_ms"] * 1000)
    if window_phase(window, request.trusted_wall_now) is not WindowPhase.IN_WINDOW:
        raise ValueError("发送意图没有时间资格")
    if final_notice.intent_at != occurred_at or final_notice.finished_at is not None:
        raise ValueError("发送意图时刻或结果矛盾")
    if old["first_window_observed_at"] is None and action["first_window_observed_at"] != request.trusted_wall_now:
        raise ValueError("首次意图必须共同保存窗口观察")


def validate_motor_finish_request(request, action_id, occurred_at, reason, branch_reasons, old):
    if request.action_id != action_id or request.occurred_at != occurred_at or branch_reasons[request.kind] != reason:
        raise ValueError("电机结束输入与历史分支或身份不符")
    if old["status"] in (3, 4, 5, 6):
        raise ValueError("电机终态不得再次结束")


def validate_motor_finish_result(request, action, old_notice, final_notice, notice_count,
                                 occurred_at, status, outcome, code, details, expiry):
    from camctl.contracts.json_values import json_equal

    if (action["status"] != status or action["error_code"] != code
            or not json_equal(action["error_details_json"], details if status == 4 else None)
            or action["expiration_reason"] != expiry):
        raise ValueError("电机动作终态与结束输入不符")
    requires_notice = old_notice is not None or request.kind is MotorFinalKind.CHANNEL_UNAVAILABLE
    if requires_notice:
        if notice_count != 1 or final_notice is None:
            raise ValueError("电机发送结束必须共同保存唯一记录")
        if (final_notice.outcome is not outcome or final_notice.finished_at != occurred_at
                or final_notice.written_bytes != request.written_bytes
                or final_notice.errno != (request.errno if request.kind is MotorFinalKind.FAILED else None)):
            raise ValueError("电机实际写入结果与结束输入不符")
    elif notice_count or final_notice is not None:
        raise ValueError("未开始过期不能补建发送记录")
