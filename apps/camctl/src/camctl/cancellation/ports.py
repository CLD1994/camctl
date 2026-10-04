"""取消目标收场端口：实际有限处理由目标所属模块提供。

端口只承担目标的必要收场（停止、读取结束、撤回或适用清理），不能
通过取消端口拥有的真实任务伪装完成；完成与失败分别表达，未完成表
示仍在有限处理中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["SettlementOutcome", "TargetSettlementPort"]


@dataclass(frozen=True)
class SettlementOutcome:
    """一次目标收场推进的结果。"""

    #: 本次有限处理是否已经结束（停止、撤回等完成或确认失败）。
    complete: bool
    #: 结束且失败（保存为逐项 target_cleanup_failed，不放弃其他项）。
    failed: bool = False


class TargetSettlementPort(Protocol):
    """目标模块提供的有限收场入口。"""

    async def settle(self, target_action_id: int) -> SettlementOutcome: ...
