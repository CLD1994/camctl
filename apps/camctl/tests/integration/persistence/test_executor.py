"""P2 数据库执行器的组件集成测试。

真实专用线程、事件循环与结果通知组合；连接用受接口约束的替身，
另以真实 SQLite 连接验证完整组合。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing as closing_connection
import threading
from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (
    DbEnqueueTimeoutError,
    DbExecutorError,
    DbJob,
    DbJobKind,
    DbOutcome,
    DbOutcomeKind,
    DbPriority,
    DbReadError,
    ReadReceipt,
    WithdrawalStatus,
)
from camctl.persistence.runtime import DbOpenMode, DbConfig, open_existing

pytestmark = pytest.mark.asyncio


class FakeConnection:
    """受 OwnedConnection 结构约束的替身；记录关闭事实。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeOwned:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.metadata = None


async def test_slow_business_reads_distinguish_execution_from_queue(monkeypatch):
    from camctl.logging_runtime.events import event_logging, monitor_slow_stage
    from camctl.persistence import business

    class Channel:
        def __init__(self):
            self.records = []

        def slog(self, record):
            self.records.append(json.loads(record.rendered_message()))

    def monitor(stage, **kwargs):
        return monitor_slow_stage(stage, threshold_s=0.3, **kwargs)

    monkeypatch.setattr(business, 'monitor_slow_stage', monitor)
    channel = Channel()
    entered, release = threading.Event(), threading.Event()
    pending = []
    closing = None
    executor = DbExecutor(_fake_factory([]))
    try:
        database = business.BusinessDatabase(executor, None)

        def blocking(owned):
            entered.set()
            release.wait(timeout=5)
            return 1

        try:
            with event_logging(channel):
                first = asyncio.create_task(database.read(blocking, description='first'))
                pending.append(first)
                assert await asyncio.to_thread(entered.wait, 2)
                second = asyncio.create_task(database.read(lambda owned: 2, description='second'))
                pending.append(second)
                await asyncio.sleep(0.4)
                starts = {r['description']: r for r in channel.records if r['event'] == 'started'}
                assert starts['first']['phase'] == 'executing', channel.records
                assert starts['second']['phase'] == 'queued'
                release.set()
                assert await asyncio.wait_for(asyncio.shield(asyncio.gather(first, second)), 5) == [1, 2]
        finally:
            release.set()
        returned = {r['description']: r for r in channel.records if r['event'] == 'returned'}
        assert returned['first']['execution_ms'] >= 300
        assert returned['second']['queue_ms'] >= 300
        assert all(r['level'] == 'INFO' for r in channel.records)
    finally:
        await _finish_executor(executor, pending, release, closing)


class BlockingWrite:
    """阻塞到外部放行的写任务替身。"""

    def __init__(self) -> None:
        self.key = new_operation_key()
        self.started = threading.Event()
        self.release = threading.Event()
        self.executions = 0

    @property
    def job(self) -> DbJob:
        return DbJob(
            key=self.key,
            description="blocking-write",
            kind=DbJobKind.WRITE,
            priority=DbPriority.BUSINESS,
            execute=self._execute,
        )

    def _execute(self, owned) -> DbOutcome:
        self.executions += 1
        self.started.set()
        self.release.wait(timeout=5)
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value="done")


def _write_job(value: str, *, priority: DbPriority = DbPriority.BUSINESS) -> DbJob:
    return DbJob(
        key=new_operation_key(),
        description="quick-write",
        kind=DbJobKind.WRITE,
        priority=priority,
        execute=lambda owned: DbOutcome(kind=DbOutcomeKind.COMPLETED, value=value),
    )


def _read_job() -> DbJob:
    return DbJob(
        key=new_operation_key(),
        description="quick-read",
        kind=DbJobKind.READ,
        priority=DbPriority.BUSINESS,
        execute=lambda owned: 7,
    )


def _fake_factory(stock: list[FakeOwned]):
    def factory() -> FakeOwned:
        if not stock:
            stock.append(FakeOwned(FakeConnection()))
        return stock[0]

    return factory


