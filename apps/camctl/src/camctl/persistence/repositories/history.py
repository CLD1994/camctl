"""历史仓储：固定 H 的全局事件分页与报告候选查询。

全局事件页在同一短读事务内核实 H 和事务分组，转换为独立数据并
在返回前释放游标与连接；继续位置沿规定排序严格推进。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import closing, contextmanager
from collections.abc import Iterator

from camctl.contracts.history_values import (
    BoundaryError, HistoryBoundary, INITIAL_BOUNDARY, ReadOrder, ReadScope, TransactionRange, validate_page,
)
from camctl.contracts.pages import Page
from camctl.contracts.json_values import parse_exact_json, JsonParseError
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID
from camctl.acceptance.definitions import read_action_spec
from camctl.history.events import EventEnvelope
from camctl.history.decoding import decode_event_row
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

__all__ = ["HistoryRepository"]


class HistoryRepository:
    """真实 SQLite 的历史读取仓储（报告进程使用只读连接）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._instance_id: str | None = None
        # 不可变历史允许复用已核验的组；只保留固定 H 和上一页末组。
        self._validated_ranges: dict[int, TransactionRange] = {}

    def _connect(self) -> sqlite3.Connection:
        owned = open_existing(self.path, DbOpenMode.EXISTING_RO, DbConfig())
        return owned.connection

    def current_boundary(self) -> HistoryBoundary:
        with self._read_connection() as connection:
            return self._boundary(connection)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with closing(connection.execute("BEGIN")):
                pass
            instance_id = _one(connection, "SELECT instance_id FROM database_metadata")[0]
            if self._instance_id is None:
                self._instance_id = instance_id
            elif self._instance_id != instance_id:
                raise ConsistencyError("历史读取的数据库实例身份发生变化")
            yield connection
            with closing(connection.execute("COMMIT")):
                pass
        except BaseException:
            self._validated_ranges.clear()
            raise
        finally:
            connection.close()

    @staticmethod
    def _boundary(connection: sqlite3.Connection) -> HistoryBoundary:
        row = _one(connection,
            "SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
        )
        latest = _one(connection, "SELECT MAX(id) FROM history_events")[0]
        if row is None:
            if latest is not None:
                raise ConsistencyError("初始化历史边界存在未归组事件")
            return INITIAL_BOUNDARY
        try:
            boundary = HistoryBoundary(row[0], row[1])
        except BoundaryError as error:
            raise ConsistencyError("当前历史边界无效") from error
        if latest != boundary.last_event_id:
            raise ConsistencyError("当前历史事务边界与末事件不一致")
        _transaction_range(connection, boundary.txn_id)
        return boundary

    def read_events(
        self, scope: ReadScope[int], boundary: HistoryBoundary
    ) -> Page[EventEnvelope, int]:
        """按固定范围分批读取事件信封；结果独立拥有，游标严格推进。"""
        lower, upper = _event_range(scope, boundary)
        if scope.order is ReadOrder.ASCENDING:
            direction, operator = "ASC", ">"
        elif scope.order is ReadOrder.DESCENDING:
            direction, operator = "DESC", "<"
        else:
            raise BoundaryError(f"排序必须是 ReadOrder 成员: {scope.order!r}")
        with self._read_connection() as connection:
            checked: dict[int, TransactionRange] = {}
            if boundary != INITIAL_BOUNDARY:
                transaction = _transaction_range(connection, boundary.txn_id,
                                                 self._validated_ranges.get(boundary.txn_id))
                if transaction.last_event_id != boundary.last_event_id:
                    raise ConsistencyError("请求的 H 不是该历史事务的完整结束位置")
                checked[boundary.txn_id] = transaction
            conditions = ["id >= ?", "id <= ?"]
            parameters = [scope.lower_position, upper]
            if scope.previous_position is not None:
                conditions.append(f"id {operator} ?")
                parameters.append(scope.previous_position)
            with closing(connection.execute(
                "SELECT id, transaction_id, event_type, event_version, occurred_at,"
                " clock_status, change_seq, body_json FROM history_events"
                f" WHERE {' AND '.join(conditions)} ORDER BY id {direction} LIMIT ?",
                (*parameters, scope.batch_limit),
            )) as cursor:
                rows = cursor.fetchall()
            start, end = lower, upper
            if scope.previous_position is not None:
                if scope.order is ReadOrder.ASCENDING:
                    start = max(start, scope.previous_position + 1)
                else:
                    end = min(end, scope.previous_position - 1)
            expected_count = min(scope.batch_limit, max(0, end - start + 1))
            positions = (range(start, start + expected_count) if scope.order is ReadOrder.ASCENDING
                         else range(end, end - expected_count, -1))
            if [row[0] for row in rows] != list(positions):
                raise ConsistencyError("全局历史事件页缺少必要位置或顺序不一致")
            for row in rows:
                if row[1] not in checked:
                    checked[row[1]] = _transaction_range(connection, row[1],
                                                        self._validated_ranges.get(row[1]))
                transaction = checked[row[1]]
                if not transaction.first_event_id <= row[0] <= transaction.last_event_id:
                    raise ConsistencyError("事件不属于所登记的历史事务范围")
            items = tuple(decode_event_row(row) for row in rows)
            has_more = len(rows) == scope.batch_limit and (
                rows[-1][0] < upper if scope.order is ReadOrder.ASCENDING
                else rows[-1][0] > lower
            )
            page = Page(
                items=items,
                next_cursor=int(rows[-1][0]) if has_more else None,
            )
            validate_page(page, scope)
            self._validated_ranges = ({txn_id: checked[txn_id]
                                       for txn_id in {boundary.txn_id, rows[-1][1]}}
                                      if has_more else {})
            return page

    def select_report_entities(
        self,
        *,
        entity_type: int,
        from_wm: int,
        to_wm: int,
        after_id: int,
        limit: int,
    ) -> list[int]:
        """按业务水位窗口与对象 ID 续读选择报告目标。"""
        if limit < 1:
            raise ValueError(f"批量上限必须是正整数: {limit}")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT DISTINCT entity_id FROM report_entity_changes"
                " WHERE entity_type = ? AND entity_id > ?"
                " AND +change_seq > ? AND +change_seq <= ?"
                " ORDER BY entity_id LIMIT ?",
                (entity_type, after_id, from_wm, to_wm, limit),
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            connection.close()

    def entity_facts(
        self, entity_type: int, entity_ids: tuple[int, ...]
    ) -> dict[str, dict[int, dict]]:
        """取得对象自身与其关联表在当前投影的行事实（报告生成用）。"""
        if not entity_ids:
            return {}
        connection = self._connect()
        try:
            tables: dict[str, dict[int, dict]] = {}
            for table in (
                "plans", "actions", "outputs", "deliveries",
                "plan_file_diagnostics", "device_activities", "auto_preview_links",
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table}"
                ).fetchall()
                names = [d[0] for d in connection.execute(f"SELECT * FROM {table} LIMIT 0").description]
                for row in rows:
                    values = dict(zip(names, row))
                    row_id = int(values.get("id", 0))
                    decoded = _decode_row(values)
                    if table == "actions":
                        read_action_spec(decoded)
                    tables.setdefault(table, {})[row_id] = decoded
            return tables
        finally:
            connection.close()


