"""报告机会决定与原子冻结。

机会决定是纯规则：累计水位与全部有效同步共同确定范围；已有
报告须同时满足业务范围和同步开始历史。仓储在写事务内选择生
成、复用或跳过，不消费事务前计算的范围。
"""

from __future__ import annotations

from collections.abc import Iterable

from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import CommandPlan, commit_operation
from camctl.reporting.models import (
    FrozenReport, ReportDecision, ReportDecisionKind, ReportOpportunity, ReportSelection,
    validate_frozen_report,
)
from camctl.reporting.ack import AckReport
from camctl.persistence.repositories.reporting import (
    read_report_opportunity, read_covering_report, read_frozen_report,
)

__all__ = [
    "ReportDecision",
    "ReportDecisionKind",
    "ReportOpportunity",
    "ReportSelection",
    "ReportingRepository",
    "decide_report",
    "freeze_report",
]


def decide_report(
    opportunity: ReportOpportunity, existing_reports: Iterable[AckReport] = (),
) -> ReportDecision:
    """一份报告须覆盖普通内容及全部同步，并包含所有同步开始历史。"""
    from_wm = opportunity.acknowledged_wm
    if opportunity.sync_from_wm is not None:
        from_wm = min(from_wm, opportunity.sync_from_wm)
    to_wm = opportunity.latest_change_wm
    if to_wm == from_wm and opportunity.sync_from_wm is None:
        return ReportDecision(ReportDecisionKind.SKIP, from_wm, to_wm)
    started = opportunity.sync_started_boundary_event_id or 0
    for report in existing_reports:
        if not isinstance(report, AckReport):
            raise ConsistencyError("已有报告必须提供明确的固定依据")
        if (report.to_wm > to_wm
                or report.frozen_event_id > opportunity.boundary.last_event_id):
            raise ConsistencyError("已有报告不能超出本次一致历史边界")
        if (report.from_wm <= from_wm and report.to_wm >= to_wm
                and report.frozen_event_id >= started):
            return ReportDecision(
                kind=ReportDecisionKind.REUSE,
                from_wm=from_wm,
                to_wm=to_wm,
                reused_report_id=report.report_id,
            )
    return ReportDecision(kind=ReportDecisionKind.GENERATE, from_wm=from_wm, to_wm=to_wm)


class _FreezeCommand:
    """原子冻结：事务内取完整 H 与范围，写入 reports 行。"""

    def __init__(self, occurred_at: int) -> None:
        self._occurred_at = occurred_at

    def plan(self, scope) -> CommandPlan:
        from camctl.history.events import RowChange, RowImage

        connection = scope.connection
        opportunity = read_report_opportunity(connection)
        preliminary = decide_report(opportunity)
        candidate = None
        if preliminary.kind is not ReportDecisionKind.SKIP:
            candidate = read_covering_report(connection, opportunity, preliminary.from_wm)
        decision = decide_report(opportunity, () if candidate is None else (candidate,))
        if decision.kind is not ReportDecisionKind.GENERATE:
            report = (read_frozen_report(connection, candidate)
                      if decision.kind is ReportDecisionKind.REUSE else None)
            return CommandPlan(events=(), owners={}, state_rows={}, read_only=True,
                               result=ReportSelection(decision.kind, report))
        boundary = opportunity.boundary
        last_event = boundary.last_event_id
        from_wm, to_wm = decision.from_wm, decision.to_wm

        report_id_row = connection.execute("SELECT MAX(id) FROM reports").fetchone()
        report_id = (report_id_row[0] if report_id_row[0] is not None else 0) + 1
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
            result=ReportSelection(ReportDecisionKind.GENERATE, report),
        )


class ReportingRepository:
    """报告冻结的 SQLite 仓储：唯一写事务经 P3 内核。"""

    def freeze_report(
        self,
        key: OperationKey,
        owned: OwnedConnection,
        *,
        occurred_at: int = 0,
    ) -> DbOutcome[ReportSelection]:
        receipt = commit_operation(
            _FreezeCommand(occurred_at), key, owned
        )
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


async def freeze_report(
    key: OperationKey, owned: OwnedConnection,
) -> DbOutcome[ReportSelection]:
    """根据事务实际事实选择并冻结报告。"""
    return ReportingRepository().freeze_report(key, owned)

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
        if after["frozen_event_id"] >= context.transaction.first_event_id:
            raise EventValidationError("冻结依据必须早于本次登记事务")
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
