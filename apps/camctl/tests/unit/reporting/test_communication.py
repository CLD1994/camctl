"""通信线程所有权：阻塞收发在专用线程执行，事件回投事件循环。

替身传输提供字节收发与关闭记录；本文件验证线程所有权、送达顺
序、协议错误与关闭语义。真实管道行为由集成测试覆盖。
"""

from __future__ import annotations

import asyncio
import queue

import pytest

from camctl.reporting.communication import (
    ChannelClosed,
    CommunicatorClosed,
    MessageEvent,
    WorkerCommunicator,
)
from camctl.reporting.messages import (
    MessageProtocolError,
    ReadyMessage,
    ResultSuccessMessage,
    ShutdownMessage,
    decode_message,
    encode_message,
)

pytestmark = pytest.mark.asyncio

_INSTANCE = "f" * 32


class FakeTransport:
    """同步替身：入站队列即管道接收端，记录发送与关闭。"""

    def __init__(self) -> None:
        self.inbound: queue.Queue[bytes] = queue.Queue()
        self.sent: list[bytes] = []
        self.closed = 0

    def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def recv_bytes(self, maxlength: int = -1) -> bytes:
        try:
            return self.inbound.get_nowait()
        except queue.Empty as error:
            raise EOFError from error

    def close(self) -> None:
        self.closed += 1


def _wait_readable(objects, timeout):
    transport = objects[0]
    return list(objects) if not transport.inbound.empty() else []


def _success(job_id: str = "task-1") -> ResultSuccessMessage:
    return ResultSuccessMessage(
        job_id=job_id, instance_id=_INSTANCE,
        path="/staging/report-1.json", size_bytes=8, sha256="0" * 64)


async def test_send_runs_on_worker_thread_and_receive_gets_reply() -> None:
    transport = FakeTransport()
    communicator = WorkerCommunicator(transport, wait_readable=_wait_readable)
    communicator.start()
    try:
        await asyncio.wait_for(communicator.send(ReadyMessage()), 5)
        assert [decode_message(data) for data in transport.sent] == [ReadyMessage()]

        transport.inbound.put(encode_message(_success()))
        event = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(event, MessageEvent)
        assert event.message == _success()
    finally:
        await communicator.close()
    assert transport.closed == 1


async def test_delivered_messages_precede_close_event_in_order() -> None:
    transport = FakeTransport()
    communicator = WorkerCommunicator(transport, wait_readable=_wait_readable)
    communicator.start()
    transport.inbound.put(encode_message(ReadyMessage()))
    transport.inbound.put(encode_message(ShutdownMessage()))
    transport.inbound.put(b"")  # 空字节触发协议错误并收场
    first = await asyncio.wait_for(communicator.receive(), 5)
    second = await asyncio.wait_for(communicator.receive(), 5)
    terminal = await asyncio.wait_for(communicator.receive(), 5)
    assert isinstance(first, MessageEvent) and first.message == ReadyMessage()
    assert isinstance(second, MessageEvent) and second.message == ShutdownMessage()
    assert isinstance(terminal, ChannelClosed)
    assert isinstance(terminal.error, MessageProtocolError)
    await communicator.close()


async def test_terminal_receive_is_repeatable_after_close() -> None:
    transport = FakeTransport()
    communicator = WorkerCommunicator(transport, wait_readable=_wait_readable)
    communicator.start()
    transport.inbound.put(encode_message(ReadyMessage()))
    await asyncio.wait_for(communicator.receive(), 5)
    await communicator.close()
    repeat = await communicator.receive()
    assert isinstance(repeat, ChannelClosed) and repeat.error is None


async def test_close_fails_pending_sends_and_rejects_later_use() -> None:
    transport = FakeTransport()
    communicator = WorkerCommunicator(transport, wait_readable=_wait_readable)
    communicator.start()
    await communicator.close()
    with pytest.raises(CommunicatorClosed):
        await communicator.send(ReadyMessage())
    assert transport.sent == []


async def test_send_failure_fails_the_awaited_send_and_closes_channel() -> None:
    class BrokenTransport(FakeTransport):
        def send_bytes(self, data: bytes) -> None:
            raise BrokenPipeError("对端已消失")

    transport = BrokenTransport()
    communicator = WorkerCommunicator(transport, wait_readable=_wait_readable)
    communicator.start()
    with pytest.raises(BrokenPipeError):
        await asyncio.wait_for(communicator.send(ReadyMessage()), 5)
    terminal = await asyncio.wait_for(communicator.receive(), 5)
    assert isinstance(terminal, ChannelClosed)
    assert isinstance(terminal.error, BrokenPipeError)
    await communicator.close()


async def test_receive_without_start_is_rejected() -> None:
    communicator = WorkerCommunicator(FakeTransport(), wait_readable=_wait_readable)
    with pytest.raises(RuntimeError):
        await communicator.receive()
    with pytest.raises(RuntimeError):
        await communicator.close()
