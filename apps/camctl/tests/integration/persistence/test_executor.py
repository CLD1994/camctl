"""P2 数据库执行器的组件集成测试。

真实专用线程、事件循环与结果通知组合；连接用受接口约束的替身，
另以真实 SQLite 连接验证完整组合。
"""

from __future__ import annotations

import asyncio
import threading

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


class TestAdmissionAndTimeout:
    async def test_admitted_job_has_no_queue_timeout(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=0.05
        )
        try:
            blocking = BlockingWrite()
            handle1 = executor.submit_write(blocking.job)
            await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            # 阻塞任务已派发并释放条目：后续操作立即入队，等待派发不设时限。
            quick = _write_job("second")
            handle2 = executor.submit_write(quick)
            await asyncio.sleep(0.2)
            assert not handle2.done()
            blocking.release.set()
            outcome1 = await handle1
            outcome2 = await handle2
            assert outcome1.kind is DbOutcomeKind.COMPLETED
            assert outcome2.kind is DbOutcomeKind.COMPLETED
            assert outcome2.value == "second"
        finally:
            blocking.release.set()
            await executor.close()

    async def test_enqueue_timeout_returns_not_executed_and_never_executes(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=0.05
        )
        blocking = BlockingWrite()
        try:
            handle1 = executor.submit_write(blocking.job)
            await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
            queued = _write_job("queued")
            handle2 = executor.submit_write(queued)
            waiting = _write_job("waiting")
            handle3 = executor.submit_write(waiting)
            timed_out = await handle3
            assert timed_out.kind is DbOutcomeKind.NOT_EXECUTED
            assert isinstance(timed_out.error, DbEnqueueTimeoutError)
            blocking.release.set()
            outcome1 = await handle1
            outcome2 = await handle2
            assert outcome1.kind is DbOutcomeKind.COMPLETED
            assert outcome2.kind is DbOutcomeKind.COMPLETED
            # 超时的操作以后也不会执行：它已不在执行器范围内。
            with pytest.raises(DbExecutorError):
                await executor.withdraw(waiting.key)
        finally:
            blocking.release.set()
            await executor.close()

    async def test_withdraw_queued_job_returns_not_executed(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        blocking = BlockingWrite()
        handle1 = executor.submit_write(blocking.job)
        await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
        queued = _write_job("queued")
        handle2 = executor.submit_write(queued)
        result = await executor.withdraw(queued.key)
        assert result.status is WithdrawalStatus.WITHDRAWN
        outcome2 = await handle2
        assert outcome2.kind is DbOutcomeKind.NOT_EXECUTED
        assert outcome2.error is None
        blocking.release.set()
        outcome1 = await handle1
        assert outcome1.kind is DbOutcomeKind.COMPLETED
        await executor.close()


class TestHandover:
    async def test_started_job_survives_waiter_cancel(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        blocking = BlockingWrite()
        deliveries: list[str] = []
        handle = executor.submit_write(blocking.job)

        async def waiter() -> None:
            try:
                outcome = await asyncio.shield(handle)
                deliveries.append(outcome.value)
            except asyncio.CancelledError:
                # 等待者取消：句柄保留给责任拥有者。
                raise

        waiter_task = asyncio.create_task(waiter())
        await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        assert deliveries == []
        blocking.release.set()
        outcome = await handle
        assert outcome.kind is DbOutcomeKind.COMPLETED
        deliveries.append(outcome.value)
        assert deliveries == ["done"]
        assert blocking.executions == 1
        await executor.close()


class TestFailurePaths:
    async def test_write_unknown_closes_connection_and_reopens(self) -> None:
        created: list[FakeOwned] = []

        def factory() -> FakeOwned:
            owned = FakeOwned(FakeConnection())
            created.append(owned)
            return owned

        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)

        def explode(owned) -> DbOutcome:
            raise RuntimeError("commit result unknown")

        bad = DbJob(
            key=new_operation_key(),
            description="unknown-write",
            kind=DbJobKind.WRITE,
            priority=DbPriority.BUSINESS,
            execute=explode,
        )
        outcome = await executor.submit_write(bad)
        assert outcome.kind is DbOutcomeKind.UNKNOWN
        assert isinstance(outcome.error, RuntimeError)
        assert created[0].connection.closed is True
        outcome2 = await executor.submit_write(_write_job("after"))
        assert outcome2.kind is DbOutcomeKind.COMPLETED
        assert len(created) == 2
        await executor.close()

    async def test_read_error_returns_receipt_error(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=1, enqueue_timeout_seconds=1.0
        )

        def explode(owned):
            raise RuntimeError("disk image malformed")

        bad_read = DbJob(
            key=new_operation_key(),
            description="bad-read",
            kind=DbJobKind.READ,
            priority=DbPriority.BUSINESS,
            execute=explode,
        )
        receipt: ReadReceipt = await executor.submit_read(bad_read)
        assert receipt.value is None
        assert isinstance(receipt.error, DbReadError)
        good: ReadReceipt = await executor.submit_read(_read_job())
        assert good.value == 7
        assert good.error is None
        await executor.close()

    async def test_factory_failure_rolls_back_write(self) -> None:
        attempts: list[int] = []

        def factory() -> FakeOwned:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("cannot open database")
            return FakeOwned(FakeConnection())

        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)
        outcome = await executor.submit_write(_write_job("first"))
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(outcome.error, RuntimeError)
        outcome2 = await executor.submit_write(_write_job("second"))
        assert outcome2.kind is DbOutcomeKind.COMPLETED
        await executor.close()


