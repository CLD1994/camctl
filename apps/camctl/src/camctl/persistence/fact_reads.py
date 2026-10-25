"""一次真实操作内的C0固定事实；当前行与业务通过结论不进入此范围。"""
from collections import OrderedDict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
import sys

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY, TransactionRange
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.decoding import decode_event_row
from camctl.history.events import EventEnvelope


MAX_FACT_ENTRIES = 128
MAX_FACT_BYTES = 2 * 1024 * 1024
EVENT_COLUMNS = 'id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json'


@dataclass(frozen=True)
class EventRecord:
    raw: tuple
    event: EventEnvelope
    transaction: TransactionRange


@dataclass
class _Facts:
    connection: object
    boundary: HistoryBoundary
    entries: OrderedDict = field(default_factory=OrderedDict)
    retained_bytes: int = 0
    active: bool = True

    def discard(self):
        self.entries.clear()
        self.retained_bytes = 0
        self.active = False


_CURRENT = ContextVar('operation_facts', default=None)


def _facts(connection):
    state = _CURRENT.get()
    if state is None or state.connection is not connection or not state.active:
        return None
    if not connection.in_transaction:
        state.discard()
        return None
    return state


@contextmanager
def operation_fact_scope(connection, boundary):
    """内核固定完整C0；同连接嵌套沿用原身份且不扩大可见范围。"""
    if not isinstance(boundary, HistoryBoundary) or not connection.in_transaction:
        raise ConsistencyError('事实范围要求原连接的真实事务及完整C0')
    current = _facts(connection)
    if current is not None:
        if typed_read_key(boundary) != typed_read_key(current.boundary):
            raise ConsistencyError('嵌套事实读取不能改变原C0')
        yield
        return
    state = _Facts(connection, boundary)
    token = _CURRENT.set(state)
    try:
        if boundary != INITIAL_BOUNDARY:
            complete = read_transaction(connection, boundary.txn_id)
            if complete.last_event_id != boundary.last_event_id:
                raise ConsistencyError('事实C0不是完整已提交事务的末位')
        yield
    finally:
        state.discard()
        _CURRENT.reset(token)


def discard_operation_facts(connection):
    """真实COMMIT/ROLLBACK发出前停用；未知或取消不保留本次事实。"""
    state = _CURRENT.get()
    if state is not None and state.connection is connection:
        state.discard()


def operation_fact_boundary(connection):
    """返回内核拥有的C0，不提供推进或登记结论的接口。"""
    state = _facts(connection)
    return None if state is None else state.boundary


@contextmanager
def opened_operation_fact_scope(connection, opened):
    """新开只读事务取得自身C0；已有事务沿用其拥有者的范围。"""
    if not opened or _facts(connection) is not None:
        yield
        return
    with closing(connection.execute('SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1')) as cursor:
        latest = cursor.fetchone()
    with closing(connection.execute('SELECT MAX(id) FROM history_events')) as cursor:
        maximum = cursor.fetchone()[0] or 0
    boundary = INITIAL_BOUNDARY if latest is None else HistoryBoundary(*latest)
    if boundary.last_event_id != maximum:
        raise ConsistencyError('只读事实范围缺少完整提交边界')
    with operation_fact_scope(connection, boundary):
        yield


def _cached(state, key):
    if state is None or key not in state.entries:
        return None
    value, _ = state.entries[key]
    state.entries.move_to_end(key)
    return deepcopy(value)


def _remember(state, key, value):
    if state is None or not state.active or not state.connection.in_transaction:
        return
    stored = deepcopy(value)
    seen = set()
    size = _retained_size(key, seen)+_retained_size(stored, seen)
    if size > MAX_FACT_BYTES:
        return
    previous = state.entries.pop(key, None)
    if previous is not None:
        state.retained_bytes -= previous[1]
    while state.entries and (len(state.entries) >= MAX_FACT_ENTRIES or state.retained_bytes+size > MAX_FACT_BYTES):
        _, (_, removed) = state.entries.popitem(last=False)
        state.retained_bytes -= removed
    state.entries[key] = stored, size
    state.retained_bytes += size


def read_transaction(connection, transaction_id):
    """只核完整已提交事务，不接受调用者授予的通过资格。"""
    ObjectId(transaction_id)
    state = _facts(connection)
    if state is not None and transaction_id > state.boundary.txn_id:
        raise ConsistencyError('历史事务越过操作开始时的完整C0')
    key = ('transaction', typed_read_key(transaction_id))
    cached = _cached(state, key)
    if cached is not None:
        return cached
    from camctl.persistence.transaction import _load_transaction_range
    value = _load_transaction_range(connection, transaction_id, None)
    if state is not None and value.last_event_id > state.boundary.last_event_id:
        raise ConsistencyError('历史事务末位越过操作开始时的C0')
    _remember(state, key, value)
    return value


def read_operation_transaction(connection, operation_key):
    """按实际原键定位完整已提交组；缺键是当前查询，不保留空结果。"""
    with closing(connection.execute(
            'SELECT id FROM history_transactions WHERE operation_key=?', (str(operation_key),))) as cursor:
        found = cursor.fetchone()
    return None if found is None else read_transaction(connection, found[0])


def _record(connection, row, requested_id=None):
    event = decode_event_row(row)
    if requested_id is not None and event.event_id != requested_id:
        raise ConsistencyError('历史事件身份与实际查询不一致')
    transaction = read_transaction(connection, event.transaction_id)
    if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
        raise ConsistencyError('历史事件不属于其完整事务')
    record = EventRecord(tuple(row), event, transaction)
    state = _facts(connection)
    _remember(state, ('event', typed_read_key(event.event_id)), record)
    return record


