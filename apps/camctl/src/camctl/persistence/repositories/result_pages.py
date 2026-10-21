"""结果页保留原完整实际结果，页范围与首次文件发现共同提交。"""
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

from camctl.capture.files import FileObservationSave, file_identity_key
from camctl.capture.result_inputs import observed_file, page_from_outcome, saved_outcome
from camctl.capture.result_pages import ResultPageSave, ResultPageRef, SavedResultPage, result_page_input
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.capture.results import CaptureFile
from camctl.capture.baseline_models import BaselineRef, BaselineState
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.contracts.history_values import HistoryBoundary
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.evidence import RESULT_LISTED_TYPE, RESULT_LISTED_VERSION
from camctl.devices.file_identity import FileIdentity
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.operations.models import AttemptTicket
from camctl.persistence.transaction import (CommandPlan, TransactionError,
    event_envelope, next_row_id, read_transaction_range, row_change, row_facts,
    saved_transaction_events, update_change)
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.history.reads import ResultPageWriteScope
from .baseline import read_reference, read_chunks, _chunk_at, _check_entries, _directories

_EVENT = load_event_registry()["events"]["RESULT_PAGE"]
_TYPE = _EVENT["id"]
_APPEND = _EVENT["branches"]["APPEND"]["reason"]
_INDEX_FILES = _EVENT["branches"]["INDEX_FILES"]["reason"]
_FILE_EVENT = load_event_registry()["events"]["DEVICE_FILE_OBSERVED"]
_COLUMNS = "id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
_PAYLOAD_FIELDS = {"run_id", "attempt_no", "activity_id", "page_no", "cursor", "outcome", "baseline_position", "last_identity", "file_ids"}
_OWNERSHIP = enum_for("device_activities.ownership_mode")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_RUN_KIND = enum_for("operation_runs.kind")
_ROLE = enum_for("device_files.role")
_FAMILY_ROLE = enum_for("result_page_files.family_role")
_BASELINE_EVENT = load_event_registry()["events"]["BASELINE_CHUNK"]
_BASELINE_TYPE = _BASELINE_EVENT["id"]
_BASELINE_FIX = _BASELINE_EVENT["branches"]["FIX"]["reason"]
_BASELINE_FIX_EMPTY = _BASELINE_EVENT["branches"]["FIX_EMPTY"]["reason"]


def capture_result_scope(action_id, ticket, last_page_event_id):
    """原单次扫描的只读上界，由所有集合与完成消费者共同使用。"""
    from camctl.history.reads import CaptureResultReadScope
    return CaptureResultReadScope(action_id, int(ticket.target_id), ticket.run_id,
        ticket.attempt_id, last_page_event_id)


def iter_result_file_facts(reader, action, ticket, last_page_event_id):
    """逐页交付当前新增候选和可靠文件事实；不混用其他轮次的文件。"""
    if reader is None or reader.scope != capture_result_scope(action['id'], ticket, last_page_event_id):
        raise ConsistencyError('结果文件核实缺少原有界扫描范围')
    cursor = None
    while True:
        pages = reader.result_pages(ticket, cursor, 1)
        for saved in pages.items:
            ids = dict(saved.file_ids)
            for entry in saved.page.entries:
                if entry.identity in ids:
                    yield entry, reader.required('device_files', ids[entry.identity])
        if pages.next_cursor is None:
            if not pages.items or pages.items[-1].ref.event_id != last_page_event_id:
                raise ConsistencyError('当前结果文件核实缺少原可靠末页')
            return
        cursor = pages.next_cursor


def verify_output_scope_ownership(reader, action, ticket, last_page_event_id):
    """当前新增必要成员必须可靠归属；明确可选预览只留发现也可确定范围。"""
    from camctl.capture.results import ProductRule, is_optional_preview
    rules = tuple(ProductRule.from_json(value) for value in action['execution_spec_json'].get('product_rules', ()))
    for entry, facts in iter_result_file_facts(reader, action, ticket, last_page_event_id):
        if (facts['source_action_id'] == action['id'] and facts['ownership_evidence_json'] is not None):
            continue
        if (facts['source_action_id'] is None and is_optional_preview(
                entry.kind, entry.format_id, entry.paired_identity, rules)):
            continue
        raise ConsistencyError('原完整扫描仍有未确认归属的新增必要文件')


class _BaselineCursor:
    """只保留正在比较的一批；已比较部分由原页位置恢复。"""

    def __init__(self, scope, activity, action, position, *, first):
        self.scope = scope
        historical_ref = getattr(scope, "result_page_baseline_ref", None)
        self.ref = (historical_ref(activity, action) if historical_ref is not None
                    else read_reference(activity["id"], scope))
        self.chunk, self.index = None, 0
        if first:
            pages = _read_baseline_chunks(self.ref, None, scope)
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
            pages = _read_baseline_chunks(self.ref, self.chunk.event_id, self.scope)
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


def _read_baseline_chunks(ref, cursor, scope):
    historical_reader = getattr(scope, "result_page_baseline_chunks", None)
    return (historical_reader(ref, cursor, 1) if historical_reader is not None
            else read_chunks(ref, cursor, 1, scope))


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


def _family_fields(identity):
    """保留原路径与主干码点，只按OSV/LRF后缀派生有限族字段。"""
    directory, separator, name = identity.rpartition("/")
    if separator and not directory:
        directory = "/"
    stem, dot, suffix = name.rpartition(".")
    if not separator or not directory or not dot or not stem:
        return None, None, None
    suffix = suffix.lower()
    if suffix == "osv":
        role = int(_FAMILY_ROLE.ORIGINAL)
    elif suffix == "lrf":
        role = int(_FAMILY_ROLE.PREVIEW)
    else:
        return None, None, None
    return directory, stem, role


def _relation_values(*, created_event_id, action_id, run_id, attempt_id,
                     page_event_id, file_id, identity):
    directory, stem, role = _family_fields(identity)
    return {
        "created_event_id": created_event_id,
        "action_id": action_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "page_event_id": page_event_id,
        "file_id": file_id,
        "identity": identity,
        "family_directory": directory,
        "family_stem": stem,
        "family_role": role,
    }


