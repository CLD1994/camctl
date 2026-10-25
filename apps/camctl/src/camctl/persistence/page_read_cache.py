"""同一事务内复用已经完整验证的不可变原页，不缓存当前资格。"""
from collections import OrderedDict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from camctl.persistence.read_reuse import clear_read_reuse


@dataclass
class _PageReads:
    connection: Any
    upper_event_id: int
    records: OrderedDict = field(default_factory=OrderedDict)
    usable: bool = True


_CURRENT = ContextVar('verified_page_reads', default=None)
# 最多复用最近三页的原历史行，覆盖当前页、首批和紧邻页的反复读取。
_PAGE_LIMIT = 3


@contextmanager
def page_read_scope(connection, upper_event_id):
    """调用者在事务开始取得上界；嵌套读取沿用原范围，不延长寿命。"""
    current = _CURRENT.get()
    if current is not None and current.connection is connection:
        yield
        return
    if not connection.in_transaction:
        yield
        return
    clear_read_reuse(connection)
    reads = _PageReads(connection, upper_event_id)
    token = _CURRENT.set(reads)
    try:
        yield
    finally:
        reads.records.clear()
        reads.usable = False
        _CURRENT.reset(token)


@contextmanager
def opened_read_page_scope(connection, opened):
    """只为本入口新开的只读事务建立范围；已有事务由拥有者管理。"""
    if not opened:
        yield
        return
    with closing(connection.execute('SELECT MAX(id) FROM history_events')) as cursor:
        upper = cursor.fetchone()[0] or 0
    with page_read_scope(connection, upper):
        yield


def _reads(connection):
    current = _CURRENT.get()
    if current is None or current.connection is not connection or not current.usable:
        return None
    if not connection.in_transaction:
        discard_page_records(connection)
        return None
    return current


def page_read_scope_active(connection):
    """有限原页范围优先，避免通用缓存保留另一份完整解码页。"""
    return _reads(connection) is not None


def cached_page_record(connection, event_id):
    reads = _reads(connection)
    if reads is None:
        return None
    record = reads.records.get(event_id)
    if record is not None:
        reads.records.move_to_end(event_id)
    return record


def prepare_page_record(connection, event_id):
    """读取新原页前释放一个旧记录，避免与完整原事务解码重叠驻留。"""
    reads = _reads(connection)
    if reads is not None and event_id <= reads.upper_event_id and len(reads.records) >= _PAGE_LIMIT:
        reads.records.popitem(last=False)


def remember_page_record(connection, event_id, transaction_last_event_id, record):
    reads = _reads(connection)
    if reads is None or transaction_last_event_id > reads.upper_event_id:
        return
    reads.records[event_id] = record
    reads.records.move_to_end(event_id)
    while len(reads.records) > _PAGE_LIMIT:
        reads.records.popitem(last=False)


def clear_page_records(connection):
    """提案和独立守卫各自核验原页，仍保持同一事务的提交上界。"""
    reads = _reads(connection)
    if reads is not None:
        reads.records.clear()


def discard_page_records(connection):
    """在真实 COMMIT 或 ROLLBACK 之前停用，未知回执也不能保留证明。"""
    current = _CURRENT.get()
    if current is not None and current.connection is connection:
        current.records.clear()
        current.usable = False
