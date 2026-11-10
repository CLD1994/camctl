"""已保存历史事务范围与原操作键的读取。"""

from contextlib import closing
from dataclasses import dataclass
import sqlite3
from typing import Any, TypedDict

from camctl.contracts.history_values import BoundaryError, TransactionRange
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.history.events import EventEnvelope
from camctl.persistence.fact_reads import operation_fact_boundary, read_transaction, iter_transaction_events


def _body_document(event: EventEnvelope) -> dict[str, Any]:
    rows = []
    for row in event.rows:
        rows.append(
            {
                "table": row.table,
                "id": row.row_id,
                "before": {"exists": row.before.exists, "values": dict(row.before.values)},
                "after": {"exists": row.after.exists, "values": dict(row.after.values)},
            }
        )
    return {"reason": event.reason, "evidence": dict(event.evidence), "rows": rows}


def read_transaction_range(
    connection: sqlite3.Connection, transaction_id: int,
    validated: TransactionRange | None = None,
) -> TransactionRange:
    """核实完整事务范围、前一边界及成员；复用范围只限已核验的不可变历史。"""
    if validated is not None and operation_fact_boundary(connection) is None:
        return _load_transaction_range(connection, transaction_id, validated)
    actual = read_transaction(connection, transaction_id)
    if validated is not None and actual != validated:
        raise ConsistencyError(f"已核验历史事务 {transaction_id} 的范围发生变化")
    return actual


def _load_transaction_range(
    connection: sqlite3.Connection, transaction_id: int,
    validated: TransactionRange | None,
) -> TransactionRange:
    with closing(connection.execute(
        "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE id = ?",
        (transaction_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"历史事务 {transaction_id} 不存在")
    try:
        transaction = TransactionRange(*row)
    except BoundaryError as error:
        raise ConsistencyError(f"历史事务 {transaction_id} 的范围无效") from error
    if transaction.txn_id != transaction_id:
        raise ConsistencyError(f"历史事务 {transaction_id} 的身份不一致")
    if transaction_id > 1:
        with closing(connection.execute(
            "SELECT last_event_id FROM history_transactions WHERE id = ?", (transaction_id - 1,),
        )) as cursor:
            previous = cursor.fetchone()
    else:
        previous = (0,)
    if previous is None or previous[0] + 1 != transaction.first_event_id:
        raise ConsistencyError(f"历史事务 {transaction_id} 与前一完整边界不连续")
    if validated is not None:
        if transaction != validated:
            raise ConsistencyError(f"已核验历史事务 {transaction_id} 的范围发生变化")
        return transaction
    with closing(connection.execute(
        "SELECT COUNT(*), MIN(id), MAX(id) FROM history_events WHERE transaction_id = ?",
        (transaction_id,),
    )) as cursor:
        count, first, last = cursor.fetchone()
    if (count != transaction.last_event_id - transaction.first_event_id + 1
            or first != transaction.first_event_id or last != transaction.last_event_id):
        raise ConsistencyError(f"历史事务 {transaction_id} 的成员与首尾范围不一致")
    return transaction


class SavedEvent(TypedDict):
    """已存事件的身份、完整事务范围和精确正文，供原操作键核实使用。"""

    event_id: int
    occurred_at: int
    transaction: TransactionRange
    type: int
    reason: int
    body: dict[str, Any]


@dataclass(frozen=True)
class SavedTransactionCursor:
    instance_id: str
    operation_key: str
    transaction: TransactionRange
    after_event_id: int


def read_saved_transaction_page(connection, operation_key, transaction, cursor=None, limit=128):
    """按固定原组分页精确解码；缺行、错组及查询失败均拒绝。"""
    from camctl.contracts.pages import Page
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("原组读取批次必须为1—128")
    row = connection.execute("SELECT instance_id FROM database_metadata").fetchone()
    instance = row[0]
    found = connection.execute("SELECT id FROM history_transactions WHERE operation_key=?",(str(operation_key),)).fetchone()
    if found is None or found[0] != transaction.txn_id:
        raise ConsistencyError("原操作键与固定事务范围不同")
    if cursor is None:
        if read_transaction_range(connection,transaction.txn_id) != transaction:
            raise ConsistencyError("原组完整范围与请求不同")
        after = transaction.first_event_id-1
    else:
        if (not isinstance(cursor,SavedTransactionCursor) or cursor.instance_id != instance
                or cursor.operation_key != str(operation_key) or cursor.transaction != transaction
                or not transaction.first_event_id <= cursor.after_event_id < transaction.last_event_id):
            raise ConsistencyError("原组游标不属于固定实例、操作键和完整范围")
        after = cursor.after_event_id
    size = min(limit,transaction.last_event_id-after)
    from itertools import islice
    events = tuple(record.event for record in islice(iter_transaction_events(
        connection, transaction, after_event_id=after, batch_size=size), size))
    following = after+size
    return Page(events,None if following==transaction.last_event_id else
        SavedTransactionCursor(instance,str(operation_key),transaction,following))


def saved_transaction_events(
    connection: sqlite3.Connection, operation_key: OperationKey
) -> list[SavedEvent] | None:
    """在调用方的同一事务内读取原键的完整、可解释事件事实。

    仅缺键返回 None；已存事务的范围、成员或正文无效时整项失败，
    不交付前缀，也不执行业务决定。取得的游标在所有出口释放。
    """
    with closing(connection.execute(
        "SELECT id FROM history_transactions WHERE operation_key = ?",
        (str(operation_key),),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    transaction = read_transaction_range(connection, row[0])
    events: list[SavedEvent] = []
    for record in iter_transaction_events(connection, transaction):
        event = record.event
        events.append(SavedEvent(
            event_id=event.event_id, occurred_at=event.occurred_at,
            transaction=transaction, type=event.event_type,
            reason=event.reason, body=_body_document(event),
        ))
    return events
