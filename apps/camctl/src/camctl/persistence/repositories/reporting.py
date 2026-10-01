"""报告确认所需的权威事实读取；不存在与不可解释分别处理。"""

from __future__ import annotations

from collections.abc import Iterator

from camctl.contracts.values import ConsistencyError
from camctl.reporting.ack import AckFacts, AckReport, SyncResponsibility


def read_ack_state(connection) -> tuple[int, int | None, AckReport | None]:
    row = connection.execute(
        "SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state WHERE id = 1"
    ).fetchone()
    if row is None:
        raise ConsistencyError("累计确认记录缺失")
    AckFacts(row[0], row[1])
    report = read_ack_report(connection, row[1]) if row[1] is not None else None
    if row[1] is not None and (report is None or report.to_wm != row[0]):
        raise ConsistencyError("累计确认位置与其报告依据不一致")
    return row[0], row[1], report


def read_ack_report(connection, report_id: int) -> AckReport | None:
    row = connection.execute(
        "SELECT from_wm, to_wm, frozen_event_id, format_version, created_event_id"
        " FROM reports WHERE id = ?", (report_id,),
    ).fetchone()
    if row is None:
        return None
    report = AckReport(report_id, row[0], row[1], row[2])
    if row[3] != 1 or report.frozen_event_id >= row[4]:
        raise ConsistencyError("ACK 报告的固定生成依据无效")
    if report.frozen_event_id != 0 and connection.execute(
        "SELECT id FROM history_transactions WHERE last_event_id = ?",
        (report.frozen_event_id,),
    ).fetchone() is None:
        raise ConsistencyError("ACK 报告引用的冻结位置不是完整历史边界")
    latest = connection.execute(
        "SELECT MAX(change_seq) FROM history_events WHERE id <= ?",
        (report.frozen_event_id,),
    ).fetchone()[0]
    latest = 0 if latest is None else latest
    if report.to_wm != latest:
        raise ConsistencyError("ACK 报告的覆盖终点与冻结历史不一致")
    return report


def read_outstanding_syncs(connection) -> Iterator[tuple[SyncResponsibility, dict]]:
    cursor = connection.execute(
        "SELECT id, action_id, from_wm, started_boundary_event_id, status,"
        " ack_report_id, ended_event_id FROM state_syncs WHERE status = 1 ORDER BY id"
    )
    try:
        for row in cursor:
            sync = SyncResponsibility(row[0], row[1], row[2], row[3])
            if row[5] is not None or row[6] is not None:
                raise ConsistencyError("未结束同步不能携带结束依据")
            if connection.execute(
                "SELECT id FROM history_transactions WHERE last_event_id = ?",
                (sync.started_boundary_event_id,),
            ).fetchone() is None:
                raise ConsistencyError("同步开始位置不是完整历史边界")
            values = dict(zip(("id", "action_id", "from_wm", "started_boundary_event_id",
                               "status", "ack_report_id", "ended_event_id"), row))
            yield sync, values
    finally:
        cursor.close()
