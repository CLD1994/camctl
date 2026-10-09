"""报告子进程监督：启动、派发、结果判定与收场确认。

主进程按分阶段单调时限管理子进程：启动等待就绪，生成等待匹配
任务身份的有效结果，停止先请求退出、宽限内确认实际退出、到期
升级终止与强杀。已送达消息先于关闭事实处理：退出或通道结束时
先检查已经送达的完整结果；旧任务结果、迟到消息不能完成当前任
务。只有生成成功且进程与通信正常的子进程被复用；其余情况确认
退出后回收，不把结束进程当作重试机会。
"""

from __future__ import annotations

import asyncio
import enum
import multiprocessing
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from camctl.reporting.communication import (
    ChannelClosed,
    CommunicatorClosed,
    MessageEvent,
    WorkerCommunicator,
)
from camctl.reporting.messages import (
    ErrorKind,
    JobMessage,
    MessageProtocolError,
    ReadyMessage,
    ResultFailureMessage,
    ResultSuccessMessage,
    ShutdownMessage,
    StartupFailedMessage,
)
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.worker import worker_main

__all__ = [
    "GenerationOutcomeKind",
    "WorkerGeneration",
    "WorkerShutdown",
    "WorkerStartup",
    "WorkerSupervisor",
]

_JOIN_POLL_SECONDS = 0.01


class GenerationOutcomeKind(enum.Enum):
    """一次生成派发的机器结果分区。"""

    SUCCESS = "success"
    REPORT_FAILURE = "report_failure"
    STATE_FAILURE = "state_failure"
    TIMEOUT = "timeout"
    CHANNEL_LOST = "channel_lost"
    PROTOCOL = "protocol"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class WorkerStartup:
    """启动结果：就绪，或失败阶段与原因。"""

    ready: bool
    failure: StartupFailedMessage | None = None
    detail: str = ""


@dataclass(frozen=True)
class WorkerGeneration:
    """一次生成派发的判定结果；成功消息不证明已经发布。"""

    kind: GenerationOutcomeKind
    job_id: str
    success: ResultSuccessMessage | None = None
    failure: ResultFailureMessage | None = None
    detail: str = ""


@dataclass(frozen=True)
class WorkerShutdown:
    """收场事实及当前未决任务已送达的状态库错误。"""

    exitcode: int | None
    forced: bool
    state_failure: ResultFailureMessage | None = None
    configuration_failure: ResultFailureMessage | None = None


class ProcessHandle(Protocol):
    """监督所需的子进程操作端口。"""

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def _default_spawn(lock_path: str, startup_seconds: float):
    """以 spawn 方式启动真实报告子进程；返回进程与两端管道。

    锁等待的启动期限以绝对单调时刻传递：主进程与子进程共享同一
    单调时钟域，子进程据此在主进程启动时限内完成等待并报告。
    """
    context = multiprocessing.get_context("spawn")
    parent_end, child_end = context.Pipe(duplex=True)
    process = context.Process(
        target=worker_main,
        args=(child_end, os.getpid(), lock_path,
              time.monotonic() + startup_seconds),
        daemon=True,
    )
    process.start()
    return process, parent_end, child_end


