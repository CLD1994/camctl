"""交付撤回及等待取消明细的业务资格。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.outputs.handoff import withdrawal_delivery_eligible, withdrawal_waiter_eligible

DELIVERY = enum_for("deliveries.status")
WITHDRAWAL = enum_for("deliveries.withdrawal_state")
ACTION = enum_for("actions.type")
STATUS = enum_for("actions.status")


@pytest.mark.parametrize("status,withdrawal,expected", [
    (DELIVERY.PUBLISHED, WITHDRAWAL.NOT_REQUESTED, True),
    (DELIVERY.WITHDRAWN, WITHDRAWAL.UNKNOWN, True),
    (DELIVERY.PUBLISHING, WITHDRAWAL.UNKNOWN, True),
    (DELIVERY.CANCELED, WITHDRAWAL.UNKNOWN, True),
    (DELIVERY.PUBLISHING, WITHDRAWAL.NOT_REQUESTED, False),
    (DELIVERY.CANCELED, WITHDRAWAL.NOT_REQUESTED, False),
    (DELIVERY.PREPARED, WITHDRAWAL.UNKNOWN, False),
])
def test_withdrawal_requires_published_or_saved_unknown(status, withdrawal, expected):
    assert withdrawal_delivery_eligible({"status": int(status), "withdrawal_state": int(withdrawal)}) is expected


def _waiter():
    return dict(delivery={"id": 7, "action_id": 8, "status": int(DELIVERY.PUBLISHED)},
                target={"id": 8, "type": int(ACTION.OBTAIN_ACTION_OUTPUTS), "cancel_requested": 1},
                parent={"id": 9, "target_action_id": 8, "action_id": 10, "status": 2},
                origin={"id": 10, "type": int(ACTION.CANCEL_TASK), "status": int(STATUS.RUNNING), "cancel_requested": 0},
                delivery_id=7, cancel_item_id=9)


@pytest.mark.parametrize("object_name,field,value", [
    ("delivery", "id", 11), ("target", "id", 11), ("target", "cancel_requested", 0),
    ("parent", "id", 11), ("parent", "target_action_id", 11), ("parent", "status", 1),
    ("origin", "id", 11), ("origin", "cancel_requested", 1),
    ("origin", "status", int(STATUS.SUCCEEDED)),
])
def test_waiter_requires_same_delivery_and_live_cancel_responsibility(object_name, field, value):
    facts = _waiter()
    assert withdrawal_waiter_eligible(**facts)
    facts[object_name][field] = value
    assert not withdrawal_waiter_eligible(**facts)
