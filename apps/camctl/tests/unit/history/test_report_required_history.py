"""报告恢复范围及选路；所有登记和存储端口由受约束替身隔离。"""
from dataclasses import replace
from contextlib import contextmanager
import re
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts import public_projection
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.contracts.history_values import TransactionRange
from camctl.history import snapshot_stream
from camctl.history.bounded import SegmentHistoryHandle
from camctl.persistence.repositories.history import _ObjectSeed, _SnapshotChoice

_REGISTRY_READER = snapshot_stream.load_event_registry


def _dependencies():
    return {
        'projections': {
            'action': {'root_table': 'actions', 'relations': ['processing'],
                'assertions': ['state'], 'fields': {
                    'result': {'value': {'op': 'project', 'projection': 'result'}},
                    'outputs': {'value': {'op': 'entities', 'entity': 'output'}}}},
            'result': {'root_table': 'actions', 'fields': {
                'failures': {'value': {'op': 'failure_union', 'branches': [
                    {'projection': 'failure', 'relations': ['selection_items'],
                     'identity': 'obtain_items.id'}]}}}},
            'failure': {'root_table': 'obtain_items', 'fields': {
                'error': {'value': {'op': 'registered_error',
                    'code_column': 'obtain_items.error_code',
                    'details_column': 'obtain_items.error_details_json'}}}},
            'output': {'root_table': 'outputs', 'fields': {
                'secret': {'value': {'op': 'read', 'column': 'operation_attempts.reply_json'}}}},
        },
        'assertions': {'state': {'columns': ['device_activities.completion_basis']}},
        'relations': {
            'processing': {'from': 'actions.id', 'to': 'recording_processing.action_id'},
            'selection_items': {'from': 'obtain_source_selections.id', 'to': 'obtain_items.selection_id'},
        },
    }


def test_projection_scope_closes_conditions_assertions_and_failure_relations(monkeypatch):
    monkeypatch.setattr(public_projection, '_dependencies', create_autospec(
        public_projection._dependencies, return_value=_dependencies()))
    assert public_projection.projection_read_tables('action') == frozenset({
        'actions', 'recording_processing', 'device_activities',
        'obtain_source_selections', 'obtain_items'})


def test_projection_scope_stops_at_independent_entities(monkeypatch):
    monkeypatch.setattr(public_projection, '_dependencies', create_autospec(
        public_projection._dependencies, return_value=_dependencies()))
    assert 'operation_attempts' not in public_projection.projection_read_tables('action')


def test_projection_scope_rejects_unknown_projection(monkeypatch):
    monkeypatch.setattr(public_projection, '_dependencies', create_autospec(
        public_projection._dependencies, return_value=_dependencies()))
    with pytest.raises(public_projection.PublicProjectionError):
        public_projection.projection_read_tables('unknown')


def test_projection_scope_handles_recursive_projection_without_losing_dependencies(monkeypatch):
    document = _dependencies()
    document['projections']['result']['fields']['nested'] = {
        'value': {'op': 'project', 'projection': 'action'}}
    monkeypatch.setattr(public_projection, '_dependencies', create_autospec(
        public_projection._dependencies, return_value=document))
    assert 'device_activities' in public_projection.projection_read_tables('action')


def test_projection_scope_reads_json_path_as_column_of_its_original_table(monkeypatch):
    document = _dependencies()
    document['projections']['result']['fields']['items'] = {
        'value': {'op': 'read', 'column': 'segment_sets.assessment_json.default_scope'}}
    monkeypatch.setattr(public_projection, '_dependencies', create_autospec(
        public_projection._dependencies, return_value=document))
    tables = public_projection.projection_read_tables('action')
    assert 'segment_sets' in tables
    assert 'segment_sets.assessment_json' not in tables


def _seed(*, floor=5, head=10, snapshot_count=None, snapshot_at=30):
    choice = None if snapshot_count is None else _SnapshotChoice(
        7, HistoryBoundary(3, snapshot_at), (snapshot_at, snapshot_count), 1,
        TransactionRange(3, snapshot_at, snapshot_at))
    return _ObjectSeed((1, 9), 'actions', HistoryBoundary(10, 100),
        HistoryBoundary(5, 50), {}, (100, head), (50, floor), choice)