async def _finish_executor(executor, pending, release, closing):
    if release is not None:
        release.set()
    try:
        if pending:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending, return_exceptions=True)), 5)
    finally:
        if closing is None:
            closing = asyncio.create_task(executor.close())
        await asyncio.wait_for(asyncio.shield(closing), 5)


class TestAdmissionAndTimeout:
    async def test_admitted_job_has_no_queue_timeout(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=0.05
        )
        try:
            try:
                blocking = BlockingWrite()
                release = blocking.release
                handle1 = executor.submit_write(blocking.job)
                pending.append(handle1)
                assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
                # 阻塞任务已派发并释放条目：后续操作立即入队，等待派发不设时限。
                quick = _write_job("second")
                handle2 = executor.submit_write(quick)
                pending.append(handle2)
                await asyncio.sleep(0.2)
                assert not handle2.done()
                blocking.release.set()
                outcome1 = await asyncio.wait_for(asyncio.shield(handle1), 5)
                outcome2 = await asyncio.wait_for(asyncio.shield(handle2), 5)
                assert outcome1.kind is DbOutcomeKind.COMPLETED
                assert outcome2.kind is DbOutcomeKind.COMPLETED
                assert outcome2.value == "second"
            finally:
                blocking.release.set()
        finally:
            await _finish_executor(executor, pending, release, closing)

    async def test_enqueue_timeout_returns_not_executed_and_never_executes(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=0.05
        )
        try:
            blocking = BlockingWrite()
            release = blocking.release
            try:
                handle1 = executor.submit_write(blocking.job)
                pending.append(handle1)
                assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
                queued = _write_job("queued")
                handle2 = executor.submit_write(queued)
                pending.append(handle2)
                executions = []
                waiting = replace(_write_job("waiting"), execute=lambda owned: executions.append(1))
                handle3 = executor.submit_write(waiting)
                pending.append(handle3)
                timed_out = await asyncio.wait_for(asyncio.shield(handle3), 5)
                assert timed_out.kind is DbOutcomeKind.NOT_EXECUTED
                assert isinstance(timed_out.error, DbEnqueueTimeoutError)
                blocking.release.set()
                outcome1 = await asyncio.wait_for(asyncio.shield(handle1), 5)
                outcome2 = await asyncio.wait_for(asyncio.shield(handle2), 5)
                assert outcome1.kind is DbOutcomeKind.COMPLETED
                assert outcome2.kind is DbOutcomeKind.COMPLETED
                # 超时的操作以后也不会执行：它已不在执行器范围内。
                with pytest.raises(DbExecutorError):
                    await executor.withdraw(waiting.key)
            finally:
                blocking.release.set()
        finally:
            await _finish_executor(executor, pending, release, closing)
        assert executions == []

    async def test_withdraw_queued_job_returns_not_executed(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        try:
            blocking = BlockingWrite()
            release = blocking.release
            handle1 = executor.submit_write(blocking.job)
            pending.append(handle1)
            assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            queued = _write_job("queued")
            handle2 = executor.submit_write(queued)
            pending.append(handle2)
            result = await executor.withdraw(queued.key)
            assert result.status is WithdrawalStatus.WITHDRAWN
            outcome2 = await asyncio.wait_for(asyncio.shield(handle2), 5)
            assert outcome2.kind is DbOutcomeKind.NOT_EXECUTED
            assert outcome2.error is None
            blocking.release.set()
            outcome1 = await asyncio.wait_for(asyncio.shield(handle1), 5)
            assert outcome1.kind is DbOutcomeKind.COMPLETED
        finally:
            await _finish_executor(executor, pending, release, closing)


class TestHandover:
    async def test_duplicate_in_flight_key_preserves_worker_result_and_later_verification(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(_fake_factory_reopen(stock), capacity=2)
        try:
            blocking = BlockingWrite()
            release = blocking.release
            original = executor.submit_write(blocking.job)
            pending.append(original)
            try:
                assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
                duplicate = replace(_write_job("wrong result"), key=blocking.key)
                with pytest.raises(DbExecutorError):
                    executor.submit_write(duplicate)
                assert not original.done()
                blocking.release.set()
                assert (await asyncio.wait_for(asyncio.shield(original), 5)).value == "done"
                assert blocking.executions == 1
                verification = executor.submit_write(replace(_write_job("verified"), key=blocking.key))
                pending.append(verification)
                assert (await asyncio.wait_for(asyncio.shield(verification), 5)).value == "verified"
            finally:
                blocking.release.set()
        finally:
            await _finish_executor(executor, pending, release, closing)

    async def test_started_job_survives_waiter_cancel(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        try:
            blocking = BlockingWrite()
            release = blocking.release
            deliveries: list[str] = []
            handle = executor.submit_write(blocking.job)
            pending.append(handle)

            async def waiter() -> None:
                try:
                    outcome = await asyncio.shield(handle)
                    deliveries.append(outcome.value)
                except asyncio.CancelledError:
                    # 等待者取消：句柄保留给责任拥有者。
                    raise

            waiter_task = asyncio.create_task(waiter())
            pending.append(waiter_task)
            assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            waiter_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter_task
            assert deliveries == []
            blocking.release.set()
            outcome = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome.kind is DbOutcomeKind.COMPLETED
            deliveries.append(outcome.value)
            assert deliveries == ["done"]
            assert blocking.executions == 1
        finally:
            await _finish_executor(executor, pending, release, closing)


class TestFailurePaths:
    async def test_write_unknown_closes_connection_and_reopens(self) -> None:
        created: list[FakeOwned] = []

        def factory() -> FakeOwned:
            owned = FakeOwned(FakeConnection())
            created.append(owned)
            return owned

        pending = []
        closing = None
        release = None
        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)
        try:

            def explode(owned) -> DbOutcome:
                raise RuntimeError("commit result unknown")

            bad = DbJob(
                key=new_operation_key(),
                description="unknown-write",
                kind=DbJobKind.WRITE,
                priority=DbPriority.BUSINESS,
                execute=explode,
            )
            handle = executor.submit_write(bad)
            pending.append(handle)
            outcome = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome.kind is DbOutcomeKind.UNKNOWN
            assert isinstance(outcome.error, RuntimeError)
            assert created[0].connection.closed is True
            handle = executor.submit_write(replace(_write_job("after"), key=bad.key))
            pending.append(handle)
            outcome2 = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome2.kind is DbOutcomeKind.COMPLETED
            assert len(created) == 2
        finally:
            await _finish_executor(executor, pending, release, closing)

    async def test_read_error_returns_receipt_error(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=1.0
        )
        try:

            def explode(owned):
                raise RuntimeError("disk image malformed")

            bad_read = DbJob(
                key=new_operation_key(),
                description="bad-read",
                kind=DbJobKind.READ,
                priority=DbPriority.BUSINESS,
                execute=explode,
            )
            handle = executor.submit_read(bad_read)
            pending.append(handle)
            receipt: ReadReceipt = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert receipt.value is None
            assert isinstance(receipt.error, DbReadError)
            handle = executor.submit_read(_read_job())
            pending.append(handle)
            good: ReadReceipt = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert good.value == 7
            assert good.error is None
        finally:
            await _finish_executor(executor, pending, release, closing)

    async def test_factory_failure_rolls_back_write(self) -> None:
        attempts: list[int] = []

        def factory() -> FakeOwned:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("cannot open database")
            return FakeOwned(FakeConnection())

        pending = []
        closing = None
        release = None
        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)
        try:
            first = _write_job("first")
            handle = executor.submit_write(first)
            pending.append(handle)
            outcome = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
            assert isinstance(outcome.error, RuntimeError)
            with pytest.raises(DbExecutorError):
                await executor.withdraw(first.key)
            handle = executor.submit_write(replace(_write_job("second"), key=first.key))
            pending.append(handle)
            outcome2 = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome2.kind is DbOutcomeKind.COMPLETED
        finally:
            await _finish_executor(executor, pending, release, closing)


class TestPriorityAndClose:
    async def test_business_runs_before_snapshot(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=4, enqueue_timeout_seconds=1.0
        )
        try:
            blocking = BlockingWrite()
            release = blocking.release
            handle0 = executor.submit_write(blocking.job)
            pending.append(handle0)
            assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            order: list[str] = []

            def record(name: str):
                def execute(owned) -> DbOutcome:
                    order.append(name)
                    return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=name)

                return DbJob(
                    key=new_operation_key(),
                    description=f"record-{name}",
                    kind=DbJobKind.WRITE,
                    priority=DbPriority.SNAPSHOT if name == "snapshot" else DbPriority.BUSINESS,
                    execute=execute,
                )

            handle_snapshot = executor.submit_write(record("snapshot"))
            pending.append(handle_snapshot)
            handle_business = executor.submit_write(record("business"))
            pending.append(handle_business)
            blocking.release.set()
            await asyncio.wait_for(asyncio.shield(handle0), 5)
            await asyncio.wait_for(asyncio.shield(handle_business), 5)
            await asyncio.wait_for(asyncio.shield(handle_snapshot), 5)
            assert order == ["business", "snapshot"]
        finally:
            await _finish_executor(executor, pending, release, closing)

    async def test_close_drains_active_and_withdraws_queued(self) -> None:
        stock: list[FakeOwned] = []
        pending = []
        closing = None
        release = None
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        try:
            blocking = BlockingWrite()
            release = blocking.release
            handle1 = executor.submit_write(blocking.job)
            pending.append(handle1)
            assert await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            handle2 = executor.submit_write(_write_job("queued"))
            pending.append(handle2)
            closing = asyncio.create_task(executor.close())
            await asyncio.sleep(0.02)
            outcome2 = await asyncio.wait_for(asyncio.shield(handle2), 5)
            assert outcome2.kind is DbOutcomeKind.NOT_EXECUTED
            assert not closing.done()
            blocking.release.set()
            await asyncio.wait_for(asyncio.shield(closing), 5)
            outcome1 = await asyncio.wait_for(asyncio.shield(handle1), 5)
            assert outcome1.kind is DbOutcomeKind.COMPLETED
            with pytest.raises(DbExecutorError):
                executor.submit_write(_write_job("late"))
        finally:
            await _finish_executor(executor, pending, release, closing)


