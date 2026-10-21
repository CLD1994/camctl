"""固定报告窗口的有界实体页；归属结构来自权威报告登记。

小窗口每种实体只预查一次，共享 4096 条记录预算。超过剩余预算
即释放候选，后续沿实体主键/父键续读；不保留整棵入选树或连接。
"""
from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING

from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.history.changes import _dependencies
from camctl.history.queries import ReportEntityCursor, ReportScopeRequest, report_target_types

if TYPE_CHECKING:
    from camctl.persistence.repositories.history import HistoryRepository

_CACHE_LIMIT = 4096


def _q(identifier):
    if not isinstance(identifier, str) or re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', identifier) is None:
        raise ConsistencyError('报告实体登记含非法 SQL 标识符')
    return '"' + identifier + '"'


@dataclass(frozen=True)
class _Entity:
    name: str
    type_id: int
    table: str
    identity: str
    parent: str | None
    foreign_key: str | None


def _entities():
    types = report_target_types()
    result = {}
    for name, spec in _dependencies()['entities'].items():
        if not spec.get('target'):
            continue
        if name not in types:
            raise ConsistencyError('报告实体与历史对象登记不一致')
        parent = spec.get('parent', {})
        result[name] = _Entity(name, types[name], _q(spec['table']), _q(spec['identity']),
                               parent.get('entity'), _q(parent['foreign_key']) if parent else None)
    if set(result) != set(types):
        raise ConsistencyError('报告目标与实体归属登记不一致')
    for spec in result.values():
        seen = set()
        current = spec
        while current.parent is not None:
            if current.name in seen or current.parent not in result:
                raise ConsistencyError('报告实体归属循环或缺失')
            seen.add(current.name)
            current = result[current.parent]
    return result


