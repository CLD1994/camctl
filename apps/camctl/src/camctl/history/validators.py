"""事件的生产校验：登记驱动的结构校验与具名守卫分派。

只有结构校验、事务范围、归属和报告影响四类通用守卫随本模块
提供；业务守卫（admission、ack 等）由所属业务任务注册。
分支要求的守卫没有实现时明确拒绝写入，不默认接受。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Mapping

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import TransactionRange
from camctl.history.changes import ChangeDerivationError, event_report_targets
from camctl.history.events import (
    EventEnvelope,
    HistoryEventError,
    RowChange,
    branch_of,
    business_columns,
    changeable_columns,
    event_type_name,
    load_event_registry,
)

Guard = Callable[[EventEnvelope, "EventContext"], None]


class EventValidationError(ValueError):
    """事件违反登记的结构、状态模型、归属、报告影响或具名业务校验。"""


@dataclass(frozen=True)
class EventContext:
    """校验一条事件所需的事务范围与已解析归属事实。

    owners 由调用方按事件发生时的实际关系解析：
    (表名, 行 ID) -> (历史对象名, 对象 ID)。state_rows 是该事件发
    生时（本事务先前事件已应用）的业务行事实，报告影响守卫用它
    沿登记关联解析应报告对象。
    """

    transaction: TransactionRange
    owners: Mapping[tuple[str, int], tuple[str, int]]
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]]


@dataclass(frozen=True)
class ValidatedEvent:
    """通过全部校验的事件及其解析出的对象引用。

    row_owners 保存逐行归属 (表名, 行 ID) -> (历史对象编号, 对象 ID)，
    供回放按对象应用事件时使用。
    """

    envelope: EventEnvelope
    event_name: str
    branch_name: str
    references: tuple[tuple[int, int], ...]
    row_owners: Mapping[tuple[str, int], tuple[int, int]]


#: 具名守卫注册表；装配期由所属业务模块注册，重复注册覆盖同名校验。
NAMED_GUARDS: dict[str, Guard] = {}


def register_guard(name: str, guard: Guard) -> None:
    NAMED_GUARDS[name] = guard


@lru_cache(maxsize=1)
def _history_objects() -> dict[str, dict[str, Any]]:
    return load_enum_registry()["history_objects"]


def _fail(message: str) -> None:
    raise EventValidationError(message)


def _check_enum_value(table: str, column: str, value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    definitions = load_enum_registry()
    enum_columns = {**definitions["enums"], **definitions["json_enums"]}
    definition = enum_columns.get(f"{table}.{column}")
    if definition is None or not isinstance(value, int):
        return
    if value not in set(definition["members"].values()):
        _fail(f"{table}.{column} 的值 {value} 不是登记的枚举编号")


def _match_create_spec(row: RowChange, spec: Mapping[str, Any]) -> bool:
    if spec.get("op") != "create" or row.table != spec["table"]:
        return False
    if row.before.exists or not row.after.exists:
        _fail(f"{row.table}#{row.row_id} 的存在性不满足创建规格")
    expected = business_columns(row.table)
    actual = set(row.after.values)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        _fail(
            f"{row.table}#{row.row_id} 创建行业务列不符: 缺少 {missing}，多出 {extra}"
        )
    for column, allowed in spec.get("after", {}).items():
        value = row.after.values.get(column)
        if value not in allowed and not (value is None and None in allowed):
            _fail(f"{row.table}#{row.row_id} 创建后 {column}={value!r} 不在允许范围 {allowed}")
    for column, value in row.after.values.items():
        _check_enum_value(row.table, column, value)
    return True


def _match_update_spec(row: RowChange, spec: Mapping[str, Any], event_name: str, branch_name: str) -> bool:
    if spec.get("op") != "update" or row.table != spec["table"]:
        return False
    if not row.before.exists or not row.after.exists:
        _fail(f"{row.table}#{row.row_id} 更新要求前后行都存在")
    before_keys = set(row.before.values)
    after_keys = set(row.after.values)
    if before_keys != after_keys:
        _fail(f"{row.table}#{row.row_id} 更新前后列集合不一致")
    declared = set(spec.get("columns", before_keys))
    if before_keys != declared:
        _fail(
            f"{row.table}#{row.row_id} 更新列 {sorted(before_keys)} 与声明的 {sorted(declared)} 不符"
        )
    allowed_columns = changeable_columns(row.table)
    illegal = before_keys - allowed_columns
    if illegal:
        _fail(f"{row.table}#{row.row_id} 更新了不可变或派生列: {sorted(illegal)}")
    for column, allowed in spec.get("before", {}).items():
        if column in row.before.values:
            value = row.before.values[column]
            if value not in allowed and not (value is None and None in allowed):
                _fail(
                    f"{row.table}#{row.row_id} 前置 {column}={value!r} 不在要求范围 {allowed}"
                )
    for column, allowed in spec.get("after", {}).items():
        if column in row.after.values:
            value = row.after.values[column]
            if value not in allowed and not (value is None and None in allowed):
                _fail(
                    f"{row.table}#{row.row_id} 更新后 {column}={value!r} 不在允许范围 {allowed}"
                )
    for column in spec.get("required", ()):
        if column in row.after.values and row.after.values[column] is None:
            _fail(f"{row.table}#{row.row_id} 的 {column} 不能为空")
    _check_transitions(row, spec, event_name, branch_name)
    for column, value in row.after.values.items():
        _check_enum_value(row.table, column, value)
    return True


def _check_transitions(row: RowChange, spec: Mapping[str, Any], event_name: str, branch_name: str) -> None:
    state_models = load_event_registry()["state_models"]
    for column, transition_ids in spec.get("transitions", {}).items():
        model = state_models.get(f"{row.table}.{column}")
        if model is None:
            _fail(f"{row.table}.{column} 没有状态模型")
        edges = [edge for edge in model["edges"] if edge["id"] in transition_ids]
        if not edges:
            _fail(f"{row.table}.{column} 的转换 {transition_ids} 未登记")
        before_value = row.before.values.get(column)
        after_value = row.after.values.get(column)
        allowed_by = f"{event_name}.{branch_name}"
        for edge in edges:
            if allowed_by in edge["by"] and edge["from"] == before_value and edge["to"] == after_value:
                return
        _fail(
            f"{row.table}#{row.row_id} 的 {column} 从 {before_value!r} 到 {after_value!r}"
            f" 不满足 {event_name}.{branch_name} 声明的转换 {transition_ids}"
        )


def _transaction_guard(event: EventEnvelope, context: EventContext) -> None:
    transaction = context.transaction
    if event.transaction_id != transaction.txn_id:
        _fail(f"事件 {event.event_id} 的事务 {event.transaction_id} 与上下文 {transaction.txn_id} 不符")
    if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
        _fail(
            f"事件 {event.event_id} 超出事务 {transaction.txn_id} 的范围"
            f" [{transaction.first_event_id}, {transaction.last_event_id}]"
        )


def _ownership_guard(event: EventEnvelope, context: EventContext) -> None:
    tables = load_event_registry()["tables"]
    for row in event.rows:
        spec = tables.get(row.table)
        if spec is None:
            _fail(f"表 {row.table} 不在业务投影登记中")
        owner = context.owners.get((row.table, row.row_id))
        if owner is None:
            _fail(f"行 {row.table}#{row.row_id} 的历史归属未提供")
        expected_entity = spec["owner"]["entity"]
        if owner[0] != expected_entity:
            _fail(
                f"行 {row.table}#{row.row_id} 的历史归属 {owner[0]!r} 与登记的 {expected_entity!r} 不一致"
            )


def _resolve_owner(event: EventEnvelope, context: EventContext, row: RowChange) -> tuple[str, int]:
    return context.owners[(row.table, row.row_id)]


def _report_impact_guard(event: EventEnvelope, context: EventContext) -> None:
    try:
        targets = event_report_targets(event, context.state_rows)
    except ChangeDerivationError as error:
        _fail(str(error))
    if targets and event.change_seq is None:
        _fail(f"事件 {event.event_id} 引起公开报告变化，必须分配 change_seq")
    if not targets and event.change_seq is not None:
        _fail(f"事件 {event.event_id} 未引起公开报告变化，change_seq 必须为空")


def _row_shape_guard(event: EventEnvelope, context: EventContext) -> None:
    # 行形状校验在守卫分派前由 validate_event 完成；此处保留登记名称分派。
    return None


register_guard("row_shape", _row_shape_guard)
register_guard("transaction", _transaction_guard)
register_guard("ownership", _ownership_guard)
register_guard("report_impact", _report_impact_guard)


def validate_event(event: EventEnvelope, context: EventContext) -> ValidatedEvent:
    """按登记完整校验一条事件并解析对象引用。

    结构校验先于守卫执行；分支要求的任何具名守卫未注册时
    整条事件被拒绝，不能默认接受。
    """
    registry = load_event_registry()
    if event.event_version != 1:
        _fail(f"事件 {event.event_id} 的正文版本 {event.event_version} 不受支持")
    try:
        event_name = event_type_name(event.event_type)
        branch_name, branch = branch_of(event.event_type, event.reason)
    except HistoryEventError as error:
        raise EventValidationError(str(error)) from error
    type_spec = registry["events"][event_name]
    if type_spec["version"] != event.event_version:
        _fail(f"事件 {event_name} 要求版本 {type_spec['version']}")
    if event.clock_status not in (1, 2, 3):
        _fail(f"事件 {event.event_id} 的时钟状态 {event.clock_status} 非法")
    if not event.rows:
        _fail(f"事件 {event.event_id} 的 rows 为空")
    seen: set[tuple[str, int]] = set()
    for row in event.rows:
        if (row.table, row.row_id) in seen:
            _fail(f"行 {row.table}#{row.row_id} 在同一事件中出现两次")
        seen.add((row.table, row.row_id))
        for spec in branch["rows"]:
            if spec.get("op") == "create":
                if _match_create_spec(row, spec):
                    break
            elif _match_update_spec(row, spec, event_name, branch_name):
                break
        else:
            _fail(
                f"行 {row.table}#{row.row_id} 不匹配 {event_name}.{branch_name}"
                " 声明的任何行规格"
            )

    for guard_name in branch["guards"]:
        guard = NAMED_GUARDS.get(guard_name)
        if guard is None:
            _fail(f"{event_name}.{branch_name} 要求的具名校验未接入: {guard_name}")
        guard(event, context)

    objects = _history_objects()
    references: list[tuple[int, int]] = []
    referenced: set[tuple[int, int]] = set()
    row_owners: dict[tuple[str, int], tuple[int, int]] = {}
    for row in event.rows:
        entity_name, entity_id = _resolve_owner(event, context, row)
        entry = (objects[entity_name]["id"], entity_id)
        row_owners[(row.table, row.row_id)] = entry
        if entry not in referenced:
            referenced.add(entry)
            references.append(entry)
    return ValidatedEvent(
        envelope=event,
        event_name=event_name,
        branch_name=branch_name,
        references=tuple(references),
        row_owners=row_owners,
    )
