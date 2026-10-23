"""独立软件重启责任的版本化历史事务与阶段守卫。"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import uuid
from typing import Mapping

from camctl.capture.software_recovery import RecoveryPhase, recovery_blocker
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.history.events import reason_for_branch
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import (CommandPlan, commit_operation, event_envelope,
    next_row_id, row_change, row_facts, saved_transaction_events, update_change)


@dataclass(frozen=True)
class RecoverySave:
    activity_id: int
    previous: Mapping | None
    state: Mapping
    occurred_at: int
    evidence: Mapping
    ended: bool = False


_IMMUTABLE = ("version", "origin_trigger_id", "device_id", "driver_id", "serial", "fingerprint",
    "created_at", "deadline_at", "timeout_s", "retry_interval_s", "max_reboots", "max_checks",
    "max_pages", "baseline_first", "baseline_last")
_NEXT = {
    RecoveryPhase.CHECKING: {RecoveryPhase.CHECKING, RecoveryPhase.INTENT, RecoveryPhase.ABANDONED},
    RecoveryPhase.INTENT: {RecoveryPhase.INTENT, RecoveryPhase.CONFIRMING, RecoveryPhase.PREPARING, RecoveryPhase.ABANDONED},
    RecoveryPhase.CONFIRMING: {RecoveryPhase.CONFIRMING, RecoveryPhase.PREPARING},
    RecoveryPhase.PREPARING: {RecoveryPhase.PREPARING, RecoveryPhase.SCANNING},
    RecoveryPhase.SCANNING: {RecoveryPhase.SCANNING, RecoveryPhase.READY},
    RecoveryPhase.READY: {RecoveryPhase.ISOLATED}, RecoveryPhase.ISOLATED: set(), RecoveryPhase.ABANDONED: {RecoveryPhase.CHECKING},
}


def _validate(previous, state, *, ended, evidence):
    try:
        phase = RecoveryPhase(state["phase"])
        from camctl.contracts.values import ObjectId, UtcMicros, MAX_OBJECT_ID
        for key in ("trigger_id", "origin_trigger_id"):
            ObjectId(state[key])
        for key in ("created_at", "deadline_at", "next_check_at"):
            UtcMicros(state[key])
            if not -MAX_OBJECT_ID - 1 <= state[key] <= MAX_OBJECT_ID:
                raise ValueError("恢复时间超出整数微秒范围")
        for key in ("timeout_s", "retry_interval_s"):
            if not isinstance(state[key], str):
                raise ValueError("恢复秒数必须是精确文本")
            number = Decimal(state[key])
            if not number.is_finite() or number < 0 or (key == "timeout_s" and number == 0):
                raise ValueError("恢复调用和等待期限无效")
        for key in ("prepared", "scope_finalized"):
            if type(state[key]) is not bool:
                raise ValueError("阶段事实必须是布尔值")
        for key in ("scope_pages", "scope_members"):
            if type(state[key]) is not int or not 0 <= state[key] <= state["max_pages"] * 128:
                raise ValueError("恢复范围累计超界")
        for key in ("boot_before", "boot_after"):
            if state[key] is not None and (not isinstance(state[key], str) or str(uuid.UUID(state[key])) != state[key]):
                raise ValueError("启动代次必须是可靠规范UUID")
        if state["reported_serial"] is not None and (not isinstance(state["reported_serial"], str) or not state["reported_serial"]):
            raise ValueError("额外设备序列号必须可靠有值或为空")
        if (type(state["version"]) is not int or state["version"] != 1 or any(not isinstance(state[key], str) or not state[key]
            for key in ("device_id", "driver_id", "serial", "fingerprint"))
                or state["deadline_at"] <= state["created_at"]):
            raise ValueError("身份或期限无效")
        for key in ("trigger_id", "max_reboots", "max_checks", "max_pages"):
            if type(state[key]) is not int or state[key] <= 0:
                raise ValueError("预算或触发身份无效")
        for key, limit in (("reboots", "max_reboots"), ("checks", "max_checks"), ("pages", "max_pages")):
            if type(state[key]) is not int or not 0 <= state[key] <= state[limit]:
                raise ValueError("累计次数超界")
        if previous is None:
            if phase is not RecoveryPhase.CHECKING or any(state[key] for key in ("reboots", "checks", "pages")):
                raise ValueError("首次责任不是空预算检查阶段")
        else:
            old_phase = RecoveryPhase(previous["phase"])
            if any(not json_equal(previous[key], state[key]) for key in _IMMUTABLE):
                raise ValueError("原身份、基准或预算发生变化")
            if phase not in _NEXT[old_phase] and not (
                    phase is RecoveryPhase.ISOLATED and old_phase not in (
                        RecoveryPhase.READY, RecoveryPhase.ISOLATED, RecoveryPhase.ABANDONED)):
                raise ValueError("恢复阶段不能重新开始")
            if state["trigger_id"] != previous["trigger_id"] and not (
                    old_phase is RecoveryPhase.ABANDONED and phase is RecoveryPhase.CHECKING
                    and previous["reboots"] == 0):
                raise ValueError("只允许未派发责任由新的有效动作接续")
            if "directory" not in evidence and any(not json_equal(previous[key], state[key])
                    for key in ("cursor", "scope_pages", "scope_members", "scope_first_event_id", "scope_last_event_id")):
                raise ValueError("范围游标和累计事实只能随可靠目录页推进")
            for key in ("boot_before", "boot_after"):
                if previous[key] is not None and previous[key] != state[key]:
                    raise ValueError("已保存的启动代次不得改写")
            for key in ("prepared", "scope_finalized"):
                if previous[key] is True and state[key] is not True:
                    raise ValueError("可靠阶段事实不得回退")
            for key in ("reboots", "checks", "pages"):
                if not previous[key] <= state[key] <= previous[key] + 1:
                    raise ValueError("累计次数不能回退或跳过")
            if state["reboots"] != previous["reboots"] and not (
                    old_phase is RecoveryPhase.CHECKING and phase is RecoveryPhase.INTENT
                    and state["boot_before"] is not None):
                raise ValueError("只有首个可靠意图可以增加派发次数")
        if ended:
            identity = evidence.get("identity")
            if (previous is None or RecoveryPhase(previous["phase"]) not in (
                    RecoveryPhase.INTENT, RecoveryPhase.CONFIRMING)
                    or phase is not RecoveryPhase.PREPARING or state["reboots"] < 1
                    or not state["boot_before"] or not state["boot_after"]
                    or state["boot_before"] == state["boot_after"]
                    or not isinstance(identity, list) or len(identity) != 3
                    or identity[:2] != [state["boot_after"], state["fingerprint"]]
                    or (state["reported_serial"] is not None and identity[2] is not None
                        and identity[2] != state["reported_serial"])):
                raise ValueError("设备结束没有同一绑定的新启动代次")
        elif previous is not None and phase is RecoveryPhase.PREPARING and RecoveryPhase(previous["phase"]) is not RecoveryPhase.PREPARING:
            raise ValueError("新启动依据必须伴随设备结束保存")
        if phase in (RecoveryPhase.SCANNING, RecoveryPhase.READY) and not state["prepared"]:
            raise ValueError("目录阶段必须先有可靠会话准备")
        if phase is RecoveryPhase.READY and not state["scope_finalized"]:
            raise ValueError("就绪阶段必须具有完整范围事实")
        if state["prepared"] and (previous is None or not previous["prepared"]):
            if (previous is None or previous["phase"] != RecoveryPhase.PREPARING.value
                    or phase is not RecoveryPhase.SCANNING or evidence.get("preparation", {}).get("ready") is not True):
                raise ValueError("会话准备完成缺少原阶段的可靠设置与存储结果")
        if state["scope_finalized"] and (previous is None or not previous["scope_finalized"]):
            directory = evidence.get("directory")
            if (not state["prepared"] or phase is not RecoveryPhase.READY or previous is None
                    or RecoveryPhase(previous["phase"]) is not RecoveryPhase.SCANNING
                    or not isinstance(directory, Mapping) or directory.get("next_cursor") is not None):
                raise ValueError("文件范围必须沿准备后的完整分页末页确定")
    except (ValueError, KeyError, TypeError, InvalidOperation, AttributeError) as error:
        raise ConsistencyError(f"软件恢复状态无效: {error}") from error


def _guard(event, context):
    recovery_rows = [row for row in event.rows if row.table == "software_recoveries"]
    if len(recovery_rows) != 1:
        raise EventValidationError("软件恢复必须修改唯一独立责任")
    row = recovery_rows[0]
    original = context.state_rows.get("software_recoveries", {}).get(row.row_id)
    state = row.after.values["state_json"]
    previous = None if original is None else original["state_json"]
    ended = any(change.table == "device_activities" for change in event.rows)
    try:
        _validate(previous, state, ended=ended, evidence=event.evidence["recovery"])
        activity_id = row.after.values.get("activity_id", (original or {}).get("activity_id"))
        activity = context.state_rows["device_activities"][activity_id]
        action = context.state_rows["actions"][activity["action_id"]]
        if action["status"] not in (4, 6) or activity["occupancy_state"] != 1:
            raise ValueError("旧动作没有失败或取消后的保留占用")
        if state["device_id"] != action["device_id"] or state["driver_id"] != action["driver_id"]:
            raise ValueError("恢复绑定与原活动不一致")
        if ended and activity["dispatch_state"] not in (2, 3):
            raise ValueError("可靠未启动活动不需要重启结束依据")
        for change in event.rows:
            if change.table == "device_activities" and (change.row_id != activity_id or change.after.values != {"activity_state": 3}):
                raise ValueError("结束观察修改了其他活动或字段")
        if context.current_reads is None:
            raise ValueError("软件恢复缺少原事务独立核实端口")
        context.current_reads.verify_software_recovery_event(event, context)
    except (ConsistencyError, ValueError, KeyError) as error:
        raise EventValidationError(str(error)) from error


def register_software_recovery_guard():
    register_guard("software_recovery", _guard)


class _Save:
    def __init__(self, request, key):
        self.request, self.key = request, key

    def plan(self, scope):
        request = self.request
        connection = scope.connection
        saved = saved_transaction_events(connection, self.key)
        request_document = {"activity_id": request.activity_id, "previous": request.previous,
            "state": request.state, "occurred_at": request.occurred_at,
            "evidence": request.evidence, "ended": request.ended}
        if saved is not None:
            if (len(saved) != 1 or saved[0]["type"] != 13
                    or saved[0]["reason"] != reason_for_branch("DEVICE_OBSERVED", "SOFTWARE_RECOVERY")
                    or not json_equal(saved[0]["body"]["evidence"]["recovery"].get("request"), request_document)):
                raise ConsistencyError("原软件恢复事务身份与完整申请不同")
            return CommandPlan((), {}, {}, read_only=True)
        activity = row_facts(connection, "device_activities", request.activity_id)
        action = row_facts(connection, "actions", activity["action_id"])
        found = connection.execute("SELECT id FROM software_recoveries WHERE activity_id=?", (request.activity_id,)).fetchone()
        original = None if found is None else row_facts(connection, "software_recoveries", found[0])
        previous = None if original is None else original["state_json"]
        if not json_equal(previous, request.previous):
            raise ConsistencyError("软件恢复原状态已经改变，不能覆盖")
        state_values = dict(request.state)
        evidence = {**request.evidence, "request": request_document}
        allocation = scope.allocate(1)
        directory = evidence.get("directory")
        if directory is not None:
            directory = dict(directory)
            directory.update(activity_id=activity["id"], page_no=previous["scope_pages"] + 1,
                previous_event_id=previous["scope_last_event_id"])
            directory["new_entries"] = classify_scope_entries(connection, activity, directory["entries"], state_values["max_pages"])
            state_values.update(scope_pages=previous["scope_pages"] + 1,
                scope_members=previous["scope_members"] + len(directory["new_entries"]),
                scope_first_event_id=previous["scope_first_event_id"] or allocation.first_event_id,
                scope_last_event_id=allocation.first_event_id)
            evidence["directory"] = directory
            _verify_directory(connection, activity, previous, state_values, directory)
        _validate(previous, state_values, ended=request.ended, evidence=evidence)
        if (previous is None or request.state["trigger_id"] != previous["trigger_id"]
                or request.state["reboots"] > previous["reboots"]):
            trigger = row_facts(connection, "actions", request.state["trigger_id"])
            if trigger["status"] not in (1, 2) or trigger["cancel_requested"]:
                raise ConsistencyError("软件重启触发动作已经结束")
            from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase
            window = LaunchWindow(trigger["scheduled_at"], trigger["scheduled_at"] + trigger["max_delay_ms"] * 1000)
            if window_phase(window, request.occurred_at) is not WindowPhase.IN_WINDOW:
                raise ConsistencyError("软件恢复触发动作没有有效时间资格")
            if recovery_blocker(connection, activity, trigger["id"]):
                raise ConsistencyError("软件重启仍有冲突责任")
        identity = next_row_id(connection, "software_recoveries") if original is None else original["id"]
        if original is None:
            changes = [row_change("software_recoveries", identity, {"action_id": action["id"],
                "activity_id": activity["id"], "state_json": state_values})]
        else:
            changes = [update_change("software_recoveries", identity,
                {"state_json": previous}, {"state_json": state_values})]
        if request.ended and activity["activity_state"] != 3:
            changes.append(update_change("device_activities", activity["id"],
                {"activity_state": activity["activity_state"]}, {"activity_state": 3}))
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 13,
            reason_for_branch("DEVICE_OBSERVED", "SOFTWARE_RECOVERY"), changes,
            request.occurred_at, evidence={"recovery": evidence})
        state = {"actions": {action["id"]: action}, "device_activities": {activity["id"]: activity},
                 "software_recoveries": {} if original is None else {identity: original}}
        owners = {("software_recoveries", identity): ("action", action["id"]),
                  ("device_activities", activity["id"]): ("action", action["id"])}
        from camctl.history.reads import SoftwareRecoveryReadScope
        return CommandPlan((event,), owners, state,
            software_recovery_read_scope=SoftwareRecoveryReadScope(action["id"], activity["id"]))


def save_recovery(request, key, owned):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.runtime import DatabaseUnsupportedFormatError
    from camctl.contracts.enums import enum_for
    if owned.metadata.format_version < int(enum_for("database_metadata.format_version").V3):
        return DbOutcome(DbOutcomeKind.ROLLED_BACK,
            error=DatabaseUnsupportedFormatError("独立软件重启责任要求显式升级到格式 V3"))
    receipt = commit_operation(_Save(request, key), key, owned)
    return DbOutcome(kind=DbOutcomeKind(receipt.kind), value=receipt.result, error=receipt.error, boundary=receipt.boundary)


def classify_scope_entries(connection, activity, entries, max_chunks):
    """按原完整基准逐批计算本页差集，内存只保留当前目录页。"""
    from .baseline import _fixed_counts, _iter_chunks, _check_entries
    from camctl.devices.file_identity import FileIdentity
    expected_chunks, expected_entries = _fixed_counts(connection, activity)
    if expected_chunks > max_chunks:
        raise ConsistencyError("原目录基准超过本恢复责任的有限读取预算")
    identities = tuple(FileIdentity.from_json(entry) for entry in entries)
    action = row_facts(connection, "actions", activity["action_id"])
    _check_entries(identities, activity, action)
    baseline_members = set()
    page_keys = {identity.sort_key for identity in identities}
    count = total = 0
    previous = None
    first, last = activity["baseline_first_event_id"], activity["baseline_last_event_id"]
    if expected_chunks:
        for chunk in _iter_chunks(connection, activity["id"], first, last, limit=max_chunks + 1):
            if chunk.chunk_no != count + 1 or (count == 0 and chunk.event_id != first):
                raise ConsistencyError("原目录基准批次不连续")
            _check_entries(chunk.entries, activity, action)
            if not chunk.entries or (previous is not None and previous >= chunk.entries[0].sort_key):
                raise ConsistencyError("原目录基准条目顺序不可靠")
            baseline_members.update(entry.sort_key for entry in chunk.entries if entry.sort_key in page_keys)
            count += 1
            total += len(chunk.entries)
            previous = chunk.entries[-1].sort_key
            final = chunk.event_id
        if count != expected_chunks or total != expected_entries or final != last:
            raise ConsistencyError("原目录基准范围不完整")
    elif first is not None or last is not None:
        raise ConsistencyError("空目录基准仍带非空引用")
    return [entry for entry, identity in zip(entries, identities) if identity.sort_key not in baseline_members]


def _verify_directory(connection, activity, previous, state, directory):
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor, DirectoryRequest
    from camctl.devices.file_identity import FileIdentity
    from decimal import Decimal
    if (previous is None or previous["phase"] != RecoveryPhase.SCANNING.value
            or directory["cursor"] != previous["cursor"] or directory["next_cursor"] != state["cursor"]):
        raise ConsistencyError("软件恢复目录页没有保持原连续游标")
    binding = DeviceBinding(state["device_id"], state["driver_id"])
    cursor = None if directory["cursor"] is None else DirectoryCursor.from_json(directory["cursor"])
    request = DirectoryRequest(binding, tuple(activity["output_scope_json"]["directories"]), cursor, 128, Decimal(state["timeout_s"]))
    identities = tuple(FileIdentity.from_json(entry) for entry in directory["entries"])
    index = 0 if cursor is None else cursor.directory_index
    if (len(identities) > 128 or any(identity.binding != binding
            or not identity.path.startswith(request.directories[index] + "/") for identity in identities)
            or any(a.sort_key >= b.sort_key for a, b in zip(identities, identities[1:]))
            or (identities and cursor is not None and cursor.after_path is not None
                and identities[0].path <= cursor.after_path)):
        raise ConsistencyError("软件恢复目录页越界或没有严格前进")
    next_cursor = None if state["cursor"] is None else DirectoryCursor.from_json(state["cursor"])
    if next_cursor is None:
        if index != len(request.directories) - 1:
            raise ConsistencyError("完整扫描遗漏原范围中的后续目录")
    elif (next_cursor.binding != binding or next_cursor.directories != request.directories
            or not ((next_cursor.directory_index == index and identities
                     and next_cursor.after_path == identities[-1].path)
                    or (next_cursor.directory_index == index + 1 and next_cursor.after_path is None))):
        raise ConsistencyError("软件恢复目录后续游标跳过范围")
    new_entries = classify_scope_entries(connection, activity, directory["entries"], state["max_pages"])
    if directory.get("new_entries") != new_entries:
        raise ConsistencyError("软件恢复差集不等于原完整基准与当前可靠页的差集")
    if (state["scope_pages"] != previous["scope_pages"] + 1
            or state["scope_members"] != previous["scope_members"] + len(new_entries)):
        raise ConsistencyError("软件恢复的范围批次或成员累计不符")


def verify_recovery_scope(connection, activity, state):
    """释放前重读完整责任的可靠页链，空差集同样要求完整末页。"""
    if (state["phase"] != RecoveryPhase.READY.value or not state["scope_finalized"]
            or state["scope_pages"] < 1 or not state["scope_first_event_id"] or not state["scope_last_event_id"]):
        raise ConsistencyError("软件恢复缺少完整分页范围事实")
    from camctl.history.decoding import decode_event_row
    rows = connection.execute("SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
        " FROM history_events WHERE id>=? AND id<=? AND event_type=13"
        " AND json_extract(body_json,'$.reason')=?"
        " AND json_extract(body_json,'$.evidence.recovery.directory.activity_id')=? ORDER BY id LIMIT ?",
        (state["scope_first_event_id"], state["scope_last_event_id"],
         reason_for_branch("DEVICE_OBSERVED", "SOFTWARE_RECOVERY"), activity["id"], state["max_pages"] + 1))
    count = members = 0
    cursor = None
    last = None
    for row in rows:
        event = decode_event_row(row)
        directory = event.evidence["recovery"]["directory"]
        if count == 0 and event.event_id != state["scope_first_event_id"]:
            raise ConsistencyError("软件恢复范围缺少首页")
        if (directory["cursor"] != cursor or directory["page_no"] != count + 1
                or directory["previous_event_id"] != last):
            raise ConsistencyError("软件恢复范围页链存在缺口")
        new_entries = classify_scope_entries(connection, activity, directory["entries"], state["max_pages"])
        if not json_equal(directory["new_entries"], new_entries):
            raise ConsistencyError("软件恢复范围差集事实不可靠")
        members += len(new_entries)
        count += 1
        cursor = directory["next_cursor"]
        last = event.event_id
        if cursor is None and last != state["scope_last_event_id"]:
            raise ConsistencyError("软件恢复末页之后仍出现范围成员")
    if (count != state["scope_pages"] or members != state["scope_members"]
            or last != state["scope_last_event_id"] or cursor is not None):
        raise ConsistencyError("软件恢复完整页数、差集范围或末引用不符")


def verify_scope_event(connection, event, context):
    row = next(change for change in event.rows if change.table == "software_recoveries")
    original = context.state_rows.get("software_recoveries", {}).get(row.row_id)
    activity_id = row.after.values.get("activity_id", (original or {}).get("activity_id"))
    activity = context.state_rows["device_activities"][activity_id]
    state = row.after.values["state_json"]
    previous = None if original is None else original["state_json"]
    if previous is None or state["trigger_id"] != previous["trigger_id"] or state["reboots"] > previous["reboots"]:
        trigger = row_facts(connection, "actions", state["trigger_id"])
        from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase
        if (trigger is None or trigger["status"] not in (1, 2) or trigger["cancel_requested"]
                or window_phase(LaunchWindow(trigger["scheduled_at"], trigger["scheduled_at"] + trigger["max_delay_ms"] * 1000), event.occurred_at)
                   is not WindowPhase.IN_WINDOW or recovery_blocker(connection, activity, trigger["id"])):
            raise ConsistencyError("软件恢复缺少有效触发资格或仍有冲突调用")
    if previous is not None and state["reboots"] > previous["reboots"]:
        identity = previous.get("last_identity", {}).get("identity")
        if (not isinstance(identity, list) or len(identity) != 3
                or identity[:2] != [state["boot_before"], state["fingerprint"]]
                or state["reported_serial"] != identity[2]):
            raise ConsistencyError("重启意图的原启动代次没有可靠身份核实")
    directory = event.evidence["recovery"].get("directory")
    if directory is not None:
        _verify_directory(connection, activity, original["state_json"], row.after.values["state_json"], directory)
        if (state["scope_last_event_id"] != event.event_id
                or state["scope_first_event_id"] != (previous["scope_first_event_id"] or event.event_id)):
            raise ConsistencyError("软件恢复范围的首末引用不是原连续页事件")
