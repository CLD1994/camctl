"""业务协程提交完整数据库操作；连接始终留在所属worker。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
import inspect
import sqlite3
from typing import TypeVar

from camctl.contracts.values import OperationKey, new_operation_key
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (DbExecutorError, DbJob, DbJobKind, DbOutcome,
    DbPriority, DbReadError, ReadReceipt, _attach_db_outcome)
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


@dataclass(frozen=True)
class _RequiredResult:
    value: object = None
    error: BaseException | None = None


async def _complete_required(awaitable):
    try:
        return _RequiredResult(value=await awaitable)
    except BaseException as error:
        # Task 的取消状态不保留重复观察同一控制错误的语义。
        return _RequiredResult(error=error)


async def _deliver_required(task):
    result, cancelled = await _settle(task)
    if result.error is not None:
        if cancelled is not None:
            raise result.error from cancelled
        raise result.error
    if cancelled is not None:
        if isinstance(result.value, DbOutcome):
            _attach_actual(cancelled, result.value)
        raise cancelled
    return result.value


async def settle_required(awaitable: Awaitable[T]) -> T:
    """完整必需阶段先应用实际结果，再传播原错误或等待取消。

    协程由本边界包装；外部 Task 的原控制错误由其拥有者稳定保存。
    """
    return await _deliver_required(asyncio.create_task(_complete_required(awaitable)))


class SharedSave:
    """同一拥有者的原保存共同等待；回执和内存应用均由原任务完成。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def run(self, save_and_apply: Callable[[], Awaitable[T]]) -> T:
        """保留原成功或失败结果；晚到等待者不能隐式重放保存。"""
        if self._task is None:
            self._task = asyncio.create_task(_complete_required(save_and_apply()))
        return await _deliver_required(self._task)

    def reset(self) -> None:
        """调用方明确开始下一阶段或核实；不能替换仍在进行的原保存。"""
        if self._task is not None and not self._task.done():
            raise DbExecutorError("原保存尚未完成，不能开始新的保存阶段")
        self._task = None

    def retry_failed(self) -> None:
        """明确恢复原申请；成功与尚在进行的保存继续保留原任务。"""
        task = self._task
        if task is not None and task.done() and (task.cancelled()
                or task.exception() is not None or task.result().error is not None):
            self._task = None


def _check_value(value):
    # callback自身负责既定有限值类型；端口不反射遍历业务对象图。
    if isinstance(value, (OwnedConnection, sqlite3.Connection, sqlite3.Cursor, Iterator, AsyncIterator)) or inspect.isawaitable(value):
        raise DbExecutorError('数据库callback只能返回有限值，不能交付连接、游标或延迟执行对象')


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
                error = (payload.error.__cause__ if isinstance(payload.error, DbReadError)
                         and payload.error.__cause__ is not None else payload.error)
                if cancelled is not None:
                    raise error from cancelled
                raise error
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
            try:
                _check_value(value.value)
            except BaseException as error:
                return replace(value, value=None, handoff_error=error)
            return value
        job = DbJob(key, description, DbJobKind.WRITE, DbPriority.BUSINESS, execute)
        outcome, cancelled = await self._wait(self._executor.submit_write(job), key)
        if not isinstance(outcome, DbOutcome):
            raise DbExecutorError('业务写入没有实际分类结果')
        error = outcome.control or outcome.handoff_error or outcome.observer_error
        if error is not None:
            _attach_actual(error, outcome)
            if cancelled is not None:
                raise error from cancelled
            raise error
        if cancelled is not None:
            _attach_actual(cancelled, outcome)
            raise cancelled
        return outcome