def _fake_factory_reopen(stock: list[FakeOwned]):
    """每次打开都返回同一持有物；UNKNOWN 关闭后重建。"""
    lock = threading.Lock()

    def factory() -> FakeOwned:
        with lock:
            if stock and not stock[0].connection.closed:
                return stock[0]
            stock.append(FakeOwned(FakeConnection()))
            return stock[-1]

    return factory


class TestRealSqliteCombination:
    async def test_real_thread_and_sqlite_commit_and_read(self, tmp_path) -> None:
        from .test_runtime import _create_valid_database

        db_path = tmp_path / "state.db"
        _create_valid_database(db_path)

        def factory():
            return open_existing(db_path, DbOpenMode.EXISTING_RW, DbConfig())

        pending = []
        closing = None
        release = None
        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)
        try:

            marker = 17

            def write_marker(owned) -> DbOutcome:
                connection = owned.connection
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE executor_marker (value INTEGER NOT NULL)")
                connection.execute("INSERT INTO executor_marker VALUES (?)", (marker,))
                connection.execute("COMMIT")
                return DbOutcome(kind=DbOutcomeKind.COMPLETED, value="written")

            write_job = DbJob(
                key=new_operation_key(),
                description="write-marker",
                kind=DbJobKind.WRITE,
                priority=DbPriority.BUSINESS,
                execute=write_marker,
            )
            handle = executor.submit_write(write_job)
            pending.append(handle)
            outcome = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert outcome.kind is DbOutcomeKind.COMPLETED

            def read_marker(owned) -> int:
                row = owned.connection.execute(
                    "SELECT value FROM executor_marker"
                ).fetchone()
                return int(row[0])

            read_job = DbJob(
                key=new_operation_key(),
                description="read-marker",
                kind=DbJobKind.READ,
                priority=DbPriority.BUSINESS,
                execute=read_marker,
            )
            handle = executor.submit_read(read_job)
            pending.append(handle)
            receipt: ReadReceipt = await asyncio.wait_for(asyncio.shield(handle), 5)
            assert receipt.error is None
            assert receipt.value == marker
            with closing_connection(sqlite3.connect(db_path)) as observer:
                assert observer.execute("SELECT value FROM executor_marker").fetchone() == (marker,)
        finally:
            await _finish_executor(executor, pending, release, closing)