class ReportEntitySelection:
    """一个固定 instance/H/水位窗口的选择会话；每页独立短读。"""
    def __init__(self, repository: HistoryRepository, request: ReportScopeRequest, instance_id: str):
        self._repository, self._request, self._instance_id = repository, request, instance_id
        self._entities = _entities()
        self._cache = {}
        self._remaining = _CACHE_LIMIT
        self._closed = False

    @property
    def request(self) -> ReportScopeRequest:
        return self._request

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def __enter__(self):
        self._active()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._cache.clear()
        self._remaining = _CACHE_LIMIT
        self._closed = True

    def _active(self):
        if self._closed:
            raise ConsistencyError('报告入选会话已关闭')

    def _paths(self, target):
        for source in self._entities.values():
            path = [source]
            while path[-1].name != target.name and path[-1].parent is not None:
                path.append(self._entities[path[-1].parent])
            if path[-1].name == target.name:
                yield path

    def _window(self, source):
        return (source.type_id, self.request.from_wm, self.request.to_wm,
                self.request.boundary.last_event_id)

    @staticmethod
    def _window_sql():
        return 'c.entity_type=? AND c.change_seq>? AND c.change_seq<=? AND c.event_id<=?'

    def _candidates_sql(self, target):
        branches, parameters = [], []
        for path in self._paths(target):
            source = path[0]
            target_alias = f'e{len(path)-1}'
            # 所有入口独立核验 source→target→root，不依赖先读父对象。
            while path[-1].parent is not None:
                path.append(self._entities[path[-1].parent])
            joins = [f'LEFT JOIN {source.table} e0 ON e0.{source.identity}=c.entity_id']
            for index, parent in enumerate(path[1:], 1):
                child = path[index - 1]
                joins.append(f'LEFT JOIN {parent.table} e{index} ON e{index}.{parent.identity}=e{index-1}.{child.foreign_key}')
            missing = ' OR '.join(f'e{index}.{spec.identity} IS NULL' for index, spec in enumerate(path))
            # NULL 排在合法整数 ID 前；任意晚 ID 损坏也不能被 LIMIT 隐藏。
            identity = f'CASE WHEN {missing} THEN NULL ELSE {target_alias}.{target.identity} END'
            parent_id = f'{target_alias}.{target.foreign_key}' if target.parent else 'NULL'
            branches.append(f'SELECT {identity}, {parent_id}'
                            ' FROM report_entity_changes c INDEXED BY report_changes_by_sequence '
                            + ' '.join(joins) + ' WHERE ' + self._window_sql())
            parameters.extend(self._window(source))
        # SQL 去重后只移交有限小记录；归属列在对象创建后不变。
        if len(branches) == 1:
            branches[0] = branches[0].replace('SELECT ', 'SELECT DISTINCT ', 1)
        return ' UNION '.join(branches) + ' ORDER BY 1 LIMIT ?', (*parameters, self._remaining + 1)

    def _page_sql(self, target, parent_id, after):
        branches, parameters = [], []
        for path in self._paths(target):
            tables, conditions = [], []
            parent_alias = 'r'
            for index in range(len(path) - 2, -1, -1):
                child, parent = path[index], path[index + 1]
                alias = f'e{index}'
                tables.append(f'{child.table} {alias}')
                conditions.append(f'{alias}.{child.foreign_key}={parent_alias}.{parent.identity}')
                parent_alias = alias
            tables.append('report_entity_changes c INDEXED BY report_changes_by_entity')
            source = path[0]
            conditions.append(f'c.entity_id={parent_alias}.{source.identity}')
            conditions.append(self._window_sql())
            # 固定外层实体后沿归属索引查后代，避免对每个父对象重扫窗口。
            branches.append('SELECT 1 FROM ' + ' CROSS JOIN '.join(tables) + ' WHERE ' + ' AND '.join(conditions))
            parameters.extend(self._window(source))
        parent_filter = f' AND r.{target.foreign_key}=?' if target.parent else ''
        outer = [after] + ([parent_id] if target.parent else [])
        sql = (f'SELECT r.{target.identity} FROM {target.table} r WHERE r.{target.identity}>?'
               + parent_filter + ' AND EXISTS (' + ' UNION ALL '.join(branches) + ')'
               f' ORDER BY r.{target.identity} LIMIT ?')
        return sql, (*outer, *parameters, self.request.entity_batch_size + 1)

    def read_page(
        self, entity: str, *, parent_id: int | None = None,
        cursor: ReportEntityCursor | None = None,
    ) -> Page[int, ReportEntityCursor]:
        """交付按整数 ID 排序的一页；游标为空表示开始，返回空游标才结束。"""
        self._active()
        try:
            target = self._entities.get(entity)
            if target is None:
                raise ConsistencyError('未登记的报告入选实体')
            if (target.parent is None) != (parent_id is None):
                raise ConsistencyError('报告实体与父对象角色不符')
            expected = ReportEntityCursor(self.instance_id, self.request.boundary,
                self.request.from_wm, self.request.to_wm, entity, parent_id, 0)
            if cursor is not None:
                if not isinstance(cursor, ReportEntityCursor) or cursor.identity != expected.identity:
                    raise ConsistencyError('报告游标的 instance/H/窗口/父对象/实体不符')
            after = 0 if cursor is None else cursor.after_id
            with self._repository._read_connection() as connection:
                if self._repository._instance_id != self.instance_id:
                    raise ConsistencyError('报告会话的数据库实例已改变')
                if entity not in self._cache:
                    sql, parameters = self._candidates_sql(target)
                    with closing(connection.execute(sql, parameters)) as reader:
                        candidates = reader.fetchall()
                    if any(row[0] is None for row in candidates):
                        raise ConsistencyError('报告目录缺少必要主对象或祖先')
                    if len(candidates) <= self._remaining:
                        self._cache[entity] = tuple(candidates)
                        self._remaining -= len(candidates)
                    else:
                        self._cache[entity] = None
                    # 超预算分支在读页/交付第一项前释放整批探测记录。
                    del candidates
                cached = self._cache[entity]
                if cached is None:
                    sql, parameters = self._page_sql(target, parent_id, after)
                    with closing(connection.execute(sql, parameters)) as reader:
                        rows = reader.fetchall()
                    identities = tuple(row[0] for row in rows)
                else:
                    identities = tuple(islice((row[0] for row in cached
                        if row[0] > after and row[1] == parent_id), self.request.entity_batch_size + 1))
            limit = self.request.entity_batch_size
            items = identities[:limit]
            continuation = (ReportEntityCursor(self.instance_id, self.request.boundary,
                self.request.from_wm, self.request.to_wm, entity, parent_id, items[-1])
                if len(identities) > limit else None)
            return Page(items, continuation)
        except BaseException:
            self.close()
            raise
