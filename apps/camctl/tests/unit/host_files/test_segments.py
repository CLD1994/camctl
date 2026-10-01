"""F3 段内小块传输与源无数据观察的单元测试。

按 [C,E) 顺序逐块读写：块大小与段长三种关系及跨边界截取、段尾
恰好读完、提前 EOF、块间停止不新增同步、进行中的同步保留实际结
果；目标写入与同步不累计源无数据等待；源错误分类透传。
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest

from camctl.devices.read_session import ReadSession, SourceFile, SourceStream, open_read
from camctl.host_files.segments import (
    SegmentError,
    SegmentResult,
    SegmentSpec,
    transfer_segment,
)
from camctl.operations.models import AttemptTicket

pytestmark = pytest.mark.asyncio

_TICKET = AttemptTicket(
    attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9",
    run_id=1,
)
_TIMEOUT_S = Decimal("5")


class ScriptedStream:
    """按脚本交付内容字节；脚本耗尽后返回空（暂无数据）。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.limits: list[int] = []

    def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return self._chunks.pop(0) if self._chunks else b""

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class RecordingTarget:
    """受约束写入端口替身：记录写入、同步及可注入的延迟与错误。"""

    def __init__(
        self,
        *,
        write_delay_s: float = 0.0,
        fail_write_on: int | None = None,
        sync_gate: threading.Event | None = None,
        fail_sync: bool = False,
    ) -> None:
        self.written: list[bytes] = []
        self.sync_calls = 0
        self._write_delay = write_delay_s
        self._fail_write_on = fail_write_on
        self._sync_gate = sync_gate
        self._fail_sync = fail_sync

    def write(self, data: bytes) -> int:
        if self._write_delay:
            time.sleep(self._write_delay)
        if self._fail_write_on is not None and len(self.written) == self._fail_write_on:
            raise RuntimeError("disk full")
        self.written.append(data)
        return len(data)

    def sync(self) -> None:
        if self._sync_gate is not None:
            assert self._sync_gate.wait(timeout=10)
        self.sync_calls += 1
        if self._fail_sync:
            raise OSError("sync error")


async def _session(size: int, offset: int = 0, stream: SourceStream | None = None,
                   timeout_s: Decimal = _TIMEOUT_S) -> ReadSession:
    return await open_read(
        SourceFile(file_id="9", locator={}, size_bytes=size),
        offset,
        _TICKET,
        stream=stream or ScriptedStream([]),
        no_data_timeout_s=timeout_s,
    )


def _spec(start: int, end: int, *, chunk: int = 10,
          stop: threading.Event | None = None) -> SegmentSpec:
    return SegmentSpec(
        attempt="copy/9",
        round_index=0,
        target_name="copy-0001.part",
        range_start=start,
        range_end=end,
        chunk_size=chunk,
        stop=stop or threading.Event(),
    )


async def test_chunk_size_smaller_equal_larger_than_segment() -> None:
    """块大小小于、等于、大于段长都精确覆盖段范围。"""
    data = bytes(range(30))
    for chunk in (10, 30, 50):
        stream = ScriptedStream([data[i:i + 10] for i in range(0, 30, 10)])
        session = await _session(30, stream=stream)
        target = RecordingTarget()
        result = transfer_segment(_spec(0, 30, chunk=chunk), session, target)
        assert result.error is None
        assert result.processed_end == 30
        assert result.synced is True
        assert b"".join(target.written) == data
        # 单次请求不超过段剩余量：跨边界截取。
        assert stream.limits[0] == min(chunk, 30)
        assert all(limit <= 30 for limit in stream.limits)


async def test_segment_tail_reaches_eof_exactly() -> None:
    """段尾恰好读完源：EOF 与段完成同时，正常结束。"""
    data = b"x" * 30
    stream = ScriptedStream([data[20:30]])
    session = await _session(30, offset=20, stream=stream)
    target = RecordingTarget()
    result = transfer_segment(_spec(20, 30, chunk=10), session, target)
    assert result.error is None
    assert result.processed_end == 30
    assert result.synced is True
    assert result.source_ended is True
    assert b"".join(target.written) == data[20:30]


async def test_early_eof_is_reported_with_actual_bytes() -> None:
    """源在段尾前结束：保留实际字节，分类提前 EOF，不做段尾同步。"""
    stream = ScriptedStream([b"a" * 10, b"b" * 10, b"c" * 10])
    session = await _session(30, stream=stream)
    target = RecordingTarget()
    result = transfer_segment(_spec(0, 50, chunk=10), session, target)
    assert isinstance(result, SegmentResult)
    assert result.error == "source_eof_early"
    assert result.processed_end == 30
    assert result.synced is False
    assert result.source_ended is True
    assert b"".join(target.written) == b"a" * 10 + b"b" * 10 + b"c" * 10


async def test_persistent_data_transfers_whole_range() -> None:
    """持续数据多块传输：目标字节恰为源切片，无额外内容。"""
    data = bytes(range(100))
    stream = ScriptedStream([data[i:i + 10] for i in range(10, 100, 10)])
    session = await _session(100, offset=10, stream=stream)
    target = RecordingTarget()
    result = transfer_segment(_spec(10, 70, chunk=16), session, target)
    assert result.error is None
    assert result.processed_end == 70
    assert b"".join(target.written) == data[10:70]
    assert target.sync_calls == 1


