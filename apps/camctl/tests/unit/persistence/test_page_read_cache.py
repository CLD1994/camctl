"""已提交页复用限于同一连接、事务和有限原页窗口。"""
from types import SimpleNamespace

import pytest

from camctl.persistence.page_read_cache import (
    cached_page_record, clear_page_records, discard_page_records,
    page_read_scope, remember_page_record,
)


def test_page_records_require_active_transaction_and_original_upper_bound():
    connection = SimpleNamespace(in_transaction=True)
    old, candidate = object(), object()
    with page_read_scope(connection, 20):
        remember_page_record(connection, 7, 10, old)
        remember_page_record(connection, 19, 21, candidate)
        assert cached_page_record(connection, 7) is old
        assert cached_page_record(connection, 19) is None
        connection.in_transaction = False
        assert cached_page_record(connection, 7) is None
    connection.in_transaction = True
    assert cached_page_record(connection, 7) is None


def test_page_records_keep_only_three_recent_pages():
    connection = SimpleNamespace(in_transaction=True)
    values = [object() for _ in range(4)]
    with page_read_scope(connection, 20):
        for event_id, value in enumerate(values, 1):
            remember_page_record(connection, event_id, event_id, value)
        assert cached_page_record(connection, 1) is None
        assert [cached_page_record(connection, i) for i in (2, 3, 4)] == values[1:]


def test_nested_scope_never_widens_original_committed_range():
    connection = SimpleNamespace(in_transaction=True)
    with page_read_scope(connection, 10):
        with page_read_scope(connection, 20):
            remember_page_record(connection, 15, 15, object())
        assert cached_page_record(connection, 15) is None


def test_nested_connection_cannot_use_or_replace_another_connections_record():
    first, second = (SimpleNamespace(in_transaction=True) for _ in range(2))
    left, right = object(), object()
    with page_read_scope(first, 10):
        remember_page_record(first, 7, 10, left)
        assert cached_page_record(second, 7) is None
        with page_read_scope(second, 10):
            remember_page_record(second, 7, 10, right)
            assert cached_page_record(second, 7) is right
            assert cached_page_record(first, 7) is None
        assert cached_page_record(first, 7) is left
    assert cached_page_record(first, 7) is None


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_scope_releases_records_after_failure_or_control_flow(failure):
    connection = SimpleNamespace(in_transaction=True)
    with pytest.raises(failure):
        with page_read_scope(connection, 10):
            remember_page_record(connection, 7, 10, object())
            raise failure()
    assert cached_page_record(connection, 7) is None


def test_transaction_end_discards_records_even_before_scope_exits():
    connection = SimpleNamespace(in_transaction=True)
    with page_read_scope(connection, 10):
        remember_page_record(connection, 7, 10, object())
        discard_page_records(connection)
        assert cached_page_record(connection, 7) is None
        with page_read_scope(connection, 20):
            remember_page_record(connection, 15, 15, object())
        assert cached_page_record(connection, 15) is None


def test_independent_proof_phase_restarts_reads_without_widening_committed_range():
    connection = SimpleNamespace(in_transaction=True)
    prior, checked = object(), object()
    with page_read_scope(connection, 10):
        remember_page_record(connection, 7, 10, prior)
        clear_page_records(connection)
        assert cached_page_record(connection, 7) is None
        remember_page_record(connection, 7, 10, checked)
        remember_page_record(connection, 11, 11, object())
        assert cached_page_record(connection, 7) is checked
        assert cached_page_record(connection, 11) is None