def _verify_page_relations(connection, saved, raw, event, transaction):
    """按完整原事务核对 APPEND 与 INDEX_FILES 的双向精确关系。"""
    from .capture import file_discovery_row

    attempt_id = event.evidence["attempt_id"]
    with closing(connection.execute(
            "SELECT r.action_id,r.activity_id,r.id,a.run_id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE a.id=?", (attempt_id,))) as cursor:
        owner = cursor.fetchone()
    if owner is None:
        raise ConsistencyError("结果页关系缺少原活动、动作或流程")
    action_id, activity_id, run_id, joined_run_id = owner
    if joined_run_id != raw["run_id"] or activity_id != raw["activity_id"]:
        raise ConsistencyError("结果页关系改变原流程、活动或尝试")
    action = row_facts(connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("结果页动作不存在")
    append_id = event.event_id
    with closing(connection.execute(
            f"SELECT {_COLUMNS} FROM history_events WHERE transaction_id=? ORDER BY id LIMIT 131",
            (transaction.txn_id,))) as cursor:
        transaction_events = [decode_event_row(row) for row in cursor]
    if (len(transaction_events) != transaction.last_event_id - transaction.first_event_id + 1
            or len(transaction_events) > 130
            or any(item.event_id != transaction.first_event_id + index
                   or item.transaction_id != transaction.txn_id
                   for index, item in enumerate(transaction_events))):
        raise ConsistencyError("结果页所在原事务事件范围不连续或超过页组容量")
    append_position = next((index for index, item in enumerate(transaction_events)
                            if item.event_id == append_id), None)
    if append_position is None or transaction_events[append_position] != event:
        raise ConsistencyError("原页APPEND不在其完整历史事务内")
    create_events = transaction_events[:append_position]
    create_type = _FILE_EVENT["id"]
    create_reason = _FILE_EVENT["branches"]["CREATE"]["reason"]
    page_entries = {entry.identity: entry for entry in saved.page.entries}
    file_ids = tuple(saved.file_ids)
    expected_created = []
    for identity, file_id in file_ids:
        facts = row_facts(connection, "device_files", file_id)
        if facts is None or facts["identity_key"] != file_identity_key(
                action["device_id"], action["driver_id"], identity):
            raise ConsistencyError("原页文件ID与完整身份登记不符")
        if transaction.first_event_id <= facts["created_event_id"] <= transaction.last_event_id:
            expected_created.append((identity, file_id, facts["created_event_id"]))
        elif facts["created_event_id"] >= transaction.first_event_id:
            raise ConsistencyError("原页文件首次发现身份越过事务末位")
    expected_created_ids = [(identity, file_id, created_id)
                            for identity, file_id, created_id in expected_created]
    actual_created = []
    for create in create_events:
        validate_event_structure(create)
        if ((create.event_type, create.reason) != (create_type, create_reason)
                or create.occurred_at != event.occurred_at or len(create.rows) != 1):
            raise ConsistencyError("结果页APPEND前包含非单行首次发现事件")
        row = create.rows[0]
        if row.table != "device_files" or row.before.exists or not row.after.exists:
            raise ConsistencyError("结果页首次发现事件改变了其他文件事实")
        try:
            identity_key = parse_exact_json(row.after.values["identity_key"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("原首次发现身份不可解释") from error
        identity = identity_key[2] if isinstance(identity_key, list) and len(identity_key) == 3 else None
        file_id = dict(file_ids).get(identity)
        entry = page_entries.get(identity)
        if file_id is None or entry is None or row.row_id != file_id:
            raise ConsistencyError("原首次发现不是APPEND的实际差集身份")
        command = FileObservationSave(action_id, identity, entry.locator, event.occurred_at,
            entry.original_name, entry.media_type)
        expected_row = file_discovery_row(command, file_id,
            file_identity_key(action["device_id"], action["driver_id"], identity))
        if dict(row.after.values) != dict(expected_row.after.values):
            raise ConsistencyError("原首次发现没有精确保存该页的原文件事实")
        if facts := row_facts(connection, "device_files", file_id):
            if facts["created_event_id"] != create.event_id:
                raise ConsistencyError("原首次发现的持久文件创建事件身份不同")
        actual_created.append((identity, file_id, create.event_id))
    expected_create_order = [item for item in file_ids if any(
        identity == item[0] and file_id == item[1] for identity, file_id, _ in expected_created_ids)]
    if actual_created != [(identity, file_id, created_id) for identity, file_id, created_id in expected_created_ids]:
        raise ConsistencyError("结果页CREATE前缀没有精确覆盖本页首次发现")
    if [(identity, file_id) for identity, file_id, _ in actual_created] != expected_create_order:
        raise ConsistencyError("结果页CREATE前缀没有保持APPEND差集顺序")
    if any(item.event_id < append_id for item in transaction_events[:append_position]
           if item.event_type != create_type):
        raise ConsistencyError("结果页CREATE前缀混入其他事件")
    with closing(connection.execute(
            "SELECT id FROM history_events WHERE transaction_id=? AND event_type=?"
            " AND json_extract(body_json,'$.reason')=? ORDER BY id",
            (transaction.txn_id, _TYPE, _APPEND))) as cursor:
        append_rows = cursor.fetchmany(2)
    if len(append_rows) != 1 or append_rows[0][0] != append_id:
        raise ConsistencyError("原事务没有唯一RESULT_PAGE.APPEND")
    with closing(connection.execute(
            "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
            " FROM history_events WHERE transaction_id=? AND event_type=?"
            " AND json_extract(body_json,'$.reason')=? ORDER BY id",
            (transaction.txn_id, _TYPE, _INDEX_FILES))) as cursor:
        index_rows = [decode_event_row(row) for row in cursor]
    with closing(connection.execute(
            "SELECT id,created_event_id,action_id,run_id,attempt_id,page_event_id,file_id,identity,"
            "family_directory,family_stem,family_role FROM result_page_files"
            " WHERE page_event_id=? ORDER BY id", (append_id,))) as cursor:
        relations = cursor.fetchmany(128)
        extra_relation = cursor.fetchone()
    if extra_relation is not None:
        raise ConsistencyError("原页INDEX_FILES关系超过单页容量")
    file_ids = tuple(saved.file_ids)
    if not file_ids:
        if index_rows or relations or transaction.last_event_id != append_id:
            raise ConsistencyError("零差集页包含关系或非规范尾事件")
        return
    if len(index_rows) != 1 or len(relations) != len(file_ids):
        raise ConsistencyError("原页与INDEX_FILES关系数量不完整或存在额外成员")
    index = index_rows[0]
    if (index.event_id != transaction.last_event_id or index.event_id != append_id + 1
            or index.occurred_at != event.occurred_at
            or index.evidence != {"result_page_index": {"page_event_id": append_id}}):
        raise ConsistencyError("原页INDEX_FILES身份、证据、时刻或事务位置不符")
    validate_event_structure(index)
    expected_rows = []
    for pair in file_ids:
        identity, file_id = pair
        expected_rows.append((
            index.event_id, action_id, run_id, attempt_id, append_id,
            file_id, identity, *_family_fields(identity)))
    relation_shape = [row[1:] for row in relations]
    if relation_shape != expected_rows:
        raise ConsistencyError("原页关系与实际APPEND差集身份、文件或族字段不符")
    if (len(index.rows) != len(expected_rows)
            or any(row.table != "result_page_files" or row.before.exists for row in index.rows)):
        raise ConsistencyError("INDEX_FILES行形状与原页关系不符")
    for relation, row in zip(relations, index.rows):
        relation_id = relation[0]
        expected = dict(zip(("created_event_id", "action_id", "run_id", "attempt_id",
                             "page_event_id", "file_id", "identity", "family_directory",
                             "family_stem", "family_role"), relation[1:]))
        if row.row_id != relation_id or dict(row.after.values) != expected:
            raise ConsistencyError("INDEX_FILES正文与持久关系行不符")


def _at_record(connection, event_id):
    with closing(connection.execute(f"SELECT {_COLUMNS} FROM history_events WHERE id=?", (event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("原结果页历史缺失")
    event = decode_event_row(row)
    txn = read_transaction_range(connection, event.transaction_id)
    if not txn.first_event_id <= event.event_id <= txn.last_event_id:
        raise ConsistencyError("结果页不属于原事务范围")
    saved, raw = _decode(event)
    try:
        _verify_page_relations(connection, saved, raw, event, txn)
    except EventValidationError as error:
        raise ConsistencyError("结果页关系事件不可解释") from error
    return saved, raw, event, txn


def _at(connection, event_id):
    saved, raw, _, _ = _at_record(connection, event_id)
    return saved, raw


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
    head, head_raw, head_event, head_txn = _at_record(owned.connection, first)
    if head.ref.ticket != ticket or head.ref.page_no != 1:
        raise ConsistencyError("结果页首引用不属于原尝试首批")
    previous = None
    verified = {first: (head, head_raw, head_event, head_txn)}
    if cursor is not None:
        ObjectId(cursor)
        if not first <= cursor <= last:
            raise ConsistencyError("结果页游标不在原范围内")
        previous, previous_raw, previous_event, previous_txn = _at_record(
            owned.connection, cursor)
        verified[cursor] = (previous, previous_raw, previous_event, previous_txn)
        if previous.ref.ticket != ticket:
            raise ConsistencyError("结果页游标属于其他尝试")
    with closing(owned.connection.execute(f"SELECT {_COLUMNS} FROM history_events WHERE id>=? AND id<=?"
            " AND id>? AND event_type=? AND json_extract(body_json,'$.reason')=?"
            " AND json_extract(body_json,'$.evidence.attempt_id')=?"
            " ORDER BY id LIMIT ?", (first, last, cursor or 0, _TYPE, _APPEND, attempt["id"], batch + 1))) as reader:
        pages = []
        for row in reader:
            event_id = row[0]
            saved_record = verified.get(event_id)
            if saved_record is None:
                event = decode_event_row(row)
                txn = read_transaction_range(owned.connection, event.transaction_id)
                if not txn.first_event_id <= event.event_id <= txn.last_event_id:
                    raise ConsistencyError("结果页不属于完整原事务")
                page, raw = _decode(event)
                try:
                    _verify_page_relations(owned.connection, page, raw, event, txn)
                except EventValidationError as error:
                    raise ConsistencyError("结果页关系事件不可解释") from error
            else:
                page, raw, event, txn = saved_record
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


def read_page_at(event_id, owned):
    """原事件解码取得票据，再复用单页读取的可靠范围校验。"""
    ObjectId(event_id)
    saved, _ = _at(owned.connection, event_id)
    return read_page(saved.ref, owned)


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


def read_file_input(last_ref, identity, owned, *, allow_missing=False):
    """从原页范围查找一个差集成员，不装载全部文件元数据。"""
    last = read_page(last_ref, owned)
    _, attempt, _, _ = _load(owned.connection, last_ref.ticket)
    with closing(owned.connection.execute(
            "SELECT DISTINCT e.id FROM history_events e,"
            " json_each(e.body_json,'$.evidence.result_page.file_ids') f"
            " WHERE e.id>=? AND e.id<=? AND e.event_type=?"
            " AND json_extract(e.body_json,'$.reason')=?"
            " AND json_extract(e.body_json,'$.evidence.attempt_id')=?"
            " AND json_extract(f.value,'$[0]')=? ORDER BY e.id LIMIT 2",
            (attempt["result_first_page_event_id"], last.ref.event_id, _TYPE, _APPEND,
             attempt["id"], identity))) as reader:
        found = reader.fetchmany(2)
    if not found and allow_missing:
        return None
    if len(found) != 1:
        raise ConsistencyError("配对原片必须唯一属于原结果页范围")
    saved, _ = _at(owned.connection, found[0][0])
    file_id = dict(saved.file_ids)[identity]
    entry = next(item for item in saved.page.entries if item.identity == identity)
    return entry, file_id


def _first_complete_event(connection, facts):
    """原首次完成事件固定 proof 与大小，不采用发现条目的元信息。"""
    with closing(connection.execute(
            f"SELECT {','.join('e.' + column for column in _COLUMNS.split(','))}"
            " FROM history_events e,json_each(e.body_json,'$.rows') r"
            " WHERE e.event_type=? AND e.id>0"
            " AND json_extract(r.value,'$.table')='device_files' AND json_extract(r.value,'$.id')=?"
            " AND json_extract(r.value,'$.after.values.completion_state')=3"
            " AND json_extract(r.value,'$.before.values.completion_state')<>3 ORDER BY e.id LIMIT 1",
            (_FILE_EVENT['id'], facts['id']))) as reader:
        row = reader.fetchone()
    if row is None:
        raise ConsistencyError('完整来源文件缺少首次可靠完成事件')
    try:
        event = decode_event_row(row)
        name, branch, _ = validate_event_structure(event)
        transaction = read_transaction_range(connection, event.transaction_id)
        changes = [value for value in event.rows if value.table == 'device_files' and value.row_id == facts['id']]
        if (name != 'DEVICE_FILE_OBSERVED' or branch != 'COMPLETE' or len(changes) != 1
                or not transaction.first_event_id <= event.event_id <= transaction.last_event_id):
            raise ValueError('首次完成不属于原完整文件事务')
        change = changes[0]
        if (not change.before.exists or change.before.values.get('completion_state') == 3
                or change.after.values.get('completion_state') != 3
                or not json_equal(change.after.values.get('completion_evidence_json'), facts['completion_evidence_json'])
                or not json_equal(change.after.values.get('size_bytes'), facts['size_bytes'])):
            raise ValueError('首次完成事件改变原可靠完成依据或固定大小')
    except (KeyError, TypeError, ValueError, EventValidationError) as error:
        raise ConsistencyError(f'完整来源的首次完成事件无效: {error}') from error
    return event


def _legacy_source_entry(connection, ticket, identity, facts, activity, *, completion_event=None):
    """按单文件读取原 v1 条目，不装载其他轮次的完整文件目录。"""
    from .capture import _ATTEMPT_RESULT_EVENT

    from .result_sources import ResultSourceKind, load_result_source

    attempt_no = None
    complete = facts['completion_state'] == 3
    if complete:
        completed = completion_event or _first_complete_event(connection, facts)
        # 下一 I 之前必须已解决原文件子责任；R 可在 F 任一侧。
        with closing(connection.execute(
                'SELECT attempt_no,result_first_page_event_id,result_last_page_event_id FROM operation_attempts'
                ' WHERE run_id=? AND intent_event_id<? ORDER BY intent_event_id DESC LIMIT 1',
                (ticket.run_id, completed.event_id))) as reader:
            owner = reader.fetchone()
        if owner is None or owner[1] is not None or owner[2] is not None:
            return None
        attempt_no = owner[0]
        load_result_source(connection, replace(ticket, attempt_id=attempt_no))
    # 原 v1 结果仍按原字段解释；只把匹配文件的条目交给 Python。
    with closing(connection.execute(
        "SELECT f.value,t.attempt_no,e.occurred_at FROM operation_attempts t JOIN history_events e ON e.id=t.result_event_id,"
        " json_each(t.result_json,'$.observations') o, json_each(o.value,'$.data.entries') f,"
        " json_each(e.body_json,'$.rows') r"
        " WHERE t.run_id=? AND e.event_type=? AND (? IS NULL OR t.attempt_no=?)"
        " AND json_extract(o.value,'$.type')=?"
        " AND json_extract(o.value,'$.version')=?"
        " AND json_extract(o.value,'$.data.activity_id')=?"
        " AND json_extract(f.value,'$.identity')=?"
        " AND json_extract(r.value,'$.table')='operation_attempts'"
        " AND json_extract(r.value,'$.id')=t.id"
        " AND json_extract(r.value,'$.after.values.status')=t.status"
        " AND json_extract(r.value,'$.after.values.effect_state')=t.effect_state"
        " AND json(json_extract(r.value,'$.after.values.result_json'))=json(t.result_json)"
        " AND json(json_extract(r.value,'$.after.values.error_json')) IS json(t.error_json)"
        " ORDER BY t.attempt_no", (ticket.run_id, _ATTEMPT_RESULT_EVENT, attempt_no, attempt_no,
            RESULT_LISTED_TYPE, RESULT_LISTED_VERSION, ticket.target_id, identity))) as reader:
        for row in reader:
            source = load_result_source(connection, replace(ticket, attempt_id=row[1]))
            if (source.natural_lower_bound is not None and row[2] < source.natural_lower_bound
                    or source.kind in (ResultSourceKind.UNCONFIRMED, ResultSourceKind.UNANCHORED) and source.requires_file_wait):
                continue
            entry = observed_file(parse_exact_json(row[0]))
            if (json_equal(entry.locator, facts["locator_json"])
                    and (not complete or entry.complete and entry.size_bytes == facts['size_bytes'])):
                return entry
    if complete:
        raise ConsistencyError('原完成尝试缺少同身份、定位及固定大小的完整 v1 条目')
    return None



def _complete_source_entry(connection, ticket, identity, file_id, facts, activity):
    """无页引用的旧 v2 完成观察按原完整条目匹配；旧 v1 继续原契约。"""
    from .result_sources import ResultSourceKind, load_result_source
    completed = _first_complete_event(connection, facts)
    legacy = _legacy_source_entry(connection, ticket, identity, facts, activity, completion_event=completed)
    if legacy is not None:
        return legacy
    observation = facts["completion_evidence_json"].get("observation")
    with closing(connection.execute(
            "SELECT DISTINCT e.id FROM history_events e,"
            " json_each(e.body_json,'$.evidence.result_page.file_ids') f"
            " WHERE e.event_type=? AND json_extract(e.body_json,'$.reason')=?"
            " AND json_extract(e.body_json,'$.evidence.result_page.run_id')=?"
            " AND json_extract(e.body_json,'$.evidence.result_page.activity_id')=?"
            " AND json_extract(f.value,'$[0]')=? AND json_extract(f.value,'$[1]')=? ORDER BY e.id",
            (_TYPE, _APPEND, ticket.run_id, int(ticket.target_id), identity, file_id))) as reader:
        for event_id, in reader:
            saved, _ = _at(connection, event_id)
            entry = next(item for item in saved.page.entries if item.identity == identity)
            if not json_equal(entry.evidence, observation):
                continue
            source = load_result_source(connection, saved.ref.ticket)
            if (source.natural_lower_bound is not None and saved.occurred_at < source.natural_lower_bound
                    or source.kind in (ResultSourceKind.UNCONFIRMED, ResultSourceKind.UNANCHORED) and source.requires_file_wait):
                continue
            return entry
    raise ConsistencyError("完整来源文件缺少与原完成观察一致的合格原条目")


def read_source_inputs(ticket, cursor, batch, owned):
    """跨原核实轮次分页读取可靠文件；完整文件保持原完成页元数据。"""
    if type(batch) is not int or not 1 <= batch <= 128:
        raise ValueError("来源文件批次必须为 1—128")
    if cursor is not None:
        ObjectId(cursor)
    _, _, action, activity = _load(owned.connection, ticket)
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
                " WHERE e.event_type=? AND json_extract(e.body_json,'$.reason')=?"
                " AND json_extract(e.body_json,'$.evidence.result_page.run_id')=?"
                " AND json_extract(f.value,'$[1]')=? ORDER BY e.id DESC LIMIT 1",
                (_TYPE, _APPEND, ticket.run_id, file_id))) as reader:
            row = reader.fetchone()
        completion = facts["completion_evidence_json"]
        if facts["completion_state"] == 3 and not isinstance(completion, dict):
            raise ConsistencyError("来源文件缺少可靠完成依据")
        source_id = (completion.get("result_page_event_id") if facts["completion_state"] == 3
            and isinstance(completion, dict) else None)
        entry = (_complete_source_entry(owned.connection, ticket, identity[2], file_id, facts, activity)
            if facts["completion_state"] == 3 and source_id is None else
            _legacy_source_entry(owned.connection, ticket, identity[2], facts, activity) if row is None else None)
        if entry is None:
            if row is None:
                raise ConsistencyError("已确认来源文件缺少可靠原 RESULTS 元数据")
            # 后轮实际页可不合格；不能覆盖既有完整文件的原来源元数据。
            saved, _ = _at(owned.connection, row[0] if source_id is None else source_id)
            if (saved.ref.ticket.run_id != ticket.run_id or saved.ref.ticket.target_id != ticket.target_id
                    or (identity[2], file_id) not in saved.file_ids):
                raise ConsistencyError("来源文件的原页输入与物理身份不符")
            entry = next(item for item in saved.page.entries if item.identity == identity[2])
        if not json_equal(entry.locator, facts["locator_json"]):
            raise ConsistencyError("原 RESULTS 定位与来源文件事实矛盾")
        ownership = facts["ownership_evidence_json"]
        if not isinstance(ownership, dict) or not isinstance(ownership.get("observation"), dict):
            raise ConsistencyError("来源文件缺少可靠归属依据")
        complete = facts["completion_state"] == 3
        if complete and (not entry.complete or entry.size_bytes != facts['size_bytes']):
            raise ConsistencyError('完成元信息没有原完整条目及固定大小，不能升级发现观察')
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
            format_id=entry.format_id, pairing_confirmed=True if paired_id is not None else None,
            size_bytes=entry.size_bytes)
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
            append_events = [item for item in saved
                             if item["type"] == _TYPE and item["reason"] == _APPEND]
            index_events = [item for item in saved
                            if item["type"] == _TYPE and item["reason"] == _INDEX_FILES]
            if len(append_events) != 1:
                raise TransactionError("原键不是结果页事务")
            event = append_events[0]
            raw = event["body"]["evidence"]["result_page"]
            expected = result_page_input(request)
            if (event["occurred_at"] != request.occurred_at or event["body"]["evidence"]["attempt_id"] != attempt["id"]
                    or any(not json_equal(raw.get(name), value) for name, value in expected.items())):
                raise TransactionError("原结果页的完整输入、时刻或尝试不同")
            original, original_raw, original_event, transaction = _at_record(
                scope.connection, event["event_id"])
            event_index = saved.index(event)
            discoveries = saved[:event_index]
            if (any(item["type"] != _FILE_EVENT["id"]
                    or item["reason"] != _FILE_EVENT["branches"]["CREATE"]["reason"]
                    or item["occurred_at"] != request.occurred_at for item in discoveries)
                    or event_index + 1 + len(index_events) != len(saved)
                    or (bool(original.file_ids) != (len(index_events) == 1))
                    or (index_events and (index_events[0] != saved[-1]
                        or index_events[0]["occurred_at"] != request.occurred_at))):
                raise TransactionError("原结果页事务没有CREATE→APPEND→可选INDEX_FILES规范顺序")
            actual_creates = []
            page_entries = {entry.identity: entry for entry in original.page.entries}
            original_file_ids = dict(original.file_ids)
            for discovery in discoveries:
                if (discovery["type"] != _FILE_EVENT["id"] or discovery["reason"] != _FILE_EVENT["branches"]["CREATE"]["reason"]
                        or discovery["occurred_at"] != request.occurred_at):
                    raise TransactionError("原结果页事务混入其他事实")
                rows = discovery["body"]["rows"]
                if (len(rows) != 1 or rows[0]["table"] != "device_files"
                        or rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]):
                    raise TransactionError("原首次发现不属于本页原输入")
                values = rows[0]["after"]["values"]
                identity_parts = parse_exact_json(values.get("identity_key"))
                identity = (identity_parts[2] if isinstance(identity_parts, list)
                            and len(identity_parts) == 3
                            and identity_parts[:2] == [action["device_id"], action["driver_id"]]
                            else None)
                entry = page_entries.get(identity)
                file_id = original_file_ids.get(identity)
                if entry is None or file_id != rows[0]["id"]:
                    raise TransactionError("原首次发现身份或文件ID不属于APPEND差集")
                from .capture import file_discovery_row
                command = FileObservationSave(action["id"], identity, entry.locator,
                    request.occurred_at, entry.original_name, entry.media_type)
                expected_row = file_discovery_row(command, file_id,
                    file_identity_key(action["device_id"], action["driver_id"], identity))
                if values != dict(expected_row.after.values):
                    raise TransactionError("原首次发现没有保存原页实际定位及元数据")
                facts = row_facts(scope.connection, "device_files", file_id)
                actual_creates.append((identity, file_id, facts["created_event_id"]))
            expected_creates = []
            txn = event["transaction"]
            for identity, file_id in original.file_ids:
                facts = row_facts(scope.connection, "device_files", file_id)
                if txn.first_event_id <= facts["created_event_id"] <= txn.last_event_id:
                    expected_creates.append((identity, file_id, facts["created_event_id"]))
            if actual_creates != expected_creates:
                raise TransactionError("原键CREATE前缀没有精确覆盖本页首次发现文件")
            action, activity, attempt, run, page_scope, historical_files, prefix, boundary = (
                _replay_page_context(scope.connection, original_event,
                    (original, original_raw), transaction))
            replay_reads = _ReplayPageReads(
                scope.connection, page_scope, prefix, boundary,
                activity, action)
            _verify_append_difference(
                original_event, SimpleNamespace(transaction=transaction), original,
                original_raw, replay_reads, action, activity, attempt, run,
                historical_files=historical_files)
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
        allocation = scope.allocate(len(creates) + 1 + (1 if file_ids else 0))
        events = [event_envelope(allocation.first_event_id + i, allocation.txn_id, _FILE_EVENT["id"],
                                _FILE_EVENT["branches"]["CREATE"]["reason"], (row,), request.occurred_at)
                  for i, row in enumerate(creates)]
        event_id = allocation.first_event_id + len(creates)
        before = {name: attempt[name] for name in ("result_first_page_event_id", "result_last_page_event_id")}
        after = {"result_first_page_event_id": before["result_first_page_event_id"] or event_id,
                 "result_last_page_event_id": event_id}
        raw = {**result_page_input(request), "baseline_position": baseline_position,
               "last_identity": last_identity, "file_ids": file_ids}
        events.append(event_envelope(event_id, allocation.txn_id, _TYPE, _APPEND,
            (update_change("operation_attempts", attempt["id"], before, after),), request.occurred_at,
            {"attempt_id": attempt["id"], "result_page": raw}))
        if file_ids:
            index_event_id = event_id + 1
            relation_id = next_row_id(scope.connection, "result_page_files")
            relation_rows = []
            for identity, file_id in file_ids:
                values = _relation_values(created_event_id=index_event_id,
                    action_id=action["id"], run_id=run["id"], attempt_id=attempt["id"],
                    page_event_id=event_id, file_id=file_id, identity=identity)
                relation_rows.append(row_change("result_page_files", relation_id, values))
                owners[("result_page_files", relation_id)] = owner
                relation_id += 1
            events.append(event_envelope(index_event_id, allocation.txn_id, _TYPE,
                _INDEX_FILES, relation_rows, request.occurred_at,
                {"result_page_index": {"page_event_id": event_id}}))
        return CommandPlan(tuple(events), owners, state,
                           result=ResultPageRef(request.ticket, request.page_no, event_id, page.next_cursor),
                           result_page_write_scope=ResultPageWriteScope(action["id"], activity["id"],
                               run["id"], attempt["id"], request.page_no))


def _verify_attempt_transition(event, attempt):
    """原页必须精确推进F前物理尝试所指的首末事件。"""
    if event.evidence["attempt_id"] != attempt["id"]:
        raise ConsistencyError("APPEND改变原物理尝试身份")
    page_no = event.evidence["result_page"]["page_no"]
    row = event.rows[0]
    if row.table != "operation_attempts" or row.row_id != attempt["id"]:
        raise ConsistencyError("APPEND没有修改原物理尝试")
    if page_no == 1:
        expected_before = {
            "result_first_page_event_id": None,
            "result_last_page_event_id": None,
        }
        expected_after = {
            "result_first_page_event_id": event.event_id,
            "result_last_page_event_id": event.event_id,
        }
        if attempt["result_first_page_event_id"] is not None or attempt["result_last_page_event_id"] is not None:
            raise ConsistencyError("首结果页的F前尝试已经有页引用")
    else:
        expected_before = {
            "result_last_page_event_id": attempt["result_last_page_event_id"],
        }
        expected_after = {"result_last_page_event_id": event.event_id}
        if (attempt["result_first_page_event_id"] is None
                or attempt["result_last_page_event_id"] is None):
            raise ConsistencyError("后续结果页的F前尝试缺少共同首末引用")
    if (dict(row.before.values) != expected_before
            or dict(row.after.values) != expected_after):
        raise ConsistencyError("APPEND的首末页指针没有精确匹配F前物理尝试")


def _verify_append_difference(event, context, page, raw, reads, action, activity, attempt, run,
                              *, historical_files=None):
    """从原SQL基准和封存CREATE前缀重算APPEND的完整差集及文件映射。"""
    scope = reads.scope
    if (scope.action_id != action["id"] or scope.activity_id != activity["id"]
            or scope.run_id != run["id"] or scope.attempt_id != attempt["id"]
            or scope.page_no != page.ref.page_no):
        raise ConsistencyError("结果页窄写入范围与原动作、活动、流程、尝试或页号不同")
    _verify_attempt_transition(event, attempt)
    if (activity["ownership_mode"] == _OWNERSHIP.BASELINE_COMPARISON
            and activity["baseline_state"] != enum_for("device_activities.baseline_state").FIXED):
        raise ConsistencyError("差集结果页需要原固定基准")
    previous, previous_raw = None, None
    prior_event_id = attempt["result_last_page_event_id"]
    if prior_event_id is not None:
        previous, previous_raw = _at(reads.connection, prior_event_id)
        if (previous.ref.ticket != page.ref.ticket or previous.ref.page_no + 1 != page.ref.page_no
                or previous.page.next_cursor is None or previous.page.outcome.error is not None
                or page.page.cursor != previous.page.next_cursor):
            raise ConsistencyError("结果页没有沿原尝试末页的成功游标连续推进")
    elif page.ref.page_no != 1 or page.page.cursor is not None:
        raise ConsistencyError("结果页首批必须从空游标开始")
    if page.ref.page_no != (1 if previous is None else previous.ref.page_no + 1):
        raise ConsistencyError("结果页未按原页号连续推进")

    if activity["ownership_mode"] == _OWNERSHIP.BASELINE_COMPARISON:
        expected_entries, baseline_position, last_identity = _difference(
            reads, activity, action, page.page, previous_raw)
    else:
        expected_entries = list(page.page.entries)
        if len({entry.identity for entry in expected_entries}) != len(expected_entries):
            raise ConsistencyError("任务独立范围页重复列出稳定身份")
        baseline_position = None
        last_identity = (expected_entries[-1].identity if expected_entries else
                         None if previous_raw is None else previous_raw["last_identity"])
    if (not json_equal(raw["baseline_position"], baseline_position)
            or raw["last_identity"] != last_identity):
        raise ConsistencyError("APPEND没有保存原基准比较位置或末身份")

    prefix = reads.result_page_create_prefix()
    expected_by_identity = {entry.identity: entry for entry in expected_entries}
    prefix_by_identity = {}
    for create in prefix:
        if (create.event_type != _FILE_EVENT["id"]
                or create.reason != _FILE_EVENT["branches"]["CREATE"]["reason"]
                or create.transaction_id != context.transaction.txn_id
                or create.occurred_at != event.occurred_at or len(create.rows) != 1):
            raise ConsistencyError("结果页CREATE前缀混入其他文件或时刻")
        row = create.rows[0]
        if row.table != "device_files" or row.before.exists or not row.after.exists:
            raise ConsistencyError("结果页CREATE前缀不是首次文件发现")
        try:
            identity_key = parse_exact_json(row.after.values["identity_key"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("结果页CREATE身份键不可解释") from error
        identity = identity_key[2] if (isinstance(identity_key, list) and len(identity_key) == 3
            and identity_key[:2] == [action["device_id"], action["driver_id"]]) else None
        entry = expected_by_identity.get(identity)
        if entry is None or identity in prefix_by_identity:
            raise ConsistencyError("结果页CREATE包含多余或重复身份")
        from .capture import file_discovery_row
        command = FileObservationSave(action["id"], entry.identity, entry.locator,
            event.occurred_at, entry.original_name, entry.media_type)
        expected_row = file_discovery_row(command, row.row_id,
            file_identity_key(action["device_id"], action["driver_id"], entry.identity))
        if dict(row.after.values) != dict(expected_row.after.values):
            raise ConsistencyError("结果页首次发现与原实际条目的定位或元数据不同")
        with closing(reads.connection.execute(
                "SELECT id FROM device_files WHERE identity_key=?", (row.after.values["identity_key"],))) as cursor:
            stored = cursor.fetchone()
        if stored is not None:
            facts = row_facts(reads.connection, "device_files", stored[0])
            if facts["created_event_id"] != create.event_id or stored[0] != row.row_id:
                raise ConsistencyError("结果页CREATE身份在F前已存在或对应另一实际文件")
        prefix_by_identity[identity] = row.row_id
    expected_prefix_order = [entry.identity for entry in expected_entries
                             if entry.identity in prefix_by_identity]
    if list(prefix_by_identity) != expected_prefix_order:
        raise ConsistencyError("结果页CREATE前缀没有保持原差集身份顺序")

    file_ids = []
    for entry in expected_entries:
        identity_key = file_identity_key(action["device_id"], action["driver_id"], entry.identity)
        if entry.identity in prefix_by_identity:
            file_id = prefix_by_identity[entry.identity]
        else:
            if historical_files is None:
                with closing(reads.connection.execute(
                        "SELECT id FROM device_files WHERE identity_key=?", (identity_key,))) as cursor:
                    stored = cursor.fetchone()
                facts = None if stored is None else row_facts(reads.connection, "device_files", stored[0])
                file_id = None if stored is None else stored[0]
                existed_before = (facts is not None
                                  and facts["created_event_id"] < context.transaction.first_event_id)
            else:
                historical = historical_files.get(entry.identity)
                file_id, facts = (None, None) if historical is None else historical
                existed_before = facts is not None
            if (not existed_before or facts["identity_key"] != identity_key
                    or not json_equal(facts["locator_json"], dict(entry.locator))
                    or facts["source_action_id"] not in (None, action["id"])):
                raise ConsistencyError("F前复用文件与本页实际身份、定位或归属不同")
        file_ids.append([entry.identity, file_id])
    if not json_equal(raw["file_ids"], file_ids):
        raise ConsistencyError("APPEND的file_ids没有精确覆盖原差集身份及实际文件ID")

    index_id = event.event_id + 1
    expected_index = None
    if file_ids:
        if index_id != context.transaction.last_event_id:
            raise ConsistencyError("非空结果页INDEX_FILES必须是唯一事务末事件")
        replayed_relation_ids = getattr(
            reads, "replayed_result_page_relation_ids", None)
        relation_ids = (replayed_relation_ids(event.event_id)
                        if replayed_relation_ids is not None else None)
        if relation_ids is not None and len(relation_ids) != len(file_ids):
            raise ConsistencyError("原键INDEX_FILES关系行数量与重算差集不同")
        first_relation_id = (next_row_id(reads.connection, "result_page_files")
                             if relation_ids is None else relation_ids[0])
        rows = []
        for offset, (identity, file_id) in enumerate(file_ids):
            relation_id = (first_relation_id + offset if relation_ids is None
                           else relation_ids[offset])
            values = _relation_values(created_event_id=index_id,
                action_id=action["id"], run_id=run["id"], attempt_id=attempt["id"],
                page_event_id=event.event_id, file_id=file_id, identity=identity)
            rows.append(row_change("result_page_files", relation_id, values))
        expected_index = event_envelope(index_id, context.transaction.txn_id, _TYPE,
            _INDEX_FILES, rows, event.occurred_at,
            {"result_page_index": {"page_event_id": event.event_id}})
    if hasattr(reads, "verify_replayed_result_page_append"):
        reads.verify_replayed_result_page_append(
            event, expected_index, file_ids, context.transaction)
    else:
        reads.prepare_result_page_append(event, expected_index)


_PAGE_CONTEXT_FIELDS = {
    "operation_attempts": (
        "id", "run_id", "attempt_no", "status", "result_first_page_event_id",
        "result_last_page_event_id",
    ),
    "operation_runs": (
        "id", "kind", "action_id", "activity_id", "responsibility_key",
    ),
    "actions": ("id", "device_id", "driver_id"),
    "device_activities": (
        "id", "action_id", "ownership_mode", "output_scope_json", "baseline_state",
        "baseline_first_event_id", "baseline_last_event_id",
    ),
}


def _actual_page_write_context(event, page, context, reads):
    """由窄页scope定位SQL权威行，并拒绝caller状态与权威行的矛盾。"""
    scope = reads.scope
    connection = reads.connection
    if (connection is None or event.evidence["attempt_id"] != scope.attempt_id
            or scope.page_no != page.ref.page_no):
        raise ConsistencyError("结果页窄写入范围与APPEND物理身份不同")
    attempt = row_facts(connection, "operation_attempts", scope.attempt_id)
    if attempt is None or attempt["run_id"] != scope.run_id:
        raise ConsistencyError("结果页窄写入范围没有定位原物理尝试")
    run = row_facts(connection, "operation_runs", attempt["run_id"])
    action = None if run is None else row_facts(connection, "actions", run["action_id"])
    activity = None if run is None else row_facts(connection, "device_activities", run["activity_id"])
    if (run is None or action is None or activity is None
            or (run["id"], run["action_id"], run["activity_id"])
                != (scope.run_id, scope.action_id, scope.activity_id)
            or activity["action_id"] != action["id"]
            or run["kind"] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS)
            or attempt["status"] != int(_ATTEMPT_STATUS.RUNNING)
            or attempt["attempt_no"] != page.ref.ticket.attempt_id
            or page.ref.ticket.run_id != run["id"]
            or page.ref.ticket.target_id != str(activity["id"])):
        raise ConsistencyError("结果页窄写入范围与真实活动、流程、动作或运行中尝试不符")
    authoritative = {
        "operation_attempts": attempt,
        "operation_runs": run,
        "actions": action,
        "device_activities": activity,
    }
    identities = {
        "operation_attempts": attempt["id"],
        "operation_runs": run["id"],
        "actions": action["id"],
        "device_activities": activity["id"],
    }
    for table, fields in _PAGE_CONTEXT_FIELDS.items():
        identity = identities[table]
        supplied = context.state_rows.get(table, {}).get(identity)
        actual = authoritative[table]
        if supplied is None or any(
                name not in supplied or not json_equal(supplied[name], actual[name])
                for name in fields):
            raise ConsistencyError(f"结果页调用方{table}资格与原连接SQL不同")
    return action, activity, attempt, run


def _latest_history_boundary(connection):
    with closing(connection.execute(
            "SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1")) as cursor:
        latest = cursor.fetchone()
    with closing(connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
        last_event_id = cursor.fetchone()[0]
    if latest is None:
        if last_event_id is not None:
            raise ConsistencyError("原键恢复发现未归组历史事件")
        return HistoryBoundary(0, 0)
    transaction = read_transaction_range(connection, latest[0])
    if transaction.last_event_id != latest[1] or latest[1] != last_event_id:
        raise ConsistencyError("原键恢复的当前历史末边界不完整")
    return HistoryBoundary(transaction.txn_id, transaction.last_event_id)


def _page_before_boundary(connection, transaction):
    if transaction.txn_id == 1:
        if transaction.first_event_id != 1:
            raise ConsistencyError("首个结果页事务没有初始化历史前态")
        return HistoryBoundary(0, 0)
    previous = read_transaction_range(connection, transaction.txn_id - 1)
    if previous.last_event_id + 1 != transaction.first_event_id:
        raise ConsistencyError("结果页事务缺少连续的F前完整历史边界")
    return HistoryBoundary(previous.txn_id, previous.last_event_id)


_PAGE_REPLAY_FIELDS = {
    "actions": frozenset({"device_id", "driver_id"}),
    "operation_runs": frozenset({"kind", "action_id", "activity_id", "responsibility_key"}),
    "device_activities": frozenset({
        "action_id", "ownership_mode", "output_scope_json", "baseline_state",
        "baseline_first_event_id", "baseline_last_event_id",
    }),
    "operation_attempts": frozenset({
        "run_id", "attempt_no", "status", "result_first_page_event_id",
        "result_last_page_event_id",
    }),
}


def _restore_page_row(connection, *, boundary, current_boundary, transaction,
                      table, row_id, owner):
    current = row_facts(connection, table, row_id)
    if current is None:
        raise ConsistencyError(f"原结果页F前恢复缺少{table}#{row_id}")
    try:
        restored = read_row_values_at_boundary(
            connection, owner=owner, table=table, row_id=row_id,
            columns=_PAGE_REPLAY_FIELDS[table], current_values=current,
            boundary=boundary, current_boundary=current_boundary,
            validated_transaction=transaction)
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError(f"原结果页{table}的F前状态不可恢复") from error
    return {**current, **restored}


def _replay_page_context(connection, event, original, transaction):
    """恢复原页F前有限资格行及最多128个本页身份对应文件行。"""
    boundary = _page_before_boundary(connection, transaction)
    current_boundary = _latest_history_boundary(connection)
    raw = original[1]
    attempt_id = event.evidence["attempt_id"]
    current_attempt = row_facts(connection, "operation_attempts", attempt_id)
    if current_attempt is None:
        raise ConsistencyError("原结果页物理尝试已经缺失")
    current_run = row_facts(connection, "operation_runs", raw["run_id"])
    if current_run is None or current_attempt["run_id"] != current_run["id"]:
        raise ConsistencyError("原结果页流程和物理尝试的当前身份不同")
    owner_action_id = current_run["action_id"]
    action = _restore_page_row(connection, boundary=boundary,
        current_boundary=current_boundary, transaction=transaction, table="actions",
        row_id=owner_action_id, owner=("action", owner_action_id))
    run = _restore_page_row(connection, boundary=boundary,
        current_boundary=current_boundary, transaction=transaction, table="operation_runs",
        row_id=current_run["id"], owner=("action", owner_action_id))
    if run["action_id"] != action["id"] or run["activity_id"] != raw["activity_id"]:
        raise ConsistencyError("原页F前流程没有绑定原动作与活动")
    activity = _restore_page_row(connection, boundary=boundary,
        current_boundary=current_boundary, transaction=transaction,
        table="device_activities", row_id=run["activity_id"],
        owner=("action", action["id"]))
    attempt = _restore_page_row(connection, boundary=boundary,
        current_boundary=current_boundary, transaction=transaction,
        table="operation_attempts", row_id=attempt_id,
        owner=("action", action["id"]))
    ticket = original[0].ref.ticket
    if (activity["action_id"] != action["id"] or attempt["run_id"] != run["id"]
            or attempt["attempt_no"] != ticket.attempt_id
            or run["id"] != ticket.run_id
            or run["activity_id"] != int(ticket.target_id)
            or run["responsibility_key"] != ticket.responsibility_key
            or run["kind"] != int(_RUN_KIND.CHECK_CAPTURE_RESULTS)
            or attempt["status"] != int(_ATTEMPT_STATUS.RUNNING)):
        raise ConsistencyError("原结果页F前资格与原typed request不一致")

    file_facts = {}
    for entry in original[0].page.entries:
        identity_key = file_identity_key(action["device_id"], action["driver_id"], entry.identity)
        with closing(connection.execute(
                "SELECT id,created_event_id FROM device_files WHERE identity_key=? LIMIT 2",
                (identity_key,))) as cursor:
            candidates = cursor.fetchall()
        if len(candidates) > 1:
            raise ConsistencyError("原结果页身份对应多个设备文件行")
        if not candidates or candidates[0][1] > boundary.last_event_id:
            continue
        file_id = candidates[0][0]
        file = row_facts(connection, "device_files", file_id)
        if file is None:
            raise ConsistencyError("原结果页F前设备文件行不存在")
        try:
            restored = read_row_values_at_boundary(
                connection, owner=("device_file", file_id), table="device_files",
                row_id=file_id,
                columns=frozenset({"identity_key", "locator_json", "source_action_id"}),
                current_values=file, boundary=boundary,
                current_boundary=current_boundary, validated_transaction=transaction)
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("原结果页文件归属或定位无法恢复到F前") from error
        file_facts[entry.identity] = (file_id, {**file, **restored})
    if len(file_facts) > 128:
        raise ConsistencyError("原结果页F前恢复超过128个文件身份")
    scope = ResultPageWriteScope(action["id"], activity["id"], run["id"],
        attempt["id"], original[0].ref.page_no)
    prefix = []
    with closing(connection.execute(
            f"SELECT {_COLUMNS} FROM history_events WHERE transaction_id=? AND id<?"
            " ORDER BY id LIMIT 129", (transaction.txn_id, event.event_id))) as cursor:
        for stored in cursor:
            prefix.append(decode_event_row(stored))
    if len(prefix) > 128 or any(item.event_id < transaction.first_event_id
            or item.event_id >= event.event_id for item in prefix):
        raise ConsistencyError("原结果页CREATE前缀超出有限页容量")
    return action, activity, attempt, run, scope, file_facts, tuple(prefix), boundary


class _ReplayPageReads:
    """原键只读重算使用的页内事实，不提供通用历史读取端口。"""

    def __init__(self, connection, scope, prefix, boundary, activity, action):
        self.connection = connection
        self.scope = scope
        self.prefix = prefix
        self.boundary = boundary
        self.activity = activity
        self.action = action
        self._baseline_ref = None

    def result_page_create_prefix(self):
        return self.prefix

    def replayed_result_page_relation_ids(self, page_event_id):
        with closing(self.connection.execute(
                "SELECT id FROM result_page_files WHERE page_event_id=? ORDER BY id",
                (page_event_id,))) as cursor:
            return tuple(row[0] for row in cursor)

    def result_page_baseline_ref(self, activity, action):
        if (activity["id"] != self.activity["id"] or action["id"] != self.action["id"]
                or activity["ownership_mode"] != int(_OWNERSHIP.BASELINE_COMPARISON)
                or activity["baseline_state"] != int(BaselineState.FIXED)):
            raise ConsistencyError("原结果页F前固定基准资格无效")
        if self._baseline_ref is not None:
            return self._baseline_ref
        owner_type = load_enum_registry()["history_objects"]["action"]["id"]
        history_columns = ",".join("h." + name for name in _COLUMNS.split(","))
        with closing(self.connection.execute(
                f"SELECT {history_columns} FROM entity_event_links l JOIN history_events h ON h.id=l.event_id"
                " WHERE l.entity_type=? AND l.entity_id=? AND h.id<=? AND h.event_type=?"
                " AND json_extract(h.body_json,'$.evidence.activity_id')=?"
                " AND json_extract(h.body_json,'$.reason') IN (?,?)"
                " ORDER BY h.id DESC LIMIT 1",
                (owner_type, action["id"], self.boundary.last_event_id,
                 _BASELINE_TYPE, activity["id"], _BASELINE_FIX, _BASELINE_FIX_EMPTY))) as cursor:
            stored = cursor.fetchone()
        if stored is None:
            raise ConsistencyError("原结果页F前固定基准缺少原FIX事件")
        event = decode_event_row(stored)
        transaction = read_transaction_range(self.connection, event.transaction_id)
        if (event.event_id > self.boundary.last_event_id
                or not transaction.first_event_id <= event.event_id <= transaction.last_event_id):
            raise ConsistencyError("原结果页FIX事件越过F前完整边界")
        name, branch, _ = validate_event_structure(event)
        evidence = event.evidence
        if (name != "BASELINE_CHUNK" or branch not in ("FIX", "FIX_EMPTY")
                or set(evidence) != {"activity_id", "chunk_count", "entry_count"}
                or evidence["activity_id"] != activity["id"]
                or len(event.rows) != 1 or event.rows[0].table != "device_activities"
                or event.rows[0].row_id != activity["id"]
                or event.rows[0].after.values.get("baseline_state") != int(BaselineState.FIXED)):
            raise ConsistencyError("原结果页F前FIX事件不属于冻结基准")
        try:
            chunk_count, entry_count = evidence["chunk_count"], evidence["entry_count"]
            ref = BaselineRef(activity["id"], BaselineState.FIXED,
                activity["baseline_first_event_id"], activity["baseline_last_event_id"],
                chunk_count, entry_count)
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("原结果页F前固定基准引用不可解释") from error
        if branch == "FIX_EMPTY":
            if (chunk_count or entry_count or ref.first_event_id is not None
                    or ref.last_event_id is not None
                    or event.rows[0].after.values.get("baseline_first_event_id") is not None
                    or event.rows[0].after.values.get("baseline_last_event_id") is not None):
                raise ConsistencyError("原结果页F前空固定基准范围或计数错误")
        else:
            if (not chunk_count or not entry_count or ref.first_event_id is None
                    or ref.last_event_id is None
                    or ref.last_event_id > self.boundary.last_event_id
                    or ref.last_event_id >= event.event_id):
                raise ConsistencyError("原结果页F前非空固定基准范围或计数错误")
            head = _chunk_at(self.connection, ref.first_event_id, activity["id"])
            tail = head if ref.last_event_id == ref.first_event_id else _chunk_at(
                self.connection, ref.last_event_id, activity["id"])
            if head.chunk_no != 1 or tail.chunk_no != chunk_count:
                raise ConsistencyError("原结果页F前基准首末批次与固定计数不符")
            _check_entries(head.entries, activity, action)
            _check_entries(tail.entries, activity, action)
        self._baseline_ref = ref
        return ref

    def result_page_baseline_chunks(self, ref, cursor, batch):
        from .baseline import _iter_chunks

        if (type(batch) is not int or batch != 1
                or ref != self.result_page_baseline_ref(self.activity, self.action)):
            raise ConsistencyError("原结果页基准页读取超出F前固定范围")
        if ref.first_event_id is None:
            if cursor is not None:
                raise ConsistencyError("原结果页空固定基准不能继续读取")
            return Page((), None)
        count, last_key = 0, None
        if cursor is not None:
            ObjectId(cursor)
            if not ref.first_event_id <= cursor <= ref.last_event_id:
                raise ConsistencyError("原结果页基准游标不在F前固定范围内")
            previous = _chunk_at(self.connection, cursor, self.activity["id"])
            count, last_key = previous.chunk_no, previous.entries[-1].sort_key
        chunks = tuple(_iter_chunks(
            self.connection, self.activity["id"], ref.first_event_id,
            ref.last_event_id, previous=cursor, limit=batch + 1))
        for chunk in chunks:
            if chunk.chunk_no != count + 1 or (count == 0 and chunk.event_id != ref.first_event_id):
                raise ConsistencyError("原结果页F前基准批次存在缺口或不连续")
            _check_entries(chunk.entries, self.activity, self.action)
            if last_key is not None and last_key >= chunk.entries[0].sort_key:
                raise ConsistencyError("原结果页F前基准身份重复或顺序不一致")
            count, last_key = chunk.chunk_no, chunk.entries[-1].sort_key
        if len(chunks) <= batch:
            last = chunks[-1].event_id if chunks else cursor
            if count != ref.chunk_count or last != ref.last_event_id:
                raise ConsistencyError("原结果页F前固定基准末页缺少原范围条目")
            return Page(chunks, None)
        return Page(chunks[:batch], chunks[batch - 1].event_id)

    def verify_replayed_result_page_append(self, append, expected_index, file_ids,
                                           transaction):
        with closing(self.connection.execute(
                "SELECT id FROM history_events WHERE transaction_id=? AND event_type=?"
                " AND json_extract(body_json,'$.reason')=? ORDER BY id",
                (transaction.txn_id, _TYPE, _INDEX_FILES))) as cursor:
            index_ids = [row[0] for row in cursor]
        with closing(self.connection.execute(
                "SELECT id,created_event_id,action_id,run_id,attempt_id,page_event_id,file_id,identity,"
                "family_directory,family_stem,family_role FROM result_page_files"
                " WHERE page_event_id=? ORDER BY id", (append.event_id,))) as cursor:
            relations = cursor.fetchall()
        if not file_ids:
            if (expected_index is not None or index_ids or relations
                    or transaction.last_event_id != append.event_id):
                raise ConsistencyError("原键零差集页含INDEX或关系事件")
            return
        index_id = append.event_id + 1
        if (expected_index is None or index_ids != [index_id]
                or transaction.last_event_id != index_id or len(relations) != len(file_ids)):
            raise ConsistencyError("原键非空结果页缺少唯一末位INDEX或完整关系")
        with closing(self.connection.execute(
                f"SELECT {_COLUMNS} FROM history_events WHERE id=?", (index_id,))) as cursor:
            stored = cursor.fetchone()
        if stored is None:
            raise ConsistencyError("原键INDEX_FILES历史缺失")
        index = decode_event_row(stored)
        expected_rows = tuple(expected_index.rows)
        expected_relation_values = [dict(row.after.values) for row in expected_rows]
        actual_relation_values = [dict(zip((
            "created_event_id", "action_id", "run_id", "attempt_id", "page_event_id",
            "file_id", "identity", "family_directory", "family_stem", "family_role"),
            row[1:])) for row in relations]
        if actual_relation_values != expected_relation_values:
            raise ConsistencyError("原键关系没有精确保存原F前重算差集")
        if (len(index.rows) != len(relations)
                or any(row.row_id != relation[0]
                       or row.table != "result_page_files" or row.before.exists
                       or dict(row.after.values) != expected
                       for row, relation, expected in zip(
                           index.rows, relations, expected_relation_values))):
            raise ConsistencyError("原键INDEX_FILES正文与关系表不符")
        with closing(self.connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM result_page_files WHERE created_event_id<?",
                (index_id,))) as cursor:
            prior_max = cursor.fetchone()[0]
        if [row[0] for row in relations] != list(
                range(prior_max + 1, prior_max + len(relations) + 1)):
            raise ConsistencyError("原键关系行身份没有保持事务内规范分配顺序")
        if (index.event_id != index_id or index.transaction_id != transaction.txn_id
                or index.occurred_at != append.occurred_at
                or index.evidence != {"result_page_index": {"page_event_id": append.event_id}}):
            raise ConsistencyError("原键INDEX_FILES身份、时刻、事务或APPEND引用不同")
        validate_event_structure(index)
        expected_index = replace(expected_index, rows=tuple(
            replace(row, row_id=relation[0])
            for row, relation in zip(expected_index.rows, relations)))
        if replace(index, change_seq=None) != expected_index:
            raise ConsistencyError("原键INDEX_FILES没有精确保存原F前重算差集")


def _page_guard(event, context):
    try:
        page, raw = _decode(event)
        from camctl.persistence.event_reads import SqlEventReads
        reads = context.current_reads
        actual_context = None
        if (type(reads) is SqlEventReads
                and isinstance(reads.scope, ResultPageWriteScope)):
            actual_context = _actual_page_write_context(event, page, context, reads)
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
        if actual_context is not None:
            _verify_append_difference(event, context, page, raw, reads,
                                      *actual_context)
    except (KeyError, TypeError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


def _page_index_guard(event, context):
    try:
        from camctl.persistence.event_reads import SqlEventReads
        reads = context.current_reads
        if (type(reads) is not SqlEventReads or not isinstance(reads.scope, ResultPageWriteScope)
                or set(event.evidence) != {"result_page_index"}):
            raise ConsistencyError("INDEX_FILES缺少原页窄写入范围")
        expected = reads.expected_result_page_index()
        page_event_id = event.evidence["result_page_index"].get("page_event_id")
        actual_append = reads.accepted_result_page_event(page_event_id, context.transaction)
        if (actual_append.event_id != expected.evidence["result_page_index"]["page_event_id"]
                or event.event_id <= actual_append.event_id
                or event.transaction_id != actual_append.transaction_id
                or event.occurred_at != actual_append.occurred_at
                or replace(event, change_seq=None) != expected):
            raise ConsistencyError("INDEX_FILES与本组实际APPEND的完整差集关系不符")
    except (KeyError, TypeError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


def register_result_page_guard():
    register_guard("result_page", _page_guard)
    register_guard("result_page_index", _page_index_guard)
