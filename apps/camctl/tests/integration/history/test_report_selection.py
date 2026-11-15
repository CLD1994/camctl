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
from ..support.report_selection_corpus import (
    submit_many as _submit_many, finish_many as _finish_many, populate_corpus, open_corpus, corpus_watermarks,
)


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
async def test_old_h_excludes_new_targets_and_their_ancestors(finished_photo, monkeypatch):
    import time
    from camctl.contracts.enums import enum_for

    started = time.perf_counter()
    owned, path = finished_photo
    await _submit(owned, path, '1')
    await _submit(owned, path, '2')
    before = _boundary(owned.connection)
    before_wm = _to_wm(owned.connection)
    assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    request = ReportScopeRequest(before, 0, before_wm, 1)
    preparation = time.perf_counter() - started

    repository = HistoryRepository(path / 'state.db')
    with monkeypatch.context() as patch:
        probe = _StatementProbe(repository, patch)
        with repository.report_read_scope():
            with repository.open_report_selection(request) as selection:
                started = time.perf_counter()
                first = selection.read_page('plan')
                assert first.items == (1,) and first.next_cursor is not None
                assert [record['kind'] for record in probe.statements] == ['candidate']
                assert probe.statements[0]['rows'] == [(1, None), (2, None)]
                first_page = time.perf_counter() - started

                # 第一页已经交付：真实完成旧动作，再受理 H 之后的新计划。
                started = time.perf_counter()
                await _finish_action(owned)
                after_action = _boundary(owned.connection)
                status, last_event = owned.connection.execute(
                    'SELECT status,last_event_id FROM actions WHERE id=1').fetchone()
                assert status == int(enum_for('actions.status').SUCCEEDED)
                assert before.last_event_id < last_event <= after_action.last_event_id
                assert owned.connection.execute(
                    'SELECT id,source_action_id FROM outputs WHERE created_event_id>? ORDER BY id',
                    (before.last_event_id,)).fetchall() == [(1, 1)]
                assert _to_wm(owned.connection) > before_wm
                await _submit(owned, path, '3')
                after_new_plan = _boundary(owned.connection)
                assert after_new_plan.last_event_id > after_action.last_event_id
                assert owned.connection.execute(
                    'SELECT id FROM plans WHERE id=3 AND created_event_id>?',
                    (before.last_event_id,)).fetchone() == (3,)
                assert owned.connection.execute(
                    'SELECT id,plan_id FROM actions WHERE id=3 AND created_event_id>?',
                    (before.last_event_id,)).fetchone() == (3, 3)
                # 未来计划的损坏不应进入旧边界；合法提交均已完成。
                owned.connection.execute('PRAGMA foreign_keys=OFF')
                owned.connection.execute('DELETE FROM plans WHERE id=3')
                assert not owned.connection.in_transaction
                assert owned.connection.execute('SELECT id FROM plans WHERE id=3').fetchone() is None
                later_changes = time.perf_counter() - started

                started = time.perf_counter()
                second = selection.read_page('plan', cursor=first.next_cursor)
                assert second.items == (2,) and second.next_cursor is None
                assert len(probe.statements) == 1
                assert _read(selection, 'action', 1) == [1]
                assert _read(selection, 'action', 2) == [2]
                assert _read(selection, 'action', 3) == []
                assert _read(selection, 'output', 1) == []
                continuation = time.perf_counter() - started

    # 在后来提交及未来计划损坏之后冷打开，仍按同一 H 独立选择。
    started = time.perf_counter()
    with HistoryRepository(path / 'state.db').open_report_selection(request) as selection:
        assert _read(selection, 'plan') == [1, 2]
        assert _read(selection, 'action', 1) == [1]
        assert _read(selection, 'action', 2) == [2]
        assert _read(selection, 'action', 3) == []
        assert _read(selection, 'output', 1) == []
    with HistoryRepository(path / 'state.db').open_report_selection(replace(request, boundary=INITIAL_BOUNDARY)) as selection:
        assert _read(selection, 'plan') == []
    print('REPORT_FROZEN_PAGE', {'preparation_s': preparation, 'first_page_s': first_page,
          'later_changes_s': later_changes, 'continuation_s': continuation,
          'cold_read_s': time.perf_counter() - started, 'before_h': repr(before),
          'before_wm': before_wm, 'after_action_h': repr(after_action),
          'after_new_plan_h': repr(after_new_plan)}, flush=True)


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
        with pytest.raises(ConsistencyError):
            selection.read_page('plan')


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
async def test_session_scope_is_readonly_and_exception_exit_releases_candidates(finished_photo, monkeypatch, error_type):
    import gc
    import sqlite3

    owned, path = finished_photo
    await _submit(owned, path, '1')
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    repository = HistoryRepository(path / 'state.db')
    probe = _QueryProbe(repository, monkeypatch)
    selection = repository.open_report_selection(request)
    with pytest.raises(error_type):
        with selection:
            assert _read(selection, 'plan') == [1]
            for field in ('request', 'instance_id'):
                with pytest.raises(AttributeError):
                    setattr(selection, field, None)
            raise error_type('writer stopped')
    with pytest.raises(ConsistencyError):
        selection.read_page('plan')
    gc.collect()
    assert probe.candidates and all(reference() is None for reference in probe.candidates)
    assert probe.connections
    for connection in probe.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


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
async def test_delivery_target_only_fills_obtain_ancestors_not_source_entities(two_item_world):
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


