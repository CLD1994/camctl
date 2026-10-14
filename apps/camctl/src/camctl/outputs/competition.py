"""同一产物的准备资格顺序；候选发现不保存选择或取得相机机会。"""

from typing import Any, Iterable, Mapping, Protocol

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, parse_object_id
from camctl.history.reads import ProductReadScope
from camctl.outputs.definitions import SelectionMode, read_selection_request
from camctl.outputs.sources import (
    ActionFacts, OriginalOutputs, ResolutionState, SourceSpec, resolve_source,
    select_for_original,
)
from camctl.scheduling.order import ProductCandidate, ProductKind, product_key


Row = Mapping[str, Any]
_ACTION = enum_for("actions.type")
_STATUS = enum_for("actions.status")
_SELECTION = enum_for("obtain_source_selections.status")
_ITEM = enum_for("obtain_items.status")
_TARGETS = enum_for("actions.target_selection_state")
_CLEANUP = enum_for("cleanup_items.status")
_RESTRICTION = enum_for("cleanup_items.restriction_state")
_AVAILABILITY = enum_for("outputs.availability")
_OUTPUT_CLEANUP = enum_for("outputs.cleanup_status")
_CAPTURE = frozenset((_ACTION.CAMERA_TAKE_PHOTO, _ACTION.CAMERA_RECORD, _ACTION.CAMERA_TIMELAPSE))
_TERMINAL = frozenset((_STATUS.SUCCEEDED, _STATUS.FAILED, _STATUS.EXPIRED, _STATUS.CANCELED))


class CompetitionReads(Protocol):
    """同一事件发生前的可靠事实；流式和静态集合接口分别补充成员读取。"""

    def actions(self) -> Iterable[tuple[int, Row]]: ...
    def required(self, table: str, identity: int) -> Row: ...
    def optional(self, table: str, identity: int) -> Row | None: ...
    def action(self, identity: int) -> Row | None: ...
    def family(self, output_id: int) -> OriginalOutputs: ...
    def processing_completed(self, source_id: int) -> bool: ...


class StaticCompetitionReads(CompetitionReads, Protocol):
    """静态边界事实已完整读取，可靠空集合也须有读取范围证明。"""

    def members(self, table: str, column: str, value: int) -> Mapping[int, Row]: ...
    def plan_actions(self, plan_id: int) -> Mapping[int, Row]: ...


class BatchedCompetitionReads(CompetitionReads, Protocol):
    """完整遍历由端口分批提供，不要求累计已处理成员。"""

    def iter_members(self, table: str, column: str, value: int) -> Iterable[tuple[int, Row]]: ...
    def iter_plan_actions(self, plan_id: int) -> Iterable[tuple[int, Row]]: ...


def _members(reads, table, column, identity):
    iterator = getattr(reads, "iter_members", None)
    return (reads.members(table, column, identity).items() if iterator is None
            else iterator(table, column, identity))


def _bounded_sources(reads, identity, action):
    """比较当前产物时逐个检查来源，不构造整组来源身份元组。"""
    state = _member(ResolutionState, action["source_resolution_state"])
    dependencies = _members(reads, "action_dependencies", "action_id", identity)
    if state == ResolutionState.FAILED:
        return
    if state == ResolutionState.FIXED:
        plan_id = _integer(action["resolved_source_plan_id"], minimum=1)
        for _, row in dependencies:
            source_id = _integer(row["depends_on_action_id"], minimum=1)
            source = _source_action(reads, source_id)
            if source["plan_id"] != plan_id or _member(_ACTION, source["type"]) not in _CAPTURE:
                raise ConsistencyError("固定来源成员不属于原来源计划的拍摄动作")
            yield source_id
        return
    if next(iter(dependencies), None) is not None:
        raise ConsistencyError("未固定来源不能携带已保存成员")
    raw = _params(action).get("source")
    if not isinstance(raw, dict):
        raise ConsistencyError("未固定来源缺少原引用")
    values = dict(raw)
    try:
        for name in ("action_instance_id", "plan_instance_id"):
            if name in values:
                values[name] = parse_object_id(values[name])
        spec = SourceSpec(**values)
    except (TypeError, ValueError) as error:
        raise ConsistencyError("资格候选的来源引用不可解释") from error
    if spec.action_instance_id is not None:
        row = reads.action(spec.action_instance_id)
        if row is not None and _member(_ACTION, row["type"]) in _CAPTURE:
            yield spec.action_instance_id
        return
    plan_id = spec.plan_instance_id or _integer(action["plan_id"], minimum=1)
    if spec.plan_instance_id is not None and reads.optional("plans", plan_id) is None:
        return
    for key, row in reads.iter_plan_actions(plan_id):
        if spec.action_name is not None and row["name"] != spec.action_name:
            continue
        if spec.group is not None and row["group_name"] != spec.group:
            continue
        if _member(_ACTION, row["type"]) in _CAPTURE:
            yield key


