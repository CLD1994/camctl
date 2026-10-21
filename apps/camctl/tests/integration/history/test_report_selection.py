"""报告入选会话：真实受理历史、SQL 页与固定窗口的资源边界。"""
import asyncio
from dataclasses import replace

import pytest

from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.values import ConsistencyError
from camctl.history.queries import ReportScopeRequest
from camctl.persistence.repositories.history import HistoryRepository

from .test_report_scope import (
    _boundary, _finish_action, _submit, _submit_diagnostic, _to_wm, finished_photo,
)
from .test_restore_paths import two_item_world, _grant_item, owned


def _read(selection, entity, parent=None):
    cursor = None
    found = []
    while True:
        page = selection.read_page(entity, parent_id=parent, cursor=cursor)
        found.extend(page.items)
        if page.next_cursor is None:
            return found
        cursor = page.next_cursor


@pytest.mark.asyncio
async def test_selection_pages_preserve_targets_and_necessary_ancestors(finished_photo):
    owned, path = finished_photo
    await _submit(owned, path, '1')
    await _finish_action(owned)
    await _submit(owned, path, '2')
    await _submit_diagnostic(owned, path, 'bad')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    repository = HistoryRepository(path / 'state.db')
    with repository.open_report_selection(request) as selection:
        assert _read(selection, 'plan') == [1, 2]
        assert _read(selection, 'action', 1) == [1]
        assert _read(selection, 'action', 2) == [2]
        assert _read(selection, 'output', 1) == [1]
        assert _read(selection, 'output', 2) == []
        assert _read(selection, 'delivery', 1) == []
        assert _read(selection, 'diagnostic') == [1]


@pytest.mark.asyncio
async def test_old_h_excludes_new_targets_and_their_ancestors(finished_photo):
    owned, path = finished_photo
    await _submit(owned, path, '1')
    before = _boundary(owned.connection)
    await _submit(owned, path, '2')
    request = ReportScopeRequest(before, 0, _to_wm(owned.connection), 1)
    with HistoryRepository(path / 'state.db').open_report_selection(request) as selection:
        assert _read(selection, 'plan') == [1]
        assert _read(selection, 'action', 1) == [1]
        assert _read(selection, 'action', 2) == []
    with HistoryRepository(path / 'state.db').open_report_selection(replace(request, boundary=INITIAL_BOUNDARY)) as selection:
        assert _read(selection, 'plan') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('instance_id', 'other-db'), ('boundary', INITIAL_BOUNDARY),
    ('from_wm', 1), ('to_wm', 9999), ('entity', 'diagnostic'), ('parent_id', 1)])
async def test_cursor_rejects_different_scope_or_parent_role(finished_photo, field, value):
    owned, path = finished_photo
    await _submit(owned, path, '1')
    await _submit(owned, path, '2')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    with HistoryRepository(path / 'state.db').open_report_selection(request) as selection:
        cursor = selection.read_page('plan').next_cursor
        assert cursor is not None
        with pytest.raises(ConsistencyError):
            selection.read_page('plan', cursor=replace(cursor, **{field: value}))
        assert selection._cache == {}
        with pytest.raises(ConsistencyError):
            selection.read_page('plan')


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
async def test_session_scope_is_readonly_and_exception_exit_releases_candidates(finished_photo, error_type):
    owned, path = finished_photo
    await _submit(owned, path, '1')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    selection = HistoryRepository(path / 'state.db').open_report_selection(request)
    with pytest.raises(error_type):
        with selection:
            assert _read(selection, 'plan') == [1]
            for field in ('request', 'instance_id'):
                with pytest.raises(AttributeError):
                    setattr(selection, field, None)
            raise error_type('writer stopped')
    assert selection._cache == {}
    with pytest.raises(ConsistencyError):
        selection.read_page('plan')