def _decode_row(values: dict) -> dict:
    decoded = dict(values)
    for key, value in list(decoded.items()):
        if isinstance(value, str) and (key.endswith("_json") or key == "body_json"):
            try:
                decoded[key] = parse_exact_json(value)
            except JsonParseError as error:
                raise ConsistencyError(f"历史投影字段 {key} 不是有效精确 JSON") from error
    return decoded


def _one(connection, statement, parameters=()):
    with closing(connection.execute(statement, parameters)) as cursor:
        return cursor.fetchone()


def _transaction_range(connection, transaction_id: int,
                       validated: TransactionRange | None = None) -> TransactionRange:
    row = _one(connection,
               "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE id = ?",
               (transaction_id,))
    if row is None:
        raise ConsistencyError(f"历史事务 {transaction_id} 不存在")
    try:
        transaction = TransactionRange(*row)
    except BoundaryError as error:
        raise ConsistencyError(f"历史事务 {transaction_id} 的范围无效") from error
    previous = (_one(connection, "SELECT last_event_id FROM history_transactions WHERE id = ?",
                     (transaction_id - 1,)) if transaction_id > 1 else (0,))
    if previous is None or previous[0] + 1 != transaction.first_event_id:
        raise ConsistencyError(f"历史事务 {transaction_id} 与前一完整边界不连续")
    if validated is not None:
        if transaction != validated:
            raise ConsistencyError(f"已核验历史事务 {transaction_id} 的范围发生变化")
        return transaction
    count, first, last = _one(connection,
        "SELECT COUNT(*), MIN(id), MAX(id) FROM history_events WHERE transaction_id = ?",
        (transaction_id,))
    if (count != transaction.last_event_id - transaction.first_event_id + 1
            or first != transaction.first_event_id or last != transaction.last_event_id):
        raise ConsistencyError(f"历史事务 {transaction_id} 的成员与首尾范围不一致")
    return transaction


def _event_range(scope: ReadScope[int], boundary: HistoryBoundary) -> tuple[int, int]:
    if not isinstance(boundary, HistoryBoundary):
        raise BoundaryError("事件读取必须指定完整历史边界")
    for name in ("lower_position", "upper_position", "previous_position"):
        value = getattr(scope, name)
        if value is None and name != "lower_position":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OBJECT_ID:
            raise BoundaryError(f"{name} 必须是范围内的整数事件位置")
    upper = boundary.last_event_id if scope.upper_position is None else scope.upper_position
    if upper > boundary.last_event_id:
        raise BoundaryError("事件读取上界超过指定完整历史边界")
    if (scope.previous_position is not None
            and not scope.lower_position <= scope.previous_position <= upper):
        raise BoundaryError("事件继续位置不属于固定读取范围")
    return max(1, scope.lower_position), upper
