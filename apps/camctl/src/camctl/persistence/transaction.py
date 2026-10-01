"""完整历史事务：编号分配、登记校验、目录派生与整组提交。

一次写事务内重新读取可靠旧状态，确定最终事件范围 F～L，经 H1
校验与 H2 目录派生后，把事件、投影、两类目录、维护进度及派生
历史列作为一组共同提交；任何检查失败整组回滚，提交阶段错误按
结果未知处理。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Callable, Mapping, Protocol

from camctl.bootstrap.resources import resource_bytes
from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import OperationKey
from camctl.history.changes import (
    ChangeDerivationError,
    StateSlice,
    derive_changes,
    event_report_targets,
)
from camctl.history.events import (
    SCHEMA_RESOURCES,
    EventEnvelope,
    RowChange,
    RowImage,
    load_event_registry,
)
from camctl.history.validators import (
    EventContext,
    EventValidationError,
    ValidatedEvent,
    validate_event,
)
from camctl.persistence.runtime import OwnedConnection


class TransactionError(ValueError):
    """写事务内核检测到命令或事实违反事务契约。"""


@dataclass(frozen=True)
class TransactionAllocations:
    """本次写事务取得的历史编号范围。"""

    txn_id: int
    first_event_id: int
    last_event_id: int


@dataclass(frozen=True)
class CommandPlan:
    """命令在可靠旧状态上形成的完整事件计划。

    events 的 event_id、transaction_id 按取得的编号范围填写，
    change_seq 保持为空，由内核按实际报告目标分配。owners 提供逐
    行历史归属，state_rows 提供事务开始时与报告关联解析相关的行
    事实。result 是提交成功后交回调用方的业务结果。

    read_only 为真的命令不产生权威事件（如已受理请求的重送且无
    需保存的新事实）：内核回滚只读事务并按已完成返回。
    """

    events: tuple[EventEnvelope, ...]
    owners: Mapping[tuple[str, int], tuple[str, int]]
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]]
    result: Any = None
    read_only: bool = False
    #: 投影写完后、事务结束前完成依赖新状态的响应判断。
    #: 只允许可靠状态查询和非阻塞资格操作，不执行长任务或新写入。
    complete_result: Callable[[sqlite3.Connection, Any], Any] | None = None


class TransactionScope:
    """命令在写事务内读取旧状态并取得编号分配的入口。"""

    def __init__(self, connection: sqlite3.Connection, max_txn_id: int, max_event_id: int) -> None:
        self.connection = connection
        self.max_txn_id = max_txn_id
        self.max_event_id = max_event_id
        self.allocation: TransactionAllocations | None = None

    def allocate(self, event_count: int) -> TransactionAllocations:
        """按本组事件数量确定事务身份与最终事件范围。

        只能调用一次；分配依据是本写事务内刚读到的已提交最大值。
        """
        if self.allocation is not None:
            raise TransactionError("一次命令只能分配一次编号范围")
        if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 1:
            raise TransactionError(f"事件数量必须是正整数: {event_count!r}")
        self.allocation = TransactionAllocations(
            txn_id=self.max_txn_id + 1,
            first_event_id=self.max_event_id + 1,
            last_event_id=self.max_event_id + event_count,
        )
        return self.allocation


class AtomicCommand(Protocol):
    """仓储实现的完整用例命令。"""

    def plan(self, scope: TransactionScope) -> CommandPlan: ...


@dataclass(frozen=True)
class WriteReceipt:
    """一次写事务的完成结果。"""

    kind: str
    result: Any = None
    error: BaseException | None = None
    boundary: HistoryBoundary | None = None
    change_set: Any = None


@lru_cache(maxsize=1)
def json_columns() -> dict[str, frozenset[str]]:
    """从权威 SQL 收集按结构化值保存的 JSON 列。"""
    collected: dict[str, set[str]] = {}
    for name in SCHEMA_RESOURCES:
        sql = resource_bytes(name).decode("utf-8")
        for block in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\)\s*STRICT", sql, re.DOTALL):
            table = block.group(1)
            body = block.group(2)
            for match in re.finditer(r"json_valid\(\s*(\w+)\s*\)", body):
                collected.setdefault(table, set()).add(match.group(1))
    return {table: frozenset(names) for table, names in collected.items()}


def encode_json_value(value: Any) -> str:
    """按精确数值编码 JSON 文本；不经过 float，不使用上下文舍入。"""

    def dump(node: Any) -> str:
        if node is None:
            return "null"
        if node is True:
            return "true"
        if node is False:
            return "false"
        if isinstance(node, int):
            return str(node)
        if isinstance(node, str):
            return json.dumps(node, ensure_ascii=False)
        from decimal import Decimal

        if isinstance(node, Decimal):
            if not node.is_finite():
                raise TransactionError("事件正文不能包含非有限数值")
            return str(node)
        if isinstance(node, list):
            return "[" + ",".join(dump(item) for item in node) + "]"
        if isinstance(node, dict):
            parts = []
            for key, item in node.items():
                if not isinstance(key, str):
                    raise TransactionError("事件正文对象键必须是字符串")
                parts.append(json.dumps(key, ensure_ascii=False) + ":" + dump(item))
            return "{" + ",".join(parts) + "}"
        raise TransactionError(f"事件正文包含无法精确编码的值: {type(node).__name__}")

    return dump(value)


def _encode_body(event: EventEnvelope) -> str:
    rows = []
    for row in event.rows:
        rows.append(
            {
                "table": row.table,
                "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)},
            }
        )
    return encode_json_value(
        {"reason": event.reason, "evidence": dict(event.evidence), "rows": rows}
    )


def _scalar(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    value = row[0] if row is not None else None
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        raise TransactionError(f"读取分配依据失败: {sql}")
    return value


def _apply_rows(
    working: dict[str, dict[int, dict[str, Any]]],
    events: tuple[EventEnvelope, ...],
) -> None:
    for event in events:
        for row in event.rows:
            table_rows = working.setdefault(row.table, {})
            merged = dict(table_rows.get(row.row_id, {}))
            merged.update(row.after.values)
            table_rows[row.row_id] = merged


def _copy_rows(
    rows: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> dict[str, dict[int, dict[str, Any]]]:
    return {table: dict(table_rows) for table, table_rows in rows.items()}


def _sql_value(table: str, column: str, value: Any) -> Any:
    if value is not None and column in json_columns().get(table, frozenset()):
        return encode_json_value(value)
    return value


def _write_projections(
    connection: sqlite3.Connection,
    events: tuple[EventEnvelope, ...],
    derived: Mapping[tuple[str, int], dict[str, int]],
) -> None:
    """按事件顺序核对旧行并写入投影。

    更新前逐列核对写事务内的当前值，防止用事务外缓存覆盖独立提
    交的事实；同事务先前事件创建或更新的行以刚写入的状态参与后
    续核对。创建行携带派生历史列（这些列非空）。
    """
    for event in events:
        for row in event.rows:
            if row.before.exists:
                columns = sorted(row.before.values)
                selected = connection.execute(
                    f"SELECT {', '.join(columns)} FROM {row.table} WHERE id = ?",
                    (row.row_id,),
                ).fetchone()
                if selected is None:
                    raise TransactionError(
                        f"更新目标 {row.table}#{row.row_id} 在当前状态中不存在"
                    )
                for column, actual in zip(columns, selected):
                    expected = _sql_value(row.table, column, row.before.values[column])
                    if actual != expected:
                        raise TransactionError(
                            f"{row.table}#{row.row_id}.{column} 的旧值与当前状态不符:"
                            f" 事件认为 {expected!r}，实际 {actual!r}"
                        )
                assignments = []
                values = []
                for column, value in row.after.values.items():
                    assignments.append(f"{column} = ?")
                    values.append(_sql_value(row.table, column, value))
                values.append(row.row_id)
                connection.execute(
                    f"UPDATE {row.table} SET {', '.join(assignments)} WHERE id = ?",
                    values,
                )
            else:
                existed = connection.execute(
                    f"SELECT 1 FROM {row.table} WHERE id = ?", (row.row_id,)
                ).fetchone()
                if existed is not None:
                    raise TransactionError(
                        f"创建目标 {row.table}#{row.row_id} 在当前状态中已存在"
                    )
                row_derived = derived.get((row.table, row.row_id), {})
                columns = ["id", *row.after.values, *row_derived]
                values = [
                    _sql_value(row.table, column, value)
                    for column, value in row.after.values.items()
                ]
                placeholders = ", ".join("?" for _ in columns)
                connection.execute(
                    f"INSERT INTO {row.table} ({', '.join(columns)})"
                    f" VALUES ({placeholders})",
                    (row.row_id, *values, *row_derived.values()),
                )


@lru_cache(maxsize=1)
def _derived_columns() -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for column, spec in load_event_registry()["derived_history"].items():
        table, _, name = column.rpartition(".")
        grouped.setdefault(table, {})[name] = spec["method"]
    return grouped


def _derived_map(
    events: tuple[EventEnvelope, ...],
    links: tuple[Any, ...],
    change_counts: Mapping[tuple[int, int], int],
) -> dict[tuple[str, int], dict[str, int]]:
    """按派生规则计算对象主表的历史元数据列值。

    first_own_event 只对本事务新建的对象生效；既有对象的创建依
    据保持原事实。
    """
    objects = load_enum_registry()["history_objects"]
    by_type = {spec["id"]: (name, spec["table"]) for name, spec in objects.items()}
    derived = _derived_columns()
    first_touch: dict[tuple[int, int], int] = {}
    last_touch: dict[tuple[int, int], int] = {}
    for link in links:
        ref = (link.entity_type, link.entity_id)
        first_touch.setdefault(ref, link.event_id)
        last_touch[ref] = link.event_id
    created_pairs = {
        (row.table, row.row_id) for event in events for row in event.rows if not row.before.exists
    }
    result: dict[tuple[str, int], dict[str, int]] = {}
    for ref, final_count in change_counts.items():
        entity_type, entity_id = ref
        name, table = by_type.get(entity_type, (None, None))
        if table is None or table not in derived or first_touch.get(ref) is None:
            continue
        values: dict[str, int] = {}
        for column, method in derived[table].items():
            if method == "first_own_event":
                if (table, entity_id) in created_pairs:
                    values[column] = first_touch[ref]
            elif method == "last_own_event":
                values[column] = last_touch[ref]
            elif method == "own_event_count":
                values[column] = final_count
            else:
                raise TransactionError(f"未知派生历史方法 {method}")
        result[(table, entity_id)] = values
    return result


def _write_derived_history(
    connection: sqlite3.Connection,
    events: tuple[EventEnvelope, ...],
    derived: Mapping[tuple[str, int], dict[str, int]],
) -> None:
    """为已存在的主表行推进派生历史元数据列。"""
    created = {
        (row.table, row.row_id)
        for event in events
        for row in event.rows
        if not row.before.exists
    }
    for (table, row_id), values in sorted(derived.items()):
        if (table, row_id) in created or not values:
            continue
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), row_id)
        )


def commit_operation(
    command: AtomicCommand,
    operation_key: OperationKey,
    owned: OwnedConnection,
) -> WriteReceipt:
    """在所属连接上执行一项完整事件事务。

    返回已提交、确认回滚或结果未知；提交成功携带完整历史边界与
    派生目录。业务拒绝由命令作为普通结果返回，同样是已提交结果。
    """
    connection: sqlite3.Connection = owned.connection
    begun = False
    committing = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        begun = True
        scope = TransactionScope(
            connection,
            _scalar(connection, "SELECT MAX(id) FROM history_transactions"),
            _scalar(connection, "SELECT MAX(id) FROM history_events"),
        )
        try:
            plan = command.plan(scope)
        except Exception as error:
            # 命令执行期意外失败：整组不提交，按回滚或未知分类。
            return _rollback_or_unknown(connection, True, error)
        events = plan.events
        if len(events) == 0:
            if not plan.read_only:
                raise TransactionError("写事务没有权威事件")
            try:
                result = (
                    plan.complete_result(connection, plan.result)
                    if plan.complete_result else plan.result
                )
            except Exception as error:
                return _rollback_or_unknown(connection, True, error)
            connection.execute("ROLLBACK")
            return WriteReceipt(kind="completed", result=result)
        allocation = scope.allocation
        if allocation is None:
            raise TransactionError("命令没有分配编号范围")
        count = len(events)
        if (
            allocation.txn_id != scope.max_txn_id + 1
            or allocation.first_event_id != scope.max_event_id + 1
        ):
            raise TransactionError("编号范围没有紧接已提交历史")
        if allocation.last_event_id - allocation.first_event_id + 1 != count:
            raise TransactionError(
                f"事件数量 {count} 与分配范围 {allocation.first_event_id}~{allocation.last_event_id} 不符"
            )
        for index, event in enumerate(events):
            expected_id = allocation.first_event_id + index
            if event.event_id != expected_id or event.transaction_id != allocation.txn_id:
                raise TransactionError(
                    f"第 {index} 条事件的编号 {event.event_id}/{event.transaction_id}"
                    f" 与分配 {expected_id}/{allocation.txn_id} 不符"
                )

        max_change_seq = _scalar(connection, "SELECT MAX(change_seq) FROM history_events")
        working = _copy_rows(plan.state_rows)
        sequenced: list[EventEnvelope] = []
        for event in events:
            targets = event_report_targets(event, working)
            change_seq = None
            if targets:
                max_change_seq += 1
                change_seq = max_change_seq
            sequenced.append(replace(event, change_seq=change_seq))
            _apply_rows(working, (event,))

        validation_state = _copy_rows(plan.state_rows)
        validated: list[ValidatedEvent] = []
        transaction_range = TransactionRange(
            txn_id=allocation.txn_id,
            first_event_id=allocation.first_event_id,
            last_event_id=allocation.last_event_id,
        )
        for event in sequenced:
            context = EventContext(
                transaction=transaction_range,
                owners=plan.owners,
                state_rows=validation_state,
            )
            validated.append(validate_event(event, context))
            _apply_rows(validation_state, (event,))

        touched = {
            (entity_type, entity_id)
            for validated_event in validated
            for entity_type, entity_id in validated_event.row_owners.values()
        }
        before_counts: dict[tuple[int, int], int] = {}
        for entity_type, entity_id in touched:
            row = connection.execute(
                "SELECT MAX(change_count) FROM entity_event_links"
                " WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id),
            ).fetchone()
            if row is not None and row[0] is not None:
                before_counts[(entity_type, entity_id)] = row[0]
        before_slice = StateSlice(rows=_copy_rows(plan.state_rows), change_counts=before_counts)
        after_counts = dict(before_counts)
        for link_event in validated:
            refs = {
                entity_ref
                for entity_ref in link_event.row_owners.values()
            }
            for entity_ref in refs:
                after_counts[entity_ref] = after_counts.get(entity_ref, 0) + 1
        after_slice = StateSlice(rows=validation_state, change_counts=after_counts)
        change_set = derive_changes(tuple(validated), before_slice, after_slice)

        connection.execute(
            "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
            " VALUES (?, ?, ?, ?)",
            (allocation.txn_id, str(operation_key), allocation.first_event_id, allocation.last_event_id),
        )
        for event in sequenced:
            connection.execute(
                "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
                " occurred_at, clock_status, change_seq, body_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.transaction_id,
                    event.event_type,
                    event.event_version,
                    event.occurred_at,
                    event.clock_status,
                    event.change_seq,
                    _encode_body(event),
                ),
            )
        for link in change_set.links:
            connection.execute(
                "INSERT INTO entity_event_links (entity_type, entity_id, event_id, change_count)"
                " VALUES (?, ?, ?, ?)",
                (link.entity_type, link.entity_id, link.event_id, link.change_count),
            )
        for report_change in change_set.report_changes:
            connection.execute(
                "INSERT INTO report_entity_changes (entity_type, entity_id, event_id, change_seq)"
                " VALUES (?, ?, ?, ?)",
                (
                    report_change.entity_type,
                    report_change.entity_id,
                    report_change.event_id,
                    report_change.change_seq,
                ),
            )
        for progress in change_set.progress_updates:
            connection.execute(
                "INSERT INTO entity_snapshot_progress (entity_type, entity_id,"
                " current_change_count, snapshot_change_count, latest_snapshot_id)"
                " VALUES (?, ?, ?, 0, NULL)"
                " ON CONFLICT(entity_type, entity_id)"
                " DO UPDATE SET current_change_count = excluded.current_change_count",
                (progress.entity_type, progress.entity_id, progress.current_change_count),
            )
        derived = _derived_map(tuple(sequenced), change_set.links, change_set.change_counts)
        _write_projections(connection, tuple(sequenced), derived)
        _write_derived_history(connection, tuple(sequenced), derived)

        try:
            result = (
                plan.complete_result(connection, plan.result)
                if plan.complete_result else plan.result
            )
        except Exception as error:
            return _rollback_or_unknown(connection, True, error)
        committing = True
        connection.execute("COMMIT")
        return WriteReceipt(
            kind="completed",
            result=result,
            boundary=HistoryBoundary(
                txn_id=allocation.txn_id, last_event_id=allocation.last_event_id
            ),
            change_set=change_set,
        )
    except (TransactionError, EventValidationError, ChangeDerivationError) as error:
        return _rollback_or_unknown(connection, begun, error)
    except sqlite3.Error as error:
        if committing:
            return WriteReceipt(kind="unknown", error=error)
        return _rollback_or_unknown(connection, begun, error)


def _rollback_or_unknown(
    connection: sqlite3.Connection, begun: bool, error: BaseException
) -> WriteReceipt:
    if not begun:
        return WriteReceipt(kind="rolled_back", error=error)
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as rollback_error:
        return WriteReceipt(kind="unknown", error=rollback_error)
    return WriteReceipt(kind="rolled_back", error=error)


# -- 仓储共享的事务构建辅助 -------------------------------------------


def row_change(table: str, row_id: int, values: dict) -> RowChange:
    """构造创建行：本事件建立该行的全部业务列。"""
    return RowChange(
        table=table,
        row_id=row_id,
        before=RowImage(exists=False, values={}),
        after=RowImage(exists=True, values=values),
    )


def update_change(table: str, row_id: int, before: dict, after: dict) -> RowChange:
    """从相同拟更新集合构造真实变化；旧值来自本事务刚读到的状态。"""
    if before.keys() != after.keys():
        raise TransactionError(f"{table}#{row_id} 更新前后字段集合不同")
    changed = [column for column in before if not json_equal(before[column], after[column])]
    if not changed:
        raise TransactionError(f"{table}#{row_id} 没有可保存的变化")
    return RowChange(
        table=table,
        row_id=row_id,
        before=RowImage(exists=True, values={column: before[column] for column in changed}),
        after=RowImage(exists=True, values={column: after[column] for column in changed}),
    )


def event_envelope(
    event_id: int,
    txn_id: int,
    event_type: int,
    reason: int,
    rows,
    occurred_at: int,
    evidence: dict | None = None,
) -> EventEnvelope:
    """按正文版本 1 构造事件信封；change_seq 由内核分配。"""
    return EventEnvelope(
        event_id=event_id,
        transaction_id=txn_id,
        event_type=event_type,
        event_version=1,
        occurred_at=occurred_at,
        clock_status=2,
        change_seq=None,
        reason=reason,
        evidence=evidence if evidence is not None else {},
        rows=tuple(rows),
    )


def row_facts(connection: sqlite3.Connection, table: str, row_id: int) -> dict | None:
    """读取一行完整列值（原始类型，JSON 列仍为文本）。"""
    cursor = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    found = cursor.fetchone()
    if found is None:
        return None
    return {
        name: value
        for name, value in zip((d[0] for d in cursor.description), found)
    }


def next_row_id(connection: sqlite3.Connection, table: str) -> int:
    """按已提交最大 ID 分配下一行编号。"""
    row = connection.execute(f"SELECT MAX(id) FROM {table}").fetchone()
    return (int(row[0]) if row[0] is not None else 0) + 1


def saved_transaction_events(
    connection: sqlite3.Connection, operation_key: OperationKey
) -> list[dict] | None:
    """按操作身份取得已提交事务的事件事实；不存在时为空。

    正文按原样解析，reason 从正文读取；供提交结果未知后的核实与
    重送复用，不产生副作用。
    """
    row = connection.execute(
        "SELECT id FROM history_transactions WHERE operation_key = ?",
        (str(operation_key),),
    ).fetchone()
    if row is None:
        return None
    events: list[dict] = []
    for event_type, body in connection.execute(
        "SELECT event_type, body_json FROM history_events"
        " WHERE transaction_id = ? ORDER BY id",
        (int(row[0]),),
    ):
        document = json.loads(body)
        events.append(
            {"type": int(event_type), "reason": document.get("reason"), "body": document}
        )
    return events
