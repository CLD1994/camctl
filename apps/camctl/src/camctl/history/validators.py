"""事件的生产校验：登记驱动的结构校验与具名守卫分派。

只有结构校验、事务范围、归属和报告影响四类通用守卫随本模块
提供；业务守卫（admission、ack 等）由所属业务任务注册。
分支要求的守卫没有实现时明确拒绝写入，不默认接受。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Mapping

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ObjectId, UtcMicros
from camctl.history.changes import ChangeDerivationError, event_report_targets
from camctl.history.reads import BaselineRangeRead, ReadCoverage
from camctl.history.events import (
    EventEnvelope,
    HistoryEventError,
    RowChange,
    branch_of,
    business_columns,
    changeable_columns,
    event_type_name,
    foreign_key_targets,
    load_event_registry,
    write_once_columns,
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
    沿登记关联解析应报告对象。transaction_rows 是完整事务提案的行
    事实，仅用于固定身份及历史归属关系；不得用于提前判断后来的
    状态或执行资格。未提供时，固定关联只能使用事件已有事实。
    read_coverage 声明本事务完整读取的范围，需确认集合完整性的
    守卫通过 complete_rows 读取该范围内的当前行。
    """

    transaction: TransactionRange
    owners: Mapping[tuple[str, int], tuple[str, int]]
    state_rows: Mapping[str, Mapping[int, Mapping[str, Any]]]
    transaction_rows: Mapping[str, Mapping[int, Mapping[str, Any]]] | None = None
    read_coverage: ReadCoverage = field(default_factory=ReadCoverage)
    baseline_reads: Mapping[int, BaselineRangeRead] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 每个事件只校验一次相关表的键，避免按主键查询时反复扫描；
        # bool、float 等与整数相等的键不能冒充可靠的物理行身份。
        for table in {table for table, _ in self.read_coverage.ranges}:
            for row_id in self.state_rows.get(table, {}):
                try:
                    ObjectId(row_id)
                except ValueError as error:
                    raise EventValidationError(f"完整读取范围的行身份无效: {table}#{row_id!r}") from error

    def complete_rows(self, table: str, column: str, identity: int) -> Mapping[int, Mapping[str, Any]]:
        """取得可靠完整范围的当前行；未知覆盖或无法判断成员时拒绝。

        主键以行映射的键为准，其他身份列必须显式存在；合法空关
        联不匹配正身份。只读取 state_rows，不读取事务未来提案。
        """
        if not self.read_coverage.covers(table, column, identity):
            raise EventValidationError(f"缺少完整读取范围: {table}.{column}={identity}")
        rows = self.state_rows.get(table, {})
        if column == "id":
            return {identity: rows[identity]} if identity in rows else {}
        result = {}
        for row_id, row in rows.items():
            if column not in row:
                raise EventValidationError(f"无法判断读取范围成员: {table}#{row_id}.{column} 缺失")
            value = row[column]
            if value is None:
                continue
            if not is_json_integer(value):
                raise EventValidationError(f"读取范围成员身份无效: {table}#{row_id}.{column}={value!r}")
            try:
                ObjectId(int(value))
            except ValueError as error:
                raise EventValidationError(f"读取范围成员身份越界: {table}#{row_id}.{column}") from error
            if value == identity:
                result[row_id] = row
        return result

    @property
    def association_rows(self) -> Mapping[str, Mapping[int, Mapping[str, Any]]]:
        """取得固定关联事实；显式空提案不表示可以退回事件状态。"""
        return self.state_rows if self.transaction_rows is None else self.transaction_rows


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
    if value is None:
        return
    definitions = load_enum_registry()
    enum_columns = {**definitions["enums"], **definitions["json_enums"]}
    definition = enum_columns.get(f"{table}.{column}")
    if definition is None:
        return
    if not is_json_integer(value):
        _fail(f"{table}.{column} 的枚举值必须是整数编号")
    if value not in set(definition["members"].values()):
        _fail(f"{table}.{column} 的值 {value} 不是登记的枚举编号")


def _allowed_value(value: Any, allowed: Any) -> bool:
    """按精确 JSON 语义判断分支取值是否在允许集合内；布尔不冒充数字。"""
    if value is None:
        return None in allowed
    return any(json_equal(value, option) for option in allowed)