@pytest.mark.asyncio
@pytest.mark.parametrize('entity,table,row_id,parent', [
    ('action', 'plans', 1, 1),
    ('output', 'actions', 1, 1), ('output', 'plans', 1, 1),
    ('delivery', 'actions', 2, 2), ('delivery', 'plans', 2, 2),
    ('plan', 'plans', 1, None), ('plan', 'actions', 2, None),
    ('action', 'actions', 1, 1), ('output', 'outputs', 2, 1),
    ('delivery', 'deliveries', 2, 2),
    ('diagnostic', 'plan_file_diagnostics', 2, None),
])
async def test_independent_entry_rejects_missing_target_or_any_required_owner(
    two_item_world, tmp_path, monkeypatch, entity, table, row_id, parent,
):
    world = two_item_world
    for item in world.item_ids:
        _grant_item(world, item)
    await _submit_diagnostic(world.owned, tmp_path, 'one')
    await _submit_diagnostic(world.owned, tmp_path, 'two')
    request = ReportScopeRequest(_boundary(world.owned.connection), 0, _to_wm(world.owned.connection), 1)
    world.owned.connection.execute('PRAGMA foreign_keys=OFF')
    world.owned.connection.execute(f'DELETE FROM {table} WHERE id=?', (row_id,))
    probe = _QueryProbe(world.history, monkeypatch)
    with world.history.open_report_selection(request) as selection:
        # 每个入口都是第一次读取；不依赖先读根对象，也不交付部分 Page。
        with pytest.raises(ConsistencyError):
            selection.read_page(entity, parent_id=parent)
        with pytest.raises(ConsistencyError):
            selection.read_page(entity, parent_id=parent)
    import gc
    import sqlite3
    gc.collect()
    assert all(reference() is None for reference in probe.candidates)
    for connection in probe.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


