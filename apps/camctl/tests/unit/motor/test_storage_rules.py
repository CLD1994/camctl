"""电机保存判定只消费已读取的动作、成员与发送事实。"""

import pytest

from camctl.motor.models import (
    FinishSendRequest, MotorActionFacts, MotorFinalKind, PrepareSendRequest, SendFacts, SendOutcome, SendPermit,
)
from camctl.motor.rules import (
    MotorRequestRuleError, derive_finish_values, expected_motor_plan_status,
    validate_motor_finish_result, validate_motor_plan_change, validate_motor_send_intent,
)


def test_plan_status_uses_only_materialized_members():
    action = {"status": 3, "execution_started": 1}
    members = {1: {"status": 2, "execution_started": 1}, 2: {"status": 1, "execution_started": 0}}
    expected = expected_motor_plan_status(1, action, members)
    assert expected == 2
    validate_motor_plan_change(expected, {"id": 7, "status": 2}, ())
    with pytest.raises(ValueError, match="不在父计划完整集合"):
        expected_motor_plan_status(3, action, members)
    with pytest.raises(ValueError, match="不能写入无变化"):
        validate_motor_plan_change(expected, {"id": 7, "status": 2}, ((7, {"status": 2}),))
    members[2] = {"status": 4, "execution_started": 0}
    assert expected_motor_plan_status(1, action, members) == 3
    validate_motor_plan_change(3, {"id": 7, "status": 2}, ((7, {"status": 3}),))


def test_send_intent_window_and_original_observation():
    old = {"status": 1, "cancel_requested": 0, "scheduled_at": 1000,
           "max_delay_ms": 1, "first_window_observed_at": None}
    notice = SendFacts(2, 1, 1000, "key", SendOutcome.PENDING, None, None, None)
    evidence = {"action_id": 1, "trusted_wall_now": 2000, "occurred_at": 1000}
    validate_motor_send_intent(PrepareSendRequest(**evidence), 1, 1000, old,
                               {"first_window_observed_at": 2000}, None, notice, 1)
    with pytest.raises(ValueError, match="没有时间资格"):
        validate_motor_send_intent(PrepareSendRequest(**{**evidence, "trusted_wall_now": 2001}), 1, 1000, old,
                                   {"first_window_observed_at": 2001}, None, notice, 1)


def test_finish_uses_original_permit_and_real_written_result():
    notice = SendFacts(2, 1, 1000, "key", SendOutcome.PENDING, None, None, None)
    permit = SendPermit(1, 2, "key")
    facts = MotorActionFacts({"id": 1, "cancel_requested": 0}, notice)
    request = FinishSendRequest(1, 1100, MotorFinalKind.WRITTEN, permit, written_bytes=12)
    status, outcome, code, details, expiry = derive_finish_values(request, facts)
    assert (status, outcome, code, details, expiry) == (3, SendOutcome.WRITTEN, None, {}, None)
    finished = SendFacts(2, 1, 1000, "key", SendOutcome.WRITTEN, 12, None, 1100)
    action = {"status": 3, "error_code": None, "error_details_json": None, "expiration_reason": None}
    validate_motor_finish_result(request, action, notice, finished, 1, 1100, status, outcome, code, details, expiry)
    with pytest.raises(MotorRequestRuleError, match="原发送许可"):
        derive_finish_values(FinishSendRequest(1, 1100, MotorFinalKind.WRITTEN, written_bytes=12), facts)
    with pytest.raises(ValueError, match="实际写入结果"):
        validate_motor_finish_result(request, action, notice, finished, 1, 1101, status, outcome, code, details, expiry)
