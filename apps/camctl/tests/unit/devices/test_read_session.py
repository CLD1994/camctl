"""D4 源读取会话与独立停止控制的单元测试。

控制通道独立于数据流：线程等待源数据时事件循环可请求停止并执
行必要录像停止；资源在实际读取结束前不关闭。无数据超时按文件
内容字节重置，调用本身不重置；偏移与短读精确；同会话并发读取
拒绝。
"""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal

import pytest

from camctl.devices.read_session import (
    ReadSessionError,
    SourceFile,
    SourceStream,
    open_read,
)
from camctl.operations.models import AttemptTicket

pytestmark = pytest.mark.asyncio

_TICKET = AttemptTicket(
    attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9"
)


class SilentStream:
    """驱动读取端替身：只有文件内容字节，永不返回数据。"""

    def __init__(self) -> None:
        self.reads = 0
        self.cancelled = 0
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.reads += 1
        return b""

    def cancel(self) -> None:
        self.cancelled += 1

    def close(self) -> None:
        self.closed = True


class ScriptedStream:
    """按脚本交付数据，可选延迟；脚本耗尽后返回空。"""

    def __init__(self, chunks: list[bytes], delays: float = 0.0) -> None:
        self._chunks = list(chunks)
        self._delay = delays
        self.closed = False
        self.cancelled = 0

    def read(self, limit: int) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        return self._chunks.pop(0) if self._chunks else b""

    def cancel(self) -> None:
        self.cancelled += 1

    def close(self) -> None:
        self.closed = True


def _source(size: int = 100) -> SourceFile:
    return SourceFile(
        file_id="9", locator={"driver": "camctl-adb"}, size_bytes=size
    )


async def test_read_control_allows_stop_and_keeps_resources() -> None:
    stream = SilentStream()
    session = await open_read(
        _source(), 0, _TICKET, stream=stream, no_data_timeout_s=Decimal("30")
    )
    result: dict = {}

    def reader() -> None:
        result["chunk"] = session.read_chunk(64)

    thread = threading.Thread(target=reader)
    thread.start()
    await asyncio.sleep(0.05)
    # 线程等待源数据：资源未关闭，事件循环仍可发出读取停止与必要录像停止。
    assert not stream.closed
    session.request_stop()
    stop_command_done = []

    async def recording_stop() -> None:
        stop_command_done.append(True)

    await recording_stop()
    assert stop_command_done == [True]
    thread.join(timeout=5)
    assert result["chunk"].error == "stopped"
    end = await session.wait_stopped()
    assert end.stopped is True
    assert stream.cancelled >= 1
    assert stream.closed is True


async def test_no_data_timeout_counts_only_file_bytes() -> None:
    stream = SilentStream()  # 持续被调用但从不给文件内容
    session = await open_read(
        _source(), 0, _TICKET, stream=stream, no_data_timeout_s=Decimal("0.05")
    )
    chunk = session.read_chunk(64)
    assert chunk.error == "no_data"
    assert chunk.data is None
    assert stream.reads > 1  # 调用次数不重置无数据计时
    await session.wait_stopped()
    assert stream.closed


async def test_arriving_data_resets_no_data_timer() -> None:
    stream = ScriptedStream([b"abc", b"def"], delays=0.02)
    session = await open_read(
        _source(size=6), 0, _TICKET, stream=stream, no_data_timeout_s=Decimal("0.1")
    )
    first = session.read_chunk(3)
    assert first.data == b"abc"
    second = session.read_chunk(3)
    assert second.data == b"def"
    assert second.eof is True
    end = await session.wait_stopped()
    assert end.bytes_read == 6


async def test_offset_and_short_read_are_exact() -> None:
    stream = ScriptedStream([b"xyz"])
    session = await open_read(
        _source(size=10), 7, _TICKET, stream=stream, no_data_timeout_s=Decimal("1")
    )
    chunk = session.read_chunk(5)  # 只剩 3 字节：短读精确，不补齐
    assert chunk.data == b"xyz"
    assert chunk.eof is True
    assert session.position() == 10


async def test_concurrent_read_chunk_rejected() -> None:
    stream = ScriptedStream([b"a"], delays=0.05)
    session = await open_read(
        _source(), 0, _TICKET, stream=stream, no_data_timeout_s=Decimal("5")
    )
    errors: list = []

    def reader() -> None:
        session.read_chunk(1)

    thread = threading.Thread(target=reader)
    thread.start()
    await asyncio.sleep(0.01)
    try:
        session.read_chunk(1)
    except ReadSessionError:
        errors.append(True)
    thread.join(timeout=5)
    assert errors == [True]
    session.request_stop()
    await session.wait_stopped()


async def test_invalid_offset_rejected() -> None:
    stream = ScriptedStream([])
    with pytest.raises(ReadSessionError):
        await open_read(
            _source(size=10), 11, _TICKET, stream=stream, no_data_timeout_s=Decimal("1")
        )


def test_source_stream_protocol_shape() -> None:
    assert hasattr(SourceStream, "read")
    assert hasattr(SourceStream, "cancel")
    assert hasattr(SourceStream, "close")
