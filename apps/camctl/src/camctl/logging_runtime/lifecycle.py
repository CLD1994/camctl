"""日志组件有序关闭与一次丢弃摘要。

正常退出时生产已结束，丢弃计数即为最终值：先按级别与计数构造
唯一摘要，随停止标记交给监听线程在消化完已接纳记录后写出；监
听线程停止后关闭文件处理器，最后关闭接纳队列。重复关闭只返回
同一事实；监听线程未能完成时跳过摘要，不补造。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CloseResult:
    """关闭结果事实：摘要是否写出、最终丢弃数与监听线程状态。"""

    summary_written: bool
    dropped_total: int
    listener_stopped: bool = True


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
    if runtime.close_attempts:
        return CloseResult(
            summary_written=runtime.summary_written,
            dropped_total=runtime.channel.counters.total_dropped(),
            listener_stopped=runtime.listener_stopped,
        )
    runtime.close_attempts = 1
    channel = runtime.channel
    dropped = channel.counters.total_dropped()
    summary: LogRecord | None = None
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
    stopped = await asyncio.to_thread(
        channel.stop_listener, stop_timeout_s, summary=summary
    )
    runtime.listener_stopped = stopped
    runtime.summary_written = stopped and channel.summary_written
    if runtime.file_closer is not None:
        await asyncio.to_thread(runtime.file_closer)
    channel.async_close()
    await channel.await_closed()
    return CloseResult(
        summary_written=runtime.summary_written,
        dropped_total=dropped,
        listener_stopped=stopped,
    )
