"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.contracts.enums import decode_member
from camctl.contracts.values import ConsistencyError
from camctl.history.validators import EventValidationError
from camctl.motor.rules import MotorRequestRuleError, derive_finish_values, expected_motor_plan_status, validate_motor_combination as _validate_combination, validate_motor_plan_change, validate_motor_send_intent, validate_motor_finish_request, validate_motor_finish_result
from camctl.motor.models import FinishSendRequest, MotorActionFacts, MotorFinalKind, PrepareSendRequest, SendFacts, SendPermit
from camctl.persistence.atomic import TransactionError
from camctl.scheduling.rules import ExpirationReason


_BRANCH = {
    MotorFinalKind.WRITTEN: 2,
    MotorFinalKind.FAILED: 3,
    MotorFinalKind.CHANNEL_UNAVAILABLE: 4,
    MotorFinalKind.EXPIRED: 5,
    MotorFinalKind.UNCONFIRMED: 7,
}


def _notice(values):
    try:
        return SendFacts(
            values["id"],
            values["action_id"],
            values["intent_at"],
            values["intent_operation_key"],
            decode_member("motor_notifications.outcome", values["outcome"]),
            values["written_bytes"],
            values["errno"],
            values["finished_at"],
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ConsistencyError("电机发送记录不可解释") from error


def _finish_values(request, facts):
    try:
        return derive_finish_values(request, facts)
    except MotorRequestRuleError as error:
        message = str(error)
    raise TransactionError(message)


def _motor_guard(event, context):
    try:
        action_rows = [row for row in event.rows if row.table == "actions"]
        if len(action_rows) != 1:
            raise ValueError("电机事务必须有唯一动作变化")
        change = action_rows[0]
        old = context.state_rows["actions"][change.row_id]
        action = {**old, **change.after.values}
        notices = context.complete_rows(
            "motor_notifications", "action_id", change.row_id
        )
        if len(notices) > 1:
            raise ValueError("电机动作发送记录不唯一")
        old_notice = None if not notices else _notice(next(iter(notices.values())))
        notice_rows = [row for row in event.rows if row.table == "motor_notifications"]
        final_notice = old_notice
        if notice_rows:
            if len(notice_rows) != 1:
                raise ValueError("电机事务发送记录变化不唯一")
            row = notice_rows[0]
            values = {
                **(
                    {}
                    if not row.before.exists
                    else context.state_rows["motor_notifications"][row.row_id]
                ),
                **row.after.values,
                "id": row.row_id,
            }
            final_notice = _notice(values)
        _validate_combination(action, final_notice)
        members = context.complete_rows("actions", "plan_id", old["plan_id"])
        expected = expected_motor_plan_status(change.row_id, action, members)
        plan = context.state_rows["plans"][old["plan_id"]]
        changes = tuple((row.row_id, row.after.values) for row in event.rows if row.table == "plans")
        validate_motor_plan_change(expected, plan, changes)
        if event.reason == 1:
            request = PrepareSendRequest(**event.evidence["request"])
            validate_motor_send_intent(request, change.row_id, event.occurred_at,
                old, action, old_notice, final_notice, len(notice_rows))
        else:
            evidence = event.evidence["request"]
            values = dict(evidence)
            values["kind"] = MotorFinalKind(values["kind"])
            values["permit"] = (
                None if values["permit"] is None else SendPermit(**values["permit"])
            )
            values["expiration_reason"] = (
                None
                if values["expiration_reason"] is None
                else ExpirationReason(values["expiration_reason"])
            )
            request = FinishSendRequest(**values)
            validate_motor_finish_request(request, change.row_id, event.occurred_at,
                event.reason, _BRANCH, old)
            status, outcome, code, details, expiry = _finish_values(
                request, MotorActionFacts(old, old_notice))
            validate_motor_finish_result(request, action, old_notice, final_notice,
                len(notice_rows), event.occurred_at, status, outcome, code, details, expiry)
    except (ValueError, KeyError, TypeError, TransactionError) as error:
        raise EventValidationError(f"电机共同事务违反契约: {error}") from error


def register_motor_guards(register):
    register("motor", _motor_guard)
