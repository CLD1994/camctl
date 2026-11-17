"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.history.validators import EventValidationError
from camctl.outputs.batches import BatchOutcome, decide_batch_grant
from camctl.persistence.repositories.file_batch_reads import FileBatchReads
from camctl.persistence.repositories.transactions import row_facts


def _batch_guard(event, context):
    if len(event.rows) != 1 or event.rows[0].table != "device_file_batches":
        raise EventValidationError("文件批次事件必须只改变一条批次")
    reads = context.file_batch_reads
    if type(reads) is not FileBatchReads:
        raise EventValidationError("文件批次缺少事务原事实读取")
    row = event.rows[0]
    if not row.before.exists:
        value = row.after.values
        action_id, device_id = value["action_id"], value["device_id"]
        if (value["acquired_event_id"] != event.event_id or value["released_event_id"] is not None):
            raise EventValidationError("文件批次取得违反完整资格、优先级或设备互斥")
        holder = reads.holder(device_id)
        ready = holder is None and reads.ready(action_id, device_id, event.occurred_at) and not reads.busy(action_id, device_id, event.occurred_at)
        first = reads.first(device_id, event.occurred_at) if holder is None and ready else None
        if (holder is not None or decide_batch_grant(action_id=action_id, holder_action_id=None,
                ready=ready, first_action_id=first) is not BatchOutcome.GRANTED):
            raise EventValidationError("文件批次取得违反完整资格、优先级或设备互斥")
    else:
        original = row_facts(reads.connection, "device_file_batches", row.row_id)
        if (original is None or original["released_event_id"] is not None
                or row.after.values != {"released_event_id": event.event_id}
                or not reads.finished(original["action_id"], original["device_id"])):
            raise EventValidationError("文件批次尚未可靠结束，不能释放")


def register_file_batch_guards(register):
    register("file_batch", _batch_guard)