@pytest.mark.asyncio
async def test_all_five_targets_keep_pages_parent_filter_and_legal_empty(
    two_item_world, tmp_path, monkeypatch,
):
    import time

    started = time.perf_counter()
    world = two_item_world
    for item in world.item_ids:
        _grant_item(world, item)
    await _submit_diagnostic(world.owned, tmp_path, 'one')
    await _submit_diagnostic(world.owned, tmp_path, 'two')
    request = ReportScopeRequest(_boundary(world.owned.connection), 0, _to_wm(world.owned.connection), 1)
    cases = [('plan', [(None, [1, 2])]), ('action', [(1, [1]), (2, [2]), (999, [])]),
             ('output', [(1, [1, 2]), (2, []), (999, [])]),
             ('delivery', [(1, []), (2, [1, 2]), (999, [])]), ('diagnostic', [(None, [1, 2])])]
    preparation = time.perf_counter() - started
    started = time.perf_counter()
    for entity, parents in cases:
        with world.history.open_report_selection(request) as selection:
            for parent, expected in parents:
                assert _read(selection, entity, parent) == expected
        # 无窗口候选时，即使 caller 的父 ID 不存在也仍是合法空页。
        with world.history.open_report_selection(replace(request, from_wm=request.to_wm)) as selection:
            assert _read(selection, entity, 999 if parents[0][0] is not None else None) == []
    original_checks = time.perf_counter() - started

    groups = [(entity, parent, expected) for entity, parents in cases for parent, expected in parents]
    found = {(entity, parent): [] for entity, parent, _ in groups}
    cursors = {}
    with monkeypatch.context() as patch:
        probe = _StatementProbe(world.history, patch)
        started = time.perf_counter()
        with world.history.report_read_scope():
            with world.history.open_report_selection(request) as selection:
                for entity, parent, expected in groups:
                    key = (entity, parent)
                    page = selection.read_page(entity, parent_id=parent)
                    assert page.items == tuple(expected[:1]), key
                    assert (page.next_cursor is not None) == (len(expected) > 1), key
                    found[key].extend(page.items)
                    cursors[key] = page.next_cursor
                # 交错续读不同类型；空页和单页再次穿插，不影响其他游标。
                for _ in range(max(len(expected) for _, _, expected in groups) - 1):
                    for entity, parent, expected in reversed(groups):
                        key = (entity, parent)
                        cursor = cursors[key]
                        if cursor is None:
                            page = selection.read_page(entity, parent_id=parent)
                            assert page.items == tuple(expected[:1]) and page.next_cursor is None, key
                            continue
                        page = selection.read_page(entity, parent_id=parent, cursor=cursor)
                        found[key].extend(page.items)
                        cursors[key] = page.next_cursor
        interleaved = time.perf_counter() - started
    for entity, parent, expected in groups:
        key = (entity, parent)
        assert found[key] == expected, key
        assert len(found[key]) == len(set(found[key])), key
        assert cursors[key] is None, key
    assert ('main', 'report_entity_changes', 'entity_type') in probe.reads
    selected_reads = {(table, column) for database, table, column in probe.reads if database == 'main'}
    selected_reads.update((table, column) for database, table, column, _ in probe.blobs if database == 'main')
    assert ('history_events', 'body_json') not in selected_reads
    assert ('entity_snapshots', 'content') not in selected_reads
    assert not probe.main_writes
    print('REPORT_CURSOR_INTERLEAVE', {'preparation_s': preparation,
          'original_checks_s': original_checks, 'interleaved_s': interleaved,
          'groups': len(groups), 'observed_statements': len(probe.statements),
          'observed_reads': sorted(probe.reads, key=repr), 'blob_opens': probe.blobs,
          'main_write_attempts': probe.main_writes}, flush=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('table', ['actions', 'plans'])
async def test_late_corruption_is_visible_before_first_page(finished_photo, table):
    owned, path = finished_photo
    _submit_many(owned, 8, 1)
    await _finish_many(owned, 8)
    _submit_many(owned, 2, 2)
    await _finish_many(owned, 3, action_id=9)
    request = ReportScopeRequest(_boundary(owned.connection), 0, _to_wm(owned.connection), 1)
    owned.connection.execute('PRAGMA foreign_keys=OFF')
    owned.connection.execute(f'DELETE FROM {table} WHERE id=?', (9 if table == 'actions' else 2,))
    repository = HistoryRepository(path / 'state.db')
    for entity, parent in [('action', 1), ('output', 1)]:
        with repository.open_report_selection(request) as selection:
            # 损坏对象位于较晚的位置；第一页也必须先失败。
            with pytest.raises(ConsistencyError):
                selection.read_page(entity, parent_id=parent)


@pytest.mark.asyncio
async def test_delivery_window_does_not_validate_unselected_source_reference_chain(two_item_world):
    world = two_item_world
    _grant_item(world, world.item_ids[0])
    from_wm = _to_wm(world.owned.connection)
    _grant_item(world, world.item_ids[1])
    request = ReportScopeRequest(_boundary(world.owned.connection), from_wm, _to_wm(world.owned.connection), 1)
    world.owned.connection.execute('PRAGMA foreign_keys=OFF')
    world.owned.connection.execute('DELETE FROM outputs WHERE id=2')
    world.owned.connection.execute('DELETE FROM actions WHERE id=1')
    world.owned.connection.execute('DELETE FROM plans WHERE id=1')
    with world.history.open_report_selection(request) as selection:
        assert _read(selection, 'delivery', 2) == [2]
        assert _read(selection, 'plan') == [2]
        assert _read(selection, 'action', 2) == [2]
        assert _read(selection, 'output', 1) == []




def _same_readonly_database(database, uri, path):
    """只匹配该状态库的只读 SQLite URI，接受等价的 URI 编码与参数顺序。"""
    import os
    from urllib.parse import parse_qs, unquote, urlsplit

    if not uri:
        return False
    raw = os.fsdecode(database)
    parts = urlsplit(raw)
    if parts.scheme != 'file' or parts.netloc not in ('', 'localhost'):
        return False
    if parse_qs(parts.query).get('mode') != ['ro']:
        return False
    try:
        return os.path.samefile(unquote(parts.path), path)
    except (FileNotFoundError, OSError):
        return False


class _QueryProbe:
    """在目标状态库的业务短读连接上观察分页与资源收场。"""
    def __init__(self, repository, monkeypatch):
        import sqlite3
        import weakref
        path = repository.path.resolve()
        original_connect = sqlite3.connect
        self.steps = 0
        self.connections = []
        self.sql = []
        self.candidates = []
        probe = self

        class Record:
            def __init__(self, row):
                self.row = row
            def __getitem__(self, key):
                return self.row[key]

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                was_in_transaction = self.in_transaction
                reads = set()
                statement = [None]
                observed = getattr(self, '_probe_attached', False)
                if observed:
                    def authorize(action, table, column, _database, _source):
                        if action == sqlite3.SQLITE_READ:
                            reads.add((table, column))
                        return sqlite3.SQLITE_OK
                    self.set_authorizer(authorize)
                    self.set_trace_callback(lambda value: statement.__setitem__(0, value))
                try:
                    cursor = super().execute(sql, *args, **kwargs)
                finally:
                    if observed:
                        self.set_authorizer(None)
                if not observed and not was_in_transaction and self.in_transaction:
                    # 状态库完成结构核验后，在业务读事务的边界开始观测。
                    self._probe_attached = True
                    self._probe_kind = None
                    probe.connections.append(self)
                    def row_factory(cursor, row):
                        value = sqlite3.Row(cursor, row)
                        if self._probe_kind == 'candidate':
                            value = Record(value)
                            probe.candidates.append(weakref.ref(value))
                        return value
                    def advance():
                        probe.steps += 100
                        return 0
                    self.set_progress_handler(advance, 100)
                    self.row_factory = row_factory
                elif observed:
                    columns = cursor.description or ()
                    touches_changes = any(table == 'report_entity_changes' for table, _ in reads)
                    kind = ('candidate' if touches_changes and len(columns) == 2 else
                            'page' if touches_changes and len(columns) == 1 else None)
                    self._probe_kind = kind
                    if kind is not None:
                        assert statement[0] is not None
                        probe.sql.append((kind, statement[0]))
                return cursor

        def connect(database, *args, **kwargs):
            if _same_readonly_database(database, kwargs.get('uri'), path):
                assert 'factory' not in kwargs
                kwargs['factory'] = ObservedConnection
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, 'connect', connect)


