"""普通原页端口使用已保存输入，不调用形成页的整组来源核验。"""
import sqlite3
from unittest.mock import create_autospec

from camctl.persistence import fact_reads
from camctl.persistence.repositories import result_pages


def test_saved_page_decodes_actual_event_without_replaying_page_formation(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    record = create_autospec(fact_reads.EventRecord, instance=True)
    record.event = object()
    saved, raw = object(), {'attempt_id': 2}
    read = create_autospec(fact_reads.read_event, return_value=record)
    decode = create_autospec(result_pages._decode, return_value=(saved, raw))
    formation = create_autospec(result_pages._verify_page_relations)
    monkeypatch.setattr(result_pages, 'read_event', read)
    monkeypatch.setattr(result_pages, '_decode', decode)
    monkeypatch.setattr(result_pages, '_verify_page_relations', formation)
    assert result_pages.read_saved_page(connection, 7) == (saved, raw)
    read.assert_called_once_with(connection, 7)
    decode.assert_called_once_with(record.event)
    formation.assert_not_called()


def test_ticket_lookup_does_not_reuse_a_missing_attempt_or_old_current_rows(monkeypatch):
    from copy import deepcopy
    import pytest
    from camctl.contracts.history_values import INITIAL_BOUNDARY
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.models import AttemptTicket

    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    cursor.fetchone.return_value = None
    rows = {
        ('operation_runs', 5): {'id': 5, 'kind': int(result_pages._RUN_KIND.CHECK_CAPTURE_RESULTS),
            'activity_id': 8, 'action_id': 3, 'responsibility_key': 'results/8'},
        ('operation_attempts', 9): {'id': 9, 'status': 1},
        ('actions', 3): {'id': 3},
        ('device_activities', 8): {'id': 8, 'action_id': 3},
    }
    read = create_autospec(result_pages.row_facts,
        side_effect=lambda db, table, identity: deepcopy(rows[(table, identity)]))
    monkeypatch.setattr(result_pages, 'row_facts', read)
    ticket = AttemptTicket(1, 'result', '8', 'results/8', 5)
    with fact_reads.operation_fact_scope(connection, INITIAL_BOUNDARY):
        with pytest.raises(ConsistencyError, match='原尝试不存在'):
            result_pages._load(connection, ticket)
        cursor.fetchone.return_value = (9,)
        assert result_pages._load(connection, ticket)[1]['status'] == 1
        rows[('operation_attempts', 9)]['status'] = 2
        assert result_pages._load(connection, ticket)[1]['status'] == 2
    assert connection.execute.call_count == 3
