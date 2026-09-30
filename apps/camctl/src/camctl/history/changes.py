"""历史目录、报告变化目录与维护计数的纯派生。

报告目标派生读取报告字段依赖登记：来源行变化沿登记关联
（routes、relations）在事件发生时的事实上解析候选对象。公开变化
判断当前使用进入公开投影的列集（与事件校验同源）；K4 的纯公开
投影落地后，在同一入口切换为事件前后公开值比较。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from camctl.bootstrap.resources import resource_bytes
from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.history.events import EventEnvelope, RowChange

if TYPE_CHECKING:
    from camctl.history.validators import ValidatedEvent

_REPORT_DEPENDENCIES_RESOURCE = "registry/report-dependencies.json"

#: (历史对象编号, 对象 ID)。
EntityRef = tuple[int, int]


class ChangeDerivationError(ValueError):
    """目录派生与事件、事务事实或登记矛盾。"""


@dataclass(frozen=True)
class StateSlice:
    """一次事务某一侧的相关业务行事实与对象累计计数基线。

    rows 是表名到 {行 ID: 列值} 的映射；报告关联解析要求路由涉
    及的表（含来源表）在切片中显式出现，空映射表示该表当时没有
    相关行。change_counts 保存事务前各对象的累计变化次数；缺席
    表示尚未出生（计数为 0）。
    """

    rows: Mapping[str, Mapping[int, Mapping[str, Any]]]
    change_counts: Mapping[EntityRef, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for table, rows in self.rows.items():
            if not isinstance(table, str) or not table:
                raise ChangeDerivationError("状态切片的表名必须是非空字符串")
            for row_id in rows:
                if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
                    raise ChangeDerivationError(f"表 {table} 的行 ID {row_id!r} 不是正整数")
        for ref, count in self.change_counts.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ChangeDerivationError(f"对象 {ref} 的累计计数 {count!r} 不是正整数")


@dataclass(frozen=True)
class EntityEventLink:
    """一条事件改变了某个对象自身需要恢复的记录（恢复目录行）。"""

    entity_type: int
    entity_id: int
    event_id: int
    change_count: int


@dataclass(frozen=True)
class ReportEntityChange:
    """一条事件使某个现有报告实体需要更新（报告目录行）。"""

    entity_type: int
    entity_id: int
    event_id: int
    change_seq: int


@dataclass(frozen=True)
class ProgressUpdate:
    """维护范围内对象在本次事务后的累计变化次数。"""

    entity_type: int
    entity_id: int
    current_change_count: int


@dataclass(frozen=True)
class ChangeSet:
    """一次完整写事务派生出的两类目录与计数。

    change_counts 只包含本次事务实际触及的对象；progress_updates
    是其中属于快照维护范围的子集，未触及对象保持原进度。
    """

    links: tuple[EntityEventLink, ...]
    report_changes: tuple[ReportEntityChange, ...]
    change_counts: Mapping[EntityRef, int]
    progress_updates: tuple[ProgressUpdate, ...]


@lru_cache(maxsize=1)
def _dependencies() -> dict[str, Any]:
    try:
        dependencies = json.loads(resource_bytes(_REPORT_DEPENDENCIES_RESOURCE))
    except (OSError, ValueError) as error:
        raise ChangeDerivationError("报告字段依赖登记不可用") from error
    if dependencies.get("format_version") != 1:
        raise ChangeDerivationError("报告字段依赖登记版本不受支持")
    return dependencies


@lru_cache(maxsize=1)
def public_columns() -> dict[str, frozenset[str]]:
    """从报告字段依赖登记收集各表进入公开投影的列。"""
    collected: dict[str, set[str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            column = node.get("column")
            if isinstance(column, str) and "." in column:
                table, _, name = column.rpartition(".")
                collected.setdefault(table, set()).add(name)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_dependencies().get("projections", {}))
    return {table: frozenset(names) for table, names in collected.items()}


@lru_cache(maxsize=1)
def _history_objects() -> dict[str, dict[str, Any]]:
    return load_enum_registry()["history_objects"]


def _split_column(column: str) -> tuple[str, str]:
    table, _, name = column.rpartition(".")
    if not table or not name:
        raise ChangeDerivationError(f"登记引用 {column!r} 不是 表.列 形式")
    return table, name


def _table_rows(state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]], table: str):
    rows = state_rows.get(table)
    if rows is None:
        raise ChangeDerivationError(f"报告关联解析缺少表 {table} 的事实")
    return rows


def _affects_public_columns(row: RowChange, public: Mapping[str, frozenset[str]]) -> bool:
    if not row.before.exists:
        return True
    return bool(set(row.after.values) & public.get(row.table, frozenset()))


def _merged_row_values(
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]], row: RowChange
) -> dict[str, Any]:
    merged = dict(_table_rows(state_rows, row.table).get(row.row_id, {}))
    merged.update(row.after.values)
    return merged


def _filter_by_when(
    when: Mapping[str, Any],
    reached: dict[str, list[tuple[int, dict[str, Any]]]],
) -> None:
    op = when.get("op")
    table, name = _split_column(when["column"])
    if op != "not_null":
        raise ChangeDerivationError(f"路由条件 {op!r} 未登记处理方式")
    if table not in reached:
        raise ChangeDerivationError(f"路由条件引用了未到达的表 {table}")
    reached[table] = [(rid, values) for rid, values in reached[table] if values.get(name) is not None]


def _route_candidates(
    route: Mapping[str, Any],
    row: RowChange,
    source_values: Mapping[str, Any],
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> Iterable[tuple[str, int]]:
    if route.get("mode") == "per_relation":
        specs = route.get("targets", ())
    else:
        specs = (route,)
    relations = _dependencies()["relations"]
    for spec in specs:
        entity_name = spec["entity"]
        current_table = row.table
        reached: dict[str, list[tuple[int, dict[str, Any]]]] = {
            row.table: [(row.row_id, dict(source_values))]
        }
        for relation_name in spec.get("relations", ()):
            relation = relations.get(relation_name)
            if relation is None:
                raise ChangeDerivationError(f"登记关联 {relation_name!r} 不存在")
            from_table, from_column = _split_column(relation["from"])
            to_table, to_column = _split_column(relation["to"])
            current_ids = [row_id for row_id, _ in reached[current_table]]
            if to_table == current_table:
                next_table, next_column = from_table, from_column
            elif from_table == current_table:
                next_table, next_column = to_table, to_column
            else:
                raise ChangeDerivationError(
                    f"登记关联 {relation_name!r} 与路径当前表 {current_table} 不相连"
                )
            next_rows = [
                (row_id, dict(values))
                for row_id, values in _table_rows(state_rows, next_table).items()
                if values.get(next_column) in current_ids
            ]
            reached[next_table] = next_rows
            current_table = next_table
        when = spec.get("when")
        if when is not None:
            _filter_by_when(when, reached)
        identity_table, identity_column = _split_column(spec["identity"])
        if identity_table not in reached:
            raise ChangeDerivationError(
                f"路由身份 {spec['identity']} 不在路径到达的表中"
            )
        for row_id, values in reached[identity_table]:
            if identity_column == "id":
                value: Any = row_id
            else:
                value = values.get(identity_column)
            if value is None:
                raise ChangeDerivationError(
                    f"路由身份 {spec['identity']} 在 {identity_table}#{row_id} 上为空"
                )
            if not isinstance(value, int) or isinstance(value, bool):
                raise ChangeDerivationError(
                    f"路由身份 {spec['identity']} 的值 {value!r} 不是整数"
                )
            yield entity_name, value


def event_report_targets(
    event: EventEnvelope,
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> tuple[EntityRef, ...]:
    """解析一条事件在发生时事实上影响的应报告对象。

    只读取传入的事件时事实；不读取当前数据库关系，也不为仅引用
    的对象产生目标。同一事件命中同一目标只返回一次。
    """
    objects = _history_objects()
    routes = _dependencies()["routes"]
    public = public_columns()
    targets: dict[EntityRef, None] = {}
    for row in event.rows:
        route = routes.get(row.table)
        if route is None or route.get("mode") != "public_change":
            continue
        if not _affects_public_columns(row, public):
            continue
        source_values = _merged_row_values(state_rows, row)
        for entity_name, entity_id in _route_candidates(route, row, source_values, state_rows):
            entity = objects.get(entity_name)
            if entity is None or not entity.get("report_target", False):
                raise ChangeDerivationError(f"路由目标 {entity_name!r} 不是登记的报告对象")
            targets.setdefault((entity["id"], entity_id), None)
    return tuple(targets)


def fill_in_parents(
    targets: Iterable[EntityRef],
    rows: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> tuple[EntityRef, ...]:
    """沿登记的父对象关联补齐报告选择所需的祖先，不包含传入目标。

    父对象补齐不进入目录、不增加计数；这里只从给定事实解析补齐
    集合，供报告选择在同一历史边界使用。
    """
    objects = _history_objects()
    entities = _dependencies()["entities"]
    by_type = {spec["id"]: name for name, spec in objects.items()}
    requested = list(dict.fromkeys(targets))
    collected: set[EntityRef] = set()
    visited: set[EntityRef] = set()
    stack = list(requested)
    while stack:
        ref = stack.pop()
        if ref in visited:
            continue
        visited.add(ref)
        name = by_type.get(ref[0])
        if name is None:
            raise ChangeDerivationError(f"未知历史对象编号 {ref[0]}")
        parent = entities.get(name, {}).get("parent")
        if not parent:
            continue
        table = objects[name]["table"]
        table_rows = rows.get(table)
        if table_rows is None or ref[1] not in table_rows:
            raise ChangeDerivationError(f"补齐父对象缺少 {table}#{ref[1]} 的事实")
        parent_id = table_rows[ref[1]].get(parent["foreign_key"])
        if not isinstance(parent_id, int) or isinstance(parent_id, bool):
            raise ChangeDerivationError(
                f"{table}#{ref[1]} 的 {parent['foreign_key']} 不是可用的父对象引用"
            )
        parent_name = parent["entity"]
        parent_ref = (objects[parent_name]["id"], parent_id)
        if parent_ref not in requested:
            collected.add(parent_ref)
        stack.append(parent_ref)
    return tuple(sorted(collected))


def derive_changes(
    events: tuple["ValidatedEvent", ...],
    before: StateSlice,
    after: StateSlice,
) -> ChangeSet:
    """从同一事务的已验证事件派生恢复目录、报告目录与计数。

    逐条事件在其发生时的事实上解析报告目标：本事务先前事件的
    行变化先应用到工作状态，再解析后续事件。after 事实必须覆盖
    事务实际改变过的列并与其应用结果一致，after 计数与派生计数
    矛盾时拒绝。
    """
    if not events:
        raise ChangeDerivationError("事务没有事件，不能派生目录")
    txn_id = events[0].envelope.transaction_id
    working: dict[str, dict[int, dict[str, Any]]] = {
        table: dict(rows) for table, rows in before.rows.items()
    }
    counts: dict[EntityRef, int] = dict(before.change_counts)
    touched: set[EntityRef] = set()
    links: list[EntityEventLink] = []
    report_changes: list[ReportEntityChange] = []
    applied: dict[tuple[str, int], dict[str, Any]] = {}
    last_event_id = 0
    for validated in events:
        event = validated.envelope
        if event.transaction_id != txn_id:
            raise ChangeDerivationError(
                f"事件 {event.event_id} 的事务 {event.transaction_id} 与 {txn_id} 不一致"
            )
        if event.event_id <= last_event_id:
            raise ChangeDerivationError(
                f"事件 {event.event_id} 未按严格递增顺序排列（前一事件 {last_event_id}）"
            )
        last_event_id = event.event_id
        targets = event_report_targets(event, working)
        if targets and event.change_seq is None:
            raise ChangeDerivationError(
                f"事件 {event.event_id} 有应报告对象但未分配 change_seq"
            )
        if not targets and event.change_seq is not None:
            raise ChangeDerivationError(
                f"事件 {event.event_id} 没有应报告对象却分配了 change_seq"
            )
        for entity_type, entity_id in targets:
            report_changes.append(
                ReportEntityChange(entity_type, entity_id, event.event_id, event.change_seq)
            )
        event_entities: dict[EntityRef, None] = {}
        for row in event.rows:
            ref = validated.row_owners.get((row.table, row.row_id))
            if ref is None:
                raise ChangeDerivationError(
                    f"事件 {event.event_id} 的行 {row.table}#{row.row_id} 缺少历史归属"
                )
            event_entities.setdefault(ref, None)
        for ref in event_entities:
            base = counts.get(ref, 0)
            counts[ref] = base + 1
            touched.add(ref)
            links.append(EntityEventLink(ref[0], ref[1], event.event_id, base + 1))
        for row in event.rows:
            if not row.after.exists:
                raise ChangeDerivationError(
                    f"事件 {event.event_id} 不允许取消行 {row.table}#{row.row_id} 的存在性"
                )
            table_rows = working.setdefault(row.table, {})
            merged = dict(table_rows.get(row.row_id, {}))
            merged.update(row.after.values)
            table_rows[row.row_id] = merged
            columns = applied.setdefault((row.table, row.row_id), {})
            columns.update(row.after.values)
    for (table, row_id), values in applied.items():
        table_rows = after.rows.get(table)
        if table_rows is None or row_id not in table_rows:
            raise ChangeDerivationError(f"after 事实缺少事务改变的行 {table}#{row_id}")
        final = table_rows[row_id]
        for column, value in values.items():
            if column not in final:
                raise ChangeDerivationError(
                    f"after 事实缺少 {table}#{row_id} 事务改变的列 {column}"
                )
            if final[column] != value:
                raise ChangeDerivationError(
                    f"after 事实与事件应用结果不一致: {table}#{row_id}.{column}"
                )
    for ref, expected in after.change_counts.items():
        actual = counts.get(ref, before.change_counts.get(ref, 0))
        if actual != expected:
            raise ChangeDerivationError(
                f"after 计数与派生不一致: 对象 {ref} 期望 {expected}，派生 {actual}"
            )
    snapshot_types = {
        spec["id"] for spec in _history_objects().values() if spec.get("snapshot", False)
    }
    progress_updates = tuple(
        sorted(
            (
                ProgressUpdate(entity_type, entity_id, counts[(entity_type, entity_id)])
                for entity_type, entity_id in touched
                if entity_type in snapshot_types
            ),
            key=lambda update: (update.entity_type, update.entity_id),
        )
    )
    return ChangeSet(
        links=tuple(links),
        report_changes=tuple(report_changes),
        change_counts={ref: counts[ref] for ref in touched},
        progress_updates=progress_updates,
    )
