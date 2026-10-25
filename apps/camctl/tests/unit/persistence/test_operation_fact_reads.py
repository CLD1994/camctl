"""内核C0固定事实跨判断和写入复用，当前行保持直读。"""
from importlib import import_module
from unittest.mock import create_autospec
import sqlite3

import pytest

from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.history.events import EventEnvelope
from camctl.persistence import transaction


@pytest.fixture
def database(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction, connection.total_changes = True, 0
    state = {'event': (2, 1, 1, 1, 0, 1, None, '{}'), 'current': 1, 'events': 0, 'ranges': 0, 'count': 3, 'output': None, 'failure': None, 'operation': None}
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith('SELECT id, first_event_id'):
            state['ranges'] += 1
            cursor.fetchone.return_value = (1, 1, state['count'])
        elif sql.startswith('SELECT COUNT(*)'):
            cursor.fetchone.return_value = (state['count'], 1, state['count'])
        elif 'FROM history_events WHERE id=' in sql:
            state['events'] += 1
            if state['failure'] is not None:
                raise state['failure']
            base = state['event']
            cursor.fetchone.return_value = None if base is None else (parameters[0], *base[1:])
        elif sql.startswith('SELECT id,last_event_id FROM history_transactions'):
            cursor.fetchone.return_value = (1, 3)
        elif sql.startswith('SELECT MAX(id) FROM history_events'):
            cursor.fetchone.return_value = (3,)
        elif sql.startswith('SELECT id,transaction_id') and 'WHERE transaction_id=' in sql:
            _, after, end, _ = parameters
            cursor.fetchall.return_value = [(identity, *state['event'][1:]) for identity in range(after+1, end+1)]
        elif 'WHERE id IN' in sql:
            cursor.fetchall.return_value = [(identity, *state['event'][1:]) for identity in sorted(parameters)]
        elif sql.startswith('SELECT id FROM history_transactions WHERE operation_key='):
            cursor.fetchone.return_value = state['operation']
        elif sql.startswith('SELECT id FROM outputs'):
            cursor.fetchone.return_value = state['output']
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
    monkeypatch.setattr(module, 'decode_event_row', create_autospec(
        module.decode_event_row, side_effect=lambda row: EventEnvelope(
            *row[:7], 1, {'nested': [1]}, ())))
    return module


def test_current_rows_are_direct_inside_operation_scope(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(0, 0)):
        assert transaction.row_facts(connection, 'sample', 1)['value'] == 1
        assert transaction.row_facts(connection, 'sample', 1)['value'] == 1
    assert connection.execute.call_count == 2


def test_committed_event_survives_independent_judgments_and_later_writes(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        first = facts.read_event(connection, 2)
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


def test_same_connection_nested_scope_cannot_expand_c0(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
        with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
            facts.read_event(connection, 2)
        with pytest.raises(ConsistencyError, match='改变原C0'):
            with facts.operation_fact_scope(connection, HistoryBoundary(1, 2)):
                pytest.fail('错误C0不能进入')
        facts.read_event(connection, 2)
    assert state['events'] == 1


def test_other_connection_is_independent_and_outer_scope_is_restored(database, facts):
    connection, state = database
    other = create_autospec(sqlite3.Connection, instance=True)
    other.in_transaction = True
    other.execute.side_effect = connection.execute.side_effect
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
        facts.read_event(other, 2)
        facts.read_event(other, 2)
        with facts.operation_fact_scope(other, HistoryBoundary(1, 3)):
            facts.read_event(other, 2)
            facts.read_event(other, 2)
        facts.read_event(connection, 2)
    assert state['events'] == 4


@pytest.mark.parametrize('ending', ['explicit', 'transaction_end', 'cancel'])
def test_scope_end_never_retains_facts_for_a_later_operation(database, facts, ending):
    connection, state = database
    try:
        with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
            facts.read_event(connection, 2)
            if ending == 'explicit':
                facts.discard_operation_facts(connection)
                assert facts.operation_fact_boundary(connection) is None
            elif ending == 'transaction_end':
                connection.in_transaction = False
                assert facts.operation_fact_boundary(connection) is None
            else:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    connection.in_transaction = True
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
    assert state['events'] == 2


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('读取失败'), KeyboardInterrupt()])
def test_sql_failure_is_original_and_does_not_register_a_fact(database, facts, failure):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        state['failure'] = failure
        with pytest.raises(type(failure)) as caught:
            facts.read_event(connection, 2)
        assert caught.value is failure
        state['failure'] = None
        facts.read_event(connection, 2)
    assert state['events'] == 2


def test_decode_failure_can_retry_the_same_original_event(database, facts):
    connection, state = database
    decode = facts.decode_event_row.side_effect
    failure = ConsistencyError('原事件不可解释')
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.decode_event_row.side_effect = failure
        with pytest.raises(ConsistencyError) as caught:
            facts.read_event(connection, 2)
        assert caught.value is failure
        facts.decode_event_row.side_effect = decode
        facts.read_event(connection, 2)
    assert state['events'] == 2


def test_event_budget_has_no_separate_three_page_limit(database, facts):
    connection, state = database
    state['count'] = 6
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 6)):
        for _ in range(2):
            for identity in range(1, 6):
                assert facts.read_event(connection, identity).event.event_id == identity
    assert state['events'] == 5


