"""从照片产物完整事务截点核对固定等待者与单角色缓存。"""
from contextlib import closing
from dataclasses import dataclass

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.history.photo_cache_request import parse_photo_cache_request
from camctl.history.validators import validate_event_structure
from camctl.persistence.fact_reads import iter_transaction_events, read_event
from camctl.persistence.transaction import row_facts
from camctl.outputs.segment_copy_reuse import _read_scope, _selection_is_bound_at_boundary


@dataclass(frozen=True)
class BoundPhotoCache:
    source_action_id: int
    source_device_file_id: int
    intermediate_file_id: int
    result_page_event_id: int
    published_boundary: HistoryBoundary
    size_bytes: int
    sha256: str
    relative_path: str
    purpose: str


def read_bound_photo_cache(*, output_id, obtain_action_id, selection_id, owned, allow_released=False, historical=False):
    """验证输出原完整事务、H 上等待者及原角色缓存。"""
    with _read_scope(owned.connection):
        return _read_bound_photo_cache(output_id=output_id, obtain_action_id=obtain_action_id,
                                       selection_id=selection_id, owned=owned, allow_released=allow_released, historical=historical)


def _read_bound_photo_cache(*, output_id, obtain_action_id, selection_id, owned, allow_released, historical):
    connection = owned.connection
    output = row_facts(connection, "outputs", output_id)
    if output is None or output["device_file_id"] is None or output["intermediate_file_id"] is not None:
        return None
    action = row_facts(connection, "actions", output["source_action_id"])
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if (action is None or action["type"] != int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
            or action["driver_id"] != DRIVER_ID
            or action["execution_spec_json"].get("output_storage") != "device"):
        return None
    output_kind = enum_for("outputs.kind")
    file_purpose = enum_for("intermediate_files.purpose")
    role = {int(output_kind.ORIGINAL): (file_purpose.CAPTURE_ORIGINAL, "original"),
            int(output_kind.PREVIEW): (file_purpose.CAPTURE_PREVIEW, "thumbnail")}.get(output["kind"])
    if role is None:
        return None
    record = read_event(connection, output["created_event_id"])
    if validate_event_structure(record.event)[0] != "OUTPUT_REGISTERED":
        raise ConsistencyError("照片产物缺少原登记事件")
    rows = [row for row in record.event.rows if row.table == "outputs" and row.row_id == output_id]
    if (len(rows) != 1 or rows[0].before.exists or not rows[0].after.exists
            or any(not json_equal(rows[0].after.values.get(name), output[name])
                   for name in ("source_action_id", "device_file_id", "intermediate_file_id", "kind"))):
        raise ConsistencyError("照片产物与原登记事件不一致")
    finished = []
    for item in iter_transaction_events(connection, record.transaction):
        event = item.event
        if validate_event_structure(event)[0] != "ACTION_FINISHED":
            continue
        finished.extend(row for row in event.rows if row.table == "actions" and row.row_id == action["id"])
    terminal = enum_for("actions.status")
    if (len(finished) != 1 or finished[0].after.values.get("status") not in
            (int(terminal.SUCCEEDED), int(terminal.FAILED), int(terminal.CANCELED))):
        raise ConsistencyError("照片产物和动作终态不在同一完整事务")
    boundary = HistoryBoundary(record.transaction.txn_id, record.transaction.last_event_id)
    from camctl.persistence.repositories.source_transactions import _created_before
    with closing(connection.execute(
        "SELECT EXISTS(SELECT 1 FROM obtain_source_selections s WHERE s.id=? AND "
        + _created_before("obtain_source_selections", "s", boundary.last_event_id + 1) + ")",
        (selection_id,))) as cursor:
        existed = cursor.fetchone()[0]
    if not existed:
        return None
    _, bound = _selection_is_bound_at_boundary(source_action_id=action["id"],
        obtain_action_id=obtain_action_id, selection_id=selection_id, boundary=boundary, owned=owned)
    if not bound:
        return None
    with closing(connection.execute(
        "SELECT id FROM intermediate_files WHERE owner_action_id=? AND purpose=? ORDER BY id LIMIT 2",
        (action["id"], int(role[0])))) as cursor:
        files = cursor.fetchall()
    if len(files) != 1:
        raise ConsistencyError("照片来源缺少唯一角色缓存责任")
    file = row_facts(connection, "intermediate_files", files[0][0])
    retention = enum_for("intermediate_files.retention_state")
    cleanup = enum_for("intermediate_files.cleanup_state")
    retained = (file["retention_state"] == int(retention.REQUIRED)
                and file["cleanup_state"] == int(cleanup.NOT_NEEDED))
    released = (allow_released and file["retention_state"] == int(retention.RELEASABLE)
                and (historical or file["cleanup_state"] != int(cleanup.COMPLETED)))
    if (not (retained or released)
            or (not allow_released and file["last_error_json"] is not None)
            or file["size_bytes"] is None or file["sha256"] is None):
        return None
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=26"
        " AND json_extract(body_json,'$.rows[0].table')='intermediate_files'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.evidence.photo_cache_request') IS NOT NULL"
        " AND id<? ORDER BY id LIMIT 2", (file["id"], record.transaction.first_event_id))) as cursor:
        cache_events = cursor.fetchall()
    if len(cache_events) != 1:
        raise ConsistencyError("照片缓存缺少唯一的原归档结果事件")
    cache = read_event(connection, cache_events[0][0])
    if (validate_event_structure(cache.event)[:2] != ("INTERMEDIATE_FILE_CHANGED", "LIFECYCLE")
            or cache.transaction.last_event_id >= record.transaction.first_event_id):
        raise ConsistencyError("照片缓存结果不在原产物事务之前")
    request = parse_photo_cache_request(cache.event.evidence.get("photo_cache_request"))
    changes = cache.event.rows
    if (request.archive_error is not None or request.action_id != action["id"]
            or request.file_id != file["id"] or request.device_file_id != output["device_file_id"]
            or request.size_bytes != file["size_bytes"] or request.sha256 != file["sha256"]
            or cache.event.occurred_at != request.occurred_at or len(changes) != 1
            or changes[0].table != "intermediate_files" or changes[0].row_id != file["id"]
            or not changes[0].before.exists or not changes[0].after.exists
            or not json_equal(changes[0].before.values, {"size_bytes": None, "sha256": None})
            or not json_equal(changes[0].after.values,
                              {"size_bytes": request.size_bytes, "sha256": request.sha256})):
        raise ConsistencyError("照片缓存申请与原产物或当前文件事实不同")
    # 来源形成时已完整核验 START、双流与一次比较；日常消费只核直接引用。
    from camctl.persistence.repositories.result_pages import read_saved_page
    page_record = read_event(connection, request.result_page_event_id)
    if page_record.transaction.last_event_id >= cache.transaction.first_event_id:
        raise ConsistencyError("照片角色页必须早于原缓存事务")
    page, _ = read_saved_page(connection, request.result_page_event_id)
    run = row_facts(connection, "operation_runs", page.ref.ticket.run_id)
    observations = [item for item in page.page.outcome.observations
                    if item.type == "result_files_listed" and item.version == 3]
    attempts = [item for item in page.page.outcome.observations
                if item.type == "photo_result_attempt" and item.version == 2]
    device = row_facts(connection, "device_files", request.device_file_id)
    evidence = None if device is None else device.get("completion_evidence_json")
    observation = evidence.get("observation") if isinstance(evidence, dict) else None
    reference = observation.get("photo_source") if isinstance(observation, dict) else None
    if (validate_event_structure(page_record.event)[:2] != ("RESULT_PAGE", "APPEND")
            or run is None or run["action_id"] != action["id"]
            or run["kind"] != int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)
            or len(observations) != 1 or observations[0].data.get("role") != role[1]
            or len(attempts) != 1 or attempts[0].data.get("role") != role[1]
            or not isinstance(attempts[0].data.get("comparison"), dict)
            or attempts[0].data["comparison"].get("selected") != request.selected
            or len(page.page.entries) != 1
            or (page.page.entries[0].identity, request.device_file_id) not in page.file_ids
            or device is None or device["source_action_id"] != action["id"]
            or device["role"] != int(enum_for("device_files.role").ORIGINAL if role[1] == "original"
                                     else enum_for("device_files.role").PREVIEW)
            or device["completion_state"] != int(enum_for("device_files.completion_state").COMPLETE)
            or device["size_bytes"] != request.size_bytes or device["sha256"] != request.sha256
            or page.page.entries[0].size_bytes != request.size_bytes
            or not json_equal(page.page.entries[0].locator, device["locator_json"])
            or not isinstance(reference, dict) or reference.get("result_page_event_id") != request.result_page_event_id
            or reference.get("role") != role[1]):
        raise ConsistencyError("照片缓存角色、设备文件或原角色页引用不一致")
    return BoundPhotoCache(action["id"], output["device_file_id"], file["id"],
        request.result_page_event_id, boundary, file["size_bytes"], file["sha256"],
        file["relative_path"], role[0].name)


