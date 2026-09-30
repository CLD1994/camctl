"""日志组件有序关闭与一次丢弃摘要。

先停止新生产并等待原重要记录与已接纳记录，再按最终计数最多由
日志线程直接写一条 WARNING 摘要，最后停止监听、关闭处理器与队
列。重复关闭只尝试一次摘要；突然终止不补造摘要。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord

__all__ = ["CloseResult", "LogRuntime", "close_logging"]


@dataclass
class LogRuntime:
    """一次进程的日志运行时：通道与未完成事实。"""

    channel: LogChannel
    summary_written: bool = False
    close_attempts: int = 0


@dataclass(frozen=True)
class CloseResult:
    summary_written: bool
    dropped_total: int


async def close_logging(runtime: LogRuntime) -> CloseResult:
    """有序关闭日志组件；线程等待异步组织，不阻塞事件循环。"""
    runtime.close_attempts += 1
    if runtime.close_attempts > 1:
        return CloseResult(
            summary_written=runtime.summary_written,
            dropped_total=runtime.channel.counters.total_dropped(),
        )
    channel = runtime.channel
    # 等待已接纳记录消费完（监听线程按序处理）。
    await asyncio.to_thread(channel.stop_listener, timeout=5)
    dropped = channel.counters.total_dropped()
    if dropped > 0:
        summary = LogRecord(
            level=LogLevel.WARNING,
            message=(
                "日志关闭摘要：接纳丢弃 "
                f"debug={channel.counters.debug_dropped} "
                f"info={channel.counters.info_dropped} "
                f"warning={channel.counters.warning_dropped} "
                f"error={channel.counters.error_dropped}"
            ),
        )
        # 摘要由日志线程直接写，不经队列。
        channel.write_direct(summary)
        runtime.summary_written = True
    channel.async_close()
    await asyncio.to_thread(channel.await_closed)
    return CloseResult(summary_written=runtime.summary_written, dropped_total=dropped)
