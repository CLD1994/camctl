"""当前行仅在实际前序未变化时共享；固定历史限定内核C0。"""
from contextlib import nullcontext
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence import transaction, fact_reads


@pytest.fixture
def database(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    connection.total_changes = 0
    state = {"row": (1, '{"values":[1.0000000000000001]}'),
             "range": (2, 3, 4), "previous": (2,), "members": (2, 3, 4),
             "cursors": [], "failure": None, "failure_prefix": ""}
    monkeypatch.setattr(transaction, "json_columns", create_autospec(
        transaction.json_columns, return_value={"sample": frozenset({"payload_json"})}))

    def execute(statement, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        state["cursors"].append(cursor)
        if statement.startswith("SELECT *"):
            cursor.description = (("id", None, None, None, None, None, None),
                                  ("payload_json", None, None, None, None, None, None))
            value = state["row"]
        elif statement.startswith("SELECT id, first_event_id"):
            value = state["range"]
        elif statement.startswith("SELECT last_event_id"):
            value = state["previous"]
        elif statement.startswith("SELECT COUNT(*)"):
            value = state["members"]
        else:
            raise AssertionError(f"未声明的 SQL: {statement}")
        cursor.fetchone.return_value = value
        if state["failure"] is not None and statement.startswith(state["failure_prefix"]):
            cursor.fetchone.side_effect = state["failure"]
        return cursor

    connection.execute.side_effect = execute
    return connection, state



@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('scoped,queries', [(False, 2), (True, 1)])
def test_current_row_and_missing_result_share_only_kernel_owned_scope(database, missing, scoped, queries):
    connection, state = database
    if missing:
        state['row'] = None
    with (fact_reads.operation_fact_scope(connection, HistoryBoundary(0, 0))
          if scoped else nullcontext()):
        first = transaction.row_facts(connection, 'sample', 1)
        second = transaction.row_facts(connection, 'sample', 1)
    assert first == second == (None if missing else {
        'id': 1, 'payload_json': {'values': [Decimal('1.0000000000000001')]}})
    assert connection.execute.call_count == queries
    for cursor in state['cursors']:
        cursor.close.assert_called_once_with()


def test_current_rows_preserve_precise_json_and_return_independent_values(database):
    connection, _ = database
    first = transaction.row_facts(connection, 'sample', 1)
    first['payload_json']['values'].append(2)
    assert transaction.row_facts(connection, 'sample', 1)['payload_json']['values'] == [Decimal('1.0000000000000001')]


def test_missing_current_row_becomes_visible_after_write(database):
    connection, state = database
    with fact_reads.operation_fact_scope(connection, HistoryBoundary(0, 0)):
        state['row'] = None
        assert transaction.row_facts(connection, 'sample', 1) is None
        connection.total_changes += 1
        state['row'] = (1, '{}')
        assert transaction.row_facts(connection, 'sample', 1) == {'id': 1, 'payload_json': {}}


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('读取失败'), KeyboardInterrupt()])
def test_current_row_query_error_is_original_and_cursor_closes(database, failure):
    connection, state = database
    state['failure'] = failure
    with pytest.raises(type(failure)) as caught:
        transaction.row_facts(connection, 'sample', 1)
    assert caught.value is failure
    state['failure'] = None
    assert transaction.row_facts(connection, 'sample', 1)['id'] == 1
    for cursor in state['cursors']:
        cursor.close.assert_called_once_with()


def test_current_row_decode_error_retries_actual_input(database):
    connection, state = database
    state['row'] = (1, '{"value":1,"value":2}')
    with pytest.raises(ConsistencyError):
        transaction.row_facts(connection, 'sample', 1)
    state['row'] = (1, '{"value":2}')
    assert transaction.row_facts(connection, 'sample', 1)['payload_json'] == {'value': 2}


@pytest.mark.parametrize('provided,queries', [(False, 6), (True, 4)])
def test_transaction_range_without_scope_is_verified_each_time(database, provided, queries):
    connection, _ = database
    validated = TransactionRange(2, 3, 4) if provided else None
    assert transaction.read_transaction_range(connection, 2, validated) == TransactionRange(2, 3, 4)
    assert transaction.read_transaction_range(connection, 2, validated) == TransactionRange(2, 3, 4)
    assert connection.execute.call_count == queries


def test_provided_range_never_replaces_c0_full_membership_proof(database):
    connection, state = database
    state['members'] = (1, 3, 3)
    with pytest.raises(ConsistencyError, match='成员'):
        with fact_reads.operation_fact_scope(connection, HistoryBoundary(2, 4)):
            pytest.fail('缺失成员的C0不能进入')
    assert fact_reads.operation_fact_boundary(connection) is None


def test_scoped_transaction_uses_actual_complete_boundary_and_rejects_different_claim(database):
    connection, _ = database
    with fact_reads.operation_fact_scope(connection, HistoryBoundary(2, 4)):
        assert transaction.read_transaction_range(connection, 2, TransactionRange(2, 3, 4)) == TransactionRange(2, 3, 4)
        with pytest.raises(ConsistencyError, match='范围发生变化'):
            transaction.read_transaction_range(connection, 2, TransactionRange(2, 3, 5))
    assert connection.execute.call_count == 3


@pytest.mark.parametrize('field,value', [
    ('range', None), ('range', (True, 3, 4)), ('range', (2, 4, 3)),
    ('range', (3, 3, 4)), ('previous', None), ('previous', (1,)),
    ('members', (1, 3, 3)),
])
def test_bad_complete_transaction_can_retry_after_failure(database, field, value):
    connection, state = database
    original = state[field]
    state[field] = value
    with pytest.raises(ConsistencyError):
        transaction.read_transaction_range(connection, 2)
    state[field] = original
    assert transaction.read_transaction_range(connection, 2) == TransactionRange(2, 3, 4)


@pytest.mark.parametrize('prefix', ['SELECT id, first_event_id', 'SELECT last_event_id', 'SELECT COUNT(*)'])
def test_transaction_sql_error_keeps_original_exception_and_closes_cursor(database, prefix):
    connection, state = database
    error = sqlite3.OperationalError('范围读取失败')
    state['failure'], state['failure_prefix'] = error, prefix
    with pytest.raises(sqlite3.OperationalError) as caught:
        transaction.read_transaction_range(connection, 2)
    assert caught.value is error
    state['failure'] = None
    assert transaction.read_transaction_range(connection, 2) == TransactionRange(2, 3, 4)
    for cursor in state['cursors']:
        cursor.close.assert_called_once_with()
