"""在单次完整事务内复用调用方已经核验的读取结果。

调用方负责让作用域只覆盖一次完整事务，并提供包含全部读取条件的键。
本模块只观察连接已有属性，不发出 SQL，也不管理事务边界。
"""

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
import sqlite3
import sys
from typing import Any, TypeVar


MAX_READ_REUSE_ENTRIES = 128
MAX_READ_REUSE_BYTES = 2 * 1024 * 1024


@dataclass
class _ReadReuseState:
    connection: sqlite3.Connection
    generation: int | None = None
    entries: OrderedDict[Hashable, tuple[Any, int]] = field(default_factory=OrderedDict)
    retained_bytes: int = 0

    def reset(self, generation: int | None) -> None:
        self.entries.clear()
        self.retained_bytes = 0
        self.generation = generation


_active_scope: ContextVar[_ReadReuseState | None] = ContextVar("read_reuse_scope", default=None)
_T = TypeVar("_T")


def typed_read_key(value: Any) -> Any:
    """保留读取输入及数据类字段的实际类型，不合并布尔值与整数。"""
    if is_dataclass(value) and not isinstance(value, type):
        return type(value), tuple(
            (member.name, typed_read_key(getattr(value, member.name)))
            for member in fields(value)
        )
    if isinstance(value, tuple):
        return type(value), tuple(typed_read_key(item) for item in value)
    return type(value), value


@contextmanager
def read_reuse_scope(connection: sqlite3.Connection) -> Iterator[None]:
    """建立当前连接的一次事务读取范围；相邻同连接嵌套共享缓存。"""
    current = _active_scope.get()
    state = (
        current if current is not None and current.connection is connection
        else _ReadReuseState(connection)
    )
    token = _active_scope.set(state)
    try:
        yield
    finally:
        _active_scope.reset(token)


def clear_read_reuse(connection: sqlite3.Connection) -> None:
    """独立校验开始前清空上一计算阶段的读取依据。"""
    current = _active_scope.get()
    if current is not None and current.connection is connection:
        current.reset(None)


def reuse_read(connection: sqlite3.Connection, key: Any, loader: Callable[[], _T]) -> _T:
    """复用本事务同一写入代的已核验值；缺失时执行调用方的完整读取。"""
    state = _active_scope.get()
    if state is None or state.connection is not connection:
        return loader()
    try:
        hash(key)
    except TypeError:
        return loader()
    if not connection.in_transaction:
        state.reset(None)
        return loader()

    generation = connection.total_changes
    if state.generation != generation:
        state.reset(generation)
    if key in state.entries:
        value, _ = state.entries[key]
        state.entries.move_to_end(key)
        return deepcopy(value)

    result = loader()
    if not connection.in_transaction:
        state.reset(None)
        return result
    current_generation = connection.total_changes
    if current_generation != generation:
        if state.generation != current_generation:
            state.reset(current_generation)
        return result

    stored = deepcopy(result)
    seen: set[int] = set()
    retained_bytes = _retained_size(key, seen) + _retained_size(stored, seen)
    if retained_bytes > MAX_READ_REUSE_BYTES:
        return result
    previous = state.entries.pop(key, None)
    if previous is not None:
        state.retained_bytes -= previous[1]
    while state.entries and (
        len(state.entries) >= MAX_READ_REUSE_ENTRIES
        or state.retained_bytes + retained_bytes > MAX_READ_REUSE_BYTES
    ):
        _, (_, removed_bytes) = state.entries.popitem(last=False)
        state.retained_bytes -= removed_bytes
    state.entries[key] = stored, retained_bytes
    state.retained_bytes += retained_bytes
    return result


def _retained_size(value: Any, seen: set[int]) -> int:
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
