"""数据库专用线程、有界等待队列与结果接手。

入队截止、派发与撤回在同一临界区决定，形成唯一结果；已开始的
操作跟踪至实际结束，等待者取消不中止实际操作。连接的创建、使
用与关闭均由数据库线程完成。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Callable

from camctl.contracts.values import OperationKey
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
    WithdrawalResult,
    WithdrawalStatus,
)
from camctl.persistence.runtime import OwnedConnection

_ConnectionFactory = Callable[[], OwnedConnection]


class _Admission:
    """一个等待队列空位的入队请求。

    state 只在队列核心的临界区内迁移：PENDING → GRANTED（取得空
    位并入队）或 PENDING → TIMED_OUT（截止前未取得空位，或被撤
    回、关闭结算）。
    """

    __slots__ = ("job", "result", "gate", "state")

    PENDING = "pending"
    GRANTED = "granted"
    TIMED_OUT = "timed_out"

    def __init__(self, job: DbJob, result: asyncio.Future) -> None:
        self.job = job
        self.result = result
        self.gate: asyncio.Future = asyncio.get_running_loop().create_future()
        self.state = self.PENDING


class QueueCore:
    """入队、派发与撤回的决策核心。

    本类只维护决策状态，不持有线程、锁或事件循环；调用方保证各
    方法在同一线程锁内串行调用。容量按等待条目计量，取出或确认
    撤回时释放。
    """

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise DbExecutorError(f"队列容量必须是正整数: {capacity!r}")
        self._capacity = capacity
        self._business: deque[DbJob] = deque()
        self._snapshot: deque[DbJob] = deque()
        self._dispatched: set[OperationKey] = set()
        self._admissions: deque[_Admission] = deque()

    def queued_count(self) -> int:
        return len(self._business) + len(self._snapshot)

    def try_admit(self, job: DbJob) -> bool:
        """有空位则立即入队；满载时返回 False，由调用方登记等待。"""
        if self.queued_count() >= self._capacity:
            return False
        self._queue_of(job).append(job)
        return True

    def register_admission(self, job: DbJob, result: asyncio.Future) -> _Admission:
        admission = _Admission(job, result)
        self._admissions.append(admission)
        return admission

    def admit_next_waiting(self) -> _Admission | None:
        """把刚释放的空位交给最早的仍在等待的入队请求。"""
        while self._admissions:
            admission = self._admissions.popleft()
            if admission.state is not _Admission.PENDING:
                continue
            admission.state = _Admission.GRANTED
            self._queue_of(admission.job).append(admission.job)
            return admission
        return None

    def settle_timeout(self, admission: _Admission) -> bool:
        """入队截止到达时结算等待请求。

        返回 True 表示该请求未取得空位（本次操作以后不会执行）；
        返回 False 表示空位竞争已经以入队取胜，操作照常执行。
        """
        if admission.state is _Admission.GRANTED:
            return False
        admission.state = _Admission.TIMED_OUT
        return True

    def pop_next(self) -> DbJob | None:
        """取出下一项操作：业务优先，同优先级按到达顺序。"""
        if self._business:
            job = self._business.popleft()
        elif self._snapshot:
            job = self._snapshot.popleft()
        else:
            return None
        self._dispatched.add(job.key)
        return job

    def withdraw(self, key: OperationKey) -> WithdrawalStatus | None:
        """撤回一项尚未执行的操作；不在范围内时返回 None。

        已派发（撤回与派发竞争已由派发取胜）时报告仍在执行；确
        认撤回排队条目时释放所占容量，由调用方把空位交给等待者。
        """
        if key in self._dispatched:
            return WithdrawalStatus.DISPATCHED
        for queue in (self._business, self._snapshot):
            for index, queued in enumerate(queue):
                if queued.key == key:
                    del queue[index]
                    return WithdrawalStatus.WITHDRAWN
        for index, admission in enumerate(self._admissions):
            if admission.job.key == key and admission.state is _Admission.PENDING:
                del self._admissions[index]
                admission.state = _Admission.TIMED_OUT
                return WithdrawalStatus.WITHDRAWN
        return None

    def withdraw_all(self) -> tuple[list[DbJob], list[_Admission]]:
        """关闭时撤回全部未开始操作：排队任务与等待入队请求。"""
        jobs: list[DbJob] = list(self._business) + list(self._snapshot)
        self._business.clear()
        self._snapshot.clear()
        admissions: list[_Admission] = []
        while self._admissions:
            admission = self._admissions.popleft()
            if admission.state is _Admission.PENDING:
                admission.state = _Admission.TIMED_OUT
                admissions.append(admission)
        return jobs, admissions

    def complete(self, key: OperationKey) -> None:
        self._dispatched.discard(key)

    def has_queued(self) -> bool:
        return bool(self._business or self._snapshot)

    def _queue_of(self, job: DbJob) -> deque[DbJob]:
        return self._business if job.priority is DbPriority.BUSINESS else self._snapshot


class DbExecutor:
    """单连接数据库线程执行器。

    submit_write 与 submit_read 返回结果句柄（asyncio.Future）：
    等待者取消后句柄仍然有效，需要接手时以 asyncio.shield 保留
    句柄并交给责任监督器。入队等待超过时限的操作以 NOT_EXECUTED
    结果返回，并且以后也不会执行。
    """

    def __init__(
        self,
        connection_factory: _ConnectionFactory,
        *,
        capacity: int = 64,
        enqueue_timeout_seconds: float = 9.0,
    ) -> None:
        if isinstance(enqueue_timeout_seconds, bool) or enqueue_timeout_seconds <= 0:
            raise DbExecutorError(f"入队时限必须是正数秒: {enqueue_timeout_seconds!r}")
        self._factory = connection_factory
        self._enqueue_timeout = enqueue_timeout_seconds
        self._core = QueueCore(capacity)
        self._lock = threading.Lock()
        self._work_available = threading.Condition(self._lock)
        self._results: dict[OperationKey, asyncio.Future] = {}
        self._stopping = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._loop = asyncio.get_running_loop()

    # -- 提交与撤回 ---------------------------------------------------

    def submit_write(self, job: DbJob) -> asyncio.Future:
        """提交一项写操作，返回 DbOutcome 的等待句柄。"""
        if job.kind is not DbJobKind.WRITE:
            raise DbExecutorError("submit_write 只接受写任务")
        return self._submit(job)

    def submit_read(self, job: DbJob) -> asyncio.Future:
        """提交一项读操作，返回 ReadReceipt 的等待句柄。"""
        if job.kind is not DbJobKind.READ:
            raise DbExecutorError("submit_read 只接受读任务")
        return self._submit(job)

    async def withdraw(self, key: OperationKey) -> WithdrawalResult:
        """撤回一项操作；不在执行器范围内时明确报错。"""
        with self._lock:
            status = self._core.withdraw(key)
            if status is None:
                raise DbExecutorError(f"操作 {key} 不在执行器范围内")
            if status is WithdrawalStatus.WITHDRAWN:
                granted = self._core.admit_next_waiting()
                if granted is not None:
                    self._schedule_grant(granted)
                result = self._results.pop(key, None)
        if status is WithdrawalStatus.WITHDRAWN and result is not None and not result.done():
            result.set_result(DbOutcome(kind=DbOutcomeKind.NOT_EXECUTED))
        return WithdrawalResult(key=key, status=status)

    async def close(self) -> None:
        """停止接纳，撤回未开始操作，等待已开始操作实际结束。"""
        released: list[asyncio.Future] = []
        with self._lock:
            if self._closed or self._stopping:
                return
            self._stopping = True
            jobs, admissions = self._core.withdraw_all()
            for job in jobs:
                released.append(self._results.pop(job.key, None))  # type: ignore[arg-type]
            for admission in admissions:
                released.append(self._results.pop(admission.job.key, None))  # type: ignore[arg-type]
            self._work_available.notify_all()
        for result in released:
            if result is not None and not result.done():
                result.set_result(DbOutcome(kind=DbOutcomeKind.NOT_EXECUTED))
        thread = self._thread
        if thread is not None:
            await self._loop.run_in_executor(None, thread.join)
        with self._lock:
            self._closed = True

    # -- 内部：提交路径 -----------------------------------------------

    def _submit(self, job: DbJob) -> asyncio.Future:
        result: asyncio.Future = self._loop.create_future()
        admission: _Admission | None = None
        with self._lock:
            if self._closed or self._stopping:
                raise DbExecutorError("执行器已关闭，不再接纳操作")
            self._results[job.key] = result
            if self._core.try_admit(job):
                self._ensure_thread_locked()
                self._work_available.notify_all()
            else:
                admission = self._core.register_admission(job, result)
                self._ensure_thread_locked()
        if admission is not None:
            self._loop.create_task(self._await_admission(admission))
        return result

    async def _await_admission(self, admission: _Admission) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(admission.gate), self._enqueue_timeout)
        except TimeoutError:
            with self._lock:
                timed_out = self._core.settle_timeout(admission)
                result = self._results.pop(admission.job.key, None) if timed_out else None
            if result is not None and not result.done():
                result.set_result(
                    DbOutcome(
                        kind=DbOutcomeKind.NOT_EXECUTED,
                        error=DbEnqueueTimeoutError(
                            f"操作 {admission.job.key} 等待队列空位超过 {self._enqueue_timeout}s"
                        ),
                    )
                )

    # -- 内部：线程与执行 ---------------------------------------------

    def _ensure_thread_locked(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="camctl-db", daemon=True)
            self._thread.start()

    def _schedule_grant(self, admission: _Admission) -> None:
        def resolve() -> None:
            if not admission.gate.done():
                admission.gate.set_result(True)

        self._loop.call_soon_threadsafe(resolve)

    def _run(self) -> None:
        owned: OwnedConnection | None = None
        while True:
            with self._lock:
                while not self._stopping and not self._core.has_queued():
                    self._work_available.wait(timeout=0.05)
                if not self._core.has_queued():
                    if self._stopping:
                        break
                    continue
                job = self._core.pop_next()
                granted = self._core.admit_next_waiting()
                if granted is not None:
                    self._schedule_grant(granted)
            if owned is None:
                try:
                    owned = self._factory()
                except Exception as error:
                    self._deliver_factory_failure(job, error)
                    with self._lock:
                        self._core.complete(job.key)
                    continue
            owned = self._execute(job, owned)
            with self._lock:
                self._core.complete(job.key)
        if owned is not None:
            owned.connection.close()

    def _execute(self, job: DbJob, owned: OwnedConnection) -> OwnedConnection | None:
        """在数据库线程执行一项操作；返回继续使用的连接。

        提交或回滚结果未知时在所属线程关闭连接并返回 None，下一
        项操作重新打开。
        """
        if job.kind is DbJobKind.READ:
            try:
                value = job.execute(owned)
            except Exception as error:
                payload: Any = ReadReceipt(error=DbReadError(f"读取 {job.description} 失败: {error}"))
            else:
                payload = ReadReceipt(value=value)
            self._loop.call_soon_threadsafe(self._finish, job, payload)
            return owned
        try:
            outcome = job.execute(owned)
            if not isinstance(outcome, DbOutcome):
                outcome = DbOutcome(
                    kind=DbOutcomeKind.UNKNOWN,
                    error=DbExecutorError(f"写任务 {job.description} 未返回分类结果"),
                )
        except Exception as error:
            outcome = DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=error)
        keep: OwnedConnection | None = owned
        if outcome.kind is DbOutcomeKind.UNKNOWN:
            # 提交或回滚结果未知：先在所属线程关闭失效连接，再通知结果。
            owned.connection.close()
            keep = None
        self._loop.call_soon_threadsafe(self._finish, job, outcome)
        return keep

    def _finish(self, job: DbJob, payload: Any) -> None:
        result = self._results.pop(job.key, None)
        if result is not None and not result.done():
            result.set_result(payload)

    def _deliver_factory_failure(self, job: DbJob, error: Exception) -> None:
        if job.kind is DbJobKind.READ:
            payload: Any = ReadReceipt(error=DbReadError(f"打开数据库连接失败: {error}"))
        else:
            payload = DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=error)
        self._loop.call_soon_threadsafe(self._finish, job, payload)