@pytest.mark.asyncio
async def test_missing_materialized_target_is_not_silently_omitted(finished_photo):
    owned, path = finished_photo
    await _submit(owned, path, '1')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    # 故障注入：报告目录仍保存动作，但当前必要主行丢失。
    owned.connection.execute('PRAGMA foreign_keys=OFF')
    owned.connection.execute('DELETE FROM actions WHERE id=1')
    with HistoryRepository(path / 'state.db').open_report_selection(request) as selection:
        with pytest.raises(ConsistencyError):
            selection.read_page('action', parent_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('cache_budget', [4096, 1])
async def test_delivery_target_only_fills_obtain_ancestors_not_source_entities(two_item_world, monkeypatch, cache_budget):
    from camctl.history import report_selection
    monkeypatch.setattr(report_selection, '_CACHE_LIMIT', cache_budget)
    world = two_item_world
    _grant_item(world, world.item_ids[0])
    from_wm = _to_wm(world.owned.connection)
    _grant_item(world, world.item_ids[1])
    request = ReportScopeRequest(_boundary(world.owned.connection), from_wm, _to_wm(world.owned.connection), 32)
    with world.history.open_report_selection(request) as selection:
        assert _read(selection, 'delivery', 2) == [2]
        assert _read(selection, 'plan') == [2]
        assert _read(selection, 'action', 2) == [2]
        assert _read(selection, 'delivery', 2) == [2]
        assert _read(selection, 'action', 1) == []
        assert _read(selection, 'output', 1) == []
        if cache_budget == 1:
            assert selection._cache['plan'] is None
            assert selection._cache['action'] is None


def _submit_many(owned, count, request_id):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from .test_report_scope import _Catalog, _plan_body, _past_schedule
    body = _plan_body(str(request_id), _past_schedule())
    prototype = body['actions'][0]
    body['actions'] = [dict(prototype, name=f'shot-{index}') for index in range(count)]
    result = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput('many.json', body), _Catalog(), CommandMode.RUN, 1),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.plan_disposition is PlanDisposition.REGISTERED, result.value


class _QueryProbe:
    """真实 SQLite 进度/SQL 观测；候选行仅加不持有成员的弱引用。"""
    def __init__(self, repository, monkeypatch):
        import sqlite3
        import weakref
        self.steps = 0
        self.connections = []
        self.sql = []
        self.candidates = []
        original = repository._connect
        probe = self
        class Record:
            def __init__(self, row):
                self.row = row
            def __getitem__(self, key):
                return self.row[key]
        def connect():
            connection = original()
            probe.connections.append(connection)
            candidate_query = False
            def trace(statement):
                nonlocal candidate_query
                candidate_query = statement.startswith('SELECT ') and 'FROM report_entity_changes c INDEXED BY report_changes_by_sequence' in statement
                if 'report_entity_changes' in statement:
                    probe.sql.append(statement)
            def row_factory(cursor, row):
                value = sqlite3.Row(cursor, row)
                if candidate_query:
                    value = Record(value)
                    probe.candidates.append(weakref.ref(value))
                return value
            def advance():
                probe.steps += 100
                return 0
            connection.set_trace_callback(trace)
            connection.set_progress_handler(advance, 100)
            connection.row_factory = row_factory
            return connection
        monkeypatch.setattr(repository, '_connect', connect)


@pytest.mark.asyncio
async def test_large_window_releases_overflow_once_and_keyset_pages_do_not_rescan_window(finished_photo, monkeypatch):
    import gc
    owned, path = finished_photo
    # 所有规模数据由真实受理事务生成，没有手填不对应事件的投影或目录。
    _submit_many(owned, 3, 1)
    early_sparse_wm = _to_wm(owned.connection)
    await _finish_many(owned, 128)
    _submit_many(owned, 4200, 2)
    early = _to_wm(owned.connection)
    _submit_many(owned, 3, 3)
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 17)
    repository = HistoryRepository(path / 'state.db')
    probe = _QueryProbe(repository, monkeypatch)
    with repository.open_report_selection(request) as selection:
        assert _read(selection, 'plan') == [1, 2, 3]
        probe.steps = 0
        first = selection.read_page('action', parent_id=2)
        preflight_steps = probe.steps
        assert first.items == tuple(range(4, 21))
        assert selection._cache['action'] is None
        assert sum(len(rows) for rows in selection._cache.values() if rows is not None) <= 4096
        gc.collect()
        # 仅三份计划候选保留；超限的动作候选已在返回第一页前释放。
        assert sum(reference() is not None for reference in probe.candidates) == 3
        cursor = first.next_cursor
        vm_pages = []
        actual = list(first.items)
        while cursor is not None:
            probe.steps = 0
            page = selection.read_page('action', parent_id=2, cursor=cursor)
            vm_pages.append(probe.steps)
            actual.extend(page.items)
            cursor = page.next_cursor
        assert actual == list(range(4, 4204))
        assert _read(selection, 'action', 3) == [4204, 4205, 4206]
        candidate_sql = [sql for sql in probe.sql if sql.startswith('SELECT e')]
        assert len(candidate_sql) == 2  # 固定窗口的 plan/action 各一次，跨父对象不重查。
        page_sql = [sql for sql in probe.sql if sql.startswith('SELECT r')]
        assert all(' IN (' not in sql for sql in page_sql)
        assert max(vm_pages) < preflight_steps // 8
        with repository._read_connection() as connection:
            plans = tuple(row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + page_sql[0]))
        assert any('actions_plan_members' in row for row in plans)
        assert any('report_changes_by_entity' in row for row in plans)
        assert not any('TEMP B-TREE FOR ORDER BY' in row for row in plans)
        print({'preflight_vm_steps': preflight_steps, 'page_vm_min': min(vm_pages),
               'page_vm_max': max(vm_pages), 'pages': len(vm_pages) + 1,
               'candidate_queries': len(candidate_sql), 'unrelated_outputs': 128, 'query_plan': plans})
    gc.collect()
    assert all(reference() is None for reference in probe.candidates)
    import sqlite3
    for connection in probe.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
    # 稀疏的末窗口不随当前已有的 4200 个无关动作遍历主表。
    probe.sql.clear()
    probe.steps = 0
    sparse = replace(request, from_wm=early)
    with repository.open_report_selection(sparse) as selection:
        assert _read(selection, 'action', 3) == [4204, 4205, 4206]
        assert _read(selection, 'action', 2) == []
        assert all(not sql.startswith('SELECT r') for sql in probe.sql)
    print({'sparse_vm_steps': probe.steps, 'sparse_candidates': len(probe.sql)})
    assert probe.steps < preflight_steps // 8
    probe.sql.clear()
    probe.steps = 0
    with repository.open_report_selection(replace(request, to_wm=early_sparse_wm)) as selection:
        assert _read(selection, 'action', 1) == [1, 2, 3]
        assert _read(selection, 'action', 2) == []
        assert all(not sql.startswith('SELECT r') for sql in probe.sql)
    print({'early_sparse_vm_steps': probe.steps})
    assert probe.steps < preflight_steps // 8


