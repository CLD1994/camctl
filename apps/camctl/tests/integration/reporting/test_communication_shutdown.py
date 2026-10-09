"""真实管道和通信线程在关闭、发送失败时保留已送达完整帧。"""

from __future__ import annotations

import asyncio
import multiprocessing
import threading

import pytest

from camctl.reporting.communication import ChannelClosed, MessageEvent, WorkerCommunicator
from camctl.reporting.messages import (
    ErrorKind,
    ReadyMessage,
    ResultFailureMessage,
    encode_message,
)


def _state_failure() -> ResultFailureMessage:
    return ResultFailureMessage(
        job_id="current-task", instance_id="f" * 32,
        error_kind=ErrorKind.STATE, error_code="ConsistencyError",
        error_message="冻结历史损坏",
    )


class _ControlledPoll:
    """第一个阻塞轮询让测试安排帧和关闭；后续按真实可读性返回。"""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.entered = threading.Event()
        self.release = threading.Event()
        self.first = True

    def __call__(self, objects, timeout):
        if self.first and timeout > 0:
            self.first = False
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("测试未释放通信轮询")
            return []
        return list(objects) if self.connection.poll(timeout) else []


@pytest.mark.asyncio
async def test_close_preserves_delivered_state_before_terminal():
    parent, peer = multiprocessing.Pipe(duplex=True)
    poll = _ControlledPoll(parent)
    communicator = WorkerCommunicator(parent, wait_readable=poll)
    communicator.start()
    close_task = None
    try:
        assert await asyncio.to_thread(poll.entered.wait, 5)
        peer.send_bytes(encode_message(_state_failure()))
        close_task = asyncio.create_task(communicator.close())
        await asyncio.sleep(0)
        poll.release.set()
        await asyncio.wait_for(close_task, 5)
        event = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(event, MessageEvent)
        assert event.message == _state_failure()
        terminal = await communicator.receive()
        assert isinstance(terminal, ChannelClosed)
        assert terminal.error is None
        assert communicator.closed
    finally:
        poll.release.set()
        if close_task is None:
            await communicator.close()
        else:
            await close_task
        peer.close()


@pytest.mark.asyncio
async def test_send_failure_preserves_already_delivered_state():
    parent, peer = multiprocessing.Pipe(duplex=True)

    class BrokenSend:
        def send_bytes(self, data: bytes) -> None:
            raise BrokenPipeError("发送方向已结束")

        def recv_bytes(self, maxlength: int = -1) -> bytes:
            return parent.recv_bytes(maxlength)

        def close(self) -> None:
            parent.close()

    poll = _ControlledPoll(parent)
    communicator = WorkerCommunicator(BrokenSend(), wait_readable=poll)
    communicator.start()
    sending = None
    try:
        assert await asyncio.to_thread(poll.entered.wait, 5)
        peer.send_bytes(encode_message(_state_failure()))
        sending = asyncio.create_task(communicator.send(ReadyMessage()))
        await asyncio.sleep(0)
        poll.release.set()
        with pytest.raises(BrokenPipeError):
            await asyncio.wait_for(sending, 5)
        event = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(event, MessageEvent)
        assert event.message == _state_failure()
        terminal = await communicator.receive()
        assert isinstance(terminal, ChannelClosed)
        assert isinstance(terminal.error, BrokenPipeError)
    finally:
        poll.release.set()
        if sending is not None:
            await asyncio.gather(sending, return_exceptions=True)
        await communicator.close()
        peer.close()
