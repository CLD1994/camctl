"""读取结果事务使用已经提交的原动作取消事件。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError
from camctl.contracts.json_values import json_equal


def binding_cancel_event(connection, action_id, max_txn_id):
    from camctl.contracts.enums import load_registry
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import load_event_registry
    from camctl.persistence.transaction import read_transaction_range

    action_type = load_registry()["history_objects"]["action"]["id"]
    event_type = load_event_registry()["events"]["CANCEL_CHANGED"]["id"]
    with closing(connection.execute(
        "SELECT e.id,e.transaction_id,e.event_type,e.event_version,e.occurred_at,e.clock_status,e.change_seq,e.body_json"
        " FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
        " WHERE l.entity_type=? AND l.entity_id=? AND e.event_type=? AND e.transaction_id<=?"
        " AND EXISTS (SELECT 1 FROM json_each(e.body_json, '$.rows') r"
        " WHERE json_extract(r.value,'$.table')='actions' AND json_extract(r.value,'$.id')=?"
        " AND json_extract(r.value,'$.before.values.cancel_requested')=0"
        " AND json_extract(r.value,'$.after.values.cancel_requested')=1) ORDER BY e.id LIMIT 2",
        (action_type, action_id, event_type, max_txn_id, action_id))) as cursor:
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise ConsistencyError("原动作缺少唯一已提交取消生效事件")
    event = decode_event_row(rows[0])
    tx = read_transaction_range(connection, event.transaction_id)
    if not tx.first_event_id <= event.event_id <= tx.last_event_id:
        raise ConsistencyError("取消生效事件不属于完整历史事务")
    return {"event_id": event.event_id, "transaction_id": event.transaction_id, "occurred_at": event.occurred_at}


def saved_binding_read_failure(connection, key, action_id, copy_id, error):
    """仅接受原键对应的完整真实读取结果和绑定失败。"""
    from camctl.persistence.transaction import row_facts, saved_transaction_events

    events = saved_transaction_events(connection, key)
    if events is None or len(events) != 2 or [(e["type"], e["reason"]) for e in events] != [(12, 1), (10, 3)]:
        raise ConsistencyError("原读取结果键缺少完整绑定失败事务")
    attempt_rows, run_rows = events[0]["body"]["rows"], events[1]["body"]["rows"]
    evidence = events[1]["body"]["evidence"].get("binding_read_finish")
    if (len(attempt_rows) != 1 or len(run_rows) != 1 or attempt_rows[0]["table"] != "operation_attempts"
            or run_rows[0]["table"] != "operation_runs" or not isinstance(evidence, dict)
            or not json_equal(evidence, {"error": error, "cancel_event": None})):
        raise ConsistencyError("原读取结果键与绑定失败申请不一致")
    attempt = row_facts(connection, "operation_attempts", attempt_rows[0]["id"])
    run = row_facts(connection, "operation_runs", run_rows[0]["id"])
    after = attempt_rows[0]["after"]["values"]
    result = after.get("result_json")
    settlement = result.get("settlement") if isinstance(result, dict) else None
    if (attempt is None or run is None or attempt["run_id"] != run["id"]
            or run["action_id"] != action_id or run["copy_id"] != copy_id
            or run["status"] != 4 or not json_equal(run["error_json"], error)
            or attempt["result_event_id"] != events[0]["event_id"] or attempt["status"] != 2
            or not json_equal(attempt["result_json"], result) or attempt["error_json"] is not None
            or after.get("status") != 2 or after.get("error_json") is not None
            or not isinstance(settlement, dict) or settlement.get("basis") != "observed"
            or settlement.get("evidence") != {"type": "read_returned", "version": 1, "data": {}}
            or run_rows[0]["after"]["values"].get("status") != 4
            or not json_equal(run_rows[0]["after"]["values"].get("error_json"), error)):
        raise ConsistencyError("原读取结果与实际调用或流程归属不一致")
    return True