def _source_matches(reads, identity, action, target, *, all_ready=False):
    sources = (_bounded_sources(reads, identity, action)
               if hasattr(reads, "iter_plan_actions") else _sources(reads, identity, action))
    found, ready = False, True
    for source_id in sources:
        found |= source_id == target
        if all_ready:
            ready &= _source_ready(reads, source_id)
    return found and ready


def _integer(value, *, minimum=0) -> int:
    if not is_json_integer(value) or not minimum <= value <= MAX_OBJECT_ID:
        raise ConsistencyError(f"候选的整数事实无效: {value!r}")
    return int(value)


def _member(enum, value):
    try:
        return enum(_integer(value))
    except ValueError as error:
        raise ConsistencyError(f"候选状态不属于 {enum.__name__}: {value!r}") from error


def _key(identity: int, action: Row):
    kind = _member(_ACTION, action["type"])
    if kind not in (_ACTION.OBTAIN_ACTION_OUTPUTS, _ACTION.DELETE_ACTION_OUTPUTS):
        raise ConsistencyError("逐产物候选必须属于取回或清理")
    return product_key(ProductCandidate(
        _integer(identity, minimum=1),
        _integer(action["scheduled_at"], minimum=-MAX_OBJECT_ID - 1),
        _integer(action["plan_id"], minimum=1),
        _integer(action["input_index"]),
        ProductKind.OBTAIN if kind == _ACTION.OBTAIN_ACTION_OUTPUTS else ProductKind.DELETE,
    ))


def _params(action: Row) -> dict:
    fields = action["input_fields_json"]
    if not isinstance(fields, dict) or not isinstance(fields.get("params"), dict):
        raise ConsistencyError("资格候选缺少原请求参数")
    return fields["params"]


class _SourceLookup:
    def __init__(self, reads: CompetitionReads, owner: Row):
        self.reads, self.owner = reads, owner

    def owner_plan_id(self):
        return _integer(self.owner["plan_id"], minimum=1)

    def plan_exists(self, plan_id):
        return self.reads.optional("plans", plan_id) is not None

    @staticmethod
    def _fact(identity, row):
        return ActionFacts(identity, row["plan_id"], row["type"], row["name"], row["group_name"])

    def action_by_id(self, identity):
        row = self.reads.action(identity)
        return None if row is None else self._fact(identity, row)

    def plan_actions(self, plan_id):
        return tuple(self._fact(identity, row)
                     for identity, row in self.reads.plan_actions(plan_id).items())


def _source_action(reads: CompetitionReads, identity: int) -> Row:
    row = reads.action(identity)
    if row is None:
        raise ConsistencyError(f"固定来源动作缺失: {identity}")
    return row