@pytest.mark.parametrize('seed,expected', [
    (_seed(floor=10, head=10, snapshot_count=10), 'current'),
    (_seed(snapshot_count=5, snapshot_at=50), 'snapshot'),
    (_seed(snapshot_count=None), 'current'),
    (_seed(snapshot_count=4), 'snapshot'),
    (_seed(floor=6, head=10, snapshot_count=1), 'current'),
    (_seed(floor=6, head=10, snapshot_count=2), 'current'),
])
def test_report_restore_chooses_original_object_count_path(seed, expected):
    from camctl.history.report_action import report_restore_strategy
    assert report_restore_strategy(seed) == expected


def _snapshot(monkeypatch, records):
    seed = _seed(snapshot_count=5, snapshot_at=50)
    handle = create_autospec(SegmentHistoryHandle, instance=True)
    handle.seed = seed
    handle.action_id = 9
    saved = {}
    handle._get.side_effect = lambda table, identity, source='rows': (
        {'id': 9, 'last_event_id': 50, 'change_count': 5}
        if source == 'candidates' and table == 'actions' else saved.get((table, identity)))
    handle._put.side_effect = lambda table, identity, values: saved.__setitem__((table, identity), values)
    monkeypatch.setattr(snapshot_stream, 'load_event_registry', create_autospec(
        _REGISTRY_READER, return_value={'tables': {
            'actions': {'immutable': [], 'write_once': [], 'derived': ['id', 'last_event_id', 'change_count']},
            'operation_attempts': {'immutable': [], 'write_once': [], 'derived': ['id']}}}))
    header = {'format_version': 1, 'entity_type': 1, 'entity_id': 9,
              'boundary_event_id': 50, 'change_count': 5}
    def record_stream(chunks):
        yield header
        yield from records
    monkeypatch.setattr(snapshot_stream, '_records', create_autospec(
        snapshot_stream._records, side_effect=record_stream))
    monkeypatch.setattr(snapshot_stream, '_chunks', create_autospec(
        snapshot_stream._chunks, return_value=()))
    return handle, saved


def test_projected_snapshot_ignores_unneeded_row_semantics_but_keeps_selected_rows(monkeypatch):
    handle, saved = _snapshot(monkeypatch, [
        {'table': 'actions', 'row': {'id': 9, 'last_event_id': 50, 'change_count': 5}},
        {'table': 'operation_attempts', 'row': {'id': 11, 'irrelevant': 'body'}},
    ])
    snapshot_stream.load_snapshot(handle, include_row=lambda table, values: table == 'actions')
    assert saved == {('actions', 9): {'id': 9, 'last_event_id': 50, 'change_count': 5}}


@pytest.mark.parametrize('record', [
    {'table': 'operation_attempts', 'row': {'id': True}},
    {'table': 'unknown', 'row': {'id': 10}},
    {'table': 'operation_attempts', 'row': {'id': 10}, 'extra': 1},
])
def test_projected_snapshot_rejects_bad_stream_structure_even_for_unneeded_rows(monkeypatch, record):
    handle, _ = _snapshot(monkeypatch, [record])
    with pytest.raises(ConsistencyError):
        snapshot_stream.load_snapshot(handle, include_row=lambda table, values: False)


def test_full_snapshot_rejects_incomplete_row_structure(monkeypatch):
    handle, _ = _snapshot(monkeypatch, [
        {'table': 'actions', 'row': {'id': 9, 'last_event_id': 50, 'change_count': 5}},
        {'table': 'operation_attempts', 'row': {'id': 11}},
    ])
    monkeypatch.setattr(snapshot_stream, 'load_event_registry', create_autospec(
        _REGISTRY_READER, return_value={'tables': {
            'actions': {'immutable': [], 'derived': ['id', 'last_event_id', 'change_count']},
            'operation_attempts': {'immutable': ['run_id'], 'derived': ['id']}}}))
    with pytest.raises(ConsistencyError):
        snapshot_stream.load_snapshot(handle)


