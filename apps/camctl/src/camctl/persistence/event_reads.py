"""原写事务的提交前投影与已验证事件覆盖；不读取未来提案。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.reads import PlanReadScope, ProductReadScope


class SqlEventReads:
    """供逐产物守卫独立复核的有限只读端口。

    查询字段由内部白名单限定。每批 SQL 游标在交付前关闭；覆盖表
    只含本事务已经验证的变化，读错误与失效事务直接传播。
    """

    _RANGES = {
        "plans": frozenset(("id",)),
        "actions": frozenset(("id", "status", "plan_id")),
        "action_dependencies": frozenset(("id", "action_id")),
        "obtain_source_selections": frozenset(("id", "dependency_id")),
        "obtain_items": frozenset(("id", "selection_id", "output_id", "requested_output_id")),
        "cleanup_items": frozenset(("id", "action_id", "output_id")),
        "recording_processing": frozenset(("id", "action_id")),
        "outputs": frozenset(("id",)),
        "device_files": frozenset(("id",)),
        "intermediate_files": frozenset(("id",)),
        "output_origins": frozenset(("id", "output_id", "original_output_id")),
    }
    SOURCE_COLUMNS = ("id", "type", "status", "plan_id", "name", "group_name")
    ACTION_COLUMNS = ("id", "type", "status", "scheduled_at", "cancel_requested", "plan_id", "input_index")

    def __init__(self, connection, scope, validated=()):
        if not isinstance(scope, (ProductReadScope, PlanReadScope)):
            raise ConsistencyError("事实读取必须使用原事务声明的范围")
        self.connection, self.scope = connection, scope
        self._overlay = {}
        for accepted in validated:
            event = accepted.envelope
            for row in event.rows:
                key = row.table, row.row_id
                if not row.after.exists:
                    self._overlay[key] = None
                else:
                    prior = self._overlay.get(key, {})
                    self._overlay[key] = {**(prior or {}), **row.after.values}

    def _check(self, table, column="id", identity=None):
        if not self.connection.in_transaction:
            raise ConsistencyError("当前事件事实端口必须在原写事务内使用")
        if table not in self._RANGES or column not in self._RANGES[table]:
            raise ConsistencyError("当前事件事实查询超出内部允许范围")
        if identity is not None:
            ObjectId(identity)
        if isinstance(self.scope, PlanReadScope) and (
                table != "actions" or column not in ("id", "plan_id")
                or (column == "plan_id" and identity != self.scope.plan_id)):
            raise ConsistencyError("计划聚合读取超出所属计划")

    def optional(self, table, identity, columns=None):
        self._check(table, identity=identity)
        from camctl.persistence.transaction import row_facts

        if columns is None:
            facts = row_facts(self.connection, table, identity)
        else:
            if table != "actions" or tuple(columns) not in (self.SOURCE_COLUMNS, self.ACTION_COLUMNS):
                raise ConsistencyError("事实列投影超出允许范围")
            with closing(self.connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE id=?", (identity,),
            )) as cursor:
                row = cursor.fetchone()
            facts = dict(zip(columns, row)) if row is not None else None
        if (table, identity) in self._overlay:
            changes = self._overlay[table, identity]
            facts = None if changes is None else {**(facts or {"id": identity}), **changes}
        if isinstance(self.scope, PlanReadScope) and facts is not None and facts.get("plan_id") != self.scope.plan_id:
            raise ConsistencyError("计划聚合记录不属于当前计划")
        return facts

    def required(self, table, identity):
        facts = self.optional(table, identity)
        if facts is None:
            raise ConsistencyError(f"当前事件关联记录缺失: {table}#{identity}")
        return facts

    def iter_members(self, table, column, identity, *, columns=None, limit=None):
        self._check(table, column, identity)
        size = self.scope.batch_size if limit is None else limit
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= self.scope.batch_size:
            raise ConsistencyError("事实读取限额超出可信范围")
        changed = tuple(key[1] for key in self._overlay if key[0] == table)
        after = 0
        while True:
            # 前序事件可能使成员进入或离开范围；它们必须参与稳定 ID 扫描。
            extra = " OR id IN (" + ",".join("?" for _ in changed) + ")" if changed else ""
            with closing(self.connection.execute(
                f"SELECT id FROM {table} WHERE ({column}=?{extra}) AND id>? ORDER BY id LIMIT ?",
                (identity, *changed, after, size),
            )) as cursor:
                candidates = tuple(cursor.fetchmany(size))
            # 未写入 SQL 的新行独立合并；数量仅由本事务实际触及行数限制。
            added = [key for key in changed if key > after and
                     not any(key == row[0] for row in candidates)]
            upper = candidates[-1][0] if len(candidates) == size else None
            ids = sorted({row[0] for row in candidates} | {
                key for key in added if upper is None or key <= upper})
            if not ids:
                return
            batch = []
            for key in ids[:size]:
                facts = self.optional(table, key, columns)
                if facts is not None and facts.get(column, key if column == "id" else None) == identity:
                    batch.append((key, facts))
            after = ids[min(size, len(ids)) - 1]
            yield tuple(batch)
            if len(candidates) < size and len(ids) <= size:
                return

    def actions(self):
        for status in (1, 2):
            for batch in self.iter_members("actions", "status", status, columns=self.ACTION_COLUMNS):
                yield from batch
