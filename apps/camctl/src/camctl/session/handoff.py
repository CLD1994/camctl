"""事务内接管与接纳关闭的纯规则。

submit 在同一写事务内查最新工作并探测接纳锁，按本规则得出接
管判定；正常关闭在同一事务内重新检查工作并释放接纳，有新工作
则保持接纳。设备、文件和报告生成不进入交接事务。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from camctl.session.locks import AdmissionProbe
from camctl.session.work import WorkDecision, WorkDecisionKind

__all__ = ["HandoffDecision", "HandoffOutcome", "WorkFactsError", "decide_handoff"]


class WorkFactsError(ValueError):
    """工作事实未知或矛盾；不输出成功接管判定。"""


class HandoffOutcome(Enum):
    """接管判定的四个分区。"""

    NO_PENDING_WORK = "no_pending_work"
    ACCEPTOR_PRESENT = "acceptor_present"
    REQUIRES_RUN = "requires_run"


@dataclass(frozen=True)
class HandoffDecision:
    outcome: HandoffOutcome

    @property
    def needs_run(self) -> bool:
        return self.outcome is HandoffOutcome.REQUIRES_RUN


def decide_handoff(work: WorkDecision, probe: AdmissionProbe) -> HandoffDecision:
    """按最新工作分类与接纳探测决定本次提交后的接管。

    无待处理工作时不需要后续 run；有待处理工作且存在真正接纳者
    时由接纳者负责；有待处理工作且接纳空闲时请求后续 run。工作
    事实未知属于会话错误，不进入本函数的成功分区。
    """
    if work.kind is WorkDecisionKind.NEEDS_DRIVER or (
        work.kind is WorkDecisionKind.EXIT_REPORT_ERROR
    ):
        if probe.is_free:
            return HandoffDecision(HandoffOutcome.REQUIRES_RUN)
        return HandoffDecision(HandoffOutcome.ACCEPTOR_PRESENT)
    if work.kind is WorkDecisionKind.CAN_EXIT_SUCCESS:
        return HandoffDecision(HandoffOutcome.NO_PENDING_WORK)
    raise WorkFactsError(f"未知工作分类: {work.kind}")
