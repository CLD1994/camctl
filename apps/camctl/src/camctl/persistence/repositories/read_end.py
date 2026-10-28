"""当前轮实际读取结束的原事务事实与保存。"""

from contextlib import closing
from dataclasses import dataclass

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.devices.read_session import ReadEnd
from camctl.history.events import reason_for_branch
from camctl.history.validators import EventValidationError
from camctl.persistence.transaction import (CommandPlan, commit_operation, event_envelope,
    row_facts, saved_transaction_events, update_change)


@dataclass(frozen=True)
class SaveReadEnd:
    ticket: object
    copy_round: int
    source_device_file_id: int | None
    source_intermediate_file_id: int | None
    target_file_id: int
    source_size: int
    end: ReadEnd
    occurred_at: int

    def __post_init__(self):
        from camctl.operations.models import AttemptTicket
        if not isinstance(self.ticket, AttemptTicket) or self.ticket.operation != "read":
            raise ValueError("读取结束必须携带原 READ 票据")
        ObjectId(self.copy_round)
        ObjectId(self.target_file_id)
        UtcMicros(self.occurred_at)
        if ((self.source_device_file_id is None) == (self.source_intermediate_file_id is None)
                or type(self.source_size) is not int or self.source_size < 0
                or not isinstance(self.end, ReadEnd) or self.end.stopped is not True
                or self.end.error is not None or type(self.end.bytes_read) is not int
                or self.end.bytes_read < 0 or self.end.bytes_read > self.source_size):
            raise ValueError("读取结束申请缺少完整实际结束事实")

    def document(self):
        ticket = self.ticket
        return {"ticket": {"run_id": ticket.run_id, "attempt_no": ticket.attempt_id,
                "operation": ticket.operation, "target_id": ticket.target_id,
                "responsibility_key": ticket.responsibility_key},
                "copy_round": self.copy_round, "source_device_file_id": self.source_device_file_id,
                "source_intermediate_file_id": self.source_intermediate_file_id,
                "target_file_id": self.target_file_id, "source_size": self.source_size,
                "committed_bytes": self.source_size, "reset_state": 1,
                "end": {"stopped": self.end.stopped, "bytes_read": self.end.bytes_read, "error": self.end.error}}


