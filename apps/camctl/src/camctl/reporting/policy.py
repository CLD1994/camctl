"""报告机会决定、原子冻结与独立发布管理事实。

机会决定是纯规则：累计水位与全部有效同步共同确定范围；已有
报告须同时满足业务范围和同步开始历史。仓储在写事务内选择生
成、复用或跳过，不消费事务前计算的范围。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from enum import IntEnum

from camctl.contracts.values import ConsistencyError, OperationKey, UtcMicros
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id,
    row_change,
    row_facts,
)
from camctl.reporting.models import (
    FrozenReport, ReportDecision, ReportDecisionKind, ReportOpportunity, ReportSelection,
    ReportBytes, ReportPublication, ReportStatus, validate_frozen_report, validate_report_management,
    SyncDisposition, SyncMode, SyncSaved,
)
from camctl.contracts.workflow_errors import registered_error, validate_error_details
from camctl.reporting.ack import AckReport
from camctl.persistence.repositories.reporting import (
    read_report_opportunity, read_covering_report, read_frozen_report,
    read_report_management, read_ack_report, read_report_bytes_at_boundary,
)
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.history.events import load_event_registry
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.persistence.transaction import encode_json_value, event_envelope, read_transaction_range, saved_transaction_events, update_change

_REPORT_EVENT = load_event_registry()["events"]["REPORT_CHANGED"]
_REPORT_ACTION_TYPE = 7
_SYNC_EVENT = load_event_registry()["events"]["SYNC_CHANGED"]
_SYNC_EVENT_ID = _SYNC_EVENT["id"]
_ACTION_STARTED_EVENT_ID = load_event_registry()["events"]["ACTION_STARTED"]["id"]
_ACTION_FINISHED_EVENT_ID = load_event_registry()["events"]["ACTION_FINISHED"]["id"]
_PLAN_STATUS_EVENT = load_event_registry()["events"]["PLAN_STATUS_CHANGED"]["id"]
#: 与拍摄、取回链一致的动作终态集合及计划完成状态。
_ACTION_TERMINAL = (3, 4, 5, 6)
_PLAN_COMPLETE = 3
#: 成功、失败、过期终态：取消不结束这些动作的同步责任。
_SYNC_KEEPING_TERMINAL = (3, 4, 5)
_SYNC_STATUS = enum_for("state_syncs.status")
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
    "record_recovered_publication",
    "validate_report_publication_result",
    "start_sync",
    "record_local_report",
    "cancel_sync",
    "finish_canceled_sync_action",
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

    def __init__(self, occurred_at: int, key: OperationKey) -> None:
        self._occurred_at = occurred_at
        self._key = key

    def plan(self, scope) -> CommandPlan:
        from camctl.history.events import RowChange, RowImage

        connection = scope.connection
        UtcMicros(self._occurred_at)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._saved_plan(connection, saved)
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

        with closing(connection.execute("SELECT MAX(id) FROM reports")) as cursor:
            report_id_row = cursor.fetchone()
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

    def _saved_plan(self, connection, saved) -> CommandPlan:
        if (len(saved) != 1 or saved[0]["type"] != _REPORT_EVENT["id"]
                or saved[0]["reason"] != _ReportChange.FREEZE):
            raise ConsistencyError("原操作键不属于报告冻结阶段")
        event = saved[0]
        if event["occurred_at"] != self._occurred_at:
            raise ConsistencyError("冻结重送的事件时刻与原输入不同")
        rows = event["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "reports"
                or rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]):
            raise ConsistencyError("原冻结必须创建恰好一份报告")
        row = rows[0]
        original = row["after"]["values"]
        validate_report_management(original)
        if original["status"] != ReportStatus.REGISTERED or original["last_error_json"] is not None:
            raise ConsistencyError("原冻结的登记状态无效")
        current = read_ack_report(connection, row["id"])
        if current is None:
            raise ConsistencyError("原冻结报告缺失")
        fixed = {"frozen_event_id": current.frozen_event_id, "from_wm": current.from_wm,
                 "to_wm": current.to_wm, "format_version": 1}
        with closing(connection.execute(
            "SELECT created_event_id FROM reports WHERE id = ?", (row["id"],),
        )) as cursor:
            created = cursor.fetchone()
        if (created is None or created[0] != event["event_id"]
                or any(not json_equal(value, original[name]) for name, value in fixed.items())):
            raise ConsistencyError("原报告身份或固定生成依据与冻结历史不符")
        transaction = event["transaction"]
        boundary = INITIAL_BOUNDARY
        if transaction.txn_id != 1:
            prior = read_transaction_range(connection, transaction.txn_id - 1)
            boundary = HistoryBoundary(prior.txn_id, prior.last_event_id)
        if original["frozen_event_id"] != boundary.last_event_id:
            raise ConsistencyError("原报告 H 必须是登记前的完整已提交边界")
        report = FrozenReport(row["id"], boundary, original["from_wm"], original["to_wm"], original["format_version"])
        validate_frozen_report(report)
        return CommandPlan(events=(), owners={}, state_rows={}, read_only=True,
                           result=ReportSelection(ReportDecisionKind.GENERATE, report))


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
            _FreezeCommand(occurred_at, key), key, owned
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

def _validate_management_change(reason: int, event_id: int, before: Mapping, after: Mapping) -> ReportBytes | None:
    """完整前后状态符合权威分支约束，字节和发布事实保持原含义。"""
    old_bytes = validate_report_management(before)
    new_bytes = validate_report_management(after)
    definition = _REPORT_EVENT["branches"][_ReportChange(reason).name]["rows"][0]
    if definition["op"] != "update":
        raise ConsistencyError("该报告分支不能更新已有报告")
    for side, values in (("before", before), ("after", after)):
        for name, allowed in definition.get(side, {}).items():
            if name not in values or not any(json_equal(values[name], value) for value in allowed):
                raise ConsistencyError(f"报告 {side}.{name} 不符合完整分支状态")
        if values["last_error_json"] is not None:
            parse_exact_json(encode_json_value(values["last_error_json"]))
    if old_bytes is not None and new_bytes != old_bytes:
        raise ConsistencyError("报告首次确定的字节依据不能改变")
    if reason == _ReportChange.INTENT and not json_equal(after["last_error_json"], before["last_error_json"]):
        raise ConsistencyError("发布意图不能提前清除文件处理错误")
    if reason == _ReportChange.PUBLISH:
        if after["publication_count"] != before["publication_count"] + 1:
            raise ConsistencyError("每次可靠发布必须恰好增加一次成功计数")
        if after["last_published_event_id"] != event_id:
            raise ConsistencyError("本次发布依据必须引用当前事件")
    return new_bytes


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
                _validate_management_change(event.reason, event.event_id, before, after)
            except (ValueError, RecursionError) as error:
                raise EventValidationError(str(error)) from error
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
        UtcMicros(self.occurred_at)
        if self.kind is _ReportChange.PREPARE:
            if not isinstance(self.contents, ReportBytes):
                raise ConsistencyError("报告准备只接收确定长度与摘要")
        if self.kind is _ReportChange.PUBLISH:
            validate_report_publication_result(self.file_result)
        if self.kind is _ReportChange.FAIL:
            if not isinstance(self.error, dict):
                raise ConsistencyError("报告文件错误必须是 JSON 对象")
            # 取得可独立保存的精确值，拒绝非法 JSON 和不可持久化类型。
            self.error = parse_exact_json(encode_json_value(self.error))
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            return self._saved_plan(scope, saved)
        facts = read_report_management(scope.connection, self.report_id)
        contents = validate_report_management(facts)
        if self.kind is _ReportChange.PREPARE and contents is not None and contents != self.contents:
            raise ConsistencyError("重建字节与首次确定的报告不一致")

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

    def _saved_plan(self, scope, events) -> CommandPlan:
        """原操作键只复用同一目标与输入的已提交事实，绝不重做发布。"""
        if len(events) != 1 or events[0]["type"] != _REPORT_EVENT["id"]:
            raise ConsistencyError("报告操作键已用于其他事务")
        event_id = events[0]["event_id"]
        body = events[0]["body"]
        changes = body["rows"]
        if (events[0]["reason"] != self.kind or len(changes) != 1
                or changes[0].get("table") != "reports" or changes[0].get("id") != self.report_id
                or changes[0].get("before", {}).get("exists") is not True
                or changes[0].get("after", {}).get("exists") is not True
                or events[0]["occurred_at"] != self.occurred_at):
            raise ConsistencyError("报告操作键与目标或管理分支不一致")
        connection = scope.connection
        facts = read_report_management(connection, self.report_id)
        columns = frozenset({"status", "size_bytes", "sha256", "publication_count",
                             "last_published_event_id", "last_error_json"})
        transaction = events[0]["transaction"]
        after = read_row_values_at_boundary(connection, owner=("report", self.report_id),
            table="reports", row_id=self.report_id, columns=columns, current_values=facts,
            boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
        changed = changes[0]["after"]["values"]
        if any(name not in after or not json_equal(value, after[name]) for name, value in changed.items()):
            raise ConsistencyError("原报告管理变化与原完整边界不一致")
        before = {**after, **changes[0]["before"]["values"]}
        contents = _validate_management_change(self.kind, event_id, before, after)
        basis = read_report_bytes_at_boundary(connection, self.report_id, transaction.last_event_id)
        if contents != basis:
            raise ConsistencyError("原报告字节与首次确定的历史依据不符")
        if self.kind is _ReportChange.PREPARE:
            if contents != self.contents:
                raise ConsistencyError("报告操作键对应不同字节")
            return self._read_only(contents)
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


# ---- R6 剩余消费者：同步开始、本地完成与取消 ------------------------

def start_sync(key: OperationKey, owned: OwnedConnection, *, action_id: int,
               mode: SyncMode, occurred_at: int,
               after_report_id: int | None = None) -> DbOutcome[SyncSaved]:
    """同步实际开始：动作进入运行、起点确定与责任建立同一事务。

    增量起点报告在本事务内核实；固定起点不存在时，动作先进入运行
    并在同一事务保存执行失败，不建立同步责任。重试、重启及同一请
    求重送沿用原记录，不重算起点。
    """
    command = _StartSyncCommand(key, action_id, mode, after_report_id,
                                occurred_at)
    receipt = commit_operation(command, key, owned)
    return _sync_outcome(receipt)


def record_local_report(key: OperationKey, owned: OwnedConnection, *,
                        action_id: int, local_report_id: int,
                        occurred_at: int) -> DbOutcome[SyncSaved]:
    """本地报告满足与动作成功共同保存（SYNC_CHANGED.LOCAL）。

    报告已经发布后，同一事务记录本地完成并保存动作成功终态；同步
    责任已被合格 ACK 结束时仍可补记本地完成。
    """
    command = _LocalReportCommand(key, action_id, local_report_id,
                                  occurred_at)
    receipt = commit_operation(command, key, owned)
    return _sync_outcome(receipt)


def cancel_sync(key: OperationKey, owned: OwnedConnection, *, action_id: int,
                occurred_at: int) -> DbOutcome[SyncSaved]:
    """取消消费者：取消事务中结束该动作的同步责任（CANCEL）。

    尚未开始的报告动作没有责任可结束；已成功的动作保留责任等待
    合格 ACK，失败或过期的动作保留责任由后续报告机会继续。
    """
    command = _CancelSyncCommand(key, action_id, occurred_at)
    receipt = commit_operation(command, key, owned)
    return _sync_outcome(receipt)


def finish_canceled_sync_action(
    key: OperationKey, owned: OwnedConnection, *, action_id: int, occurred_at: int,
) -> DbOutcome[SyncSaved]:
    """结束报告动作的本地取消收场；不等待或停止共享报告生成。"""
    receipt = commit_operation(
        _FinishCanceledSyncCommand(key, action_id, occurred_at), key, owned)
    return _sync_outcome(receipt)


def record_recovered_publication(key: OperationKey, owned: OwnedConnection,
                                 report_id: int, *, observed_sha256: str,
                                 occurred_at: int = 0) -> DbOutcome[ReportPublication | None]:
    """恢复时发现报告已发布：按目录观察证据保存发布事实。

    观察摘要与首次确定字节核对一致才保存；未发布过的报告与发布
    意图（必要时连同失败恢复）在同一事务补齐。保留现有文件，不
    重复生成或投放。
    """
    command = _RecoveredPublicationCommand(
        key, report_id, observed_sha256, occurred_at)
    receipt = commit_operation(command, key, owned)
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _sync_outcome(receipt) -> DbOutcome[SyncSaved]:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _require_startable(action) -> None:
    if (action["status"] != 1 or action["execution_started"] != 0
            or action["cancel_requested"] != 0):
        raise TransactionError(
            "同步开始要求未启动且未取消的待执行报告动作:"
            f" {action['id']} status={action['status']!r}"
            f" cancel_requested={action['cancel_requested']!r}")


class _StartSyncCommand:
    """同步实际开始：同一事务写入动作开始并固定同步起点。"""

    def __init__(self, key, action_id, mode, after_report_id,
                 occurred_at) -> None:
        if mode not in tuple(SyncMode):
            raise TypeError(f"同步模式必须使用 SyncMode: {mode!r}")
        if (mode is SyncMode.INCREMENTAL) == (after_report_id is None):
            raise ValueError("增量同步必须携带起点报告，全量不携带")
        self._key = key
        self._action_id = action_id
        self._mode = mode
        self._after_report_id = after_report_id
        self._occurred_at = occurred_at
        self._owners: dict = {}
        self._state: dict = {"actions": {}, "state_syncs": {}, "reports": {}}

    def plan(self, scope):
        connection = scope.connection
        action = _load_action(connection, self._state, self._action_id)
        if action["type"] != _REPORT_ACTION_TYPE:
            raise ConsistencyError("同步开始要求报告动作")
        existing = connection.execute(
            "SELECT id FROM state_syncs WHERE action_id = ?",
            (self._action_id,)).fetchone()
        if existing is not None:
            # 重试、重启及同一请求重送沿用原记录，不重算起点。
            self._state["state_syncs"] = {
                int(existing[0]): row_facts(
                    connection, "state_syncs", int(existing[0]))}
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=SyncSaved(disposition=SyncDisposition.ALREADY,
                                 sync_id=int(existing[0])))
        _require_startable(action)
        # 动作开始事实与计划状态同事务保存：曾有动作开始且未全部
        # 终态时计划为执行中（计划执行状态规格的完整分区）。
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError(f"计划不存在: {action['plan_id']}")
        self._state.setdefault("plans", {})[plan["id"]] = plan
        anchor = None
        if self._mode is SyncMode.INCREMENTAL:
            anchor = row_facts(connection, "reports", self._after_report_id)
        if anchor is not None:
            self._state["reports"][anchor["id"]] = anchor
        # 正常分支本动作进入执行中，计划至多首次开始；锚点缺失分
        # 支本动作直接终态，计划可能随之完成。
        plan_terminal = False
        next_plan_status = None
        if self._mode is SyncMode.INCREMENTAL and anchor is None:
            siblings = connection.execute(
                "SELECT id, status FROM actions WHERE plan_id = ?",
                (action["plan_id"],)).fetchall()
            plan_terminal = all(
                status in _ACTION_TERMINAL or int(row_id) == self._action_id
                for row_id, status in siblings)
            next_plan_status = (
                _PLAN_COMPLETE if plan_terminal else 2
                if plan["status"] in (1, 2) else None)
        elif plan["status"] == 1:
            next_plan_status = 2
        allocation = scope.allocate(
            3 if next_plan_status is not None else 2)
        start = self._action_started_event(allocation)
        if self._mode is SyncMode.INCREMENTAL and anchor is None:
            return self._anchor_missing_plan(allocation, start, plan,
                                             next_plan_status)
        from_wm = anchor["to_wm"] if anchor is not None else 0
        events = [start]
        if next_plan_status is not None:
            events.append(self._plan_change_event(
                allocation, plan, next_plan_status))
        values = {
            "action_id": self._action_id,
            "mode": self._mode.value,
            "after_report_id": self._after_report_id,
            "from_wm": from_wm,
            "started_boundary_event_id": allocation.last_event_id,
            "status": 1,
            "local_report_id": None,
            "ack_report_id": None,
            "ended_event_id": None,
        }
        sync_id = next_row_id(connection, "state_syncs")
        self._state["state_syncs"][sync_id] = dict(values, id=sync_id)
        self._owners[("state_syncs", sync_id)] = ("state_sync", sync_id)
        create = _envelope(
            allocation.last_event_id, allocation.txn_id,
            _SYNC_EVENT_ID, 1,
            (row_change("state_syncs", sync_id, values),), self._occurred_at)
        return CommandPlan(
            events=(*events, create), owners=self._owners,
            state_rows=self._state,
            result=SyncSaved(disposition=SyncDisposition.SAVED,
                             sync_id=sync_id))

    def _plan_change_event(self, allocation, plan, next_status: int):
        """计划状态变化事件：执行中（START）或全部终态（COMPLETE）。"""
        reason = 2 if next_status == _PLAN_COMPLETE else 1
        self._owners[("plans", plan["id"])] = ("plan", plan["id"])
        update = update_change(
            "plans", plan["id"],
            {"status": plan["status"]}, {"status": next_status})
        return _envelope(
            allocation.first_event_id + 1, allocation.txn_id,
            _PLAN_STATUS_EVENT, reason, (update,), self._occurred_at)

    def _action_started_event(self, allocation):
        self._owners[("actions", self._action_id)] = (
            "action", self._action_id)
        update = update_change(
            "actions", self._action_id,
            {"status": 1, "execution_started": 0},
            {"status": 2, "execution_started": 1})
        return _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTION_STARTED_EVENT_ID, 1, (update,), self._occurred_at)

    def _anchor_missing_plan(self, allocation, start, plan, next_plan_status):
        """固定起点不存在：同一事务保存动作开始与执行失败。"""
        code = "sync_report_not_found"
        details = {"after_report_id": str(self._after_report_id)}
        validate_error_details(code, details)
        fail = update_change(
            "actions", self._action_id,
            {"status": 2, "error_code": None, "error_details_json": None},
            {"status": 4, "error_code": registered_error(code)["action_error_id"],
             "error_details_json": details})
        event = _envelope(
            allocation.last_event_id, allocation.txn_id,
            _ACTION_FINISHED_EVENT_ID, 2, (fail,), self._occurred_at)
        # 本动作在该事务内直接终态；计划随之首次开始或完成。
        events = [start]
        if next_plan_status is not None:
            events.append(self._plan_change_event(
                allocation, plan, next_plan_status))
        events.append(event)
        return CommandPlan(
            events=tuple(events), owners=self._owners,
            state_rows=self._state,
            result=SyncSaved(disposition=SyncDisposition.FAILED,
                             sync_id=None))


class _LocalReportCommand:
    """本地报告满足：同一事务填写本地报告并保存动作成功。"""

    def __init__(self, key, action_id, local_report_id, occurred_at) -> None:
        self._key = key
        self._action_id = action_id
        self._local_report_id = local_report_id
        self._occurred_at = occurred_at
        self._owners: dict = {}
        self._state: dict = {"actions": {}, "state_syncs": {}, "reports": {}}

    def plan(self, scope):
        connection = scope.connection
        sync = _load_sync(connection, self._state, self._action_id)
        if sync["local_report_id"] == self._local_report_id:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=SyncSaved(disposition=SyncDisposition.ALREADY,
                                 sync_id=sync["id"]))
        if sync["local_report_id"] is not None:
            raise TransactionError("本地报告已确定，不改写为另一份报告")
        report = row_facts(connection, "reports", self._local_report_id)
        if report is None:
            raise ConsistencyError(f"本地报告不存在: {self._local_report_id}")
        if ReportStatus(report["status"]) is not ReportStatus.PUBLISHED:
            raise TransactionError(
                f"本地报告满足要求报告已经发布: {self._local_report_id}")
        self._state["reports"][report["id"]] = report
        action = _load_action(connection, self._state, self._action_id)
        if (action["status"] != 2 or action["execution_started"] != 1
                or action["cancel_requested"] != 0):
            raise TransactionError(
                "本地报告满足要求报告动作正在运行且未取消:"
                f" {self._action_id} status={action['status']!r}")
        # 计划内全部动作到达终态时，动作成功与计划完成同事务保存
        # （与拍摄、取回链的终态事务一致，不留下无人推进的计划）。
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError(f"计划不存在: {action['plan_id']}")
        siblings = connection.execute(
            "SELECT id, status FROM actions WHERE plan_id = ?",
            (action["plan_id"],)).fetchall()
        plan_complete = all(
            status in _ACTION_TERMINAL or int(row_id) == self._action_id
            for row_id, status in siblings)
        allocation = scope.allocate(3 if plan_complete else 2)
        self._owners[("state_syncs", sync["id"])] = (
            "state_sync", sync["id"])
        local = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _SYNC_EVENT_ID, 2,
            (update_change(
                "state_syncs", sync["id"],
                {"local_report_id": None},
                {"local_report_id": self._local_report_id}),),
            self._occurred_at)
        self._owners[("actions", self._action_id)] = (
            "action", self._action_id)
        succeed = _envelope(
            allocation.first_event_id + 1, allocation.txn_id,
            _ACTION_FINISHED_EVENT_ID, 1,
            (update_change(
                "actions", self._action_id,
                {"status": 2}, {"status": 3}),),
            self._occurred_at)
        events = [local, succeed]
        if plan_complete and plan["status"] in (1, 2):
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            self._state.setdefault("plans", {})[plan["id"]] = plan
            events.append(_envelope(
                allocation.last_event_id, allocation.txn_id,
                _PLAN_STATUS_EVENT, 2,
                (update_change(
                    "plans", plan["id"],
                    {"status": plan["status"]}, {"status": _PLAN_COMPLETE}),),
                self._occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners,
            state_rows=self._state,
            result=SyncSaved(disposition=SyncDisposition.SAVED,
                             sync_id=sync["id"]))


class _CancelSyncCommand:
    """取消消费者：取消事务中结束本动作的同步责任。"""

    def __init__(self, key, action_id, occurred_at) -> None:
        self._key = key
        self._action_id = action_id
        self._occurred_at = occurred_at
        self._owners: dict = {}
        self._state: dict = {"actions": {}, "state_syncs": {}}

    def plan(self, scope):
        connection = scope.connection
        row = connection.execute(
            "SELECT id FROM state_syncs WHERE action_id = ?",
            (self._action_id,)).fetchone()
        if row is None:
            # 尚未开始的报告动作没有同步责任可结束。
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=SyncSaved(disposition=SyncDisposition.ALREADY,
                                 sync_id=None))
        sync = _load_sync(connection, self._state, self._action_id)
        if sync["status"] in (2, 3):
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=SyncSaved(disposition=SyncDisposition.ALREADY,
                                 sync_id=sync["id"]))
        action = _load_action(connection, self._state, self._action_id)
        if action["status"] in _SYNC_KEEPING_TERMINAL:
            # 成功等待合格 ACK；失败或过期保留责任，由后续报告机会继续。
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=SyncSaved(disposition=SyncDisposition.KEPT,
                                 sync_id=sync["id"]))
        if not action["cancel_requested"]:
            raise TransactionError("同步取消要求动作取消请求已生效")
        allocation = scope.allocate(1)
        self._owners[("state_syncs", sync["id"])] = (
            "state_sync", sync["id"])
        update = update_change(
            "state_syncs", sync["id"],
            {"status": 1, "ended_event_id": None},
            {"status": 3, "ended_event_id": allocation.first_event_id})
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _SYNC_EVENT_ID, 4, (update,),
            self._occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=SyncSaved(disposition=SyncDisposition.SAVED,
                             sync_id=sync["id"]))


class _FinishCanceledSyncCommand:
    """同步已结束后，动作取消终态与适用计划完成共同保存。"""

    def __init__(self, key, action_id, occurred_at) -> None:
        self._key = key
        self._action_id = action_id
        self._occurred_at = occurred_at
        self._owners: dict = {}
        self._state: dict = {"actions": {}, "state_syncs": {}}

    def plan(self, scope):
        connection = scope.connection
        UtcMicros(self._occurred_at)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            kinds = [(event["type"], event["reason"]) for event in saved]
            if (kinds not in ([(_ACTION_FINISHED_EVENT_ID, 4)],
                             [(_ACTION_FINISHED_EVENT_ID, 4), (_PLAN_STATUS_EVENT, 2)])
                    or any(event["occurred_at"] != self._occurred_at for event in saved)):
                raise TransactionError("原操作键不是本次报告取消收场")
            rows = saved[0]["body"]["rows"]
            if (len(rows) != 1 or rows[0]["table"] != "actions"
                    or rows[0]["id"] != self._action_id
                    or rows[0]["after"]["values"] != {"status": 6}):
                raise TransactionError("原报告取消收场属于其他动作")
        action = _load_action(connection, self._state, self._action_id)
        if action["type"] != _REPORT_ACTION_TYPE:
            raise TransactionError("同步取消收场要求报告动作")
        if saved is not None and len(saved) == 2:
            plan_rows = saved[1]["body"]["rows"]
            plan = row_facts(connection, "plans", action["plan_id"])
            if (len(plan_rows) != 1 or plan_rows[0]["table"] != "plans"
                    or plan_rows[0]["id"] != action["plan_id"]
                    or plan_rows[0]["after"]["values"] != {"status": _PLAN_COMPLETE}
                    or plan is None or plan["status"] != _PLAN_COMPLETE):
                raise ConsistencyError("原报告取消收场的计划完成事实未保持")
        if action["status"] in _SYNC_KEEPING_TERMINAL:
            if saved is not None:
                raise ConsistencyError("原报告取消终态未保持")
            return self._already(None, SyncDisposition.KEPT)
        if action["status"] == 6 and action["execution_started"] == 0:
            if not action["cancel_requested"] or saved is not None:
                raise ConsistencyError("报告执行前取消状态与收场事实不符")
            return self._already(None)
        sync = _load_sync(connection, self._state, self._action_id)
        if (action["status"] not in (2, 6) or action["execution_started"] != 1
                or action["cancel_requested"] != 1
                or sync["local_report_id"] is not None
                or sync["status"] not in (int(_SYNC_STATUS.CANCELED), int(_SYNC_STATUS.ACKNOWLEDGED))
                or sync["ended_event_id"] is None):
            raise ConsistencyError("报告取消收场缺少已生效取消与原同步结束事实")
        if action["status"] == 6:
            return self._already(sync["id"])
        if saved is not None:
            raise ConsistencyError("原报告取消收场未保存动作终态")
        plan = row_facts(connection, "plans", action["plan_id"])
        if plan is None:
            raise ConsistencyError("报告取消收场缺少所属计划")
        with closing(connection.execute(
            "SELECT id, status FROM actions WHERE plan_id = ?", (action["plan_id"],),
        )) as cursor:
            siblings = cursor.fetchall()
        complete = (plan["status"] in (1, 2) and all(
            status in _ACTION_TERMINAL or action_id == self._action_id
            for action_id, status in siblings))
        allocation = scope.allocate(1 + int(complete))
        self._owners[("actions", self._action_id)] = ("action", self._action_id)
        events = [_envelope(
            allocation.first_event_id, allocation.txn_id, _ACTION_FINISHED_EVENT_ID, 4,
            (update_change("actions", self._action_id, {"status": 2}, {"status": 6}),),
            self._occurred_at)]
        if complete:
            self._state["plans"] = {plan["id"]: plan}
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            events.append(_envelope(
                allocation.last_event_id, allocation.txn_id, _PLAN_STATUS_EVENT, 2,
                (update_change("plans", plan["id"],
                               {"status": plan["status"]}, {"status": _PLAN_COMPLETE}),),
                self._occurred_at))
        return CommandPlan(
            events=tuple(events), owners=self._owners, state_rows=self._state,
            result=SyncSaved(disposition=SyncDisposition.SAVED, sync_id=sync["id"]))

    def _already(self, sync_id, disposition=SyncDisposition.ALREADY):
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state, read_only=True,
            result=SyncSaved(disposition=disposition, sync_id=sync_id))


class _RecoveredPublicationCommand:
    """恢复时发现已发布：以目录观察证据补齐发布事实。

    证据核对：观察文件自称摘要与首次确定字节一致。PREPARED 的报
    告同事务补发布意图与发布；FAILED 的报告先恢复到发布中再保存
    发布；已发布过的报告不追加事实。
    """

    def __init__(self, key, report_id, observed_sha256, occurred_at) -> None:
        self._key = key
        self._report_id = report_id
        self._observed_sha256 = observed_sha256
        self._occurred_at = occurred_at
        self._owners: dict = {}
        self._state: dict = {"reports": {}}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        facts = read_report_management(connection, self._report_id)
        contents = validate_report_management(facts)
        self._state["reports"][self._report_id] = facts
        if contents is None:
            raise ConsistencyError(
                f"恢复发现要求报告已有确定字节: {self._report_id}")
        if contents.sha256 != self._observed_sha256:
            raise ConsistencyError(
                f"恢复观察与首次确定的报告字节不一致: {self._report_id}")
        self._owners[("reports", self._report_id)] = (
            "report", self._report_id)
        status = ReportStatus(facts["status"])
        if status is ReportStatus.PUBLISHED:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ReportPublication(
                    self._report_id, facts["publication_count"],
                    facts["last_published_event_id"]))

        if status is ReportStatus.PREPARED:
            prefix = (_ReportChange.INTENT,)
        elif status is ReportStatus.PUBLISHING:
            prefix = ()
        elif status is ReportStatus.FAILED:
            prefix = (_ReportChange.RECOVER,)
        else:
            raise ConsistencyError(
                "报告状态不能由恢复观察保存发布: "
                f"{self._report_id} status={status.value!r}")
        allocation = scope.allocate(len(prefix) + 1)
        publish_event_id = allocation.last_event_id
        reasons = (*prefix, _ReportChange.PUBLISH)
        row_groups = []
        for reason in reasons:
            if reason is _ReportChange.PUBLISH:
                row_groups.append((update_change(
                    "reports", self._report_id,
                    {"status": ReportStatus.PUBLISHING,
                     "publication_count": facts["publication_count"],
                     "last_published_event_id": facts["last_published_event_id"],
                     "last_error_json": facts["last_error_json"]},
                    {"status": ReportStatus.PUBLISHED,
                     "publication_count": facts["publication_count"] + 1,
                     "last_published_event_id": publish_event_id,
                     "last_error_json": None}),))
            else:
                row_groups.append((update_change(
                    "reports", self._report_id,
                    {"status": status},
                    {"status": ReportStatus.PUBLISHING}),))
        events = tuple(
            _envelope(event_id, allocation.txn_id, _REPORT_EVENT["id"],
                      reason, rows_of_one, self._occurred_at)
            for event_id, reason, rows_of_one in zip(
                range(allocation.first_event_id, publish_event_id + 1),
                reasons, row_groups)
        )
        return CommandPlan(
            events=events, owners=self._owners, state_rows=self._state,
            result=ReportPublication(
                self._report_id, facts["publication_count"] + 1,
                publish_event_id))


def _load_action(connection, state, action_id):
    action = row_facts(connection, "actions", action_id)
    if action is None:
        raise ConsistencyError(f"动作不存在: {action_id}")
    state["actions"][action_id] = action
    return action


def _load_sync(connection, state, action_id):
    row = connection.execute(
        "SELECT id FROM state_syncs WHERE action_id = ?",
        (action_id,)).fetchone()
    if row is None:
        raise ConsistencyError(f"同步责任不存在: {action_id}")
    sync = row_facts(connection, "state_syncs", int(row[0]))
    state["state_syncs"][sync["id"]] = sync
    return sync


def _sync_guard(event, context) -> None:
    """同步的结束事实引用本事件；ACK 资格由 ACK 守卫负责。"""
    for row in event.rows:
        if row.table != "state_syncs" or row.before.exists is False:
            continue
        if "ended_event_id" in row.after.values:
            if row.after.values["ended_event_id"] != event.event_id:
                raise EventValidationError("同步结束依据必须是本事件")


def _action_start_guard(event, context) -> None:
    """同步责任建立时，发起动作已进入运行且未取消。

    开始事务先写入 ACTION_STARTED 再建立同步责任；回放按事件时点
    状态核对这一顺序。时间与会话资格由运行入口负责，不在事件层
    复验。
    """
    if event.event_type != _SYNC_EVENT_ID:
        return
    for row in event.rows:
        if row.table != "state_syncs" or row.before.exists:
            continue
        action_id = row.after.values.get("action_id")
        facts = context.state_rows.get("actions", {}).get(action_id)
        if facts is None:
            raise EventValidationError(
                f"同步建立缺少动作事实: actions#{action_id!r}")
        if (facts.get("status") != 2 or facts.get("execution_started") != 1
                or facts.get("cancel_requested") != 0):
            raise EventValidationError(
                "同步责任要求发起动作已进入运行且未取消:"
                f" actions#{action_id} status={facts.get('status')!r}")


def register_sync_guard() -> None:
    register_guard("sync", _sync_guard)
    register_guard("action_start", _action_start_guard)
