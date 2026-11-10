"""文件批次的取得、释放及独立事务守卫。"""

from camctl.contracts.values import ConsistencyError
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError
from camctl.outputs.batches import BatchOutcome, decide_batch_grant
from camctl.persistence.repositories.file_batch_reads import FileBatchReads
from camctl.persistence.repositories.transactions import (
    CommandPlan, commit_operation, event_envelope, next_row_id,
    row_change, row_facts, saved_transaction_events, update_change,
)


class _BatchCommand:
    def __init__(self, request, key, release):
        self.request, self.key, self.release = request, key, release

    def plan(self, scope):
        request, connection = self.request, scope.connection
        reads = FileBatchReads(connection, capture_read_parallel_supported=request.capture_read_parallel_supported)
        if not reads.supported():
            raise ConsistencyError("同机文件批次要求 V9；请先执行 init --upgrade-device-file-batches")
        action = reads.action(request.action_id)
        state = {"actions": {request.action_id: action}, "device_file_batches": {}}
        saved = saved_transaction_events(connection, self.key)
        if saved is not None:
            spec = load_event_registry()["events"]["FILE_BATCH_CHANGED"]
            if len(saved) != 1 or saved[0]["type"] != spec["id"]:
                raise ConsistencyError("文件批次原操作键对应其他事务")
            if saved[0]["occurred_at"] != request.occurred_at:
                raise ConsistencyError("文件批次重送改变了原申请时刻")
            row = saved[0]["body"]["rows"][0]
            current = row_facts(connection, "device_file_batches", row["id"])
            if current is None or (current["action_id"], current["device_id"]) != (request.action_id, request.device_id):
                raise ConsistencyError("文件批次原事务身份不一致")
            expected = spec["branches"]["RELEASE" if self.release else "GRANT"]["reason"]
            if saved[0]["reason"] != expected or any(current[k] != v for k,v in row["after"]["values"].items() if k != "released_event_id"):
                raise ConsistencyError("文件批次原事务方向或保存事实不一致")
            if self.release and current["released_event_id"] != row["after"]["values"]["released_event_id"]:
                raise ConsistencyError("文件批次原释放引用不一致")
            return CommandPlan((), {}, state, result=BatchOutcome.RELEASED if self.release else BatchOutcome.GRANTED, read_only=True)
        holder = reads.holder(request.device_id)
        if self.release:
            if holder is None or holder[1] != request.action_id:
                return CommandPlan((), {}, state, result=BatchOutcome.FINISHED, read_only=True)
            current = row_facts(connection, "device_file_batches", holder[0])
            state["device_file_batches"][holder[0]] = current
            if not reads.finished(request.action_id, request.device_id):
                return CommandPlan((), {}, state, result=BatchOutcome.HELD, read_only=True)
            allocation = scope.allocate(1)
            row = update_change("device_file_batches", holder[0], {"released_event_id": None}, {"released_event_id": allocation.first_event_id})
            outcome, branch = BatchOutcome.RELEASED, "RELEASE"
        else:
            prior = connection.execute("SELECT released_event_id FROM device_file_batches WHERE action_id=? AND device_id=?", (request.action_id, request.device_id)).fetchone()
            if prior is not None and prior[0] is not None:
                return CommandPlan((), {}, state, result=BatchOutcome.FINISHED, read_only=True)
            ready = reads.ready(request.action_id, request.device_id, request.occurred_at) and not reads.busy(request.action_id, request.device_id, request.occurred_at)
            outcome = decide_batch_grant(action_id=request.action_id,
                holder_action_id=None if holder is None else holder[1], ready=ready,
                first_action_id=reads.first(request.device_id, request.occurred_at) if holder is None and ready else None)
            if outcome is not BatchOutcome.GRANTED:
                return CommandPlan((), {}, state, result=outcome, read_only=True)
            allocation = scope.allocate(1)
            row = row_change("device_file_batches", next_row_id(connection, "device_file_batches"), {
                "action_id": request.action_id, "device_id": request.device_id,
                "acquired_event_id": allocation.first_event_id, "released_event_id": None})
            branch = "GRANT"
        spec = load_event_registry()["events"]["FILE_BATCH_CHANGED"]
        event = event_envelope(allocation.first_event_id, allocation.txn_id, spec["id"],
            spec["branches"][branch]["reason"], (row,), request.occurred_at)
        return CommandPlan((event,), {("device_file_batches", row.row_id): ("action", request.action_id)}, state, result=outcome)


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


class FileBatchRepository:
    def acquire(self, request, key, owned):
        return self._write(_BatchCommand(request, key, False), key, owned)

    def release(self, request, key, owned):
        return self._write(_BatchCommand(request, key, True), key, owned)

    @staticmethod
    def _write(command, key, owned):
        from camctl.persistence.models import DbOutcome, DbOutcomeKind
        if command.request.capture_read_parallel_supported and not command.release:
            receipt = commit_operation(command, key, owned, storage=_BatchStorage())
        else:
            receipt = commit_operation(command, key, owned)
        if receipt.kind == "completed":
            return DbOutcome(DbOutcomeKind.COMPLETED, receipt.result, boundary=receipt.boundary)
        return DbOutcome(DbOutcomeKind.ROLLED_BACK if receipt.kind == "rolled_back"
            else DbOutcomeKind.UNKNOWN, error=receipt.error)


class _BatchStorage:
    """把本次申请的驱动声明交给事务守卫，其他规则仍由正式装配验证。"""

    def validate_event(self, event, context):
        from dataclasses import replace
        from .validation import default_business_storage
        reads = context.file_batch_reads
        context = replace(context, file_batch_reads=FileBatchReads(
            reads.connection, capture_read_parallel_supported=True))
        return default_business_storage().validate_event(event, context)
