"""结果页保留原完整实际结果，页范围与首次文件发现共同提交。"""
from contextlib import closing
from dataclasses import replace

from camctl.capture.files import FileObservationSave, file_identity_key
from camctl.capture.result_inputs import page_from_outcome, saved_outcome
from camctl.capture.result_pages import ResultPageSave, ResultPageRef, SavedResultPage, result_page_input
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.capture.results import CaptureFile
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.file_identity import FileIdentity
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.operations.models import AttemptTicket
from camctl.persistence.transaction import (CommandPlan, TransactionError,
    event_envelope, next_row_id, read_transaction_range, row_facts, saved_transaction_events, update_change)
from .baseline import read_reference, read_chunks, _chunk_at, _check_entries, _directories

_EVENT = load_event_registry()["events"]["RESULT_PAGE"]
_TYPE = _EVENT["id"]
_APPEND = _EVENT["branches"]["APPEND"]["reason"]
_FILE_EVENT = load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]
_COLUMNS = "id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
_PAYLOAD_FIELDS = {"run_id", "attempt_no", "activity_id", "page_no", "cursor", "outcome", "baseline_position", "last_identity", "file_ids"}
_OWNERSHIP = enum_for("device_activities.ownership_mode")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_RUN_KIND = enum_for("operation_runs.kind")
_ROLE = enum_for("device_files.role")


class _BaselineCursor:
    """只保留正在比较的一批；已比较部分由原页位置恢复。"""

    def __init__(self, scope, activity, action, position, *, first):
        self.scope = scope
        self.ref = read_reference(activity["id"], scope)
        self.chunk, self.index = None, 0
        if first:
            pages = read_chunks(self.ref, None, 1, scope)
            self.chunk = pages.items[0] if pages.items else None
        elif position is not None:
            if not self.ref.first_event_id <= position["event_id"] <= self.ref.last_event_id:
                raise ConsistencyError("结果页比较位置不在原固定基准范围内")
            self.chunk = _chunk_at(scope.connection, position["event_id"], activity["id"])
            _check_entries(self.chunk.entries, activity, action)
            self.index = position["entry_index"]
            if self.index >= len(self.chunk.entries):
                raise ConsistencyError("结果页比较位置超出原基准批次")

    @property
    def position(self):
        return None if self.chunk is None else {"event_id": self.chunk.event_id, "entry_index": self.index}

    def _advance(self):
        self.index += 1
        if self.index == len(self.chunk.entries):
            pages = read_chunks(self.ref, self.chunk.event_id, 1, self.scope)
            self.chunk = pages.items[0] if pages.items else None
            self.index = 0

    def is_new(self, identity):
        while self.chunk is not None:
            original = self.chunk.entries[self.index]
            if original.sort_key > identity.sort_key:
                return True
            self._advance()
            if original.sort_key == identity.sort_key:
                return False
        return True


def _difference(scope, activity, action, page, previous):
    binding = DeviceBinding(action["device_id"], action["driver_id"])
    roots = tuple(_directories(activity))
    last = None if previous is None else previous["last_identity"]
    for cursor in (page.cursor, page.next_cursor):
        if cursor is not None and (cursor.binding != binding or cursor.directories != roots):
            raise ConsistencyError("结果页游标改变原完整输出范围")
    entries = []
    comparison = _BaselineCursor(scope, activity, action,
        None if previous is None else previous["baseline_position"], first=previous is None)
    for entry in page.entries:
        identity = FileIdentity(binding, entry.identity)
        _check_entries((identity,), activity, action)
        if not json_equal(entry.locator, {"path": identity.path}):
            raise ConsistencyError("路径稳定身份与实际定位结构不同")
        if last is not None and identity.path <= last:
            raise ConsistencyError("结果页身份须沿原扫描严格递增且无重复")
        directory_index = next(index for index, root in enumerate(roots) if identity.path.startswith(root + "/"))
        position = (directory_index, identity.path)
        if page.cursor is not None and position <= (page.cursor.directory_index, page.cursor.after_path or ""):
            raise ConsistencyError("结果条目没有沿原请求游标推进")
        if page.next_cursor is not None and position > (page.next_cursor.directory_index, page.next_cursor.after_path or ""):
            raise ConsistencyError("下一游标尚未覆盖本页实际条目")
        if comparison.is_new(identity):
            entries.append(entry)
        last = identity.path
    return entries, comparison.position, last


