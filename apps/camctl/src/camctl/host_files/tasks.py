"""默认线程池文件任务与唯一修改资格。

文件任务经共享默认线程池执行：等待协程取消不释放实际资格，已开
始任务的实际结果由监督拥有者接手消费；排队任务撤回后确定不执行。
撤回与开始经同一锁裁决，只产生一个结果。停止请求幂等，等待结束
不依赖等待协程的生命周期。
"""

from __future__ import annotations

import asyncio
import enum
import threading
from contextvars import ContextVar
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Protocol

from camctl.session.supervision import (
    OwnedTask,
    ResponsibilityOwner,
    Supervisor,
)

__all__ = [
    "AsyncFileTask",
    "AsyncFileControl",
    "FileLease",
    "FileTask",
    "FileTaskError",
    "FileTaskExecutor",
    "FileTaskHandle",
    "FileTaskId",
    "FileTaskResult",
    "ThreadRunner",
]


class FileTaskError(ValueError):
    """文件任务使用错误：重复身份、同文件并发或接手契约违反。"""


@dataclass(frozen=True)
class FileTaskId:
    """文件任务身份；由调用方在提交前指定。"""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise FileTaskError("任务身份不能为空")


#: 任务实际执行体：接受停止通知，返回实际结果；在线程池中同步运行。
TaskBody = Callable[[threading.Event], Any]

_owned_scope: ContextVar[Any] = ContextVar("camctl_owned_file_scope", default=None)


@dataclass(frozen=True)
class FileTask:
    """一项待执行的文件任务：身份、互斥文件及同步执行体。"""

    task_id: FileTaskId
    file_id: int
    stage: str
    business: str
    body: TaskBody
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.file_id) is not int or self.file_id <= 0:
            raise FileTaskError(f"文件身份必须是正整数: {self.file_id!r}")
        if not self.stage or not self.business:
            raise FileTaskError("阶段与业务标签不能为空")

    @property
    def file_ids(self) -> tuple[int, ...]:
        return (self.file_id,)


@dataclass(frozen=True)
class AsyncFileTask:
    """持有全部关联文件的异步流程；阻塞工作仍由流程提交默认线程池。"""

    task_id: FileTaskId
    file_ids: tuple[int, ...]
    stage: str
    business: str
    body: Callable[["AsyncFileControl"], Awaitable[Any]]
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.file_ids, tuple) or not self.file_ids
                or any(type(identity) is not int or identity <= 0 for identity in self.file_ids)
                or len(set(self.file_ids)) != len(self.file_ids)):
            raise FileTaskError("关联文件必须是互不重复的正整数身份")
        if not self.stage or not self.business:
            raise FileTaskError("阶段与业务标签不能为空")


class AsyncFileControl:
    """同一停止请求供工具协程和文件线程观察；不取消执行协程。"""

    def __init__(self, state: "_TaskState") -> None:
        self._state = state

    @property
    def stop_requested(self) -> bool:
        return self._state.stop_event.is_set()

    @property
    def stop_event(self) -> threading.Event:
        return self._state.stop_event

    def request_stop(self) -> None:
        self._state.request_stop()

    async def requested(self) -> None:
        if not self.stop_requested:
            await self._state.stop_signal.wait()


@dataclass(frozen=True)
class FileTaskResult:
    """一次文件任务的实际结果。

    ran 为 False 表示撤回成功、任务从未执行；ran 为 True 且 error
    为空时为成功，value 可以是 None。失败时 error 保存实际诊断。
    """

    task_id: FileTaskId
    ran: bool
    value: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.value is not None and self.error is not None:
            raise FileTaskError("实际结果与执行错误不能同时提供")
        if not self.ran and (self.value is not None or self.error is not None):
            raise FileTaskError("未执行任务不携带结果或错误")


class FileLease:
    """一个文件的唯一修改资格；released 只反映实际结束。

    acquired 在任务实际开始执行时变为 True；released 在执行结束或
    撤回裁决后变为 True，恰好转换一次。
    """

    def __init__(self) -> None:
        self._acquired = False
        self._released = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def released(self) -> bool:
        return self._released

    def _mark_acquired(self) -> None:
        self._acquired = True

    def _mark_ended(self) -> None:
        self._released = True


