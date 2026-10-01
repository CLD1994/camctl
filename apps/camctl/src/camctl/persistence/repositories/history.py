"""历史短读事务仓储：固定 H 分批读取与候选续读。

同一短读事务内取得投影与绑定 C，转换为独立数据并在返回前结束
游标与读事务；游标沿规定排序严格推进。仅在可靠确认范围结束时
返回空继续位置。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from camctl.contracts.history_values import BoundaryError, HistoryBoundary, ReadOrder, ReadScope, validate_page
from camctl.contracts.pages import Page
from camctl.contracts.json_values import parse_exact_json, JsonParseError
from camctl.contracts.values import ConsistencyError
from camctl.acceptance.definitions import read_action_spec
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

__all__ = ["HistoryRepository"]


class HistoryRepository:
    """真实 SQLite 的历史读取仓储（报告进程使用只读连接）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        owned = open_existing(self.path, DbOpenMode.EXISTING_RO, DbConfig())
        return owned.connection

    def current_boundary(self) -> HistoryBoundary:
        connection = self._connect()
        try:
            return self._boundary(connection)
        finally:
            connection.close()

    @staticmethod
    def _boundary(connection: sqlite3.Connection) -> HistoryBoundary:
        from camctl.contracts.history_values import INITIAL_BOUNDARY

        row = connection.execute(
            "SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return INITIAL_BOUNDARY
        return HistoryBoundary(txn_id=int(row[0]), last_event_id=int(row[1]))

    def read_events(
        self, scope: ReadScope[int], boundary: HistoryBoundary
    ) -> Page[EventEnvelope, int]:
        """按固定范围分批读取事件信封；结果独立拥有，游标严格推进。"""
        if scope.batch_limit < 1:
            raise ValueError(f"批量上限必须是正整数: {scope.batch_limit}")
        if scope.order is ReadOrder.ASCENDING:
            direction, operator = "ASC", ">"
        elif scope.order is ReadOrder.DESCENDING:
            direction, operator = "DESC", "<"
        else:
            raise BoundaryError(f"排序必须是 ReadOrder 成员: {scope.order!r}")
        connection = self._connect()
        try:
            upper = boundary.last_event_id if scope.upper_position is None else min(
                scope.upper_position, boundary.last_event_id
            )
            conditions = ["id >= ?", "id <= ?"]
            parameters = [scope.lower_position, upper]
            if scope.previous_position is not None:
                conditions.append(f"id {operator} ?")
                parameters.append(scope.previous_position)
            rows = connection.execute(
                "SELECT id, transaction_id, event_type, event_version, occurred_at,"
                " clock_status, change_seq, body_json FROM history_events"
                f" WHERE {' AND '.join(conditions)} ORDER BY id {direction} LIMIT ?",
                (*parameters, scope.batch_limit),
            ).fetchall()
            items = tuple(_envelope_from_row(row) for row in rows)
            has_more = len(rows) == scope.batch_limit and (
                rows[-1][0] < upper if scope.order is ReadOrder.ASCENDING
                else rows[-1][0] > scope.lower_position
            )
            page = Page(
                items=items,
                next_cursor=int(rows[-1][0]) if has_more else None,
            )
            validate_page(page, scope)
            return page
        finally:
            connection.close()

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


def _envelope_from_row(row) -> EventEnvelope:
    body = parse_exact_json(row[7])
    rows = tuple(
        RowChange(
            table=item["table"],
            row_id=item["id"],
            before=RowImage(
                exists=item["before"]["exists"], values=item["before"]["values"]
            ),
            after=RowImage(
                exists=item["after"]["exists"], values=item["after"]["values"]
            ),
        )
        for item in body["rows"]
    )
    return EventEnvelope(
        event_id=int(row[0]),
        transaction_id=int(row[1]),
        event_type=int(row[2]),
        event_version=int(row[3]),
        occurred_at=int(row[4]),
        clock_status=int(row[5]),
        change_seq=int(row[6]) if row[6] is not None else None,
        reason=body["reason"],
        evidence=body.get("evidence", {}),
        rows=rows,
    )
