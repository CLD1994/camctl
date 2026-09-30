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

    def update_lower_bound(self, check, key: OperationKey, owned: OwnedConnection):
        """保存可信历史时间下界变更；无变更时按只读事务完成。"""
        receipt = commit_operation(_LowerBoundCommand(check), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

class _LowerBoundCommand:
    """以短事务保存可信时间下界（内部管理事实，不占业务水位）。"""

    def __init__(self, check) -> None:
        self._check = check

    def plan(self, scope) -> CommandPlan:
        from camctl.history.events import EventEnvelope, RowChange, RowImage
        from camctl.persistence.transaction import TransactionError

        check = self._check
        if not check.trusted or not check.needs_bound_update:
            raise TransactionError("没有可信下界变更需要保存")
        row = scope.connection.execute(
            "SELECT trusted_time_lower_bound, trusted_time_event_id"
            " FROM runtime_state WHERE id = 1"
        ).fetchone()
        current_bound, current_event = int(row[0]) if row[0] is not None else None, row[1]
        if current_bound is not None and check.reading_micros <= current_bound:
            return CommandPlan(
                events=(),
                owners={},
                state_rows={},
                read_only=True,
                result=None,
            )
        allocation = scope.allocate(1)
        change = RowChange(
            table="runtime_state",
            row_id=1,
            before=RowImage(
                exists=True,
                values={
                    "trusted_time_lower_bound": current_bound,
                    "trusted_time_event_id": current_event,
                },
            ),
            after=RowImage(
                exists=True,
                values={
                    "trusted_time_lower_bound": check.new_lower_bound_micros,
                    "trusted_time_event_id": allocation.first_event_id,
                },
            ),
        )
        event = EventEnvelope(
            event_id=allocation.first_event_id,
            transaction_id=allocation.txn_id,
            event_type=31,
            event_version=1,
            occurred_at=check.reading_micros,
            clock_status=1,
            change_seq=None,
            reason=1 if current_bound is None else 2,
            evidence={},
            rows=(change,),
        )
        return CommandPlan(
            events=(event,),
            owners={("runtime_state", 1): ("runtime_state", 1)},
            state_rows={"runtime_state": {}},
        )