class FileTaskHandle:
    """已开始文件任务的接手入口：等待实际结束并可请求停止。"""

    def __init__(self, state: "_TaskState", received: Callable[[], None]) -> None:
        self._state = state
        self._received = received

    @property
    def task_id(self) -> FileTaskId:
        return self._state.task.task_id

    @property
    def lease(self) -> FileLease:
        return self._state.lease

    def request_stop(self) -> None:
        """幂等请求停止；不等待，立即返回。"""
        self._state.request_stop()

    async def wait(self) -> FileTaskResult:
        """等待实际结果；取消本次等待不取消共享完成事实。"""
        completion = self._state.completion
        if completion.done():
            result = completion.result()
        else:
            result = await asyncio.shield(asyncio.wrap_future(completion))
        self._received()
        return result


class ThreadRunner(Protocol):
    """线程执行端口：把同步执行体交给线程池并返回其结果。"""

    def __call__(self, fn: Callable[[], FileTaskResult]) -> Awaitable[FileTaskResult]: ...


async def _run_in_default_pool(fn: Callable[[], FileTaskResult]) -> FileTaskResult:
    return await asyncio.to_thread(fn)


class _TaskStage(enum.Enum):
    QUEUED = "queued"
    STARTED = "started"
    WITHDRAWN = "withdrawn"
    FINISHED = "finished"


class _TaskState:
    """一项任务的运行时状态；由执行器锁保护裁决与结束。"""

    def __init__(self, task: FileTask | AsyncFileTask) -> None:
        self.task = task
        self.stage = _TaskStage.QUEUED
        self.stop_event = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.stop_signal = asyncio.Event()
        self.lease = FileLease()
        self.completion: Future[FileTaskResult] = Future()

    def request_stop(self) -> None:
        self.stop_event.set()
        if not self.completion.done():
            self.loop.call_soon_threadsafe(self.stop_signal.set)


