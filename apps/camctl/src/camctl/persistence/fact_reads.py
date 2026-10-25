"""一次真实操作内的C0固定事实；当前行与业务通过结论不进入此范围。"""
from collections import OrderedDict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY, TransactionRange
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.decoding import decode_event_row
from camctl.history.events import EventEnvelope, load_event_registry
from camctl.persistence.read_reuse import _retained_size, typed_read_key


MAX_FACT_ENTRIES = 128
MAX_FACT_BYTES = 2 * 1024 * 1024
MAX_RESULT_PAGE_EVENTS = 3
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


def _cached(state, key):
    if state is None or key not in state.entries:
        return None
    value, _, _ = state.entries[key]
    state.entries.move_to_end(key)
    return deepcopy(value)


def _remember(state, key, value, *, page=False):
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
    if page:
        pages = [identity for identity, (_, _, is_page) in state.entries.items() if is_page]
        if len(pages) >= MAX_RESULT_PAGE_EVENTS:
            _, removed, _ = state.entries.pop(pages[0])
            state.retained_bytes -= removed
    while state.entries and (len(state.entries) >= MAX_FACT_ENTRIES or state.retained_bytes+size > MAX_FACT_BYTES):
        _, (_, removed, _) = state.entries.popitem(last=False)
        state.retained_bytes -= removed
    state.entries[key] = stored, size, page
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


def _record(connection, row, requested_id=None):
    event = decode_event_row(row)
    if requested_id is not None and event.event_id != requested_id:
        raise ConsistencyError('历史事件身份与实际查询不一致')
    transaction = read_transaction(connection, event.transaction_id)
    if not transaction.first_event_id <= event.event_id <= transaction.last_event_id:
        raise ConsistencyError('历史事件不属于其完整事务')
    record = EventRecord(tuple(row), event, transaction)
    state = _facts(connection)
    page_type = load_event_registry()['events']['RESULT_PAGE']['id']
    _remember(state, ('event', typed_read_key(event.event_id)), record, page=event.event_type == page_type)
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


def iter_transaction_events(connection, transaction, *, batch_size=128):
    """按完整组顺序交付有限批次，不保留整个事件数组。"""
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError('历史事件批次必须是1至128的整数')
    if read_transaction(connection, transaction.txn_id) != transaction:
        raise ConsistencyError('历史组读取改变原完整边界')
    after = transaction.first_event_id-1
    while after < transaction.last_event_id:
        end = min(after+batch_size, transaction.last_event_id)
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


def reuse_fixed_file(connection, file_id, file_event_id, boundary, loader):
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
