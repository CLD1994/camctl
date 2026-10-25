"""业务协程提交完整数据库操作；连接始终留在所属worker。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import fields, is_dataclass
import inspect
import sqlite3
from typing import TypeVar

from camctl.contracts.values import OperationKey, new_operation_key
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (DbExecutorError, DbJob, DbJobKind, DbOutcome,
    DbPriority, ReadReceipt, _attach_db_outcome)
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

T = TypeVar('T')


async def _settle(future):
    cancelled = None
    while True:
        try:
            value = await asyncio.shield(future)
        except asyncio.CancelledError as error:
            if future.cancelled():
                if cancelled is not None:
                    raise error from cancelled
                raise
            if cancelled is None:
                cancelled = error
        except BaseException as error:
            if cancelled is not None:
                raise error from cancelled
            raise
        else:
            return value, cancelled


def _attach_actual(error, outcome):
    _attach_db_outcome(error, outcome)
    from camctl.persistence.transaction import _attach_write_receipt, write_receipt_for
    for original in (outcome.control, outcome.error):
        if original is not None and (receipt := write_receipt_for(original)) is not None:
            _attach_write_receipt(error, receipt)
            break


async def settle_required(awaitable: Awaitable[T]) -> T:
    """完整必需阶段先完成DB及loop状态应用，再传播原取消。"""
    result, cancelled = await _settle(asyncio.ensure_future(awaitable))
    if cancelled is not None:
        if isinstance(result, DbOutcome):
            _attach_actual(cancelled, result)
        raise cancelled
    return result


def _check_value(value, seen=None):
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, (OwnedConnection, sqlite3.Connection, sqlite3.Cursor, Iterator)) or inspect.isawaitable(value):
        raise DbExecutorError('数据库callback只能返回有限值，不能交付连接、游标或延迟执行对象')
    if isinstance(value, Mapping):
        for key, member in value.items():
            _check_value(key, seen)
            _check_value(member, seen)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for member in value:
            _check_value(member, seen)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _check_value(getattr(value, field.name), seen)


class BusinessDatabase:
    """一次run的BUSINESS端口；快照复用executor而非另建写连接。"""

    def __init__(self, executor: DbExecutor, metadata: DatabaseMetadata) -> None:
        self._executor, self._metadata = executor, metadata
        self._closing = None

    @classmethod
    async def open(cls, factory: Callable[[], OwnedConnection], *, capacity=64,
                   enqueue_timeout_seconds=9.0, confirmed_write_observer=None):
        executor = DbExecutor(factory, capacity=capacity,
            enqueue_timeout_seconds=enqueue_timeout_seconds,
            confirmed_write_observer=confirmed_write_observer)
        database = cls(executor, None)
        try:
            database._metadata = await database.read(lambda owned: owned.metadata,
                description='业务数据库元信息')
        except BaseException:
            await settle_required(executor.close())
            raise
        return database

    @property
    def metadata(self) -> DatabaseMetadata:
        return self._metadata

    @property
    def executor(self) -> DbExecutor:
        return self._executor

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._executor.close())
        await settle_required(self._closing)

    async def _wait(self, future, key):
        try:
            return await asyncio.shield(future), None
        except asyncio.CancelledError as cancelled:
            if not future.done():
                try:
                    await _settle(asyncio.ensure_future(self._executor.withdraw(key)))
                except DbExecutorError:
                    # 已完成但尚未投递时，唯一原Future仍拥有真实结果。
                    pass
            try:
                result, _ = await _settle(future)
            except BaseException as error:
                raise error from cancelled
            return result, cancelled

    async def read(self, callback: Callable[[OwnedConnection], T], *, description='业务读取') -> T:
        def execute(owned):
            value = callback(owned)
            _check_value(value)
            return value
        key = new_operation_key()
        job = DbJob(key, description, DbJobKind.READ, DbPriority.BUSINESS, execute)
        payload, cancelled = await self._wait(self._executor.submit_read(job), key)
        if isinstance(payload, ReadReceipt):
            if payload.error is not None:
                if cancelled is not None:
                    raise payload.error from cancelled
                raise payload.error
            if cancelled is not None:
                raise cancelled
            return payload.value
        if cancelled is not None and isinstance(payload, DbOutcome):
            _attach_actual(cancelled, payload)
            raise cancelled
        if isinstance(payload, DbOutcome) and payload.error is not None:
            raise payload.error
        raise DbExecutorError('业务读取未完成，不交付默认值')

    async def write(self, callback: Callable[[OwnedConnection], DbOutcome[T]], *,
                    key: OperationKey, description='业务写入') -> DbOutcome[T]:
        key = OperationKey(key)
        def execute(owned):
            value = callback(owned)
            if not isinstance(value, DbOutcome):
                raise DbExecutorError('业务写callback必须返回DbOutcome')
            _check_value(value.value)
            return value
        job = DbJob(key, description, DbJobKind.WRITE, DbPriority.BUSINESS, execute)
        outcome, cancelled = await self._wait(self._executor.submit_write(job), key)
        if not isinstance(outcome, DbOutcome):
            raise DbExecutorError('业务写入没有实际分类结果')
        error = outcome.control or outcome.observer_error
        if error is not None:
            _attach_actual(error, outcome)
            if cancelled is not None:
                raise error from cancelled
            raise error
        if cancelled is not None:
            _attach_actual(cancelled, outcome)
            raise cancelled
        return outcome
