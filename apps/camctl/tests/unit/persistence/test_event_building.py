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
                        {"status": 1, "last_error_json": {"a": Decimal("1.00"), "b": True}},
                        {"status": 2, "last_error_json": {"b": True, "a": 1}})
    assert row.before.values == {"status": 1}
    assert row.after.values == {"status": 2}


def test_update_builder_preserves_boolean_to_number_change():
    row = update_change("operation_runs", 1,
                        {"timeout_s_json": True}, {"timeout_s_json": 1})
    assert row.before.values["timeout_s_json"] is True
    assert type(row.after.values["timeout_s_json"]) is int


@pytest.mark.parametrize("before,after", [
    ({}, {}),
    ({"status": 2}, {"status": Decimal("2.0")}),
    ({"size_bytes": 42}, {"size_bytes": 42}),
])
def test_update_builder_rejects_empty_changes(before, after):
    with pytest.raises(TransactionError):
        update_change("reports", 1, before, after)


@pytest.mark.parametrize("before,after", [
    ({"status": 1}, {"size_bytes": 2}),
    ({"status": 1}, {}),
])
def test_update_builder_does_not_hide_missing_old_or_new_fields(before, after):
    with pytest.raises(TransactionError):
        update_change("reports", 1, before, after)


@pytest.mark.parametrize("before,after", [
    # 未变化的非法列不能被过滤成合法子集后静默接受。
    ({"status": 1, "name": "photo"}, {"status": 2, "name": "photo"}),
    # 变化的非法列同样在构造边界拒绝，不留给内核兜底。
    ({"name": "photo"}, {"name": "video"}),
])
def test_update_builder_rejects_illegal_column_sets(before, after):
    with pytest.raises(TransactionError, match="不是可变业务列"):
        update_change("actions", 1, before, after)


def test_update_builder_rejects_unknown_table():
    with pytest.raises(TransactionError, match="未知业务投影表"):
        update_change("sample", 1, {"value": True}, {"value": 1})
