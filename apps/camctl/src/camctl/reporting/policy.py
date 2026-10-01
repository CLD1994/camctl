"""报告机会决定与原子冻结。

机会决定是纯规则：按累计水位与最新变化判断是否生成、复用或跳
过；已有报告按覆盖范围满足需求（不以 ID 大小代替覆盖）。冻结
在写事务内重新取得完整 H 与范围，不含本事务的新变化。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Tuple

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.values import OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import CommandPlan, commit_operation
from camctl.reporting.models import FrozenReport, validate_frozen_report
from camctl.reporting.ack import AckReport

__all__ = [
    "ReportDecision",
    "ReportDecisionKind",
    "ReportOpportunity",
    "ReportingRepository",
    "decide_report",
    "freeze_report",
]


class ReportDecisionKind(Enum):
    GENERATE = "generate"
    REUSE = "reuse"
    SKIP = "skip"


@dataclass(frozen=True)
class ReportOpportunity:
    """一次报告机会的事实：种类、需求范围与已有报告覆盖。"""

    kind: str  # normal | full_sync | partial_sync
    requested_from_wm: int
    latest_change_wm: int
    acknowledged_wm: int
    existing_report_coverages: Tuple[Tuple[int, int, int], ...] = ()


@dataclass(frozen=True)
class ReportDecision:
    kind: ReportDecisionKind
    from_wm: int = 0
    to_wm: int = 0
    reused_report_id: int | None = None


def decide_report(opportunity: ReportOpportunity) -> ReportDecision:
    """按机会事实决定生成、复用或跳过。

    完整同步从 0 起算；已有报告按覆盖范围满足需求时复用；没有
    新变化（累计水位之后无业务序号）时跳过。
    """
    from_wm = 0 if opportunity.kind == "full_sync" else opportunity.requested_from_wm
    to_wm = max(opportunity.latest_change_wm, from_wm)
    if to_wm <= from_wm and not (opportunity.kind == "full_sync" and to_wm > 0):
        return ReportDecision(kind=ReportDecisionKind.SKIP)
    for report_id, cover_from, cover_to in opportunity.existing_report_coverages:
        if cover_from <= from_wm and cover_to >= to_wm:
            return ReportDecision(
                kind=ReportDecisionKind.REUSE,
                from_wm=from_wm,
                to_wm=to_wm,
                reused_report_id=report_id,
            )
    return ReportDecision(kind=ReportDecisionKind.GENERATE, from_wm=from_wm, to_wm=to_wm)


class _FreezeCommand:
    """原子冻结：事务内取完整 H 与范围，写入 reports 行。"""

    def __init__(self, decision: ReportDecision, occurred_at: int) -> None:
        self._decision = decision
        self._occurred_at = occurred_at

    def plan(self, scope) -> CommandPlan:
        from camctl.history.events import RowChange, RowImage

        connection = scope.connection
        # 完整 H：取已提交事务的最大末位事件（不含本事务）。
        boundary_row = connection.execute(
            "SELECT MAX(last_event_id) FROM history_transactions"
        ).fetchone()
        last_event = int(boundary_row[0]) if boundary_row[0] is not None else 0
        txn_row = connection.execute(
            "SELECT MAX(id) FROM history_transactions"
        ).fetchone()
        txn_id = int(txn_row[0]) if txn_row[0] is not None else 0
        boundary = (
            INITIAL_BOUNDARY
            if last_event == 0
            else HistoryBoundary(txn_id=txn_id, last_event_id=last_event)
        )
        # 终点与 H 同源；事务前的生成决定不固定最终业务水位。
        max_wm_row = connection.execute(
            "SELECT MAX(change_seq) FROM history_events WHERE id <= ?", (last_event,)
        ).fetchone()
        max_wm = int(max_wm_row[0]) if max_wm_row[0] is not None else 0
        to_wm = max_wm
        from_wm = self._decision.from_wm

        report_id_row = connection.execute("SELECT MAX(id) FROM reports").fetchone()
        report_id = (int(report_id_row[0]) if report_id_row[0] is not None else 0) + 1
        AckReport(report_id, from_wm, to_wm, last_event)

        row = RowChange(
            table="reports",
            row_id=report_id,
            before=RowImage(exists=False, values={}),
            after=RowImage(
                exists=True,
                values={
                    "frozen_event_id": last_event,
                    "from_wm": from_wm,
                    "to_wm": to_wm,
                    "format_version": 1,
                    "status": 1,
                    "size_bytes": None,
                    "sha256": None,
                    "publication_count": 0,
                    "last_published_event_id": None,
                    "last_error_json": None,
                },
            ),
        )
        from camctl.history.events import EventEnvelope

        allocation = scope.allocate(1)
        event = EventEnvelope(
            event_id=allocation.first_event_id,
            transaction_id=allocation.txn_id,
            event_type=28,
            event_version=1,
            occurred_at=self._occurred_at,
            clock_status=2,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(row,),
        )
        report = FrozenReport(
            report_id=report_id,
            boundary=boundary,
            from_wm=from_wm,
            to_wm=to_wm,
            format_version=1,
            scope=(),
        )
        validate_frozen_report(report)
        return CommandPlan(
            events=(event,),
            owners={("reports", report_id): ("report", report_id)},
            state_rows={"reports": {}},
            result=report,
        )


class ReportingRepository:
    """报告冻结的 SQLite 仓储：唯一写事务经 P3 内核。"""

    def freeze_report(
        self,
        decision: ReportDecision,
        key: OperationKey,
        owned: OwnedConnection,
        *,
        occurred_at: int = 0,
    ) -> DbOutcome[FrozenReport]:
        receipt = commit_operation(
            _FreezeCommand(decision, occurred_at), key, owned
        )
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


async def freeze_report(
    decision: ReportDecision, key: OperationKey, owned: OwnedConnection
) -> DbOutcome[FrozenReport]:
    """冻结一份报告（异步入口；FreezeReport 只指定机会决定）。"""
    return ReportingRepository().freeze_report(decision, key, owned)

def _report_guard(event, context) -> None:
    """REPORT_CHANGED.FREEZE 的正式守卫（reports-runtime.md#报告字段）。"""
    for row in event.rows:
        if row.table != "reports" or row.before.exists:
            continue
        after = row.after.values
        try:
            AckReport(row.row_id, after.get("from_wm"), after.get("to_wm"),
                      after.get("frozen_event_id"))
        except ValueError as error:
            raise EventValidationError(str(error)) from error
        version = after.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise EventValidationError("报告格式版本不受支持")
        if after.get("status") != 1:
            raise EventValidationError("冻结创建的状态必须是 REGISTERED")


