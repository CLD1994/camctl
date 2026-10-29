"""可靠清理结果的纯文件事实交接；不推进原成员终态。"""

from dataclasses import dataclass

from camctl.contracts.values import ObjectId, UtcMicros, OperationKey
from camctl.operations.models import AttemptTicket
from camctl.outputs.cleanup_flow import _read_cleanup


@dataclass(frozen=True)
class HandoffCleanupAbsence:
    """在新交接时刻保存文件事实，并固定原结果及唯一承载身份。"""

    database_id: str
    item_id: int
    ticket: AttemptTicket
    result_event_id: int
    source_key: OperationKey
    output_id: int
    device_file_id: int | None
    intermediate_file_id: int | None
    occurred_at: int

    def __post_init__(self):
        if not isinstance(self.database_id, str) or not self.database_id:
            raise ValueError("缺席交接必须绑定原数据库")
        for value in (self.item_id, self.result_event_id, self.output_id):
            ObjectId(value)
        if (self.device_file_id is None) == (self.intermediate_file_id is None):
            raise ValueError("缺席交接必须绑定唯一承载文件")
        ObjectId(self.device_file_id or self.intermediate_file_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.ticket, AttemptTicket) or not isinstance(
            self.source_key, OperationKey
        ):
            raise TypeError("缺席交接必须持有原票据与事务键")


async def handoff_terminal_absence(runtime, item_id):
    """原保存结果是唯一依据；迟到 raw 返回不参与来源选择。"""
    from camctl.outputs.cleanup_results import saved_cleanup_absence
    from camctl.outputs.cleanup_flow import CleanupStep
    from camctl.outputs.cleanup_ownership import save_cleanup_request
    from camctl.persistence.models import DbOutcomeKind

    if item_id in runtime.pending_companions:
        from camctl.outputs.cleanup_flow import _companion_member_result

        await _companion_member_result(runtime, item_id)
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT c.status,c.output_id,o.device_file_id,o.intermediate_file_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id WHERE c.id=?', (item_id,)).fetchone())
    if row is None or row[0] not in (4, 5, 6):
        return None
    saved = await _read_cleanup(runtime, lambda connection: saved_cleanup_absence(connection, item_id))
    if saved is None:
        return None
    request = HandoffCleanupAbsence(
        runtime.database.metadata.instance_id,
        item_id,
        saved.ticket,
        saved.result_event_id,
        saved.key,
        row[1],
        row[2],
        row[3],
        runtime.occurred_at(),
    )
    result = await save_cleanup_request(runtime, 'handoff_cleanup_absence', request)
    if result.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("result_rejected", str(result.error))
    return None


async def handoff_group_terminal_absence(runtime, output_ids):
    """从共同来源的终态成员接回原可靠缺席，逐页查找。"""
    from camctl.outputs.cleanup_results import saved_cleanup_absence

    after = 0
    while True:
        rows = await _read_cleanup(runtime, lambda connection: connection.execute(
            "SELECT c.id FROM cleanup_items c WHERE c.id>? AND c.status IN (4,5,6)"
            " AND c.output_id IN (%s) AND NOT EXISTS(SELECT 1 FROM operation_runs r"
            " JOIN operation_attempts a ON a.run_id=r.id WHERE r.cleanup_item_id=c.id"
            " AND (a.status=1 OR a.result_event_id IS NULL)) ORDER BY c.id LIMIT ?"
            % ",".join("?" * len(output_ids)),
            (after, *output_ids, runtime.discovery_batch_size)).fetchall())
        if not rows:
            return None
        after = rows[-1][0]
        for (item_id,) in rows:
            saved = await _read_cleanup(runtime, lambda connection: saved_cleanup_absence(connection, item_id))
            if saved is not None:
                return await handoff_terminal_absence(runtime, item_id)