class TestPriorityAndClose:
    async def test_business_runs_before_snapshot(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=4, enqueue_timeout_seconds=1.0
        )
        blocking = BlockingWrite()
        handle0 = executor.submit_write(blocking.job)
        await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
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
        handle_business = executor.submit_write(record("business"))
        blocking.release.set()
        await handle0
        await handle_business
        await handle_snapshot
        assert order == ["business", "snapshot"]
        await executor.close()

    async def test_close_drains_active_and_withdraws_queued(self) -> None:
        stock: list[FakeOwned] = []
        executor = DbExecutor(
            _fake_factory_reopen(stock), capacity=2, enqueue_timeout_seconds=1.0
        )
        blocking = BlockingWrite()
        handle1 = executor.submit_write(blocking.job)
        await asyncio.get_running_loop().run_in_executor(None, blocking.started.wait, 5)
        handle2 = executor.submit_write(_write_job("queued"))
        closing = asyncio.create_task(executor.close())
        await asyncio.sleep(0.02)
        outcome2 = await handle2
        assert outcome2.kind is DbOutcomeKind.NOT_EXECUTED
        blocking.release.set()
        await closing
        outcome1 = await handle1
        assert outcome1.kind is DbOutcomeKind.COMPLETED
        with pytest.raises(DbExecutorError):
            executor.submit_write(_write_job("late"))


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

        executor = DbExecutor(factory, capacity=2, enqueue_timeout_seconds=1.0)

        def write_marker(owned) -> DbOutcome:
            connection = owned.connection
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE runtime_state SET acknowledged_wm = acknowledged_wm")
            connection.execute("COMMIT")
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value="written")

        write_job = DbJob(
            key=new_operation_key(),
            description="insert-runtime-state",
            kind=DbJobKind.WRITE,
            priority=DbPriority.BUSINESS,
            execute=write_marker,
        )
        outcome = await executor.submit_write(write_job)
        assert outcome.kind is DbOutcomeKind.COMPLETED

        def read_marker(owned) -> int:
            row = owned.connection.execute(
                "SELECT COUNT(*) FROM runtime_state"
            ).fetchone()
            return int(row[0])

        read_job = DbJob(
            key=new_operation_key(),
            description="count-runtime-state",
            kind=DbJobKind.READ,
            priority=DbPriority.BUSINESS,
            execute=read_marker,
        )
        receipt: ReadReceipt = await executor.submit_read(read_job)
        assert receipt.error is None
        # UPDATE 不改变行数：写事务经真实线程提交后读取方可见。
        assert receipt.value == 1
        await executor.close()
