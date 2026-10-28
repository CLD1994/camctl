"""逐分段检查事实与检查输入副本的持久化边界。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.transaction import (
    CommandPlan,
    commit_operation,
    event_envelope,
    next_row_id,
    row_change,
    row_facts,
    saved_transaction_events,
    update_change,
)
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import PathRuleError, validate_relative_file_path
from camctl.outputs.work_files import RetentionRelease, RetentionReleaseOutcome

_EVENT = load_event_registry()["events"]["SEGMENT_INSPECTION"]
_CREATE = _EVENT["branches"]["CREATE"]["reason"]
_RESULT = _EVENT["branches"]["RESULT"]["reason"]
_STATE = enum_for("segment_inspections.state")
_ROLE = enum_for("segment_members.role")
_OWNERSHIP = enum_for("segment_members.ownership")
_MEMBER_COMPLETION = enum_for("segment_members.completion")
_FILE_ROLE = enum_for("device_files.role")
_FILE_COMPLETION = enum_for("device_files.completion_state")
_PRESENCE = enum_for("device_files.presence_state")
_SET_STATE = enum_for("segment_sets.state")
_RETENTION = enum_for("intermediate_files.retention_state")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")
_COPY_VERIFICATION = enum_for("file_copies.verification_state")
_PURPOSE = enum_for("intermediate_files.purpose")


@dataclass(frozen=True, slots=True)
class SegmentInspectionRef:
    inspection_id: int
    action_id: int
    set_id: int
    member_id: int
    source_device_file_id: int
    state: int
    target_json: Mapping[str, Any]
    budget_json: Mapping[str, Any]
    result_json: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class InspectionCopyFacts:
    inspection_id: int
    action_id: int
    set_id: int
    member_id: int
    source_device_file_id: int
    copy_id: int
    target_file_id: int
    input_relative_path: str
    source_size: int
    committed_bytes: int
    source_sha256: str | None
    target_sha256: str | None
    verification_state: int
    inspection_state: int
    retention_state: int
    cleanup_state: int


@dataclass(frozen=True, slots=True)
class InspectionMemberFacts:
    inspection: SegmentInspectionRef | None
    copy: InspectionCopyFacts | None


@dataclass(frozen=True, slots=True)
class InspectionCreate:
    action_id: int
    member_id: int
    target_json: Mapping[str, Any]
    budget_json: Mapping[str, Any]
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        ObjectId(self.member_id)
        if not isinstance(self.target_json, Mapping) or not self.target_json:
            raise ValueError("分段检查目标必须是非空对象")
        if not isinstance(self.budget_json, Mapping) or not self.budget_json:
            raise ValueError("分段检查预算必须是非空对象")
        if type(self.occurred_at) is not int:
            raise ValueError("分段检查时间必须是整数 UTC 微秒")


@dataclass(frozen=True, slots=True)
class InspectionResultSave:
    inspection_id: int
    state: int
    result_json: Mapping[str, Any]
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.inspection_id)
        if self.state not in tuple(int(value) for value in _STATE) or self.state == int(_STATE.PENDING):
            raise ValueError("分段检查结果状态必须是已登记终态")
        if not isinstance(self.result_json, Mapping) or not self.result_json:
            raise ValueError("分段检查结果必须是非空对象")
        if type(self.occurred_at) is not int:
            raise ValueError("分段检查时间必须是整数 UTC 微秒")


def _inspection_ref(row: Mapping[str, Any]) -> SegmentInspectionRef:
    return SegmentInspectionRef(
        inspection_id=row["id"], action_id=row["action_id"], set_id=row["set_id"],
        member_id=row["member_id"], source_device_file_id=row["source_device_file_id"],
        state=row["state"], target_json=row["target_json"], budget_json=row["budget_json"],
        result_json=row["result_json"],
    )


def _one(connection, sql: str, parameters=()):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def inspection_input_read_succeeded(connection, inspection) -> bool:
    """检查副本及其原 READ 均已可靠完成，才能消费媒体检查结果。"""
    from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads

    with closing(connection.execute(
        "SELECT c.source_device_file_id,c.source_size,c.committed_bytes,"
        " c.verification_state,c.target_sha256,r.id,r.action_id,r.status,"
        " r.attempts_used,r.retry_wait_required,t.status,t.result_json,t.result_event_id"
        " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id AND r.kind=?"
        " LEFT JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=r.attempts_used"
        " WHERE c.inspection_id=? LIMIT 2",
        (int(enum_for("operation_runs.kind").READ_FILE), inspection["id"]),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        return False
    source, size, committed, verification, target_hash, run_id, action_id, status, used, waiting, attempt_status, result, event_id = rows[0]
    verified = enum_for("file_copies.verification_state")
    if (source != inspection["source_device_file_id"] or action_id != inspection["action_id"]
            or size is None or committed != size or target_hash is None
            or verification not in (int(verified.MATCHED), int(verified.SOURCE_CHECKSUM_UNAVAILABLE))
            or status != int(enum_for("operation_runs.status").SUCCEEDED)
            or not used or waiting
            or attempt_status != int(enum_for("operation_attempts.status").SUCCEEDED)
            or result is None or event_id is None):
        return False
    return RetryWaitReads(connection).no_unsettled_attempts(run_id)


def _result_requires_read(state, document):
    tool = document.get("tool")
    failure = document.get("failure")
    return (state == int(_STATE.COMPLETED)
            or isinstance(tool, Mapping) and tool.get("ran") is True
            or isinstance(failure, Mapping) and failure.get("code") == "probe_failed")


def _validate_member(connection, request: InspectionCreate) -> tuple[dict, dict, dict, dict]:
    member = row_facts(connection, "segment_members", request.member_id)
    if member is None:
        raise ConsistencyError(f"分段检查成员不存在: {request.member_id}")
    set_row = row_facts(connection, "segment_sets", member["set_id"])
    file_row = row_facts(connection, "device_files", member["file_id"])
    action = row_facts(connection, "actions", request.action_id)
    if set_row is None or file_row is None or action is None:
        raise ConsistencyError("分段检查成员缺少固定集合、设备文件或来源动作")
    if (
        set_row["action_id"] != request.action_id
        or set_row["state"] not in (int(_SET_STATE.MEMBERS_FIXED), int(_SET_STATE.ASSESSED))
        or member["role"] != int(_ROLE.ORIGINAL)
        or member["ownership"] != int(_OWNERSHIP.OWNED)
        or member["completion"] != int(_MEMBER_COMPLETION.COMPLETE)
        or file_row["source_action_id"] != request.action_id
        or file_row["role"] != int(_FILE_ROLE.ORIGINAL)
        or file_row["completion_state"] != int(_FILE_COMPLETION.COMPLETE)
        or file_row["presence_state"] != int(_PRESENCE.PRESENT)
        or file_row["size_bytes"] is None
    ):
        raise ConsistencyError("分段检查要求同一来源动作内已归属、写完的实际原片成员")
    return action, set_row, member, file_row


class _CreateInspectionCommand:
    _TABLES = ("actions", "segment_sets", "segment_members", "device_files", "segment_inspections")

    def __init__(self, request: InspectionCreate, key: OperationKey):
        self.request = request
        self.key = key
        self.owners = {}
        self.state = {table: {} for table in self._TABLES}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = saved_transaction_events(connection, self.key)
        if saved is not None:
            return self._reuse(saved, connection)
        action, set_row, member, file_row = _validate_member(connection, self.request)
        for table, row in (("actions", action), ("segment_sets", set_row),
                           ("segment_members", member), ("device_files", file_row)):
            self.state[table][row["id"]] = row
        with closing(connection.execute(
            "SELECT id FROM segment_inspections WHERE action_id=? AND member_id=? LIMIT 2",
            (self.request.action_id, self.request.member_id),
        )) as cursor:
            existing = cursor.fetchall()
        if len(existing) > 1:
            raise ConsistencyError("同一动作与分段成员存在重复检查责任")
        if existing:
            row = row_facts(connection, "segment_inspections", existing[0][0])
            self._require_request(row)
            self.state["segment_inspections"][row["id"]] = row
            self.owners[("segment_inspections", row["id"])] = ("action", row["action_id"])
            return self._decision(_inspection_ref(row), read_only=True)
        inspection_id = next_row_id(connection, "segment_inspections")
        allocation = scope.allocate(1)
        after = {
            "created_event_id": allocation.first_event_id,
            "action_id": self.request.action_id,
            "set_id": set_row["id"],
            "member_id": member["id"],
            "source_device_file_id": file_row["id"],
            "target_json": dict(self.request.target_json),
            "budget_json": dict(self.request.budget_json),
            "state": int(_STATE.PENDING),
            "result_json": None,
            "result_event_id": None,
        }
        self.owners[("segment_inspections", inspection_id)] = ("action", self.request.action_id)
        self.state["segment_inspections"][inspection_id] = {"id": inspection_id, **after}
        event = event_envelope(allocation.first_event_id, allocation.txn_id,
            _EVENT["id"], _CREATE, (row_change("segment_inspections", inspection_id, after),),
            self.request.occurred_at)
        return CommandPlan(events=(event,), owners=self.owners, state_rows=self.state,
            result=SegmentInspectionRef(inspection_id, self.request.action_id, set_row["id"],
                member["id"], file_row["id"], int(_STATE.PENDING),
                after["target_json"], after["budget_json"]))

    def _require_request(self, row):
        if row is None:
            raise ConsistencyError("分段检查唯一记录缺失")
        if (row["action_id"] != self.request.action_id
                or row["member_id"] != self.request.member_id
                or not json_equal(row["target_json"], dict(self.request.target_json))
                or not json_equal(row["budget_json"], dict(self.request.budget_json))):
            raise ConsistencyError("相同成员的分段检查不能替换来源、目标或原预算")

    def _decision(self, result, *, read_only=False):
        return CommandPlan(events=(), owners=self.owners, state_rows=self.state,
            result=result, read_only=read_only)

    def _reuse(self, saved, connection):
        if (len(saved) != 1 or saved[0]["type"] != _EVENT["id"]
                or saved[0]["reason"] != _CREATE
                or saved[0]["occurred_at"] != self.request.occurred_at):
            raise ConsistencyError("原操作身份不属于分段检查建立阶段")
        row = row_facts(connection, "segment_inspections", saved[0]["body"]["rows"][0]["id"])
        if row is None:
            raise ConsistencyError("已保存分段检查创建事件缺少当前检查行")
        _validate_member(connection, self.request)
        self._require_saved_values(saved[0]["body"]["rows"][0]["after"]["values"])
        return self._decision(_inspection_ref(row), read_only=True)

    def _require_saved_values(self, values):
        if (values.get("action_id") != self.request.action_id
                or values.get("member_id") != self.request.member_id
                or not json_equal(values.get("target_json"), dict(self.request.target_json))
                or not json_equal(values.get("budget_json"), dict(self.request.budget_json))):
            raise ConsistencyError("原分段检查创建申请与重送目标不同")


class _SaveInspectionResultCommand:
    _TABLES = ("actions", "segment_inspections")

    def __init__(self, request: InspectionResultSave, key: OperationKey):
        self.request = request
        self.key = key
        self.owners = {}
        self.state = {table: {} for table in self._TABLES}

    def plan(self, scope):
        connection = scope.connection
        saved = saved_transaction_events(connection, self.key)
        if saved is not None:
            return self._reuse(saved, connection)
        inspection = row_facts(connection, "segment_inspections", self.request.inspection_id)
        if inspection is None:
            raise ConsistencyError(f"分段检查记录不存在: {self.request.inspection_id}")
        action = row_facts(connection, "actions", inspection["action_id"])
        if action is None:
            raise ConsistencyError("分段检查所属动作缺失")
        self.state["actions"][action["id"]] = action
        self.state["segment_inspections"][inspection["id"]] = inspection
        self.owners[("segment_inspections", inspection["id"])] = ("action", action["id"])
        if inspection["state"] != int(_STATE.PENDING):
            if (inspection["state"] == self.request.state
                    and json_equal(inspection["result_json"], dict(self.request.result_json))):
                return self._decision(_inspection_ref(inspection), read_only=True)
            raise ConsistencyError("已结束的分段检查不能替换其结果")
        if (_result_requires_read(self.request.state, self.request.result_json)
                and not inspection_input_read_succeeded(connection, inspection)):
            raise ConsistencyError("媒体检查结果要求原读取流程可靠成功")
        allocation = scope.allocate(1)
        event_id = allocation.first_event_id
        row = update_change("segment_inspections", inspection["id"],
            {"state": inspection["state"], "result_json": inspection["result_json"],
             "result_event_id": inspection["result_event_id"]},
            {"state": self.request.state, "result_json": dict(self.request.result_json),
             "result_event_id": event_id})
        event = event_envelope(event_id, allocation.txn_id, _EVENT["id"], _RESULT,
            (row,), self.request.occurred_at)
        result = {**inspection, "state": self.request.state,
                  "result_json": dict(self.request.result_json), "result_event_id": event_id}
        return CommandPlan(events=(event,), owners=self.owners, state_rows=self.state,
            result=_inspection_ref(result))

    def _decision(self, result, *, read_only=False):
        return CommandPlan(events=(), owners=self.owners, state_rows=self.state,
            result=result, read_only=read_only)

    def _reuse(self, saved, connection):
        if (len(saved) != 1 or saved[0]["type"] != _EVENT["id"]
                or saved[0]["reason"] != _RESULT
                or saved[0]["occurred_at"] != self.request.occurred_at):
            raise ConsistencyError("原操作身份不属于分段检查结果保存阶段")
        rows = saved[0]["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "segment_inspections" \
                or rows[0]["id"] != self.request.inspection_id:
            raise ConsistencyError("原分段检查结果事务身份不同")
        after = rows[0]["after"]["values"]
        if (after.get("state") != self.request.state
                or not json_equal(after.get("result_json"), dict(self.request.result_json))):
            raise ConsistencyError("原分段检查结果申请不同")
        inspection = row_facts(connection, "segment_inspections", self.request.inspection_id)
        if inspection is None:
            raise ConsistencyError("已保存分段检查结果缺少当前检查行")
        return self._decision(_inspection_ref(inspection), read_only=True)


class SegmentInspectionRepository:
    def create(self, request: InspectionCreate, key: OperationKey, owned) -> DbOutcome:
        return _outcome(commit_operation(_CreateInspectionCommand(request, key), key, owned))

    def save_result(self, request: InspectionResultSave, key: OperationKey,
                    owned) -> DbOutcome:
        return _outcome(commit_operation(_SaveInspectionResultCommand(request, key), key, owned))

    def read(self, inspection_id: int, owned) -> SegmentInspectionRef:
        ObjectId(inspection_id)
        row = row_facts(owned.connection, "segment_inspections", inspection_id)
        if row is None:
            raise ConsistencyError(f"分段检查记录不存在: {inspection_id}")
        return _inspection_ref(row)

    def read_for_member(self, action_id: int, member_id: int, owned) -> InspectionMemberFacts:
        return read_inspection_for_member(action_id, member_id, owned)


def read_inspection_copy_facts(inspection_id: int, owned) -> InspectionCopyFacts | None:
    """读取检查责任对应的完整副本与源成员固定身份。"""
    ObjectId(inspection_id)
    connection = owned.connection
    inspection = row_facts(connection, "segment_inspections", inspection_id)
    if inspection is None:
        return None
    with closing(connection.execute(
        "SELECT c.id,c.target_file_id,c.source_size,c.committed_bytes,c.source_sha256,"
        "c.target_sha256,c.verification_state,f.relative_path,f.retention_state,f.cleanup_state,"
        "f.owner_action_id,f.owner_delivery_id,f.purpose,f.size_bytes,f.sha256,"
        "c.delivery_id,c.processing_id,c.source_intermediate_file_id,"
        "m.set_id,m.file_id,m.role,m.ownership,m.completion,s.action_id,s.state,d.size_bytes,d.sha256"
        " FROM file_copies c"
        " JOIN intermediate_files f ON f.id=c.target_file_id"
        " JOIN segment_inspections i ON i.id=c.inspection_id"
        " JOIN segment_members m ON m.id=i.member_id"
        " JOIN segment_sets s ON s.id=i.set_id"
        " JOIN device_files d ON d.id=i.source_device_file_id"
        " WHERE c.inspection_id=? LIMIT 2", (inspection_id,)
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise ConsistencyError("同一分段检查关联了多份读取副本")
    if not rows:
        return None
    (copy_id,target_id,source_size,committed,source_digest,target_digest,verification,path,
     retention,cleanup,owner_action_id,owner_delivery_id,purpose,target_size,target_digest_file,
     delivery_id,processing_id,source_intermediate_id,set_id,source_file,role,ownership,
     completion,action_id,set_state,device_size,device_digest) = rows[0]
    if (set_id != inspection["set_id"] or set_state not in (
                int(_SET_STATE.MEMBERS_FIXED), int(_SET_STATE.ASSESSED))
            or source_file != inspection["source_device_file_id"]
            or action_id != inspection["action_id"] or committed > source_size
            or source_size != device_size or delivery_id is not None or processing_id is not None
            or source_intermediate_id is not None or owner_action_id != inspection["action_id"]
            or owner_delivery_id is not None or purpose != int(_PURPOSE.RECORDING_INPUT)
            or role != int(_ROLE.ORIGINAL) or ownership != int(_OWNERSHIP.OWNED)
            or completion != int(_MEMBER_COMPLETION.COMPLETE)):
        raise ConsistencyError("检查副本与原动作、集合、成员或进度不一致")
    try:
        validate_relative_file_path(FilePurpose.RECORDING_INPUT, target_id, path)
    except PathRuleError as error:
        raise ConsistencyError("检查副本路径与固定输入身份不一致") from error
    if verification in (int(_COPY_VERIFICATION.MATCHED),
                        int(_COPY_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE)):
        if (committed != source_size or target_size != source_size
                or target_digest_file != target_digest or target_digest is None):
            raise ConsistencyError("已通过校验的检查副本缺少完整目标字节事实")
    return InspectionCopyFacts(
        inspection_id, action_id, set_id, inspection["member_id"], source_file,
        copy_id, target_id, path, source_size, committed, source_digest, target_digest,
        verification, inspection["state"], retention, cleanup)


def read_inspection_for_member(action_id: int, member_id: int, owned) -> InspectionMemberFacts:
    """按来源动作与固定成员读取唯一检查事实及其关联副本。"""
    ObjectId(action_id)
    ObjectId(member_id)
    connection = owned.connection
    member = row_facts(connection, "segment_members", member_id)
    if member is None:
        raise ConsistencyError(f"分段成员不存在: {member_id}")
    set_row = row_facts(connection, "segment_sets", member["set_id"])
    if set_row is None or set_row["action_id"] != action_id:
        raise ConsistencyError("检查查询的动作与分段成员所属集合不一致")
    if (set_row["state"] not in (int(_SET_STATE.MEMBERS_FIXED), int(_SET_STATE.ASSESSED))
            or member["role"] != int(_ROLE.ORIGINAL)
            or member["ownership"] != int(_OWNERSHIP.OWNED)
            or member["completion"] != int(_MEMBER_COMPLETION.COMPLETE)):
        raise ConsistencyError("检查查询必须定位已固定集合中的实际完整原片成员")
    with closing(connection.execute(
        "SELECT id FROM segment_inspections WHERE action_id=? AND member_id=? LIMIT 2",
        (action_id, member_id),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise ConsistencyError("同一来源成员存在重复检查责任")
    if not rows:
        return InspectionMemberFacts(None, None)
    inspection = SegmentInspectionRepository().read(rows[0][0], owned)
    if (inspection.action_id != action_id or inspection.member_id != member_id
            or inspection.set_id != set_row["id"]
            or inspection.source_device_file_id != member["file_id"]):
        raise ConsistencyError("分段检查固定身份与真实成员关系不一致")
    return InspectionMemberFacts(inspection,
        read_inspection_copy_facts(inspection.inspection_id, owned))


def release_inspection_copy_pin(inspection_id: int, occurred_at: int,
                                key: OperationKey, owned) -> DbOutcome[RetentionReleaseOutcome]:
    """V7 在来源及取回责任收场后显式释放检查副本保留状态。"""
    facts = read_inspection_copy_facts(inspection_id, owned)
    if facts is None:
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK,
            error=ConsistencyError(f"检查副本尚未建立: {inspection_id}"))
    if facts.inspection_state == int(_STATE.PENDING):
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK,
            error=ConsistencyError("未结束的分段检查不能释放其输入副本保留"))
    from .outputs import OutputsRepository
    return OutputsRepository().save_retention_release(
        RetentionRelease(facts.target_file_id, occurred_at), key, owned)


def _outcome(receipt):
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


def _segment_inspection_guard(event, context) -> None:
    if event.event_type != _EVENT["id"]:
        return
    for row in event.rows:
        if row.table != "segment_inspections":
            continue
        values = row.after.values
        action_id = values.get("action_id", context.state_rows.get(
            "segment_inspections", {}).get(row.row_id, {}).get("action_id"))
        action = context.state_rows.get("actions", {}).get(action_id)
        if action is None:
            raise EventValidationError("分段检查事件缺少所属动作事实")
        if event.reason == _CREATE:
            if (row.before.exists or values.get("state") != int(_STATE.PENDING)
                    or values.get("created_event_id") != event.event_id):
                raise EventValidationError("分段检查创建必须是 PENDING 初始事实")
            set_id = values.get("set_id")
            member_id = values.get("member_id")
            file_id = values.get("source_device_file_id")
            set_row = context.state_rows.get("segment_sets", {}).get(set_id)
            member = context.state_rows.get("segment_members", {}).get(member_id)
            file_row = context.state_rows.get("device_files", {}).get(file_id)
            if (set_row is None or member is None or file_row is None
                    or set_row["action_id"] != action_id
                    or set_row["state"] not in (int(_SET_STATE.MEMBERS_FIXED), int(_SET_STATE.ASSESSED))
                    or member["set_id"] != set_id or member["file_id"] != file_id
                    or member["role"] != int(_ROLE.ORIGINAL)
                    or member["ownership"] != int(_OWNERSHIP.OWNED)
                    or member["completion"] != int(_MEMBER_COMPLETION.COMPLETE)
                    or file_row["source_action_id"] != action_id
                    or file_row["role"] != int(_FILE_ROLE.ORIGINAL)
                    or file_row["completion_state"] != int(_FILE_COMPLETION.COMPLETE)
                    or file_row["presence_state"] != int(_PRESENCE.PRESENT)
                    or file_row["size_bytes"] is None):
                raise EventValidationError("检查责任必须引用已固定集合中的同动作完整原片")
        elif event.reason == _RESULT:
            current = context.state_rows.get("segment_inspections", {}).get(row.row_id)
            if current is None or current["state"] != int(_STATE.PENDING):
                raise EventValidationError("分段检查结果只能结束 PENDING 责任")
            if values.get("result_event_id") != event.event_id or values.get("result_json") is None:
                raise EventValidationError("分段检查结果必须引用自身可靠保存事件")
            if _result_requires_read(values.get("state"), values["result_json"]):
                reads = context.retry_wait_reads
                from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
                if (type(reads) is not RetryWaitReads
                        or not inspection_input_read_succeeded(reads.connection, current)):
                    raise EventValidationError("媒体检查结果要求原读取流程可靠成功")
        else:
            raise EventValidationError("分段检查事件分支未登记")


register_guard("segment_inspection", _segment_inspection_guard)
