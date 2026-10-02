"""报告确认所需的权威事实读取；不存在与不可解释分别处理。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing

from camctl.contracts.values import ConsistencyError
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.enums import decode_member, load_registry as load_enum_registry
from camctl.contracts.json_values import parse_exact_json
from camctl.persistence.transaction import row_facts
from camctl.history.events import load_event_registry
from camctl.reporting.ack import AckFacts, AckReport, SyncResponsibility
from camctl.reporting.models import FrozenReport, ReportOpportunity, ReportPublication, ReportStatus, validate_report_management


def _read_one(connection, sql, parameters=()):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def read_ack_state(connection) -> tuple[int, int | None, AckReport | None]:
    row = _read_one(connection,
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state WHERE id = 1"
    )
    if row is None:
        raise ConsistencyError("累计确认记录缺失")
    AckFacts(row[0], row[1])
    report = read_ack_report(connection, row[1]) if row[1] is not None else None
    if row[1] is not None and (report is None or report.to_wm != row[0]):
        raise ConsistencyError("累计确认位置与其报告依据不一致")
    return row[0], row[1], report


def read_ack_report(connection, report_id: int) -> AckReport | None:
    row = _read_one(connection,
        "SELECT from_wm, to_wm, frozen_event_id, format_version, created_event_id"
        " FROM reports WHERE id = ?", (report_id,),
    )
    if row is None:
        return None
    report = AckReport(report_id, row[0], row[1], row[2])
    if row[3] != 1 or report.frozen_event_id >= row[4]:
        raise ConsistencyError("ACK 报告的固定生成依据无效")
    if report.frozen_event_id != 0 and _read_one(connection,
        "SELECT id FROM history_transactions WHERE last_event_id = ?",
        (report.frozen_event_id,),
    ) is None:
        raise ConsistencyError("ACK 报告引用的冻结位置不是完整历史边界")
    latest = _read_one(connection,
        "SELECT MAX(change_seq) FROM history_events WHERE id <= ?",
        (report.frozen_event_id,),
    )[0]
    latest = 0 if latest is None else latest
    if report.to_wm != latest:
        raise ConsistencyError("ACK 报告的覆盖终点与冻结历史不一致")
    if report.from_wm != 0 and _read_one(connection,
        "SELECT event.id FROM history_events AS event"
        " JOIN history_transactions AS txn ON txn.id = event.transaction_id"
        " WHERE event.change_seq = ? AND txn.last_event_id <= ?"
        " AND NOT EXISTS (SELECT 1 FROM history_events AS later"
        " WHERE later.transaction_id = event.transaction_id AND later.change_seq > event.change_seq)",
        (report.from_wm, report.frozen_event_id),
    ) is None:
        raise ConsistencyError("报告覆盖起点必须位于完整业务事务边界")
    return report


def read_outstanding_syncs(connection) -> Generator[tuple[SyncResponsibility, dict], None, None]:
    cursor = connection.execute(
        "SELECT id, action_id, from_wm, started_boundary_event_id, status,"
        " ack_report_id, ended_event_id, mode, after_report_id"
        " FROM state_syncs WHERE status = 1 ORDER BY id"
    )
    try:
        for row in cursor:
            sync = SyncResponsibility(row[0], row[1], row[2], row[3])
            if row[5] is not None or row[6] is not None:
                raise ConsistencyError("未结束同步不能携带结束依据")
            if _read_one(connection,
                "SELECT id FROM history_transactions WHERE last_event_id = ?",
                (sync.started_boundary_event_id,),
            ) is None:
                raise ConsistencyError("同步开始位置不是完整历史边界")
            if row[7] == 2:
                origin = read_ack_report(connection, row[8])
                if origin is None or origin.to_wm != sync.from_wm:
                    raise ConsistencyError("局部同步固定起点与其报告依据不一致")
                created = _read_one(connection,
                    "SELECT created_event_id FROM reports WHERE id = ?", (row[8],),
                )
                if created is None or created[0] > sync.started_boundary_event_id:
                    raise ConsistencyError("局部同步起点报告在开始边界尚未登记")
            values = dict(zip(("id", "action_id", "from_wm", "started_boundary_event_id",
                               "status", "ack_report_id", "ended_event_id", "mode", "after_report_id"), row))
            yield sync, values
    finally:
        cursor.close()


def read_report_opportunity(connection) -> ReportOpportunity:
    """在调用方的同一事务中按流取得全部有效同步，汇总范围与历史要求。"""
    row = _read_one(connection,
        "SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
    )
    boundary = INITIAL_BOUNDARY if row is None else HistoryBoundary(row[0], row[1])
    latest = _read_one(connection,
        "SELECT MAX(change_seq) FROM history_events WHERE id <= ?", (boundary.last_event_id,),
    )[0]
    latest = 0 if latest is None else latest
    acknowledged, _, _ = read_ack_state(connection)
    sync_from = None
    sync_boundary = None
    with closing(read_outstanding_syncs(connection)) as syncs:
        for sync, _ in syncs:
            if sync.from_wm > latest or sync.started_boundary_event_id > boundary.last_event_id:
                raise ConsistencyError("同步固定定义超出本次冻结历史")
            sync_from = sync.from_wm if sync_from is None else min(sync_from, sync.from_wm)
            sync_boundary = (sync.started_boundary_event_id if sync_boundary is None
                             else max(sync_boundary, sync.started_boundary_event_id))
    return ReportOpportunity(boundary, latest, acknowledged, sync_from, sync_boundary)


def read_covering_report(connection, opportunity: ReportOpportunity, from_wm: int) -> AckReport | None:
    """只取一份同时覆盖普通范围和全部同步开始历史的候选，再校验其依据。"""
    started = opportunity.sync_started_boundary_event_id or 0
    row = _read_one(connection,
        "SELECT id FROM reports WHERE from_wm <= ? AND to_wm >= ? AND frozen_event_id >= ?"
        " ORDER BY frozen_event_id DESC, id LIMIT 1",
        (from_wm, opportunity.latest_change_wm, started),
    )
    if row is None:
        return None
    report = read_ack_report(connection, row[0])
    if report is None:
        raise ConsistencyError("已选报告的固定依据缺失")
    return report


def read_frozen_report(connection, report: AckReport) -> FrozenReport:
    """复用原冻结依据，不能换成本次事务的较新边界。"""
    if report.frozen_event_id == 0:
        boundary = INITIAL_BOUNDARY
    else:
        row = _read_one(connection,
            "SELECT id, last_event_id FROM history_transactions WHERE last_event_id = ?",
            (report.frozen_event_id,),
        )
        if row is None:
            raise ConsistencyError("已选报告的完整冻结事务缺失")
        boundary = HistoryBoundary(row[0], row[1])
    return FrozenReport(report.report_id, boundary, report.from_wm, report.to_wm, 1)


def read_report_management(connection, report_id: int) -> dict:
    """在写事务内取得可靠固定依据与完整管理事实，JSON 保持精确值。"""
    if read_ack_report(connection, report_id) is None:
        raise ConsistencyError(f"报告 {report_id} 不存在")
    facts = row_facts(connection, "reports", report_id)
    if facts is None:
        raise ConsistencyError("报告管理记录缺失")
    validate_report_management(facts)
    definition = load_event_registry()["events"]["REPORT_CHANGED"]
    published = _read_one(connection,
        "SELECT event.id, event.event_type, event.event_version, event.body_json"
        " FROM entity_event_links AS link JOIN history_events AS event ON event.id = link.event_id"
        " WHERE link.entity_type = ? AND link.entity_id = ? AND event.event_type = ?"
        " AND json_extract(event.body_json, '$.reason') = ? ORDER BY link.event_id DESC LIMIT 1",
        (load_enum_registry()["history_objects"]["report"]["id"], report_id,
         definition["id"], definition["branches"]["PUBLISH"]["reason"]),
    )
    if (published is None) != (facts["last_published_event_id"] is None):
        raise ConsistencyError("报告成功投影与实际发布历史不一致")
    if published is not None:
        if (published[0] != facts["last_published_event_id"] or published[1] != definition["id"] or published[2] != 1
                or facts["last_published_event_id"] > facts["last_event_id"]):
            raise ConsistencyError("报告成功引用没有可靠的原发布事件")
        body = parse_exact_json(published[3])
        if not isinstance(body, dict) or body.get("reason") != definition["branches"]["PUBLISH"]["reason"]:
            raise ConsistencyError("报告成功引用不是可靠发布分支")
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise ConsistencyError("报告成功事件缺少行事实")
        own = [row for row in rows if row.get("table") == "reports" and row.get("id") == report_id]
        if len(own) != 1:
            raise ConsistencyError("报告成功事件不属于该报告")
        before, after = own[0].get("before", {}), own[0].get("after", {})
        old, new = before.get("values", {}), after.get("values", {})
        recorded = ReportPublication(own[0]["id"], new.get("publication_count"), new.get("last_published_event_id"))
        count = old.get("publication_count")
        if (before.get("exists") is not True or after.get("exists") is not True
                or isinstance(count, bool) or not isinstance(count, int) or count < 0
                or decode_member("reports.status", old.get("status")) != ReportStatus.PUBLISHING
                or decode_member("reports.status", new.get("status")) != ReportStatus.PUBLISHED
                or recorded.publication_count != facts["publication_count"]
                or count + 1 != facts["publication_count"]
                or recorded.published_event_id != facts["last_published_event_id"]):
            raise ConsistencyError("报告成功次数与原发布事件不一致")
    return facts
