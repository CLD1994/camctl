"""会话侧仓储：事务内接纳关闭。

正常关闭在同一写事务内重新检查工作并释放接纳；有新工作或报告
失败等待变化时保持接纳。锁释放是事务内的非阻塞锁操作，不产生
权威事件。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from camctl.contracts.values import OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import CommandPlan, commit_operation
from camctl.session.work import WorkDecision, WorkDecisionKind, WorkFacts, classify_work

__all__ = ["CloseAdmission", "CloseDecision", "SessionRepository"]


@dataclass(frozen=True)
class CloseAdmission:
    """正常关闭接纳的命令：事务内重新查询工作并按结果释放接纳。"""

    facts_query: Callable[[sqlite3.Connection], WorkFacts]
    release_admission: Callable[[], None]


@dataclass(frozen=True)
class CloseDecision:
    """关闭事务的结果。"""

    admission_closed: bool
    work: WorkDecision


class _CloseCommand:
    def __init__(self, command: CloseAdmission) -> None:
        self._command = command

    def plan(self, scope) -> CommandPlan:
        facts = self._command.facts_query(scope.connection)
        decision = classify_work(facts)
        closed = decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS
        if closed:
            self._command.release_admission()
        return CommandPlan(
            events=(),
            owners={},
            state_rows={},
            read_only=True,
            result=CloseDecision(admission_closed=closed, work=decision),
        )


class SessionRepository:
    """接纳关闭的 SQLite 仓储。"""

    def close_admission(
        self,
        command: CloseAdmission,
        key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[CloseDecision]:
        receipt = commit_operation(_CloseCommand(command), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)
