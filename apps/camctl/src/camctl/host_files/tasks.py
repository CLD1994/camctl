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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from camctl.session.supervision import (
    OwnedTask,
    ResponsibilityOwner,
    Supervisor,
)

__all__ = [
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
        if not isinstance(self.file_id, int) or self.file_id <= 0:
            raise FileTaskError(f"文件身份必须是正整数: {self.file_id!r}")
        if not self.stage or not self.business:
            raise FileTaskError("阶段与业务标签不能为空")


@dataclass(frozen=True)
class FileTaskResult:
    """一次文件任务的实际结果。

    ran 为 False 表示撤回成功、任务从未执行；ran 为 True 时 value
    与 error 恰好其一携带实际结局。
    """

    task_id: FileTaskId
    ran: bool
    value: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.ran and self.value is None and self.error is None:
            raise FileTaskError("已执行任务必须携带实际结果或错误")
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

    def __init__(self, state: "_TaskState") -> None:
        self._state = state

    @property
    def task_id(self) -> FileTaskId:
        return self._state.task.task_id

    @property
    def lease(self) -> FileLease:
        return self._state.lease

    def request_stop(self) -> None:
        """幂等请求停止；不等待，立即返回。"""
        self._state.stop_event.set()

    async def wait(self) -> FileTaskResult:
        """等待任务实际结束并返回实际结果。"""
        loop = asyncio.get_running_loop()
        if self._state.done_event.is_set():
            return self._state.result
        future: asyncio.Future[None] = loop.create_future()

        def _notify() -> None:
            if not future.done():
                future.set_result(None)

        watcher = threading.Thread(
            target=self._await_done, args=(_notify, loop), daemon=True
        )
        watcher.start()
        await future
        return self._state.result

    def _await_done(self, notify: Callable[[], None], loop) -> None:
        self._state.done_event.wait()
        loop.call_soon_threadsafe(notify)


class ThreadRunner(Protocol):
    """线程执行端口：把同步执行体交给线程池并返回其结果。"""

    def __call__(self, fn: Callable[[], FileTaskResult]) -> Awaitable[FileTaskResult]: ...


async def _run_in_default_pool(fn: Callable[[], FileTaskResult]) -> FileTaskResult:
    return await asyncio.to_thread(fn)


class _TaskStage(enum.Enum):
    QUEUED = "queued"
    STARTED = "started"
    WITHDRAWN = "withdrawn"


class _TaskState:
    """一项任务的运行时状态；由执行器锁保护裁决与结束。"""

    def __init__(self, task: FileTask) -> None:
        self.task = task
        self.stage = _TaskStage.QUEUED
        self.stop_event = threading.Event()
        self.lease = FileLease()
        self.result: FileTaskResult | None = None
        self.done_event = threading.Event()


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

    async def run_file_task(
        self, task: FileTask, owner: ResponsibilityOwner
    ) -> FileTaskResult:
        """提交并等待一项文件任务的实际结果。

        等待协程被取消时：任务仍在排队则撤回（确定不执行）；已开
        始则登记接手，实际结果由 owner 消费。两种情况都不以等待
        取消作为实际结束。
        """
        state = self._admit(task)
        future = asyncio.ensure_future(self._runner(lambda: self._execute(state)))
        try:
            return await future
        except asyncio.CancelledError:
            if state.done_event.is_set():
                raise
            if self._try_withdraw(state):
                raise
            self._hand_over(state, owner)
            raise

    def request_stop(self, task_id: FileTaskId) -> None:
        """幂等请求停止；对未知或已结束任务无操作。"""
        with self._lock:
            state = self._tasks.get(task_id)
        if state is not None:
            state.stop_event.set()

    def lease_of(self, task_id: FileTaskId) -> FileLease | None:
        """未结束任务的当前修改资格；已结束或未知返回 None。"""
        with self._lock:
            state = self._tasks.get(task_id)
        return state.lease if state is not None else None

    def unfinished_files(self) -> tuple[int, ...]:
        """仍有未结束任务的文件身份。"""
        with self._lock:
            return tuple(sorted(self._files))

    def _admit(self, task: FileTask) -> _TaskState:
        with self._lock:
            if task.task_id in self._tasks:
                raise FileTaskError(f"任务身份未结束不能重复提交: {task.task_id.value}")
            if task.file_id in self._files:
                raise FileTaskError(
                    f"同文件已有未结束任务: file {task.file_id}"
                    f" 任务 {self._files[task.file_id].value}"
                )
            state = _TaskState(task)
            self._tasks[task.task_id] = state
            self._files[task.file_id] = task.task_id
            return state

    def _execute(self, state: _TaskState) -> FileTaskResult:
        """线程池中的实际执行：开始裁决、执行体及结束清理。"""
        with self._lock:
            if state.stage is not _TaskStage.QUEUED:
                # 撤回已在等待侧裁决：任务确定未执行。
                return _withdrawn_result(state)
            state.stage = _TaskStage.STARTED
            state.lease._mark_acquired()
        task = state.task
        try:
            value = task.body(state.stop_event)
            result = FileTaskResult(task_id=task.task_id, ran=True, value=value)
        except Exception as error:  # 执行体异常保留诊断，不吞掉。
            result = FileTaskResult(
                task_id=task.task_id,
                ran=True,
                error=f"{type(error).__name__}: {error}",
            )
        self._finish(state, result)
        return result

    def _try_withdraw(self, state: _TaskState) -> bool:
        """撤回排队任务；与开始裁决竞争，只允许一个结果。"""
        with self._lock:
            if state.stage is not _TaskStage.QUEUED:
                return False
            state.stage = _TaskStage.WITHDRAWN
            self._forget(state)
            state.result = _withdrawn_result(state)
            state.lease._mark_ended()
            state.done_event.set()
            return True

    def _finish(self, state: _TaskState, result: FileTaskResult) -> None:
        with self._lock:
            if state.result is not None:
                return
            state.result = result
            self._forget(state)
            state.lease._mark_ended()
            state.done_event.set()

    def _forget(self, state: _TaskState) -> None:
        task = state.task
        if self._tasks.get(task.task_id) is state:
            del self._tasks[task.task_id]
        if self._files.get(task.file_id) == task.task_id:
            del self._files[task.file_id]

    def _hand_over(self, state: _TaskState, owner: ResponsibilityOwner) -> None:
        token = self._supervisor.register(owner)
        task = state.task
        self._supervisor.handoff(
            token,
            OwnedTask(
                identity=task.task_id.value,
                stage=task.stage,
                business=task.business,
                resources=(f"file:{task.file_id}", *task.resources),
                pending=FileTaskHandle(state),
            ),
        )


def _withdrawn_result(state: _TaskState) -> FileTaskResult:
    return FileTaskResult(task_id=state.task.task_id, ran=False)