class WorkerSupervisor:
    """报告子进程的启动、派发与收场监督。"""

    def __init__(
        self,
        *,
        limits: MaintenanceLimits,
        lock_path: str,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[], tuple[Any, Any, Any]] | None = None,
    ) -> None:
        self._limits = limits
        self._lock_path = lock_path
        self._clock = clock
        self._spawn = spawn if spawn is not None else self._default_spawn
        self._process: ProcessHandle | None = None
        self._communicator: WorkerCommunicator | None = None
        self._active_job: JobMessage | None = None
        self._receive_task: asyncio.Task[MessageEvent | ChannelClosed] | None = None
        self._retirement_task: asyncio.Task[WorkerShutdown] | None = None
        self._stopping = False
        self._retirement_protocol_detail = ""

    def _default_spawn(self):
        return _default_spawn(self._lock_path, self._limits.startup_seconds)

    @property
    def reusable(self) -> bool:
        """当前子进程是否可继续派发（存活且最近一次生成成功）。"""
        return (self._process is not None and self._communicator is not None
                and not self._stopping
                and self._process.is_alive() and not self._communicator.closed)

    async def start(self) -> WorkerStartup:
        """确保有就绪子进程；启动失败、超时或通道错误时收场并返回。"""
        if self.reusable:
            return WorkerStartup(ready=True)
        if (self._process is not None or self._communicator is not None
                or self._retirement_task is not None):
            await self._retire()
        self._retirement_task = None
        self._stopping = False
        self._retirement_protocol_detail = ""
        process, parent_end, child_end = self._spawn()
        # 父进程不使用子进程端：spawn 传递后立即关闭本地引用。
        child_end.close()
        communicator = WorkerCommunicator(parent_end)
        communicator.start()
        self._process, self._communicator = process, communicator
        deadline = self._clock() + self._limits.startup_seconds
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                await self._retire()
                return WorkerStartup(ready=False, detail="启动时限内未收到就绪通知")
            try:
                event = await self._receive(communicator, remaining)
            except asyncio.TimeoutError:
                await self._retire()
                return WorkerStartup(ready=False, detail="启动时限内未收到就绪通知")
            except asyncio.CancelledError:
                if not self._stopping:
                    await self._retire()
                    raise
                await self._retire()
                return WorkerStartup(ready=False, detail="启动期间已请求停止")
            if self._stopping:
                await self._retire()
                return WorkerStartup(ready=False, detail="启动期间已请求停止")
            if isinstance(event, MessageEvent):
                if isinstance(event.message, ReadyMessage) and self._process.is_alive():
                    return WorkerStartup(ready=True)
                if isinstance(event.message, StartupFailedMessage):
                    failure = event.message
                    await self._retire()
                    return WorkerStartup(ready=False, failure=failure,
                                         detail=failure.reason)
                detail = f"启动阶段收到非就绪消息: {type(event.message).__name__}"
                await self._retire()
                return WorkerStartup(ready=False, detail=detail)
            error = event.error
            detail = "启动通道结束" if error is None else f"启动通道错误: {error}"
            await self._retire()
            return WorkerStartup(ready=False, detail=detail)

    async def generate(self, job: JobMessage) -> WorkerGeneration:
        """派发一次生成任务并在总时限内等待匹配任务身份的有效结果。

        进程退出或通道结束时先检查已送达消息；旧任务结果与迟到
        消息不完成当前任务。成功后子进程保持可用；失败、超时或
        通道不可靠时确认实际退出并回收。
        """
        startup = await self.start()
        if not startup.ready:
            return WorkerGeneration(
                kind=GenerationOutcomeKind.START_FAILED, job_id=job.job_id,
                detail=startup.detail)
        process, communicator = self._process, self._communicator
        assert process is not None and communicator is not None
        self._active_job = job
        deadline = self._clock() + self._limits.total_seconds
        try:
            await asyncio.wait_for(
                communicator.send(job), max(deadline - self._clock(), 0.001))
        except (asyncio.TimeoutError, CommunicatorClosed, MessageProtocolError,
                OSError) as error:
            return await self._finish_generation(
                job, GenerationOutcomeKind.CHANNEL_LOST,
                detail=f"任务发送失败: {error}")
        except asyncio.CancelledError:
            await self._retire()
            raise
        while True:
            if self._stopping:
                return await self._finish_generation(
                    job, GenerationOutcomeKind.CHANNEL_LOST,
                    detail="生成任务已请求停止")
            remaining = deadline - self._clock()
            if remaining <= 0:
                return await self._finish_generation(
                    job, GenerationOutcomeKind.TIMEOUT,
                    detail="生成总时限内未取得有效结果")
            try:
                event = await self._receive(communicator, remaining)
            except asyncio.TimeoutError:
                return await self._finish_generation(
                    job, GenerationOutcomeKind.TIMEOUT,
                    detail="生成总时限内未取得有效结果")
            except asyncio.CancelledError:
                if not self._stopping:
                    await self._retire()
                    raise
                return await self._finish_generation(
                    job, GenerationOutcomeKind.CHANNEL_LOST,
                    detail="生成任务已请求停止")
            if self._stopping:
                return await self._finish_generation(
                    job, GenerationOutcomeKind.CHANNEL_LOST,
                    detail="生成任务已请求停止")
            if isinstance(event, ChannelClosed):
                error = event.error
                kind = (GenerationOutcomeKind.PROTOCOL
                        if isinstance(error, MessageProtocolError)
                        else GenerationOutcomeKind.CHANNEL_LOST)
                return await self._finish_generation(
                    job, kind,
                    detail="通道结束且无已送达的有效结果" if error is None
                    else f"通道错误且无已送达的有效结果: {error}")
            message = event.message
            if isinstance(message, (ResultSuccessMessage, ResultFailureMessage)):
                if not self._matches_job(message, job):
                    return await self._finish_generation(
                        job, GenerationOutcomeKind.PROTOCOL,
                        detail="结果的任务身份或数据库实例身份不匹配")
                if isinstance(message, ResultSuccessMessage):
                    # 同步确认成功后解除未决身份，后续收场不撤回该事实。
                    self._active_job = None
                    if not process.is_alive():
                        # 已确认成功结果保留；该进程不再复用。
                        await self._retire()
                        return WorkerGeneration(
                            kind=GenerationOutcomeKind.SUCCESS, job_id=job.job_id,
                            success=message,
                            detail="生成成功后子进程已退出，不再复用")
                    return WorkerGeneration(
                        kind=GenerationOutcomeKind.SUCCESS, job_id=job.job_id,
                        success=message)
                kind = (GenerationOutcomeKind.STATE_FAILURE
                        if message.error_kind is ErrorKind.STATE
                        else GenerationOutcomeKind.REPORT_FAILURE)
                return await self._finish_generation(
                    job, kind, failure=message)
            # 就绪或退出指令出现在任务阶段按协议错误处理。
            return await self._finish_generation(
                job, GenerationOutcomeKind.PROTOCOL,
                detail=f"任务阶段收到非结果消息: {type(message).__name__}")

    async def _receive(
        self, communicator: WorkerCommunicator, timeout: float,
    ) -> MessageEvent | ChannelClosed:
        """保持唯一接收所有权，允许显式停止把未消费结果交给收场。"""
        task = asyncio.create_task(communicator.receive())
        self._receive_task = task
        try:
            return await asyncio.wait_for(task, timeout)
        finally:
            if not self._stopping and self._receive_task is task:
                self._receive_task = None

    @staticmethod
    def _matches_job(
        message: ResultSuccessMessage | ResultFailureMessage, job: JobMessage,
    ) -> bool:
        return (message.job_id == job.job_id
                and message.instance_id == job.instance_id)

    async def _finish_generation(
        self, job: JobMessage, kind: GenerationOutcomeKind, *,
        failure: ResultFailureMessage | None = None, detail: str = "",
    ) -> WorkerGeneration:
        """保留原停止原因，并让有效迟到 STATE 进入既有错误分区。"""
        shutdown = await self._retire()
        if self._retirement_protocol_detail:
            detail = "; ".join(filter(None, (detail, self._retirement_protocol_detail)))
        if shutdown.state_failure is not None:
            kind, failure = GenerationOutcomeKind.STATE_FAILURE, shutdown.state_failure
        elif shutdown.configuration_failure is not None:
            kind, failure = GenerationOutcomeKind.REPORT_FAILURE, shutdown.configuration_failure
        elif failure is None and self._retirement_protocol_detail:
            kind = GenerationOutcomeKind.PROTOCOL
        return WorkerGeneration(
            kind=kind, job_id=job.job_id, failure=failure, detail=detail)

    async def stop(self) -> WorkerShutdown:
        """请求退出并确认实际退出；宽限到期升级终止与强杀。"""
        return await self._retire()

    async def _retire(self) -> WorkerShutdown:
        """先撤销普通交付资格，生成和显式停止共享一次实际收场。"""
        if self._retirement_task is None:
            self._stopping = True
            self._retirement_task = asyncio.create_task(self._retire_current())
        # 调用方取消不能取消负责确认实际退出的收场任务。
        return await asyncio.shield(self._retirement_task)

    async def _retire_current(self) -> WorkerShutdown:
        """实际退出后关闭通信，并逐条读取到终端以保存有效 STATE。"""
        process, communicator = self._process, self._communicator
        job = self._active_job
        received = self._receive_task
        self._receive_task = None
        pending_event = None
        if received is not None:
            if not received.done():
                received.cancel()
            try:
                pending_event = await received
            except asyncio.CancelledError:
                pass
        if process is None and communicator is None:
            return WorkerShutdown(exitcode=None, forced=False)
        forced = False
        if process is not None:
            if process.is_alive():
                if communicator is not None:
                    try:
                        await asyncio.wait_for(
                            communicator.send(ShutdownMessage()),
                            self._limits.stop_grace_seconds)
                    except (asyncio.TimeoutError, CommunicatorClosed,
                            MessageProtocolError, OSError):
                        pass
                if not await self._join_within(
                        process, self._limits.stop_grace_seconds):
                    process.terminate()
                    forced = True
                    if not await self._join_within(
                            process, self._limits.stop_grace_seconds):
                        process.kill()
                        if not await self._join_within(
                                process, self._limits.stop_grace_seconds):
                            # 信号已经发出仍不代表退出；保留句柄和通道。
                            raise RuntimeError("报告子进程强杀后仍未确认实际退出")
            else:
                process.join(0)
        if communicator is not None:
            await communicator.close()
        state_failure = None
        configuration_failure = None
        if communicator is not None:
            event = pending_event
            while True:
                if event is None:
                    event = await communicator.receive()
                if isinstance(event, ChannelClosed):
                    if job is not None and isinstance(event.error, MessageProtocolError):
                        self._retirement_protocol_detail = f"收场通信协议错误: {event.error}"
                    break
                message = event.message
                if job is not None and isinstance(
                        message, (ResultFailureMessage, ResultSuccessMessage)):
                    if not self._matches_job(message, job):
                        self._retirement_protocol_detail = "收场结果的任务身份或数据库实例身份不匹配"
                    elif (isinstance(message, ResultFailureMessage)
                          and message.error_kind is ErrorKind.STATE
                          and state_failure is None):
                        state_failure = message
                    elif (isinstance(message, ResultFailureMessage)
                          and message.error_kind is ErrorKind.REPORT
                          and message.error_code == "configuration_error"
                          and configuration_failure is None):
                        configuration_failure = message
                event = None
        self._process = None
        self._communicator = None
        self._active_job = None
        return WorkerShutdown(
            exitcode=getattr(process, "exitcode", None), forced=forced,
            state_failure=state_failure, configuration_failure=configuration_failure)

    async def _join_within(self, process: ProcessHandle, timeout: float) -> bool:
        """在时限内确认实际退出；轮询避免跨线程阻塞等待。"""
        deadline = self._clock() + timeout
        while True:
            if not process.is_alive():
                process.join(0)
                return True
            if self._clock() >= deadline:
                return False
            await asyncio.sleep(_JOIN_POLL_SECONDS)
