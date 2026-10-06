"""固定 H 的报告入选选择与对象恢复请求类型。

选择按业务水位窗口与对象 ID 游标分页，父对象沿报告实体登记的
外键补齐；入选树与公开投影的逐层入选结构一致。恢复请求绑定
数据库身份、对象、H 与恢复范围，由仓储在同一短读事务内联合读
取当前投影与 C 后组合正逆恢复。

关联文件查询按固定 H 取设备文件或主机中间文件：引用类查询先
恢复引用方再按当时引用取文件；候选类查询绑定扫描固定上界，逐
候选恢复到 H 后筛选，继续位置越过已检查候选。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import BoundaryError, HistoryBoundary
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, ObjectId

__all__ = [
    "PlanSubtree",
    "ReportScope",
    "ReportScopeRequest",
    "build_report_scope",
    "report_target_types",
    "FileHistoryKind",
    "FileRecord",
    "FileHistoryCursor",
    "FileHistoryRequest",
    "FileHistoryPage",
    "candidate_scan_spec",
]


def candidate_scan_spec(kind: FileHistoryKind) -> tuple[str, str, str]:
    """候选类查询的 (文件表, 历史对象名, 固定归属列)。"""
    try:
        return _CANDIDATE_SCANS[kind]
    except KeyError:
        raise BoundaryError(f"查询种类 {kind.name} 不是候选扫描") from None


@dataclass(frozen=True)
class ReportScopeRequest:
    """一次报告入选选择的固定输入。

    boundary 是冻结的完整已提交边界 H；from_wm/to_wm 是覆盖水位
    窗口（from 开排除、to 含端值）；entity_batch_size 是每页入选
    对象上限。
    """

    boundary: HistoryBoundary
    from_wm: int
    to_wm: int
    entity_batch_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, HistoryBoundary):
            raise ConsistencyError("报告入选必须使用完整历史边界")
        for name in ("from_wm", "to_wm", "entity_batch_size"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConsistencyError(f"{name} 必须是非负整数: {value!r}")
        if self.entity_batch_size < 1:
            raise ConsistencyError(f"entity_batch_size 必须是正整数: {self.entity_batch_size}")
        if self.to_wm < self.from_wm:
            raise ConsistencyError("覆盖水位窗口的终点早于起点")


@dataclass(frozen=True)
class PlanSubtree:
    """一个入选计划及其逐层限定的入选树。"""

    plan_id: int
    #: {"action": {action_id: {"output": {id: {}}, "delivery": {id: {}}}}}
    selection: Mapping[str, Mapping[int, Mapping[str, Any]]]


@dataclass(frozen=True)
class ReportScope:
    """一次报告的全部入选根实体。"""

    plans: tuple[PlanSubtree, ...]
    diagnostics: tuple[int, ...]

    @property
    def plan_ids(self) -> tuple[int, ...]:
        return tuple(subtree.plan_id for subtree in self.plans)

    def _subtree(self, plan_id: int) -> PlanSubtree:
        for subtree in self.plans:
            if subtree.plan_id == plan_id:
                return subtree
        raise ConsistencyError(f"计划 {plan_id} 不在报告入选集合中")

    def action_ids_of(self, plan_id: int) -> tuple[int, ...]:
        actions = self._subtree(plan_id).selection.get("action", {})
        return tuple(sorted(actions))

    def output_ids_of(self, plan_id: int, action_id: int) -> tuple[int, ...]:
        actions = self._subtree(plan_id).selection.get("action", {})
        subtree = actions.get(action_id, {})
        return tuple(sorted(subtree.get("output", {})))


def report_target_types() -> dict[str, int]:
    """报告目标对象类型及编号（单一权威登记）。"""
    return {
        name: spec["id"] for name, spec in load_enum_registry()["history_objects"].items()
        if spec.get("report_target")
    }


def build_report_scope(
    *,
    plans: Sequence[int],
    actions: Sequence[int],
    outputs: Sequence[int],
    deliveries: Sequence[int],
    diagnostics: Sequence[int],
    action_plan: Mapping[int, int],
    output_action: Mapping[int, int],
    delivery_action: Mapping[int, int],
) -> ReportScope:
    """按入选集合与归属外键构建报告范围；父对象沿外键补齐。

    外键映射须覆盖全部入选子实体的实际归属；缺失归属按状态库一
    致性错误处理，不猜默认父对象。
    """
    def owner(mapping: Mapping[int, int], identity: int, kind: str) -> int:
        parent = mapping.get(identity)
        if parent is None:
            raise ConsistencyError(f"{kind} {identity} 缺少归属父对象事实")
        ObjectId(parent)
        return parent

    # 计划为根：直接入选或由入选后代补齐。
    root_plans: dict[int, dict[int, dict[str, Any]]] = {
        plan_id: {} for plan_id in sorted(plans)}
    included_actions: dict[int, set[int]] = {
        plan_id: set() for plan_id in root_plans}

    def plan_of_action(action_id: int) -> int:
        plan_id = owner(action_plan, action_id, "动作")
        if plan_id not in root_plans:
            root_plans[plan_id] = {}
            included_actions[plan_id] = set()
        return plan_id

    action_children: dict[int, dict[str, dict[int, Any]]] = {
        action_id: {} for action_id in actions}
    for action_id in actions:
        plan_of_action(action_id)
    for output_id in sorted(outputs):
        action_id = owner(output_action, output_id, "产物")
        action_children.setdefault(action_id, {}).setdefault(
            "output", {})[output_id] = {}
        plan_of_action(action_id)
    for delivery_id in sorted(deliveries):
        action_id = owner(delivery_action, delivery_id, "交付")
        action_children.setdefault(action_id, {}).setdefault(
            "delivery", {})[delivery_id] = {}
        plan_of_action(action_id)

    for plan_id in root_plans:
        plan_actions = root_plans[plan_id]
        for action_id in sorted(
            action_id for action_id in action_children
            if action_plan[action_id] == plan_id
        ):
            plan_actions[action_id] = action_children[action_id]
            included_actions[plan_id].add(action_id)

    subtrees = tuple(
        PlanSubtree(
            plan_id=plan_id,
            selection={
                "action": {
                    action_id: action_children[action_id]
                    for action_id in sorted(included)
                }
            },
        )
        for plan_id, included in sorted(root_plans.items())
    )
    seen_plans = [subtree.plan_id for subtree in subtrees]
    if len(seen_plans) != len(set(seen_plans)):
        raise ConsistencyError("报告范围出现重复计划")
    for plan_id in seen_plans:
        ObjectId(plan_id)
    for diagnostic_id in diagnostics:
        ObjectId(diagnostic_id)
    return ReportScope(plans=subtrees, diagnostics=tuple(sorted(diagnostics)))

# ---- 关联文件的同边界历史查询 ----


class FileHistoryKind(Enum):
    """关联文件查询的登记种类及所属对象。

    前三种按引用方在 H 的实际引用取文件；后四种按固定归属列扫
    描候选并逐个恢复到 H 后筛选。
    """

    OUTPUT_FILE = "output_file"
    COPY_FILES = "copy_files"
    PROCESSING_FILES = "processing_files"
    OBSERVED_DEVICE_FILES = "observed_device_files"
    CONFIRMED_SOURCE_FILES = "confirmed_source_files"
    ACTION_INTERMEDIATE_FILES = "action_intermediate_files"
    DELIVERY_INTERMEDIATE_FILES = "delivery_intermediate_files"


#: 各种类必需的所属对象字段；恰好一个字段适用。
_KIND_OWNER_FIELDS: dict[FileHistoryKind, tuple[str, ...]] = {
    FileHistoryKind.OUTPUT_FILE: ("output_id",),
    FileHistoryKind.COPY_FILES: ("copy_id",),
    FileHistoryKind.PROCESSING_FILES: ("action_id",),
    FileHistoryKind.OBSERVED_DEVICE_FILES: ("action_id",),
    FileHistoryKind.CONFIRMED_SOURCE_FILES: ("action_id",),
    FileHistoryKind.ACTION_INTERMEDIATE_FILES: ("action_id",),
    FileHistoryKind.DELIVERY_INTERMEDIATE_FILES: ("delivery_id",),
}

#: 候选类查询的 (文件表, 历史对象名, 固定归属列)。
_CANDIDATE_SCANS: dict[FileHistoryKind, tuple[str, str, str]] = {
    FileHistoryKind.OBSERVED_DEVICE_FILES: (
        "device_files", "device_file", "observer_action_id"),
    FileHistoryKind.CONFIRMED_SOURCE_FILES: (
        "device_files", "device_file", "source_action_id"),
    FileHistoryKind.ACTION_INTERMEDIATE_FILES: (
        "intermediate_files", "intermediate_file", "owner_action_id"),
    FileHistoryKind.DELIVERY_INTERMEDIATE_FILES: (
        "intermediate_files", "intermediate_file", "owner_delivery_id"),
}


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoundaryError(f"{name} 必须是整数: {value!r}")
    ObjectId(value)
    return value


@dataclass(frozen=True)
class FileRecord:
    """一份恢复到 H 的独立文件记录；身份始终带类型。"""

    file_type: str
    file_id: int
    row: Mapping[str, Any]


@dataclass(frozen=True)
class FileHistoryCursor:
    """候选扫描的继续位置：绑定种类、H、所属对象与扫描上界。"""

    kind: FileHistoryKind
    boundary: HistoryBoundary
    owner_id: int
    upper_id: int
    after_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FileHistoryKind):
            raise BoundaryError(f"继续位置种类未登记: {self.kind!r}")
        if not isinstance(self.boundary, HistoryBoundary):
            raise BoundaryError("继续位置必须绑定完整历史边界")
        _positive(self.owner_id, "继续位置的所属对象")
        for name in ("upper_id", "after_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BoundaryError(f"{name} 必须是非负整数: {value!r}")
        if self.after_id > self.upper_id:
            raise BoundaryError("继续位置越过了扫描固定上界")


@dataclass(frozen=True)
class FileHistoryRequest:
    """一次关联文件查询的固定输入。

    boundary 是完整已提交边界 H；所属对象字段按种类恰好提供一个；
    cursor 为空表示首次查询，非空时必须与本请求的种类、H 和所属
    对象一致。
    """

    boundary: HistoryBoundary
    kind: FileHistoryKind
    action_id: int | None = None
    delivery_id: int | None = None
    output_id: int | None = None
    copy_id: int | None = None
    batch_size: int = 32
    cursor: FileHistoryCursor | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, HistoryBoundary):
            raise BoundaryError("关联文件查询必须使用完整历史边界")
        if not isinstance(self.kind, FileHistoryKind):
            raise BoundaryError(f"关联文件查询种类未登记: {self.kind!r}")
        if (isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int)
                or self.batch_size < 1):
            raise BoundaryError(f"batch_size 必须是正整数: {self.batch_size!r}")
        owners = _KIND_OWNER_FIELDS[self.kind]
        present = [name for name in owners if getattr(self, name) is not None]
        if present != list(owners):
            raise BoundaryError(
                f"查询种类 {self.kind.name} 需要且只需要所属对象 {list(owners)}")
        for name in ("action_id", "delivery_id", "output_id", "copy_id"):
            if getattr(self, name) is not None:
                _positive(getattr(self, name), name)
        if self.cursor is not None and not isinstance(self.cursor, FileHistoryCursor):
            raise BoundaryError("继续位置类型无效")

    @property
    def owner_id(self) -> int:
        """本次查询绑定的所属对象身份。"""
        name = _KIND_OWNER_FIELDS[self.kind][0]
        return getattr(self, name)

    def check_cursor(self) -> None:
        """核对继续位置与本次查询身份一致，不在不同集合间复用。"""
        if self.cursor is None:
            return
        cursor = self.cursor
        if (cursor.kind is not self.kind or cursor.boundary != self.boundary
                or cursor.owner_id != self.owner_id):
            raise BoundaryError("继续位置与查询种类、H 或所属对象不一致")


@dataclass(frozen=True)
class FileHistoryPage:
    """一次关联文件查询的结果。

    owner_present 为假表示所需主对象在 H 不存在，与存在但集合为
    空分别表达；page 遵守分页结果契约，next_cursor 为空即读完。
    """

    owner_present: bool
    page: Page[FileRecord, FileHistoryCursor]