@pytest.mark.parametrize('budget', ['entries', 'bytes', 'oversized'])
def test_one_budget_bounds_raw_events_and_fixed_files_and_eviction_allows_reload(database, facts, monkeypatch, budget):
    connection, state = database
    if budget == 'entries':
        monkeypatch.setattr(facts, 'MAX_FACT_ENTRIES', 3)
        payload = 'x'
    else:
        monkeypatch.setattr(facts, 'MAX_FACT_BYTES', 4096)
        payload = 'x' * (3000 if budget == 'bytes' else 5000)
    loaded = []
    def read_file(identity):
        def load():
            loaded.append(identity)
            return {'id': identity, 'last_event_id': 2, 'payload': payload}
        return facts._reuse_fixed_file(connection, identity, 2, HistoryBoundary(1, 3), load)
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
        for identity in range(10, 14):
            read_file(identity)
            retained = facts._facts(connection)
            assert len(retained.entries) <= facts.MAX_FACT_ENTRIES
            assert retained.retained_bytes <= facts.MAX_FACT_BYTES
        read_file(10)
        assert loaded.count(10) == 2
        if budget == 'oversized':
            assert not any(key[0] == 'file' for key in retained.entries)


def test_current_missing_row_and_empty_relationship_become_visible_after_write(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
        state['current'] = None
        assert transaction.row_facts(connection, 'sample', 1)['value'] is None
        assert connection.execute('SELECT id FROM outputs WHERE source_action_id=?', (1,)).fetchone() is None
        connection.total_changes += 1
        state['current'], state['output'] = 7, (9,)
        assert transaction.row_facts(connection, 'sample', 1)['value'] == 7
        assert connection.execute('SELECT id FROM outputs WHERE source_action_id=?', (1,)).fetchone() == (9,)
        facts.read_event(connection, 2)
    assert state['events'] == 1


def test_complete_group_batch_reuses_individual_event_facts(database, facts):
    connection, state = database
    group = TransactionRange(1, 1, 3)
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        first = tuple(facts.iter_transaction_events(connection, group, batch_size=2))
        second = tuple(facts.iter_transaction_events(connection, group, batch_size=2))
        assert [item.event.event_id for item in first] == [1, 2, 3]
        assert first == second
        assert facts.read_event(connection, 2) == first[1]
    event_batches = [call for call in connection.execute.call_args_list
                     if 'WHERE transaction_id=? AND id>?' in call.args[0]]
    assert len(event_batches) == 2
    assert state['events'] == 0


def test_batch_port_loads_missing_identities_once_and_preserves_requested_order(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        facts.read_event(connection, 2)
        records = facts.read_events(connection, (3, 2, 1))
        assert [item.event.event_id for item in records] == [3, 2, 1]
        assert [item.event.event_id for item in facts.read_events(connection, (1, 2, 3))] == [1, 2, 3]
    batches = [call for call in connection.execute.call_args_list if 'WHERE id IN' in call.args[0]]
    assert len(batches) == 1 and batches[0].args[1] == (3, 1)
    assert state['events'] == 1


def test_group_prefix_stops_before_append_body(database, facts):
    connection, _ = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        assert [record.event.event_id for record in facts.iter_transaction_events(
            connection, TransactionRange(1, 1, 3), end_event_id=1)] == [1]
    body_queries = [call for call in connection.execute.call_args_list if 'WHERE transaction_id=? AND id>?' in call.args[0]]
    assert len(body_queries) == 1 and body_queries[0].args[1] == (1, 0, 1, 128)


def test_operation_key_lookup_does_not_preserve_absence_or_admit_future_group(database, facts):
    connection, state = database
    with facts.operation_fact_scope(connection, HistoryBoundary(1, 3)):
        assert facts.read_operation_transaction(connection, 'key') is None
        state['operation'] = (1,)
        assert facts.read_operation_transaction(connection, 'key') == TransactionRange(1, 1, 3)
        state['operation'] = (2,)
        with pytest.raises(ConsistencyError, match='C0'):
            facts.read_operation_transaction(connection, 'key')
