"""纯公开投影与变化比较。

输入明确历史边界处的事实（行值与已选实体），输出公共契约字段；
不查询设备、不打开数据库、不执行副作用。字段、出现条件与转换
全部来自报告字段依赖登记（report-dependencies.json），按登记的
真实节点形状解释，不复制第二份规则。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Mapping, Sequence

from camctl.resources import resource_bytes
from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.json_values import MISSING, JsonParseError, is_json_integer, parse_exact_json
from camctl.contracts.input_fields import reconstruct_action_input
from camctl.contracts.values import ConsistencyError, format_utc_micros
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details

__all__ = [
    "OMIT",
    "ProjectionInput",
    "PublicFragment",
    "PublicProjectionError",
    "ProjectionStructure",
    "projection_structure",
    "project_public",
    "public_changed",
]

_RESOURCE = "registry/report-dependencies.json"


class PublicProjectionError(ValueError):
    """公开投影缺少必要事实、映射缺失或登记不可解释。"""


@dataclass(frozen=True)
class ProjectionInput:
    """一个对象在固定 H 的事实：行值、关联事实与已选实体。

    selected_entities 是当前对象的入选子树：键为子实体名，值为
    {子实体 ID: 该子对象自己的入选子树}。入选沿实体层级逐层限定，
    每个子对象只携带自己的入选后代。
    """

    entity: str
    root_id: int
    tables: Mapping[str, Mapping[int, Mapping[str, Any]]]
    selected_entities: Mapping[str, Mapping[int, Mapping[str, Any]]] = field(default_factory=dict)


PublicFragment = dict[str, Any]


@dataclass(frozen=True)
class ProjectionStructure:
    """公开对象的身份字段及实体子集合；顺序来自字段依赖登记。"""

    identity_field: str | None
    entity_fields: tuple[tuple[str, str], ...]


class _Omit:
    """合法省略标记。"""

    _instance: "_Omit | None" = None

    def __new__(cls) -> "_Omit":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - 诊断用途
        return "OMIT"


OMIT = _Omit()


@lru_cache(maxsize=1)
def _dependencies() -> dict[str, Any]:
    document = parse_exact_json(resource_bytes(_RESOURCE).decode("utf-8"))
    if document.get("format_version") != 1:
        raise PublicProjectionError("报告字段依赖登记版本不受支持")
    return document


def projection_structure(entity: str) -> ProjectionStructure:
    """从同一权威投影取得编码结构，不复制字段与实体清单。"""
    definition = _dependencies()["projections"].get(entity)
    if definition is None:
        raise PublicProjectionError(f"未登记的公开实体投影: {entity}")
    identities = []
    children = []
    for name, field_spec in definition["fields"].items():
        value = field_spec["value"]
        if (value.get("op") == "read" and value.get("encoding") == "id"
                and value.get("column") == definition["root_table"] + ".id"):
            identities.append(name)
        if value.get("op") == "entities":
            order = value.get("encoding_order")
            if not is_json_integer(order) or not 1 <= order <= len(definition["fields"]):
                raise PublicProjectionError(f"实体子集合 {entity}.{name} 缺少合法编码顺序")
            children.append((int(order), name, value["entity"]))
    if len(identities) > 1:
        raise PublicProjectionError(f"公开投影 {entity} 声明了多个对象身份")
    children.sort()
    if [order for order, _, _ in children] != list(range(1, len(children) + 1)):
        raise PublicProjectionError(f"投影 {entity} 的子集合编码顺序必须唯一且连续")
    return ProjectionStructure(identities[0] if identities else None,
                               tuple((name, child) for _, name, child in children))


def _enum_member(column: str, value: int) -> str:
    members = load_enum_registry()["enums"].get(column, {}).get("members", {})
    for member_name, code in members.items():
        if code == value:
            return member_name
    raise PublicProjectionError(f"{column} 的枚举编号 {value} 未登记")


@dataclass(frozen=True)
class _Context:
    input: ProjectionInput
    row: Mapping[str, Any]
    table: str
    row_id: int
    #: 当前投影声明的关联；跨表列沿这些关联解析到唯一关联行。
    relations: tuple[str, ...] = ()

    def with_row(self, table: str, row_id: int) -> "_Context":
        row = self.input.tables.get(table, {}).get(row_id)
        if row is None:
            raise PublicProjectionError(f"缺少 {table}#{row_id} 的 H 事实")
        return _Context(self.input, row, table, row_id, self.relations)

    def column(self, column: str) -> Any:
        table, _, name = column.rpartition(".")
        if table == self.table:
            source: Mapping[str, Any] | None = self.row
            source_id: int | None = self.row_id
        else:
            source, source_id = self._related_column_source(table, column)
        if source is None:
            # optional 关联无关联行：跨表列按 SQL NULL 参与条件与取值。
            return None
        if name == "id":
            return source_id
        if name not in source:
            raise PublicProjectionError(f"{table}#{source_id} 缺少列 {name} 的 H 事实")
        return source[name]

    def _related_column_source(
        self, table: str, column: str
    ) -> tuple[Mapping[str, Any] | None, int | None]:
        """沿投影声明的关联解析跨表列的唯一关联行。

        声明关联作为无向边连通投影的各表：正向步沿外键取子行，
        反向步沿同一外键取回父行。枚举到达目标表的全部路径；可选
        外键为空使该路径无行，跨表列按 SQL NULL 参与条件与取值。
        """
        registry = _dependencies()["relations"]
        adjacency: dict[str, list[tuple[Mapping[str, Any], bool]]] = {}
        for name in self.relations:
            relation = registry[name]
            from_table = relation["from"].split(".")[0]
            to_table = relation["to"].split(".")[0]
            if from_table == to_table:
                continue
            adjacency.setdefault(from_table, []).append((relation, True))
            adjacency.setdefault(to_table, []).append((relation, False))
        paths = _table_paths(adjacency, self.table, table)
        if not paths:
            raise PublicProjectionError(f"列 {column} 不在当前上下文表 {self.table} 中")
        final: list[tuple[str, int]] = []
        null_gap = False
        missing = False
        for path in paths:
            positions = ((self.table, self.row_id),)
            died_by_null = False
            for relation, forward in path:
                positions, any_anchor = _hop_positions(
                    self.input.tables, relation, positions, forward)
                if positions:
                    continue
                died_by_null = not any_anchor
                break
            if positions:
                final.extend(positions)
            elif died_by_null:
                null_gap = True
            else:
                missing = True
        if final:
            unique = sorted(set(final))
            if len(unique) > 1:
                raise PublicProjectionError(
                    f"列 {column} 需要恰好一行 {table} 关联事实，实际 {len(unique)} 行")
            row = self.input.tables.get(table, {}).get(unique[0][1])
            if row is None:
                raise PublicProjectionError(
                    f"缺少 {table}#{unique[0][1]} 的 H 事实")
            return row, unique[0][1]
        if null_gap and not missing:
            # optional 关联无关联行：跨表列按 SQL NULL 参与条件与取值。
            return None, None
        raise PublicProjectionError(
            f"列 {column} 引用的 {table} 关联事实缺失（外键起点 {self.table}#{self.row_id}）"
        )

    def related(self, relations: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
        """沿关系链取相关行（当前行出发，每步沿声明关联推进）。"""
        current: tuple[tuple[str, int], ...] = ((self.table, self.row_id),)
        registry = _dependencies()["relations"]
        for relation_name in relations:
            relation = registry[relation_name]
            from_table = relation["from"].split(".")[0]
            from_field = relation["from"].split(".")[1]
            to_table = relation["to"].split(".")[0]
            to_field = relation["to"].split(".")[1]
            next_positions: list[tuple[str, int]] = []
            rows_from = self.input.tables.get(from_table, {})
            rows_to = self.input.tables.get(to_table, {})
            for candidate_id, candidate in rows_to.items():
                target_value = (
                    candidate_id if to_field == "id" else candidate.get(to_field)
                )
                for source_id, source in rows_from.items():
                    source_value = (
                        source_id if from_field == "id" else source.get(from_field)
                    )
                    if source_value != target_value:
                        continue
                    if (from_table, source_id) in current or (
                        from_table == self.table and source_id == self.row_id
                    ):
                        next_positions.append((to_table, candidate_id))
            current = tuple(sorted(set(next_positions)))
            if not current:
                return ()
        return current


def project_public(facts: ProjectionInput) -> PublicFragment:
    """计算一个对象的完整公开字段（出现条件参与字段级省略）。"""
    projection = _dependencies()["projections"].get(facts.entity)
    if projection is None:
        raise PublicProjectionError(f"未登记的公开实体投影: {facts.entity}")
    root_table = projection["root_table"]
    root = facts.tables.get(root_table, {}).get(facts.root_id)
    if root is None:
        raise PublicProjectionError(
            f"缺少 {root_table}#{facts.root_id} 的 H 事实（对象未出生或未取得）"
        )
    if facts.entity == "action":
        try:
            reconstruct_action_input(root)
        except (ConsistencyError, JsonParseError, KeyError, ValueError) as error:
            raise PublicProjectionError(str(error)) from error
    context = _Context(facts, root, root_table, facts.root_id,
                       tuple(projection.get("relations", ())))
    if not _truthy(projection.get("when", {"op": "literal", "value": True}), context):
        return OMIT  # 类型检查器友好的省略标记
    fragment: PublicFragment = {}
    for field_name, spec in projection["fields"].items():
        when = spec.get("when", {"op": "literal", "value": True})
        if not _truthy(when, context):
            continue
        value = _eval(spec["value"], context)
        if value is OMIT:
            continue
        fragment[field_name] = value
    return fragment


def public_changed(before: PublicFragment, after: PublicFragment) -> bool:
    """公开字段值或出现条件是否实际变化。"""
    if set(before) != set(after):
        return True
    for key in before:
        if before[key] != after[key]:
            return True
    return False


def _truthy(node: Mapping[str, Any], context: _Context) -> bool:
    return bool(_eval(node, context))


def _eval(node: Mapping[str, Any], context: _Context) -> Any:
    op = node.get("op")
    if op == "literal":
        return node["value"]
    if op == "omit":
        return OMIT
    if op == "error":
        raise PublicProjectionError(f"登记要求状态库错误: {node['code']}")
    if op == "read":
        return _read(node, context)
    if op == "enum":
        value = context.column(node["column"])
        member = _enum_member(node["column"], value)
        mapped = node["map"].get(member)
        if mapped is None:
            raise PublicProjectionError(f"{node['column']} 的成员 {member} 没有公开映射")
        return mapped
    if op == "enum_is":
        value = context.column(node["column"])
        if value is None:
            return False
        return _enum_member(node["column"], value) in node.get("members", ())
    if op == "not_null":
        return context.column(node["column"]) is not None
    if op == "eq":
        return _eval(node["left"], context) == _eval(node["right"], context)
    if op == "exists":
        relation = _relation_between(context.table, node["table"])
        if relation is None:
            return False
        return bool(context.related((relation,)))
    if op == "json_has":
        document = _json(node["column"], context)
        return _pointer(document, node["pointer"]) is not MISSING
    if op == "json_member":
        document = _json(node["column"], context)
        value = _pointer(document, node["pointer"])
        if value is MISSING:
            raise PublicProjectionError(f"{node['column']} 缺少成员 {node['pointer']!r}")
        return value
    if op == "all":
        return all(_truthy(child, context) for child in node["args"])
    if op == "any":
        return any(_truthy(child, context) for child in node["args"])
    if op == "not":
        return not _truthy(node["arg"], context)
    if op == "cases":
        for branch in node["branches"]:
            if _truthy(branch["when"], context):
                return _eval(branch["value"], context)
        return _eval(node["otherwise"], context)
    if op == "project":
        return _project(node["projection"], context)
    if op == "entities":
        return _entities(node, context)
    if op == "rows":
        return _rows(node, context)
    if op == "registered_error":
        return _registered_error(node, context)
    if op == "extra_input":
        return _extra_input(node, context)
    if op == "failure_union":
        return _failure_union(node, context)
    raise PublicProjectionError(f"未登记的操作: {op!r}")


def _read(node: Mapping[str, Any], context: _Context) -> Any:
    value = context.column(node["column"])
    encoding = node.get("encoding", "identity")
    if encoding == "identity":
        return value
    if encoding == "id":
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PublicProjectionError(f"{node['column']} 不是合法对象 ID: {value!r}")
        return str(value)
    if encoding == "utc_seconds":
        if not isinstance(value, int) or isinstance(value, bool):
            raise PublicProjectionError(f"{node['column']} 不是整数微秒: {value!r}")
        return format_utc_micros(value)
    if encoding == "json":
        return _json(node["column"], context)
    raise PublicProjectionError(f"未登记的读取编码: {encoding!r}")


def _json(column: str, context: _Context) -> Any:
    value = context.column(column)
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return parse_exact_json(value)
    except (JsonParseError, TypeError) as error:
        raise PublicProjectionError(f"{column} 不是合法 JSON 文本: {error}") from error


def _pointer(document: Any, pointer: str) -> Any:
    node: Any = document
    if pointer == "":
        return node
    for encoded in pointer.lstrip("/").split("/"):
        segment = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or segment not in node:
            return MISSING
        node = node[segment]
    return node


def _relation_between(source_table: str, target_table: str) -> str | None:
    for name, relation in _dependencies()["relations"].items():
        if (
            relation["from"].split(".")[0] == source_table
            and relation["to"].split(".")[0] == target_table
        ):
            return name
    return None


def _project(projection_name: str, context: _Context) -> Any:
    projection = _dependencies()["projections"].get(projection_name)
    if projection is None:
        raise PublicProjectionError(f"未登记的嵌套投影: {projection_name}")
    target_table = projection["root_table"]
    if target_table == context.table:
        positions: tuple[tuple[str, int], ...] = ((context.table, context.row_id),)
    else:
        relation = _relation_between(context.table, target_table)
        if relation is None:
            raise PublicProjectionError(
                f"嵌套投影 {projection_name} 与 {context.table} 无声明关联"
            )
        positions = context.related((relation,))
    if len(positions) != 1:
        raise PublicProjectionError(
            f"嵌套投影 {projection_name} 需要恰好一行关联事实，实际 {len(positions)} 行"
        )
    return project_public(
        ProjectionInput(
            entity=projection_name,
            root_id=positions[0][1],
            tables=context.input.tables,
            selected_entities=context.input.selected_entities,
        )
    )


def _entities(node: Mapping[str, Any], context: _Context) -> Any:
    entity = node["entity"]
    children = context.input.selected_entities.get(entity, {})
    if not isinstance(children, Mapping):
        raise PublicProjectionError(f"实体集合 {entity} 的入选必须是映射")
    if not children:
        return OMIT if node.get("empty") == "omit" else []
    fragments: list[Any] = []
    for child_id, subtree in children.items():
        if not isinstance(child_id, int) or isinstance(child_id, bool) or child_id < 1:
            raise PublicProjectionError(f"实体集合 {entity} 的入选身份非法: {child_id!r}")
        if not isinstance(subtree, Mapping):
            raise PublicProjectionError(f"实体 {entity}#{child_id} 的入选子树必须是映射")
        fragments.append(
            project_public(
                ProjectionInput(
                    entity=entity,
                    root_id=child_id,
                    tables=context.input.tables,
                    selected_entities=subtree,
                )
            )
        )
    return fragments


def _rows(node: Mapping[str, Any], context: _Context) -> Any:
    matched = context.related(tuple(node["relations"]))
    if not matched and node.get("empty") == "omit":
        return OMIT
    return [
        project_public(
            ProjectionInput(
                entity=node["projection"],
                root_id=row_id,
                tables=context.input.tables,
                selected_entities=context.input.selected_entities,
            )
        )
        for _table, row_id in matched
    ]


def _registered_error(node: Mapping[str, Any], context: _Context) -> Any:
    code_value = context.column(node["code_column"])
    if code_value is None:
        raise PublicProjectionError("错误字段要求非空错误编号")
    details_value = context.column(node["details_column"])
    try:
        name, spec = registered_error_spec(node["registry_key"], code_value)
        details = parse_exact_json(details_value) if isinstance(details_value, str) else details_value
        validate_error_details(name, details)
    except (TypeError, ValueError) as error:
        raise PublicProjectionError(str(error)) from error
    return {"code":name, "stage":spec["stage"], "details":details}


def _extra_input(node: Mapping[str, Any], context: _Context) -> Any:
    document = _json(node["column"], context)
    if document is None:
        return OMIT
    if not isinstance(document, Mapping):
        raise PublicProjectionError("额外输入要求对象事实")
    plan_schema = json.loads(resource_bytes("protocol/plan.schema.json"))
    excluded = set(plan_schema["$defs"]["action"].get("properties", {}))
    extras = {key: value for key, value in document.items() if key not in excluded}
    return extras or OMIT


def _table_paths(
    adjacency: Mapping[str, Sequence[tuple[Mapping[str, Any], bool]]],
    source: str, target: str,
) -> list[tuple[tuple[Mapping[str, Any], bool], ...]]:
    """在投影声明的关联图上枚举 source 到 target 的全部简单路径。"""
    found: list[tuple[tuple[Mapping[str, Any], bool], ...]] = []

    def walk(current: str, visited: set[str],
             path: tuple[tuple[Mapping[str, Any], bool], ...]) -> None:
        if current == target:
            found.append(path)
            return
        for relation, forward in adjacency.get(current, ()):
            far = (
                relation["to"].split(".")[0] if forward
                else relation["from"].split(".")[0])
            if far in visited:
                continue
            walk(far, visited | {far}, path + ((relation, forward),))

    walk(source, {source}, ())
    return found


def _hop_positions(
    tables: Mapping[str, Mapping[int, Mapping[str, Any]]],
    relation: Mapping[str, Any],
    positions: tuple[tuple[str, int], ...],
    forward: bool,
) -> tuple[tuple[tuple[str, int], ...], bool]:
    """沿一条关联推进行位置，返回（到达位置, 是否存在非空外键）。

    forward 为 True 时从 from 端推进到 to 端（取子行）；False 时从
    to 端推进回 from 端（取父行）。两端按外键值相等匹配。
    """
    from_table, _, from_field = relation["from"].rpartition(".")
    to_table, _, to_field = relation["to"].rpartition(".")
    if forward:
        near_table, near_field = from_table, from_field
        far_table, far_field = to_table, to_field
    else:
        near_table, near_field = to_table, to_field
        far_table, far_field = from_table, from_field
    near_rows = tables.get(near_table, {})
    far_rows = tables.get(far_table, {})
    matched: set[tuple[str, int]] = set()
    any_anchor = False
    for _table, near_id in positions:
        near_row = near_rows.get(near_id)
        if near_row is None:
            raise PublicProjectionError(f"缺少 {near_table}#{near_id} 的 H 事实")
        anchor = near_id if near_field == "id" else near_row.get(near_field)
        if anchor is None:
            continue
        any_anchor = True
        for far_id, far_row in far_rows.items():
            value = far_id if far_field == "id" else far_row.get(far_field)
            if value == anchor:
                matched.add((far_table, far_id))
    return tuple(sorted(matched)), any_anchor


def _relation_paths(
    context: _Context, relations: tuple[str, ...],
) -> list[tuple[tuple[str, int], ...]]:
    """沿关系链正向推进并保留每条完整到达路径。

    与 Context.related 的逐级匹配同义，但保留中间表行位置，供顺
    序键沿路径解析；同一终点经不同父行到达时保留全部路径。
    """
    registry = _dependencies()["relations"]
    paths = [((context.table, context.row_id),)]
    previous_table = context.table
    for relation_name in relations:
        relation = registry[relation_name]
        from_table = relation["from"].split(".")[0]
        from_field = relation["from"].split(".")[1]
        to_table = relation["to"].split(".")[0]
        to_field = relation["to"].split(".")[1]
        if from_table != previous_table:
            raise PublicProjectionError(
                f"关系链在 {previous_table} 之后经 {relation_name} 离开 {from_table}，链不连贯")
        rows_from = context.input.tables.get(from_table, {})
        rows_to = context.input.tables.get(to_table, {})
        next_paths = []
        for candidate_id, candidate in rows_to.items():
            target_value = (
                candidate_id if to_field == "id" else candidate.get(to_field))
            for path in paths:
                source_id = path[-1][1]
                source = rows_from.get(source_id)
                if source is None:
                    continue
                source_value = (
                    source_id if from_field == "id" else source.get(from_field))
                if source_value == target_value:
                    next_paths.append(path + ((to_table, candidate_id),))
        paths = next_paths
        previous_table = to_table
        if not paths:
            break
    return paths


def _chain_order_value(
    key: str, path: tuple[tuple[str, int], ...],
    tables: Mapping[str, Mapping[int, Mapping[str, Any]]],
) -> Any:
    """沿到达路径解析顺序键：路径行上的 id 或普通列值。"""
    table, _, name = key.rpartition(".")
    positions = [row_id for path_table, row_id in path if path_table == table]
    if len(positions) != 1:
        raise PublicProjectionError(f"顺序键 {key} 不在到达路径的恰一表上")
    if name == "id":
        return positions[0]
    row = tables.get(table, {}).get(positions[0])
    if row is None or name not in row:
        raise PublicProjectionError(f"顺序键 {key} 缺少 H 事实")
    return row[name]


def _failure_union(node: Mapping[str, Any], context: _Context) -> Any:
    """取回失败汇总：各分支沿登记关系链展开，按依赖、分支与身份排序。

    分支出现条件不满足的行不进入汇总；同一分支重复返回同一身份
    是查询或归属问题，按登记以状态库错误暴露，不静默去重。
    """
    projections = _dependencies()["projections"]
    entries: list[tuple[tuple[Any, ...], int, Any, PublicFragment]] = []
    for branch_index, branch in enumerate(node["branches"]):
        projection = projections.get(branch["projection"])
        if projection is None:
            raise PublicProjectionError(f"未登记的失败分支投影: {branch['projection']}")
        root_table = projection["root_table"]
        identity_table, _, _identity_field = branch["identity"].rpartition(".")
        if identity_table != root_table:
            raise PublicProjectionError(
                f"失败分支 {branch['projection']} 的身份列不在根表 {root_table} 上")
        for path in _relation_paths(context, tuple(branch["relations"])):
            terminal_table, terminal_id = path[-1]
            if terminal_table != root_table:
                raise PublicProjectionError(
                    f"失败分支 {branch['projection']} 的关系链止于 {terminal_table}，"
                    f"不是根表 {root_table}")
            fragment = project_public(ProjectionInput(
                entity=branch["projection"], root_id=terminal_id,
                tables=context.input.tables,
                selected_entities=context.input.selected_entities))
            if fragment is OMIT:
                continue
            identity = context.with_row(
                terminal_table, terminal_id).column(branch["identity"])
            order_values = tuple(
                branch_index if key == "branch_index"
                else identity if key == "branch_identity"
                else _chain_order_value(key, path, context.input.tables)
                for key in node["order_by"])
            entries.append((order_values, branch_index, identity, fragment))
    seen: set[tuple[int, Any]] = set()
    for _order, branch_index, identity, _fragment in entries:
        if (branch_index, identity) in seen:
            if node.get("duplicate") != "state_database_error":
                raise PublicProjectionError(
                    f"未登记的重复身份处理: {node.get('duplicate')!r}")
            raise PublicProjectionError(
                f"失败分支 {branch_index} 重复返回身份 {identity!r}，按状态库错误处理")
        seen.add((branch_index, identity))
    if not entries and node.get("empty") == "omit":
        return OMIT
    entries.sort(key=lambda entry: entry[0])
    return [fragment for _order, _index, _identity, fragment in entries]