def _load(connection, ticket):
    ObjectId(ticket.run_id)
    ObjectId(ticket.attempt_id)
    run = row_facts(connection, "operation_runs", ticket.run_id)
    if (run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)
            or ticket.operation != "result" or ticket.target_id != str(run["activity_id"])
            or ticket.responsibility_key != run["responsibility_key"]):
        raise ConsistencyError("结果页票据与原活动责任不符")
    with closing(connection.execute("SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                                   (ticket.run_id, ticket.attempt_id))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("结果页的原尝试不存在")
    attempt = row_facts(connection, "operation_attempts", row[0])
    action = row_facts(connection, "actions", run["action_id"])
    activity = row_facts(connection, "device_activities", run["activity_id"])
    if action is None or activity is None or activity["action_id"] != action["id"]:
        raise ConsistencyError("结果页原动作与活动关系缺失")
    return run, attempt, action, activity


def _decode(event):
    name, branch, _ = validate_event_structure(event)
    if name != "RESULT_PAGE" or branch != "APPEND" or set(event.evidence) != {"attempt_id", "result_page"}:
        raise ConsistencyError("结果页引用不是完整页事实")
    raw = event.evidence["result_page"]
    if not isinstance(raw, dict) or set(raw) != _PAYLOAD_FIELDS:
        raise ConsistencyError("结果页历史正文成员不完整")
    try:
        ObjectId(event.evidence["attempt_id"])
        for field in ("run_id", "attempt_no", "activity_id", "page_no"):
            ObjectId(raw[field])
        ticket = AttemptTicket(raw["attempt_no"], "result", str(raw["activity_id"]),
                               f"results/{raw['activity_id']}", raw["run_id"])
        cursor = None if raw["cursor"] is None else DirectoryCursor.from_json(raw["cursor"])
        actual = raw["outcome"]
        if not isinstance(actual, dict) or set(actual) != {"status", "effect_state", "error", "result"}:
            raise ValueError("实际结果成员不完整")
        page = page_from_outcome(ticket, saved_outcome(actual["status"], actual["effect_state"],
                                actual["result"], actual["error"]), cursor=cursor)
        file_ids = raw["file_ids"]
        if not isinstance(file_ids, list) or len(file_ids) > 128:
            raise ValueError("本页文件引用须是有界数组")
        identities = {entry.identity for entry in page.entries}
        seen = set()
        seen_ids = set()
        for item in file_ids:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("本页文件引用必须是身份和 ID 的二项数组")
            identity, file_id = item
            ObjectId(file_id)
            if not isinstance(identity, str) or identity not in identities or identity in seen or file_id in seen_ids:
                raise ValueError("文件引用与原页输入不符")
            seen.add(identity)
            seen_ids.add(file_id)
        position = raw["baseline_position"]
        if position is not None:
            if (not isinstance(position, dict) or set(position) != {"event_id", "entry_index"}
                    or type(position["entry_index"]) is not int or not 0 <= position["entry_index"] < 128):
                raise ValueError("基准比较位置无效")
            ObjectId(position["event_id"])
        if raw["last_identity"] is not None and (not isinstance(raw["last_identity"], str) or not raw["last_identity"]):
            raise ValueError("末稳定身份无效")
        if page.entries and raw["last_identity"] != page.entries[-1].identity:
            raise ValueError("末稳定身份与原实际页不符")
        if (len(event.rows) != 1 or event.rows[0].table != "operation_attempts"
                or event.rows[0].row_id != event.evidence["attempt_id"]
                or event.rows[0].after.values.get("result_last_page_event_id") != event.event_id):
            raise ValueError("页引用与原物理尝试不符")
        before, after = event.rows[0].before.values, event.rows[0].after.values
        if raw["page_no"] == 1:
            columns = {"result_first_page_event_id", "result_last_page_event_id"}
            if (set(before) != columns or set(after) != columns
                    or any(value is not None for value in before.values())
                    or any(value != event.event_id for value in after.values())):
                raise ValueError("首结果页没有共同建立原范围")
        else:
            columns = {"result_last_page_event_id"}
            if set(before) != columns or set(after) != columns:
                raise ValueError("后续结果页只能推进原末引用")
            ObjectId(before["result_last_page_event_id"])
            if before["result_last_page_event_id"] >= event.event_id:
                raise ValueError("后续结果页没有沿原历史推进")
        ref = ResultPageRef(ticket, raw["page_no"], event.event_id, page.next_cursor)
        return SavedResultPage(ref, page, event.occurred_at, tuple((identity, file_id) for identity, file_id in file_ids)), raw
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("结果页原历史不可解释") from error


def _at(connection, event_id):
    with closing(connection.execute(f"SELECT {_COLUMNS} FROM history_events WHERE id=?", (event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("原结果页历史缺失")
    event = decode_event_row(row)
    txn = read_transaction_range(connection, event.transaction_id)
    if not txn.first_event_id <= event.event_id <= txn.last_event_id:
        raise ConsistencyError("结果页不属于原事务范围")
    return _decode(event)


def read_pages(ticket, cursor, batch, owned):
    if type(batch) is not int or not 1 <= batch <= 128:
        raise ValueError("结果页读取批次必须为 1—128")
    _, attempt, _, _ = _load(owned.connection, ticket)
    first, last = attempt["result_first_page_event_id"], attempt["result_last_page_event_id"]
    if first is None and last is None:
        if cursor is not None:
            raise ConsistencyError("没有结果页，不能使用继续游标")
        return Page((), None)
    if first is None or last is None or first > last:
        raise ConsistencyError("结果页首末引用无效")
    head, _ = _at(owned.connection, first)
    if head.ref.ticket != ticket or head.ref.page_no != 1:
        raise ConsistencyError("结果页首引用不属于原尝试首批")
    previous = None
    if cursor is not None:
        ObjectId(cursor)
        if not first <= cursor <= last:
            raise ConsistencyError("结果页游标不在原范围内")
        previous, _ = _at(owned.connection, cursor)
        if previous.ref.ticket != ticket:
            raise ConsistencyError("结果页游标属于其他尝试")
    with closing(owned.connection.execute(f"SELECT {_COLUMNS} FROM history_events WHERE id>=? AND id<=?"
            " AND id>? AND event_type=? AND json_extract(body_json,'$.evidence.attempt_id')=?"
            " ORDER BY id LIMIT ?", (first, last, cursor or 0, _TYPE, attempt["id"], batch + 1))) as reader:
        pages = []
        for row in reader:
            event = decode_event_row(row)
            txn = read_transaction_range(owned.connection, event.transaction_id)
            if not txn.first_event_id <= event.event_id <= txn.last_event_id:
                raise ConsistencyError("结果页不属于完整原事务")
            page, raw = _decode(event)
            if page.ref.ticket != ticket or page.ref.page_no != (1 if previous is None else previous.ref.page_no + 1):
                raise ConsistencyError("结果页存在缺口或混入其他轮次")
            if previous is not None and (previous.page.next_cursor is None or previous.page.outcome.error is not None
                                        or page.page.cursor != previous.page.next_cursor):
                raise ConsistencyError("原页范围的游标或结束阶段不连续")
            if previous is not None and event.rows[0].before.values["result_last_page_event_id"] != previous.ref.event_id:
                raise ConsistencyError("结果页的旧末引用不是原上一页")
            pages.append(page)
            previous = page
    if len(pages) <= batch:
        if (previous is None or previous.ref.event_id != last):
            raise ConsistencyError("结果页末引用没有原连续事实")
        return Page(tuple(pages), None)
    return Page(tuple(pages[:batch]), pages[batch - 1].ref.event_id)


def read_page(ref, owned):
    """核对原可靠页引用，只读取它的实际历史输入。"""
    if not isinstance(ref, ResultPageRef):
        raise TypeError("单页读取要求完整原 ResultPageRef")
    ObjectId(ref.event_id)
    _, attempt, _, _ = _load(owned.connection, ref.ticket)
    first, last = attempt["result_first_page_event_id"], attempt["result_last_page_event_id"]
    if first is None or last is None or not first <= ref.event_id <= last:
        raise ConsistencyError("结果页引用不在原尝试可靠范围内")
    saved, _ = _at(owned.connection, ref.event_id)
    if saved.ref != ref:
        raise ConsistencyError("结果页引用改变原票据、批次或后续游标")
    return saved


def read_last_page(ticket, owned):
    """原尝试的可靠末页；无分页输入与不完整引用分别处理。"""
    _, attempt, _, _ = _load(owned.connection, ticket)
    first, last = attempt["result_first_page_event_id"], attempt["result_last_page_event_id"]
    if first is None and last is None:
        return None
    if first is None or last is None or first > last:
        raise ConsistencyError("结果页首末引用无效")
    saved, _ = _at(owned.connection, last)
    if saved.ref.ticket != ticket:
        raise ConsistencyError("结果页末引用不属于原尝试")
    return read_page(saved.ref, owned)


def read_file_input(last_ref, identity, owned):
    """从原页范围查找一个差集成员，不装载全部文件元数据。"""
    last = read_page(last_ref, owned)
    _, attempt, _, _ = _load(owned.connection, last_ref.ticket)
    with closing(owned.connection.execute(
            "SELECT DISTINCT e.id FROM history_events e,"
            " json_each(e.body_json,'$.evidence.result_page.file_ids') f"
            " WHERE e.id>=? AND e.id<=? AND e.event_type=?"
            " AND json_extract(e.body_json,'$.evidence.attempt_id')=?"
            " AND json_extract(f.value,'$[0]')=? ORDER BY e.id LIMIT 2",
            (attempt["result_first_page_event_id"], last.ref.event_id, _TYPE, attempt["id"], identity))) as reader:
        found = reader.fetchall()
    if len(found) != 1:
        raise ConsistencyError("配对原片必须唯一属于原结果页范围")
    saved, _ = _at(owned.connection, found[0][0])
    file_id = dict(saved.file_ids)[identity]
    entry = next(item for item in saved.page.entries if item.identity == identity)
    return entry, file_id


def read_source_inputs(ticket, cursor, batch, owned):
    """跨原核实轮次分页读取已确认来源文件及其最后实际元数据。"""
    if type(batch) is not int or not 1 <= batch <= 128:
        raise ValueError("来源文件批次必须为 1—128")
    if cursor is not None:
        ObjectId(cursor)
    _, _, action, _ = _load(owned.connection, ticket)
    with closing(owned.connection.execute(
            "SELECT id FROM device_files WHERE source_action_id=? AND id>? ORDER BY id LIMIT ?",
            (action["id"], cursor or 0, batch + 1))) as reader:
        identifiers = [row[0] for row in reader]
    items = []
    for file_id in identifiers[:batch]:
        facts = row_facts(owned.connection, "device_files", file_id)
        identity = parse_exact_json(facts["identity_key"])
        if (not isinstance(identity, list) or len(identity) != 3
                or identity[:2] != [action["device_id"], action["driver_id"]]):
            raise ConsistencyError("原来源文件的身份与固定绑定不符")
        with closing(owned.connection.execute(
                "SELECT DISTINCT e.id FROM history_events e,"
                " json_each(e.body_json,'$.evidence.result_page.file_ids') f"
                " WHERE e.event_type=? AND json_extract(e.body_json,'$.evidence.result_page.run_id')=?"
                " AND json_extract(f.value,'$[1]')=? ORDER BY e.id DESC LIMIT 1",
                (_TYPE, ticket.run_id, file_id))) as reader:
            row = reader.fetchone()
        if row is None:
            raise ConsistencyError("已确认来源文件缺少原已保存 RESULTS 页元数据")
        saved, _ = _at(owned.connection, row[0])
        if (saved.ref.ticket.run_id != ticket.run_id or (identity[2], file_id) not in saved.file_ids):
            raise ConsistencyError("来源文件的原页输入与物理身份不符")
        entry = next(item for item in saved.page.entries if item.identity == identity[2])
        if not json_equal(entry.locator, facts["locator_json"]):
            raise ConsistencyError("原 RESULTS 定位与来源文件事实矛盾")
        ownership = facts["ownership_evidence_json"]
        if not isinstance(ownership, dict) or not isinstance(ownership.get("observation"), dict):
            raise ConsistencyError("来源文件缺少可靠归属依据")
        complete = facts["completion_state"] == 3
        if complete and not isinstance(facts["completion_evidence_json"], dict):
            raise ConsistencyError("来源文件缺少可靠完成依据")
        paired_id = facts["original_device_file_id"]
        if paired_id is not None:
            original = row_facts(owned.connection, "device_files", paired_id)
            if (original is None or original["source_action_id"] != action["id"] or original["role"] != int(_ROLE.ORIGINAL)
                    or facts["role"] != int(_ROLE.PREVIEW) or entry.paired_identity != parse_exact_json(original["identity_key"])[2]):
                raise ConsistencyError("来源文件的原配对与可靠同源原片不符")
        elif entry.paired_identity is not None:
            raise ConsistencyError("来源文件的原配对尚未可靠保存")
        entry = replace(entry, complete=complete, size_bytes=facts["size_bytes"] if complete else None)
        file = CaptureFile(entry.identity, entry.kind, complete, ownership_confirmed=True,
            format_id=entry.format_id, pairing_confirmed=True if paired_id is not None else None)
        items.append((entry, file, file_id))
    return Page(tuple(items), identifiers[batch - 1] if len(identifiers) > batch else None)


class SaveResultPageCommand:
    def __init__(self, request, key):
        if not isinstance(request, ResultPageSave):
            raise TypeError("结果页要求完整 ResultPageSave")
        self.request, self.key = request, key

    def plan(self, scope):
        request = self.request
        run, attempt, action, activity = _load(scope.connection, request.ticket)
        owner = ("action", action["id"])
        state = {"operation_runs": {run["id"]: run}, "operation_attempts": {attempt["id"]: attempt},
                 "actions": {action["id"]: action}, "device_activities": {activity["id"]: activity},
                 "device_files": {}, "outputs": {}}
        owners = {("operation_attempts", attempt["id"]): owner}
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            if not saved or saved[-1]["type"] != _TYPE or saved[-1]["reason"] != _APPEND:
                raise TransactionError("原键不是结果页事务")
            event = saved[-1]
            raw = event["body"]["evidence"]["result_page"]
            expected = result_page_input(request)
            if (event["occurred_at"] != request.occurred_at or event["body"]["evidence"]["attempt_id"] != attempt["id"]
                    or any(not json_equal(raw.get(name), value) for name, value in expected.items())):
                raise TransactionError("原结果页的完整输入、时刻或尝试不同")
            original, _ = _at(scope.connection, event["event_id"])
            for discovery in saved[:-1]:
                if (discovery["type"] != _FILE_EVENT["id"] or discovery["reason"] != _FILE_EVENT["branches"]["CREATE"]["reason"]
                        or discovery["occurred_at"] != request.occurred_at):
                    raise TransactionError("原结果页事务混入其他事实")
                rows = discovery["body"]["rows"]
                if (len(rows) != 1 or rows[0]["table"] != "device_files" or rows[0]["before"]["exists"]
                        or [rows[0]["after"]["values"]["identity_key"], rows[0]["id"]] not in [
                            [file_identity_key(action["device_id"], action["driver_id"], identity), file_id]
                            for identity, file_id in original.file_ids]):
                    raise TransactionError("原首次发现不属于本页原输入")
            return CommandPlan((), owners, state, result=original.ref, read_only=True)
        if attempt["status"] != int(enum_for("operation_attempts.status").RUNNING):
            raise ConsistencyError("已结束尝试不能再追加结果页")
        page = page_from_outcome(request.ticket, request.outcome.outcome, cursor=request.cursor,
                                 binding=DeviceBinding(action["device_id"], action["driver_id"]))
        tail, previous = None, None
        if attempt["result_last_page_event_id"] is not None:
            tail, previous = _at(scope.connection, attempt["result_last_page_event_id"])
            if tail.ref.ticket != request.ticket:
                raise ConsistencyError("原末页属于其他活动或尝试")
        if request.page_no != (1 if tail is None else tail.ref.page_no + 1):
            raise ConsistencyError("结果页必须沿原轮次连续推进")
        if tail is None:
            if request.cursor is not None:
                raise ConsistencyError("本轮首批必须从空游标开始")
        elif tail.page.next_cursor is None or tail.page.outcome.error is not None or request.cursor != tail.page.next_cursor:
            raise ConsistencyError("结果页不能越过末页、实际错误或原游标")
        if activity["ownership_mode"] == _OWNERSHIP.BASELINE_COMPARISON:
            entries, baseline_position, last_identity = _difference(scope, activity, action, page, previous)
        else:
            entries = page.entries
            if len({entry.identity for entry in entries}) != len(entries):
                raise ConsistencyError("一次结果页不能重复列出同一稳定身份")
            baseline_position = None
            last_identity = entries[-1].identity if entries else None if previous is None else previous["last_identity"]
        from .capture import file_discovery_row
        file_ids, creates = [], []
        next_id = next_row_id(scope.connection, "device_files")
        for entry in entries:
            command = FileObservationSave(action["id"], entry.identity, entry.locator, request.occurred_at,
                                          entry.original_name, entry.media_type)
            key = file_identity_key(action["device_id"], action["driver_id"], entry.identity)
            with closing(scope.connection.execute("SELECT id FROM device_files WHERE identity_key=?", (key,))) as reader:
                found = reader.fetchone()
            if found is None:
                file_id = next_id
                next_id += 1
                row = file_discovery_row(command, file_id, key)
                creates.append(row)
                state["device_files"][file_id] = {"id": file_id, **row.after.values}
                owners[("device_files", file_id)] = ("device_file", file_id)
            else:
                file_id = found[0]
                facts = row_facts(scope.connection, "device_files", file_id)
                if not json_equal(entry.locator, facts["locator_json"]):
                    raise ConsistencyError("原结果页定位与已登记文件矛盾")
                if facts["source_action_id"] not in (None, action["id"]):
                    raise ConsistencyError("本次结果页不能引用已属于其他动作的文件")
                state["device_files"][file_id] = facts
            file_ids.append([entry.identity, file_id])
        allocation = scope.allocate(len(creates) + 1)
        events = [event_envelope(allocation.first_event_id + i, allocation.txn_id, _FILE_EVENT["id"],
                                _FILE_EVENT["branches"]["CREATE"]["reason"], (row,), request.occurred_at)
                  for i, row in enumerate(creates)]
        event_id = allocation.last_event_id
        before = {name: attempt[name] for name in ("result_first_page_event_id", "result_last_page_event_id")}
        after = {"result_first_page_event_id": before["result_first_page_event_id"] or event_id,
                 "result_last_page_event_id": event_id}
        raw = {**result_page_input(request), "baseline_position": baseline_position,
               "last_identity": last_identity, "file_ids": file_ids}
        events.append(event_envelope(event_id, allocation.txn_id, _TYPE, _APPEND,
            (update_change("operation_attempts", attempt["id"], before, after),), request.occurred_at,
            {"attempt_id": attempt["id"], "result_page": raw}))
        return CommandPlan(tuple(events), owners, state,
                           result=ResultPageRef(request.ticket, request.page_no, event_id, page.next_cursor))


def _page_guard(event, context):
    try:
        page, raw = _decode(event)
        attempt = context.state_rows.get("operation_attempts", {}).get(event.evidence["attempt_id"])
        run = None if attempt is None else context.state_rows.get("operation_runs", {}).get(attempt["run_id"])
        if (attempt is None or run is None or attempt["status"] != _ATTEMPT_STATUS.RUNNING or run["kind"] != _RUN_KIND.CHECK_CAPTURE_RESULTS
                or run["id"] != page.ref.ticket.run_id or run["activity_id"] != int(page.ref.ticket.target_id)
                or attempt["attempt_no"] != page.ref.ticket.attempt_id):
            raise ConsistencyError("结果页的原活动、流程与运行中尝试不符")
        action = context.state_rows.get("actions", {}).get(run["action_id"])
        activity = context.state_rows.get("device_activities", {}).get(run["activity_id"])
        if action is None or activity is None or activity["action_id"] != action["id"]:
            raise ConsistencyError("结果页缺少原动作与活动")
        if activity["ownership_mode"] == _OWNERSHIP.TASK_SCOPE:
            if raw["baseline_position"] is not None or {identity for identity, _ in page.file_ids} != {entry.identity for entry in page.page.entries}:
                raise ConsistencyError("任务独立范围页必须保持全部原文件引用且没有基准位置")
        else:
            if activity["baseline_state"] != enum_for("device_activities.baseline_state").FIXED:
                raise ConsistencyError("差集结果页需要原固定基准")
            position = raw["baseline_position"]
            if position is not None and not activity["baseline_first_event_id"] <= position["event_id"] <= activity["baseline_last_event_id"]:
                raise ConsistencyError("差集比较位置不在原固定基准范围")
        for identity, file_id in page.file_ids:
            facts = context.state_rows.get("device_files", {}).get(file_id)
            if (facts is None or facts["identity_key"] != file_identity_key(action["device_id"], action["driver_id"], identity)
                    or facts["source_action_id"] not in (None, action["id"])):
                raise ConsistencyError("原页文件引用与可靠文件身份或归属不同")
    except (KeyError, TypeError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


def register_result_page_guard():
    register_guard("result_page", _page_guard)
