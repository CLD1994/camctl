"""统一待处理责任分类。

needs_run 与正常退出检查共用本分类：各类责任可以并存，任一类
待处理即需驱动；不能依据内存队列或 latest_plan_id 判定完成，未
知事实不判为无工作。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "FactDimensionError",
    "WorkDecision",
    "WorkDecisionKind",
    "WorkFacts",
    "classify_work",
]


class FactDimensionError(ValueError):
    """责任维度取值未知或非法；不能判为无工作。"""


class WorkDecisionKind(Enum):
    NEEDS_DRIVER = "needs_driver"
    CAN_EXIT_SUCCESS = "can_exit_success"
    EXIT_REPORT_ERROR = "exit_report_error"


@dataclass(frozen=True)
class WorkFacts:
    """会话责任维度的可靠事实。

    数量维度为 None 表示无法可靠判断（事实错误）；布尔维度由查
    询方可靠取得后填写。
    """

    #: 尚未完成的动作数（含未来 scheduled_at 的 pending 动作）。
    unfinished_actions: int | None
    #: 仍应继续执行或恢复的有限设备收场、产物处理或交付流程数。
    required_settlements: int | None
    #: 应报告的业务变化尚未完成本地报告职责（含受理拒绝与 ACK 错误）。
    pending_report_changes: bool | None
    #: 本次报告处理已失败且尚未出现需要下一轮处理的新变化。
    report_failed_no_new_changes: bool
    #: 已结束动作留下的残留设备事实（无安全且必要的主动收场责任）。
    residual_device_facts: bool
    #: 允许延后到后续正常会话处理的取回半成品清理残留。
    deferred_work_cleanup: bool
    #: 本地报告职责已完成，仅等待发送或客户端 ACK。
    waiting_acknowledgement: bool
    #: 仅用于加速历史查询的快照维护尚未完成。
    snapshot_backlog: bool

    def __post_init__(self) -> None:
        for name in ("unfinished_actions", "required_settlements"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 0):
                raise FactDimensionError(f"{name} 必须是非负整数或 None: {value!r}")
        if self.pending_report_changes is None:
            raise FactDimensionError("pending_report_changes 未知")


@dataclass(frozen=True)
class WorkDecision:
    """分类结果；kind 表达会话侧的处理方向。"""

    kind: WorkDecisionKind

    @property
    def needs_driver(self) -> bool:
        return self.kind is WorkDecisionKind.NEEDS_DRIVER


def classify_work(facts: WorkFacts) -> WorkDecision:
    """按持久化责任维度判定是否存在待处理工作。

    任一必要维度未知即事实错误；存在未完成动作、必要收场或待报
    告变化时需要驱动；仅剩报告失败等待新变化时按报告失败退出；
    残留设备事实、可延后清理、ACK 等待与快照积压不构成工作。
    """
    if facts.unfinished_actions is None or facts.required_settlements is None:
        raise FactDimensionError("责任数量维度未知，不能判为无工作")
    if facts.unfinished_actions > 0 or facts.required_settlements > 0:
        return WorkDecision(WorkDecisionKind.NEEDS_DRIVER)
    if facts.pending_report_changes:
        return WorkDecision(WorkDecisionKind.NEEDS_DRIVER)
    if facts.report_failed_no_new_changes:
        return WorkDecision(WorkDecisionKind.EXIT_REPORT_ERROR)
    return WorkDecision(WorkDecisionKind.CAN_EXIT_SUCCESS)
