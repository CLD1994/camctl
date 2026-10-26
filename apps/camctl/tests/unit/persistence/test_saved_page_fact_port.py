"""普通原页端口使用已保存输入，不调用形成页的整组来源核验。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.persistence import fact_reads
from camctl.persistence.repositories import result_pages


def test_saved_page_decodes_actual_event_without_replaying_page_formation(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    record = create_autospec(fact_reads.EventRecord, instance=True)
    record.event = SimpleNamespace(evidence={'result_page': {'run_id': 5}})
    run = {'id': 5, 'activity_id': 8, 'kind': int(result_pages._RUN_KIND.CHECK_CAPTURE_RESULTS),
        'responsibility_key': 'results/8'}
    saved, raw = object(), {'attempt_id': 2}
    read = create_autospec(fact_reads.read_event, return_value=record)
    decode = create_autospec(result_pages._decode, return_value=(saved, raw))
    formation = create_autospec(result_pages._verify_page_relations)
    monkeypatch.setattr(result_pages, 'read_event', read)
    monkeypatch.setattr(result_pages, 'row_facts', create_autospec(result_pages.row_facts, return_value=run))
    monkeypatch.setattr(result_pages, '_decode', decode)
    monkeypatch.setattr(result_pages, '_verify_page_relations', formation)
    assert result_pages.read_saved_page(connection, 7) == (saved, raw)
    read.assert_called_once_with(connection, 7)
    decode.assert_called_once_with(record.event, run=run)
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


def test_saved_scope_page_restores_original_ticket_with_unknown_metadata(monkeypatch):
    from camctl.contracts.enums import enum_for
    from camctl.contracts.history_values import TransactionRange
    from camctl.devices.evidence import DeviceObservation
    from camctl.operations.models import AttemptTicket, CallOutcome, EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.result_format import result_document
    from camctl.persistence.transaction import event_envelope, update_change

    connection = create_autospec(sqlite3.Connection, instance=True)
    run = {'id': 5, 'activity_id': 8, 'kind': int(result_pages._RUN_KIND.RESOLVE_OUTPUT_SCOPE),
        'responsibility_key': 'scope/8'}
    outcome = CallOutcome(settlement=Settlement(SettlementBasis.OBSERVED,
        EvidenceValue('result_list_returned', 1, {})), observations=(DeviceObservation('result_files_listed', 2,
        dict(activity_id='8', entries=[dict(identity='/DCIM/a.MP4',
            locator={'path': '/DCIM/a.MP4'}, complete=False, size_bytes=None)],
            cursor=None, next_cursor=None, set_finalized=True, completion_evidence=None)),))
    raw = dict(run_id=5, attempt_no=1, activity_id=8, page_no=1, cursor=None,
        outcome=dict(status=int(enum_for('operation_attempts.status').SUCCEEDED),
            effect_state=int(enum_for('operation_attempts.effect_state').UNKNOWN),
            error=None, result=result_document(outcome)), baseline_position=None,
        last_identity='/DCIM/a.MP4', file_ids=[['/DCIM/a.MP4', 3]])
    event = event_envelope(7, 1, result_pages._TYPE, result_pages._APPEND,
        (update_change('operation_attempts', 9,
            dict(result_first_page_event_id=None, result_last_page_event_id=None),
            dict(result_first_page_event_id=7, result_last_page_event_id=7)),),
        1_750_000_000_000_000, {'attempt_id': 9, 'result_page': raw})
    monkeypatch.setattr(result_pages, 'row_facts', create_autospec(result_pages.row_facts, return_value=run))
    monkeypatch.setattr(result_pages, 'read_event', create_autospec(result_pages.read_event,
        return_value=SimpleNamespace(event=event, transaction=TransactionRange(1, 7, 8))))

    saved, _ = result_pages.read_saved_page(connection, 7)

    assert saved.ref.ticket == AttemptTicket(1, 'result', '8', 'scope/8', 5)
    assert saved.page.scan_complete and saved.page.set_finalized
    assert not saved.page.entries[0].complete and saved.page.entries[0].size_bytes is None


@pytest.mark.parametrize('kind,key,accepted', [
    ('CHECK_CAPTURE_RESULTS', 'results/8', True), ('RESOLVE_OUTPUT_SCOPE', 'scope/8', True),
    ('CHECK_CAPTURE_RESULTS', 'scope/8', False), ('RESOLVE_OUTPUT_SCOPE', 'results/8', False),
])
def test_result_read_scope_checks_original_responsibility(monkeypatch, kind, key, accepted):
    from camctl.contracts.pages import Page
    from camctl.contracts.values import ConsistencyError
    from camctl.history.reads import CaptureResultReadScope
    from camctl.operations.models import AttemptTicket
    from camctl.persistence import transaction
    from camctl.persistence.event_reads import SqlEventReads

    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    run = dict(id=5, action_id=3, activity_id=8, kind=int(result_pages._RUN_KIND[kind]),
        responsibility_key='scope/8' if kind == 'RESOLVE_OUTPUT_SCOPE' else 'results/8')
    monkeypatch.setattr(transaction, 'row_facts', create_autospec(transaction.row_facts, return_value=run))
    read = create_autospec(result_pages.read_pages, return_value=Page((), None))
    monkeypatch.setattr(result_pages, 'read_pages', read)
    reader = SqlEventReads(connection, CaptureResultReadScope(3, 8, 5, 1, 10))
    ticket = AttemptTicket(1, 'result', '8', key, 5)
    if accepted:
        assert reader.result_pages(ticket, None, 1) == Page((), None)
    else:
        with pytest.raises(ConsistencyError):
            reader.result_pages(ticket, None, 1)
        read.assert_not_called()
