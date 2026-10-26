"""日志组件有序关闭与一次丢弃摘要。

正常退出时生产已结束，丢弃计数即为最终值：先按级别与计数构造
唯一摘要，随停止标记交给监听线程在消化完已接纳记录后写出；监
听线程停止后关闭文件处理器，最后关闭接纳队列。重复关闭只返回
同一事实；监听线程未能完成时跳过摘要，不补造。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord

__all__ = ["CloseResult", "LogRuntime", "close_logging"]

_LEVEL_RANK = {
    LogLevel.DEBUG: 0,
    LogLevel.INFO: 1,
    LogLevel.WARNING: 2,
    LogLevel.ERROR: 3,
}


@dataclass
class LogRuntime:
    """一次进程的日志组件集合与关闭事实。"""

    channel: LogChannel
    level: LogLevel = LogLevel.WARNING
    file_closer: Callable[[], None] | None = None
    identity: str = "camctl"
    summary_written: bool = False
    listener_stopped: bool = True
    close_attempts: int = 0
    #: 共享文件日志 I/O 或协调失败后停用后续文件日志。
    path_unsafe: bool = False
    _close_operation: _CloseOperation | None = field(default=None, init=False, repr=False)


@dataclass(frozen=True)
class CloseResult:
    """关闭结果事实：摘要是否写出、最终丢弃数与监听线程状态。"""

    summary_written: bool
    dropped_total: int
    listener_stopped: bool = True
    close_error: str | None = None


@dataclass(frozen=True)
class _CloseFact:
    result: CloseResult | None = None
    error: BaseException | None = None


@dataclass
class _CloseOperation:
    task: asyncio.Task[_CloseFact] | None = None
    errors: list[tuple[str, BaseException]] = field(default_factory=list)
    first_control: BaseException | None = None
    parent_cancel: asyncio.CancelledError | None = None

    def record(self, label: str, error: BaseException) -> None:
        if any(error is previous for _, previous in self.errors):
            return
        self.errors.append((label, error))
        if not isinstance(error, Exception) and self.first_control is None:
            self.first_control = error

    def record_parent_cancel(self, error: asyncio.CancelledError) -> None:
        if self.parent_cancel is None:
            self.parent_cancel = error
            self.record("log_close_wait_canceled", error)

    def propagate_control(self) -> None:
        control = self.first_control
        if control is None:
            return
        existing = set(tuple(getattr(control, "__notes__", ())))
        for label, error in self.errors:
            if error is control:
                continue
            notes = [f"{label}: {type(error).__name__}: {error}"]
            notes.extend(f"{label}附注: {note}"
                         for note in tuple(getattr(error, "__notes__", ())))
            for note in notes:
                if note not in existing:
                    control.add_note(note)
                    existing.add(note)
        raise control.with_traceback(control.__traceback__)


def _allows_warning(level: LogLevel) -> bool:
    return _LEVEL_RANK[LogLevel.WARNING] >= _LEVEL_RANK[level]


async def close_logging(
    runtime: LogRuntime, *, stop_timeout_s: float = 5.0
) -> CloseResult:
    """按正常退出次序关闭日志组件；重复调用不重复尝试摘要。

    摘要构造在停止标记入队前完成（计数已最终、级别已判定），写
    出由监听线程执行：标记排在全部已接纳记录之后，摘要因此先于
    通道关闭、经原文件处理器写出且不经接纳队列。
    """
    operation = runtime._close_operation
    if operation is None:
        if runtime.close_attempts:
            return CloseResult(
                summary_written=runtime.summary_written,
                dropped_total=runtime.channel.counters.total_dropped(),
                listener_stopped=runtime.listener_stopped,
            )
        runtime.close_attempts = 1
        operation = _CloseOperation()
        runtime._close_operation = operation

        async def own_close() -> _CloseFact:
            try:
                return _CloseFact(result=await _close_logging_owned(
                    runtime, stop_timeout_s=stop_timeout_s, operation=operation))
            except BaseException as error:
                operation.record("log_close_failed", error)
                return _CloseFact(error=error)

        operation.task = asyncio.create_task(own_close(), name="camctl-log-close")
    while True:
        try:
            fact = await asyncio.shield(operation.task)
            break
        except asyncio.CancelledError as error:
            operation.record_parent_cancel(error)
    operation.propagate_control()
    if fact.error is not None:
        raise fact.error.with_traceback(fact.error.__traceback__)
    assert fact.result is not None
    return fact.result


async def _close_logging_owned(
    runtime: LogRuntime, *, stop_timeout_s: float, operation: _CloseOperation,
) -> CloseResult:
    channel = runtime.channel
    if runtime.path_unsafe:
        from camctl.logging_runtime.service import ChannelError
        channel.disable_channel(ChannelError("共享文件日志 I/O 或协调不可用"))
    await channel.finish_producers()
    await channel.join()
    dropped = channel.counters.total_dropped()
    close_errors: list[str] = []

    def record_close_error(label: str, error: BaseException) -> None:
        operation.record(label, error)
        if isinstance(error, Exception):
            close_errors.append(f"{label}: {type(error).__name__}: {error}")

    stopped = False
    runtime.listener_stopped = False
    summary: LogRecord | None = None
    if runtime.path_unsafe:
        from camctl.logging_runtime.service import ChannelError

        try:
            channel.disable_channel(ChannelError("共享文件日志 I/O 或协调不可用"))
        except BaseException as error:
            record_close_error("channel_disable_failed", error)
    else:
        if dropped > 0 and _allows_warning(runtime.level):
            counters = channel.counters
            summary = LogRecord(
                level=LogLevel.WARNING,
                message=(
                    f"日志关闭摘要（{runtime.identity}）：接纳丢弃 "
                    f"debug={counters.debug_dropped} info={counters.info_dropped} "
                    f"warning={counters.warning_dropped} "
                    f"error={counters.error_dropped}"
                ),
            )
    try:
        if runtime.path_unsafe:
            stopped = await asyncio.to_thread(channel.stop_listener, None)
        else:
            stopped = await asyncio.to_thread(
                channel.stop_listener, stop_timeout_s, summary=summary)
    except BaseException as error:
        record_close_error("listener_stop_failed", error)
    runtime.listener_stopped = stopped
    runtime.summary_written = (not runtime.path_unsafe
                               and stopped and channel.summary_written)
    # 停用分支只在确认监听结束后关闭文件；普通分支保持原关闭机会。
    close_file = stopped if runtime.path_unsafe else True

    if close_file and runtime.file_closer is not None:
        try:
            await asyncio.to_thread(runtime.file_closer)
        except BaseException as error:
            record_close_error("file_handler_close_failed", error)
    try:
        channel.async_close()
    except BaseException as error:
        record_close_error("channel_close_failed", error)
    try:
        await channel.await_closed()
    except BaseException as error:
        record_close_error("channel_wait_failed", error)

    return CloseResult(
        summary_written=runtime.summary_written,
        dropped_total=dropped,
        listener_stopped=stopped,
        close_error="; ".join(close_errors) if close_errors else None,
    )
