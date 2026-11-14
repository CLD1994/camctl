"""固定登记的公开值与消费入口。"""

from camctl.contracts.enums import enum_for
from camctl.history.events import event_type_name


def test_plan_state_and_event_values_remain_consistent():
    for _ in range(2):
        assert int(enum_for("plans.status").PENDING) == 1
        assert event_type_name(1) == "PLAN_ACCEPTED"