def register_report_guards() -> None:
    register_guard("report", _report_guard)
    register_sync_guard()


class _PublishCommand:
    """可靠发布：INTENT（2→3）与 PUBLISH（3→4）同一事务完成。"""

    def __init__(self, report_id: int, occurred_at: int) -> None:
        self._report_id = report_id
        self._occurred_at = occurred_at

    def plan(self, scope) -> CommandPlan:
        from camctl.history.events import EventEnvelope, RowChange, RowImage
        from camctl.persistence.transaction import TransactionError

        connection = scope.connection
        row = connection.execute(
            "SELECT status, size_bytes, sha256, publication_count,"
            " last_published_event_id, last_error_json FROM reports WHERE id = ?",
            (self._report_id,),
        ).fetchone()
        if row is None:
            raise TransactionError(f"报告 {self._report_id} 不存在")
        status, size_bytes, sha256, publication_count, last_pub, _err = row
        if size_bytes is None or sha256 is None:
            raise TransactionError("报告尚无确定字节，不能发布")

        allocation = scope.allocate(2)
        intent = EventEnvelope(
            event_id=allocation.first_event_id,
            transaction_id=allocation.txn_id,
            event_type=28, event_version=1, occurred_at=self._occurred_at,
            clock_status=2, change_seq=None, reason=3, evidence={},
            rows=(
                RowChange(
                    table="reports", row_id=self._report_id,
                    before=RowImage(exists=True, values={"status": status, "last_error_json": None}),
                    after=RowImage(exists=True, values={"status": 3, "last_error_json": None}),
                ),
            ),
        )
        publish = EventEnvelope(
            event_id=allocation.last_event_id,
            transaction_id=allocation.txn_id,
            event_type=28, event_version=1, occurred_at=self._occurred_at,
            clock_status=2, change_seq=None, reason=4, evidence={},
            rows=(
                RowChange(
                    table="reports", row_id=self._report_id,
                    before=RowImage(
                        exists=True,
                        values={"status": 3, "publication_count": publication_count,
                                "last_published_event_id": last_pub, "last_error_json": None},
                    ),
                    after=RowImage(
                        exists=True,
                        values={"status": 4, "publication_count": publication_count + 1,
                                "last_published_event_id": allocation.last_event_id,
                                "last_error_json": None},
                    ),
                ),
            ),
        )
        return CommandPlan(
            events=(intent, publish),
            owners={("reports", self._report_id): ("report", self._report_id)},
            state_rows={"reports": {}},
            result={"report_id": self._report_id, "publication_count": publication_count + 1},
        )


