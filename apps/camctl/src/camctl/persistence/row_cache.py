"""在当前执行范围内可选复用实际 SQL 行。"""

from contextlib import contextmanager
from contextvars import ContextVar


_CURRENT = ContextVar("row_cache", default=None)


@contextmanager
def row_cache_scope(reuse):
    token = _CURRENT.set(reuse)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def read_cached_row(connection, table, row_id, load):
    reuse = _CURRENT.get()
    return load() if reuse is None else reuse(connection, table, row_id, load)
