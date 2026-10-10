"""完整路径基准的历史生产者与有界读取。

追加读取上一批，固定才流式核对完整范围；基准条目不建立业务
文件行。只有原申请的可靠保存结果允许拥有者继续目录读取。
"""
from contextlib import closing

from camctl.capture.baseline_models import (BaselineChunk, BaselineChunkResult,
    BaselineChunkSave, BaselineFixSave, BaselineRef, BaselineState)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.reads import BaselineRangeRead
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (CommandPlan, TransactionError, event_envelope,
    read_transaction_range, row_facts, saved_transaction_events, update_change)

_EVENT = load_event_registry()["events"]["BASELINE_CHUNK"]
_TYPE = _EVENT["id"]
_APPEND = _EVENT["branches"]["APPEND"]["reason"]
_FIX = _EVENT["branches"]["FIX"]["reason"]
_FIX_EMPTY = _EVENT["branches"]["FIX_EMPTY"]["reason"]
_OWNERSHIP = enum_for("device_activities.ownership_mode")
_DISPATCH = enum_for("device_activities.dispatch_state")
_OCCUPANCY = enum_for("device_activities.occupancy_state")
_EVENT_COLUMNS = "id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"


def _activity(connection, activity_id):
    ObjectId(activity_id)
    activity = row_facts(connection, "device_activities", activity_id)
    if activity is None:
        raise ConsistencyError("基准所属活动不存在")
    action = row_facts(connection, "actions", activity["action_id"])
    if action is None:
        raise ConsistencyError("基准所属动作不存在")
    if activity["ownership_mode"] != _OWNERSHIP.BASELINE_COMPARISON:
        raise ConsistencyError("活动没有声明基准比较归属")
    _directories(activity)
    return activity, action


def _directories(activity):
    scope = activity["output_scope_json"]
    if not isinstance(scope, dict) or set(scope) != {"directories"}:
        raise ConsistencyError("路径基准需要已声明的完整目录范围")
    directories = scope["directories"]
    if (not isinstance(directories, list) or not directories
            or any(not isinstance(root, str) for root in directories)
            or len(set(directories)) != len(directories)):
        raise ConsistencyError("路径基准目录列表无效")
    binding = DeviceBinding("scope", "scope")
    for root in directories:
        try:
            FileIdentity(binding, root)
        except ValueError as error:
            raise ConsistencyError("基准目录路径不符合已声明的定位结构") from error
    return directories


def _check_entries(entries, activity, action):
    binding = DeviceBinding(action["device_id"], action["driver_id"])
    roots = _directories(activity)
    for entry in entries:
        if entry.binding != binding or not any(entry.path.startswith(root + "/") for root in roots):
            raise ConsistencyError("基准身份不属于原设备绑定及输出范围")


def _decode_chunk(event):
    name, branch, _ = validate_event_structure(event)
    if name != "BASELINE_CHUNK" or branch != "APPEND" or set(event.evidence) != {"activity_id", "chunk_no", "entries"}:
        raise ConsistencyError("基准引用不是完整追加事件")
    evidence = event.evidence
    try:
        request = BaselineChunkSave(int(evidence["activity_id"]), int(evidence["chunk_no"]),
            tuple(FileIdentity.from_json(value) for value in evidence["entries"]), event.occurred_at)
    except (ValueError, TypeError, KeyError) as error:
        raise ConsistencyError("基准历史条目不可解释") from error
    if len(event.rows) != 1 or event.rows[0].table != "device_activities" or event.rows[0].row_id != request.activity_id:
        raise ConsistencyError("基准证据与活动行不一致")
    after = event.rows[0].after.values
    if after.get("baseline_last_event_id") != event.event_id:
        raise ConsistencyError("基准末引用不是实际追加事件")
    if request.chunk_no == 1 and after.get("baseline_first_event_id") != event.event_id:
        raise ConsistencyError("基准首批没有建立新的范围")
    return request.activity_id, BaselineChunk(event.event_id, request.chunk_no, request.entries)


