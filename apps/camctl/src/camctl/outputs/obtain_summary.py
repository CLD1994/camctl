"""取回统一发布汇总的纯判定。

预览选择与默认选择共用 sources.py 的同一路径（SelectionMode.
PREVIEW），自动与手动取回不另建选择规则。本模块汇总一个取回动
作全部固定来源与逐项准备结果，按开始发布的条件决定能否统一发
布：任一来源未判定、显式条目未判定或准备未定都继续等待，全部
确定后发布成功项并保留失败结果；没有成功文件时不创建交付。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from camctl.contracts.enums import enum_for

__all__ = [
    "ObtainDecision",
    "ObtainFacts",
    "ObtainItemStage",
    "ObtainPhase",
    "ObtainSourceFacts",
    "decide_obtain_finish",
    "obtain_item_stage",
]

_ITEM_STATUS = enum_for("obtain_items.status")
_DELIVERY_STATUS = enum_for("deliveries.status")

_ITEM_TERMINAL_FAILED = frozenset({
    _ITEM_STATUS.FAILED.value, _ITEM_STATUS.CANCELED.value})
_ITEM_ADDRESSED = frozenset({
    _ITEM_STATUS.SELECTED.value, _ITEM_STATUS.DELIVERY_CREATED.value})

_DELIVERY_STAGES = {
    _DELIVERY_STATUS.PENDING.value: None,  # 由条目阶段缺省表达
    _DELIVERY_STATUS.PREPARING.value: "processing",
    _DELIVERY_STATUS.PREPARED.value: "prepared",
    _DELIVERY_STATUS.PUBLISHING.value: "prepared",
    _DELIVERY_STATUS.PUBLISHED.value: "prepared",
    _DELIVERY_STATUS.FAILED.value: "failed",
    _DELIVERY_STATUS.CANCELED.value: "failed",
    _DELIVERY_STATUS.WITHDRAWN.value: "failed",
}


class ObtainItemStage(Enum):
    """单个所选产物的准备阶段汇总。

    PREPARED 含发布中与已发布（发布逐文件进行，不回退等待）；
    FAILED 是逐项最终失败，含取消与撤回的交付。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    PREPARED = "prepared"
    FAILED = "failed"


def obtain_item_stage(item_status: int,
                      delivery_status: int | None) -> ObtainItemStage:
    """把已判定条目及其交付状态映射为准备阶段。

    待判定条目不计入准备汇总（计入来源事实的待判定数）；未知状
    态按解释错误拒绝，不猜测阶段。
    """
    if isinstance(item_status, bool) or item_status not in _ITEM_ADDRESSED \
            and item_status not in _ITEM_TERMINAL_FAILED \
            and item_status != _ITEM_STATUS.UNRESOLVED.value:
        raise ValueError(f"条目状态不属于登记枚举: {item_status!r}")
    if item_status == _ITEM_STATUS.UNRESOLVED.value:
        raise ValueError("待判定条目不计入准备汇总")
    if item_status in _ITEM_TERMINAL_FAILED:
        return ObtainItemStage.FAILED
    if delivery_status is None:
        return ObtainItemStage.PENDING
    if isinstance(delivery_status, bool) \
            or delivery_status not in _DELIVERY_STAGES:
        raise ValueError(f"交付状态不属于登记枚举: {delivery_status!r}")
    if delivery_status == _DELIVERY_STATUS.PENDING.value:
        return ObtainItemStage.PENDING
    return ObtainItemStage(_DELIVERY_STAGES[delivery_status])


@dataclass(frozen=True)
class ObtainSourceFacts:
    """一个固定来源的汇总事实。

    selection_fixed 表达来源已结束且产物判定已固定；显式 ID 的待
    判定条目数独立计数，判定完成前不进入发布。
    """

    selection_fixed: bool
    unresolved_items: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.selection_fixed, bool):
            raise TypeError(f"选择固定性必须是布尔值: {self.selection_fixed!r}")
        if (isinstance(self.unresolved_items, bool)
                or not isinstance(self.unresolved_items, int)
                or self.unresolved_items < 0):
            raise ValueError(
                f"待判定条目数必须是非负整数: {self.unresolved_items!r}")


@dataclass(frozen=True)
class ObtainFacts:
    """一次取回的发布汇总事实；取回自身的取消与终态由调用方先行处理。"""

    sources: tuple[ObtainSourceFacts, ...]
    items: tuple[ObtainItemStage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple) or not all(
                isinstance(source, ObtainSourceFacts) for source in self.sources):
            raise TypeError("来源事实必须是 ObtainSourceFacts 元组")
        if not isinstance(self.items, tuple) or not all(
                isinstance(stage, ObtainItemStage) for stage in self.items):
            raise TypeError("条目阶段必须是 ObtainItemStage 元组")


class ObtainPhase(Enum):
    """统一发布汇总的阶段分区。"""

    WAIT_SOURCES = "wait_sources"
    WAIT_ITEMS = "wait_items"
    WAIT_PREPARATION = "wait_preparation"
    READY_TO_PUBLISH = "ready_to_publish"


@dataclass(frozen=True)
class ObtainDecision:
    """一次汇总判定：阶段与成功/失败项计数。"""

    phase: ObtainPhase
    prepared: int = 0
    failed: int = 0

    @property
    def may_publish(self) -> bool:
        return self.phase is ObtainPhase.READY_TO_PUBLISH


def decide_obtain_finish(facts: ObtainFacts) -> ObtainDecision:
    """按开始发布的条件决定统一发布阶段。

    来源等待优先于显式条目与准备等待；全部来源判定完成且全部条
    目取得最终准备结果后进入发布阶段，成功项与失败结果一并确
    定，整次有失败仍保留成功交付。
    """
    if not isinstance(facts, ObtainFacts):
        raise TypeError(f"汇总事实必须使用 ObtainFacts: {facts!r}")
    if any(not source.selection_fixed for source in facts.sources):
        return ObtainDecision(ObtainPhase.WAIT_SOURCES)
    if any(source.unresolved_items for source in facts.sources):
        return ObtainDecision(ObtainPhase.WAIT_ITEMS)
    prepared = sum(stage is ObtainItemStage.PREPARED for stage in facts.items)
    failed = sum(stage is ObtainItemStage.FAILED for stage in facts.items)
    if prepared + failed != len(facts.items):
        return ObtainDecision(ObtainPhase.WAIT_PREPARATION)
    return ObtainDecision(
        ObtainPhase.READY_TO_PUBLISH, prepared=prepared, failed=failed)
