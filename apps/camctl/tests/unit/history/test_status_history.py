"""状态时间只消费已派生的创建与转换，事件顺序独立于墙钟。"""
import pytest
from camctl.contracts.values import ConsistencyError
from camctl.history.queries import status_transitions


def test_state_order_preserves_equal_and_reversed_wall_clock():
    rows = [(1, None, 1, 2_900_000, 1), (9, 1, 2, 1_100_000, 2), (100, 2, 6, 1_100_000, 3)]
    assert status_transitions(rows, 6) == [
        {"status": 1, "changed_at": 2_900_000, "clock_status": 1},
        {"status": 2, "changed_at": 1_100_000, "clock_status": 2},
        {"status": 6, "changed_at": 1_100_000, "clock_status": 3},
    ]


def test_creation_can_already_be_terminal_and_at_epoch():
    assert status_transitions([(1, None, 4, 0, 2)], 4) == [
        {"status": 4, "changed_at": 0, "clock_status": 2}]


@pytest.mark.parametrize("rows,current", [
    ([], 1), ([(1, 1, 2, 1, 2)], 2), ([(1, None, 1, 1, 2)], 2),
    ([(2, None, 1, 1, 2), (1, 1, 2, 1, 2)], 2),
    ([(1, None, 1, 1, 2), (2, 2, 3, 1, 2)], 3),
    ([(1, None, 1, 1, 2), (2, 1, 1, 1, 2)], 1),
    ([(1, None, 1, 1, 2), (2, None, 2, 1, 2)], 2),
])
def test_incomplete_or_inconsistent_directory_is_rejected(rows, current):
    with pytest.raises(ConsistencyError):
        status_transitions(rows, current)
