"""已取消录像的内部读取结果与机会收场。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.outputs.read_attempts import run_owned_read, save_read_business, settle_canceled_read
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.repositories.outputs import OutputsRepository, _canceled_binding_processing_settled
from camctl.persistence.transaction import row_facts


def _candidate(owned, action_id):
    connection = owned.connection
    with closing(connection.execute(
        "SELECT c.id,c.target_file_id,c.slot_device_id,r.id FROM actions a"
        " JOIN recording_processing p ON p.action_id=a.id"
        " JOIN file_copies c ON c.processing_id=p.id"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.action_id=a.id AND r.kind=3"
        " WHERE a.id=? AND a.cancel_requested=1 AND a.status IN (2,6)"
        " AND (r.status=? OR (r.status=4 AND json_extract(r.error_json,'$.code')='device_binding_unavailable')) LIMIT 2",
        (action_id, int(enum_for("operation_runs.status").CANCELED)),
    )) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ConsistencyError("取消录像存在多个内部绑定读取责任")
    copy_id, target_id, slot, run_id = rows[0]
    copy = row_facts(connection, "file_copies", copy_id)
    run = row_facts(connection, "operation_runs", run_id)
    action = row_facts(connection, "actions", action_id)
    if copy is None or run is None or action is None:
        raise ConsistencyError("取消录像内部读取归属缺失")
    ready = _canceled_binding_processing_settled(connection, copy, run, action)
    if ready is None:
        if run["status"] != int(enum_for("operation_runs.status").CANCELED):
            raise ConsistencyError("取消录像的原读取资格不可解释")
        from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
        ready = RetryWaitReads(connection).no_unsettled_attempts(run_id)
    return copy_id, target_id, slot, ready


async def settle_canceled_processing_read(runtime, action_id):
    """原读取可靠结束后，保留原事实并释放已取消处理的读取机会。"""
    if (action_id, "capture_binding_failure") in runtime.pending_read_business:
        return False
    def unfinished(owned):
        with closing(owned.connection.execute(
            "SELECT c.id,ob.device_id,ob.driver_id FROM actions a"
            " JOIN recording_processing p ON p.action_id=a.id"
            " JOIN file_copies c ON c.processing_id=p.id"
            " JOIN operation_runs r ON r.copy_id=c.id AND r.action_id=a.id AND r.kind=3"
            " JOIN device_files f ON f.id=c.source_device_file_id"
            " JOIN actions ob ON ob.id=f.observer_action_id"
            " WHERE a.id=? AND a.cancel_requested=1 AND a.status IN (2,6)"
            " AND r.status IN (1,2) LIMIT 2", (action_id,),
        )) as cursor:
            return cursor.fetchall()
    reads = await runtime.database.read(unfinished)
    if len(reads) > 1:
        raise ConsistencyError("取消录像存在多个内部读取责任")
    if reads:
        from camctl.capture.media_flow import _save_internal_read_and_settle
        copy_id, device_id, driver_id = reads[0]
        lookup = runtime.recovery_evidence_for
        evidence = None if lookup is None else lookup(DeviceBinding(device_id, driver_id), "read")
        if not await settle_canceled_read(runtime, copy_id, evidence, _save_internal_read_and_settle):
            return False
    current = await runtime.database.read(lambda owned: _candidate(owned, action_id))
    if current is None:
        return True
    copy_id, target_id, slot, ready = current
    pending = (copy_id, "release_read_slot") in runtime.pending_read_business
    executor = runtime.file_executor
    if not ready or executor is None or target_id in executor.unfinished_files():
        return False
    for result in runtime.pending_read_results.values():
        ticket = getattr(result, "ticket", None)
        if ticket is None:
            ticket = result.finish.ticket
        if int(ticket.target_id) == copy_id:
            return False
    if slot is None and not pending:
        return True

    async def release(_control):
        latest = await runtime.database.read(lambda owned: _candidate(owned, action_id))
        if latest is None or latest[0] != copy_id or not latest[3]:
            return False
        if latest[2] is None and (copy_id, "release_read_slot") not in runtime.pending_read_business:
            return True
        outcome = await save_read_business(runtime, copy_id, "release_read_slot",
            SlotRequest(copy_id, runtime.occurred_at()), OutputsRepository().release_read_slot,
            retry=(copy_id, "release_read_slot") in runtime.pending_read_business)
        return outcome.value.outcome in (SlotOutcome.RELEASED, SlotOutcome.ALREADY_RELEASED)

    return await run_owned_read(runtime, copy_id, release)
