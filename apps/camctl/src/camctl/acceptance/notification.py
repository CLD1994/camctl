"""提交后的受理结果通知。

通知由消费完整提交结果的责任方触发，不依赖原等待者仍存在；只
有完整提交的首次受理产生新工作通知，复用与拒绝不产生新工作。
Q3 的调度端口承接本模块的通知入口。
"""

from __future__ import annotations

from typing import Protocol

from camctl.acceptance.service import AcceptanceResult, PlanDisposition

__all__ = ["AcceptanceNotifier", "notify_acceptance"]


class AcceptanceNotifier(Protocol):
    """Q3 工作通知端口：向调度侧表达新提交的可执行工作。"""

    def work_available(self, plan_id: int) -> None: ...


def notify_acceptance(result: AcceptanceResult, notifier: AcceptanceNotifier) -> None:
    """把完整提交的受理结果交给调度通知。

    仅首次注册产生新工作；回滚在服务层表现为状态库错误，调用
    方不会到达本函数。
    """
    if result.plan_disposition is PlanDisposition.REGISTERED:
        notifier.work_available(result.plan_id)