def read_event(connection, event_id):
    """读取一个固定原事件及完整组；返回值与保留事实隔离。"""
    ObjectId(event_id)
    state = _facts(connection)
    if state is not None and event_id > state.boundary.last_event_id:
        raise ConsistencyError('历史事件越过操作开始时的完整C0')
    cached = _cached(state, ('event', typed_read_key(event_id)))
    if cached is not None:
        return cached
    with closing(connection.execute(f'SELECT {EVENT_COLUMNS} FROM history_events WHERE id=?', (event_id,))) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError('引用的历史事件不存在')
    return _record(connection, row, event_id)


def read_events(connection, event_ids):
    """以身份读取至多128个原事件；批量缺失项只发一条正文查询。"""
    identities = tuple(event_ids)
    if len(identities) > 128:
        raise ValueError('历史事件批量读取不能超过128项')
    state = _facts(connection)
    records, missing = {}, []
    for identity in identities:
        ObjectId(identity)
        if state is not None and identity > state.boundary.last_event_id:
            raise ConsistencyError('历史事件越过操作开始时的完整C0')
        if identity in records or identity in missing:
            continue
        record = _cached(state, ('event', typed_read_key(identity)))
        if record is None:
            missing.append(identity)
        else:
            records[identity] = record
    if missing:
        placeholders = ','.join('?' for _ in missing)
        with closing(connection.execute(
                f'SELECT {EVENT_COLUMNS} FROM history_events WHERE id IN ({placeholders}) ORDER BY id',
                tuple(missing))) as cursor:
            rows = cursor.fetchall()
        if len(rows) != len(missing) or {row[0] for row in rows} != set(missing):
            raise ConsistencyError('历史事件批次缺少引用成员或身份不符')
        for row in rows:
            records[row[0]] = _record(connection, row, row[0])
    return tuple(deepcopy(records[identity]) for identity in identities)


def iter_transaction_events(connection, transaction, *, after_event_id=None, end_event_id=None, batch_size=128):
    """按完整组顺序交付有限批次，不保留整个事件数组。"""
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError('历史事件批次必须是1至128的整数')
    if read_transaction(connection, transaction.txn_id) != transaction:
        raise ConsistencyError('历史组读取改变原完整边界')
    after = transaction.first_event_id-1 if after_event_id is None else after_event_id
    if type(after) is not int or not transaction.first_event_id-1 <= after <= transaction.last_event_id:
        raise ConsistencyError('历史事件游标不属于完整原组')
    stop = transaction.last_event_id if end_event_id is None else end_event_id
    if type(stop) is not int or not after <= stop <= transaction.last_event_id:
        raise ConsistencyError('历史事件末位不属于完整原组')
    while after < stop:
        end = min(after+batch_size, stop)
        state = _facts(connection)
        keys = [('event', typed_read_key(identity)) for identity in range(after+1, end+1)]
        if state is not None and all(key in state.entries for key in keys):
            records = [_cached(state, key) for key in keys]
        else:
            with closing(connection.execute(f'SELECT {EVENT_COLUMNS} FROM history_events WHERE transaction_id=? AND id>? AND id<=? ORDER BY id LIMIT ?',
                    (transaction.txn_id, after, end, batch_size))) as cursor:
                rows = cursor.fetchall()
            if len(rows) != end-after:
                raise ConsistencyError('完整历史事务缺少必要成员')
            records = []
            for identity, row in zip(range(after+1, end+1), rows):
                if row[0] != identity or row[1] != transaction.txn_id:
                    raise ConsistencyError('完整历史事务的事件顺序或身份不连续')
                cached = _cached(state, ('event', typed_read_key(identity)))
                records.append(cached if cached is not None else _record(connection, row, identity))
        yield from records
        after = end


def _reuse_fixed_file(connection, file_id, file_event_id, boundary, loader):
    """复用完整固定边界的文件原值，不接收提案或来源通过结论。"""
    ObjectId(file_id)
    ObjectId(file_event_id)
    if not isinstance(boundary, HistoryBoundary) or file_event_id > boundary.last_event_id:
        raise ConsistencyError('固定文件事件不属于完整历史边界')
    actual = read_transaction(connection, boundary.txn_id)
    if actual.last_event_id != boundary.last_event_id:
        raise ConsistencyError('固定文件读取不是完整历史边界')
    state = _facts(connection)
    key = ('file', typed_read_key(file_id), typed_read_key(file_event_id), typed_read_key(boundary))
    cached = _cached(state, key)
    if cached is not None:
        return cached
    value = loader()
    if value.get('id') != file_id or value.get('last_event_id') != file_event_id:
        raise ConsistencyError('固定文件事实改变原文件或历史位置')
    _remember(state, key, value)
    return value


def typed_read_key(value):
    """保留读取输入及数据类字段的实际类型，不合并布尔值与整数。"""
    if is_dataclass(value) and not isinstance(value, type):
        return type(value), tuple(
            (member.name, typed_read_key(getattr(value, member.name)))
            for member in fields(value)
        )
    if isinstance(value, tuple):
        return type(value), tuple(typed_read_key(item) for item in value)
    return type(value), value


def _retained_size(value, seen):
    """估算键和值占用；容器与数据类中的同一对象只计一次。"""
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(
            _retained_size(key, seen) + _retained_size(item, seen)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_retained_size(item, seen) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        if hasattr(value, "__dict__"):
            size += sys.getsizeof(vars(value))
        return size + sum(
            _retained_size(getattr(value, member.name), seen) for member in fields(value)
        )
    return size
