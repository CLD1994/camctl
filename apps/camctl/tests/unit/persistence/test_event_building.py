"""事件更新构造的独立语义：完整旧值与真实变化，不访问持久化边界。"""
from decimal import Decimal

import pytest

from camctl.persistence.transaction import TransactionError, update_change


def test_update_builder_retains_only_changed_fields():
    row = update_change("reports", 7,
                        {"status": 2, "last_error_json": None, "size_bytes": 42},
                        {"status": 3, "last_error_json": None, "size_bytes": 42})
    assert row.before.values == {"status": 2}
    assert row.after.values == {"status": 3}


def test_update_builder_uses_exact_json_equivalence():
    row = update_change("reports", 7,
                        {"state": 1, "data": {"a": Decimal("1.00"), "b": True}},
                        {"state": 2, "data": {"b": True, "a": 1}})
    assert row.before.values == {"state": 1}
    assert row.after.values == {"state": 2}


def test_update_builder_preserves_boolean_to_number_change():
    row = update_change("sample", 1, {"value": True}, {"value": 1})
    assert row.before.values["value"] is True
    assert type(row.after.values["value"]) is int


@pytest.mark.parametrize("before,after", [({}, {}), ({"value": 1}, {"value": Decimal("1.0")})])
def test_update_builder_rejects_empty_changes(before, after):
    with pytest.raises(TransactionError):
        update_change("sample", 1, before, after)


@pytest.mark.parametrize("before,after", [({"a": 1}, {"b": 2}), ({"a": 1}, {})])
def test_update_builder_does_not_hide_missing_old_or_new_fields(before, after):
    with pytest.raises(TransactionError):
        update_change("sample", 1, before, after)