def test_projected_snapshot_primary_metadata_follows_primary_table_derived_columns(monkeypatch):
    handle, saved = _snapshot(monkeypatch, [
        {'table': 'device_files', 'row': {'id': 9, 'last_event_id': 50}},
    ])
    handle.seed = replace(handle.seed, primary_table='device_files')
    handle._get.side_effect = lambda table, identity, source='rows': (
        {'id': 9, 'last_event_id': 50} if source == 'candidates' else saved.get((table, identity)))
    monkeypatch.setattr(snapshot_stream, 'load_event_registry', create_autospec(
        _REGISTRY_READER, return_value={'tables': {
            'device_files': {'immutable': [], 'write_once': [], 'derived': ['id', 'last_event_id']}}}))
    snapshot_stream.load_snapshot(handle, include_row=lambda table, values: True)
    assert saved == {('device_files', 9): {'id': 9, 'last_event_id': 50}}


@pytest.mark.parametrize('snapshot_value', [None, 7])
def test_projected_snapshot_uses_its_own_write_once_value_without_borrowing_c(monkeypatch, snapshot_value):
    values = {'id': 9, 'last_event_id': 50, 'change_count': 5, 'fixed': snapshot_value}
    handle, saved = _snapshot(monkeypatch, [{'table': 'actions', 'row': values}])
    handle._get.side_effect = lambda table, identity, source='rows': (
        {**values, 'fixed': 7} if source == 'candidates' else saved.get((table, identity)))
    monkeypatch.setattr(snapshot_stream, 'load_event_registry', create_autospec(
        _REGISTRY_READER, return_value={'tables': {
            'actions': {'immutable': [], 'write_once': ['fixed'],
                        'derived': ['id', 'last_event_id', 'change_count']}}}))
    snapshot_stream.load_snapshot(handle, include_row=lambda table, values: True)
    assert saved[('actions', 9)]['fixed'] == snapshot_value


@pytest.mark.parametrize('H,expected', [(50, {('actions', 9)}),
    (90, {('actions', 9), ('segment_sets', 4), ('segment_members', 29), ('segment_members', 30)})])
def test_report_candidates_exclude_internal_projection_and_other_or_future_sets(monkeypatch, H, expected):
    from camctl.history import report_action
    from camctl.persistence.repositories import history
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    handle.entity, handle.action_id = 'action', 9
    handle.seed = _seed()
    handle.boundary = HistoryBoundary(5, H)
    handle.batch_size, handle.event_batch_size = 2, 256
    handle.required_tables = frozenset({'actions', 'segment_sets', 'segment_members'})
    handle._set_foreign_keys = {'segment_members': 'set_id'}
    handle._published_identity = None
    handle.stats = {'projection_rows': 0, 'events': 0, 'candidate_pages': 0}
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    connection = create_autospec(sqlite3.Connection, instance=True)
    rows = {
        'actions': {9: {'id': 9}},
        'segment_sets': {3: {'id': 3, 'published_event_id': None},
                         4: {'id': 4, 'published_event_id': 80}},
        'segment_members': {28: {'id': 28, 'set_id': 3},
                            29: {'id': 29, 'set_id': 4}, 30: {'id': 30, 'set_id': 4}},
    }
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith('SELECT format_version'):
            cursor.fetchone.return_value = (2,)
        elif sql.startswith('SELECT id FROM segment_sets WHERE action_id'):
            cursor.fetchall.return_value = [(4,)] if H >= 80 else []
        else:
            table = re.search(r'FROM (actions|segment_sets|segment_members)\b', sql)[1]
            identities = list(rows[table])
            if table == 'segment_sets':
                identities = [identity for identity in identities if identity == handle._published_identity]
            elif table == 'segment_members':
                identities = [identity for identity in identities
                              if rows[table][identity]['set_id'] == handle._published_identity]
            if sql.startswith('SELECT COUNT'):
                cursor.fetchone.return_value = len(identities), max(identities, default=None)
            else:
                after, maximum, limit = parameters[-3:]
                cursor.fetchall.return_value = [(identity,) for identity in identities
                                                if after < identity <= maximum][:limit]
        return cursor
    connection.execute.side_effect = execute
    repo = create_autospec(history.HistoryRepository, instance=True)
    @contextmanager
    def read_connection():
        yield connection
    repo._read_connection.side_effect = read_connection
    repo._boundary.return_value = HistoryBoundary(10, 100)
    repo._object_seed.return_value = _seed()
    handle.repository = repo
    # 归属 SQL 由真实类型约束的分支替身提供，单元不访问包资源。
    from camctl.history.bounded import _OwnerBranch
    def owner_branches(table, entity):
        branch = create_autospec(_OwnerBranch, instance=True)
        branch.query.return_value = f'SELECT id FROM {table} WHERE id=?'
        return (branch,)
    monkeypatch.setattr(report_action, '_ownership_branches', create_autospec(
        report_action._ownership_branches, side_effect=owner_branches))
    monkeypatch.setattr(report_action, 'business_tables_for_format', create_autospec(
        report_action.business_tables_for_format, return_value=tuple(rows) + (
            'operation_runs', 'operation_attempts', 'result_page_files', 'baseline_read_calls')))
    reads = []
    def table_row(db, table, identity):
        reads.append((table, identity))
        return rows[table][identity]
    monkeypatch.setattr(history, '_table_row', create_autospec(history._table_row, side_effect=table_row))
    upper, head = handle._copy_candidates()
    assert set(reads) == expected
    assert handle.stats['projection_rows'] == len(expected)
    assert upper == HistoryBoundary(10, 100)
    assert head == (100, 10)


