"""报告依赖范围的磁盘恢复；完整对象和快照维护继续使用完整句柄。"""
from __future__ import annotations

from contextlib import closing

from camctl.contracts.public_projection import _dependencies, projection_read_tables
from camctl.contracts.report_reads import ReportExpansionRequest
from camctl.contracts.values import ConsistencyError
from camctl.history.bounded import SegmentHistoryHandle, _event_limit, _limit, _ownership_branches
from camctl.history.events import business_tables_for_format, foreign_key_targets
from camctl.history.replay import ReplayError
from camctl.history.validators import EventValidationError
from camctl.persistence.transaction import encode_json_value


def report_restore_strategy(seed):
    """先处理当前命中，再以对象累计次数比较快照正向和当前逆向。"""
    if seed.head[1] == seed.floor[1]:
        return 'current'
    choice = seed.snapshot
    if choice is not None and (
            choice.boundary == seed.target or
            seed.floor[1]-choice.position[1] < seed.head[1]-seed.floor[1]):
        return 'snapshot'
    return 'current'


def _required_tables(entity, primary_table, *, phase='finite'):
    registry = _dependencies()
    projections = registry['projections']
    if entity in projections:
        required = set(projection_read_tables(entity, phase=phase, _registry=registry)) | {primary_table}
    else:
        # 文件等非公开对象只保留实际公开依赖；归属在候选查询中再次收窄。
        required = {primary_table}
        for name in projections:
            tables = projection_read_tables(name, _registry=registry)
            if primary_table in tables:
                required.update(tables)
    pending = list(required)
    while pending:
        table = pending.pop()
        for branch in _ownership_branches(table, entity):
            for parent in branch.tables:
                if parent not in required:
                    required.add(parent)
                    pending.append(parent)
    return frozenset(required)


def open_report_history(repository, entity, identity, boundary, *,
                        event_batch_size=256, row_batch_size=128, request=None):
    _event_limit(event_batch_size)
    _limit(row_batch_size)
    if request is not None and (not isinstance(request, ReportExpansionRequest)
            or entity != 'action' or identity != request.source_action_id):
        raise ConsistencyError('报告增长请求不属于源动作')
    phase = 'finite' if request is None else request.phase
    with repository._read_connection() as connection:
        seed = repository._object_seed(connection, entity, identity, boundary,
            event_batch_size=event_batch_size, primary_only=True, metadata_only=True,
            _bound_instance=True)
    handle = ReportHistoryHandle(repository, seed, entity=entity,
        tables=_required_tables(entity, seed.primary_table, phase=phase), phase=phase,
        event_batch_size=event_batch_size, row_batch_size=row_batch_size)
    try:
        return handle.restore(report_restore_strategy(seed))
    except (ReplayError, EventValidationError, ValueError, TypeError, KeyError) as error:
        handle.close()
        if isinstance(error, ConsistencyError):
            raise
        raise ConsistencyError('报告必要历史无法可靠恢复') from error
    except BaseException:
        handle.close()
        raise