class _StatementProbe:
    """记录真实语句和取行；VM 指令估计为进度回调次数乘以粒度。"""

    def __init__(self, repository, monkeypatch):
        import sqlite3

        original_connect = sqlite3.connect
        path = repository.path.resolve()
        self.statements = []
        self.reads = set()
        self.blobs = []
        self.main_writes = []
        probe = self
        write_actions = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE,
            sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
        }
        persistent_pragmas = {
            'application_id', 'user_version', 'schema_version', 'journal_mode',
            'auto_vacuum', 'default_cache_size', 'encoding', 'page_size',
        }
        maintenance_pragmas = {'incremental_vacuum', 'wal_checkpoint'}

        class ObservedCursor(sqlite3.Cursor):
            def fetchall(self):
                record = getattr(self, 'record', None)
                try:
                    rows = super().fetchall()
                    if record is not None:
                        record['rows'] = [tuple(row) for row in rows]
                    return rows
                finally:
                    if record is not None:
                        self.connection.set_progress_handler(None, 0)

        class ObservedConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._statement_reads = None
                self._reads_by_sql = {}
                self.set_authorizer(self._authorize)

            def _authorize(self, action, table, column, database, _source):
                if action == sqlite3.SQLITE_READ:
                    probe.reads.add((database, table, column))
                    if self._statement_reads is not None:
                        self._statement_reads.add((table, column))
                elif (database == 'main' and action in write_actions
                      or action == sqlite3.SQLITE_ALTER_TABLE and table == 'main'
                      or action == sqlite3.SQLITE_PRAGMA and database in (None, 'main')
                      and table and (table.lower() in maintenance_pragmas
                                     or table.lower() in persistent_pragmas and column is not None)):
                    probe.main_writes.append((action, table, column, database))
                return sqlite3.SQLITE_OK

            def blobopen(self, table, column, row, /, *, readonly=False, name='main'):
                probe.blobs.append((name, table, column, readonly))
                if name == 'main' and not readonly:
                    probe.main_writes.append(('blobopen', table, column, name))
                return super().blobopen(table, column, row, readonly=readonly, name=name)

            def execute(self, sql, parameters=(), /):
                reads = set()
                steps = [0]

                def advance():
                    steps[0] += 100
                    if record is not None:
                        record['vm_steps_estimate'] = steps[0]
                    return 0

                record = None
                self._statement_reads = reads
                self.set_progress_handler(advance, 100)
                cursor = self.cursor(factory=ObservedCursor)
                try:
                    cursor.execute(sql, parameters)
                except BaseException:
                    self.set_progress_handler(None, 0)
                    cursor.close()
                    raise
                finally:
                    self._statement_reads = None
                    # 绑定失败时 SQLite 也可能已缓存准备好的语句。
                    if reads:
                        self._reads_by_sql[sql] = frozenset(reads)
                if not reads:
                    # SQLite 可复用本连接已准备的语句而不再调用 authorizer。
                    reads = set(self._reads_by_sql.get(sql, ()))
                columns = cursor.description or ()
                if any(table == 'report_entity_changes' for table, _ in reads) and len(columns) in (1, 2):
                    record = {'kind': 'candidate' if len(columns) == 2 else 'page',
                              'sql': sql, 'parameters': tuple(parameters),
                              'vm_steps_estimate': steps[0], 'reads': reads}
                    cursor.record = record
                    probe.statements.append(record)
                else:
                    self.set_progress_handler(None, 0)
                return cursor

        def connect(database, *args, **kwargs):
            if _same_readonly_database(database, kwargs.get('uri'), path):
                assert 'factory' not in kwargs
                kwargs['factory'] = ObservedConnection
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, 'connect', connect)


def test_statement_probe_observes_cached_sql_on_each_connection(tmp_path, monkeypatch):
    """真实 SQLite 复用语句时仍逐次记录输入和取行。"""
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    path = tmp_path / 'probe.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            'CREATE TABLE report_entity_changes(entity_type INTEGER,entity_id INTEGER);'
            'INSERT INTO report_entity_changes VALUES(1,7);'
            'CREATE TABLE unrelated(a INTEGER,b INTEGER); INSERT INTO unrelated VALUES(1,7);')
        connection.commit()
    probe = _StatementProbe(SimpleNamespace(path=path), monkeypatch)
    uri = f'{path.resolve().as_uri()}?mode=ro'
    sql = 'SELECT entity_type,entity_id FROM report_entity_changes WHERE entity_id>?'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        assert connection.execute(sql, (0,)).fetchall() == [(1, 7)]
        assert connection.execute(sql, (7,)).fetchall() == []
        assert connection.execute(sql, (6,)).fetchall() == [(1, 7)]
        other = 'SELECT a,b FROM unrelated WHERE b>?'
        assert connection.execute(other, (0,)).fetchall() == [(1, 7)]
        assert connection.execute(other, (7,)).fetchall() == []
        assert connection.execute('SELECT entity_id FROM report_entity_changes WHERE entity_id=?', (7,)).fetchall() == [(7,)]
        fresh_sql = 'SELECT entity_type,entity_id FROM report_entity_changes WHERE entity_id>=?'
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute(fresh_sql, ())
        with pytest.raises(sqlite3.OperationalError):
            connection.execute('SELECT FROM report_entity_changes')
        assert len(probe.statements) == 4
        assert connection.execute(fresh_sql, (7,)).fetchall() == [(1, 7)]
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute(sql, ())
        assert connection.execute(sql, (0,)).fetchall() == [(1, 7)]
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        assert connection.execute(sql, (7,)).fetchall() == []
        assert connection.execute(sql, (0,)).fetchall() == [(1, 7)]

    assert [(record['kind'], record['parameters'], record['rows']) for record in probe.statements] == [
        ('candidate', (0,), [(1, 7)]), ('candidate', (7,), []), ('candidate', (6,), [(1, 7)]),
        ('page', (7,), [(7,)]), ('candidate', (7,), [(1, 7)]), ('candidate', (0,), [(1, 7)]),
        ('candidate', (7,), []), ('candidate', (0,), [(1, 7)]),
    ]
    assert ('main', 'unrelated', 'a') in probe.reads