def verify_photo_cache_binding_failure(connection, copy, *, before_event_id, historical=False):
    """核对照片缓存完整读取、真实不符及原 DEVICE 产物的直接事实。"""
    from types import SimpleNamespace
    from camctl.history.events import reason_for_branch
    if (copy.get("source_intermediate_file_id") is None or copy.get("source_device_file_id") is not None
            or copy.get("delivery_id") is None or copy.get("read_end_event_id") is None
            or copy.get("verification_state") != int(enum_for("file_copies.verification_state").MISMATCHED)
            or copy.get("committed_bytes") != copy.get("source_size")
            or not isinstance(copy.get("source_sha256"), str) or not isinstance(copy.get("target_sha256"), str)
            or copy["source_sha256"] == copy["target_sha256"]):
        raise ConsistencyError("照片缓存绑定失败缺少完整读取与实际摘要不符")
    with closing(connection.execute(
        "SELECT d.output_id,d.action_id,i.selection_id FROM deliveries d"
        " JOIN obtain_items i ON i.delivery_id=d.id WHERE d.id=? LIMIT 2", (copy["delivery_id"],))) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise ConsistencyError("照片缓存绑定失败缺少唯一正式取回项")
    output_id, action_id, selection_id = rows[0]
    bound = read_bound_photo_cache(output_id=output_id, obtain_action_id=action_id, selection_id=selection_id,
        owned=SimpleNamespace(connection=connection), allow_released=True, historical=historical)
    if (bound is None or bound.intermediate_file_id != copy["source_intermediate_file_id"]
            or bound.size_bytes != copy["source_size"] or bound.sha256 != copy["source_sha256"]):
        raise ConsistencyError("照片缓存绑定失败不对应原 H 角色缓存")
    end = read_event(connection, copy["read_end_event_id"])
    doc = end.event.evidence.get("read_end")
    if (validate_event_structure(end.event)[:2] != ("COPY_CHANGED", "READ_END")
            or end.transaction.last_event_id >= before_event_id or len(end.event.rows) != 1
            or end.event.rows[0].table != "file_copies" or end.event.rows[0].row_id != copy["id"]
            or end.event.rows[0].after.values.get("read_end_event_id") != copy["read_end_event_id"]
            or not isinstance(doc, dict) or doc.get("copy_round") != copy["round"]
            or doc.get("source_intermediate_file_id") != copy["source_intermediate_file_id"]
            or doc.get("source_device_file_id") is not None or doc.get("target_file_id") != copy["target_file_id"]
            or doc.get("source_size") != copy["source_size"] or doc.get("committed_bytes") != copy["source_size"]
            or doc.get("reset_state") != int(enum_for("file_copies.reset_state").READY)
            or not isinstance(doc.get("end"), dict) or set(doc["end"]) != {"stopped", "bytes_read", "error"}
            or doc["end"].get("stopped") is not True or doc["end"].get("error") is not None
            or type(doc["end"].get("bytes_read")) is not int
            or not 0 <= doc["end"]["bytes_read"] <= copy["source_size"]
            or not isinstance(doc.get("ticket"), dict) or doc["ticket"].get("target_id") != str(copy["id"])):
        raise ConsistencyError("照片缓存绑定失败缺少原读取实际结束")
    ticket = doc["ticket"]
    if (set(ticket) != {"run_id", "attempt_no", "operation", "target_id", "responsibility_key"}
            or ticket["operation"] != "read" or ticket["responsibility_key"] != f"read/{copy['id']}"):
        raise ConsistencyError("照片缓存原读取结束的票据不完整")
    with closing(connection.execute(
        "SELECT r.action_id,r.copy_id,r.kind,a.intent_event_id,a.result_event_id,r.responsibility_key,r.attempts_used"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.id=? AND a.attempt_no=?", (ticket["run_id"], ticket["attempt_no"]))) as cursor:
        original = cursor.fetchone()
    if (original is None or original[0] != action_id or original[1] != copy["id"]
            or original[2] != int(enum_for("operation_runs.kind").READ_FILE)
            or original[5] != f"read/{copy['id']}" or original[6] != ticket["attempt_no"]
            or original[3] is None or original[3] >= end.transaction.first_event_id
            or (original[4] is not None and original[4] <= end.transaction.last_event_id)):
        raise ConsistencyError("照片缓存读取结束不属于原实际 READ 票据")
    with closing(connection.execute(
        "SELECT id FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
        " AND id>? AND id<? AND json_extract(body_json,'$.rows[0].table')='file_copies'"
        " AND json_extract(body_json,'$.rows[0].id')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.verification_state')=?"
        " AND json_extract(body_json,'$.rows[0].after.values.target_sha256')=? ORDER BY id DESC LIMIT 1",
        (22, reason_for_branch("COPY_CHANGED", "VERIFY"), end.event.event_id, before_event_id,
         copy["id"], int(enum_for("file_copies.verification_state").MISMATCHED), copy["target_sha256"]))) as cursor:
        mismatch = cursor.fetchone()
    if mismatch is None:
        raise ConsistencyError("照片缓存绑定失败缺少原不符事件")
    verification = read_event(connection, mismatch[0])
    if (validate_event_structure(verification.event)[:2] != ("COPY_CHANGED", "VERIFY")
            or verification.transaction.last_event_id >= before_event_id):
        raise ConsistencyError("照片缓存不符尚未可靠提交")
    return bound.source_device_file_id


def photo_cache_waiters_settled(file_id, owned):
    """分页核对该角色仍需要缓存的 H 等待者。"""
    with _read_scope(owned.connection):
        return _photo_cache_waiters_settled(file_id, owned)


def _photo_cache_waiters_settled(file_id, owned):
    connection = owned.connection
    file = row_facts(connection, "intermediate_files", file_id)
    if file is None or file["owner_action_id"] is None:
        return True
    action = row_facts(connection, "actions", file["owner_action_id"])
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if (action is None or action["driver_id"] != DRIVER_ID
            or action["type"] != int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
            or action["execution_spec_json"].get("output_storage") != "device"):
        return True
    purposes, kinds = enum_for("intermediate_files.purpose"), enum_for("outputs.kind")
    kind = {int(purposes.CAPTURE_ORIGINAL): int(kinds.ORIGINAL),
            int(purposes.CAPTURE_PREVIEW): int(kinds.PREVIEW)}.get(file["purpose"])
    if kind is None or file["size_bytes"] is None or file["sha256"] is None:
        return True
    with closing(connection.execute(
        "SELECT id FROM outputs WHERE source_action_id=? AND kind=? ORDER BY id LIMIT 2",
        (action["id"], kind))) as cursor:
        outputs = cursor.fetchall()
    if not outputs:
        return True
    if len(outputs) != 1:
        raise ConsistencyError("照片缓存角色对应多个正式产物")
    output_id = outputs[0][0]
    statuses = enum_for("actions.status")
    selection_status = enum_for("obtain_source_selections.status")
    item_status = enum_for("obtain_items.status")
    delivery_status = enum_for("deliveries.status")
    terminal_actions = {int(statuses.SUCCEEDED), int(statuses.FAILED), int(statuses.CANCELED), int(statuses.EXPIRED)}
    terminal_deliveries = {int(delivery_status.PUBLISHED), int(delivery_status.FAILED),
                           int(delivery_status.CANCELED), int(delivery_status.WITHDRAWN)}
    after_action = after_selection = 0
    while True:
        with closing(connection.execute(
            "SELECT a.id,s.id,a.status,s.status FROM actions a"
            " JOIN action_dependencies d ON d.action_id=a.id"
            " JOIN obtain_source_selections s ON s.dependency_id=d.id"
            " WHERE d.depends_on_action_id=? AND a.type=?"
            " AND (a.id>? OR (a.id=? AND s.id>?)) ORDER BY a.id,s.id LIMIT 32",
            (action["id"], int(enum_for("actions.type").OBTAIN_ACTION_OUTPUTS),
             after_action, after_action, after_selection))) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return True
        for obtain_id, selection_id, status, selection in rows:
            if status in terminal_actions:
                continue
            if selection == int(selection_status.FIXED):
                with closing(connection.execute(
                    "SELECT i.status,d.status FROM obtain_items i LEFT JOIN deliveries d ON d.id=i.delivery_id"
                    " WHERE i.selection_id=? AND (i.output_id=? OR (i.status=? AND i.requested_output_id=?))"
                    " ORDER BY i.id LIMIT 2",
                    (selection_id, output_id, int(item_status.UNRESOLVED), output_id))) as cursor:
                    items = cursor.fetchall()
                if len(items) > 1:
                    raise ConsistencyError("同一来源选择重复包含照片角色产物")
                if not items or items[0][0] in (int(item_status.FAILED), int(item_status.CANCELED)):
                    continue
                if items[0][1] in terminal_deliveries:
                    continue
            bound = read_bound_photo_cache(output_id=output_id, obtain_action_id=obtain_id,
                selection_id=selection_id, owned=owned, allow_released=True)
            if bound is not None and bound.intermediate_file_id == file_id:
                return False
        after_action, after_selection = rows[-1][0], rows[-1][1]