class ReportHistoryHandle(SegmentHistoryHandle):
    """与报告既有分页端口兼容，只恢复权威投影闭包内的自身行。"""

    def __init__(self, repository, seed, *, entity, tables,
                 event_batch_size=256, row_batch_size=128, phase='finite'):
        self.entity = entity
        self.phase = phase
        self.required_tables = frozenset(tables)
        self._published_identity = None
        self._set_foreign_keys = {
            table: column for (table, column), target in foreign_key_targets().items()
            if target == 'segment_sets'}
        super().__init__(repository, seed, row_batch_size, event_batch_size=event_batch_size)

    def primary(self):
        self._require_ready()
        return self._get(self.seed.primary_table, self.seed.ref[1])

    def open_expansion(self, request):
        """独立恢复所请求的增长关系，保留原有限句柄及其游标。"""
        self._require_ready()
        if (not isinstance(request, ReportExpansionRequest) or self.entity != 'action'
                or request.source_action_id != self.action_id):
            raise ConsistencyError('报告增长请求不属于源动作')
        child = open_report_history(self.repository, self.entity, self.action_id, self.boundary,
            request=request, event_batch_size=self.event_batch_size, row_batch_size=self.batch_size)
        try:
            if (child.instance_id != self.instance_id or child.boundary != self.boundary
                    or child.active_result_ref() != self.active_result_ref()):
                raise ConsistencyError('增长关系与有限事实不属于同实例/H/正式集合')
            return child
        except BaseException:
            child.close()
            raise

    def _require_table(self, table):
        if table not in self.required_tables:
            raise ConsistencyError('报告尚未读取该增长关系')

    def row(self, table, identity):
        self._require_table(table)
        return super().row(table, identity)

    def related_rows(self, table, column=None, value=None):
        self._require_table(table)
        yield from super().related_rows(table, column, value)

    def read_page(self, table, *, ref=None, cursor=None, limit=128):
        self._require_table(table)
        return super().read_page(table, ref=ref, cursor=cursor, limit=limit)

    def _set_validation_scope(self):
        if self.phase == 'finite':
            return 'finite'
        return ('scope' if _dependencies()['read_expansions'][self.phase]
                ['members']['selection'] == 'referenced' else 'complete')

    def _member_reference(self):
        phase = getattr(self, 'phase', 'finite')
        if phase == 'finite':
            return None
        spec = _dependencies().get('read_expansions', {}).get(phase)
        if spec is None or spec['members']['selection'] != 'referenced':
            return None
        return _dependencies()['relations'][spec['members']['relation']]

    def _row_owner(self):
        return self.entity, self.seed.ref[1]

    def _selected_row(self, table, values):
        if table not in self.required_tables:
            return False
        if table == 'segment_sets':
            return values['id'] == self._published_identity
        reference = self._member_reference()
        if reference is not None and table == reference['to'].partition('.')[0]:
            if getattr(self, '_forward', False):
                return self._one('SELECT 1 FROM selected_members WHERE id=?', (values['id'],)) is not None
            return self._get(table, values['id'], 'candidates') is not None
        column = self._set_foreign_keys.get(table)
        return column is None or (
            self._published_identity is not None and values.get(column) == self._published_identity)

    def _candidate_query(self, table, branches):
        sql = ' UNION '.join(branch.query() for branch in branches)
        parameters = (self.seed.ref[1],)*len(branches)
        if table == 'segment_sets':
            sql = f'SELECT id FROM ({sql}) WHERE id=?'
            parameters += (self._published_identity,)
        elif table in self._set_foreign_keys:
            column = self._set_foreign_keys[table]
            sql = f'SELECT id FROM ({sql}) WHERE id IN (SELECT id FROM {table} WHERE {column}=?)'
            parameters += (self._published_identity,)
        reference = self._member_reference()
        if reference is not None and table == reference['to'].partition('.')[0]:
            source, _, column = reference['from'].partition('.')
            set_column = self._set_foreign_keys[source]
            sql = (f'SELECT id FROM ({sql}) WHERE id IN '
                   f'(SELECT {column} FROM {source} WHERE {set_column}=? AND created_event_id<=?)')
            parameters += (self._published_identity, self.boundary.last_event_id)
        return sql, parameters

    def _select_published(self, connection):
        if self.entity != 'action' or 'segment_sets' not in self.required_tables:
            return
        with closing(connection.execute(
                'SELECT id FROM segment_sets WHERE action_id=?'
                ' AND published_event_id IS NOT NULL AND published_event_id<=? LIMIT 2',
                (self.action_id, self.boundary.last_event_id))) as cursor:
            published = cursor.fetchall()
        if len(published) > 1:
            raise ConsistencyError('H处动作存在多个正式分段集合')
        self._published_identity = None if not published else published[0][0]

    def _prepare_forward(self):
        with self.repository._read_connection() as connection:
            self._select_published(connection)
        reference = self._member_reference()
        if reference is None:
            return
        self._db.execute('CREATE TABLE selected_members(id INTEGER PRIMARY KEY) WITHOUT ROWID').close()
        source, _, column = reference['from'].partition('.')
        previous = 0
        while self._published_identity is not None:
            with self.repository._read_connection() as connection:
                with closing(connection.execute(
                        f'SELECT id,{column} FROM {source} WHERE set_id=? AND created_event_id<=?'
                        ' AND id>? ORDER BY id LIMIT ?',
                        (self._published_identity, self.boundary.last_event_id, previous, self.batch_size))) as cursor:
                    rows = cursor.fetchall()
            if not rows:
                return
            for _, identity in rows:
                self._db.execute('INSERT OR IGNORE INTO selected_members VALUES (?)', (identity,)).close()
            previous = rows[-1][0]

    def _forward_candidate(self, table, identity):
        if table not in self.required_tables:
            return False
        branches = _ownership_branches(table, self.entity)
        if not branches:
            return False
        sql, parameters = self._candidate_query(table, branches)
        with self.repository._read_connection() as connection:
            with closing(connection.execute(f'SELECT 1 FROM ({sql}) WHERE id=? LIMIT 1',
                    (*parameters, identity))) as cursor:
                return cursor.fetchone() is not None

    def _copy_candidates(self):
        from camctl.persistence.repositories.history import _table_row
        specs = []
        with self.repository._read_connection() as connection:
            self.repository._boundary_within(connection)
            with closing(connection.execute('SELECT format_version FROM database_metadata WHERE id=1')) as cursor:
                database_format = cursor.fetchone()[0]
            available = business_tables_for_format(database_format)
            self._select_published(connection)
            for table in sorted(self.required_tables & set(available)):
                branches = _ownership_branches(table, self.entity)
                if not branches:
                    continue
                sql, parameters = self._candidate_query(table, branches)
                with closing(connection.execute(f'SELECT COUNT(*),MAX(id) FROM ({sql})', parameters)) as cursor:
                    count, maximum = cursor.fetchone()
                specs.append((table, sql, parameters, count, maximum or 0))
        for table, sql, parameters, expected, maximum in specs:
            previous, actual = 0, 0
            while previous < maximum:
                with self.repository._read_connection() as connection:
                    upper = self.repository._boundary_within(connection)
                    with closing(connection.execute(
                            f'SELECT id FROM ({sql}) WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                            (*parameters, previous, maximum, self.batch_size))) as cursor:
                        identities = cursor.fetchall()
                    selected = [(identity, _table_row(connection, table, identity))
                                for (identity,) in identities]
                if not selected:
                    raise ConsistencyError('报告候选计数与永久身份范围不符')
                self.stats['candidate_pages'] += 1
                for identity, values in selected:
                    if values is None:
                        raise ConsistencyError('报告必要候选缺少永久投影')
                    self._db.execute('INSERT INTO candidates VALUES (?,?,?,?)',
                        (table, identity, encode_json_value(values), upper.last_event_id)).close()
                actual += len(selected)
                previous = selected[-1][0]
            if actual != expected:
                raise ConsistencyError('报告候选计数与永久身份范围不符')
            self.stats['projection_rows'] += actual
        with self.repository._read_connection() as connection:
            maximum = self.repository._boundary_within(connection)
            head = self.repository._object_seed(connection, self.entity, self.seed.ref[1], maximum,
                event_batch_size=self.event_batch_size, include_snapshot=False, primary_only=True,
                _current_boundary=maximum)
        return maximum, head.head

    def _load_snapshot(self):
        from camctl.history.snapshot_stream import load_snapshot
        load_snapshot(self, include_row=self._selected_row)
