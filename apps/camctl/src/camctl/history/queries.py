"""固定 H 的报告入选选择与对象恢复请求类型。

选择按业务水位窗口与对象 ID 游标分页，父对象沿报告实体登记的
外键补齐；入选树与公开投影的逐层入选结构一致。恢复请求绑定
数据库身份、对象、H 与恢复范围，由仓储在同一短读事务内联合读
取当前投影与 C 后组合正逆恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError, ObjectId

__all__ = [
    "PlanSubtree",
    "ReportScope",
    "ReportScopeRequest",
    "build_report_scope",
    "report_target_types",
]


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
