"""限定列的逆向值应用不代表完整历史对象恢复。"""

from decimal import Decimal

import pytest

from camctl.history import replay
from camctl.history.events import RowChange, RowImage
from camctl.history.replay import ReplayError


def _change(before, after, *, created=False):
    return RowChange("operation_runs", 7, RowImage(not created, before), RowImage(True, after))


@pytest.mark.parametrize("current,after", [
    (False, 0), (True, 1), ({"value": True}, {"value": 1}),
    ([1, 2], [2, 1]), ({}, {"value": None}),
    (Decimal("0.10000000000000001"), Decimal("0.1")),
])
def test_reverse_requested_column_rejects_distinct_exact_value(current, after):
    values = {"error_json": current}
    with pytest.raises(ReplayError):
        replay.reverse_row_values(values, _change({"error_json": None}, {"error_json": after}), frozenset({"error_json"}))
    assert values["error_json"] is current


@pytest.mark.parametrize("current,after", [
    (None, None), (Decimal("1.000"), 1),
    ({"b": [True, None], "a": Decimal("0.10000000000000001")},
     {"a": Decimal("0.100000000000000010"), "b": [True, None]}),
])
def test_reverse_requested_column_accepts_same_exact_value(current, after):
    result = replay.reverse_row_values({"error_json": current},
                                       _change({"error_json": "old"}, {"error_json": after}),
                                       frozenset({"error_json"}))
    assert result == {"error_json": "old"}


def test_reverse_requires_every_requested_column():
    with pytest.raises(ReplayError):
        replay.reverse_row_values({}, _change({"status": 1}, {"status": 2}), frozenset({"status"}))


def test_reverse_rejects_creation_after_requested_boundary():
    with pytest.raises(ReplayError):
        replay.reverse_row_values({"status": 2}, _change({}, {"status": 2}, created=True), frozenset({"status"}))


def test_reverse_only_returns_requested_columns():
    result = replay.reverse_row_values({"status": 2, "id": 7},
                                       _change({"attempts_used": 1}, {"attempts_used": 2}),
                                       frozenset({"status"}))
    assert result == {"status": 2}
