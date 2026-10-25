"""状态历史只表达真实主行转换，顺序由事件身份决定。"""
import pytest

from camctl.contracts.values import ConsistencyError
from camctl.history import queries
from camctl.history.events import EventEnvelope, RowChange, RowImage


def event(identity, before, after, occurred_at, *, clock=2, table="actions", row_id=7):
    return EventEnvelope(identity, identity, 1, 1, occurred_at, clock, None, 1, {}, (
        RowChange(table, row_id, RowImage(before is not None, before or {}), RowImage(True, after)),
    ))


def test_status_history_keeps_event_order_and_ignores_internal_updates():
    events = [event(1, None, {"status": 1}, 2_900_000, clock=1),
              event(2, {"status": 1}, {"status": 2}, 1_100_000),
              event(3, {"cancel_requested": 0}, {"cancel_requested": 1}, 7_000_000),
              event(4, {"status": 2}, {"status": 6}, 1_100_000, clock=3)]
    assert queries.status_transitions(events, "actions", 7, 6) == [
        {"status": 1, "changed_at": 2_900_000, "clock_status": 1},
        {"status": 2, "changed_at": 1_100_000, "clock_status": 2},
        {"status": 6, "changed_at": 1_100_000, "clock_status": 3},
    ]


def test_creation_can_already_be_terminal():
    assert queries.status_transitions([event(1, None, {"status": 4}, 0)], "actions", 7, 4) == [
        {"status": 4, "changed_at": 0, "clock_status": 2},
    ]


def test_only_proven_placeholder_events_have_unknown_time():
    events = [event(1, None, {"status": 2}, 1), event(2, {"status": 2}, {"status": 3}, 0)]
    assert queries.status_transitions(events, "actions", 7, 3, placeholder_events={2})[-1] == {
        "status": 3, "changed_at": None, "clock_status": 2,
    }


@pytest.mark.parametrize("events,current", [
    ([], 1),
    ([event(1, {"status": 1}, {"status": 2}, 1)], 2),
    ([event(1, None, {"status": 1}, 1)], 2),
    ([event(2, None, {"status": 1}, 1), event(1, {"status": 1}, {"status": 2}, 1)], 2),
    ([event(1, None, {"status": 1}, 1), event(2, {"status": 2}, {"status": 3}, 1)], 3),
])
def test_incomplete_or_inconsistent_history_is_rejected(events, current):
    with pytest.raises(ConsistencyError):
        queries.status_transitions(events, "actions", 7, current)
