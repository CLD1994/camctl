"""取消目标的完整寻址、自身包含检查与固定集合规则。

寻址先取得完整直接目标，自动预览候选由已保存关联提供；去重后的
完整集合在施加任何取消前检查是否包含本次取消动作自身，包含时整
个取消以 cancel_self_target 失败且不产生任何目标效果。检查通过后
按联动条件固定实际处理集合：拒绝取消的拍摄不触发联动，但其直接
目标保留在集合中。
"""

from __future__ import annotations

from typing import Mapping, Protocol

from camctl.cancellation.models import (
    CancelTarget,
    CancelTargetError,
    CancellationEffect,
    FixedCancelSet,
    FixedTarget,
    ResolvedTargets,
    SelectionBasis,
    TargetResolution,
)
from camctl.contracts.values import ObjectId

__all__ = [
    "CancelLookup",
    "CancelLookupError",
    "Lookup",
    "missing_target_error",
    "prepare_cancel_set",
    "resolve_cancel_target",
]


class CancelLookupError(Exception):
    """目标查询无法可靠完成；不得解释为目标不存在。"""


class CancelLookup(Protocol):
    """执行期目标查询端口；实现只读已保存事实。"""

    def plan_by_request(self, request_id: str) -> tuple[int, ...] | None: ...

    def plan_actions(self, plan_id: int) -> tuple[int, ...] | None: ...

    def group_actions(self, plan_id: int, group: str) -> tuple[int, ...] | None: ...

    def action_exists(self, action_id: int) -> bool: ...


class Lookup:
    """测试替身：按预置集合回答寻址，可注入统一查询失败。"""

    def __init__(self, actions: Mapping[int, bool] | None = None,
                 plans: Mapping[int, tuple[int, ...]] | None = None,
                 groups: Mapping[tuple[int, str], tuple[int, ...]] | None = None,
                 requests: Mapping[str, tuple[int, ...]] | None = None,
                 failure: CancelLookupError | None = None) -> None:
        self._actions = dict(actions or {})
        self._plans = dict(plans or {})
        self._groups = dict(groups or {})
        self._requests = dict(requests or {})
        self._failure = failure

    def _check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def plan_by_request(self, request_id: str) -> tuple[int, ...] | None:
        self._check()
        return self._requests.get(request_id)

    def plan_actions(self, plan_id: int) -> tuple[int, ...] | None:
        self._check()
        return self._plans.get(plan_id)

    def group_actions(self, plan_id: int, group: str) -> tuple[int, ...] | None:
        self._check()
        return self._groups.get((plan_id, group))

    def action_exists(self, action_id: int) -> bool:
        self._check()
        return bool(self._actions.get(action_id))


def resolve_cancel_target(
        target: CancelTarget, facts: CancelLookup) -> TargetResolution:
    """按寻址入口取得完整直接目标动作集合。

    可靠不存在与查询错误分别表达：查询失败不折叠为不存在，调用方
    按持久化及恢复错误处理，不猜测集合有效。
    """
    try:
        if target.action_instance_id is not None:
            ObjectId(target.action_instance_id)
            if not facts.action_exists(target.action_instance_id):
                return TargetResolution(missing=True)
            return TargetResolution(action_ids=(target.action_instance_id,))
        if target.request_id is not None:
            members = facts.plan_by_request(target.request_id)
        elif target.group is not None:
            members = facts.group_actions(target.plan_instance_id, target.group)
        else:
            members = facts.plan_actions(target.plan_instance_id)
    except CancelLookupError as error:
        return TargetResolution(lookup_failed=str(error))
    if members is None:
        return TargetResolution(missing=True)
    return TargetResolution(action_ids=tuple(sorted(members)))


def missing_target_error(target: CancelTarget) -> CancelTargetError:
    """可靠不存在目标时的登记错误；详情保留原请求目标对象。"""
    return CancelTargetError(
        "cancel_target_not_found", {"target": target.as_fields()})


def prepare_cancel_set(
        origin: int, resolved: ResolvedTargets) -> FixedCancelSet | CancelTargetError:
    """自身包含检查后固定实际处理集合。

    检查使用完整集合（直接目标加全部自动关联候选），不受联动条件
    或目标当前可取消性提前缩小；包含自身时整个取消失败，不固定任
    何目标效果。通过后直接目标全部保留，拒绝取消的拍摄不触发联动。
    """
    ObjectId(origin)
    direct = {facts.action_id: facts for facts in resolved.direct}
    if len(direct) != len(resolved.direct):
        raise ValueError("直接目标集合不能重复")
    candidates = {
        obtain for _, obtain in resolved.auto_candidates}
    if origin in direct or origin in candidates:
        return CancelTargetError(
            "cancel_self_target", {"action_instance_id": str(origin)})
    targets: dict[int, FixedTarget] = {}
    for action_id, facts in direct.items():
        targets[action_id] = FixedTarget(
            action_id=action_id,
            basis=SelectionBasis.DIRECT,
            cancellation_effect=(
                CancellationEffect.NOT_REQUIRED if facts.terminal
                else CancellationEffect.NOT_APPLIED))
    for source_id, obtain_id in resolved.auto_candidates:
        source = direct.get(source_id)
        if source is None:
            continue
        if not (source.terminal or source.may_cancel):
            # 拍摄拒绝取消时不触发联动；直接目标本身仍保留在集合中。
            continue
        existing = targets.get(obtain_id)
        if existing is None:
            targets[obtain_id] = FixedTarget(
                action_id=obtain_id,
                basis=SelectionBasis.AUTO_PREVIEW,
                cancellation_effect=CancellationEffect.NOT_APPLIED)
        elif existing.basis is SelectionBasis.DIRECT:
            targets[obtain_id] = FixedTarget(
                action_id=obtain_id,
                basis=SelectionBasis.BOTH,
                cancellation_effect=existing.cancellation_effect)
    return FixedCancelSet(targets=tuple(
        targets[action_id] for action_id in sorted(targets)))
