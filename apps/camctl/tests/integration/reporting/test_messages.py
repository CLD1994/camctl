"""真实 Pipe 上的通信线程：完整帧往返、送达顺序与通道中断。

一端使用生产通信线程，另一端由对端线程直接操作 Connection 字节
接口，覆盖：就绪后派发与结果往返、关闭前送达消息的顺序保持、非
法载荷与超容量帧按协议错误收场、对端关闭的 EOF 语义。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import threading

import pytest

from camctl.reporting.communication import (
    ChannelClosed,
    MessageEvent,
    WorkerCommunicator,
)
from camctl.reporting.messages import (
    MAX_MESSAGE_BYTES,
    JobMessage,
    ReadyMessage,
    ResultSuccessMessage,
    ShutdownMessage,
    decode_message,
    encode_message,
)

pytestmark = pytest.mark.asyncio

_INSTANCE = "f" * 32


def _job(job_id: str = "task-1") -> JobMessage:
    return JobMessage(
        job_id=job_id, report_id=1, from_wm=0, to_wm=4, frozen_event_id=9,
        instance_id=_INSTANCE,
        db_path="/var/lib/camctl/state.db",
        staging_path="/var/lib/camctl/staging/reports/report-1.json",
        entity_batch_size=32, event_batch_size=256, busy_timeout_ms=9000)


def _success(job_id: str = "task-1") -> ResultSuccessMessage:
    return ResultSuccessMessage(
        job_id=job_id, instance_id=_INSTANCE,
        path="/var/lib/camctl/staging/reports/report-1.json",
        size_bytes=128, sha256="0" * 64)


async def test_ready_then_job_round_trip_over_real_pipe() -> None:
    parent, child = multiprocessing.Pipe(duplex=True)
    communicator = WorkerCommunicator(parent)
    communicator.start()
    try:
        child.send_bytes(encode_message(ReadyMessage()))

        def peer() -> None:
            job = decode_message(child.recv_bytes())
            child.send_bytes(encode_message(_success(job.job_id)))

        threading.Thread(target=peer, daemon=True).start()
        job = _job()
        await asyncio.wait_for(communicator.send(job), 5)
        ready = await asyncio.wait_for(communicator.receive(), 5)
        result = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(ready, MessageEvent) and ready.message == ReadyMessage()
        assert isinstance(result, MessageEvent)
        assert result.message == _success(job.job_id)
    finally:
        await communicator.close()
        child.close()


async def test_delivered_messages_precede_eof_in_order() -> None:
    parent, child = multiprocessing.Pipe(duplex=True)
    communicator = WorkerCommunicator(parent)
    communicator.start()
    child.send_bytes(encode_message(ReadyMessage()))
    child.send_bytes(encode_message(ShutdownMessage()))
    child.close()
    try:
        first = await asyncio.wait_for(communicator.receive(), 5)
        second = await asyncio.wait_for(communicator.receive(), 5)
        terminal = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(first, MessageEvent) and first.message == ReadyMessage()
        assert isinstance(second, MessageEvent) and second.message == ShutdownMessage()
        assert isinstance(terminal, ChannelClosed) and terminal.error is None
        repeat = await communicator.receive()
        assert repeat == terminal
    finally:
        await communicator.close()


async def test_illegal_payload_closes_channel_with_protocol_error() -> None:
    parent, child = multiprocessing.Pipe(duplex=True)
    communicator = WorkerCommunicator(parent)
    communicator.start()
    child.send_bytes(b"not-a-json-message")
    try:
        terminal = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(terminal, ChannelClosed)
        assert isinstance(terminal.error, Exception)
    finally:
        await communicator.close()
        child.close()


async def test_oversized_frame_closes_channel() -> None:
    parent, child = multiprocessing.Pipe(duplex=True)
    communicator = WorkerCommunicator(parent)
    communicator.start()

    def oversize_peer() -> None:
        # 读端按容量上限拒收并关闭后，对端写满大帧可能遇到断管。
        try:
            child.send_bytes(b"x" * (MAX_MESSAGE_BYTES + 512))
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=oversize_peer, daemon=True).start()
    try:
        terminal = await asyncio.wait_for(communicator.receive(), 5)
        assert isinstance(terminal, ChannelClosed)
        assert terminal.error is not None
    finally:
        await communicator.close()
        child.close()


async def test_close_after_peer_exit_returns_and_closes_endpoint_once() -> None:
    parent, child = multiprocessing.Pipe(duplex=True)
    communicator = WorkerCommunicator(parent)
    communicator.start()
    child.close()
    terminal = await asyncio.wait_for(communicator.receive(), 5)
    assert isinstance(terminal, ChannelClosed) and terminal.error is None
    await asyncio.wait_for(communicator.close(), 5)
    assert communicator.closed
    # 端点已由通信线程关闭；再次收场保持幂等。
    await asyncio.wait_for(communicator.close(), 5)