def _event_at(connection, event_id):
    with closing(connection.execute(f"SELECT {_EVENT_COLUMNS} FROM history_events WHERE id=?", (event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("基准引用的事件不存在")
    event = decode_event_row(row)
    transaction = read_transaction_range(connection, event.transaction_id)
    if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
        raise ConsistencyError("基准事件不属于原事务范围")
    return event


def _chunk_at(connection, event_id, activity_id):
    identity, chunk = _decode_chunk(_event_at(connection, event_id))
    if identity != activity_id:
        raise ConsistencyError("基准范围混入其他活动")
    return chunk


def _tail_read(connection, activity):
    first, last = activity["baseline_first_event_id"], activity["baseline_last_event_id"]
    if first is None and last is None:
        return BaselineRangeRead(None, None, 0, 0, None, True)
    if first is None or last is None or first > last:
        raise ConsistencyError("基准首尾范围无效")
    head = _chunk_at(connection, first, activity["id"])
    tail = head if last == first else _chunk_at(connection, last, activity["id"])
    if head.chunk_no != 1 or tail.chunk_no < 1:
        raise ConsistencyError("基准范围没有连续收集的首批")
    return BaselineRangeRead(first, last, tail.chunk_no, None, tail.entries[-1].sort_key, False)


def _iter_chunks(connection, activity_id, first, last, *, previous=None, limit=None):
    parameters = [first, last, _TYPE, activity_id]
    conditions = ["id>=?", "id<=?", "event_type=?", "json_extract(body_json,'$.evidence.activity_id')=?",
                  "json_extract(body_json,'$.reason')=?"]
    parameters.append(_APPEND)
    if previous is not None:
        conditions.append("id>?")
        parameters.append(previous)
    suffix = ""
    if limit is not None:
        suffix = " LIMIT ?"
        parameters.append(limit)
    with closing(connection.execute(f"SELECT {_EVENT_COLUMNS} FROM history_events WHERE "
                                   + " AND ".join(conditions) + " ORDER BY id" + suffix, parameters)) as cursor:
        for stored in cursor:
            event = decode_event_row(stored)
            transaction = read_transaction_range(connection, event.transaction_id)
            if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
                raise ConsistencyError("基准事件不属于原事务")
            identity, chunk = _decode_chunk(event)
            if identity != activity_id:
                raise ConsistencyError("基准条目不属于本活动")
            yield chunk


def _complete_read(connection, activity, action):
    tail = _tail_read(connection, activity)
    if tail.first_event_id is None:
        return tail
    count, entries, last_key, last_event = 0, 0, None, None
    for chunk in _iter_chunks(connection, activity["id"], tail.first_event_id, tail.last_event_id):
        if chunk.chunk_no != count + 1 or (count == 0 and chunk.event_id != tail.first_event_id):
            raise ConsistencyError("基准历史批次存在缺口或混入旧收集")
        _check_entries(chunk.entries, activity, action)
        if last_key is not None and last_key >= chunk.entries[0].sort_key:
            raise ConsistencyError("基准跨批重复或顺序不一致")
        count += 1
        entries += len(chunk.entries)
        last_key = chunk.entries[-1].sort_key
        last_event = chunk.event_id
    if count != tail.chunk_count or last_event != tail.last_event_id:
        raise ConsistencyError("基准历史范围不完整")
    return BaselineRangeRead(tail.first_event_id, tail.last_event_id, count, entries, last_key, True)


def _fixed_counts(connection, activity):
    # 沿动作历史目录读取最后固定事实，避免每一页重新扫描整个基准。
    from camctl.contracts.enums import load_registry
    owner_type = load_registry()["history_objects"]["action"]["id"]
    columns = ",".join("h." + name for name in _EVENT_COLUMNS.split(","))
    with closing(connection.execute(f"SELECT {columns} FROM entity_event_links l JOIN history_events h ON h.id=l.event_id"
        " WHERE l.entity_type=? AND l.entity_id=? AND h.event_type=?"
        " AND json_extract(h.body_json,'$.evidence.activity_id')=?"
        " AND json_extract(h.body_json,'$.reason') IN (?,?) ORDER BY h.id DESC LIMIT 1",
        (owner_type, activity["action_id"], _TYPE, activity["id"], _FIX, _FIX_EMPTY))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("固定基准缺少可靠固定历史")
    event = decode_event_row(row)
    validate_event_structure(event)
    evidence = event.evidence
    if set(evidence) != {"activity_id", "chunk_count", "entry_count"}:
        raise ConsistencyError("固定基准历史缺少完整计数")
    try:
        request = BaselineFixSave(int(evidence["activity_id"]), int(evidence["chunk_count"]), int(evidence["entry_count"]), event.occurred_at)
    except (ValueError, TypeError) as error:
        raise ConsistencyError("固定基准计数不可解释") from error
    if (event.reason == _FIX_EMPTY) != (request.chunk_count == 0):
        raise ConsistencyError("固定基准分支与计数不一致")
    return request.chunk_count, request.entry_count


def read_reference(activity_id, owned):
    activity, action = _activity(owned.connection, activity_id)
    state = BaselineState(activity["baseline_state"])
    if state is BaselineState.FIXED:
        counts = _fixed_counts(owned.connection, activity)
    elif state is BaselineState.COLLECTING:
        stats = _complete_read(owned.connection, activity, action)
        counts = stats.chunk_count, stats.entry_count
    else:
        raise ConsistencyError("活动没有有效基准状态")
    return BaselineRef(activity_id, state, activity["baseline_first_event_id"], activity["baseline_last_event_id"], *counts)


def read_chunks(ref, cursor, batch, owned):
    if not isinstance(ref, BaselineRef) or ref.state is not BaselineState.FIXED:
        raise ConsistencyError("未固定基准不能读取用于差集的条目")
    if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= 128:
        raise ValueError("基准读取批次数必须为 1—128")
    activity, action = _activity(owned.connection, ref.activity_id)
    if read_reference(ref.activity_id, owned) != ref:
        raise ConsistencyError("基准引用与原活动固定范围不同")
    if ref.first_event_id is None:
        if cursor is not None:
            raise ConsistencyError("空基准没有继续游标")
        return Page((), None)
    count, last_key = 0, None
    if cursor is not None:
        ObjectId(cursor)
        if not ref.first_event_id <= cursor <= ref.last_event_id:
            raise ConsistencyError("基准游标不在原范围内")
        previous = _chunk_at(owned.connection, cursor, ref.activity_id)
        count, last_key = previous.chunk_no, previous.entries[-1].sort_key
    chunks = tuple(_iter_chunks(owned.connection, ref.activity_id, ref.first_event_id, ref.last_event_id,
                               previous=cursor, limit=batch + 1))
    for chunk in chunks:
        if chunk.chunk_no != count + 1 or (count == 0 and chunk.event_id != ref.first_event_id):
            raise ConsistencyError("基准页存在缺口或不连续")
        _check_entries(chunk.entries, activity, action)
        if last_key is not None and last_key >= chunk.entries[0].sort_key:
            raise ConsistencyError("基准页身份重复或顺序不一致")
        count, last_key = chunk.chunk_no, chunk.entries[-1].sort_key
    if len(chunks) <= batch:
        last = chunks[-1].event_id if chunks else cursor
        if count != ref.chunk_count or last != ref.last_event_id:
            raise ConsistencyError("基准末页缺少原范围条目")
        return Page(chunks, None)
    return Page(chunks[:batch], chunks[batch - 1].event_id)


class _BaselineCommand:
    def __init__(self, request, key):
        self.request, self.key = request, key

    def plan(self, scope):
        activity, action = _activity(scope.connection, self.request.activity_id)
        self.activity, self.action = activity, action
        owner = ("action", activity["action_id"])
        self.owners = {("device_activities", activity["id"]): owner}
        self.state = {"device_activities": {activity["id"]: activity}, "actions": {action["id"]: action}}
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            return self._reuse(scope, saved)
        if (activity["baseline_state"] != BaselineState.COLLECTING
                or activity["dispatch_state"] != _DISPATCH.NOT_DISPATCHED
                or activity["occupancy_state"] != _OCCUPANCY.HELD):
            raise ConsistencyError("只有未派发且保持占用的收集活动可保存基准")
        return self._new(scope)

    def _saved(self, saved, reason, evidence):
        if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"]) != (_TYPE, reason)
                or saved[0]["occurred_at"] != self.request.occurred_at
                or not json_equal(saved[0]["body"]["evidence"], evidence)):
            raise TransactionError("基准原键重送的分支、时刻或完整输入不同")
        rows = saved[0]["body"]["rows"]
        if len(rows) != 1 or rows[0]["table"] != "device_activities" or rows[0]["id"] != self.request.activity_id:
            raise TransactionError("基准原键属于其他活动")
        return saved[0]

    def _save(self, scope, reason, before, after, evidence, stats, result):
        allocation = scope.allocate(1)
        if reason == _APPEND:
            after = {**after, "baseline_last_event_id": allocation.first_event_id}
            if self.request.chunk_no == 1:
                after["baseline_first_event_id"] = allocation.first_event_id
            result = BaselineChunkResult(self.request.activity_id, self.request.chunk_no, allocation.first_event_id)
        changed = {key: value for key, value in after.items() if not json_equal(before[key], value)}
        old = {key: before[key] for key in changed}
        event = event_envelope(allocation.first_event_id, allocation.txn_id, _TYPE, reason,
            (update_change("device_activities", self.request.activity_id, old, changed),), self.request.occurred_at, evidence)
        return CommandPlan((event,), self.owners, self.state, result=result,
                           baseline_reads={self.request.activity_id: stats})


class AppendBaselineCommand(_BaselineCommand):
    def __init__(self, request, key):
        if not isinstance(request, BaselineChunkSave):
            raise TypeError("基准追加需要完整 BaselineChunkSave")
        super().__init__(request, key)

    def _evidence(self):
        return {"activity_id": self.request.activity_id, "chunk_no": self.request.chunk_no,
                "entries": [entry.as_json() for entry in self.request.entries]}

    def _new(self, scope):
        stats = _tail_read(scope.connection, self.activity)
        _check_entries(self.request.entries, self.activity, self.action)
        if self.request.chunk_no != 1:
            if self.request.chunk_no != stats.chunk_count + 1 or stats.last_identity is None:
                raise ConsistencyError("追加批号必须沿原收集连续推进")
            if stats.last_identity >= self.request.entries[0].sort_key:
                raise ConsistencyError("追加身份与上一批重复或顺序不一致")
        before = {key: self.activity[key] for key in ("baseline_first_event_id", "baseline_last_event_id")}
        return self._save(scope, _APPEND, before, {}, self._evidence(), stats, None)

    def _reuse(self, scope, saved):
        event = self._saved(saved, _APPEND, self._evidence())
        result = BaselineChunkResult(self.request.activity_id, self.request.chunk_no, event["event_id"])
        return CommandPlan((), self.owners, self.state, result=result, read_only=True)


class FixBaselineCommand(_BaselineCommand):
    def __init__(self, request, key):
        if not isinstance(request, BaselineFixSave):
            raise TypeError("基准固定需要完整 BaselineFixSave")
        super().__init__(request, key)

    def _evidence(self):
        return {"activity_id": self.request.activity_id, "chunk_count": self.request.chunk_count,
                "entry_count": self.request.entry_count}

    def _new(self, scope):
        request = self.request
        empty = request.chunk_count == 0
        stats = _tail_read(scope.connection, self.activity) if empty else _complete_read(scope.connection, self.activity, self.action)
        if not empty and (stats.chunk_count, stats.entry_count) != (request.chunk_count, request.entry_count):
            raise ConsistencyError("固定申请与实际完整基准计数不同")
        before = {"baseline_state": self.activity["baseline_state"]}
        after = {"baseline_state": int(BaselineState.FIXED)}
        if empty:
            for key in ("baseline_first_event_id", "baseline_last_event_id"):
                before[key], after[key] = self.activity[key], None
        result = BaselineRef(request.activity_id, BaselineState.FIXED,
            None if empty else stats.first_event_id, None if empty else stats.last_event_id,
            request.chunk_count, request.entry_count)
        return self._save(scope, _FIX_EMPTY if empty else _FIX, before, after, self._evidence(), stats, result)

    def _reuse(self, scope, saved):
        event = self._saved(saved, _FIX_EMPTY if not self.request.chunk_count else _FIX, self._evidence())
        transaction = event["transaction"]
        values = read_row_values_at_boundary(scope.connection, owner=("action", self.activity["action_id"]),
            table="device_activities", row_id=self.request.activity_id,
            columns=frozenset({"baseline_state", "baseline_first_event_id", "baseline_last_event_id"}),
            current_values=self.activity, boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
        result = BaselineRef(self.request.activity_id, BaselineState(values["baseline_state"]),
            values["baseline_first_event_id"], values["baseline_last_event_id"],
            self.request.chunk_count, self.request.entry_count)
        return CommandPlan((), self.owners, self.state, result=result, read_only=True)


def baseline_guard(event, context):
    """独立核对允许的范围变化与同事务取得的历史读取摘要。"""
    try:
        evidence = event.evidence
        activity_id = evidence["activity_id"]
        ObjectId(activity_id)
        if len(event.rows) != 1 or event.rows[0].table != "device_activities" or event.rows[0].row_id != activity_id:
            raise ValueError("基准证据与活动行不同")
        activity = context.state_rows["device_activities"][activity_id]
        action = context.state_rows["actions"][activity["action_id"]]
        if (activity["ownership_mode"] != _OWNERSHIP.BASELINE_COMPARISON
                or activity["baseline_state"] != BaselineState.COLLECTING
                or activity["dispatch_state"] != _DISPATCH.NOT_DISPATCHED
                or activity["occupancy_state"] != _OCCUPANCY.HELD):
            raise ValueError("基准活动不满足未派发收集资格")
        stats = context.baseline_reads[activity_id]
        if (stats.first_event_id, stats.last_event_id) != (activity["baseline_first_event_id"], activity["baseline_last_event_id"]):
            raise ValueError("基准读取摘要属于其他收集范围")
        after = {**activity, **event.rows[0].after.values}
        if event.reason == _APPEND:
            _, chunk = _decode_chunk(event)
            _check_entries(chunk.entries, activity, action)
            if chunk.chunk_no == 1:
                first = event.event_id
            else:
                if chunk.chunk_no != stats.chunk_count + 1 or stats.last_identity is None or stats.last_identity >= chunk.entries[0].sort_key:
                    raise ValueError("基准批号或跨批身份不连续")
                first = stats.first_event_id
            if after["baseline_first_event_id"] != first or after["baseline_last_event_id"] != event.event_id:
                raise ValueError("追加后的首尾不是原连续收集范围")
        elif event.reason in (_FIX, _FIX_EMPTY):
            if set(evidence) != {"activity_id", "chunk_count", "entry_count"}:
                raise ValueError("基准固定缺少完整计数")
            request = BaselineFixSave(int(activity_id), int(evidence["chunk_count"]), int(evidence["entry_count"]), event.occurred_at)
            if after["baseline_state"] != BaselineState.FIXED:
                raise ValueError("基准固定没有保存 FIXED")
            if event.reason == _FIX_EMPTY:
                if request.chunk_count or request.entry_count or after["baseline_first_event_id"] is not None or after["baseline_last_event_id"] is not None:
                    raise ValueError("空基准必须保存零计数并清空范围")
            elif (not stats.complete or not stats.chunk_count or
                    (request.chunk_count, request.entry_count) != (stats.chunk_count, stats.entry_count)
                    or (after["baseline_first_event_id"], after["baseline_last_event_id"]) != (stats.first_event_id, stats.last_event_id)):
                raise ValueError("非空基准固定缺少完整连续范围和计数")
        else:
            raise ValueError("未知基准分支")
    except (KeyError, ValueError, TypeError) as error:
        raise EventValidationError(f"基准历史校验失败: {error}") from error


def register_baseline_guard():
    register_guard("baseline", baseline_guard)
