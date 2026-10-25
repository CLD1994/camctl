"""内核C0固定事实跨判断和写入复用，当前行保持直读。"""
from importlib import import_module
from unittest.mock import create_autospec
import sqlite3

import pytest

from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.history.events import EventEnvelope
from camctl.persistence import transaction
from camctl.persistence.read_reuse import read_reuse_scope, clear_read_reuse


@pytest.fixture
def database(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction, connection.total_changes = True, 0
    state = {'event': (2, 1, 1, 1, 0, 1, None, '{}'), 'current': 1, 'events': 0, 'ranges': 0}
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith('SELECT id, first_event_id'):
            state['ranges'] += 1
            cursor.fetchone.return_value = (1, 1, 3)
        elif sql.startswith('SELECT COUNT(*)'):
            cursor.fetchone.return_value = (3, 1, 3)
        elif 'FROM history_events WHERE id=' in sql:
            state['events'] += 1
            cursor.fetchone.return_value = state['event']
        elif sql.startswith('SELECT id,last_event_id FROM history_transactions'):
            cursor.fetchone.return_value = (1, 3)
        elif sql.startswith('SELECT MAX(id) FROM history_events'):
            cursor.fetchone.return_value = (3,)
        elif sql.startswith('SELECT * FROM sample'):
            cursor.fetchone.return_value = (1, state['current'])
            cursor.description = (('id',), ('value',))
        else:
            raise AssertionError(sql)
        return cursor
    connection.execute.side_effect = execute
    monkeypatch.setattr(transaction, 'json_columns', create_autospec(transaction.json_columns, return_value={}))
    return connection, state


@pytest.fixture
def facts(monkeypatch):
    module = import_module('camctl.persistence.fact_reads')
    event = EventEnvelope(2, 1, 1, 1, 0, 1, None, 1, {'nested': [1]}, ())
    monkeypatch.setattr(module, 'decode_event_row', create_autospec(
        module.decode_event_row, return_value=event))
    return module


def test_current_rows_are_direct_even_inside_legacy_reuse_scope(database):
    connection, state = database
    with read_reuse_scope(connection):
        assert transaction.row_facts(connection, 'sample', 1)['value'] == 1
        assert transaction.row_facts(connection, 'sample', 1)['value'] == 1
    assert connection.execute.call_count == 2


def test_committed_event_survives_stage_clear_and_later_writes(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        first = facts.read_event(connection, 2)
        clear_read_reuse(connection)
        connection.total_changes += 9
        second = facts.read_event(connection, 2)
        assert second.event == first.event
        assert second.transaction == TransactionRange(1, 1, 3)
        assert second.raw == state['event']
    assert state['events'] == 1
    assert state['ranges'] == 1


def test_event_value_isolation_and_scope_exit(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2).event.evidence['nested'].append(9)
        assert facts.read_event(connection, 2).event.evidence['nested'] == [1]
    facts.read_event(connection, 2)
    assert state['events'] == 2


@pytest.mark.parametrize('identity', [True, 0, 4])
def test_invalid_or_future_event_is_not_a_committed_fact(database, facts, identity):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        with pytest.raises((ConsistencyError, ValueError, TypeError)):
            facts.read_event(connection, identity)
    assert state['events'] == 0


def test_failed_event_read_is_not_cached(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        state['event'] = None
        with pytest.raises(ConsistencyError):
            facts.read_event(connection, 2)
        state['event'] = (2, 1, 1, 1, 0, 1, None, '{}')
        assert facts.read_event(connection, 2).event.event_id == 2
    assert state['events'] == 2


def test_fixed_file_is_separate_from_mutable_rows_and_returns_owned_values(database, facts):
    connection, _ = database
    source = create_autospec(transaction.row_facts, return_value={'id': 9, 'last_event_id': 2, 'nested': [1]})
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        first = facts._reuse_fixed_file(connection, 9, 2, HistoryBoundary(1, 3), lambda: source(connection, 'device_files', 9))
        first['nested'].append(9)
        connection.total_changes += 1
        second = facts._reuse_fixed_file(connection, 9, 2, HistoryBoundary(1, 3), lambda: source(connection, 'device_files', 9))
        assert second['nested'] == [1]
    source.assert_called_once()


@pytest.mark.parametrize('opened,reads', [(False, 2), (True, 1)])
def test_readonly_scope_belongs_only_to_transaction_owner(database, facts, opened, reads):
    connection, state = database
    with facts.opened_operation_fact_scope(connection, opened):
        facts.read_event(connection, 2)
        facts.read_event(connection, 2)
    assert state['events'] == reads
    assert facts.operation_fact_boundary(connection) is None
