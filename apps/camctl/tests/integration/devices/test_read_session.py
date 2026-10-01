"""D4 读取会话的组件集成测试。

真实文件流、默认线程池与真实单调钟：偏移与短读精确、取消独立
于读取线程、无数据超时按实际字节重置，读取等待期间仍可执行真
实受管工具（O3 收场）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from decimal import Decimal

import pytest

from camctl.devices.read_session import (
    SourceFile,
    open_read,
)
from camctl.operations.models import AttemptTicket
from camctl.operations.process import ToolSpec, execute_tool

pytestmark = pytest.mark.asyncio

_TICKET = AttemptTicket(
    attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9",
    run_id=1,
)


class RealFileStream:
    """真实文件读取端：pread 按偏移精确读取，未到数据时可等待。"""

    def __init__(
        self, path: str, *, start: int = 0, delay_before_first: float = 0.0
    ) -> None:
        self._fd = os.open(path, os.O_RDONLY)
        os.lseek(self._fd, start, os.SEEK_SET)
        self._position = start
        self._delay = delay_before_first
        self.cancelled = 0
        self.closed = False
        self._cancel_event = threading.Event()

    def read(self, limit: int) -> bytes:
        if self._delay > 0 and not self._cancel_event.is_set():
            # 真实等待源数据：被取消唤醒，或等到数据出现。
            if self._cancel_event.wait(timeout=self._delay):
                return b""
            self._delay = 0.0
        data = os.read(self._fd, limit)
        self._position += len(data)
        return data

    def cancel(self) -> None:
        self.cancelled += 1
        self._cancel_event.set()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self._fd)


async def test_real_file_offset_and_short_read() -> None:
    path = _temp_file(b"0123456789")
    stream = RealFileStream(path, start=7)
    session = await open_read(
        SourceFile(file_id="9", locator={}, size_bytes=10),
        7,
        _TICKET,
        stream=stream,
        no_data_timeout_s=Decimal("5"),
    )
    chunk = await asyncio.to_thread(session.read_chunk, 5)
    assert chunk.data == b"789"
    assert chunk.eof is True
    await session.wait_stopped()
    assert stream.closed


async def test_cancel_while_waiting_and_recording_stop_still_runs() -> None:
    path = _temp_file(b"abcdef")
    stream = RealFileStream(path, delay_before_first=30.0)
    session = await open_read(
        SourceFile(file_id="9", locator={}, size_bytes=6),
        0,
        _TICKET,
        stream=stream,
        no_data_timeout_s=Decimal("30"),
    )
    reading = asyncio.ensure_future(asyncio.to_thread(session.read_chunk, 4))
    await asyncio.sleep(0.05)
    # 读取线程在真实等待源数据时，事件循环执行真实受管的必要控制。
    class _Never:
        async def requested(self) -> None:
            await asyncio.Future()

    control = await execute_tool(
        ToolSpec(
            argv=(sys.executable, "-c", "print('control-ok')"),
            timeout_s=Decimal("5"),
            terminate_grace_s=Decimal("1"),
        ),
        stop=_Never(),
    )
    assert control.exit is not None
    assert control.exit.exit_code == 0
    session.request_stop()
    chunk = await reading
    assert chunk.error == "stopped"
    end = await session.wait_stopped()
    assert end.stopped is True
    assert stream.cancelled == 1
    assert stream.closed


async def test_no_data_timeout_with_real_clock() -> None:
    path = _temp_file(b"xy")
    stream = RealFileStream(path, start=2)
    session = await open_read(
        SourceFile(file_id="9", locator={}, size_bytes=100),
        2,
        _TICKET,
        stream=stream,
        no_data_timeout_s=Decimal("0.1"),
    )
    start = time.monotonic()
    chunk = await asyncio.to_thread(session.read_chunk, 8)
    assert chunk.error == "no_data"
    assert time.monotonic() - start >= 0.1
    await session.wait_stopped()
    assert stream.closed


def _temp_file(content: bytes) -> str:
    import tempfile

    handle, path = tempfile.mkstemp()
    with os.fdopen(handle, "wb") as writer:
        writer.write(content)
    return path