def _sources(reads: CompetitionReads, identity: int, action: Row) -> tuple[int, ...]:
    state = _member(ResolutionState, action["source_resolution_state"])
    dependencies = reads.members("action_dependencies", "action_id", identity)
    if state == ResolutionState.FAILED:
        return ()
    if state == ResolutionState.FIXED:
        plan_id = _integer(action["resolved_source_plan_id"], minimum=1)
        members = []
        for dependency in dependencies.values():
            source_id = _integer(dependency["depends_on_action_id"], minimum=1)
            source = _source_action(reads, source_id)
            if source["plan_id"] != plan_id or _member(_ACTION, source["type"]) not in _CAPTURE:
                raise ConsistencyError("固定来源成员不属于原来源计划的拍摄动作")
            members.append(source_id)
        return tuple(members)
    if dependencies:
        raise ConsistencyError("未固定来源不能携带已保存成员")
    raw = _params(action).get("source")
    if not isinstance(raw, dict):
        raise ConsistencyError("未固定来源缺少原引用")
    values = dict(raw)
    try:
        for name in ("action_instance_id", "plan_instance_id"):
            if name in values:
                values[name] = parse_object_id(values[name])
        resolution = resolve_source(SourceSpec(**values), _SourceLookup(reads, action))
    except (ValueError, TypeError) as error:
        raise ConsistencyError("资格候选的来源引用不可解释") from error
    return resolution.member_action_ids if resolution.state == ResolutionState.FIXED else ()


def _source_ready(reads: CompetitionReads, source_id: int) -> bool:
    source = _source_action(reads, source_id)
    return (_member(_STATUS, source["status"]) in _TERMINAL
            and reads.processing_completed(source_id))


def _requested_ids(params: dict) -> tuple[int, ...]:
    values = params["output_ids"]
    if not isinstance(values, list) or not values:
        raise ConsistencyError("精确清理缺少非空原目标列表")
    try:
        identities = tuple(parse_object_id(value) for value in values)
    except ValueError as error:
        raise ConsistencyError("精确清理目标不是合法产物身份") from error
    if len(set(identities)) != len(identities):
        raise ConsistencyError("精确清理目标不能重复")
    return identities


def _cleanup_targets(reads: CompetitionReads, identity: int, action: Row, output_id: int, source_id: int) -> bool:
    state = _member(_TARGETS, action["target_selection_state"])
    if state == _TARGETS.FAILED:
        return False
    items = _members(reads, "cleanup_items", "action_id", identity)
    if state == _TARGETS.FIXED:
        for _, item in items:
            if item["requested_output_id"] != output_id:
                continue
            actual = item["output_id"]
            if actual not in (None, output_id):
                raise ConsistencyError("清理项的已确认产物与原请求不一致")
            stage = _member(_CLEANUP, item["status"])
            restriction = _member(_RESTRICTION, item["restriction_state"])
            if stage in (_CLEANUP.SUCCEEDED, _CLEANUP.FAILED, _CLEANUP.CANCELED):
                return False
            if stage != _CLEANUP.UNRESOLVED or restriction != _RESTRICTION.NOT_ESTABLISHED:
                raise ConsistencyError("可用产物不能同时存在已开始清理的责任")
            return True
        return False
    if next(iter(items), None) is not None:
        raise ConsistencyError("未固定清理集合不能已有目标明细")
    params = _params(action)
    if "output_ids" in params:
        return output_id in _requested_ids(params)
    return _source_matches(reads, identity, action, source_id, all_ready=True)


