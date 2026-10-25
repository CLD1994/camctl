"""报告有限事实和显式增长范围；不连接数据库或创建文件。"""
from unittest.mock import create_autospec
from types import SimpleNamespace
from contextlib import contextmanager
import sqlite3

import pytest

from camctl.contracts import public_projection
from camctl.contracts.values import ConsistencyError
from camctl.history import report_action
from camctl.history.bounded import _OwnerBranch
from camctl.persistence.transaction import encode_json_value


def registration():
    return {
        'projections': {'action': {'root_table': 'actions',
            'relations': ['sets', 'members', 'gaps', 'scope'],
            'assertions': ['finite', 'error', 'coverage'], 'fields': {}}},
        'relations': {
            'sets': {'from': 'actions.id', 'to': 'segment_sets.action_id'},
            'members': {'from': 'segment_sets.id', 'to': 'segment_members.set_id',
                        'read_phase': ['segment_error']},
            'gaps': {'from': 'segment_sets.id', 'to': 'segment_gaps.set_id',
                     'read_phase': ['segment_error']},
            'scope': {'from': 'segment_sets.id', 'to': 'segment_scope_members.set_id',
                      'read_phase': ['segment_error', 'video_coverage']},
            'scope_member': {'from': 'segment_scope_members.member_id', 'to': 'segment_members.id',
                             'read_phase': ['video_coverage']},
        },
        'assertions': {
            'finite': {'columns': ['segment_sets.assessment_json']},
            'error': {'columns': ['segment_members.file_id', 'segment_gaps.gap_no'],
                      'read_phase': ['segment_error']},
            'coverage': {'columns': ['segment_members.file_id', 'segment_scope_members.ordinal'],
                         'read_phase': ['video_coverage']},
        },
        'read_expansions': {
            'segment_error': {'trigger': {'kind': 'registered_error',
                'details_schema': '#/$defs/segment_error_header'},
                'relations': ['sets', 'members', 'gaps', 'scope'],
                'assertion': 'error', 'members': {'relation': 'members', 'selection': 'all'}},
            'video_coverage': {'trigger': {'kind': 'projection', 'projection': 'video_coverage'},
                'relations': ['sets', 'scope', 'scope_member'], 'assertion': 'coverage',
                'members': {'relation': 'scope_member', 'selection': 'referenced'}},
        },
    }


@pytest.fixture(autouse=True)
def registered_scope(monkeypatch):
    monkeypatch.setattr(report_action, '_dependencies', create_autospec(
        report_action._dependencies, return_value=registration()))


