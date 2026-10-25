"""终态拍摄的取消仍须等待原准备责任和占用释放。"""
import pytest

from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.repositories import cancellation


@pytest.mark.parametrize("resolved,occupancy,expected", [
    (False, 1, True),
    (True, 1, True),
    (False, 2, True),
    (True, 2, False),
])
def test_terminal_preparation_is_not_finished_before_original_release(resolved, occupancy, expected):
    facts = StartFacts({"id": 11, "occupancy_state": occupancy}, None, 0, True, 1,
                       preparation_resolved=resolved)
    assert cancellation._preparation_cancel_pending(facts) is expected


def test_started_activity_keeps_its_existing_cancellation_contract():
    facts = StartFacts({"id": 11, "occupancy_state": 1}, None, 1, False, 3)
    assert cancellation._preparation_cancel_pending(facts) is False
