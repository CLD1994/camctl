"""读取结果事务使用已经提交的原动作取消事件。"""

from contextlib import closing

from camctl.contracts.values import ConsistencyError


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
