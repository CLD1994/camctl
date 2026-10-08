"""从电机状态表独立列举优先级和闭区间窗口。"""
import pytest

from camctl.motor.rules import MotorDecision, MotorFacts, decide_motor
from camctl.scheduling.rules import LaunchWindow


@pytest.mark.parametrize("facts,expected", [
    ({"terminal": True, "has_intent": True}, MotorDecision.KEEP_TERMINAL),
    ({"has_intent": True, "now": 2001}, MotorDecision.RECOVER_UNKNOWN),
    ({"terminal": True, "now": 999}, MotorDecision.KEEP_TERMINAL),
    ({"now": 999}, MotorDecision.WAIT),
    ({"now": 1000}, MotorDecision.PREPARE),
    ({"now": 2000}, MotorDecision.PREPARE),
    ({"now": 2001}, MotorDecision.EXPIRE),
    ({"available": False}, MotorDecision.CHANNEL_UNAVAILABLE),
    ({"has_intent": True, "owns_intent": True}, MotorDecision.SEND),
    ({"terminal": True, "has_intent": True, "owns_intent": True}, MotorDecision.KEEP_TERMINAL),
    ({"has_intent": True, "owns_intent": True, "now": 2001}, MotorDecision.EXPIRE),
    ({"has_intent": True, "owns_intent": True, "now": 999}, MotorDecision.WAIT),
])
def test_motor_state_partition(facts, expected):
    values = dict(terminal=False, has_intent=False, owns_intent=False,
                  now=1500, available=True,
                  window=LaunchWindow(1000, 2000))
    values.update(facts)
    assert decide_motor(MotorFacts(**values)) is expected


@pytest.mark.parametrize("now,expected", [
    (999, MotorDecision.WAIT), (1000, MotorDecision.PREPARE),
    (1001, MotorDecision.EXPIRE),
])
def test_zero_width_window(now, expected):
    facts = MotorFacts(terminal=False, has_intent=False, owns_intent=False,
                       now=now, available=True,
                       window=LaunchWindow(1000, 1000))
    assert decide_motor(facts) is expected


def test_owner_without_persisted_intent_is_inconsistent():
    with pytest.raises(ValueError):
        decide_motor(MotorFacts(False, False, True, 1500, True,
                                LaunchWindow(1000, 2000)))