def _prepare_statistics_copy(source, destination, statistics):
    """从已核验固定样本准备独立副本，不改变来源库。"""
    import sqlite3
    from contextlib import closing
    from camctl.resources import resource_bytes
    from ..support.database import SCHEMA_RESOURCES

    empty_stats = None
    if statistics == 'empty_analyzed':
        with closing(sqlite3.connect(destination, uri=True)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            assert connection.execute('PRAGMA foreign_keys').fetchone() == (1,)
            assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
            for schema in SCHEMA_RESOURCES:
                connection.executescript(resource_bytes(schema).decode('utf-8'))
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                name = '"' + table.replace('"', '""') + '"'
                assert connection.execute(f'SELECT COUNT(*) FROM main.{name}').fetchone() == (0,), table
            connection.execute('ANALYZE')
            empty_stats = connection.execute('SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY tbl,idx').fetchall()
            assert all(int(row[2].split()[0]) == 0 for row in empty_stats)
            connection.execute('ATTACH DATABASE ? AS source', (f'{source.resolve().as_uri()}?mode=ro',))
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM source.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            connection.execute('BEGIN')
            connection.execute('PRAGMA defer_foreign_keys=ON')
            for table in tables:
                name = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in connection.execute(f'PRAGMA main.table_xinfo({name})') if row[6] == 0]
                column_names = ', '.join('"' + column.replace('"', '""') + '"' for column in columns)
                connection.execute(f'INSERT INTO main.{name} ({column_names}) SELECT {column_names} FROM source.{name}')
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            connection.commit()
            assert connection.execute('SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY tbl,idx').fetchall() == empty_stats
    else:
        with closing(sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination)) as connection:
                original.backup(connection)
                if statistics == 'representative_analyzed':
                    connection.execute('ANALYZE')
                    connection.commit()

    with closing(sqlite3.connect(f'{destination.resolve().as_uri()}?mode=ro', uri=True)) as connection:
        connection.execute('ATTACH DATABASE ? AS source', (f'{source.resolve().as_uri()}?mode=ro',))
        schemas = lambda db: connection.execute(
            f"SELECT type,name,tbl_name,sql FROM {db}.sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        assert schemas('main') == schemas('source')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM source.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            name = '"' + table.replace('"', '""') + '"'
            assert connection.execute(f'SELECT COUNT(*) FROM main.{name}').fetchone() == connection.execute(
                f'SELECT COUNT(*) FROM source.{name}').fetchone(), table
            assert connection.execute(f'SELECT * FROM main.{name} EXCEPT SELECT * FROM source.{name} LIMIT 1').fetchone() is None, table
            assert connection.execute(f'SELECT * FROM source.{name} EXCEPT SELECT * FROM main.{name} LIMIT 1').fetchone() is None, table
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA journal_mode').fetchone() == ('wal',)
        has_stats = bool(connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_stat1'").fetchone())
        assert has_stats == (statistics != 'none')
        if statistics == 'empty_analyzed':
            assert connection.execute('SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY tbl,idx').fetchall() == empty_stats
        elif statistics == 'representative_analyzed':
            stat = connection.execute(
                "SELECT stat FROM sqlite_stat1 WHERE idx='report_changes_by_sequence'").fetchone()
            assert stat is not None and int(stat[0].split()[0]) >= 4200


@pytest.fixture
def report_selection_sample(tmp_path):
    """每个消费者取得独立只读副本；关闭时核对来源和副本未改变。"""
    import time
    start = time.perf_counter()
    with open_corpus(tmp_path) as sample:
        print('CORPUS_SETUP', {'seconds': time.perf_counter() - start,
                               'scope': '解压、摘要、完整性、元数据和边界核对'}, flush=True)
        try:
            yield sample
        finally:
            closing_started = time.perf_counter()
    print('CORPUS_CLOSE', {'seconds': time.perf_counter() - closing_started,
                           'scope': '关闭连接并重核来源和副本摘要'}, flush=True)


def _actual_plans(path, statements):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as connection:
        return [tuple(row[3] for row in connection.execute(
            'EXPLAIN QUERY PLAN ' + statement['sql'], statement['parameters']))
            for statement in statements]


def _report_directory_uses_range_index(sql, plan):
    """逐个核对实际语句中的报告目录引用及其查询计划。"""
    import re
    from collections import Counter

    sources = re.findall(
        r'\b(?:FROM|JOIN)\s+(?:"report_entity_changes"|report_entity_changes)'
        r'(?:\s+(?:AS\s+)?("[A-Za-z_]\w*"|[A-Za-z_]\w*))?', sql, re.IGNORECASE)
    reserved = {'indexed', 'not', 'where', 'join', 'left', 'cross', 'inner', 'order', 'group', 'limit'}
    aliases = Counter((name.strip('"').lower() if name and name.lower() not in reserved
                       else 'report_entity_changes') for name in sources)
    accesses = []
    for row in plan:
        match = re.match(r'^(SEARCH|SCAN)\s+(?:"([A-Za-z_]\w*)"|([A-Za-z_]\w*))(?=\s|$)', row, re.IGNORECASE)
        if match and (match.group(2) or match.group(3)).lower() in aliases:
            accesses.append((match.group(1).upper(), (match.group(2) or match.group(3)).lower(), row))
    return bool(aliases) and Counter(alias for _, alias, _ in accesses) == aliases and all(
        operation == 'SEARCH' and 'report_changes_by_sequence' in row
        and 'entity_type=?' in row and 'change_seq>?' in row and 'change_seq<?' in row
        for operation, _, row in accesses)


@pytest.mark.asyncio
async def test_small_window_uses_index_after_physical_report_directory_growth(finished_photo, tmp_path, monkeypatch):
    """同一窄窗口在真实目录增长前后选中相同对象，并观察实际查询。"""
    import time
    from camctl.history.queries import report_target_types

    started = time.perf_counter()
    owned, small_path = finished_photo
    _submit_many(owned, 3, 1)
    from_wm = _to_wm(owned.connection)
    await _finish_many(owned, 4)
    to_wm = _to_wm(owned.connection)
    small_boundary = _boundary(owned.connection)
    large_dir = tmp_path / 'large'
    large_dir.mkdir()
    with open_corpus(large_dir) as (large_connection, large_state, watermarks):
        preparation = time.perf_counter() - started
        verification_started = time.perf_counter()
        assert (from_wm, to_wm) == watermarks[:2]
        action_type = report_target_types()['action']

        def facts(connection):
            total = connection.execute('SELECT COUNT(*) FROM report_entity_changes').fetchone()[0]
            actions = connection.execute(
                'SELECT COUNT(*) FROM report_entity_changes WHERE entity_type=?', (action_type,)).fetchone()[0]
            window = connection.execute(
                'SELECT entity_type,entity_id,event_id,change_seq FROM report_entity_changes '
                'WHERE change_seq>? AND change_seq<=? ORDER BY entity_type,entity_id,event_id',
                (from_wm, to_wm)).fetchall()
            return total, actions, window

        small_facts = facts(owned.connection)
        large_facts = facts(large_connection)
        assert large_facts[0] - small_facts[0] > 4200
        assert large_facts[1] - small_facts[1] > 4200
        assert small_facts[2] == large_facts[2]
        assert small_facts[2]
        large_boundary = _boundary(large_connection)
        assert max(row[2] for row in small_facts[2]) <= small_boundary.last_event_id
        assert max(row[2] for row in large_facts[2]) <= large_boundary.last_event_id
        comparison = time.perf_counter() - verification_started

        observations = []
        reading = 0.0
        for name, state, boundary in (
                ('small', small_path / 'state.db', small_boundary),
                ('large', large_state, large_boundary)):
            repository = HistoryRepository(state)
            request = ReportScopeRequest(boundary, from_wm, to_wm, 7)
            with monkeypatch.context() as patch:
                probe = _StatementProbe(repository, patch)
                read_started = time.perf_counter()
                with repository.open_report_selection(request) as selection:
                    first = selection.read_page('action', parent_id=1)
                    action_queries = len(probe.statements)
                    again = selection.read_page('action', parent_id=1)
                    after_repeat = len(probe.statements)
                    outputs = _read(selection, 'output', 1)
                    plans = _read(selection, 'plan')
                reading += time.perf_counter() - read_started
            observations.append((name, state, first, again, action_queries, after_repeat,
                                 outputs, plans, probe.statements))

        verification_started = time.perf_counter()
        for name, state, first, again, action_queries, after_repeat, outputs, plans, records in observations:
            assert first.items == again.items == (1,), name
            assert first.next_cursor is None and again.next_cursor is None, name
            assert outputs == [1, 2, 3, 4], name
            assert plans == [1], name
            assert action_queries == after_repeat == 1, name
            action = records[0]
            assert action['kind'] == 'candidate', name
            assert action['rows'] == [(1, 1)], name
            plan = _actual_plans(state, [action])[0]
            print('REPORT_DIRECTORY_GROWTH', {
                'database': name, 'window': (from_wm, to_wm),
                'boundary': (small_boundary if name == 'small' else large_boundary),
                'directory_rows': small_facts[0] if name == 'small' else large_facts[0],
                'action_rows': small_facts[1] if name == 'small' else large_facts[1],
                'window_associations': len(small_facts[2]), 'action_candidate_queries': action_queries,
                'sql': action['sql'], 'parameters': action['parameters'], 'rows': action['rows'],
                'vm_steps_estimate': action['vm_steps_estimate'], 'vm_granularity': 100,
                'plan': plan}, flush=True)
            assert _report_directory_uses_range_index(action['sql'], plan), (name, plan)
        verification = comparison + time.perf_counter() - verification_started
        print('REPORT_DIRECTORY_GROWTH_PHASE', {'preparation_s': preparation,
              'reading_s': reading, 'verification_s': verification}, flush=True)


@pytest.mark.parametrize('statistics', ['none', 'empty_analyzed', 'representative_analyzed'])
def test_actual_report_query_plans_with_statistics(report_selection_sample, tmp_path, monkeypatch, statistics):
    """三种统计状态均观察报告实际执行的首批和续页。"""
    import time

    source_connection, source, _ = report_selection_sample
    started = time.perf_counter()
    state = tmp_path / f'{statistics}.db'
    _prepare_statistics_copy(source, state, statistics)
    preparation = time.perf_counter() - started
    request = ReportScopeRequest(_boundary(source_connection), 0, _to_wm(source_connection), 100)
    repository = HistoryRepository(state)
    with monkeypatch.context() as patch:
        probe = _StatementProbe(repository, patch)
        started = time.perf_counter()
        with repository.open_report_selection(request) as selection:
            first = selection.read_page('action', parent_id=2)
            second = selection.read_page('action', parent_id=2, cursor=first.next_cursor)
        reading = time.perf_counter() - started
    started = time.perf_counter()
    records = probe.statements
    assert first.items == tuple(range(4, 104))
    assert first.next_cursor is not None
    assert second.items == tuple(range(104, 204))
    assert second.next_cursor is not None
    assert [record['kind'] for record in records] == ['candidate', 'page', 'page']
    assert [row[0] for row in records[1]['rows']] == list(range(4, 105))
    assert [row[0] for row in records[2]['rows']] == list(range(104, 205))
    assert first.next_cursor.after_id == 103
    assert second.next_cursor.after_id == 203
    plans = _actual_plans(state, records)
    print('REPORT_STATISTICS', {'statistics': statistics, 'preparation_s': preparation,
          'reading_s': reading, 'verification_s': time.perf_counter() - started,
          'sqlite': __import__('sqlite3').sqlite_version,
          'statements': [(record['kind'], record['sql'], record['parameters'], len(record['rows']),
                          record['vm_steps_estimate'], plan) for record, plan in zip(records, plans)],
          'vm_granularity': 100}, flush=True)
    problems = []
    candidate_access = [row for row in plans[0] if 'report_changes_by_sequence' in row]
    if not candidate_access or not all('SEARCH' in row and 'COVERING INDEX' in row for row in candidate_access):
        problems.append(('candidate_not_covering', plans[0]))
    for page, plan in enumerate(plans[1:], 1):
        access = [row for row in plan if 'report_changes_by_entity' in row]
        if not access or not all('SEARCH' in row and 'COVERING INDEX' in row for row in access):
            problems.append((f'page_{page}_not_covering', plan))
        if any('TEMP B-TREE FOR DISTINCT' in row or 'TEMP B-TREE FOR ORDER BY' in row for row in plan):
            problems.append((f'page_{page}_temporary_sort', plan))
    assert not problems, problems


@pytest.mark.parametrize('page_size', [100, 7])
def test_actual_report_query_work_for_page_sizes(report_selection_sample, monkeypatch, page_size):
    """固定范围完整读出 4200 个动作，分别记录每次真实查询的工作量。"""
    import time

    connection, state, _ = report_selection_sample
    request = ReportScopeRequest(_boundary(connection), 0, _to_wm(connection), page_size)
    repository = HistoryRepository(state)
    with monkeypatch.context() as patch:
        probe = _StatementProbe(repository, patch)
        started = time.perf_counter()
        with repository.report_read_scope():
            with repository.open_report_selection(request) as selection:
                actual = _read(selection, 'action', 2)
        reading = time.perf_counter() - started
    started = time.perf_counter()
    assert actual == list(range(4, 4204))
    records = probe.statements
    assert [record['kind'] for record in records].count('candidate') == 1
    pages = [record for record in records if record['kind'] == 'page']
    assert len(pages) == (4200 + page_size - 1) // page_size
    assert all(len(record['rows']) <= page_size + 1 for record in pages)
    assert ('main', 'report_entity_changes', 'entity_type') in probe.reads
    selected_reads = {(table, column) for database, table, column in probe.reads if database == 'main'}
    selected_reads.update((table, column) for database, table, column, _ in probe.blobs if database == 'main')
    assert ('history_events', 'body_json') not in selected_reads
    assert ('entity_snapshots', 'content') not in selected_reads
    assert not probe.main_writes
    print('REPORT_PAGE_WORK', {'page_size': page_size, 'reading_s': reading,
          'verification_s': time.perf_counter() - started, 'queries': len(records),
          'observed_reads': sorted(probe.reads, key=repr), 'blob_opens': probe.blobs,
          'main_write_attempts': probe.main_writes,
          'candidate_vm_estimate': records[0]['vm_steps_estimate'],
          'page_vm_total_estimate': sum(record['vm_steps_estimate'] for record in pages),
          'page_vm_first_estimate': pages[0]['vm_steps_estimate'], 'page_vm_last_estimate': pages[-1]['vm_steps_estimate'],
          'page_vm_min_estimate': min(record['vm_steps_estimate'] for record in pages),
          'page_vm_max_estimate': max(record['vm_steps_estimate'] for record in pages),
          'vm_granularity': 100}, flush=True)


@pytest.mark.asyncio
async def test_real_source_preparation_preserves_large_report_selection(finished_photo):
    """真实受理与拍摄形成大窗口，并由历史公开入口独立核对。"""
    import time
    owned, path = finished_photo
    started = time.perf_counter()
    early_sparse_wm, output_window_end, early = await populate_corpus(owned, path)
    preparation = time.perf_counter() - started
    verify_started = time.perf_counter()
    assert owned.connection.execute('SELECT COUNT(*) FROM plans').fetchone() == (3,)
    assert owned.connection.execute('SELECT plan_id,COUNT(*) FROM actions GROUP BY plan_id ORDER BY plan_id').fetchall() == [(1, 3), (2, 4200), (3, 3)]
    assert owned.connection.execute('SELECT id,source_action_id FROM outputs ORDER BY id').fetchall() == [(1, 1), (2, 1), (3, 1), (4, 1)]
    boundary = _boundary(owned.connection)
    assert owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone() == (boundary.last_event_id,)
    assert corpus_watermarks(owned.connection) == (early_sparse_wm, output_window_end, early)
    verification = time.perf_counter() - verify_started
    repository = HistoryRepository(path / 'state.db')
    request = ReportScopeRequest(boundary, 0, _to_wm(owned.connection), 17)
    target_started = time.perf_counter()
    selection = repository.open_report_selection(request)
    try:
        plan_ids = _read(selection, 'plan')
        first_actions = _read(selection, 'action', 1)
        large_actions = _read(selection, 'action', 2)
        last_actions = _read(selection, 'action', 3)
        outputs = _read(selection, 'output', 1)
        target = time.perf_counter() - target_started
        verify_started = time.perf_counter()
        assert plan_ids == [1, 2, 3]
        assert first_actions == [1, 2, 3]
        assert large_actions == list(range(4, 4204))
        assert last_actions == [4204, 4205, 4206]
        assert outputs == [1, 2, 3, 4]
        assert 0 < early_sparse_wm < output_window_end < early < request.to_wm
        verification += time.perf_counter() - verify_started
    finally:
        closing_started = time.perf_counter()
        selection.close()
        close = time.perf_counter() - closing_started
    print('REAL_SOURCE_PHASE', {'preparation_s': preparation, 'reading_s': target,
        'verification_s': verification, 'close_s': close,
        'note': '准备含真实受理、调度、拍摄及其测试替身；fixture收场另计'}, flush=True)


@pytest.mark.asyncio
async def test_large_window_releases_candidates_and_keyset_pages_do_not_rescan_window(report_selection_sample, monkeypatch):
    import gc
    import time
    from camctl.history.report_selection import ReportEntitySelection
    elapsed = {'reading': 0.0, 'close': 0.0}
    depth = [0]
    def timed(original, phase):
        def call(*args, **kwargs):
            outer = depth[0] == 0
            depth[0] += 1
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                depth[0] -= 1
                if outer:
                    elapsed[phase] += time.perf_counter() - started
        return call
    monkeypatch.setattr(HistoryRepository, 'open_report_selection', timed(HistoryRepository.open_report_selection, 'reading'))
    monkeypatch.setattr(ReportEntitySelection, 'read_page', timed(ReportEntitySelection.read_page, 'reading'))
    monkeypatch.setattr(ReportEntitySelection, '__exit__', timed(ReportEntitySelection.__exit__, 'close'))
    started = time.perf_counter()
    connection, state, watermarks = report_selection_sample
    early_sparse_wm, output_window_end, early = watermarks
    request = ReportScopeRequest(_boundary(connection), 0, _to_wm(connection), 17)
    repository = HistoryRepository(state)
    # 前一计划的产物 4 与后一计划的动作 4 共用整数编号；此窗口只包含前一计划的变化。
    assert connection.execute('SELECT source_action_id FROM outputs WHERE id=4').fetchone()[0] == 1
    assert connection.execute('SELECT plan_id FROM actions WHERE id=4').fetchone()[0] == 2
    output_window = replace(request, from_wm=early_sparse_wm, to_wm=output_window_end)
    with repository.open_report_selection(output_window) as selection:
        assert _read(selection, 'output', 1) == [1, 2, 3, 4]
        assert _read(selection, 'output', 2) == []
        assert _read(selection, 'action', 2) == []
    probe = _QueryProbe(repository, monkeypatch)
    with repository.open_report_selection(request) as selection:
        assert _read(selection, 'plan') == [1, 2, 3]
        action_candidates_start = len(probe.candidates)
        probe.steps = 0
        first = selection.read_page('action', parent_id=2)
        preflight_steps = probe.steps
        assert first.items == tuple(range(4, 21))
        gc.collect()
        action_candidates = probe.candidates[action_candidates_start:]
        assert action_candidates and all(reference() is None for reference in action_candidates)
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
        candidate_sql = [sql for kind, sql in probe.sql if kind == 'candidate']
        assert len(candidate_sql) == 2  # 固定窗口的 plan/action 各一次，跨父对象不重查。
        page_sql = [sql for kind, sql in probe.sql if kind == 'page']
        assert max(vm_pages) < preflight_steps // 8
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(f"{repository.path.resolve().as_uri()}?mode=ro", uri=True,
                                     isolation_level=None)) as connection:
            connection.execute('BEGIN').close()
            plans = tuple(row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + page_sql[0]))
            candidate_plan = tuple(row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + candidate_sql[1]))
        assert any('actions_plan_members' in row for row in plans)
        assert any('report_changes_by_entity' in row for row in plans)
        assert not any('TEMP B-TREE FOR ORDER BY' in row for row in plans)
        print({'preflight_vm_steps': preflight_steps, 'page_vm_min': min(vm_pages),
               'page_vm_max': max(vm_pages), 'pages': len(vm_pages) + 1,
               'candidate_queries': len(candidate_sql), 'unrelated_outputs': 4,
               'query_plan': plans, 'candidate_query_plan': candidate_plan})
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
        assert all(kind != 'page' for kind, _ in probe.sql)
    print({'sparse_vm_steps': probe.steps, 'sparse_candidates': len(probe.sql)})
    assert probe.steps < preflight_steps // 8
    probe.sql.clear()
    probe.steps = 0
    with repository.open_report_selection(replace(request, to_wm=early_sparse_wm)) as selection:
        assert _read(selection, 'action', 1) == [1, 2, 3]
        assert _read(selection, 'action', 2) == []
        assert all(kind != 'page' for kind, _ in probe.sql)
    print({'early_sparse_vm_steps': probe.steps})
    assert probe.steps < preflight_steps // 8
    total = time.perf_counter() - started
    print('SAMPLE_READ_PHASE', {'reading_s': elapsed['reading'],
        'verification_and_observer_s': total - elapsed['reading'] - elapsed['close'],
        'close_s': elapsed['close'], 'call_s': total,
        'note': '读取包含SQLite trace、进度和弱引用观测'}, flush=True)




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
        import sqlite3
        original_connect = sqlite3.connect
        attempts = []
        def failed_connection(database, *args, **kwargs):
            if _same_readonly_database(database, kwargs.get('uri'), repository.path):
                attempts.append(database)
                raise OSError('read page failure')
            return original_connect(database, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', failed_connection)
            with pytest.raises(OSError, match='read page failure'):
                selection.read_page('plan', cursor=cursor)
        assert len(attempts) == 1
        with pytest.raises(ConsistencyError):
            selection.read_page('plan')

    # 继续使用原会话和游标，将同一路径替换为另一个完整状态库。
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    import os
    state_path = path / 'state.db'
    replacement_path = path / 'replacement.db'
    from contextlib import closing
    try:
        _create_valid_database(replacement_path)
        replacement = open_existing(replacement_path, DbOpenMode.EXISTING_RW, DbConfig())
        with closing(replacement.connection):
            replacement_id = replacement.metadata.instance_id
            assert replacement_id != owned.metadata.instance_id
            replacement_checkpoint = replacement.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            assert replacement_checkpoint == (0, 0, 0)
        with repository.open_report_selection(request) as selection:
            cursor = selection.read_page('plan').next_cursor
            assert cursor is not None
            assert selection.instance_id == owned.metadata.instance_id
            assert cursor.instance_id == owned.metadata.instance_id
            assert selection.read_page('plan', cursor=cursor).items == (2,)
            checkpoint = owned.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            assert checkpoint == (0, 0, 0)
            owned.connection.close()
            for database in (state_path, replacement_path):
                assert not database.with_name(database.name + '-wal').exists()
                assert not database.with_name(database.name + '-shm').exists()
            os.replace(replacement_path, state_path)
            with pytest.raises(ConsistencyError):
                selection.read_page('plan', cursor=cursor)
            with pytest.raises(ConsistencyError):
                selection.read_page('plan', cursor=cursor)
    finally:
        owned.connection.close()
