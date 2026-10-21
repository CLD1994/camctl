"""报告的 Schema 义务与有界字段完成证据。

调用者提供权威本地 registry 和投影的延迟路径；本模块不读资源、
数据库或文件。有限子树仍由既有精确 validator 校验，跨延迟字段
只派生当前报告所需的结构谓词。此处不负责历史完整性或文件发布。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import unquote, urldefrag, urljoin

from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import MISSING
from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.values import ObjectId

_DRAFT = 'https://json-schema.org/draft/2020-12/schema'
_SEAL = object()
Path = tuple[str, ...]


class StreamValidationError(ValueError):
    """规则不能拆分，或字段/对象尚不能取得完整成功回执。"""


class FieldKind(StrEnum):
    ARRAY = 'array'
    VALUE = 'value'
    OBJECT = 'object'


class FieldState(StrEnum):
    UNDECIDED = 'undecided'
    OMITTED = 'omitted'
    OPEN = 'open'
    CLOSED = 'closed'
    FAILED = 'failed'


@dataclass(frozen=True)
class DeferredField:
    path: Path
    kind: FieldKind = FieldKind.ARRAY
    omit_empty: bool = False
    item: ProjectionDescriptor | None = None
    node: NodeValidationPlan | None = None
    # 已登记稳定身份的严格顺序；uniqueItems 不允许无界去重集。
    identity: Callable[[Any], Any] | None = None
    # 按业务顺序写出的成员，可由同一完成义务核权威唯一关系。
    unique_by: str | None = None


@dataclass(frozen=True)
class CompletionRequirement:
    """业务展开器必须完成的字段义务；此声明不证明已经完成。"""
    name: str
    path: Path
    unique_fields: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ProjectionDescriptor:
    pointer: str
    fields: tuple[DeferredField, ...] = ()
    completions: tuple[CompletionRequirement, ...] = ()


@dataclass(frozen=True)
class NodeBinding:
    instance_id: str
    owner_entity: str
    owner_id: int
    boundary: HistoryBoundary
    occurrence: tuple[str | int, ...]
    schema_uri: str
    projection_pointer: str

    def __post_init__(self):
        ObjectId(self.owner_id)
        if not isinstance(self.boundary, HistoryBoundary):
            raise StreamValidationError('对象绑定缺少完整 H')
        if any(not isinstance(value, str) or not value for value in (
                self.instance_id, self.owner_entity, self.schema_uri, self.projection_pointer)):
            raise StreamValidationError('对象绑定缺少身份或节点')
        if (not isinstance(self.occurrence, tuple) or
                any(type(value) not in (str, int) for value in self.occurrence)):
            raise StreamValidationError('对象出现位置必须是固定路径')


@dataclass(frozen=True)
class ReadEnd:
    """上层读取端口确认同绑定页已耗尽后的有限证据。"""
    binding: NodeBinding
    path: Path
    count: int
    last_position: str | int | None


@dataclass(frozen=True)
class CompletionRequest:
    binding: NodeBinding
    requirement: CompletionRequirement
    counts: tuple[tuple[Path, int], ...]
    plan: NodeValidationPlan


@runtime_checkable
class CompletionProof(Protocol):
    """由真实业务展开器拥有的一次性凭据；失败必须抛出错误。

    consume 必须核原 plan 实例、字段/source/H/ref、声明覆盖的唯一关系及耗尽事实。A1 只
    提供此接缝，不生成分段业务凭据，布尔值不能替代它。
    """
    def consume(self, request: CompletionRequest) -> None: ...


@dataclass(frozen=True)
class FiniteCheck:
    schema_uri: str
    value_path: Path


@dataclass(frozen=True)
class ParentPredicate:
    schema_uri: str
    value_path: Path
    keyword: str


@dataclass(frozen=True)
class FieldObligation:
    path: Path
    schema_uris: tuple[str, ...]
    array_rules: tuple[tuple[str, str], ...]
    item_schema_uris: tuple[str, ...]


@dataclass(frozen=True)
class _Predicate:
    uri: str
    check: Callable[[Any], bool]


@dataclass
class _Array:
    count: int = 0
    last_position: str | int | None = None
    last_identity: Any = MISSING
    item_results: dict[str, bool] = field(default_factory=dict)


@dataclass
class _Slot:
    spec: DeferredField
    state: FieldState = FieldState.UNDECIDED
    array: _Array = field(default_factory=_Array)


def _child(uri: str, *parts: str | int) -> str:
    return uri + ('/' if '#' in uri else '#/') + '/'.join(
        str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def _reference(uri: str, reference: str, registry: Registry) -> str:
    # Pointer 经过独立资源 $id 时，兄弟相对引用从该资源解析。
    document, fragment = urldefrag(uri)
    value = registry.resolver().lookup(document).contents
    base = document
    if isinstance(value, dict) and '$id' in value:
        base = urljoin(base, value['$id'])
    if fragment:
        if not fragment.startswith('/'):
            raise StreamValidationError(f'跨延迟引用需要原 JSON Pointer: {uri}')
        for token in unquote(fragment)[1:].split('/'):
            part = token.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
            if isinstance(value, dict) and '$id' in value:
                base = urljoin(base, value['$id'])
    return urljoin(base, reference)


def _at(value: Any, path: Path):
    for name in path:
        if not isinstance(value, dict) or name not in value:
            return MISSING
        value = value[name]
    return value


class _Compiler:
    # 仅跨延迟结构的关键字；有限子树不受这张解释能力表限制。
    _STRUCTURAL = frozenset({
        '$ref', 'type', 'properties', 'additionalProperties', 'required',
        'dependentRequired', 'minProperties', 'maxProperties', 'allOf',
        'anyOf', 'not', 'if', 'then', 'else', 'items', 'minItems',
        'maxItems', 'uniqueItems',
    })
    _ANNOTATIONS = frozenset({
        '$schema', '$id', '$defs', '$comment', 'title', 'description',
        'default', 'examples', 'readOnly', 'writeOnly', 'deprecated',
    })

    def __init__(self, registry: Registry, projection: ProjectionDescriptor):
        self.registry, self.projection = registry, projection
        self.specs = {}
        self.unique_covered: set[Path] = set()
        requirements = {requirement.name: requirement for requirement in projection.completions}
        for spec in projection.fields:
            self.specs[spec.path] = spec
            if spec.unique_by is not None:
                requirement = requirements.get(spec.unique_by)
                if (requirement is None or spec.path not in requirement.unique_fields
                        or spec.path[:len(requirement.path)] != requirement.path):
                    raise StreamValidationError('唯一关系义务必须覆盖准确字段')
                self.unique_covered.add(spec.path)
            if spec.node is not None:
                if spec.node._compiler.registry is not registry:
                    raise StreamValidationError('对象子计划必须引用同一权威注册表')
                for child_path, child in spec.node._compiler.specs.items():
                    self.specs[spec.path + child_path] = replace(child, path=spec.path + child_path)
                    if child_path in spec.node._compiler.unique_covered:
                        self.unique_covered.add(spec.path + child_path)
        self.predicates: dict[tuple[str, Path], _Predicate] = {}
        self.visiting: set[tuple[str, Path]] = set()
        self.items: dict[Path, dict[str, NodeValidationPlan]] = {}
        self.reached: set[Path] = set()
        self.preflighted: set[int] = set()
        self.finite_checks: list[FiniteCheck] = []
        self.parent_predicates: list[ParentPredicate] = []

    @staticmethod
    def _check_dialect(schema, uri):
        if isinstance(schema, dict) and schema.get('$schema', _DRAFT) != _DRAFT:
            raise StreamValidationError(f'Schema 方言不是 Draft 2020-12: {uri}')

    def _preflight(self, resource, resolver, uri):
        """按原方言枚举有限子规则，只提前核本地引用，不解释其语义。"""
        contents = resource.contents
        self._check_dialect(contents, uri)
        identity = id(contents)
        if identity in self.preflighted:
            return
        self.preflighted.add(identity)
        if isinstance(contents, dict) and '$ref' in contents:
            try:
                resolved = resolver.lookup(contents['$ref'])
            except Unresolvable as error:
                raise StreamValidationError(f'本地 Schema 不可解释: {uri}: {contents["$ref"]}') from error
            self._preflight(DRAFT202012.create_resource(resolved.contents), resolved.resolver, uri)
        for child in resource.subresources():
            self._preflight(child, resolver.in_subresource(child), uri)

    def finite(self, uri, path):
        resolved = self.registry.resolver().lookup(uri)
        self._preflight(DRAFT202012.create_resource(resolved.contents), resolved.resolver, uri)
        validator = create_validator({'$schema': _DRAFT, '$ref': uri}, registry=self.registry)
        self.finite_checks.append(FiniteCheck(uri, path))
        def valid(value):
            if value is MISSING:
                return True
            return not validation_errors(validator, value)
        return _Predicate(uri, valid)

    def compile(self, uri: str, path: Path = ()) -> _Predicate:
        key = (uri, path)
        if key in self.visiting:
            raise StreamValidationError(f'跨延迟循环引用: {uri}')
        if key in self.predicates:
            return self.predicates[key]
        try:
            schema = self.registry.resolver().lookup(uri).contents
        except Unresolvable as error:
            raise StreamValidationError(f'本地 Schema 不可解释: {uri}') from error
        self._check_dialect(schema, uri)
        self.visiting.add(key)
        try:
            relevant = [spec for location, spec in self.specs.items()
                        if location[:len(path)] == path]
            if path in self.specs:
                self.reached.add(path)
            # 仅延迟一个有限值时，在它真实给定后仍可完整委托。
            structured = any(spec.kind is FieldKind.ARRAY or len(spec.path) > len(path)
                             for spec in relevant)
            if not structured or isinstance(schema, bool):
                predicate = self.finite(uri, path)
            else:
                unknown = set(schema) - self._STRUCTURAL - self._ANNOTATIONS
                if unknown:
                    raise StreamValidationError(f'未支持的跨延迟关键字 {sorted(unknown)}: {uri}')
                predicate = self._structured(uri, path, schema)
            self.predicates[key] = predicate
            return predicate
        finally:
            self.visiting.remove(key)

    def _structured(self, uri, path, schema):
        checks = []
        for keyword, rule in schema.items():
            location = _child(uri, keyword)
            if keyword in self._ANNOTATIONS or keyword in ('then', 'else'):
                continue
            self.parent_predicates.append(ParentPredicate(location, path, keyword))
            if keyword == '$ref':
                checks.append(self.compile(_reference(uri, rule, self.registry), path).check)
            elif keyword == 'type':
                validator = create_validator({'$schema': _DRAFT, 'type': rule}, registry=self.registry)
                types = (rule,) if isinstance(rule, str) else tuple(rule)
                checks.append(lambda value, v=validator, types=types:
                    'array' in types if isinstance(value, _Array) else any(v.is_type(value, kind) for kind in types))
            elif keyword == 'properties':
                members = [(name, self.compile(_child(location, name), path + (name,)))
                           for name in rule]
                checks.append(lambda value, members=members: not isinstance(value, dict) or all(
                    name not in value or pred.check(value[name]) for name, pred in members))
            elif keyword == 'required':
                checks.append(lambda value, required=tuple(rule): not isinstance(value, dict) or all(
                    name in value for name in required))
            elif keyword == 'dependentRequired':
                pairs = tuple((name, tuple(required)) for name, required in rule.items())
                checks.append(lambda value, pairs=pairs: not isinstance(value, dict) or all(
                    trigger not in value or all(name in value for name in required)
                    for trigger, required in pairs))
            elif keyword == 'additionalProperties':
                allowed = frozenset(schema.get('properties', {}))
                extra = self.compile(location, path + ('<additional>',))
                checks.append(lambda value, allowed=allowed, extra=extra: not isinstance(value, dict) or all(
                    name in allowed or extra.check(member) for name, member in value.items()))
            elif keyword in ('minProperties', 'maxProperties'):
                checks.append(lambda value, limit=rule, minimum=keyword == 'minProperties':
                    not isinstance(value, dict) or (len(value) >= limit if minimum else len(value) <= limit))
            elif keyword in ('allOf', 'anyOf'):
                parts = tuple(self.compile(_child(location, index), path) for index in range(len(rule)))
                checks.append(lambda value, parts=parts, all_parts=keyword == 'allOf':
                    (all if all_parts else any)(part.check(value) for part in parts))
            elif keyword == 'not':
                part = self.compile(location, path)
                checks.append(lambda value, part=part: not part.check(value))
            elif keyword == 'if':
                condition = self.compile(location, path)
                yes = self.compile(_child(uri, 'then'), path) if 'then' in schema else None
                no = self.compile(_child(uri, 'else'), path) if 'else' in schema else None
                def conditional(value, condition=condition, yes=yes, no=no):
                    selected = yes if condition.check(value) else no
                    return selected is None or selected.check(value)
                checks.append(conditional)
            elif keyword in ('minItems', 'maxItems'):
                checks.append(lambda value, limit=rule, minimum=keyword == 'minItems':
                    not isinstance(value, _Array) or (value.count >= limit if minimum else value.count <= limit))
            elif keyword == 'items':
                spec = self.specs.get(path)
                if spec is None or spec.kind is not FieldKind.ARRAY:
                    raise StreamValidationError(f'延迟数组缺少投影描述: {uri}')
                item_projection = spec.item or ProjectionDescriptor(self.projection.pointer + '/items')
                plan = derive_stream_plan(location, item_projection, registry=self.registry)
                self.items.setdefault(path, {})[location] = plan
                checks.append(lambda value, location=location: not isinstance(value, _Array) or
                              value.item_results.get(location, True))
            elif keyword == 'uniqueItems':
                if rule and (path not in self.specs or
                             (self.specs[path].identity is None and path not in self.unique_covered)):
                    raise StreamValidationError(f'uniqueItems 缺少有界唯一身份: {uri}')
                # 严格稳定身份逐项核对，或由已覆盖该字段的真实完整凭据证明。
        return _Predicate(uri, lambda value: all(check(value) for check in checks))


@dataclass(frozen=True)
class NodeValidationPlan:
    schema_uri: str
    projection: ProjectionDescriptor
    _compiler: _Compiler = field(repr=False, compare=False)
    _predicate: _Predicate = field(repr=False, compare=False)

    @property
    def finite_checks(self):
        return tuple(self._compiler.finite_checks)

    @property
    def parent_predicates(self):
        return tuple(self._compiler.parent_predicates)

    @property
    def field_obligations(self):
        return tuple(FieldObligation(
            spec.path,
            tuple(uri for uri, path in self._compiler.predicates if path == spec.path),
            tuple((rule.schema_uri, rule.keyword) for rule in self.parent_predicates
                  if rule.value_path == spec.path and rule.keyword in ('minItems', 'maxItems', 'uniqueItems')),
            tuple(self._compiler.items.get(spec.path, {})),
        ) for spec in self.projection.fields)

    def begin(self, binding: NodeBinding, finite_fields: dict) -> NodeGate:
        if (binding.schema_uri != self.schema_uri or
                binding.projection_pointer != self.projection.pointer):
            raise StreamValidationError('对象与 Schema/投影绑定不符')
        return NodeGate(self, binding, finite_fields)


def derive_stream_plan(schema_uri: str, projection: ProjectionDescriptor, *, registry: Registry) -> NodeValidationPlan:
    """在写出前派生规则；调用方先从权威资源入口装载合法本地 Schema。

    不执行资源检索；registry 和投影由调用方显式提供。跨延迟语义
    不可解释时初始化拒绝，有限规则仍交现有精确 validator。
    """
    if not isinstance(projection, ProjectionDescriptor) or not projection.pointer:
        raise StreamValidationError('投影描述缺失')
    paths = [spec.path for spec in projection.fields]
    if (len(set(paths)) != len(paths) or any(not path or any(not isinstance(p, str) or not p for p in path) for path in paths)
            or any(a != b and b[:len(a)] == a for a in paths for b in paths)):
        raise StreamValidationError('延迟字段路径重复、重叠或无效')
    if any(not isinstance(spec.kind, FieldKind)
           or (spec.item is not None and spec.kind is not FieldKind.ARRAY)
           or (spec.kind is FieldKind.OBJECT) != (spec.node is not None)
           for spec in projection.fields):
        raise StreamValidationError('延迟字段种类或逐项投影无效')
    names = [requirement.name for requirement in projection.completions]
    if len(set(names)) != len(names) or any(not isinstance(name, str) or not name for name in names):
        raise StreamValidationError('完成义务身份重复或为空')
    arrays = {spec.path for spec in projection.fields if spec.kind is FieldKind.ARRAY}
    if any(set(requirement.unique_fields) - arrays for requirement in projection.completions):
        raise StreamValidationError('唯一关系义务必须指向本对象真实数组字段')
    compiler = _Compiler(registry, projection)
    predicate = compiler.compile(schema_uri)
    if set(paths) - compiler.reached:
        raise StreamValidationError(f'投影延迟路径不在 Schema 属性中: {set(paths)-compiler.reached}')
    return NodeValidationPlan(schema_uri, projection, compiler, predicate)


class NodeReceipt:
    """只能由成功关闭的门禁产生，并按准确绑定消费一次。"""
    def __init__(self, plan, binding, values, seal):
        if seal is not _SEAL:
            raise StreamValidationError('对象回执缺少成功关闭证明')
        self._plan, self._binding = plan, binding
        self._values, self._used = values, False

    @property
    def plan(self):
        return self._plan

    @property
    def binding(self):
        return self._binding

    def consume(self, expected: NodeBinding):
        if self._used or self.binding != expected:
            raise StreamValidationError('对象回执已经消费或绑定不符')
        self._used = True
        return self._values


class NodeGate:
    """一个报告对象的有限状态；数组只保留计数/末位置/逐规则结果。"""
    def __init__(self, plan, binding, finite_fields):
        if not isinstance(finite_fields, dict):
            raise StreamValidationError('对象有限字段必须是实际对象')
        self._plan, self._binding = plan, binding
        self._values = deepcopy(finite_fields)
        self._slots = {spec.path: _Slot(spec) for spec in plan.projection.fields}
        self._proofs: dict[str, CompletionProof] = {}
        self._closed, self._failure = False, None
        for path, slot in self._slots.items():
            if _at(self._values, path) is not MISSING:
                raise StreamValidationError('有限字段不得预载延迟值')
            if _at(self._values, path[:-1]) is MISSING:
                slot.state = FieldState.OMITTED

    @property
    def plan(self):
        return self._plan

    @property
    def binding(self):
        return self._binding

    def _active(self):
        if self._closed or self._failure is not None:
            raise StreamValidationError('对象已经关闭或失败')

    def _slot(self, path):
        self._active()
        try:
            return self._slots[path]
        except KeyError as error:
            raise StreamValidationError('未声明的延迟字段') from error

    def _set(self, path, value):
        parent = _at(self._values, path[:-1])
        if not isinstance(parent, dict):
            raise StreamValidationError('延迟字段父对象不存在')
        parent[path[-1]] = value

    def state(self, path):
        return self._slots[path].state

    def count(self, path):
        return self._slots[path].array.count

    def last_position(self, path):
        return self._slots[path].array.last_position

    def omit(self, path, *, authority_end=None):
        slot = self._slot(path)
        if slot.state is not FieldState.UNDECIDED:
            raise StreamValidationError('字段存在性已确定')
        if slot.spec.omit_empty:
            self._check_end(path, slot, authority_end)
        slot.state = FieldState.OMITTED

    def set_value(self, path, value):
        slot = self._slot(path)
        if slot.spec.kind is not FieldKind.VALUE or slot.state is not FieldState.UNDECIDED:
            raise StreamValidationError('有限延迟值状态不符')
        self._set(path, deepcopy(value))
        slot.state = FieldState.CLOSED

    def open_array(self, path):
        slot = self._slot(path)
        if slot.spec.kind is not FieldKind.ARRAY or slot.state is not FieldState.UNDECIDED:
            raise StreamValidationError('数组不是未决定状态')
        self._set(path, slot.array)
        slot.state = FieldState.OPEN

    def object_binding(self, path):
        slot = self._slot(path)
        if slot.spec.kind is not FieldKind.OBJECT:
            raise StreamValidationError('字段不是对象子节点')
        plan = slot.spec.node
        return NodeBinding(self.binding.instance_id, self.binding.owner_entity, self.binding.owner_id,
            self.binding.boundary, self.binding.occurrence + path,
            plan.schema_uri, plan.projection.pointer)

    def accept_object(self, path, receipt):
        slot = self._slot(path)
        if slot.spec.kind is not FieldKind.OBJECT or slot.state is not FieldState.UNDECIDED:
            raise StreamValidationError('对象字段种类或状态不符')
        if not isinstance(receipt, NodeReceipt):
            raise StreamValidationError('对象字段必须提供成功关闭回执')
        try:
            if receipt.plan is not slot.spec.node:
                raise StreamValidationError('对象子回执不是字段的已派生计划')
            value = receipt.consume(self.object_binding(path))
            self._set(path, value)
            slot.state = FieldState.CLOSED
        except BaseException as error:
            self.fail(error)
            raise

    def item_plan(self, path):
        plans = self.plan._compiler.items.get(path, {})
        if len(plans) != 1:
            raise StreamValidationError('逐项节点需要明确的唯一 Schema 投影')
        return next(iter(plans.values()))

    def child_binding(self, path, position):
        plan = self.item_plan(path)
        return NodeBinding(self.binding.instance_id, self.binding.owner_entity, self.binding.owner_id,
            self.binding.boundary, self.binding.occurrence + path + (position,),
            plan.schema_uri, plan.projection.pointer)

    def accept_item(self, path, value, *, position):
        slot = self._slot(path)
        if slot.state is not FieldState.OPEN:
            raise StreamValidationError('数组没有打开')
        try:
            if type(position) not in (str, int):
                raise StreamValidationError('逐项位置必须是有限稳定身份')
            plans = self.plan._compiler.items.get(path, {})
            if slot.spec.item is not None:
                if not isinstance(value, NodeReceipt):
                    raise StreamValidationError('嵌套延迟项必须提供真实对象回执')
                if value.plan is not self.item_plan(path):
                    raise StreamValidationError('逐项回执不是字段的完整派生计划')
                member = value.consume(self.child_binding(path, position))
            else:
                if isinstance(value, NodeReceipt):
                    raise StreamValidationError('未声明逐项延迟投影')
                member = value
            for uri, plan in plans.items():
                valid = plan._predicate.check(member)
                slot.array.item_results[uri] = slot.array.item_results.get(uri, True) and valid
            if slot.spec.identity is not None:
                identity = slot.spec.identity(member)
                if slot.array.last_identity is not MISSING and identity <= slot.array.last_identity:
                    raise StreamValidationError('数组唯一身份没有严格推进')
                slot.array.last_identity = identity
            slot.array.count += 1
            slot.array.last_position = deepcopy(position)
        except BaseException as error:
            self.fail(error)
            raise

    def _check_end(self, path, slot, authority_end):
        if (not isinstance(authority_end, ReadEnd) or authority_end.binding != self.binding
                or authority_end.path != path or type(authority_end.count) is not int
                or authority_end.count != slot.array.count
                or authority_end.last_position != slot.array.last_position):
            error = StreamValidationError('耗尽证据与真实字段/计数/末位置不符')
            self.fail(error)
            raise error

    def exhaust_array(self, path, authority_end):
        slot = self._slot(path)
        if slot.state is not FieldState.OPEN:
            raise StreamValidationError('未打开的数组不能耗尽关闭')
        self._check_end(path, slot, authority_end)
        slot.state = FieldState.CLOSED

    def supply_completion(self, name, proof):
        self._active()
        if (name not in {r.name for r in self.plan.projection.completions}
                or name in self._proofs or not isinstance(proof, CompletionProof)):
            raise StreamValidationError('完成凭据未声明、重复或不具备消费契约')
        self._proofs[name] = proof

    def fail(self, error: BaseException):
        if self._closed:
            raise StreamValidationError('成功关闭后不能改为失败')
        self._failure = error
        for slot in self._slots.values():
            if slot.state in (FieldState.OPEN, FieldState.UNDECIDED):
                slot.state = FieldState.FAILED

    def close_node(self):
        self._active()
        if any(slot.state not in (FieldState.CLOSED, FieldState.OMITTED) for slot in self._slots.values()):
            raise StreamValidationError('对象仍有未决定或未耗尽字段')
        if any(requirement.name not in self._proofs for requirement in self.plan.projection.completions):
            raise StreamValidationError('对象缺少真实业务完整回执')
        try:
            if not self.plan._predicate.check(self._values):
                raise StreamValidationError(f'对象不符合权威 Schema: {self.plan.schema_uri}')
            for requirement in self.plan.projection.completions:
                counts = tuple((path, slot.array.count) for path, slot in self._slots.items()
                    if slot.spec.kind is FieldKind.ARRAY and path[:len(requirement.path)] == requirement.path)
                returned = self._proofs[requirement.name].consume(CompletionRequest(self.binding, requirement, counts, self.plan))
                if returned is not None:
                    raise StreamValidationError('完成凭据必须消费准确请求，不返回布尔结论')
        except BaseException as error:
            self.fail(error)
            raise
        self._closed = True
        return NodeReceipt(self.plan, self.binding, self._values, _SEAL)
