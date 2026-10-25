"""报告依赖范围的磁盘恢复；完整对象和快照维护继续使用完整句柄。"""
from __future__ import annotations

from contextlib import closing

from camctl.contracts.public_projection import _dependencies, projection_read_tables
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


def _required_tables(entity, primary_table):
    registry = _dependencies()
    projections = registry['projections']
    if entity in projections:
        required = set(projection_read_tables(entity, _registry=registry)) | {primary_table}
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
                        event_batch_size=256, row_batch_size=128):
    _event_limit(event_batch_size)
    _limit(row_batch_size)
    with repository._read_connection() as connection:
        seed = repository._object_seed(connection, entity, identity, boundary,
            event_batch_size=event_batch_size, primary_only=True)
    handle = ReportHistoryHandle(repository, seed, entity=entity,
        tables=_required_tables(entity, seed.primary_table),
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
                 event_batch_size=256, row_batch_size=128):
        self.entity = entity
        self.required_tables = frozenset(tables)
        self._published_identity = None
        self._set_foreign_keys = {
            table: column for (table, column), target in foreign_key_targets().items()
            if target == 'segment_sets'}
        super().__init__(repository, seed, row_batch_size, event_batch_size=event_batch_size)

    def primary(self):
        self._require_ready()
        return self._get(self.seed.primary_table, self.seed.ref[1])

    def _row_owner(self):
        return self.entity, self.seed.ref[1]

    def _selected_row(self, table, values):
        if table not in self.required_tables:
            return False
        if table == 'segment_sets':
            return values['id'] == self._published_identity
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
        return sql, parameters

    def _copy_candidates(self):
        from camctl.persistence.repositories.history import _table_row
        specs = []
        with self.repository._read_connection() as connection:
            self.repository._boundary(connection)
            with closing(connection.execute('SELECT format_version FROM database_metadata WHERE id=1')) as cursor:
                database_format = cursor.fetchone()[0]
            available = business_tables_for_format(database_format)
            if self.entity == 'action' and 'segment_sets' in self.required_tables:
                with closing(connection.execute(
                        'SELECT id FROM segment_sets WHERE action_id=?'
                        ' AND published_event_id IS NOT NULL AND published_event_id<=? LIMIT 2',
                        (self.action_id, self.boundary.last_event_id))) as cursor:
                    published = cursor.fetchall()
                if len(published) > 1:
                    raise ConsistencyError('H处动作存在多个正式分段集合')
                self._published_identity = None if not published else published[0][0]
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
                    upper = self.repository._boundary(connection)
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
            maximum = self.repository._boundary(connection)
            head = self.repository._object_seed(connection, self.entity, self.seed.ref[1], maximum,
                event_batch_size=self.event_batch_size, include_snapshot=False, primary_only=True)
        return maximum, head.head

    def _load_snapshot(self):
        from camctl.history.snapshot_stream import load_snapshot
        load_snapshot(self, include_row=self._selected_row)
