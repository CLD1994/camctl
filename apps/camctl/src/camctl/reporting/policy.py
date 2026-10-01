"""报告机会决定、原子冻结与独立发布管理事实。

机会决定是纯规则：累计水位与全部有效同步共同确定范围；已有
报告须同时满足业务范围和同步开始历史。仓储在写事务内选择生
成、复用或跳过，不消费事务前计算的范围。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum

from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import CommandPlan, commit_operation
from camctl.reporting.models import (
    FrozenReport, ReportDecision, ReportDecisionKind, ReportOpportunity, ReportSelection,
    ReportBytes, ReportPublication, ReportStatus, validate_frozen_report, validate_report_management,
)
from camctl.reporting.ack import AckReport
from camctl.persistence.repositories.reporting import (
    read_report_opportunity, read_covering_report, read_frozen_report,
    read_report_management,
)
from camctl.history.events import load_event_registry
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.persistence.transaction import encode_json_value, event_envelope, update_change

_REPORT_EVENT = load_event_registry()["events"]["REPORT_CHANGED"]
_ReportChange = IntEnum("ReportChange", {name: spec["reason"] for name, spec in _REPORT_EVENT["branches"].items()})

__all__ = [
    "ReportDecision",
    "ReportDecisionKind",
    "ReportOpportunity",
    "ReportSelection",
    "ReportingRepository",
    "decide_report",
    "freeze_report",
    "record_report_bytes",
    "record_report_publish_intent",
    "record_report_failure",
    "publish_report",
    "validate_report_publication_result",
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
    """验证 REPORT_CHANGED 各分支的固定字节与管理事实。"""
    for row in event.rows:
        if row.table != "reports":
            continue
        if row.before.exists:
            before = dict(context.state_rows.get("reports", {}).get(row.row_id, {}))
            before.update(row.before.values)
            after = {**before, **row.after.values}
            try:
                old_bytes = validate_report_management(before)
                new_bytes = validate_report_management(after)
                for values in (before, after):
                    if values["last_error_json"] is not None:
                        parse_exact_json(encode_json_value(values["last_error_json"]))
                if event.reason == _ReportChange.INTENT and not json_equal(after["last_error_json"], before["last_error_json"]):
                    raise EventValidationError("发布意图不能提前清除文件处理错误")
            except (ValueError, RecursionError) as error:
                raise EventValidationError(str(error)) from error
            if old_bytes is not None and new_bytes != old_bytes:
                raise EventValidationError("报告首次确定的字节依据不能改变")
            if event.reason == _ReportChange.PUBLISH:
                if after["publication_count"] != before["publication_count"] + 1:
                    raise EventValidationError("每次可靠发布必须恰好增加一次成功计数")
                if after["last_published_event_id"] != event.event_id:
                    raise EventValidationError("本次发布依据必须引用当前事件")
            continue
        after = row.after.values
        try:
            validate_report_management(after)
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
        if after["last_error_json"] is not None:
            raise EventValidationError("新登记报告尚无文件处理错误")


def register_report_guards() -> None:
    register_guard("report", _report_guard)
    register_sync_guard()


def validate_report_publication_result(result: PublishResult) -> None:
    """只有已确认移动、源移除及适用目录同步完整成功才能保存发布。"""
    if (not isinstance(result, PublishResult) or result.stage is not PublishStage.MOVED
            or result.source_removed is not True or result.error is not None
            or (result.directory is not DirectorySyncStage.SYNCED and result.directory is not DirectorySyncStage.UNSUPPORTED)):
        raise ConsistencyError("报告文件交接尚未可靠完成，不能保存发布成功")


class _ReportManagementCommand:
    """一次独立管理事实；每次在同一事务重新核对实际旧状态。"""

    def __init__(self, kind: _ReportChange, key: OperationKey, report_id: int, occurred_at: int,
                 *, contents: ReportBytes | None = None, file_result: PublishResult | None = None,
                 error: dict | None = None) -> None:
        self.kind, self.key, self.report_id = kind, key, report_id
        self.occurred_at, self.contents = occurred_at, contents
        self.file_result, self.error = file_result, error

    def plan(self, scope) -> CommandPlan:
        from camctl.contracts.values import ObjectId

        ObjectId(self.report_id)
        facts = read_report_management(scope.connection, self.report_id)
        contents = validate_report_management(facts)
        if self.kind is _ReportChange.PREPARE:
            if not isinstance(self.contents, ReportBytes):
                raise ConsistencyError("报告准备只接收确定长度与摘要")
            if contents is not None and contents != self.contents:
                raise ConsistencyError("重建字节与首次确定的报告不一致")
        if self.kind is _ReportChange.PUBLISH:
            validate_report_publication_result(self.file_result)
        if self.kind is _ReportChange.FAIL:
            if not isinstance(self.error, dict):
                raise ConsistencyError("报告文件错误必须是 JSON 对象")
            # 取得可独立保存的精确值，拒绝非法 JSON 和不可持久化类型。
            self.error = parse_exact_json(encode_json_value(self.error))
        saved = self._saved_plan(scope.connection, contents)
        if saved is not None:
            return saved

        status = ReportStatus(facts["status"])
        after = {"status": status, "last_error_json": facts["last_error_json"]}
        result = contents
        if self.kind is _ReportChange.PREPARE:
            if status not in (ReportStatus.REGISTERED, ReportStatus.FAILED):
                return self._read_only(contents)
            after.update(status=ReportStatus.PREPARED, size_bytes=self.contents.size_bytes,
                         sha256=self.contents.sha256, last_error_json=None)
            result = self.contents
        elif self.kind is _ReportChange.INTENT:
            if contents is None:
                raise ConsistencyError("报告尚无确定字节，不能保存发布意图")
            if status is ReportStatus.PUBLISHING:
                return self._read_only(contents)
            after["status"] = ReportStatus.PUBLISHING
        elif self.kind is _ReportChange.PUBLISH:
            if status is not ReportStatus.PUBLISHING:
                raise ConsistencyError("报告必须先保存本次发布意图")
            after.update(status=ReportStatus.PUBLISHED, publication_count=facts["publication_count"] + 1,
                         last_published_event_id=None, last_error_json=None)
        elif self.kind is _ReportChange.FAIL:
            if status is ReportStatus.FAILED and json_equal(self.error, facts["last_error_json"]):
                return self._read_only(None)
            after.update(status=ReportStatus.FAILED, last_error_json=self.error)
            result = None
        else:
            raise ConsistencyError("该管理入口不支持此报告变化")

        allocation = scope.allocate(1)
        if self.kind is _ReportChange.PUBLISH:
            after["last_published_event_id"] = allocation.first_event_id
            result = ReportPublication(self.report_id, after["publication_count"], allocation.first_event_id)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, _REPORT_EVENT["id"],
                               self.kind, (update_change("reports", self.report_id,
                                                        {k: facts[k] for k in after}, after),), self.occurred_at)
        return CommandPlan(events=(event,), owners={("reports", self.report_id): ("report", self.report_id)},
                           state_rows={"reports": {self.report_id: facts}}, result=result)

    @staticmethod
    def _read_only(result: ReportBytes | ReportPublication | None) -> CommandPlan:
        return CommandPlan(events=(), owners={}, state_rows={}, read_only=True, result=result)

    def _saved_plan(self, connection, contents: ReportBytes | None) -> CommandPlan | None:
        """原操作键只复用同一目标与输入的已提交事实，绝不重做发布。"""
        rows = connection.execute(
            "SELECT event.id, event.event_type, event.body_json FROM history_events AS event"
            " JOIN history_transactions AS txn ON txn.id = event.transaction_id"
            " WHERE txn.operation_key = ? ORDER BY event.id LIMIT 2", (str(self.key),),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0][1] != _REPORT_EVENT["id"]:
            raise ConsistencyError("报告操作键已用于其他事务")
        event_id, _, raw = rows[0]
        body = parse_exact_json(raw)
        changes = body.get("rows", [])
        if (body.get("reason") != self.kind or len(changes) != 1
                or changes[0].get("table") != "reports" or changes[0].get("id") != self.report_id
                or changes[0].get("before", {}).get("exists") is not True):
            raise ConsistencyError("报告操作键与目标或管理分支不一致")
        after = changes[0]["after"]["values"]
        if self.kind is _ReportChange.PREPARE:
            if ReportBytes(after["size_bytes"], after["sha256"]) != self.contents:
                raise ConsistencyError("报告操作键对应不同字节")
            return self._read_only(self.contents)
        if self.kind is _ReportChange.PUBLISH:
            if after["last_published_event_id"] != event_id:
                raise ConsistencyError("原报告发布事实引用无效")
            return self._read_only(ReportPublication(self.report_id, after["publication_count"], event_id))
        if self.kind is _ReportChange.FAIL:
            if not json_equal(after["last_error_json"], self.error):
                raise ConsistencyError("报告操作键对应不同错误")
            return self._read_only(None)
        return self._read_only(contents)


def _record_management(kind, key, owned, report_id, occurred_at, **values) -> DbOutcome:
    receipt = commit_operation(_ReportManagementCommand(kind, key, report_id, occurred_at, **values), key, owned)
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def record_report_bytes(key: OperationKey, owned: OwnedConnection, report_id: int,
                        contents: ReportBytes, *, occurred_at: int = 0) -> DbOutcome[ReportBytes]:
    """保存合格生成结果的长度与摘要；重建匹配时保持首次确定字节。"""
    return _record_management(_ReportChange.PREPARE, key, owned, report_id, occurred_at, contents=contents)


def record_report_publish_intent(key: OperationKey, owned: OwnedConnection, report_id: int,
                                 *, occurred_at: int = 0) -> DbOutcome[ReportBytes]:
    """发布文件之前保存意图；保持原确定字节和曾经成功的发布事实。"""
    return _record_management(_ReportChange.INTENT, key, owned, report_id, occurred_at)


def publish_report(key: OperationKey, owned: OwnedConnection, report_id: int,
                   file_result: PublishResult, *, occurred_at: int = 0) -> DbOutcome[ReportPublication]:
    """按已确认交接结果保存一次成功；不重新检查文件是否仍在 ready。"""
    return _record_management(_ReportChange.PUBLISH, key, owned, report_id, occurred_at, file_result=file_result)


def record_report_failure(key: OperationKey, owned: OwnedConnection, report_id: int,
                          error: dict, *, occurred_at: int = 0) -> DbOutcome[None]:
    """保留本次文件处理错误；不抹除已有字节及此前成功发布。"""
    return _record_management(_ReportChange.FAIL, key, owned, report_id, occurred_at, error=error)


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
