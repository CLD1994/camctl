"""报告通信线程：主进程侧管道端点的唯一拥有者。

可能阻塞的收发都在专用线程执行；接收到的控制消息与通道关闭事实
通过 call_soon_threadsafe 按发生顺序回投事件循环。端点只由本线
程关闭：请求收场后线程完成当前收发循环即关闭端点并退出，调用协
程等待线程实际退出。关闭不依赖从其他线程关闭句柄来唤醒阻塞收
发；对端退出或关闭后阻塞读取自然返回。
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import wait as _wait_readable
from typing import Callable, Protocol

from camctl.reporting.messages import (
    MAX_MESSAGE_BYTES,
    ControlMessage,
    MessageProtocolError,
    decode_message,
    encode_message,
)

__all__ = [
    "ChannelClosed",
    "CommunicatorClosed",
    "MessageEvent",
    "WorkerCommunicator",
]

#: 可读性轮询间隔：兼顾待发送消息的及时写出与收场响应。
_POLL_SECONDS = 0.05


class CommunicatorClosed(RuntimeError):
    """通信线程已收场后继续使用发送接口。"""


@dataclass(frozen=True)
class MessageEvent:
    """一条已通过校验的控制消息。"""

    message: ControlMessage


@dataclass(frozen=True)
class ChannelClosed:
    """通道结束事实；error 为空表示正常关闭或对端结束。"""

    error: BaseException | None


class PipeTransport(Protocol):
    """通信线程拥有的字节管道端点接口。"""

    def send_bytes(self, data: bytes) -> None: ...

    def recv_bytes(self, maxlength: int = -1) -> bytes: ...

    def close(self) -> None: ...


class WorkerCommunicator:
    """专用通信线程：独占端点，阻塞收发与事件回投。"""

    def __init__(
        self,
        connection: PipeTransport,
        *,
        wait_readable: Callable[..., object] | None = None,
    ) -> None:
        self._connection = connection
        self._wait = wait_readable or _wait_readable
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._pending: deque[tuple[bytes, "asyncio.Future[None]"]] = deque()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._ended = threading.Event()
        self._terminal: ChannelClosed | None = None

    def start(self) -> None:
        """在当前事件循环上启动通信线程；只能启动一次。"""
        if self._thread is not None:
            raise RuntimeError("通信线程只能启动一次")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(
            target=self._run, name="camctl-report-comm", daemon=True)
        self._thread.start()

    @property
    def closed(self) -> bool:
        """线程是否已经实际退出（端点已由线程关闭）。"""
        return self._ended.is_set()

    async def send(self, message: ControlMessage) -> None:
        """把一条控制消息交给通信线程写出并等待写完成或失败。"""
        data = encode_message(message)
        if self._loop is None:
            raise RuntimeError("通信线程未启动")
        future: asyncio.Future[None] = self._loop.create_future()
        with self._lock:
            if self._closing.is_set() or self._ended.is_set():
                raise CommunicatorClosed("通信线程已收场")
            self._pending.append((data, future))
        await future

    async def receive(self) -> MessageEvent | ChannelClosed:
        """取得下一条事件；已送达消息先于关闭事实，关闭后幂等返回。"""
        if self._queue is None:
            raise RuntimeError("通信线程未启动")
        if self._terminal is not None and self._queue.empty():
            return self._terminal
        event = await self._queue.get()
        if isinstance(event, ChannelClosed):
            self._terminal = event
        return event

    async def close(self) -> None:
        """请求收场并等待线程实际退出；端点由通信线程关闭。

        已送达事件仍可继续读取。请求后未写出的待发送消息以
        CommunicatorClosed 失败，不再尝试写出；端点中已经可读取
        的完整消息仍先于关闭事实交给接收方。
        """
        if self._thread is None:
            raise RuntimeError("通信线程未启动")
        self._closing.set()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ended.wait)

    # ---- 线程侧 -------------------------------------------------------

    def _run(self) -> None:
        terminal: ChannelClosed | None = None
        try:
            while not self._closing.is_set():
                failure = self._drain_pending()
                if failure is not None:
                    terminal = failure
                    break
                readable = self._wait((self._connection,), _POLL_SECONDS)
                if not readable:
                    continue
                outcome = self._receive_one()
                if isinstance(outcome, ChannelClosed):
                    terminal = outcome
                    break
                self._emit(outcome)
        except BaseException as error:
            terminal = ChannelClosed(error)
        # 请求关闭或发送失败不能丢弃另一方向已经送达的完整结果。
        # 不等待新消息；对端实际退出后，未完成的读取会由 EOF 收场。
        try:
            while self._wait((self._connection,), 0):
                outcome = self._receive_one()
                if isinstance(outcome, ChannelClosed):
                    if terminal is None or terminal.error is None:
                        terminal = outcome
                    break
                self._emit(outcome)
        except BaseException as error:
            if terminal is None or terminal.error is None:
                terminal = ChannelClosed(error)
        # 正常收场也发布关闭事实，接收方不会无限等待。
        if terminal is None:
            terminal = ChannelClosed(None)
        try:
            self._connection.close()
        except OSError:
            pass
        self._fail_pending()
        self._emit(terminal)
        self._ended.set()

    def _drain_pending(self) -> ChannelClosed | None:
        """写出全部待发送消息；写失败时失败该发送并按通道结束处理。"""
        while True:
            with self._lock:
                item = self._pending.popleft() if self._pending else None
            if item is None:
                return None
            data, future = item
            try:
                self._connection.send_bytes(data)
            except BaseException as error:
                self._settle(future, error=error)
                return ChannelClosed(error)
            self._settle(future)

    def _receive_one(self) -> MessageEvent | ChannelClosed:
        try:
            payload = self._connection.recv_bytes(MAX_MESSAGE_BYTES + 1)
        except EOFError:
            return ChannelClosed(None)
        except (OSError, ValueError) as error:
            return ChannelClosed(error)
        try:
            return MessageEvent(decode_message(payload))
        except MessageProtocolError as error:
            return ChannelClosed(error)

    def _fail_pending(self) -> None:
        with self._lock:
            leftovers = list(self._pending)
            self._pending.clear()
        for _, future in leftovers:
            self._settle(future, error=CommunicatorClosed("通信线程已收场"))

    def _emit(self, event: MessageEvent | ChannelClosed) -> None:
        assert self._loop is not None
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
        except RuntimeError:
            pass  # 事件循环已先于线程收场；事件无处送达。

    def _settle(
        self, future: "asyncio.Future[None]", error: BaseException | None = None
    ) -> None:
        def settle() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(None)

        try:
            self._loop.call_soon_threadsafe(settle)
        except RuntimeError:
            pass  # 事件循环已先于线程收场；等待方已随会话结束。
