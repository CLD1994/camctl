"""实际工具调用与原业务结果交接共用一个取消拥有范围。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any


class _CallScope:
    def __init__(self):
        self.task = asyncio.current_task()
        self.interrupted: asyncio.CancelledError | None = None

    def checkpoint(self):
        if self.interrupted is not None:
            raise self.interrupted


_scope: ContextVar[_CallScope | None] = ContextVar("camctl_tool_call_owner", default=None)


def current_call_scope():
    scope = _scope.get()
    return scope if scope is not None and scope.task is asyncio.current_task() else None


def check_call_interruption():
    scope = current_call_scope()
    if scope is not None:
        scope.checkpoint()


@dataclass(frozen=True)
class _ActualResult:
    value: Any = None
    error: BaseException | None = None


def owned_tool_call(function):
    """持有实际调用及原结果保存；重复取消只中断原等待一次。"""
    @wraps(function)
    async def owned(*args, **kwargs):
        scope = current_call_scope()
        if scope is not None:
            scope.checkpoint()
            value = await function(*args, **kwargs)
            scope.checkpoint()
            return value

        async def run():
            scope = _CallScope()
            token = _scope.set(scope)
            try:
                value = await function(*args, **kwargs)
                scope.checkpoint()
                return _ActualResult(value)
            except BaseException as error:
                return _ActualResult(error=error)
            finally:
                _scope.reset(token)

        task = asyncio.create_task(run())
        interrupted = None
        while True:
            try:
                actual = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if interrupted is None:
                    interrupted = error
                    # 执行体收到一次取消；工具边界延后传播直到原结果交接。
                    if not task.done():
                        task.cancel()
                if task.cancelled():
                    raise interrupted
        if interrupted is not None:
            if actual.error is not None and not isinstance(actual.error, asyncio.CancelledError):
                raise interrupted from actual.error
            if actual.error is not None and actual.error.__cause__ is not None:
                raise interrupted from actual.error.__cause__
            raise interrupted
        if actual.error is not None:
            raise actual.error
        return actual.value

    return owned