def test_report_constructor_preserves_configured_event_batch_across_base_initialization(monkeypatch):
    from camctl.history import report_action
    def initialize(self, repository, seed, batch_size, *, event_batch_size=256):
        self.event_batch_size = event_batch_size
        self.batch_size = batch_size
    monkeypatch.setattr(SegmentHistoryHandle, '__init__', create_autospec(
        SegmentHistoryHandle.__init__, side_effect=initialize))
    monkeypatch.setattr(report_action, 'foreign_key_targets', create_autospec(
        report_action.foreign_key_targets, return_value={}))
    handle = report_action.ReportHistoryHandle(None, _seed(), entity='action',
        tables={'actions'}, event_batch_size=731, row_batch_size=32)
    assert handle.event_batch_size == 731
    assert handle.batch_size == 32


@pytest.mark.parametrize('forward', [True, False])
def test_selected_row_replay_does_not_restore_unselected_internal_projection(monkeypatch, forward):
    from camctl.history import bounded, report_action
    from camctl.history.events import EventEnvelope, RowChange, RowImage
    from camctl.persistence.transaction import encode_json_value
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    candidate = {'id': 9, 'status': 2 if not forward else 1, 'fixed': 7 if not forward else None}
    rows = {('actions', 9): dict(candidate)}
    handle._forward_candidate = create_autospec(handle._forward_candidate,
        side_effect=lambda table, identity: (table, identity) == ('actions', 9))
    handle._one = create_autospec(handle._one, side_effect=lambda sql, key: (
        (encode_json_value(candidate), 100) if key == ('actions', 9) else None))
    handle._get = create_autospec(handle._get, side_effect=lambda table, identity: rows.get((table, identity)))
    handle._put = create_autospec(handle._put, side_effect=lambda table, identity, value:
        rows.__setitem__((table, identity), value))
    monkeypatch.setattr(bounded, 'business_columns', create_autospec(
        bounded.business_columns, return_value=frozenset({'status', 'fixed'})))
    event = EventEnvelope(90, 9, 1, 1, 0, 1, 1, 1, {}, (
        RowChange('actions', 9, RowImage(True, {'status': 1, 'fixed': None}),
                  RowImage(True, {'status': 2, 'fixed': 7})),
        RowChange('operation_attempts', 20, RowImage(False, {}), RowImage(True, {'reply_json': {}})),
    ))
    handle._apply(event, forward)
    assert rows == {('actions', 9): {'id': 9, 'status': 2 if forward else 1, 'fixed': 7 if forward else None}}


def test_reverse_creation_removes_future_required_row(monkeypatch):
    from camctl.history import bounded, report_action
    from camctl.history.events import EventEnvelope, RowChange, RowImage
    from camctl.persistence.transaction import encode_json_value
    handle = report_action.ReportHistoryHandle.__new__(report_action.ReportHistoryHandle)
    rows = {('actions', 9): {'id': 9, 'status': 1}}
    handle._one = create_autospec(handle._one, return_value=(encode_json_value(rows[('actions', 9)]), 100))
    handle._get = create_autospec(handle._get, side_effect=lambda table, identity: rows.get((table, identity)))
    handle._db = create_autospec(sqlite3.Connection, instance=True)
    def execute(sql, key):
        assert sql.startswith('DELETE FROM rows')
        rows.pop(key)
        return create_autospec(sqlite3.Cursor, instance=True)
    handle._db.execute.side_effect = execute
    monkeypatch.setattr(bounded, 'business_columns', create_autospec(
        bounded.business_columns, return_value=frozenset({'status'})))
    event = EventEnvelope(90, 9, 1, 1, 0, 1, 1, 1, {}, (
        RowChange('actions', 9, RowImage(False, {}), RowImage(True, {'status': 1})),))
    handle._apply(event, False)
    assert rows == {}