def _match_create_spec(row: RowChange, spec: Mapping[str, Any]) -> bool:
    if spec.get("op") != "create" or row.table != spec["table"]:
        return False
    # 分支的行规格是备选集合：存在性不满足时尝试下一规格。
    if row.before.exists or not row.after.exists:
        return False
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
        if not _allowed_value(value, allowed):
            _fail(f"{row.table}#{row.row_id} 创建后 {column}={value!r} 不在允许范围 {allowed}")
    for column, value in row.after.values.items():
        _check_enum_value(row.table, column, value)
    return True


def _match_update_spec(row: RowChange, spec: Mapping[str, Any], event_name: str, branch_name: str) -> bool:
    if spec.get("op") != "update" or row.table != spec["table"]:
        return False
    # 分支的行规格是备选集合：存在性不满足时尝试下一规格。
    if not row.before.exists or not row.after.exists:
        return False
    before_keys = set(row.before.values)
    after_keys = set(row.after.values)
    if before_keys != after_keys:
        _fail(f"{row.table}#{row.row_id} 更新前后列集合不一致")
    declared = set(spec.get("columns", before_keys))
    if not before_keys or not before_keys <= declared:
        _fail(
            f"{row.table}#{row.row_id} 更新列 {sorted(before_keys)} 与声明的 {sorted(declared)} 不符"
        )
    missing = set(spec.get("required", ())) - before_keys
    if missing:
        _fail(f"{row.table}#{row.row_id} 缺少必要更新列: {sorted(missing)}")
    unchanged = {column for column in before_keys
                 if json_equal(row.before.values[column], row.after.values[column])}
    if unchanged:
        _fail(f"{row.table}#{row.row_id} 的更新列没有实际变化: {sorted(unchanged)}")
    allowed_columns = changeable_columns(row.table)
    illegal = before_keys - allowed_columns
    if illegal:
        _fail(f"{row.table}#{row.row_id} 更新了不可变或派生列: {sorted(illegal)}")
    reassigned = {
        column for column in before_keys & write_once_columns(row.table)
        if row.before.values[column] is not None
    }
    if reassigned:
        # 一次写列只允许从空值赋值；正文未变化列已被拒绝，出现在
        # 正文中的已赋值一次写列必然是再次更改。
        _fail(
            f"{row.table}#{row.row_id} 的一次写列 {sorted(reassigned)} 已赋值，不能再次更改"
        )
    for image in (row.before, row.after):
        for column, value in image.values.items():
            _check_enum_value(row.table, column, value)
    for column, allowed in spec.get("before", {}).items():
        if column in row.before.values:
            value = row.before.values[column]
            if not _allowed_value(value, allowed):
                _fail(
                    f"{row.table}#{row.row_id} 前置 {column}={value!r} 不在要求范围 {allowed}"
                )
    for column, allowed in spec.get("after", {}).items():
        if column in row.after.values:
            value = row.after.values[column]
            if not _allowed_value(value, allowed):
                _fail(
                    f"{row.table}#{row.row_id} 更新后 {column}={value!r} 不在允许范围 {allowed}"
                )
    _check_transitions(row, spec, event_name, branch_name)
    return True


def _check_transitions(row: RowChange, spec: Mapping[str, Any], event_name: str, branch_name: str) -> None:
    state_models = load_event_registry()["state_models"]
    for column, transition_ids in spec.get("transitions", {}).items():
        model = state_models.get(f"{row.table}.{column}")
        if model is None:
            _fail(f"{row.table}.{column} 没有状态模型")
        before_value = row.before.values.get(column)
        after_value = row.after.values.get(column)
        if json_equal(before_value, after_value):
            # 状态字段没有改变时无需状态转换；同一记录的其他真实变化
            # 由事件行规格与业务校验约束。精确比较使布尔与数字的差别
            # 不被 == 折叠，仍须命中已登记的转换边。
            continue
        edges = [edge for edge in model["edges"] if edge["id"] in transition_ids]
        if not edges:
            _fail(f"{row.table}.{column} 的转换 {transition_ids} 未登记")
        allowed_by = f"{event_name}.{branch_name}"
        for edge in edges:
            if (allowed_by in edge["by"]
                    and json_equal(edge["from"], before_value)
                    and json_equal(edge["to"], after_value)):
                break
        else:
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