def _obtain_targets(reads: CompetitionReads, identity: int, action: Row, output_id: int, source_id: int) -> bool:
    if not _source_matches(reads, identity, action, source_id) or not _source_ready(reads, source_id):
        return False
    if action["source_resolution_state"] == int(ResolutionState.FIXED):
        dependencies = _members(reads, "action_dependencies", "action_id", identity)
        matching = []
        for key, row in dependencies:
            if row["depends_on_action_id"] == source_id:
                matching.append(key)
                if len(matching) > 1:
                    raise ConsistencyError("同一取回来源必须有唯一依赖")
        if len(matching) != 1:
            raise ConsistencyError("同一取回来源必须有唯一依赖")
        selections = iter(_members(reads, "obtain_source_selections", "dependency_id", matching[0]))
        first_selection = next(selections, None)
        if next(selections, None) is not None:
            raise ConsistencyError("固定来源必须有唯一选择责任")
        if _member(_STATUS, action["status"]) == _STATUS.PENDING:
            if first_selection is not None:
                raise ConsistencyError("尚未开始的取回不能已有来源选择责任")
        elif first_selection is None:
            raise ConsistencyError("固定来源必须有唯一选择责任")
        else:
            selection_id, selection = first_selection
            state = _member(_SELECTION, selection["status"])
            if state == _SELECTION.FIXED:
                if selection["error_code"] is not None:
                    return False
                # 仅读取涉及当前产物的成员，不加载其他原片或整个选择目录。
                for column in ("output_id", "requested_output_id"):
                    for _, item in _members(reads, "obtain_items", column, output_id):
                        if item["selection_id"] != selection_id:
                            continue
                        status = _member(_ITEM, item["status"])
                        if status in (_ITEM.SELECTED, _ITEM.UNRESOLVED):
                            if item["delivery_id"] is not None or item["source_dependency"] != 0:
                                raise ConsistencyError("尚未授予的取回项已携带准备责任")
                            return True
                return False
            if next(iter(_members(reads, "obtain_items", "selection_id", selection_id)), None) is not None:
                raise ConsistencyError("尚未固定的选择不能已有成员")
    try:
        mode, requested = read_selection_request(action["execution_spec_json"], action["input_fields_json"])
    except (TypeError, ValueError) as error:
        raise ConsistencyError("候选的取回选择定义与原请求不一致") from error
    if mode == SelectionMode.EXPLICIT_IDS:
        return output_id in requested
    family = reads.family(output_id)
    selected = select_for_original(family.source_action_id, family.original, mode,
                                   preview=family.preview, repaired=family.repaired)
    return selected.output_id == output_id and selected.status == int(_ITEM.SELECTED)


def has_product_predecessor(reads: CompetitionReads, action_id: int, output_id: int, occurred_at: int) -> bool:
    """是否存在更早的合格候选；读不到完整事实时抛错，不推测无阻挡。"""
    try:
        mine = _key(action_id, reads.required("actions", action_id))
        output = reads.required("outputs", output_id)
        if (_member(_AVAILABILITY, output["availability"]) != _AVAILABILITY.AVAILABLE
                or _member(_OUTPUT_CLEANUP, output["cleanup_status"]) not in (
                    _OUTPUT_CLEANUP.NOT_REQUESTED, _OUTPUT_CLEANUP.CANCELED)):
            raise ConsistencyError("首次授予要求产物当前可用且没有有效清理限制")
        if hasattr(reads, "view"):
            scope = reads.view.scope
            if not isinstance(scope, ProductReadScope) or (scope.action_id, scope.output_id, scope.occurred_at) != (action_id, output_id, occurred_at):
                raise ConsistencyError("逐产物事实读取不属于当前资格比较范围")
        for _, item in _members(reads, "cleanup_items", "output_id", output_id):
            if _member(_RESTRICTION, item["restriction_state"]) in (_RESTRICTION.ACTIVE, _RESTRICTION.IRREVERSIBLE):
                raise ConsistencyError("首次授予不能越过已生效的清理限制")
        source_id = _integer(output["source_action_id"], minimum=1)
        for identity, facts in reads.actions():
            kind = _member(_ACTION, facts["type"])
            if identity == action_id or kind not in (_ACTION.OBTAIN_ACTION_OUTPUTS, _ACTION.DELETE_ACTION_OUTPUTS):
                continue
            if facts["cancel_requested"] or _member(_STATUS, facts["status"]) not in (_STATUS.PENDING, _STATUS.RUNNING):
                continue
            other = _key(identity, facts)
            if other[0] > occurred_at or other >= mine:
                continue
            action = reads.required("actions", identity)
            targets = _obtain_targets if kind == _ACTION.OBTAIN_ACTION_OUTPUTS else _cleanup_targets
            if targets(reads, identity, action, output_id, source_id):
                return True
        return False
    except KeyError as error:
        raise ConsistencyError(f"逐产物候选缺少必要字段: {error}") from error
