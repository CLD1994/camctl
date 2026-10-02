"""在调用方的可靠写事务内恢复一行的限定业务列。

读取固定 (H, C] 的对象目录，保留有容量限制的批次和最近事务核验。
调用方沿只读关联确定归属并在同一事务取得 C 时的行值；结果不是
完整对象镜像，不执行业务守卫、设备操作或新写入。
"""

from contextlib import closing
import sqlite3
from typing import Any, Mapping

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.decoding import decode_event_row
from camctl.history.events import business_columns, HistoryEventError
from camctl.history.replay import ReplayError, reverse_row_values
from camctl.persistence.transaction import read_transaction_range

_BATCH_LIMIT = 128


def _one(connection, sql, parameters):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def _anchor(row, name):
    if row is None or len(row) != 2:
        raise ConsistencyError(f"{name} 缺少可靠末事件与累计次数")
    try:
        for value in row:
            ObjectId(value)
    except ValueError as error:
        raise ConsistencyError(f"{name} 的末事件或累计次数无效") from error
    return row


def read_row_values_at_boundary(
    connection: sqlite3.Connection, *, owner: tuple[str, int], table: str, row_id: int,
    columns: frozenset[str], current_values: Mapping[str, Any],
    boundary: HistoryBoundary, current_boundary: HistoryBoundary,
) -> dict[str, Any]:
    """核实完整 H/C 和目录后返回 H 时存在行的指定列，游标全部释放。"""
    if not connection.in_transaction:
        raise ConsistencyError("限定行恢复要求调用方持有可靠事务")
    try:
        ObjectId(row_id)
        ObjectId(owner[1])
        allowed = business_columns(table)
    except (ValueError, HistoryEventError) as error:
        raise ConsistencyError("限定行恢复的身份或业务表无效") from error
    if not columns or not columns <= allowed or not columns <= current_values.keys():
        raise ConsistencyError("请求列必须是拥有可靠当前值的非空业务列集合")
    if (boundary.txn_id > current_boundary.txn_id
            or boundary.last_event_id > current_boundary.last_event_id):
        raise ConsistencyError("请求的历史边界晚于可靠当前边界")
    recent = None
    for value in (boundary, current_boundary):
        if value.txn_id == 0:
            continue
        recent = read_transaction_range(connection, value.txn_id)
        if recent.last_event_id != value.last_event_id:
            raise ConsistencyError("限定行恢复的 H 或 C 不是完整事务边界")
    entity = load_enum_registry()["history_objects"].get(owner[0])
    if entity is None:
        raise ConsistencyError(f"限定行恢复的对象种类 {owner[0]} 未登记")
    ref = (entity["id"], owner[1])
    primary = _anchor(_one(connection,
        f"SELECT last_event_id, change_count FROM {entity['table']} WHERE id = ?", (owner[1],)), "当前对象")
    head = _anchor(_one(connection,
        "SELECT event_id, change_count FROM entity_event_links"
        " WHERE entity_type = ? AND entity_id = ? ORDER BY event_id DESC LIMIT 1", ref), "对象目录")
    if primary != head or head[0] > current_boundary.last_event_id:
        raise ConsistencyError("当前对象的末事件与次数不符合可靠目录头及 C")
    floor_row = _one(connection,
        "SELECT event_id, change_count FROM entity_event_links"
        " WHERE entity_type = ? AND entity_id = ? AND event_id <= ? ORDER BY event_id DESC LIMIT 1",
        (*ref, boundary.last_event_id))
    floor = (0, 0) if floor_row is None else _anchor(floor_row, "H 的对象目录位置")
    if (floor[0] > boundary.last_event_id or floor[1] > head[1]
            or (floor[1] == head[1]) != (floor[0] == head[0])):
        raise ConsistencyError("H 的目录位置与当前对象目录矛盾")
    restored = {name: current_values[name] for name in columns}
    expected_count = head[1]
    previous = current_boundary.last_event_id + 1
    while expected_count > floor[1]:
        with closing(connection.execute(
            "SELECT l.change_count, e.id, e.transaction_id, e.event_type, e.event_version,"
            " e.occurred_at, e.clock_status, e.change_seq, e.body_json FROM entity_event_links AS l"
            " LEFT JOIN history_events AS e ON e.id = l.event_id"
            " WHERE l.entity_type = ? AND l.entity_id = ? AND l.event_id > ?"
            " AND l.event_id <= ? AND l.event_id < ? ORDER BY l.event_id DESC LIMIT ?",
            (*ref, boundary.last_event_id, current_boundary.last_event_id, previous, _BATCH_LIMIT),
        )) as cursor:
            rows = cursor.fetchall()
        if not rows or len(rows) > _BATCH_LIMIT:
            raise ConsistencyError("对象历史目录缺少必要事件或批量范围无效")
        for stored in rows:
            event = decode_event_row(stored[1:])
            if (stored[0] != expected_count or not boundary.last_event_id < event.event_id < previous
                    or not boundary.txn_id < event.transaction_id <= current_boundary.txn_id):
                raise ConsistencyError("对象历史目录的事件位置、事务或累计次数不连续")
            recent = read_transaction_range(connection, event.transaction_id,
                recent if recent is not None and recent.txn_id == event.transaction_id else None)
            if not recent.first_event_id <= event.event_id <= recent.last_event_id:
                raise ConsistencyError("对象历史事件不属于其完整事务范围")
            for change in event.rows:
                if (change.table, change.row_id) == (table, row_id):
                    try:
                        restored = reverse_row_values(restored, change, columns)
                    except ReplayError as error:
                        raise ConsistencyError(f"限定行 {table}#{row_id} 的恢复依据不连续") from error
            expected_count -= 1
            previous = event.event_id
        if expected_count < floor[1]:
            raise ConsistencyError("对象历史目录越过 H 的可靠累计次数")
    return restored
