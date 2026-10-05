"""历史仓储：固定 H 的全局事件分页、报告范围选择与对象恢复。

全局事件页在同一短读事务内核实 H 和事务分组，转换为独立数据并
在返回前释放游标与连接；继续位置沿规定排序严格推进。报告范围
按业务水位窗口分页选择并沿归属外键补齐父对象；对象恢复在同一
短读事务内联合读取当前投影与 C，按对象目录逆向恢复到 H。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from contextlib import closing, contextmanager
from collections.abc import Iterator, Mapping, Sequence

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import (
    BoundaryError, HistoryBoundary, INITIAL_BOUNDARY, ReadOrder, ReadScope, TransactionRange, validate_page,
)
from camctl.contracts.pages import Page
from camctl.contracts.json_values import parse_exact_json, JsonParseError
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId
from camctl.acceptance.definitions import read_action_spec
from camctl.history.events import EventEnvelope, load_event_registry
from camctl.history.decoding import decode_event_row
from camctl.history.queries import (
    ReportScope,
    ReportScopeRequest,
    build_report_scope,
    report_target_types,
)
from camctl.history.replay import ReplayError, reverse_row_values
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import read_transaction_range as _transaction_range

__all__ = ["HistoryRepository"]


@dataclass(frozen=True)
class _FrozenRegistration:
    """一份报告在冻结事务中登记的窗口依据与冻结边界。"""

    from_wm: int
    to_wm: int
    boundary: HistoryBoundary


class HistoryRepository:
    """真实 SQLite 的历史读取仓储（报告进程使用只读连接）。"""

    def __init__(self, path: Path, *, config: DbConfig | None = None) -> None:
        self.path = Path(path)
        self._config = config if config is not None else DbConfig()
        self._instance_id: str | None = None
        # 不可变历史允许复用已核验的组；只保留固定 H 和上一页末组。
        self._validated_ranges: dict[int, TransactionRange] = {}

    def _connect(self) -> sqlite3.Connection:
        owned = open_existing(self.path, DbOpenMode.EXISTING_RO, self._config)
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

    def frozen_registration(self, report_id: int) -> "_FrozenRegistration":
        """读取一份报告的冻结依据并核实其冻结边界完整。

        frozen_event_id 必须是其所在历史事务的末事件；否则该位置
        不是完整已提交边界，按状态库一致性错误处理。
        """
        ObjectId(report_id)
        connection = self._connect()
        try:
            with closing(connection.execute(
                    "SELECT from_wm, to_wm, frozen_event_id, format_version"
                    " FROM reports WHERE id = ?", (report_id,))) as cursor:
                row = cursor.fetchone()
            if row is None:
                raise ConsistencyError(f"报告 {report_id} 未登记")
            if int(row[3]) != 1:
                raise ConsistencyError(f"报告 {report_id} 的格式版本不受支持")
            frozen_event_id = int(row[2])
            with closing(connection.execute(
                    "SELECT transaction_id FROM history_events WHERE id = ?",
                    (frozen_event_id,))) as cursor:
                event_row = cursor.fetchone()
            if event_row is None:
                raise ConsistencyError(
                    f"报告 {report_id} 的冻结事件不在已提交历史中")
            transaction = _transaction_range(connection, int(event_row[0]))
            if transaction.last_event_id != frozen_event_id:
                raise ConsistencyError(
                    f"报告 {report_id} 的冻结位置不是完整已提交边界")
            return _FrozenRegistration(
                from_wm=int(row[0]), to_wm=int(row[1]),
                boundary=HistoryBoundary(transaction.txn_id, frozen_event_id))
        finally:
            connection.close()

    def select_report_scope(self, request: ReportScopeRequest) -> ReportScope:
        """选择一份报告在固定窗口内的全部入选对象并补齐父对象。

        各报告目标类型按对象 ID 游标分页读取至集合结束；归属外键
        来自当前投影（计划归属在创建后不变）。窗口内容只由已提交
        历史决定，后续提交不改变既有窗口。
        """
        types = report_target_types()
        selected: dict[str, list[int]] = {name: [] for name in types}
        connection = self._connect()
        try:
            for name, type_id in types.items():
                after = 0
                while True:
                    page = self.select_report_entities(
                        entity_type=type_id, from_wm=request.from_wm,
                        to_wm=request.to_wm, after_id=after,
                        limit=request.entity_batch_size)
                    selected[name].extend(page)
                    if len(page) < request.entity_batch_size:
                        break
                    after = page[-1]
            action_plan = self._foreign_keys(
                connection, "actions", "plan_id", selected["action"])
            output_action = self._foreign_keys(
                connection, "outputs", "source_action_id", selected["output"])
            delivery_action = self._foreign_keys(
                connection, "deliveries", "action_id", selected["delivery"])
            # 产物与交付的父动作可能是仅被补齐的动作：补充其归属查询。
            parent_actions = sorted(
                set(output_action.values()) | set(delivery_action.values()))
            missing = [action_id for action_id in parent_actions
                       if action_id not in action_plan]
            if missing:
                marks = ",".join("?" * len(missing))
                with closing(connection.execute(
                        f"SELECT id, plan_id FROM actions WHERE id IN ({marks})",
                        tuple(missing))) as cursor:
                    for row in cursor.fetchall():
                        action_plan[int(row[0])] = int(row[1])
        finally:
            connection.close()
        return build_report_scope(
            plans=selected["plan"], actions=selected["action"],
            outputs=selected["output"], deliveries=selected["delivery"],
            diagnostics=selected["diagnostic"],
            action_plan=action_plan, output_action=output_action,
            delivery_action=delivery_action)

    @staticmethod
    def _foreign_keys(
        connection: sqlite3.Connection, table: str, column: str,
        identities: Sequence[int],
    ) -> dict[int, int]:
        if not identities:
            return {}
        marks = ",".join("?" * len(identities))
        with closing(connection.execute(
                f"SELECT id, {column} FROM {table} WHERE id IN ({marks})",
                tuple(identities))) as cursor:
            rows = cursor.fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    def restore_entity(
        self, entity: str, entity_id: int, boundary: HistoryBoundary,
        *, event_batch_size: int = 128,
    ) -> dict[tuple[str, int], dict]:
        """恢复一个历史对象在固定 H 的自身行（当前投影逆向恢复）。

        同一短读事务内取得当前投影与 C；随后按对象目录自 C 逆向处
        理 (H, C] 的关联事件。H 之后创建的行不进入结果；正逆恢复
        不改变真实投影。读取批次只影响分页次数，不影响结果。
        """
        ObjectId(entity_id)
        if event_batch_size < 1:
            raise ValueError(f"事件批量必须是正整数: {event_batch_size}")
        spec = load_enum_registry()["history_objects"].get(entity)
        if spec is None:
            raise ConsistencyError(f"未知历史对象类型: {entity!r}")
        with self._read_connection() as connection:
            current_boundary = self._boundary(connection)
            if (boundary.txn_id > current_boundary.txn_id
                    or boundary.last_event_id > current_boundary.last_event_id):
                raise ConsistencyError("恢复目标边界晚于可靠当前边界")
            seeded = self._seed_entity_rows(connection, spec, entity_id)
            if not seeded:
                raise ConsistencyError(
                    f"对象 {entity}#{entity_id} 在当前投影中不存在")
            rows = self._reverse_to_boundary(
                connection,
                ref=(spec["id"], entity_id), primary_table=spec["table"],
                seeded=seeded, boundary=boundary,
                current_boundary=current_boundary,
                event_batch_size=event_batch_size)
            return rows

    def _seed_entity_rows(
        self, connection: sqlite3.Connection, spec: dict, entity_id: int,
    ) -> dict[tuple[str, int], dict]:
        """取得对象在当前投影的自身行（报告字段相关的归属表）。"""
        tables: dict[tuple[str, int], dict] = {}
        for table, row_id in self._entity_row_ids(
            connection, spec, entity_id
        ):
            tables[(table, row_id)] = _table_row(connection, table, row_id)
        return tables

    @staticmethod
    def _entity_row_ids(
        connection: sqlite3.Connection, spec: dict, entity_id: int,
    ) -> Iterator[tuple[str, int]]:
        """枚举对象自身行的 (表, 行 ID)：主表行与归属子表行。"""
        name = spec["table"]
        if name == "actions":
            yield from _ids_where(connection, "actions", "id", entity_id)
            action = entity_id
            yield from _ids_where(connection, "device_activities", "action_id", action)
            yield from _ids_where(connection, "recording_processing", "action_id", action)
            yield from _ids_where(connection, "cleanup_items", "action_id", action)
            yield from _ids_where(connection, "cancel_items", "action_id", action)
            yield from _ids_where(connection, "auto_preview_links", "obtain_action_id", action)
            dependencies = _ids_where(connection, "action_dependencies", "action_id", action)
            yield from dependencies
            selection_ids: list[int] = []
            for _, dependency_id in dependencies:
                selection_ids.extend(_ids_where(
                    connection, "obtain_source_selections", "dependency_id", dependency_id,
                    ids_only=True))
            yield from [("obtain_source_selections", selection_id)
                       for selection_id in selection_ids]
            for selection_id in selection_ids:
                yield from _ids_where(
                    connection, "obtain_items", "selection_id", selection_id)
            for _, cancel_item_id in _ids_where(connection, "cancel_items", "action_id", action):
                yield from _ids_where(
                    connection, "cancel_delivery_items", "cancel_item_id", cancel_item_id)
            # 取消成员的目标动作行：cancel_item 投影经 cancel_target
            # 关联读取目标类型，目标可能属于其他计划。
            with closing(connection.execute(
                    "SELECT target_action_id FROM cancel_items"
                    " WHERE action_id = ?", (action,))) as cursor:
                target_actions = {int(row[0]) for row in cursor.fetchall()}
            for target in sorted(target_actions):
                yield from _ids_where(connection, "actions", "id", target)
        elif name == "outputs":
            yield from _ids_where(connection, "outputs", "id", entity_id)
            yield from _ids_where(connection, "output_origins", "output_id", entity_id)
        elif name == "deliveries":
            yield from _ids_where(connection, "deliveries", "id", entity_id)
            yield from _ids_where(connection, "file_copies", "delivery_id", entity_id)
        else:
            yield from _ids_where(connection, name, "id", entity_id)

    def related_entity_ids(
        self, table: str, column: str, value: int,
    ) -> list[int]:
        """按归属列枚举关联对象的行 ID（如动作的同步记录）。"""
        connection = self._connect()
        try:
            with closing(connection.execute(
                    f"SELECT id FROM {table} WHERE {column} = ?", (value,))) as cursor:
                return [int(row[0]) for row in cursor.fetchall()]
        finally:
            connection.close()

    def _reverse_to_boundary(
        self, connection: sqlite3.Connection, *, ref: tuple[int, int],
        primary_table: str, seeded: Mapping[tuple[str, int], dict],
        boundary: HistoryBoundary, current_boundary: HistoryBoundary,
        event_batch_size: int,
    ) -> dict[tuple[str, int], dict]:
        """按对象目录自 C 逆向恢复到 H；核对锚点与目录连续性。"""
        from camctl.persistence.row_history import _anchor

        derived = load_event_registry()["tables"][primary_table]["derived"]
        anchored = "last_event_id" in derived
        counted = "change_count" in derived
        head = _anchor(_one(connection,
            "SELECT event_id, change_count FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = ? ORDER BY event_id DESC LIMIT 1", ref),
            "对象目录")
        if head[0] > current_boundary.last_event_id:
            raise ConsistencyError("对象目录头超出可靠当前边界")
        if anchored:
            # 主表声明可靠末事件的对象额外核对表内锚点与目录头一致；
            # 不声明锚点的对象（不可变诊断行、同步责任行等）以对象
            # 目录为唯一位置依据。
            names = "last_event_id, change_count" if counted else "last_event_id"
            primary = _one(connection,
                           f"SELECT {names} FROM {primary_table} WHERE id = ?", (ref[1],))
            if counted:
                primary = _anchor(primary, "当前对象")
            else:
                if primary is None or len(primary) != 1:
                    raise ConsistencyError("当前对象缺少可靠末事件")
                try:
                    ObjectId(primary[0])
                except ValueError as error:
                    raise ConsistencyError("当前对象的末事件无效") from error
            if (primary != head if counted else primary[0] != head[0]):
                raise ConsistencyError("当前对象的末事件与次数不符合可靠目录头及 C")
        floor_row = _one(connection,
            "SELECT event_id, change_count FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = ? AND event_id <= ?"
            " ORDER BY event_id DESC LIMIT 1",
            (*ref, boundary.last_event_id))
        floor = (0, 0) if floor_row is None else _anchor(floor_row, "H 的对象目录位置")
        if (floor[0] > boundary.last_event_id or floor[1] > head[1]
                or (floor[1] == head[1]) != (floor[0] == head[0])):
            raise ConsistencyError("H 的目录位置与当前对象目录矛盾")

        rows: dict[tuple[str, int], dict] = {
            key: dict(values) for key, values in seeded.items()}
        expected_count = head[1]
        previous = current_boundary.last_event_id + 1
        recent: TransactionRange | None = None
        while expected_count > floor[1]:
            with closing(connection.execute(
                "SELECT l.change_count, e.id, e.transaction_id, e.event_type,"
                " e.event_version, e.occurred_at, e.clock_status, e.change_seq,"
                " e.body_json FROM entity_event_links AS l"
                " LEFT JOIN history_events AS e ON e.id = l.event_id"
                " WHERE l.entity_type = ? AND l.entity_id = ? AND l.event_id > ?"
                " AND l.event_id <= ? AND l.event_id < ?"
                " ORDER BY l.event_id DESC LIMIT ?",
                (*ref, boundary.last_event_id, current_boundary.last_event_id,
                 previous, event_batch_size),
            )) as cursor:
                stored_rows = cursor.fetchall()
            if not stored_rows or len(stored_rows) > event_batch_size:
                raise ConsistencyError("对象历史目录缺少必要事件或批量范围无效")
            for stored in stored_rows:
                event = decode_event_row(stored[1:])
                if (stored[0] != expected_count
                        or not boundary.last_event_id < event.event_id < previous
                        or not boundary.txn_id < event.transaction_id <= current_boundary.txn_id):
                    raise ConsistencyError("对象历史目录的事件位置、事务或累计次数不连续")
                recent = _transaction_range(
                    connection, event.transaction_id,
                    recent if recent is not None and recent.txn_id == event.transaction_id else None)
                if not recent.first_event_id <= event.event_id <= recent.last_event_id:
                    raise ConsistencyError("对象历史事件不属于其完整事务范围")
                for change in event.rows:
                    key = (change.table, change.row_id)
                    target = rows.get(key)
                    if target is None:
                        continue
                    if not change.before.exists:
                        # 创建于 (H, C]：目标边界处该行尚不存在。
                        del rows[key]
                        continue
                    if not change.after.exists:
                        raise ConsistencyError(
                            f"行 {key} 的更新事件在保存后不存在，无法逆向恢复")
                    try:
                        rows[key] = reverse_row_values(
                            target, change, frozenset(target))
                    except ReplayError as error:
                        raise ConsistencyError(
                            f"对象 {ref} 的行 {key} 恢复依据不连续") from error
                expected_count -= 1
                previous = event.event_id
            if expected_count < floor[1]:
                raise ConsistencyError("对象历史目录越过 H 的可靠累计次数")
        return rows

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


def _ids_where(
    connection: sqlite3.Connection, table: str, column: str, value: int,
    *, ids_only: bool = False,
) -> list[tuple[str, int]] | list[int]:
    """按归属外键枚举子表行身份（报告字段相关归属表）。"""
    with closing(connection.execute(
            f"SELECT id FROM {table} WHERE {column} = ?", (value,))) as cursor:
        rows = [int(row[0]) for row in cursor.fetchall()]
    return rows if ids_only else [(table, row_id) for row_id in rows]


def _table_row(connection: sqlite3.Connection, table: str, row_id: int) -> dict:
    """取得并解码一行当前投影（业务列与 JSON 列结构化）。"""
    with closing(connection.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"当前投影缺少 {table}#{row_id}")
    names = [description[0] for description in cursor.description]
    return _decode_row(dict(zip(names, row)))


def _one(connection, statement, parameters=()):
    with closing(connection.execute(statement, parameters)) as cursor:
        return cursor.fetchone()


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