class FileTaskExecutor:
    """经默认线程池执行文件任务并维护唯一修改资格。"""

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        thread_runner: ThreadRunner | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._runner: ThreadRunner = thread_runner or _run_in_default_pool
        self._lock = threading.Lock()
        self._tasks: dict[FileTaskId, _TaskState] = {}
        self._files: dict[int, FileTaskId] = {}
        self._runners: set[asyncio.Future] = set()

    async def run_file_task(
        self, task: FileTask, owner: ResponsibilityOwner
    ) -> FileTaskResult:
        """提交并等待一项文件任务的实际结果。

        等待协程被取消时：任务仍在排队则撤回（确定不执行）；已开
        始则登记接手，实际结果由 owner 消费。两种情况都不以等待
        取消作为实际结束。
        """
        state = self._admit(task)
        return await self._wait_for_execution(
            state, owner, lambda: self._runner(lambda: self._execute(state))
        )

    async def run_async_file_task(
        self, task: AsyncFileTask, owner: ResponsibilityOwner
    ) -> FileTaskResult:
        """执行持有多文件的异步任务，共用撤回、冲突及实际结果接手机制。"""
        scope = _owned_scope.get()
        if (scope is not None and scope[0] is self and scope[3] is asyncio.current_task()
                and set(task.file_ids) <= set(scope[1])):
            try:
                value = await task.body(scope[2])
                return FileTaskResult(task.task_id, ran=True, value=value)
            except Exception as error:
                return FileTaskResult(task.task_id, ran=True,
                                      error=f"{type(error).__name__}: {error}")
        state = self._admit(task)
        return await self._wait_for_execution(state, owner, lambda: self._execute_async(state))

    async def run_owned_async_file_task(self, task: AsyncFileTask) -> Any:
        """等待完整文件操作及原结果保存，取消后仍完成实际接手。

        执行体包括所属流程的结果保存，异常保持原类型。外部取消
        只中断原等待，已开始的任务经登记拥有者消费实际结果；
        重复取消不能使持有连接的调用方提前离开。
        """
        scope = _owned_scope.get()
        if (scope is not None and scope[0] is self and scope[3] is asyncio.current_task()
                and set(task.file_ids) <= set(scope[1])):
            return await task.body(scope[2])
        owner = _OwnedFileResult()

        async def body(control: AsyncFileControl) -> _OwnedFileOutcome:
            token = _owned_scope.set((self, task.file_ids, control, asyncio.current_task()))
            try:
                return _OwnedFileOutcome(value=await task.body(control))
            except BaseException as error:
                return _OwnedFileOutcome(error=error)
            finally:
                _owned_scope.reset(token)

        try:
            result = await self.run_async_file_task(replace(task, body=body), owner)
        except BaseException as interrupted:
            if not owner.handed_over:
                raise
            self.request_stop(task.task_id)
            drain = asyncio.create_task(self._supervisor.drain_required())
            while True:
                try:
                    settled = await asyncio.shield(drain)
                    break
                except asyncio.CancelledError:
                    if drain.done() and drain.cancelled():
                        raise
            if owner.result is not None:
                actual = owner.result.value
                if isinstance(actual, _OwnedFileOutcome) and actual.error is not None:
                    interrupted.add_note(
                        f"实际文件操作收场失败: {type(actual.error).__name__}: {actual.error}")
            for failure in settled.failed:
                interrupted.add_note(f"文件任务接手失败: {failure.error}")
            raise interrupted
        if not result.ran:
            raise FileTaskError("完整文件操作未执行，不能提供完成结果")
        actual = result.value
        if not isinstance(actual, _OwnedFileOutcome):
            raise FileTaskError(f"完整文件操作缺少实际结果: {result.error}")
        if actual.error is not None:
            raise actual.error
        return actual.value

    async def _wait_for_execution(
        self, state: _TaskState, owner: ResponsibilityOwner,
        start: Callable[[], Awaitable[FileTaskResult]],
    ) -> FileTaskResult:
        try:
            future = asyncio.ensure_future(start())
            self._runners.add(future)
            future.add_done_callback(self._observe_runner)
            result = await asyncio.shield(future)
            self._release_identity(state)
            return result
        except (asyncio.CancelledError, Exception):
            # 结果已经形成但尚未交付时，也必须交给原责任拥有者。
            if not self._try_withdraw(state):
                self._hand_over(state, owner)
            raise

    def _observe_runner(self, future: asyncio.Future) -> None:
        """等待者离开后仍跟踪执行包装器，实际结果由共享完成事实保存。"""
        self._runners.discard(future)
        if not future.cancelled():
            future.exception()

    def request_stop(self, task_id: FileTaskId) -> None:
        """幂等请求停止；对未知或已结束任务无操作。"""
        with self._lock:
            state = self._tasks.get(task_id)
        if state is not None:
            state.request_stop()

    def lease_of(self, task_id: FileTaskId) -> FileLease | None:
        """未结束任务的当前修改资格；已结束或未知返回 None。"""
        with self._lock:
            state = self._tasks.get(task_id)
        return state.lease if state is not None and not state.lease.released else None

    def unfinished_files(self) -> tuple[int, ...]:
        """仍有未结束任务的文件身份。"""
        with self._lock:
            return tuple(sorted(self._files))

    def _admit(self, task: FileTask | AsyncFileTask) -> _TaskState:
        with self._lock:
            if task.task_id in self._tasks:
                raise FileTaskError(f"任务身份仍被执行或未交付结果占用: {task.task_id.value}")
            for identity in task.file_ids:
                if identity in self._files:
                    raise FileTaskError(
                        f"同文件已有未结束任务: file {identity}"
                        f" 任务 {self._files[identity].value}"
                    )
            state = _TaskState(task)
            self._tasks[task.task_id] = state
            for identity in task.file_ids:
                self._files[identity] = task.task_id
            return state

    def _execute(self, state: _TaskState) -> FileTaskResult:
        """线程池中的实际执行：开始裁决、执行体及结束清理。"""
        if not self._begin(state):
            return state.completion.result()
        task = state.task
        try:
            value = task.body(state.stop_event)
            result = FileTaskResult(task_id=task.task_id, ran=True, value=value)
        except asyncio.CancelledError as error:
            # 执行体已经退出；先保存实际失败，再传播取消并交接结果。
            self._finish(state, FileTaskResult(
                task_id=task.task_id, ran=True,
                error=f"{type(error).__name__}: {error}",
            ))
            raise
        except Exception as error:  # 执行体异常保留诊断，不吞掉。
            result = FileTaskResult(
                task_id=task.task_id,
                ran=True,
                error=f"{type(error).__name__}: {error}",
            )
        self._finish(state, result)
        return result

    async def _execute_async(self, state: _TaskState) -> FileTaskResult:
        if not self._begin(state):
            return state.completion.result()
        try:
            value = await state.task.body(AsyncFileControl(state))
            result = FileTaskResult(state.task.task_id, ran=True, value=value)
        except asyncio.CancelledError as error:
            self._finish(state, FileTaskResult(
                state.task.task_id, ran=True, error=f"{type(error).__name__}: {error}",
            ))
            raise
        except Exception as error:
            result = FileTaskResult(
                state.task.task_id, ran=True, error=f"{type(error).__name__}: {error}",
            )
        self._finish(state, result)
        return result

    def _begin(self, state: _TaskState) -> bool:
        with self._lock:
            if state.stage is not _TaskStage.QUEUED:
                if state.completion.done():
                    return False
                raise FileTaskError("执行包装器不能并发调用同一任务")
            state.stage = _TaskStage.STARTED
            state.lease._mark_acquired()
            return True

    def _try_withdraw(self, state: _TaskState) -> bool:
        """撤回排队任务；与开始裁决竞争，只允许一个结果。"""
        with self._lock:
            if state.stage is not _TaskStage.QUEUED:
                return False
            state.stage = _TaskStage.WITHDRAWN
            self._complete_locked(state, _withdrawn_result(state))
            self._forget_identity_locked(state)
            return True

    def _finish(self, state: _TaskState, result: FileTaskResult) -> None:
        with self._lock:
            if state.completion.done():
                return
            state.stage = _TaskStage.FINISHED
            self._complete_locked(state, result)

    def _complete_locked(self, state: _TaskState, result: FileTaskResult) -> None:
        self._release_file_locked(state)
        state.lease._mark_ended()
        state.completion.set_result(result)

    def _release_file_locked(self, state: _TaskState) -> None:
        task = state.task
        for identity in task.file_ids:
            if self._files.get(identity) == task.task_id:
                del self._files[identity]

    def _release_identity(self, state: _TaskState) -> None:
        with self._lock:
            self._forget_identity_locked(state)

    def _forget_identity_locked(self, state: _TaskState) -> None:
        # 旧句柄的重复观察不能移除已复用身份的新任务。
        if self._tasks.get(state.task.task_id) is state:
            del self._tasks[state.task.task_id]

    def _hand_over(self, state: _TaskState, owner: ResponsibilityOwner) -> None:
        token = self._supervisor.register(owner)
        if isinstance(owner, _OwnedFileResult):
            owner.handed_over = True
        task = state.task
        self._supervisor.handoff(
            token,
            OwnedTask(
                identity=task.task_id.value,
                stage=task.stage,
                business=task.business,
                resources=(*(f"file:{identity}" for identity in task.file_ids), *task.resources),
                pending=FileTaskHandle(state, lambda: self._release_identity(state)),
            ),
        )


def _withdrawn_result(state: _TaskState) -> FileTaskResult:
    return FileTaskResult(task_id=state.task.task_id, ran=False)


@dataclass(frozen=True)
class _OwnedFileOutcome:
    """完整业务操作的实际返回或原异常，不丢弃异常分类。"""

    value: Any = None
    error: BaseException | None = None


class _OwnedFileResult:
    """登记的接手方只消费完整执行体已经形成的实际结束结果。"""

    def __init__(self) -> None:
        self.result: FileTaskResult | None = None
        self.handed_over = False

    async def take_over(self, task: OwnedTask) -> None:
        self.result = await task.pending.wait()
