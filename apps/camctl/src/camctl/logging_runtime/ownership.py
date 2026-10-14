"""日志文件实际操作的会话内拥有者。"""

from __future__ import annotations

import asyncio
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from camctl.logging_runtime.models import LogFileStage
from camctl.session.supervision import OwnedTask, Supervisor


@dataclass(frozen=True)
class _ThreadFact:
    started: bool
    value: Any = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _ServiceFact:
    """服务协程的正常值或内部 BaseException；owner task 本身不取消。"""

    value: Any = None
    error: BaseException | None = None


class LoggingOwner:
    """在 run 的会话锁内接手日志线程结果并完成主循环诊断。"""

    def __init__(
        self,
        *,
        logs_dir: Path,
        ownership: Any,
        supervisor: Supervisor,
        owner_settled: bool,
        diagnose: Callable[[str], Awaitable[None]],
        ordinary_log_path: Path | None = None,
    ) -> None:
        self._logs_dir = Path(logs_dir)
        self._ownership = ownership
        self._supervisor = supervisor
        self._token = supervisor.register(self)
        self._owner_settled = owner_settled
        self._diagnose = diagnose
        self._ordinary_log_path = ordinary_log_path
        self._stopping = threading.Event()
        self._startup_stopping = threading.Event()
        self._start_gate = threading.Lock()
        self._handed_off: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()
        self._next_identity = 0
        self._startup_task: asyncio.Task | None = None

    def start(self) -> None:
        """在取得 SessionLock 后立即登记并启动唯一一次启动检查。"""
        if self._startup_task is not None:
            return
        task = asyncio.get_running_loop().create_task(
            self._run_startup_sweep(), name="camctl-log-startup-sweep")
        self._startup_task = task
        self._handoff(task, LogFileStage.STARTUP_SWEEP.value)

    async def start_session(self) -> None:
        """同步登记启动扫描，不等待默认线程池中的普通日志工作。"""
        self.start()

    def required_settlements(self) -> int:
        return sum(not task.done() for task in self._handed_off)

    def stop_new_work(self) -> None:
        """阻止普通服务步骤；启动扫描使用独立的真实取消事实。"""
        with self._start_gate:
            self._stopping.set()

    def cancel_startup_sweep(self) -> None:
        """真实 run 取消时撤回 queued 扫描并阻止下一候选删除。"""
        with self._start_gate:
            self._startup_stopping.set()

    def resume_restricted_logging(self) -> None:
        """普通业务收场及受限停止成功后恢复一次受限日志责任。"""
        with self._start_gate:
            self._stopping.clear()

    def stop_requested(self) -> bool:
        """普通日志步骤读取该 run 的普通停止/取消事实。"""
        return self._stopping.is_set()

    def startup_stop_requested(self) -> bool:
        """启动扫描只读取真实取消的停止事实。"""
        return self._startup_stopping.is_set()

    def _unlink_startup_candidate(self, path: Path) -> None:
        from camctl.logging_runtime.copies import (
            _LoggingOperationStopped, _unlink,
        )

        with self._start_gate:
            if self._startup_stopping.is_set():
                raise _LoggingOperationStopped(
                    LogFileStage.STARTUP_SWEEP_UNLINK)
        # 取得 start_gate 是本次 unlink 的不可撤回 started 时点；
        # 后续 stop 只阻止下一候选，不伪称撤销已经开始的删除。
        _unlink(path)

    async def settle(self) -> None:
        result = await self._supervisor.drain_required()
        for failure in result.failed:
            await self._safe_diagnose(
                "日志文件责任收场诊断："
                f"stage={failure.record.stage}; outcome=unknown; "
                f"identity={failure.record.identity}; error={failure.error}"
            )

    async def run_service_operation(
        self,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """在独立服务任务内持有 FailureLogService 协调锁及其诊断。"""
        async def own_result() -> _ServiceFact:
            try:
                return _ServiceFact(value=await operation())
            except BaseException as error:
                # 不让内部 CancelledError 把被 Supervisor 消费的 task
                # 变成 cancelled；FailureLogService 已在锁内交付诊断。
                return _ServiceFact(error=error)

        task = asyncio.get_running_loop().create_task(
            own_result(), name=f"camctl-log-{stage}")
        try:
            fact = await asyncio.shield(task)
        except asyncio.CancelledError:
            self.stop_new_work()
            self.cancel_startup_sweep()
            self._handoff(task, stage)
            raise
        if fact.error is not None:
            raise fact.error.with_traceback(fact.error.__traceback__)
        return fact.value

    async def owned_call(
        self,
        stage: LogFileStage,
        operation: Callable[[], Any],
        *,
        required: bool = False,
    ) -> Any:
        """在线程池入口裁决 queued/started；工作线程只返回事实。"""
        from camctl.logging_runtime.copies import _LoggingOperationStopped

        stopping = self._startup_stopping if stage.is_startup else self._stopping
        with self._start_gate:
            if not required and stopping.is_set():
                raise _LoggingOperationStopped(stage)

        def run() -> _ThreadFact:
            with self._start_gate:
                started = required or not stopping.is_set()
            if not started:
                return _ThreadFact(started=False)
            try:
                return _ThreadFact(started=True, value=operation())
            except BaseException as error:
                return _ThreadFact(started=True, error=error)

        fact = await asyncio.get_running_loop().run_in_executor(None, run)
        if not fact.started:
            raise _LoggingOperationStopped(stage)
        if fact.error is not None:
            raise fact.error.with_traceback(fact.error.__traceback__)
        return fact.value

    async def take_over(self, task: OwnedTask) -> None:
        pending = task.pending
        if not isinstance(pending, asyncio.Task):
            raise TypeError("日志实际责任必须由同一 asyncio.Task 持有")
        fact = await asyncio.shield(pending)
        # 服务任务把内部 BaseException 作为普通返回事实交给 owner；
        # 失败诊断已在 FailureLogService 锁内交付，不能再次抛入 drain。
        if isinstance(fact, _ServiceFact) and fact.error is not None:
            return

    async def _run_startup_sweep(self) -> None:
        from camctl.logging_runtime.copies import (
            _LoggingOperationStopped, startup_sweep,
        )

        try:
            result = await self.owned_call(
                LogFileStage.STARTUP_SWEEP,
                lambda: startup_sweep(
                    self._logs_dir,
                    ownership=self._ownership,
                    owner_settled=self._owner_settled,
                    ordinary_log_path=self._ordinary_log_path,
                    stop_requested=self.startup_stop_requested,
                    unlink_candidate=self._unlink_startup_candidate,
                ),
                required=False,
            )
        except _LoggingOperationStopped:
            return
        except asyncio.CancelledError as error:
            await self._safe_diagnose(
                "run 启动日志遗留检查诊断："
                f"stage={LogFileStage.STARTUP_SWEEP.value}; "
                f"outcome=unknown; error={type(error).__name__}: {error}"
            )
            return
        except Exception as error:
            await self._safe_diagnose(
                "run 启动日志遗留检查诊断："
                f"stage={LogFileStage.STARTUP_SWEEP.value}; "
                f"outcome=unknown; error={type(error).__name__}: {error}"
            )
            return
        if result.error is None and not result.failures:
            return
        failures = "; ".join(
            f"{name}: {error}" for name, error in result.failures)
        await self._safe_diagnose(
            "run 启动日志遗留检查诊断："
            f"stage={LogFileStage.STARTUP_SWEEP.value}; "
            f"error={result.error or 'none'}; "
            f"failures={failures or 'none'}"
        )

    def _handoff(self, task: asyncio.Task, stage: str) -> None:
        if task in self._handed_off:
            return
        self._next_identity += 1
        identity = f"logging/{self._next_identity:08x}/{stage}"
        self._handed_off.add(task)
        self._supervisor.handoff(self._token, OwnedTask(
            identity=identity,
            stage=stage,
            business="日志文件交付与诊断",
            resources=("staging/logs",),
            pending=task,
        ))

    async def _safe_diagnose(self, message: str) -> None:
        try:
            await self._diagnose(message)
        except Exception:
            return