class ReadEndReads:
    def __init__(self, connection):
        self.connection = connection

    def verify(self, copy, document, event_id, *, verify_chain=True):
        from .outputs import _copy_round_chain_proven
        if (not isinstance(document, dict) or set(document) != {
                "ticket", "copy_round", "source_device_file_id", "source_intermediate_file_id",
                "target_file_id", "source_size", "committed_bytes", "reset_state", "end"}):
            raise ConsistencyError("读取结束正文的字段不完整")
        ticket = document.get("ticket", {})
        if (not isinstance(ticket, dict) or set(ticket) != {
                "run_id", "attempt_no", "operation", "target_id", "responsibility_key"}
                or type(ticket["run_id"]) is not int or ticket["run_id"] <= 0
                or type(ticket["attempt_no"]) is not int or ticket["attempt_no"] <= 0
                or type(document["copy_round"]) is not int or document["copy_round"] <= 0
                or type(document["target_file_id"]) is not int or document["target_file_id"] <= 0
                or type(document["source_size"]) is not int or document["source_size"] < 0
                or type(document["committed_bytes"]) is not int
                or type(document["reset_state"]) is not int
                or ((document["source_device_file_id"] is None) == (document["source_intermediate_file_id"] is None))
                or any(type(value) is not int or value <= 0 for value in (
                    document["source_device_file_id"], document["source_intermediate_file_id"]) if value is not None)):
            raise ConsistencyError("读取结束正文的身份或进度类型无效")
        run = row_facts(self.connection, "operation_runs", ticket.get("run_id"))
        if (run is None or run.get("kind") != int(enum_for("operation_runs.kind").READ_FILE)
                or run.get("copy_id") != copy["id"] or run.get("responsibility_key") != f"read/{copy['id']}"
                or ticket.get("operation") != "read" or ticket.get("target_id") != str(copy["id"])
                or ticket.get("responsibility_key") != run["responsibility_key"]
                or ticket.get("attempt_no") != run["attempts_used"]):
            raise ConsistencyError("读取结束与原票据或流程不一致")
        from .outputs import _copy_action_id, _verify_source_identity
        state = {"file_copies": {copy["id"]: copy}}
        if (run.get("action_id") != _copy_action_id(self.connection, copy, state)
                or run.get("delivery_id") != copy["delivery_id"]):
            raise ConsistencyError("读取结束与拷贝所属责任不一致")
        _verify_source_identity(self.connection, copy)
        if copy["source_device_file_id"] is None:
            if copy["slot_device_id"] is not None:
                raise ConsistencyError("主机源读取不能持有相机机会")
        else:
            source = row_facts(self.connection, "device_files", copy["source_device_file_id"])
            observer = None if source is None else row_facts(self.connection, "actions", source["observer_action_id"])
            origin = None if source is None else row_facts(self.connection, "actions", source["source_action_id"])
            if (observer is None or origin is None or not observer.get("device_id")
                    or not observer.get("driver_id")
                    or (observer["device_id"], observer["driver_id"]) != (origin["device_id"], origin["driver_id"])
                    or (verify_chain and copy["slot_device_id"] != observer["device_id"])
                    or (copy["slot_device_id"] is not None and copy["slot_device_id"] != observer["device_id"])):
                raise ConsistencyError("读取结束缺少原来源设备的相机机会")
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=? LIMIT 2",
            (run["id"], run["attempts_used"]))) as cursor:
            rows = cursor.fetchmany(2)
        if len(rows) != 1:
            raise ConsistencyError("读取结束缺少唯一原尝试")
        attempt = row_facts(self.connection, "operation_attempts", rows[0][0])
        if (attempt is None or attempt["status"] != int(enum_for("operation_attempts.status").RUNNING)
                or attempt["result_event_id"] is not None or attempt["intent_event_id"] is None):
            raise ConsistencyError("读取结束的原尝试已经结束或意图缺失")
        from .retry_wait_reads import RetryWaitReads
        reads = RetryWaitReads(self.connection)
        reads._require_index("operation_attempts_run_status")
        reads._require_index("operation_attempts_without_result")
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND status=? AND id<>? LIMIT 1",
            (run["id"], int(enum_for("operation_attempts.status").RUNNING), attempt["id"]))) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("读取结束时其他尝试仍在执行")
        with closing(self.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND result_json IS NULL AND id<>? LIMIT 1",
            (run["id"], attempt["id"]))) as cursor:
            if cursor.fetchone() is not None:
                raise ConsistencyError("读取结束时其他尝试结果尚未交付")
        if (document.get("copy_round") != copy["round"]
                or document.get("source_device_file_id") != copy["source_device_file_id"]
                or document.get("source_intermediate_file_id") != copy["source_intermediate_file_id"]
                or document.get("target_file_id") != copy["target_file_id"]
                or document.get("source_size") != copy["source_size"]
                or document.get("committed_bytes") != copy["committed_bytes"]
                or document.get("reset_state") != copy["reset_state"]
                or copy["committed_bytes"] != copy["source_size"] or copy["reset_state"] != 1):
            raise ConsistencyError("读取结束与当前轮次、来源或可靠进度不一致")
        end = document.get("end")
        if (not isinstance(end, dict) or set(end) != {"stopped", "bytes_read", "error"}
                or end["stopped"] is not True or end["error"] is not None
                or type(end["bytes_read"]) is not int or not 0 <= end["bytes_read"] <= copy["source_size"]):
            raise ConsistencyError("读取结束缺少真实完整停止结果")
        if verify_chain and attempt["copy_round"] != copy["round"] and not _copy_round_chain_proven(
                self.connection, copy["id"], attempt["copy_round"], copy["round"],
                attempt["intent_event_id"], event_id, copy["source_size"]):
            raise ConsistencyError("读取结束缺少同一原尝试内的连续重拷关系")
        return attempt

    def local_ready(self, copy):
        if copy["source_intermediate_file_id"] is not None:
            return True
        source = row_facts(self.connection, "device_files", copy["source_device_file_id"])
        if source is None:
            raise ConsistencyError("读取结束缺少原设备文件")
        return copy["source_sha256"] is not None or source["checksum_support"] == int(
            enum_for("device_files.checksum_support").UNSUPPORTED)


