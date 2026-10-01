"""可停止的设备读取会话。

字节在驱动读取端与分段线程之间流动：会话的 read_chunk 由线程池
中的分段任务同步调用，控制通道（request_stop/wait_stopped）由事
件循环独立使用。无数据超时只按实际取得的文件内容字节重置，读
取调用本身不重置；资源在实际读取结束并确认停止后才关闭。源定
位信息留在驱动，主机路径由 F1 管理。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Protocol, runtime_checkable

from camctl.operations.models import AttemptTicket

__all__ = [
    "ReadChunk",
    "ReadEnd",
    "ReadError",
    "ReadSession",
    "ReadSessionError",
    "SourceFile",
    "SourceStream",
    "open_read",
]

#: 无数据等待时的轮询间隔；数据到达由读取端立即观察。
_POLL_INTERVAL_S = 0.001


class ReadSessionError(ValueError):
    """会话使用错误：偏移越界或并发读取同一会话。"""


class ReadError(Exception):
    """读取结束的分类原因。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind  # stopped | no_data | failed


@dataclass(frozen=True)
class SourceFile:
    """设备源文件：原身份、驱动定位信息及固定长度。"""

    file_id: str
    locator: Mapping[str, Any]
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError(f"源文件长度必须是整数: {self.size_bytes!r}")


@dataclass(frozen=True)
class ReadChunk:
    """一次读取结果：字节或明确 EOF/错误，恰好一种。"""

    data: bytes | None
    eof: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ReadEnd:
    """会话实际停止的证据：是否已停止、累计读取及结束原因。"""

    stopped: bool
    bytes_read: int
    error: str | None


@runtime_checkable
class SourceStream(Protocol):
    """驱动读取端端口：只交付文件内容字节，已过滤日志与控制响应。

    read 返回空字节串表示此刻没有新文件数据（不是 EOF）；cancel
    唤醒可能在驱动内阻塞的等待；close 在读取实际结束后调用。
    """

    def read(self, limit: int) -> bytes: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class ReadSession:
    """一个源文件的一次连续读取会话；线程读数据，事件循环控停。"""

    def __init__(
        self,
        source: SourceFile,
        offset: int,
        stream: SourceStream,
        no_data_timeout_s: Decimal,
    ) -> None:
        self._source = source
        self._position = offset
        self._stream = stream
        self._timeout_s = float(no_data_timeout_s)
        self._stop_requested = threading.Event()
        self._reading = threading.Lock()
        self._active = threading.Event()
        self._active.set()
        self._bytes_read = 0
        self._end_error: str | None = None
        self._stopped_confirmed = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def position(self) -> int:
        return self._position

    def read_chunk(self, limit: int) -> ReadChunk:
        """同步读取下一段文件内容；同会话并发调用拒绝。"""
        if limit <= 0:
            raise ReadSessionError(f"读取长度必须是正整数: {limit!r}")
        if not self._active.is_set():
            return ReadChunk(data=None, error="stopped")
        if not self._reading.acquire(blocking=False):
            raise ReadSessionError("同一会话不能并发读取")
        try:
            return self._read_locked(limit)
        finally:
            self._reading.release()

    def _read_locked(self, limit: int) -> ReadChunk:
        remaining = self._source.size_bytes - self._position
        if remaining <= 0:
            self._finish(None)
            return ReadChunk(data=None, eof=True)
        last_data = time.monotonic()
        while True:
            if self._stop_requested.is_set():
                self._finish("stopped")
                return ReadChunk(data=None, error="stopped")
            data = self._stream.read(min(limit, remaining))
            if data:
                # 文件内容字节到达：立即重置无数据计时并交付。
                last_data = time.monotonic()
                self._position += len(data)
                self._bytes_read += len(data)
                eof = self._position >= self._source.size_bytes
                if eof:
                    self._finish(None)
                return ReadChunk(data=data, eof=eof)
            if time.monotonic() - last_data >= self._timeout_s:
                self._finish("no_data")
                return ReadChunk(data=None, error="no_data")
            time.sleep(_POLL_INTERVAL_S)

    def request_stop(self) -> None:
        """线程安全请求停止；不等待读取线程，立即返回。

        没有读取在途时由本调用直接确认停止并关闭资源；有读取在
        途时由读取循环观察到停止后确认。
        """
        self._stop_requested.set()
        self._stream.cancel()
        if not self._active.is_set():
            return
        if self._reading.acquire(blocking=False):
            try:
                if not self._active.is_set():
                    return
                self._finish("stopped")
            finally:
                self._reading.release()

    async def wait_stopped(self) -> ReadEnd:
        """等待读取实际结束并确认资源关闭。"""
        loop = asyncio.get_running_loop()
        self._loop = loop
        if self._stopped_confirmed.is_set():
            return self._end()
        future = loop.create_future()

        def _done() -> None:
            if not future.done():
                future.set_result(None)

        watcher = threading.Thread(target=self._await_confirmed, args=(_done, loop))
        watcher.start()
        await future
        return self._end()

    def _await_confirmed(self, notify, loop) -> None:
        self._stopped_confirmed.wait()
        loop.call_soon_threadsafe(notify)

    def _finish(self, error: str | None) -> None:
        if self._active.is_set():
            self._active.clear()
            self._end_error = error
            # 资源在读取实际结束后才关闭。
            self._stream.close()
            self._stopped_confirmed.set()

    def _end(self) -> ReadEnd:
        return ReadEnd(
            stopped=True, bytes_read=self._bytes_read, error=self._end_error
        )


async def open_read(
    source: SourceFile,
    offset: int,
    ticket: AttemptTicket,
    *,
    stream: SourceStream,
    no_data_timeout_s: Decimal,
) -> ReadSession:
    """打开一个读取会话；偏移必须落在已知固定长度内。"""
    if not 0 <= offset <= source.size_bytes:
        raise ReadSessionError(
            f"读取偏移越界: {offset} 不在 0~{source.size_bytes}"
        )
    if ticket.operation != "read":
        raise ReadSessionError(f"尝试票据操作类别不是读取: {ticket.operation!r}")
    if not no_data_timeout_s.is_finite() or no_data_timeout_s <= 0:
        raise ReadSessionError(f"无数据超时必须是有限正秒数: {no_data_timeout_s!r}")
    return ReadSession(source, offset, stream, no_data_timeout_s)