@pytest.mark.parametrize('event_batch', [True, 0, -1, 1.5])
def test_report_entry_rejects_invalid_event_batch_before_reading(event_batch):
    from camctl.history.report_action import open_report_history
    from camctl.persistence.repositories.history import HistoryRepository
    repository = create_autospec(HistoryRepository, instance=True)
    with pytest.raises(ValueError):
        open_report_history(repository, 'action', 9, HistoryBoundary(5, 50), event_batch_size=event_batch)
    repository._read_connection.assert_not_called()


@pytest.mark.parametrize('error', [ConsistencyError('bad saved fact'), ValueError('bad row'),
                                  sqlite3.OperationalError('read failed')])
def test_report_entry_releases_handle_and_preserves_error_classification(monkeypatch, error):
    from camctl.history import report_action
    from camctl.persistence.repositories.history import HistoryRepository
    repository = create_autospec(HistoryRepository, instance=True)
    repository._object_seed.return_value = _seed()
    constructor = create_autospec(report_action.ReportHistoryHandle)
    handle = constructor.return_value
    handle.restore.side_effect = error
    monkeypatch.setattr(report_action, 'ReportHistoryHandle', constructor)
    monkeypatch.setattr(report_action, '_required_tables', create_autospec(
        report_action._required_tables, return_value=frozenset({'actions'})))
    expected = ConsistencyError if isinstance(error, ValueError) else type(error)
    with pytest.raises(expected) as caught:
        report_action.open_report_history(repository, 'action', 9, HistoryBoundary(5, 50))
    if isinstance(error, (ConsistencyError, sqlite3.Error)):
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error
    handle.close.assert_called_once_with()


def test_ordinary_report_entry_returns_only_owned_required_rows_and_closes(monkeypatch):
    from camctl.history import report_action
    from camctl.history.snapshots import SnapshotRow
    from camctl.persistence.repositories.history import HistoryRepository
    repository = create_autospec(HistoryRepository, instance=True)
    handle = create_autospec(report_action.ReportHistoryHandle, instance=True)
    handle.__enter__.return_value = handle
    handle.iter_rows.return_value = iter((
        SnapshotRow('deliveries', 11, {'id': 11, 'status': 1}),
        SnapshotRow('file_copies', 14, {'id': 14, 'source_size': 500}),
    ))
    opener = create_autospec(report_action.open_report_history, return_value=handle)
    monkeypatch.setattr(report_action, 'open_report_history', opener)
    result = HistoryRepository.restore_report_entity(repository, 'delivery', 11,
        HistoryBoundary(5, 50), event_batch_size=731)
    assert result == {('deliveries', 11): {'id': 11, 'status': 1},
                      ('file_copies', 14): {'id': 14, 'source_size': 500}}
    opener.assert_called_once_with(repository, 'delivery', 11, HistoryBoundary(5, 50), event_batch_size=731)
    handle.__exit__.assert_called_once()


def test_report_scope_closes_ownership_parents_without_another_field_list(monkeypatch):
    from camctl.history import report_action
    from camctl.history.bounded import _OwnerBranch
    document = {'projections': {'action': {'root_table': 'actions', 'fields': {
        'copy': {'value': {'op': 'read', 'column': 'file_copies.source_size'}}}}},
        'relations': {}, 'assertions': {}}
    monkeypatch.setattr(report_action, '_dependencies', create_autospec(
        report_action._dependencies, return_value=document))
    def branches(table, entity):
        path = {'file_copies': ('file_copies', 'segment_inspections'),
                'segment_inspections': ('segment_inspections', 'segment_sets')}.get(table, (table,))
        return (_OwnerBranch(path, (), (), 'action_id'),)
    monkeypatch.setattr(report_action, '_ownership_branches', create_autospec(
        report_action._ownership_branches, side_effect=branches))
    assert report_action._required_tables('action', 'actions') == frozenset({
        'actions', 'file_copies', 'segment_inspections', 'segment_sets'})