def publish_report(
    repository_key: OperationKey,
    owned: OwnedConnection,
    report_id: int,
    *,
    occurred_at: int = 0,
) -> DbOutcome:
    """记录一次可靠发布（字节与文件证据在事务外已就绪）。"""
    receipt = commit_operation(
        _PublishCommand(report_id, occurred_at), repository_key, owned
    )
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def record_report_bytes(
    key: OperationKey,
    owned: OwnedConnection,
    report_id: int,
    payload: bytes,
    *,
    occurred_at: int = 0,
) -> DbOutcome:
    """保存报告确定字节（REPORT_CHANGED.BYTES：size 与 sha256）。"""
    import hashlib

    from camctl.history.events import EventEnvelope, RowChange, RowImage

    digest = hashlib.sha256(payload).hexdigest()

    class _BytesCommand:
        def plan(self, scope) -> CommandPlan:
            connection = scope.connection
            row = connection.execute(
                "SELECT status, size_bytes, sha256, publication_count FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            if row is None:
                from camctl.persistence.transaction import TransactionError

                raise TransactionError(f"报告 {report_id} 不存在")
            status, size_bytes, sha256, _count = row
            allocation = scope.allocate(1)
            change = RowChange(
                table="reports",
                row_id=report_id,
                before=RowImage(
                    exists=True,
                    values={
                        "status": status,
                        "size_bytes": size_bytes,
                        "sha256": sha256,
                        "last_error_json": None,
                    },
                ),
                after=RowImage(
                    exists=True,
                    values={
                        "status": 2,
                        "size_bytes": len(payload),
                        "sha256": digest,
                        "last_error_json": None,
                    },
                ),
            )
            event = EventEnvelope(
                event_id=allocation.first_event_id,
                transaction_id=allocation.txn_id,
                event_type=28,
                event_version=1,
                occurred_at=occurred_at,
                clock_status=2,
                change_seq=None,
                reason=2,
                evidence={},
                rows=(change,),
            )
            return CommandPlan(
                events=(event,),
                owners={("reports", report_id): ("report", report_id)},
                state_rows={"reports": {}},
                result={"report_id": report_id, "size_bytes": len(payload), "sha256": digest},
            )

    receipt = commit_operation(_BytesCommand(), key, owned)
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _sync_guard(event, context) -> None:
    """同步的结束事实引用本事件；ACK 资格由 ACK 守卫负责。"""
    for row in event.rows:
        if row.table != "state_syncs" or row.before.exists is False:
            continue
        if "ended_event_id" in row.after.values:
            if row.after.values["ended_event_id"] != event.event_id:
                raise EventValidationError("同步结束依据必须是本事件")


def register_sync_guard() -> None:
    register_guard("sync", _sync_guard)
