"""已提交快照与初始恢复只读取所选正向区间。"""
from types import SimpleNamespace
from unittest.mock import call, create_autospec
from contextlib import contextmanager
import sqlite3

import pytest

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.history import bounded
from camctl.history.bounded import SegmentHistoryHandle
from camctl.persistence.repositories.history import HistoryRepository
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.contracts.values import ConsistencyError


@pytest.mark.parametrize('strategy,snapshot_count,floor,expected', [
    ('snapshot', 5, 5, []), ('snapshot', 3, 5, [(31, 50, True)]),
    ('initial', None, 5, [(0, 50, True)]),
])
def test_forward_restore_reads_only_its_required_interval(monkeypatch, strategy, snapshot_count, floor, expected):
    target = HistoryBoundary(5, 50)
    snapshot = None if snapshot_count is None else SimpleNamespace(
        boundary=target if snapshot_count == floor else HistoryBoundary(3, 31),
        position=(50 if snapshot_count == floor else 31, snapshot_count), transaction=object())
    handle = SegmentHistoryHandle.__new__(SegmentHistoryHandle)
    handle.seed = SimpleNamespace(ref=(1, 9), primary_table='actions', target=target,
        snapshot=snapshot, floor=(50, floor), head=(100, 10))
    handle.boundary, handle.action_id, handle.event_batch_size = target, 9, 256
    handle.repository = create_autospec(HistoryRepository, instance=True)
    handle.stats = {'events': 0}
    rows = {('actions', 9): {'id': 9, 'once': None}}
    handle._get = create_autospec(handle._get, side_effect=lambda table, identity: rows.get((table, identity)))
    handle._put = create_autospec(handle._put, side_effect=lambda table, identity, value: rows.__setitem__((table, identity), value))
    handle._load_snapshot = create_autospec(handle._load_snapshot)
    handle._copy_candidates = create_autospec(handle._copy_candidates,
        side_effect=AssertionError('正向恢复不应读取当前业务候选'))
    handle._validate = create_autospec(handle._validate)
    handle._validate_sets = create_autospec(handle._validate_sets)
    monkeypatch.setattr(bounded, 'load_event_registry', create_autospec(
        bounded.load_event_registry, return_value={'tables': {'actions': {'derived': ['id']}}}))
    calls = []
    def events(ref, lower, upper, previous, count, *, forward, batch_size, remaining):
        calls.append((lower.last_event_id, upper.last_event_id, forward))
        return tuple(SimpleNamespace(event_id=lower.last_event_id+i+1) for i in range(remaining))
    handle.repository._object_event_page.side_effect = events
    handle._apply = create_autospec(handle._apply)
    handle.restore(strategy)
    assert calls == expected
    assert rows[('actions', 9)]['once'] is None
    assert handle.ready
    assert handle._validate.call_args_list == (
        [call(build_indexes=False), call()]
        if snapshot_count is not None and snapshot_count < floor else
        [call()])


def _event(changes, identity=41):
    return EventEnvelope(identity, 4, 1, 1, 0, 1, None, 1, {}, tuple(changes))


def _forward_handle(monkeypatch, rows, owned=True):
    handle = SegmentHistoryHandle.__new__(SegmentHistoryHandle)
    handle._forward, handle._forward_count = True, 4
    handle.seed = SimpleNamespace(ref=(1, 9))
    handle._get = create_autospec(handle._get, side_effect=lambda table, identity: rows.get((table, identity)))
    handle._put = create_autospec(handle._put, side_effect=lambda table, identity, value: rows.__setitem__((table, identity), value))
    handle._forward_candidate = create_autospec(handle._forward_candidate, return_value=owned)
    handle._one = create_autospec(handle._one, side_effect=AssertionError('正向行不得借用C候选'))
    monkeypatch.setattr(bounded, 'business_columns', create_autospec(
        bounded.business_columns, return_value=frozenset({'once'})))
    return handle


