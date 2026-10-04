"""源产物清理的目标固定输入与结果类型。

精确清理按原请求逐项固定；范围清理等待全部来源固定并完成后固
定。缺失目标按 output_not_found 直接终态，全部不可解析时同事务
以 cleanup_items_failed 结束动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from camctl.contracts.values import ObjectId, UtcMicros

__all__ = [
    "CleanupTargetsDisposition",
    "CleanupTargetsSaved",
    "FixCleanupTargets",
]


@dataclass(frozen=True)
class FixCleanupTargets:
    """一次清理目标固定的申请输入。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupTargetsDisposition(Enum):
    """目标固定事务的结果分类。"""

    SAVED = "saved"
    #: 已固定集合或原键重送：只读复用首次结果。
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupTargetsSaved:
    """目标固定事务的保存结果；item_ids 按创建顺序排列。"""

    disposition: CleanupTargetsDisposition
    item_ids: tuple[int, ...]