async def _finish_many(owned, count):
    import time
    from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
    from camctl.capture.results import FileKind
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from .test_report_scope import _runtime
    from ..capture.test_capture_contract import ResultsDouble, _entry
    now = int(time.time() * 1_000_000)
    started = SchedulingRepository().start_action(StartActionRequest(1, now, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    from camctl.persistence.repositories.scheduling import StartOutcome
    assert started.value.outcome is StartOutcome.STARTED, (started.value, tuple(owned.connection.execute('SELECT type,status,error_code,input_fields_json FROM actions WHERE id=1').fetchone()))
    runtime = _runtime(owned)
    runtime.results = ResultsDouble({1: tuple(_entry(f'photo-{index}', kind=FileKind.PHOTO) for index in range(count))})
    dispatched = await dispatch_ready(runtime, ready_capture_actions(owned.connection, now))
    assert all(not isinstance(outcome, BaseException) for _, outcome in dispatched), dispatched
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()[0] == count, tuple(owned.connection.execute('SELECT status,error_code,error_details_json FROM actions WHERE id=1').fetchone())


@pytest.mark.asyncio
async def test_initialization_rejects_future_or_partial_h(finished_photo):
    from camctl.contracts.history_values import HistoryBoundary
    owned, path = finished_photo
    await _submit(owned, path, '1')
    await _finish_action(owned)
    boundary = _boundary(owned.connection)
    repository = HistoryRepository(path / 'state.db')
    for wrong in (HistoryBoundary(boundary.txn_id + 1, boundary.last_event_id + 1),
                  HistoryBoundary(boundary.txn_id, boundary.last_event_id - 1)):
        with pytest.raises(ConsistencyError):
            repository.open_report_selection(ReportScopeRequest(wrong, 0, _to_wm(owned.connection), 1))


@pytest.mark.asyncio
async def test_read_failure_and_database_replacement_close_bound_session(finished_photo, monkeypatch):
    from ..persistence.test_runtime import _create_valid_database
    owned, path = finished_photo
    await _submit(owned, path, '1')
    await _submit(owned, path, '2')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    repository = HistoryRepository(path / 'state.db')
    with repository.open_report_selection(request) as selection:
        cursor = selection.read_page('plan').next_cursor
        other = path / 'replacement.db'
        _create_valid_database(other)
        repository.path = other
        with pytest.raises(ConsistencyError):
            selection.read_page('plan', cursor=cursor)
        assert selection._cache == {}
    repository = HistoryRepository(path / 'state.db')
    with repository.open_report_selection(request) as selection:
        cursor = selection.read_page('plan').next_cursor
        def failed_connection():
            raise OSError('read page failure')
        monkeypatch.setattr(repository, '_connect', failed_connection)
        with pytest.raises(OSError):
            selection.read_page('plan', cursor=cursor)
        assert selection._cache == {}
        with pytest.raises(ConsistencyError):
            selection.read_page('plan')