@pytest.mark.parametrize('before,after', [(None, 7), (None, None)])
def test_forward_update_uses_snapshot_null_and_only_event_interval(monkeypatch, before, after):
    rows = {('actions', 9): {'id': 9, 'once': None}}
    handle = _forward_handle(monkeypatch, rows)
    handle._apply(_event([RowChange('actions', 9,
        RowImage(True, {'once': before}), RowImage(True, {'once': after}))]), True)
    assert rows[('actions', 9)] == {'id': 9, 'once': after}
    handle._one.assert_not_called()


def test_forward_create_uses_original_event_and_authoritative_derived_methods(monkeypatch):
    from camctl.persistence import transaction
    rows = {}
    handle = _forward_handle(monkeypatch, rows)
    monkeypatch.setattr(transaction, 'load_enum_registry', create_autospec(
        transaction.load_enum_registry, return_value={'history_objects': {'action': {'id': 1, 'table': 'actions'}}}))
    monkeypatch.setattr(transaction, 'load_event_registry', create_autospec(
        transaction.load_event_registry, return_value={'derived_history': {
            'actions.created_event_id': {'method': 'first_own_event'},
            'actions.last_event_id': {'method': 'last_own_event'},
            'actions.change_count': {'method': 'own_event_count'}}}))
    event = _event([RowChange('actions', 9, RowImage(False, {}), RowImage(True, {'once': None}))])
    handle._apply(event, True)
    assert rows[('actions', 9)] == {'id': 9, 'once': None, 'created_event_id': 41,
        'last_event_id': 41, 'change_count': 4}
    handle._one.assert_not_called()


@pytest.mark.parametrize('bad_creation', [False, True])
def test_forward_rows_preserve_object_selection_and_before_continuity(monkeypatch, bad_creation):
    rows = {} if bad_creation else {('actions', 9): {'id': 9, 'once': None}}
    handle = _forward_handle(monkeypatch, rows, owned=False)
    other = RowChange('actions', 10, RowImage(False, {}), RowImage(True, {'once': 9}))
    handle._apply(_event([other]), True)
    assert ('actions', 10) not in rows
    handle._forward_candidate.return_value = True
    change = (RowChange('actions', 9, RowImage(False, {}), RowImage(True, {})) if bad_creation
        else RowChange('actions', 9, RowImage(True, {'once': 7}), RowImage(True, {'once': None})))
    with pytest.raises(ConsistencyError):
        handle._apply(_event([change]), True)


@pytest.mark.parametrize('count', [3, 5])
def test_generic_snapshot_route_never_copies_current_business_rows(monkeypatch, count):
    repository = create_autospec(HistoryRepository, instance=True)
    connection = create_autospec(sqlite3.Connection, instance=True)
    @contextmanager
    def read_connection():
        yield connection
    repository._read_connection.side_effect = read_connection
    target = HistoryBoundary(5, 50)
    choice = SimpleNamespace(boundary=target if count == 5 else HistoryBoundary(3, 31), position=(31, count))
    seed = SimpleNamespace(ref=(3, 9), primary_table='outputs', target=target,
        current=HistoryBoundary(10, 100), rows={('outputs', 9): {'last_event_id': 100}},
        floor=(50, 5), head=(100, 10), snapshot=choice)
    repository._object_seed.return_value = seed
    repository._seed_entity_rows.side_effect = AssertionError('选中S不应复制C业务行')
    repository._snapshot_rows.return_value = {('outputs', 9): {'id': 9, 'once': None}}
    event = _event([RowChange('outputs', 9, RowImage(True, {'once': None}), RowImage(True, {'once': 7}))])
    repository._object_event_page.return_value = (event, event)
    repository._apply_object_event.side_effect = lambda seed, rows, event, *, forward: rows[('outputs', 9)].update(once=7)
    repository._finish_rows.side_effect = lambda seed, rows: rows
    result = HistoryRepository.restore_entity(repository, 'output', 9, target)
    repository._object_seed.assert_called_once_with(connection, 'output', 9, target,
        event_batch_size=128, metadata_only=True)
    assert result[('outputs', 9)]['once'] == (None if count == 5 else 7)
    if count == 5:
        repository._object_event_page.assert_not_called()
    else:
        args, options = repository._object_event_page.call_args
        assert args[1:3] == (choice.boundary, target)
        assert options['forward'] is True