def read_end_guard(event, context):
    if len(event.rows) != 1 or event.rows[0].table != "file_copies":
        raise EventValidationError("读取结束必须作用于唯一拷贝")
    row = event.rows[0]
    copy = context.state_rows.get("file_copies", {}).get(row.row_id)
    reads = context.read_end_reads
    if type(reads) is not ReadEndReads or copy is None:
        raise EventValidationError("读取结束缺少原事务事实")
    try:
        if (copy["read_end_event_id"] is not None or row.before.values.get("read_end_event_id") is not None
                or row.after.values.get("read_end_event_id") != event.event_id):
            raise ConsistencyError("读取结束引用不属于当前事件")
        reads.verify(copy, event.evidence["read_end"], event.event_id)
        slot = copy["slot_device_id"]
        if set(row.before.values) != set(row.after.values) or set(row.after.values) - {"read_end_event_id", "slot_device_id"}:
            raise ConsistencyError("读取结束事件包含无关变化")
        if slot is not None:
            released = row.after.values.get("slot_device_id", slot) is None
            if ("slot_device_id" in row.after.values
                    and row.before.values["slot_device_id"] != slot):
                raise ConsistencyError("读取结束不能改变其他机会")
            if released != reads.local_ready(copy):
                raise ConsistencyError("读取结束机会释放与源摘要责任不一致")
        elif "slot_device_id" in row.after.values:
            raise ConsistencyError("读取结束不能建立相机机会")
    except (KeyError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


class SaveReadEndCommand:
    def __init__(self, request, key):
        if not isinstance(request, SaveReadEnd):
            raise TypeError("读取结束必须使用完整申请")
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        from .outputs import _copy_owner_of
        copy_id = int(self.request.ticket.target_id)
        saved = saved_transaction_events(scope.connection, self.key)
        reason = reason_for_branch("COPY_CHANGED", "READ_END")
        if saved is not None:
            if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"]) != (22, reason)
                    or saved[0]["occurred_at"] != self.request.occurred_at
                    or not json_equal(saved[0]["body"]["evidence"], {"read_end": self.request.document()})
                    or len(saved[0]["body"]["rows"]) != 1
                    or saved[0]["body"]["rows"][0]["id"] != copy_id
                    or saved[0]["body"]["rows"][0]["table"] != "file_copies"
                    or saved[0]["body"]["rows"][0]["before"]["values"].get("read_end_event_id", -1) is not None
                    or saved[0]["body"]["rows"][0]["after"]["values"].get("read_end_event_id") != saved[0]["event_id"]):
                raise ConsistencyError("读取结束原键与申请不一致")
            return CommandPlan((), {}, {}, saved[0]["event_id"], read_only=True)
        copy = row_facts(scope.connection, "file_copies", copy_id)
        if copy is None or copy["read_end_event_id"] is not None:
            raise ConsistencyError("当前轮已有读取结束依据或拷贝不存在")
        reads = ReadEndReads(scope.connection)
        allocation = scope.allocate(1)
        reads.verify(copy, self.request.document(), allocation.first_event_id)
        before, after = {"read_end_event_id": None}, {"read_end_event_id": allocation.first_event_id}
        if copy["slot_device_id"] is not None and reads.local_ready(copy):
            before["slot_device_id"], after["slot_device_id"] = copy["slot_device_id"], None
        row = update_change("file_copies", copy_id, before, after)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 22, reason,
                               (row,), self.request.occurred_at, {"read_end": self.request.document()})
        state, owners = {"file_copies": {copy_id: copy}}, {}
        _copy_owner_of(scope.connection, copy, state, owners)
        return CommandPlan((event,), owners, state, allocation.first_event_id)


def save_read_end(request, key, owned):
    from .operations import _outcome_of
    return _outcome_of(commit_operation(SaveReadEndCommand(request, key), key, owned))


def load_read_end(copy_id, owned):
    """按当前引用核对原事件，不重复扫描旧轮次。"""
    from camctl.history.decoding import decode_event_row
    from camctl.history.validators import validate_event_structure
    from camctl.operations.models import AttemptTicket

    copy = row_facts(owned.connection, "file_copies", copy_id)
    if copy is None:
        raise ConsistencyError("读取结束恢复缺少原拷贝")
    event_id = copy["read_end_event_id"]
    if event_id is None:
        return None
    with closing(owned.connection.execute(
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
        " FROM history_events WHERE id=?", (event_id,))) as cursor:
        raw = cursor.fetchone()
    if raw is None:
        raise ConsistencyError("读取结束引用缺少原历史事件")
    event = decode_event_row(raw)
    if (validate_event_structure(event)[:2] != ("COPY_CHANGED", "READ_END")
            or len(event.rows) != 1 or event.rows[0].table != "file_copies"
            or event.rows[0].row_id != copy_id or event.rows[0].after.values.get("read_end_event_id") != event_id):
        raise ConsistencyError("读取结束引用与原历史事件不一致")
    document = event.evidence.get("read_end")
    if not isinstance(document, dict):
        raise ConsistencyError("读取结束历史缺少原返回")
    ReadEndReads(owned.connection).verify(copy, document, event_id, verify_chain=False)
    original = document["ticket"]
    ticket = AttemptTicket(original["attempt_no"], original["operation"], original["target_id"],
                           original["responsibility_key"], original["run_id"])
    return ticket, ReadEnd(**document["end"]), event.occurred_at, copy["slot_device_id"]
