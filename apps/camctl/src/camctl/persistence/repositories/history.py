"""历史仓储：固定 H 的全局事件分页、报告范围选择与对象恢复。

全局事件页在同一短读事务内核实 H 和事务分组，转换为独立数据并
在返回前释放游标与连接；继续位置沿规定排序严格推进。报告范围
按业务水位窗口分页选择并沿归属外键补齐父对象；对象恢复联合固定
当前投影与 C、候选快照 S 和目录计数，每页短读结束后再应用事件。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from contextlib import closing, contextmanager
from collections.abc import Iterator, Mapping, Sequence

from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.history_values import (
    BoundaryError, HistoryBoundary, INITIAL_BOUNDARY, ReadOrder, ReadScope, TransactionRange, validate_page,
)
from camctl.contracts.pages import Page
from camctl.contracts.json_values import json_equal, parse_exact_json, JsonParseError
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId, new_operation_key
from camctl.acceptance.definitions import read_action_spec
from camctl.history.events import EventEnvelope, business_columns, load_event_registry
from camctl.history.initial_state import runtime_state_values
from camctl.history.decoding import decode_event_row
from camctl.history.queries import (
    FileHistoryCursor,
    FileHistoryKind,
    FileHistoryPage,
    FileHistoryRequest,
    FileRecord,
    ReportScope,
    ReportScopeRequest,
    build_report_scope,
    candidate_scan_spec,
    report_target_types,
)
from camctl.history.replay import ReplayError, reverse_row_values
from camctl.history.snapshots import (
    FORMAT_VERSION,
    EntityImage,
    PreparedSnapshot,
    SavedProgress,
    SnapshotEnqueueTimeout,
    SnapshotMaintenanceError,
    SnapshotRef,
    SnapshotRow,
    decode_snapshot,
)
from camctl.history.validators import EventContext, EventValidationError, _resolve_owner_spec
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (
    DbEnqueueTimeoutError,
    DbJob,
    DbJobKind,
    DbOutcome,
    DbOutcomeKind,
    DbPriority,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.row_validation import ProjectionRowValidator
from camctl.persistence.transaction import read_transaction_range as _transaction_range

__all__ = ["HistoryRepository", "SqliteSnapshotStore"]


@dataclass(frozen=True)
class _FrozenRegistration:
    """一份报告在冻结事务中登记的窗口依据与冻结边界。"""

    from_wm: int
    to_wm: int
    boundary: HistoryBoundary


@dataclass(frozen=True)
class _SnapshotChoice:
    """一次读取已固定的快照身份、完整 S 与 S 处目录依据。"""

    snapshot_id: int
    boundary: HistoryBoundary
    position: tuple[int, int]
    format_version: int
    transaction: TransactionRange


@dataclass(frozen=True)
class _ObjectSeed:
    """同一个 C 读取视图内取得的自身行和恢复路径依据。"""

    ref: tuple[int, int]
    primary_table: str
    current: HistoryBoundary
    target: HistoryBoundary
    rows: Mapping[tuple[str, int], dict]
    head: tuple[int, int]
    floor: tuple[int, int]
    snapshot: _SnapshotChoice | None


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
            "SELECT id, last_event_id FROM history_transactions"
            " WHERE id = (SELECT MAX(id) FROM history_transactions)"
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
        """按固定 C、H 和可用 S 的相关次数恢复对象自身行。

        先在同一短读视图取得投影副本和路径依据。选中快照只读取
        其固定 ID；随后每页结束游标和读事务，再应用本页事件。
        """
        with self._read_connection() as connection:
            seed = self._object_seed(
                connection, entity, entity_id, boundary,
                event_batch_size=event_batch_size)
        if seed.head[1] == seed.floor[1]:
            return self._finish_rows(seed, dict(seed.rows))
        snapshot = seed.snapshot
        forward = (snapshot is not None and
                   (snapshot.boundary == boundary or
                    seed.floor[1] - snapshot.position[1] < seed.head[1] - seed.floor[1]))
        if forward:
            rows = self._snapshot_rows(seed)
            lower, upper = snapshot.boundary, boundary
            expected_count, end_count = snapshot.position[1] + 1, seed.floor[1]
            previous = lower.last_event_id
        else:
            rows = dict(seed.rows)
            lower, upper = boundary, seed.current
            expected_count, end_count = seed.head[1], seed.floor[1] + 1
            previous = upper.last_event_id + 1
        while (expected_count <= end_count if forward else expected_count >= end_count):
            events = self._object_event_page(
                seed.ref, lower, upper, previous, expected_count,
                forward=forward, batch_size=event_batch_size,
                remaining=(end_count - expected_count + 1 if forward
                           else expected_count - end_count + 1))
            for event in events:
                self._apply_object_event(seed, rows, event, forward=forward)
                expected_count += 1 if forward else -1
                previous = event.event_id
        return self._finish_rows(seed, rows)

    def restore_within(
            self, connection: sqlite3.Connection, entity: str,
            entity_id: int, boundary: HistoryBoundary,
            *, event_batch_size: int = 128,
    ) -> dict[tuple[str, int], dict]:
        """在调用方一致视图内复制当前对象，供快照种子读取。

        历史恢复须使用 restore_entity，使事件应用不持有读取事务。
        本入口只接受该视图的完整当前边界。
        """
        seed = self._object_seed(
            connection, entity, entity_id, boundary,
            event_batch_size=event_batch_size, include_snapshot=False)
        if boundary != seed.current:
            raise ConsistencyError("一致视图复制只接受当前边界，历史恢复须释放每页读事务")
        return self._finish_rows(seed, dict(seed.rows))

    def _object_seed(
            self, connection: sqlite3.Connection, entity: str,
            entity_id: int, boundary: HistoryBoundary, *, event_batch_size: int,
            include_snapshot: bool = True,
    ) -> _ObjectSeed:
        from camctl.persistence.row_history import _anchor

        ObjectId(entity_id)
        if isinstance(event_batch_size, bool) or not isinstance(event_batch_size, int) or event_batch_size < 1:
            raise ValueError(f"事件批量必须是正整数: {event_batch_size}")
        spec = load_enum_registry()["history_objects"].get(entity)
        if spec is None:
            raise ConsistencyError(f"未知历史对象类型: {entity!r}")
        current_boundary = self._boundary(connection)
        if (boundary.txn_id > current_boundary.txn_id
                or boundary.last_event_id > current_boundary.last_event_id):
            raise ConsistencyError("恢复目标边界晚于可靠当前边界")
        if boundary != INITIAL_BOUNDARY:
            transaction = _transaction_range(connection, boundary.txn_id)
            if transaction.last_event_id != boundary.last_event_id:
                raise ConsistencyError("恢复目标 H 不是该历史事务的完整结束位置")
        seeded = self._seed_entity_rows(connection, spec, entity_id)
        primary_key = (spec["table"], entity_id)
        if primary_key not in seeded:
            raise ConsistencyError(
                f"对象 {entity}#{entity_id} 在当前投影中不存在")
        ref = (spec["id"], entity_id)
        head_row = _one(connection,
            "SELECT event_id, change_count FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = ? ORDER BY event_id DESC LIMIT 1", ref)
        initialized = (spec["table"] == "runtime_state"
                       and entity_id == runtime_state_values()["id"])
        head = (0, 0) if head_row is None and initialized else _anchor(head_row, "对象目录")
        if head[0] > current_boundary.last_event_id:
            raise ConsistencyError("对象目录头超出可靠当前边界")
        derived = load_event_registry()["tables"][spec["table"]]["derived"]
        primary = seeded[primary_key]
        if (("last_event_id" in derived and primary["last_event_id"] != head[0])
                or ("change_count" in derived and primary["change_count"] != head[1])):
            raise ConsistencyError("当前对象的末事件与次数不符合可靠目录头及 C")
        floor = _directory_position(connection, ref, boundary.last_event_id)
        if (floor[1] > head[1]
                or (floor[1] == head[1]) != (floor[0] == head[0])):
            raise ConsistencyError("H 的目录位置与当前对象目录矛盾")
        snapshot = None
        if include_snapshot and head[1] != floor[1] and floor[1] > 0:
            candidate = _one(connection,
                "SELECT id, boundary_event_id, change_count, format_version"
                " FROM entity_snapshots WHERE entity_type = ? AND entity_id = ?"
                " AND boundary_event_id <= ? ORDER BY boundary_event_id DESC LIMIT 1",
                (*ref, boundary.last_event_id))
            if candidate is not None:
                event_txn = _one(connection, "SELECT transaction_id FROM history_events WHERE id = ?",
                                 (candidate[1],))
                if event_txn is None:
                    raise ConsistencyError("快照 S 缺少必要历史事务")
                transaction = _transaction_range(connection, event_txn[0])
                if transaction.last_event_id != candidate[1]:
                    raise ConsistencyError("快照 S 不是完整历史事务的结束位置")
                position = _directory_position(connection, ref, candidate[1])
                if (candidate[3] != FORMAT_VERSION or candidate[2] != position[1]
                        or not 0 < position[1] <= floor[1]):
                    raise ConsistencyError("快照格式或 S 处累计次数与对象目录不符")
                snapshot = _SnapshotChoice(candidate[0],
                    HistoryBoundary(transaction.txn_id, transaction.last_event_id),
                    position, candidate[3], transaction)
        return _ObjectSeed(ref, spec["table"], current_boundary, boundary,
                           seeded, head, floor, snapshot)

    def _seed_entity_rows(
            self, connection: sqlite3.Connection, spec: dict, entity_id: int,
    ) -> dict[tuple[str, int], dict]:
        """取得对象在当前投影的自身行（报告字段相关的归属表）。"""
        tables: dict[tuple[str, int], dict] = {}
        for table, row_id in self._entity_row_ids(
            connection, spec, entity_id
        ):
            values = _table_row(connection, table, row_id)
            if table == "actions":
                read_action_spec(values)
            tables[(table, row_id)] = values
        return tables

    # ---- 关联文件的同边界查询 ----

    def read_files_at_h(self, request: FileHistoryRequest) -> FileHistoryPage:
        """按固定 H 查询关联的设备文件或主机中间文件。

        引用类查询先恢复引用方，再按 H 时的实际引用恢复文件；必需
        引用指向不存在的文件按一致性错误处理。候选类查询绑定扫描
        固定上界，逐候选恢复到 H 后按当时的固定归属筛选；继续位置
        越过已检查候选，不取最后一个有效结果。主对象在 H 不存在与
        存在但集合为空分别表达。
        """
        if request.kind in (FileHistoryKind.OUTPUT_FILE, FileHistoryKind.COPY_FILES,
                            FileHistoryKind.PROCESSING_FILES):
            return self._read_referenced_files(request)
        return self._read_candidate_files(request)

    def _restore_owner_rows(
            self, entity: str, entity_id: int, boundary: HistoryBoundary,
    ) -> dict[tuple[str, int], dict] | None:
        """恢复引用方到 H；从未存在或 H 时不存在都返回 None。"""
        spec = load_enum_registry()["history_objects"].get(entity)
        with self._read_connection() as connection:
            present = _one(connection,
                f"SELECT 1 FROM {spec['table']} WHERE id = ?", (entity_id,))
            if present is None:
                return None
        rows = self.restore_entity(entity, entity_id, boundary)
        return rows or None

    def _restored_file(
            self, entity: str, table: str, file_id: int,
            boundary: HistoryBoundary, *,
            required: bool,
    ) -> FileRecord | None:
        """恢复一个文件到 H；required 时缺失按一致性错误处理。"""
        rows = self._restore_owner_rows(entity, file_id, boundary)
        row = None if rows is None else rows.get((table, file_id))
        if row is None:
            if required:
                raise ConsistencyError(
                    f"H 保存的必需引用指向不存在或不完整的文件: {entity}#{file_id}")
            raise ConsistencyError(
                f"候选 {entity}#{file_id} 的创建元数据与恢复历史矛盾")
        return FileRecord(file_type=entity, file_id=file_id, row=row)

    def _read_referenced_files(self, request: FileHistoryRequest) -> FileHistoryPage:
        kind = request.kind
        boundary = request.boundary
        if kind is FileHistoryKind.OUTPUT_FILE:
            rows = self._restore_owner_rows("output", request.output_id, boundary)
            if rows is None:
                return self._absent_page()
            output_row = rows.get(("outputs", request.output_id))
            if output_row is None:
                return self._absent_page()
            refs: list[tuple[str, str, int]] = []
            if output_row["device_file_id"] is not None:
                refs.append(("device_file", "device_files",
                             output_row["device_file_id"]))
            if output_row["intermediate_file_id"] is not None:
                refs.append(("intermediate_file", "intermediate_files",
                             output_row["intermediate_file_id"]))
            if not refs:
                raise ConsistencyError(f"产物 {request.output_id} 缺少文件引用")
        elif kind is FileHistoryKind.COPY_FILES:
            with self._read_connection() as connection:
                owner = _one(connection,
                    "SELECT delivery_id, processing_id FROM file_copies WHERE id = ?",
                    (request.copy_id,))
            if owner is None:
                return self._absent_page()
            if owner[0] is not None:
                rows = self._restore_owner_rows("delivery", int(owner[0]), boundary)
            else:
                with self._read_connection() as connection:
                    action_id = _one(
                        connection,
                        "SELECT action_id FROM recording_processing WHERE id = ?",
                        (int(owner[1]),))
                if action_id is None:
                    raise ConsistencyError(
                        f"拷贝 {request.copy_id} 的处理行不存在: {owner[1]!r}")
                rows = self._restore_owner_rows("action", int(action_id[0]), boundary)
            if rows is None:
                return self._absent_page()
            copy_row = rows.get(("file_copies", request.copy_id))
            if copy_row is None:
                return self._absent_page()
            refs = []
            if copy_row["source_device_file_id"] is not None:
                refs.append(("device_file", "device_files",
                             copy_row["source_device_file_id"]))
            if copy_row["source_intermediate_file_id"] is not None:
                refs.append(("intermediate_file", "intermediate_files",
                             copy_row["source_intermediate_file_id"]))
            if copy_row["target_file_id"] is None:
                raise ConsistencyError(f"拷贝 {request.copy_id} 缺少目标文件")
            refs.append(("intermediate_file", "intermediate_files",
                         copy_row["target_file_id"]))
        else:
            rows = self._restore_owner_rows("action", request.action_id, boundary)
            if rows is None:
                return self._absent_page()
            processing = {
                row_id: values for (table, row_id), values in rows.items()
                if table == "recording_processing"}
            if len(processing) > 1:
                raise ConsistencyError(
                    f"动作 {request.action_id} 拥有多条处理记录")
            refs = []
            if processing:
                values = next(iter(processing.values()))
                if values["source_device_file_id"] is not None:
                    refs.append(("device_file", "device_files",
                                 values["source_device_file_id"]))
                if values["repair_output_file_id"] is not None:
                    refs.append(("intermediate_file", "intermediate_files",
                                 values["repair_output_file_id"]))
        items = tuple(
            self._restored_file(entity, table, file_id, boundary, required=True)
            for entity, table, file_id in refs)
        return FileHistoryPage(True, Page(items=items, next_cursor=None))

    def _read_candidate_files(self, request: FileHistoryRequest) -> FileHistoryPage:
        request.check_cursor()
        table, entity, column = candidate_scan_spec(request.kind)
        owner_table = (
            "deliveries" if column == "owner_delivery_id" else "actions")
        with self._read_connection() as connection:
            current = self._boundary(connection)
            if (request.boundary.txn_id > current.txn_id
                    or request.boundary.last_event_id > current.last_event_id):
                raise ConsistencyError("关联文件查询边界晚于可靠当前边界")
            owner = _one(connection,
                f"SELECT created_event_id FROM {owner_table} WHERE id = ?",
                (request.owner_id,))
            owner_present = bool(
                owner is not None and owner[0] <= request.boundary.last_event_id)
            if request.cursor is not None:
                upper_id = request.cursor.upper_id
                after_id = request.cursor.after_id
            else:
                upper_id = int(_one(
                    connection, f"SELECT MAX(id) FROM {table}")[0] or 0)
                after_id = 0
            with closing(connection.execute(
                    f"SELECT id FROM {table} WHERE {column} = ? AND id > ?"
                    " AND id <= ? AND created_event_id <= ?"
                    " ORDER BY id LIMIT ?",
                    (request.owner_id, after_id, upper_id,
                     request.boundary.last_event_id, request.batch_size),
            )) as scan:
                candidates = [int(row[0]) for row in scan.fetchall()]
        if not owner_present:
            return self._absent_page()
        items: list[FileRecord] = []
        for candidate_id in candidates:
            record = self._restored_file(
                entity, table, candidate_id, request.boundary, required=False)
            assert record is not None
            restored = record.row.get(column)
            if restored is None:
                # H 时归属尚未成立（如来源从未知补为确认）：不是成员。
                if column == "source_action_id":
                    continue
                raise ConsistencyError(
                    f"候选 {entity}#{candidate_id} 的固定归属 {column} 为空")
            if restored != request.owner_id:
                if column == "source_action_id":
                    # 来源确认后不可改归属；不同值属于矛盾投影。
                    raise ConsistencyError(
                        f"候选 {entity}#{candidate_id} 的来源归属与扫描条件矛盾")
                raise ConsistencyError(
                    f"候选 {entity}#{candidate_id} 的固定归属 {column} 与扫描条件矛盾")
            items.append(record)
        if (len(candidates) < request.batch_size
                or candidates[-1] >= upper_id):
            next_cursor = None
        else:
            next_cursor = FileHistoryCursor(
                kind=request.kind, boundary=request.boundary,
                owner_id=request.owner_id, upper_id=upper_id,
                after_id=candidates[-1])
        return FileHistoryPage(True, Page(items=tuple(items), next_cursor=next_cursor))

    @staticmethod
    def _absent_page() -> FileHistoryPage:
        return FileHistoryPage(False, Page(items=(), next_cursor=None))


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
            yield from _ids_where(connection, "motor_notifications", "action_id", action)
            # 录像动作自身包含内部处理引用的拷贝行（交付拷贝由交付
            # 子树包含）；归属列不变，恢复范围按处理行归属。
            for _, processing_id in _ids_where(connection, "recording_processing", "action_id", action):
                yield "recording_processing", processing_id
                yield from _ids_where(
                    connection, "file_copies", "processing_id", processing_id)
            # 停止沿原活动归属，读取沿拷贝归属，查询保留触发动作。
            for _, run_id in _owned_operation_run_ids(connection, "action", action):
                yield "operation_runs", run_id
                yield from _ids_where(
                    connection, "operation_attempts", "run_id", run_id)
            yield from _ids_where(connection, "cleanup_items", "action_id", action)
            yield from _ids_where(connection, "auto_preview_links", "obtain_action_id", action)
            for _, dependency_id in _ids_where(connection, "action_dependencies", "action_id", action):
                yield "action_dependencies", dependency_id
                for _, selection_id in _ids_where(
                        connection, "obtain_source_selections", "dependency_id", dependency_id):
                    yield "obtain_source_selections", selection_id
                    yield from _ids_where(connection, "obtain_items", "selection_id", selection_id)
            for _, cancel_item_id in _ids_where(connection, "cancel_items", "action_id", action):
                yield "cancel_items", cancel_item_id
                yield from _ids_where(
                    connection, "cancel_delivery_items", "cancel_item_id", cancel_item_id)
        elif name == "outputs":
            yield from _ids_where(connection, "outputs", "id", entity_id)
            yield from _ids_where(connection, "output_origins", "output_id", entity_id)
        elif name == "deliveries":
            yield from _ids_where(connection, "deliveries", "id", entity_id)
            yield from _ids_where(connection, "file_copies", "delivery_id", entity_id)
            for _, run_id in _owned_operation_run_ids(connection, "delivery", entity_id):
                yield "operation_runs", run_id
                yield from _ids_where(
                    connection, "operation_attempts", "run_id", run_id)
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

    def _snapshot_rows(self, seed: _ObjectSeed) -> dict[tuple[str, int], dict]:
        """读取固定快照正文，释放事务后核对格式、主行及归属链。"""
        choice = seed.snapshot
        assert choice is not None
        with self._read_connection() as connection:
            stored = _one(connection,
                "SELECT entity_type, entity_id, boundary_event_id, change_count,"
                " format_version, content FROM entity_snapshots WHERE id = ?",
                (choice.snapshot_id,))
        if (stored is None or tuple(stored[:5]) !=
                (*seed.ref, choice.boundary.last_event_id,
                 choice.position[1], choice.format_version)):
            raise ConsistencyError("已选快照的固定身份或元数据发生变化")
        try:
            header, decoded = decode_snapshot(stored[5], expect_ref=SnapshotRef(*seed.ref))
            if (header["boundary_event_id"] != choice.boundary.last_event_id
                    or header["change_count"] != choice.position[1]):
                raise SnapshotMaintenanceError("快照正文头与固定元数据不一致")
            rows = {}
            registry = load_event_registry()["tables"]
            with ProjectionRowValidator() as validator:
                for record in decoded:
                    ObjectId(record.values.get("id"))
                    key = (record.table, record.row_id)
                    current = seed.rows.get(key)
                    if key in rows:
                        raise SnapshotMaintenanceError(f"快照重复保存自身行: {key}")
                    if current is None or record.table not in registry:
                        raise SnapshotMaintenanceError(f"快照包含对象自身范围以外的行: {key}")
                    values = dict(record.values)
                    if values.keys() != current.keys():
                        raise SnapshotMaintenanceError(f"快照行缺少必要列或包含未知列: {key}")
                    # 不可变身份和创建位置必须保持；业务可变值使用 S 的
                    # 原值，不能用新 C 的值覆盖已冻结快照。
                    fixed = registry[record.table]["immutable"]
                    for column in (*fixed, "id", "created_event_id"):
                        if column in current and not json_equal(values[column], current[column]):
                            raise SnapshotMaintenanceError(f"快照行的固定事实与当前对象矛盾: {key}.{column}")
                    if ("created_event_id" in values
                            and values["created_event_id"] > choice.boundary.last_event_id):
                        raise SnapshotMaintenanceError(f"快照包含 S 之后创建的行: {key}")
                    validator.validate(record.table, values)
                    if record.table == "actions":
                        read_action_spec(values)
                    rows[key] = values
            primary = rows.get((seed.primary_table, seed.ref[1]))
            if primary is None:
                raise SnapshotMaintenanceError("快照缺少对象完整主行")
            derived = registry[seed.primary_table]["derived"]
            if (("last_event_id" in derived and primary["last_event_id"] != choice.position[0])
                    or ("change_count" in derived and primary["change_count"] != choice.position[1])):
                raise SnapshotMaintenanceError("快照主行的 S 处末事件或累计次数不符")
            state = {}
            for (table, identity), values in rows.items():
                state.setdefault(table, {})[identity] = values
            context = EventContext(choice.transaction, {}, state)
            name = _snapshot_entity_name(seed.ref[0])
            for (table, identity), values in rows.items():
                owner = _resolve_owner_spec(registry[table]["owner"], table, values, context, {})
                if owner != (name, seed.ref[1]):
                    raise SnapshotMaintenanceError(f"快照行 {table}#{identity} 的历史归属不符")
            return rows
        except (SnapshotMaintenanceError, EventValidationError, ValueError, TypeError, KeyError) as error:
            raise ConsistencyError(f"对象 {seed.ref} 的已选快照无法可靠恢复") from error

    def _object_event_page(
            self, ref: tuple[int, int], lower: HistoryBoundary, upper: HistoryBoundary,
            previous: int, expected_count: int, *, forward: bool,
            batch_size: int, remaining: int,
    ) -> tuple[EventEnvelope, ...]:
        """固定范围内复制一页目录事件；完整事务核验限于本页所需组。"""
        direction, operator = ("ASC", ">") if forward else ("DESC", "<")
        limit = min(batch_size, remaining)
        with self._read_connection() as connection:
            with closing(connection.execute(
                "SELECT l.change_count, e.id, e.transaction_id, e.event_type,"
                " e.event_version, e.occurred_at, e.clock_status, e.change_seq,"
                " e.body_json FROM entity_event_links AS l"
                " LEFT JOIN history_events AS e ON e.id = l.event_id"
                " WHERE l.entity_type = ? AND l.entity_id = ? AND l.event_id > ?"
                f" AND l.event_id <= ? AND l.event_id {operator} ?"
                f" ORDER BY l.event_id {direction} LIMIT ?",
                (*ref, lower.last_event_id, upper.last_event_id, previous, limit),
            )) as cursor:
                stored = cursor.fetchall()
            if len(stored) != limit:
                raise ConsistencyError("对象历史目录缺少必要事件或累计次数不连续")
            events = []
            recent = None
            for row in stored:
                event = decode_event_row(row[1:])
                ordered = event.event_id > previous if forward else event.event_id < previous
                if (row[0] != expected_count or not ordered
                        or not lower.last_event_id < event.event_id <= upper.last_event_id
                        or not lower.txn_id < event.transaction_id <= upper.txn_id):
                    raise ConsistencyError("对象历史目录的事件位置、事务或累计次数不连续")
                recent = _transaction_range(connection, event.transaction_id,
                    recent if recent is not None and recent.txn_id == event.transaction_id
                    else self._validated_ranges.get(event.transaction_id))
                if not recent.first_event_id <= event.event_id <= recent.last_event_id:
                    raise ConsistencyError("对象历史事件不属于其完整事务范围")
                events.append(event)
                previous = event.event_id
                expected_count += 1 if forward else -1
            # 不可变组跨页复用；缓存只保留本页最后一组并由实例身份约束。
            self._validated_ranges = {recent.txn_id: recent}
            return tuple(events)

    @staticmethod
    def _apply_object_event(
            seed: _ObjectSeed, rows: dict[tuple[str, int], dict],
            event: EventEnvelope, *, forward: bool,
    ) -> None:
        """在读事务外应用自身行；固定归属不随可变状态改变。"""
        touched = False
        for change in event.rows:
            key = (change.table, change.row_id)
            current = seed.rows.get(key)
            if current is None:
                continue
            touched = True
            columns = business_columns(change.table)
            target = rows.get(key)
            try:
                if forward:
                    if not change.after.exists:
                        raise ReplayError(f"行 {key} 的保存后事实不存在")
                    if not change.before.exists:
                        if target is not None or set(change.after.values) != columns:
                            raise ReplayError(f"行 {key} 的创建事实不完整或已存在")
                        created = {name: value for name, value in current.items() if name not in columns}
                        created.update(change.after.values)
                        rows[key] = created
                    else:
                        if target is None:
                            raise ReplayError(f"行 {key} 的更新前不存在")
                        for name, value in change.before.values.items():
                            if name not in target or not json_equal(target[name], value):
                                raise ReplayError(f"行 {key} 的 {name} 与事件前值不连续")
                        updated = dict(target)
                        updated.update(change.after.values)
                        rows[key] = updated
                elif not change.before.exists:
                    if target is None or not json_equal(
                            {name: target[name] for name in columns}, dict(change.after.values)):
                        raise ReplayError(f"行 {key} 的完整创建后事实不连续")
                    del rows[key]
                else:
                    if target is None:
                        raise ReplayError(f"行 {key} 的逆向更新前不存在")
                    rows[key] = reverse_row_values(target, change, frozenset(target))
            except ReplayError as error:
                raise ConsistencyError(f"对象 {seed.ref} 的行 {key} 恢复依据不连续") from error
        if not touched:
            raise ConsistencyError("对象目录事件没有对象自身行，关联范围不一致")

    @staticmethod
    def _finish_rows(seed: _ObjectSeed, rows: dict[tuple[str, int], dict]) -> dict[tuple[str, int], dict]:
        """按 H 的对象目录重建主行元字段，并核对存在性分类。"""
        key = (seed.primary_table, seed.ref[1])
        if seed.floor[1] == 0:
            if seed.primary_table == "runtime_state":
                initial = runtime_state_values()
                if (key != ("runtime_state", initial["id"]) or set(rows) != {key}
                        or not json_equal(rows[key], initial)):
                    raise ConsistencyError("全局单例在首次自身历史之前必须保持完整初始化事实")
                return rows
            if rows:
                raise ConsistencyError("对象在 H 尚未创建，但历史恢复仍保留自身行")
            return rows
        if key not in rows:
            raise ConsistencyError("对象在 H 已存在，但历史恢复缺少完整主行")
        primary = dict(rows[key])
        derived = load_event_registry()["tables"][seed.primary_table]["derived"]
        if "last_event_id" in derived:
            primary["last_event_id"] = seed.floor[0]
        if "change_count" in derived:
            primary["change_count"] = seed.floor[1]
        rows[key] = primary
        return rows


def _decode_row(values: dict) -> dict:
    decoded = dict(values)
    for key, value in list(decoded.items()):
        if isinstance(value, str) and (key.endswith("_json") or key == "body_json"):
            try:
                decoded[key] = parse_exact_json(value)
            except JsonParseError as error:
                raise ConsistencyError(f"历史投影字段 {key} 不是有效精确 JSON") from error
    return decoded


def _owned_operation_run_ids(
        connection: sqlite3.Connection, entity: str,
        entity_id: int,
) -> Iterator[tuple[str, int]]:
    """从固定关联的候选流程中解析唯一归属，不将触发者当作所有者。

    LEFT JOIN 保留直接关联到本对象但缺失归属链的候选；随后核对
    实际父记录和重复关联，异常不能被连接查询静默省略。
    """
    kinds = enum_for("operation_runs.kind")
    candidates = _operation_run_candidates(connection, entity, entity_id, kinds)
    for run_id in candidates:
        run = _table_row(connection, "operation_runs", run_id)
        try:
            kind = kinds(run["kind"])
        except ValueError as error:
            raise ConsistencyError(f"流程 {run_id} 的种类未登记") from error
        if kind is kinds.READ_FILE:
            copy = _table_row(connection, "file_copies", run["copy_id"])
            delivery_id, processing_id = copy["delivery_id"], copy["processing_id"]
            if (delivery_id is None) == (processing_id is None):
                raise ConsistencyError(f"读取流程 {run_id} 的拷贝没有唯一归属")
            if run["delivery_id"] != delivery_id:
                raise ConsistencyError(f"读取流程 {run_id} 的交付关联与拷贝不符")
            if delivery_id is not None:
                parent = _table_row(connection, "deliveries", delivery_id)
                owner = ("delivery", delivery_id)
            else:
                parent = _table_row(connection, "recording_processing", processing_id)
                owner = ("action", parent["action_id"])
            if run["action_id"] != parent["action_id"]:
                raise ConsistencyError(f"读取流程 {run_id} 的动作关联与拷贝归属不符")
        elif kind in (kinds.STOP_RESIDUAL, kinds.EMERGENCY_STOP):
            activity = _table_row(connection, "device_activities", run["activity_id"])
            if run["delivery_id"] is not None:
                raise ConsistencyError(f"停止流程 {run_id} 不应有关联交付")
            owner = ("action", activity["action_id"])
        else:
            if run["delivery_id"] is not None:
                raise ConsistencyError(f"流程 {run_id} 的种类不适用交付归属")
            owner = ("action", run["action_id"])
        if owner == (entity, entity_id):
            yield "operation_runs", run_id


def _operation_run_candidates(connection, entity, entity_id, kinds) -> Iterator[int]:
    """沿现有归属索引逐页定位候选，避免扫描其他对象的流程。"""
    previous = 0
    while True:
        if entity == "action":
            statement = (
                "SELECT id FROM operation_runs WHERE action_id = ? AND id > ?"
                " UNION SELECT r.id FROM device_activities a"
                " JOIN operation_runs r ON r.activity_id = a.id"
                " WHERE a.action_id = ? AND r.kind = ? AND r.id > ?"
                " UNION SELECT r.id FROM device_activities a"
                " JOIN operation_runs r ON r.activity_id = a.id"
                " WHERE a.action_id = ? AND r.kind = ? AND r.id > ?"
                " UNION SELECT r.id FROM recording_processing p"
                " JOIN file_copies c ON c.processing_id = p.id"
                " JOIN operation_runs r ON r.copy_id = c.id AND r.kind = ?"
                " WHERE p.action_id = ? AND r.id > ? ORDER BY id LIMIT ?")
            parameters = (entity_id, previous,
                entity_id, int(kinds.STOP_RESIDUAL), previous,
                entity_id, int(kinds.EMERGENCY_STOP), previous,
                int(kinds.READ_FILE), entity_id, previous, _MEMBER_BATCH_SIZE)
        else:
            # READ_FILE 的 delivery_id 必须与 copy 的 delivery_id 相同；
            # 第二分支仍定位关联不一致的候选，让归属核对明确拒绝。
            statement = (
                "SELECT id FROM operation_runs WHERE delivery_id = ? AND id > ?"
                " UNION SELECT r.id FROM file_copies c"
                " JOIN operation_runs r ON r.copy_id = c.id AND r.kind = ?"
                " WHERE c.delivery_id = ? AND r.id > ? ORDER BY id LIMIT ?")
            parameters = (entity_id, previous, int(kinds.READ_FILE),
                          entity_id, previous, _MEMBER_BATCH_SIZE)
        with closing(connection.execute(statement, parameters)) as cursor:
            candidates = cursor.fetchall()
        for row in candidates:
            previous = int(row[0])
            yield previous
        if len(candidates) < _MEMBER_BATCH_SIZE:
            return


_MEMBER_BATCH_SIZE = 128


def _ids_where(
    connection: sqlite3.Connection, table: str, column: str, value: int,
) -> Iterator[tuple[str, int]]:
    """按固定归属逐页复制身份；不另存完整成员 ID 集合。"""
    previous = 0
    while True:
        with closing(connection.execute(
                f"SELECT id FROM {table} WHERE {column} = ? AND id > ?"
                " ORDER BY id LIMIT ?", (value, previous, _MEMBER_BATCH_SIZE))) as cursor:
            rows = cursor.fetchall()
        for row in rows:
            previous = int(row[0])
            yield table, previous
        if len(rows) < _MEMBER_BATCH_SIZE:
            return


def _table_row(connection: sqlite3.Connection, table: str, row_id: int) -> dict:
    """取得并解码一行当前投影（JSON 列结构化）。"""
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


def _directory_position(connection, ref: tuple[int, int], upper: int) -> tuple[int, int]:
    from camctl.persistence.row_history import _anchor

    row = _one(connection,
        "SELECT event_id, change_count FROM entity_event_links"
        " WHERE entity_type = ? AND entity_id = ? AND event_id <= ?"
        " ORDER BY event_id DESC LIMIT 1", (*ref, upper))
    return (0, 0) if row is None else _anchor(row, "对象目录位置")


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


class SqliteSnapshotStore:
    """经数据库线程以快照优先级执行的快照维护窄仓储。

    候选查询与依据读取是读操作，依据读取在同一读事务内取一致
    边界 S 并恢复全部对象；保存是短写事务，插入完整快照后按写
    事务最新计数更新维护进度。入队前超时翻译为维护停用信号，
    其余数据库失败按状态库错误表达。
    """

    def __init__(self, path: Path, executor: DbExecutor, *,
                 config: DbConfig | None = None) -> None:
        self._path = Path(path)
        self._executor = executor
        self._config = config

    async def snapshot_candidates(
            self, threshold: int, limit: int) -> tuple[SnapshotRef, ...]:
        def execute(owned):
            with closing(owned.connection.execute(
                    "SELECT entity_type, entity_id FROM entity_snapshot_progress"
                    " WHERE pending_changes >= ?"
                    " ORDER BY pending_changes DESC, entity_type_name COLLATE BINARY,"
                    " entity_id LIMIT ?", (threshold, limit))) as cursor:
                return tuple(
                    SnapshotRef(entity_type=int(row[0]), entity_id=int(row[1]))
                    for row in cursor.fetchall())
        return await self._run(DbJobKind.READ, execute, "快照候选查询")

    async def load_entity_images(
            self, refs: tuple[SnapshotRef, ...]) -> tuple[EntityImage, ...]:
        repository = HistoryRepository(self._path, config=self._config)

        def execute(owned):
            connection = owned.connection
            with closing(connection.execute("BEGIN")):
                pass
            try:
                boundary = repository._boundary(connection)
                images = []
                for ref in refs:
                    name = _snapshot_entity_name(ref.entity_type)
                    rows = repository.restore_within(
                        connection, name, ref.entity_id, boundary)
                    counted = _one(
                        connection,
                        "SELECT MAX(change_count) FROM entity_event_links"
                        " WHERE entity_type = ? AND entity_id = ? AND event_id <= ?",
                        (ref.entity_type, ref.entity_id, boundary.last_event_id))
                    change_count = None if counted is None else counted[0]
                    if change_count is None or int(change_count) < 1:
                        raise SnapshotMaintenanceError(
                            f"对象 {ref} 在读取视图缺少可靠累计次数")
                    images.append(EntityImage(
                        entity_type=ref.entity_type,
                        entity_id=ref.entity_id,
                        boundary=boundary,
                        change_count=int(change_count),
                        rows=tuple(
                            SnapshotRow(table=table, row_id=row_id, values=values)
                            for (table, row_id), values in sorted(rows.items()))))
                with closing(connection.execute("COMMIT")):
                    pass
                return tuple(images)
            except BaseException:
                with closing(connection.execute("ROLLBACK")):
                    pass
                raise
        return await self._run(DbJobKind.READ, execute, "快照依据读取")

    async def save_snapshots(
            self, prepared: tuple[PreparedSnapshot, ...]) -> tuple[SavedProgress, ...]:
        def execute(owned):
            connection = owned.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                saved = tuple(
                    _save_one_snapshot(connection, item) for item in prepared)
            except BaseException as error:
                with closing(connection.execute("ROLLBACK")):
                    pass
                return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=error)
            # 提交阶段错误交由执行器按结果未知处理（失效连接关闭）。
            connection.execute("COMMIT")
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=saved)
        return await self._run(DbJobKind.WRITE, execute, "快照保存")

    async def _run(self, kind: DbJobKind, execute, description: str):
        job = DbJob(
            key=new_operation_key(), description=description, kind=kind,
            priority=DbPriority.SNAPSHOT, execute=execute)
        submit = (self._executor.submit_read if kind is DbJobKind.READ
                  else self._executor.submit_write)
        payload = await submit(job)
        if kind is DbJobKind.READ:
            if isinstance(payload, DbOutcome):
                _raise_snapshot_failure(payload.error, description)
            if payload.error is not None:
                raise SnapshotMaintenanceError(
                    f"{description}失败: {payload.error}") from payload.error
            return payload.value
        if payload.kind is DbOutcomeKind.COMPLETED:
            return payload.value
        _raise_snapshot_failure(payload.error, description)


def _raise_snapshot_failure(error: BaseException | None, description: str) -> None:
    if isinstance(error, DbEnqueueTimeoutError):
        raise SnapshotEnqueueTimeout(f"{description}入队前等待空位超时") from error
    raise SnapshotMaintenanceError(f"{description}失败: {error}") from error


def _snapshot_entity_name(entity_type: int) -> str:
    for name, spec in load_enum_registry()["history_objects"].items():
        if spec["id"] == entity_type:
            return name
    raise SnapshotMaintenanceError(f"未知的历史对象类型编号: {entity_type!r}")


def _save_one_snapshot(
        connection: sqlite3.Connection, item: PreparedSnapshot) -> SavedProgress:
    """在保存事务内写入一份快照并按最新计数更新维护进度。"""
    progress = _one(
        connection,
        "SELECT current_change_count, snapshot_change_count"
        " FROM entity_snapshot_progress WHERE entity_type = ? AND entity_id = ?",
        (item.entity_type, item.entity_id))
    if progress is None:
        raise SnapshotMaintenanceError(
            f"对象 {SnapshotRef(item.entity_type, item.entity_id)} 缺少维护进度")
    latest, baseline = int(progress[0]), int(progress[1])
    existing = _one(
        connection,
        "SELECT id, change_count, format_version, length(content)"
        " FROM entity_snapshots WHERE entity_type = ? AND entity_id = ?"
        " AND boundary_event_id = ?",
        (item.entity_type, item.entity_id, item.boundary.last_event_id))
    if existing is not None:
        snapshot_id = int(existing[0])
        if (int(existing[1]) != item.change_count
                or int(existing[2]) != item.format_version
                or int(existing[3]) != item.length):
            raise SnapshotMaintenanceError(
                f"对象 {SnapshotRef(item.entity_type, item.entity_id)} 在边界"
                f" {item.boundary.last_event_id} 的既有快照与本次内容矛盾")
        _verify_existing_content(connection, snapshot_id, item)
    else:
        cursor = connection.execute(
            "INSERT INTO entity_snapshots (entity_type, entity_id,"
            " boundary_event_id, change_count, format_version, content)"
            " VALUES (?, ?, ?, ?, ?, zeroblob(?))",
            (item.entity_type, item.entity_id, item.boundary.last_event_id,
             item.change_count, item.format_version, item.length))
        snapshot_id = int(cursor.lastrowid)
        with connection.blobopen("entity_snapshots", "content", snapshot_id) as blob:
            for chunk in item.chunks:
                blob.write(chunk)
    new_baseline = max(baseline, item.change_count)
    if new_baseline != baseline:
        connection.execute(
            "UPDATE entity_snapshot_progress SET snapshot_change_count = ?,"
            " latest_snapshot_id = ? WHERE entity_type = ? AND entity_id = ?",
            (new_baseline, snapshot_id, item.entity_type, item.entity_id))
    return SavedProgress(
        ref=SnapshotRef(item.entity_type, item.entity_id),
        remaining_changes=latest - new_baseline)



def _verify_existing_content(
        connection: sqlite3.Connection, snapshot_id: int, item: PreparedSnapshot) -> None:
    """逐块核对既有快照字节与本次内容一致后复用，不重复扣减。"""
    with connection.blobopen(
            "entity_snapshots", "content", snapshot_id) as blob:
        for chunk in item.chunks:
            if blob.read(len(chunk)) != chunk:
                raise SnapshotMaintenanceError(
                    f"对象 {SnapshotRef(item.entity_type, item.entity_id)} 的"
                    f"既有快照 #{snapshot_id} 内容与本次不一致")
