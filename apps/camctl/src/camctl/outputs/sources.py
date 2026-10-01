"""来源固定与逐来源产物选择的纯计算。

六种来源引用形式解析为具体拍摄来源成员：动作与组引用要求真实
存在且类型可产生产物，计划范围允许合法空集合。选择在来源终态
且适用产物处理后一次固定：默认方式以修复成品替代原片且不含预
览，所选不可用不回退也不扩大范围；预览方式按有效预览与修复成
品完整大小比较选择；精确 ID 逐项判定不存在、归属不匹配与已清
理。此前可靠确认存在的产物记录缺失是状态库矛盾。本模块只计算
选择结果，由仓储在同一事务保存并保持不可重选。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Mapping, Protocol

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.catalog import OutputKind
from camctl.contracts.workflow_errors import action_error_id, item_error_id

__all__ = [
    "ActionFacts",
    "CatalogEntry",
    "ItemStatus",
    "ResolveFailure",
    "ResolutionState",
    "SelectedItem",
    "SelectionFacts",
    "SelectionMode",
    "SelectionSnapshot",
    "SourceLookup",
    "SourceResolution",
    "SourceSpec",
    "resolve_source",
    "select_outputs",
]

#: 三种拍摄动作类型（成员名来自登记，第一版全部可产生正式产物）。
_CAPTURE_TYPES = frozenset(
    int(member.value)
    for member in enum_for("actions.type")
    if member.name in {"CAMERA_TAKE_PHOTO", "CAMERA_RECORD", "CAMERA_TIMELAPSE"}
)

#: 取回明细错误编号（公共登记为唯一权威来源）。
_ITEM_OUTPUT_NOT_FOUND = item_error_id("obtain_items", "output_not_found")
_ITEM_OUTPUT_SOURCE_MISMATCH = item_error_id("obtain_items", "output_source_mismatch")
_ITEM_OUTPUT_UNAVAILABLE = item_error_id("obtain_items", "output_unavailable")
_ITEM_OUTPUT_CLEANUP_STARTED = item_error_id("obtain_items", "output_cleanup_started")
_ITEM_SOURCE_FILE_UNCONFIRMED = item_error_id("obtain_items", "source_file_unconfirmed")
_ITEM_PREVIEW_MISSING = item_error_id("obtain_items", "preview_missing")

#: 来源级选择错误编号。
SELECTION_NO_OUTPUTS = item_error_id("obtain_source_selections", "no_outputs")
_SELECTION_PREVIEW_MISSING = item_error_id(
    "obtain_source_selections", "preview_missing"
)

#: 来源解析失败的动作错误编号。
_ACTION_SOURCE_RESOLUTION_FAILED = action_error_id("source_resolution_failed")

#: 可用性到 output_unavailable 公共详情取值的映射。
_UNAVAILABLE_TEXT = {3: "cleaned", 4: "missing"}


class SelectionMode(IntEnum):
    """执行定义保存的取回选择方式。"""

    DEFAULT = 1
    PREVIEW = 2
    EXPLICIT_IDS = 3


class ResolveFailure(Enum):
    """来源解析失败的公共原因（写入动作错误详情的 reason 字段）。"""

    ACTION_NOT_FOUND = "action_not_found"
    PLAN_NOT_FOUND = "plan_not_found"
    GROUP_NOT_FOUND = "group_not_found"
    NOT_OUTPUT_SOURCE = "not_output_source"


#: 来源解析状态复用 actions.source_resolution_state 登记枚举。
ResolutionState = enum_for("actions.source_resolution_state")

#: 选择与逐项状态复用明细登记枚举。
ItemStatus = enum_for("obtain_items.status")

ItemBasis = enum_for("obtain_items.basis")

Availability = enum_for("outputs.availability")


@dataclass(frozen=True)
class SourceSpec:
    """六种来源引用形式之一；恰好一种字段组合合法。

    本计划内引用（动作名、组、当前计划）在受理时解析；实例与跨
    计划引用在取得执行资格后一次固定。字段组合混用即非法。
    """

    action_instance_id: int | None = None
    plan_instance_id: int | None = None
    group: str | None = None
    action_name: str | None = None
    current_plan: bool = False

    def __post_init__(self) -> None:
        has_action_id = self.action_instance_id is not None
        has_plan = self.plan_instance_id is not None
        has_group = self.group is not None
        has_name = self.action_name is not None
        forms = (
            has_action_id,
            has_plan and has_group,
            has_name,
            has_group and not has_plan,
            self.current_plan,
            has_plan and not has_group,
        )
        if sum(1 for flag in forms if flag) != 1:
            raise ValueError(
                "来源引用必须恰好采用六种字段组合之一: "
                f"action_instance_id={self.action_instance_id!r}, "
                f"plan_instance_id={self.plan_instance_id!r}, group={self.group!r}, "
                f"action_name={self.action_name!r}, current_plan={self.current_plan!r}"
            )
        for value in (self.action_instance_id, self.plan_instance_id):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"来源实例引用必须是正整数: {value!r}")
        for text in (self.group, self.action_name):
            if text is not None and not text:
                raise ValueError("来源名称引用不能为空")


@dataclass(frozen=True)
class ActionFacts:
    """来源解析所需的一个动作事实。"""

    action_id: int
    plan_id: int
    action_type: int
    name: str
    group_name: str | None


class SourceLookup(Protocol):
    """来源解析的只读查询端口。"""

    def owner_plan_id(self) -> int:
        """发起取回所属计划的 ID。"""
        ...

    def plan_exists(self, plan_id: int) -> bool:
        """计划实例是否存在。"""
        ...

    def action_by_id(self, action_id: int) -> ActionFacts | None:
        """按稳定实例 ID 取一个动作；不存在时为空。"""
        ...

    def plan_actions(self, plan_id: int) -> tuple[ActionFacts, ...]:
        """一个计划内的全部动作，按 ID 升序。"""
        ...


@dataclass(frozen=True)
class SourceResolution:
    """一次来源解析的结果。

    FIXED 携带完整成员集合（可为合法空集合）与真实来源计划；
    FAILED 携带公共失败原因。查询不可靠由调用方按状态库错误处
    理，不用 FAILED 表达。
    """

    state: ResolutionState
    member_action_ids: tuple[int, ...] = ()
    source_plan_id: int | None = None
    failure: ResolveFailure | None = None

    def __post_init__(self) -> None:
        if self.state == ResolutionState.FIXED:
            if self.failure is not None:
                raise ValueError("FIXED 来源不携带失败原因")
            if self.source_plan_id is None:
                raise ValueError("FIXED 来源必须保存真实来源计划")
        elif self.state == ResolutionState.FAILED:
            if self.failure is None or self.member_action_ids or self.source_plan_id:
                raise ValueError("FAILED 来源只携带失败原因")

    def action_error(self, spec: SourceSpec) -> tuple[int, dict[str, Any]]:
        """解析失败对应的动作错误编号与公共详情。"""
        if self.state != ResolutionState.FAILED or self.failure is None:
            raise ValueError("只有 FAILED 来源生成动作错误")
        return _ACTION_SOURCE_RESOLUTION_FAILED, {
            "reason": self.failure.value,
            "source": _spec_details(spec),
        }


def _spec_details(spec: SourceSpec) -> dict[str, Any]:
    """按公共详情模式表达原来源引用。"""
    details: dict[str, Any] = {}
    if spec.action_instance_id is not None:
        details["action_instance_id"] = spec.action_instance_id
    if spec.plan_instance_id is not None:
        details["plan_instance_id"] = spec.plan_instance_id
    if spec.group is not None:
        details["group"] = spec.group
    if spec.action_name is not None:
        details["action_name"] = spec.action_name
    if spec.current_plan:
        details["current_plan"] = True
    return details


def _capture_members(actions: tuple[ActionFacts, ...]) -> tuple[ActionFacts, ...]:
    return tuple(
        fact for fact in actions if int(fact.action_type) in _CAPTURE_TYPES
    )


def resolve_source(spec: SourceSpec, lookup: SourceLookup) -> SourceResolution:
    """把一种来源引用解析为具体拍摄来源成员。

    动作与组引用要求真实存在且成员类型可产生产物，目标计划不存
    在按解析失败处理；计划范围引用允许没有任何拍摄成员的合法空
    集合，仍保存真实来源计划。
    """
    if spec.action_instance_id is not None:
        fact = lookup.action_by_id(spec.action_instance_id)
        if fact is None:
            return _failed(ResolveFailure.ACTION_NOT_FOUND)
        if int(fact.action_type) not in _CAPTURE_TYPES:
            return _failed(ResolveFailure.NOT_OUTPUT_SOURCE)
        return SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(fact.action_id,),
            source_plan_id=fact.plan_id,
        )
    if spec.action_name is not None:
        owner_plan = lookup.owner_plan_id()
        candidates = [
            fact
            for fact in lookup.plan_actions(owner_plan)
            if fact.name == spec.action_name
        ]
        if not candidates:
            return _failed(ResolveFailure.ACTION_NOT_FOUND)
        (fact,) = candidates
        if int(fact.action_type) not in _CAPTURE_TYPES:
            return _failed(ResolveFailure.NOT_OUTPUT_SOURCE)
        return SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=(fact.action_id,),
            source_plan_id=owner_plan,
        )
    if spec.current_plan:
        owner_plan = lookup.owner_plan_id()
        members = _capture_members(lookup.plan_actions(owner_plan))
        return SourceResolution(
            state=ResolutionState.FIXED,
            member_action_ids=tuple(fact.action_id for fact in members),
            source_plan_id=owner_plan,
        )
    # 组引用与计划引用：指定计划实例必须真实存在；本计划内组在
    # 所属计划内定位成员。
    if spec.group is not None:
        if spec.plan_instance_id is not None:
            plan_id = spec.plan_instance_id
            if not lookup.plan_exists(plan_id):
                return _failed(ResolveFailure.PLAN_NOT_FOUND)
        else:
            plan_id = lookup.owner_plan_id()
        plan_actions = lookup.plan_actions(plan_id)
        group_members = tuple(
            fact for fact in plan_actions if fact.group_name == spec.group
        )
        if not group_members:
            return _failed(ResolveFailure.GROUP_NOT_FOUND)
        members = _capture_members(group_members)
        if not members:
            return _failed(ResolveFailure.NOT_OUTPUT_SOURCE)
    else:
        plan_id = spec.plan_instance_id
        assert plan_id is not None, "计划引用必须携带计划实例 ID"
        if not lookup.plan_exists(plan_id):
            return _failed(ResolveFailure.PLAN_NOT_FOUND)
        members = _capture_members(lookup.plan_actions(plan_id))
    return SourceResolution(
        state=ResolutionState.FIXED,
        member_action_ids=tuple(fact.action_id for fact in members),
        source_plan_id=plan_id,
    )


def _failed(reason: ResolveFailure) -> SourceResolution:
    return SourceResolution(state=ResolutionState.FAILED, failure=reason)


@dataclass(frozen=True)
class CatalogEntry:
    """来源目录中一份已登记产物的事实。

    original_output_id 来自产物来源关联（修复与预览指向原片）；
    size_bytes 是承载文件的完整长度，未知为空。
    """

    output_id: int
    kind: OutputKind
    availability: int
    original_output_id: int | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class SelectionFacts:
    """一个固定来源的选择事实。

    outputs 是该来源动作的全部正式产物；known_output_ids 覆盖整
    个产物目录，用于区分“不存在”与“归属其他来源”；
    previously_confirmed_ids 是此前已可靠确认存在的产物身份，其
    记录缺失属于状态库矛盾。source_completed 表示来源已终态且
    适用产物处理完成。
    """

    source_action_id: int
    source_completed: bool
    outputs: tuple[CatalogEntry, ...]
    known_output_ids: frozenset[int]
    previously_confirmed_ids: frozenset[int] = field(default=frozenset())


@dataclass(frozen=True)
class SelectedItem:
    """一个取回目标的选择结果。

    basis 与 status 使用明细登记枚举；error_details 采用公共登
    记模式。选择阶段不创建交付，delivery_created 恒为假。
    """

    basis: int
    status: int
    output_id: int | None = None
    requested_output_id: int | None = None
    original_output_id: int | None = None
    preview_output_id: int | None = None
    preview_size: int | None = None
    repaired_size: int | None = None
    error_code: int | None = None
    error_details: Mapping[str, Any] | None = None

    @property
    def basis_name(self) -> str:
        return ItemBasis(self.basis).name

    @property
    def status_name(self) -> str:
        return ItemStatus(self.status).name

    @property
    def delivery_created(self) -> bool:
        return False


@dataclass(frozen=True)
class SelectionSnapshot:
    """一个来源的完整选择结果。

    is_fixed 为假表示尚未选定（来源未完成或选择未保存）；合法空
    选择是已固定且没有条目。source_error_code 为来源级错误（没
    有产物、预览缺失），逐目标失败仍在 items 中逐项表达。
    """

    is_fixed: bool
    items: tuple[SelectedItem, ...] = ()
    source_error_code: int | None = None

    @property
    def selected_output_ids(self) -> tuple[int, ...]:
        return tuple(
            item.output_id
            for item in self.items
            if item.status == ItemStatus.SELECTED and item.output_id is not None
        )


def select_outputs(
    source: SourceResolution,
    facts: SelectionFacts,
    mode: SelectionMode,
    requested_output_ids: tuple[int, ...] = (),
) -> SelectionSnapshot:
    """按已保存的选择方式计算一个来源的完整选择。

    来源尚未完成时保持未选定；此前可靠确认存在的产物记录缺失按
    状态库矛盾拒绝。选择一次固定后不因新文件出现扩大范围，由保
    存层保持。
    """
    if source.state != ResolutionState.FIXED:
        raise ValueError("只有 FIXED 来源能进行产物选择")
    missing = facts.previously_confirmed_ids - facts.known_output_ids
    if missing:
        raise ConsistencyError(
            f"此前已可靠确认存在的产物记录缺失: {sorted(missing)}"
        )
    if not facts.source_completed:
        return SelectionSnapshot(is_fixed=False)
    if mode == SelectionMode.DEFAULT:
        return _select_default(facts)
    if mode == SelectionMode.PREVIEW:
        return _select_preview(facts)
    return _select_explicit(facts, requested_output_ids)


def _availability_failure(
    entry: CatalogEntry,
    basis: int,
    **references: Any,
) -> SelectedItem:
    """按可用性生成不可取回的逐项失败或未决。"""
    availability = Availability(entry.availability)
    references.setdefault("output_id", entry.output_id)
    if availability == Availability.RESTRICTED:
        return SelectedItem(
            basis=basis,
            status=ItemStatus.FAILED,
            error_code=_ITEM_OUTPUT_CLEANUP_STARTED,
            error_details={"output_id": entry.output_id},
            **references,
        )
    if availability in (Availability.CLEANED, Availability.MISSING):
        return SelectedItem(
            basis=basis,
            status=ItemStatus.FAILED,
            error_code=_ITEM_OUTPUT_UNAVAILABLE,
            error_details={
                "output_id": entry.output_id,
                "availability": _UNAVAILABLE_TEXT[availability.value],
            },
            **references,
        )
    # 状态库仍无法确认文件事实：不猜缺失也不当作可选。
    return SelectedItem(
        basis=basis,
        status=ItemStatus.FAILED,
        error_code=_ITEM_SOURCE_FILE_UNCONFIRMED,
        error_details={"output_id": entry.output_id},
        **references,
    )


def _unique_derived(
    outputs: tuple[CatalogEntry, ...],
    kind: OutputKind,
    original_id: int,
) -> CatalogEntry | None:
    """一个原片至多一份同种类派生产物；多份是状态矛盾。"""
    found = [
        entry
        for entry in outputs
        if entry.kind is kind and entry.original_output_id == original_id
    ]
    if len(found) > 1:
        raise ConsistencyError(
            f"原片 {original_id} 存在多份 {kind.value} 产物: "
            f"{[entry.output_id for entry in found]}"
        )
    return found[0] if found else None


def _select_default(facts: SelectionFacts) -> SelectionSnapshot:
    originals = sorted(
        (
            entry
            for entry in facts.outputs
            if entry.kind is OutputKind.ORIGINAL
        ),
        key=lambda entry: entry.output_id,
    )
    if not originals:
        return SelectionSnapshot(
            is_fixed=True, items=(), source_error_code=SELECTION_NO_OUTPUTS
        )
    items: list[SelectedItem] = []
    for original in originals:
        repaired = _unique_derived(facts.outputs, OutputKind.REPAIRED, original.output_id)
        chosen = repaired if repaired is not None else original
        if Availability(chosen.availability) is Availability.AVAILABLE:
            items.append(
                SelectedItem(
                    basis=ItemBasis.REPAIRED
                    if repaired is not None
                    else ItemBasis.ORIGINAL,
                    status=ItemStatus.SELECTED,
                    output_id=chosen.output_id,
                    original_output_id=original.output_id
                    if repaired is not None
                    else None,
                )
            )
        else:
            # 所选修复成品不可用不回退原片，失败项保留实际所选身份。
            items.append(
                _availability_failure(
                    chosen,
                    ItemBasis.REPAIRED
                    if repaired is not None
                    else ItemBasis.ORIGINAL,
                    original_output_id=original.output_id
                    if repaired is not None
                    else None,
                )
            )
    return SelectionSnapshot(is_fixed=True, items=tuple(items))


def _select_preview(facts: SelectionFacts) -> SelectionSnapshot:
    originals = sorted(
        (
            entry
            for entry in facts.outputs
            if entry.kind is OutputKind.ORIGINAL
        ),
        key=lambda entry: entry.output_id,
    )
    if not originals:
        return SelectionSnapshot(
            is_fixed=True, items=(), source_error_code=SELECTION_NO_OUTPUTS
        )
    items: list[SelectedItem] = []
    any_preview_target = False
    for original in originals:
        preview = _unique_derived(facts.outputs, OutputKind.PREVIEW, original.output_id)
        if preview is None:
            # 逐份保存预览缺失；不因有修复成品自动改传该成品。
            items.append(
                SelectedItem(
                    basis=ItemBasis.PREVIEW,
                    status=ItemStatus.FAILED,
                    original_output_id=original.output_id,
                    error_code=_ITEM_PREVIEW_MISSING,
                    error_details={
                        "source_action_instance_id": facts.source_action_id,
                        "original_output_id": original.output_id,
                    },
                )
            )
            continue
        any_preview_target = True
        repaired = _unique_derived(facts.outputs, OutputKind.REPAIRED, original.output_id)
        if (
            repaired is not None
            and preview.size_bytes is not None
            and repaired.size_bytes is not None
        ):
            if repaired.size_bytes <= preview.size_bytes:
                items.append(
                    _preview_family_item(
                        repaired,
                        ItemBasis.REPAIRED_NOT_LARGER,
                        original.output_id,
                        preview.output_id,
                        preview.size_bytes,
                        repaired.size_bytes,
                    )
                )
            else:
                items.append(
                    _preview_family_item(
                        preview,
                        ItemBasis.PREVIEW,
                        original.output_id,
                        preview.output_id,
                        preview.size_bytes,
                        repaired.size_bytes,
                    )
                )
            continue
        if repaired is not None and (
            preview.size_bytes is None or repaired.size_bytes is None
        ):
            # 完整大小无法确认：不猜测选择，也不冒充无产物。
            items.append(
                SelectedItem(
                    basis=ItemBasis.PREVIEW,
                    status=ItemStatus.FAILED,
                    output_id=preview.output_id,
                    preview_output_id=preview.output_id,
                    original_output_id=original.output_id,
                    error_code=_ITEM_SOURCE_FILE_UNCONFIRMED,
                    error_details={"output_id": preview.output_id},
                )
            )
            continue
        items.append(
            _preview_family_item(
                preview,
                ItemBasis.PREVIEW,
                original.output_id,
                preview.output_id,
                None,
                None,
            )
        )
    source_error = None if any_preview_target else _SELECTION_PREVIEW_MISSING
    return SelectionSnapshot(
        is_fixed=True, items=tuple(items), source_error_code=source_error
    )


def _preview_family_item(
    entry: CatalogEntry,
    basis: int,
    original_id: int,
    preview_id: int,
    preview_size: int | None,
    repaired_size: int | None,
) -> SelectedItem:
    if Availability(entry.availability) == Availability.AVAILABLE:
        return SelectedItem(
            basis=basis,
            status=ItemStatus.SELECTED,
            output_id=entry.output_id,
            original_output_id=original_id,
            preview_output_id=preview_id,
            preview_size=preview_size,
            repaired_size=repaired_size,
        )
    return _availability_failure(
        entry,
        basis,
        original_output_id=original_id,
        preview_output_id=preview_id,
        preview_size=preview_size,
        repaired_size=repaired_size,
    )


def _select_explicit(
    facts: SelectionFacts, requested_output_ids: tuple[int, ...]
) -> SelectionSnapshot:
    if not requested_output_ids:
        raise ValueError("精确 ID 取回必须提供非空请求列表")
    if len(set(requested_output_ids)) != len(requested_output_ids):
        raise ValueError(f"请求列表不得重复: {requested_output_ids}")
    by_id = {entry.output_id: entry for entry in facts.outputs}
    items: list[SelectedItem] = []
    for requested in requested_output_ids:
        if requested not in facts.known_output_ids:
            items.append(
                SelectedItem(
                    basis=ItemBasis.EXPLICIT,
                    status=ItemStatus.FAILED,
                    requested_output_id=requested,
                    error_code=_ITEM_OUTPUT_NOT_FOUND,
                    error_details={"requested_output_id": requested},
                )
            )
            continue
        entry = by_id.get(requested)
        if entry is None:
            items.append(
                SelectedItem(
                    basis=ItemBasis.EXPLICIT,
                    status=ItemStatus.FAILED,
                    requested_output_id=requested,
                    error_code=_ITEM_OUTPUT_SOURCE_MISMATCH,
                    error_details={"requested_output_id": requested},
                )
            )
            continue
        if Availability(entry.availability) == Availability.AVAILABLE:
            items.append(
                SelectedItem(
                    basis=ItemBasis.EXPLICIT,
                    status=ItemStatus.SELECTED,
                    output_id=entry.output_id,
                    requested_output_id=requested,
                )
            )
        elif Availability(entry.availability) == Availability.UNKNOWN:
            # 暂未完成不是不存在：保留请求值等待核实。
            items.append(
                SelectedItem(
                    basis=ItemBasis.EXPLICIT,
                    status=ItemStatus.UNRESOLVED,
                    requested_output_id=requested,
                )
            )
        else:
            items.append(
                _availability_failure(
                    entry,
                    ItemBasis.EXPLICIT,
                    requested_output_id=requested,
                )
            )
    return SelectionSnapshot(is_fixed=True, items=tuple(items))