def _row_facts(
    context: "EventContext",
    table: str,
    row_id: int,
    overlays: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[str, Any]:
    """按行编号取得归属解析所需的固定行关系（含自身编号）。

    完整提案允许解析本事务后续创建的关联对象。归属登记只沿固定
    身份列取值；overlays 保留同一事件内的行写入。这里不判断状态。
    """
    facts = dict(context.association_rows.get(table, {}).get(row_id, {}))
    overlay = overlays.get((table, row_id))
    if overlay is not None:
        facts.update(overlay)
    facts.setdefault("id", row_id)
    return facts


def _event_row_facts(
    row: RowChange,
    context: "EventContext",
    overlays: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[str, Any]:
    """事件行的固定归属事实：关联图叠加本事件写入的值。"""
    facts = _row_facts(context, row.table, row.row_id, overlays)
    facts.update(row.after.values)
    return facts


def _condition_matches(facts: Mapping[str, Any], column: str, condition: Any) -> bool:
    if condition == "present":
        return facts.get(column) is not None
    if isinstance(condition, list):
        return facts.get(column) in condition
    _fail(f"归属条件 {column}={condition!r} 未登记处理方式")
    return False  # 仅为类型完整；_fail 必然抛出。


def _resolve_owner_spec(
    owner_spec: Mapping[str, Any],
    table: str,
    facts: Mapping[str, Any],
    context: "EventContext",
    overlays: Mapping[tuple[str, int], Mapping[str, Any]],
    depth: int = 0,
) -> tuple[str, int]:
    """按登记的归属规格解析唯一历史对象 (实体名, 对象编号)。

    cases 按行事实选择分支；inherit 沿外键继承被引用行的归属；
    entity/id/via 沿外键链到达持有对象后读取编号列。解析所需的
    引用行的固定关系由完整事务提案提供；未提供提案时，使用事件
    之前的事实与本事件写入。缺少必需关系时明确拒绝。
    """
    if depth > 8:
        _fail(f"{table} 的归属解析链超出深度限制")
    if "cases" in owner_spec:
        for case in owner_spec["cases"]:
            when = case["when"]
            if all(_condition_matches(facts, column, rule) for column, rule in when.items()):
                return _resolve_owner_spec(
                    case["owner"], table, facts, context, overlays, depth + 1
                )
        return _resolve_owner_spec(
            owner_spec["otherwise"], table, facts, context, overlays, depth + 1
        )
    if "inherit" in owner_spec:
        column = owner_spec["inherit"]
        referenced = facts.get(column)
        if referenced is None:
            _fail(f"{table}.{column} 的继承归属引用为空")
        ref_table = foreign_key_targets().get((table, column))
        if ref_table is None:
            _fail(f"未登记外键指向: {table}.{column}")
        return _resolve_owner_spec(
            load_event_registry()["tables"][ref_table]["owner"],
            ref_table,
            _row_facts(context, ref_table, referenced, overlays),
            context,
            overlays,
            depth + 1,
        )
    current_table: str = table
    current_facts: Mapping[str, Any] = facts
    for column in owner_spec.get("via", ()):
        referenced = current_facts.get(column)
        if referenced is None:
            _fail(f"{current_table}.{column} 的归属链引用为空")
        ref_table = foreign_key_targets().get((current_table, column))
        if ref_table is None:
            _fail(f"未登记外键指向: {current_table}.{column}")
        current_table = ref_table
        current_facts = _row_facts(context, ref_table, referenced, overlays)
    owner_id = current_facts.get(owner_spec["id"])
    if owner_id is None:
        _fail(f"{current_table}.{owner_spec['id']} 的归属编号缺失")
    return owner_spec["entity"], owner_id


def _ownership_guard(event: EventEnvelope, context: EventContext) -> None:
    tables = load_event_registry()["tables"]
    overlays = {
        (row.table, row.row_id): dict(row.after.values) if row.after.exists else {}
        for row in event.rows
    }
    for row in event.rows:
        spec = tables.get(row.table)
        if spec is None:
            _fail(f"表 {row.table} 不在业务投影登记中")
        owner = context.owners.get((row.table, row.row_id))
        if owner is None:
            _fail(f"行 {row.table}#{row.row_id} 的历史归属未提供")
        expected = _resolve_owner_spec(
            spec["owner"], row.table, _event_row_facts(row, context, overlays), context, overlays
        )
        if owner != expected:
            _fail(
                f"行 {row.table}#{row.row_id} 的历史归属 {owner!r}"
                f" 与登记解析的 {expected!r} 不一致"
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


def _check_evidence_member(event_id: int, member: str, value: Any) -> None:
    """按格式 1 的公共与基准成员规则核对依据取值。

    成员白名单来自分支登记；此处只核对已出现成员的取值类型。
    布尔不是整数编号；驱动依据必须是结构化对象，成员顺序无关。
    """
    if member == "attempt_id":
        if not is_json_integer(value) or value <= 0:
            _fail(f"事件 {event_id} 的 evidence.attempt_id 必须是正整数")
    elif member == "input_key":
        if (not isinstance(value, str) or len(value) != 32
                or any(char not in "0123456789abcdef" for char in value)):
            _fail(f"事件 {event_id} 的 evidence.input_key 必须是 32 位十六进制身份")
    elif member == "observation":
        if not isinstance(value, Mapping):
            _fail(f"事件 {event_id} 的 evidence.observation 必须是结构化对象")
    elif member in ("activity_id", "chunk_no"):
        if not is_json_integer(value) or value <= 0:
            _fail(f"事件 {event_id} 的 evidence.{member} 必须是正整数")
    elif member in ("chunk_count", "entry_count"):
        if not is_json_integer(value) or value < 0:
            _fail(f"事件 {event_id} 的 evidence.{member} 必须是非负整数")
    elif member == "entries":
        if not isinstance(value, list):
            _fail(f"事件 {event_id} 的 evidence.entries 必须是数组")


def validate_event_structure(event: EventEnvelope) -> tuple[str, str, Mapping[str, Any]]:
    """保存与历史读取共用格式、分支及行权限，不执行业务守卫。"""
    try:
        for value in (event.event_id, event.transaction_id, event.event_type,
                      event.event_version, event.reason):
            ObjectId(value)
        UtcMicros(event.occurred_at)
        if event.change_seq is not None:
            ObjectId(event.change_seq)
        ObjectId(event.clock_status)
    except ValueError as error:
        raise EventValidationError(str(error)) from error
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
    _check_enum_value("history_events", "clock_status", event.clock_status)
    if not isinstance(event.evidence, Mapping):
        _fail(f"事件 {event.event_id} 的 evidence 必须是对象")
    undeclared = set(event.evidence) - set(branch["evidence"])
    if undeclared:
        _fail(
            f"事件 {event.event_id} 的 evidence 成员 {sorted(undeclared)}"
            f" 不在 {event_name}.{branch_name} 的登记成员内"
        )
    for member, value in event.evidence.items():
        _check_evidence_member(event.event_id, member, value)
    if not event.rows:
        _fail(f"事件 {event.event_id} 的 rows 为空")
    seen: set[tuple[str, int]] = set()
    for row in event.rows:
        try:
            ObjectId(row.row_id)
        except ValueError as error:
            raise EventValidationError(str(error)) from error
        for image in (row.before, row.after):
            if type(image.exists) is not bool or not isinstance(image.values, Mapping):
                _fail(f"行 {row.table}#{row.row_id} 的存在性和值结构非法")
            if not image.exists and image.values:
                _fail(f"不存在的行 {row.table}#{row.row_id} 不能携带列值")
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
    return event_name, branch_name, branch


def validate_event(event: EventEnvelope, context: EventContext) -> ValidatedEvent:
    """完整结构校验后执行业务守卫，再解析实际对象引用。"""
    event_name, branch_name, branch = validate_event_structure(event)
    for row in event.rows:
        if not row.before.exists:
            continue
        before = dict(context.state_rows.get(row.table, {}).get(row.row_id, {}))
        before.update(row.before.values)
        after = {**before, **row.after.values}
        spec = next(spec for spec in branch["rows"]
                    if spec["table"] == row.table and spec["op"] == "update")
        for side, facts in (("before", before), ("after", after)):
            for column, allowed in spec.get(side, {}).items():
                if column not in facts:
                    _fail(f"{row.table}#{row.row_id} 缺少 {side}.{column} 的可靠事实")
                if not any(json_equal(facts[column], value) for value in allowed):
                    _fail(f"{row.table}#{row.row_id} 的 {side}.{column} 不满足分支条件")

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