def test_forward_coverage_keeps_s_member_referenced_by_scope_created_before_h(monkeypatch):
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.history import snapshot_stream
    from camctl.persistence.repositories.history import HistoryRepository
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.entity, handle.action_id, handle.phase = 'action', 9, 'video_coverage'
    handle.boundary, handle.batch_size, handle._forward = HistoryBoundary(5, 50), 2, True
    handle.required_tables = frozenset({'actions', 'segment_sets', 'segment_members', 'segment_scope_members'})
    handle._set_foreign_keys = {'segment_members': 'set_id', 'segment_scope_members': 'set_id'}
    handle._published_identity = None
    handle.seed = SimpleNamespace(ref=(1, 9), primary_table='actions', snapshot=SimpleNamespace(
        boundary=HistoryBoundary(3, 31), position=(31, 3)))
    selected = set()
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    def local_execute(sql, args=()):
        if sql.startswith('INSERT OR IGNORE'):
            selected.add(args[0])
        else:
            assert sql.startswith('CREATE TABLE selected_members')
        return create_autospec(sqlite3.Cursor, instance=True)
    handle._db.execute.side_effect = local_execute
    handle._one = create_autospec(handle._one, side_effect=lambda sql, args:
        (1,) if args[0] in selected else None)
    connection = create_autospec(sqlite3.Connection, instance=True)
    def source_execute(sql, args=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith('SELECT id FROM segment_sets'):
            assert args == (9, 50)
            cursor.fetchall.return_value = [(4,)]
        else:
            assert sql.startswith('SELECT id,member_id FROM segment_scope_members')
            assert 'created_event_id<=?' in sql
            assert args[:2] == (4, 50)
            # member 29 已存在于S；引用它的scope在S后、H前才创建。
            cursor.fetchall.return_value = [(41, 29)] if args[2] == 0 else []
        return cursor
    connection.execute.side_effect = source_execute
    handle.repository = create_autospec(HistoryRepository, instance=True)
    @contextmanager
    def read_connection():
        yield connection
    handle.repository._read_connection.side_effect = read_connection
    rows = {}
    handle._put = create_autospec(handle._put, side_effect=lambda table, identity, values:
        rows.__setitem__((table, identity), values))
    handle._get = create_autospec(handle._get, side_effect=lambda table, identity: rows.get((table, identity)))
    records = [
        {'format_version': 1, 'entity_type': 1, 'entity_id': 9, 'boundary_event_id': 31, 'change_count': 3},
        {'table': 'actions', 'row': {'id': 9, 'last_event_id': 31, 'change_count': 3}},
        {'table': 'segment_members', 'row': {'id': 29, 'set_id': 4, 'created_event_id': 20}},
        {'table': 'segment_members', 'row': {'id': 30, 'set_id': 4, 'created_event_id': 20}},
        {'table': 'segment_sets', 'row': {'id': 4, 'action_id': 9, 'created_event_id': 15, 'published_event_id': None}},
    ]
    monkeypatch.setattr(snapshot_stream, '_records', create_autospec(
        snapshot_stream._records, return_value=(record for record in records)))
    monkeypatch.setattr(snapshot_stream, '_chunks', create_autospec(snapshot_stream._chunks, return_value=()))
    monkeypatch.setattr(snapshot_stream, 'load_event_registry', create_autospec(
        snapshot_stream.load_event_registry, return_value={'tables': {
            'actions': {'derived': ['id', 'last_event_id', 'change_count']},
            'segment_members': {'derived': ['id'], 'immutable': ['set_id', 'created_event_id']},
            'segment_sets': {'derived': ['id'], 'immutable': ['action_id', 'created_event_id'],
                             'write_once': ['published_event_id']}}}))
    handle._prepare_forward()
    handle._load_snapshot()
    assert ('segment_members', 29) in rows
    assert ('segment_members', 30) not in rows
    assert rows[('segment_sets', 4)]['published_event_id'] is None
    assert not handle._selected_row('segment_sets', {'id': 5})
    from camctl.history import bounded
    from camctl.history.events import EventEnvelope, RowChange, RowImage
    handle._forward_count = 4
    monkeypatch.setattr(bounded, 'business_columns', create_autospec(
        bounded.business_columns, return_value=frozenset({'published_event_id'})))
    handle._forward_candidate = create_autospec(handle._forward_candidate,
        side_effect=lambda table, identity: table == 'segment_sets' and identity == 4)
    handle._apply(EventEnvelope(45, 4, 1, 1, 0, 1, None, 1, {}, (
        RowChange('segment_sets', 4, RowImage(True, {'published_event_id': None}), RowImage(True, {'published_event_id': 45})),
        RowChange('segment_sets', 5, RowImage(False, {}), RowImage(True, {'published_event_id': 70})),
        RowChange('operation_attempts', 11, RowImage(False, {}), RowImage(True, {'reply_json': {}})),
    )), True)
    assert rows[('segment_sets', 4)]['published_event_id'] == 45
    assert ('segment_sets', 5) not in rows and ('operation_attempts', 11) not in rows


def test_finite_scope_does_not_reintroduce_growth_through_relation_endpoints():
    assert public_projection.projection_read_tables('action', phase='finite',
        _registry=registration()) == frozenset({'actions', 'segment_sets'})


def test_full_dependencies_and_explicit_coverage_keep_their_distinct_scope():
    registry = registration()
    assert public_projection.projection_read_tables('action', _registry=registry) == frozenset({
        'actions', 'segment_sets', 'segment_members', 'segment_gaps', 'segment_scope_members'})
    assert public_projection.projection_read_tables('action', phase='video_coverage',
        _registry=registry) == frozenset({
        'actions', 'segment_sets', 'segment_members', 'segment_scope_members'})


@pytest.mark.parametrize('port,args', [('row', ('segment_members', 29)),
    ('related_rows', ('segment_gaps',)), ('read_page', ('segment_scope_members',))])
def test_unread_growth_is_rejected_instead_of_returning_an_empty_collection(monkeypatch, port, args):
    from camctl.history import bounded
    monkeypatch.setattr(bounded, 'load_event_registry', create_autospec(
        bounded.load_event_registry, return_value={'tables': dict.fromkeys(
            ['segment_members', 'segment_gaps', 'segment_scope_members'])}))
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.required_tables = frozenset({'actions', 'segment_sets'})
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    handle._db.execute.return_value.fetchone.return_value = None
    handle._db.execute.return_value.fetchall.return_value = []
    handle._db.execute.return_value.fetchmany.return_value = []
    handle._closed, handle.ready = False, True
    with pytest.raises(ConsistencyError, match='未读取'):
        result = getattr(handle, port)(*args)
        if port == 'related_rows':
            list(result)
    handle._db.execute.assert_not_called()


def test_finite_set_validation_keeps_header_without_comparing_unread_members():
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.phase = 'finite'
    handle.action_id, handle.batch_size = 9, 128
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    handle._db.execute.return_value = cursor
    cursor.fetchall.side_effect = [[(4, encode_json_value({
        'id': 4, 'member_count': 500, 'facts_json': None, 'assessment_json': None}))], []]
    handle._one = create_autospec(handle._one, return_value=(0, None, None))
    handle._validate_sets()
    handle._one.assert_not_called()


@pytest.mark.parametrize('profile,raises', [('complete', True), ('video_coverage', False)])
def test_full_default_counts_every_member_while_coverage_only_counts_scope(profile, raises):
    from camctl.history.bounded import SegmentHistoryHandle
    cls = SegmentHistoryHandle if profile == 'complete' else report_action.ReportHistoryHandle
    handle = cls.__new__(cls)
    handle.phase, handle.action_id, handle.batch_size = profile, 9, 128
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    handle._db.execute.return_value = cursor
    cursor.fetchall.side_effect = [[(4, encode_json_value({
        'id': 4, 'member_count': 500, 'facts_json': None, 'assessment_json': None}))], []]
    handle._one = create_autospec(handle._one, return_value=(2, 1, 2))
    if raises:
        with pytest.raises(ConsistencyError, match='完整成员数量'):
            handle._validate_sets()
    else:
        handle._validate_sets()
        assert [call.args[1][0] for call in handle._one.call_args_list] == ['segment_scope_members']


def test_coverage_candidates_follow_scope_member_reference_not_all_set_members():
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.phase, handle.action_id = 'video_coverage', 9
    from camctl.contracts.history_values import HistoryBoundary
    handle.boundary = HistoryBoundary(5, 50)
    handle._published_identity = 4
    handle._set_foreign_keys = {'segment_members': 'set_id', 'segment_scope_members': 'set_id'}
    handle.seed = SimpleNamespace(ref=(1, 9))
    sql, parameters = handle._candidate_query('segment_members', (
        _OwnerBranch(('segment_members', 'segment_sets'), ('set_id',), (), 'action_id'),))
    assert 'segment_scope_members' in sql
    assert 'member_id' in sql
    assert parameters[-2:] == (4, 50)


def test_coverage_snapshot_does_not_select_non_scope_members():
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.phase = 'video_coverage'
    handle.required_tables = frozenset({'segment_members'})
    handle._set_foreign_keys = {'segment_members': 'set_id'}
    handle._published_identity = 4
    handle._get = create_autospec(handle._get, return_value=None)
    assert not handle._selected_row('segment_members', {'id': 30, 'set_id': 4})


@pytest.mark.parametrize('same_ref,same_instance', [(False, True), (True, False)])
def test_growth_child_rejects_a_different_h_source_ref_or_instance(monkeypatch, same_ref, same_instance):
    from camctl.contracts.report_reads import ReportExpansionRequest
    from camctl.contracts.history_values import HistoryBoundary
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle._closed, handle.ready, handle._db = False, True, object()
    handle.entity, handle.action_id, handle.instance_id = 'action', 9, 'instance'
    handle.boundary = HistoryBoundary(5, 50)
    handle.batch_size, handle.event_batch_size, handle.repository = 32, 777, object()
    handle.active_result_ref = create_autospec(handle.active_result_ref, return_value=(9, 4, 1))
    child = create_autospec(report_action.ReportHistoryHandle, instance=True)
    child.instance_id = 'instance' if same_instance else 'other'
    child.boundary = handle.boundary
    child.active_result_ref.return_value = (9, 4 if same_ref else 5, 1)
    monkeypatch.setattr(report_action, 'open_report_history', create_autospec(
        report_action.open_report_history, return_value=child))
    with pytest.raises(ConsistencyError):
        handle.open_expansion(ReportExpansionRequest('video_coverage', 9))
    child.close.assert_called_once_with()