async def test_cancel_stops_between_chunks() -> None:
    """第一块写入后停止：不新增读取与同步，保留实际字节。"""
    stop = threading.Event()
    stream = ScriptedStream([b"a" * 10, b"b" * 10, b"c" * 10])
    session = await _session(30, stream=stream)
    target = RecordingTarget()

    original_write = target.write

    def write_and_stop(data: bytes) -> int:
        written = original_write(data)
        stop.set()
        return written

    target.write = write_and_stop  # type: ignore[method-assign]
    result = transfer_segment(_spec(0, 30, chunk=10, stop=stop), session, target)
    assert result.error == "stopped"
    assert result.processed_end == 10
    assert result.synced is False
    assert result.source_ended is False
    assert target.sync_calls == 0
    assert b"".join(target.written) == b"a" * 10


async def test_stop_before_tail_sync_skips_sync() -> None:
    """全部字节写入后、段尾同步前停止：同步不发起，进度不推进。"""
    stop = threading.Event()
    stream = ScriptedStream([b"a" * 10])
    session = await _session(10, stream=stream)
    target = RecordingTarget()
    target.write = (  # type: ignore[method-assign]
        lambda data: (RecordingTarget.write(target, data), stop.set(), len(data))[2]
    )
    result = transfer_segment(_spec(0, 10, chunk=10, stop=stop), session, target)
    assert result.error == "stopped"
    assert result.processed_end == 10
    assert result.synced is False
    assert target.sync_calls == 0


async def test_sync_in_progress_keeps_actual_result() -> None:
    """同步进行中停止到达：等待真实结果，已发生的同步保留。"""
    gate = threading.Event()
    stop = threading.Event()
    stream = ScriptedStream([b"a" * 10])
    session = await _session(10, stream=stream)
    target = RecordingTarget(sync_gate=gate)
    holder: list[SegmentResult] = []

    def run() -> None:
        holder.append(transfer_segment(_spec(0, 10, chunk=10, stop=stop), session, target))

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 10
    while target.sync_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    gate.set()
    worker.join(timeout=10)
    result = holder[0]
    assert result.error is None
    assert result.synced is True
    assert result.processed_end == 10


async def test_sync_failure_is_reported() -> None:
    stream = ScriptedStream([b"a" * 10])
    session = await _session(10, stream=stream)
    target = RecordingTarget(fail_sync=True)
    result = transfer_segment(_spec(0, 10, chunk=10), session, target)
    assert result.error is not None
    assert result.error.startswith("sync_failed")
    assert result.processed_end == 10
    assert result.synced is False


async def test_write_failure_preserves_written_bytes() -> None:
    stream = ScriptedStream([b"a" * 10, b"b" * 10])
    session = await _session(20, stream=stream)
    target = RecordingTarget(fail_write_on=1)
    result = transfer_segment(_spec(0, 20, chunk=10), session, target)
    assert result.error is not None
    assert result.error.startswith("write_failed")
    assert "disk full" in result.error
    assert result.processed_end == 10
    assert result.synced is False


async def test_no_data_timeout_passes_through() -> None:
    stream = ScriptedStream([])
    session = await _session(30, stream=stream, timeout_s=Decimal("0.2"))
    target = RecordingTarget()
    result = transfer_segment(_spec(0, 30, chunk=10), session, target)
    assert result.error == "no_data"
    assert result.processed_end == 0
    assert result.source_ended is True


async def test_session_stop_passes_through() -> None:
    stream = ScriptedStream([])
    session = await _session(30, stream=stream)
    session.request_stop()
    target = RecordingTarget()
    result = transfer_segment(_spec(0, 30, chunk=10), session, target)
    assert result.error == "stopped"
    assert result.source_ended is True


async def test_target_writes_do_not_count_as_no_data_wait() -> None:
    """目标写入耗时不受源无数据超时累计：慢写之后仍继续交付。"""
    stream = ScriptedStream([b"a" * 10, b"b" * 10])
    session = await _session(20, stream=stream, timeout_s=Decimal("0.5"))
    target = RecordingTarget(write_delay_s=0.6)
    result = transfer_segment(_spec(0, 20, chunk=10), session, target)
    assert result.error is None
    assert result.processed_end == 20
    assert result.synced is True


async def test_position_mismatch_is_rejected() -> None:
    stream = ScriptedStream([b"a" * 10])
    session = await _session(30, offset=5, stream=stream)
    with pytest.raises(SegmentError):
        transfer_segment(_spec(0, 10, chunk=10), session, RecordingTarget())


async def test_empty_segment_is_rejected() -> None:
    session = await _session(30)
    with pytest.raises(SegmentError):
        transfer_segment(_spec(5, 5), session, RecordingTarget())


@pytest.mark.parametrize(
    "chunk",
    [0, -1, 4194305],
)
async def test_invalid_chunk_size_is_rejected(chunk: int) -> None:
    with pytest.raises(ValueError):
        _spec(0, 10, chunk=chunk)


async def test_invalid_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        _spec(10, 5)
    with pytest.raises(ValueError):
        _spec(-1, 5)
