"""有界报告游标的固定身份与精确整数，不访问资源或数据库。"""
from dataclasses import replace

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError, ValueTypeError, ValueRangeError
from camctl.history.queries import ReportEntityCursor


def _cursor():
    return ReportEntityCursor('instance', HistoryBoundary(2, 4), 3, 9, 'action', 10, 20)


@pytest.mark.parametrize('field,value', [('after_id', True), ('after_id', 1.0), ('after_id', -1),
    ('parent_id', True), ('parent_id', 0), ('from_wm', True), ('to_wm', 2),
    ('instance_id', ''), ('entity', '')])
def test_cursor_rejects_missing_identity_and_inexact_numbers(field, value):
    with pytest.raises((ConsistencyError, ValueTypeError, ValueRangeError)):
        replace(_cursor(), **{field: value})


def test_cursor_advancement_preserves_complete_scope_identity():
    cursor = _cursor()
    advanced = replace(cursor, after_id=9007199254740993)
    assert advanced.after_id == 9007199254740993
    assert advanced.identity == cursor.identity
    for field, value in [('parent_id', 11), ('entity', 'output'), ('boundary', HistoryBoundary(3, 5)),
                         ('from_wm', 4), ('to_wm', 10), ('instance_id', 'other')]:
        assert replace(cursor, **{field: value}).identity != cursor.identity
