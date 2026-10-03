"""F3 段传输与真实文件流的组合集成测试。

真实文件作为源读取流与目标写入端：不同小块与段划分产生完全相同
的字节；停止控制可在段内到达并保留实际写入。
"""

from __future__ import annotations

import asyncio
import os
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.devices.read_session import ReadSessionError, SourceFile, open_read
from camctl.host_files.segments import SegmentSpec, transfer_segment
from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.operations.models import AttemptTicket
from camctl.session.supervision import ResponsibilityOwner, Supervisor

pytestmark = pytest.mark.asyncio

_TICKET = AttemptTicket(
    attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9",
    run_id=1,
)
_SIZE = 8000
#: Windows 的 os.open 默认文本模式：0x1A 被当作 EOF、0x0A 写入时扩成
#: CRLF，二进制内容必须显式按二进制打开。
_BINARY = getattr(os, "O_BINARY", 0)


class RealFileStream:
    """真实文件读取端：顺序读取本地文件字节。"""

    def __init__(self, path: Path) -> None:
        self._fd = os.open(path, os.O_RDONLY | _BINARY)

    def read(self, limit: int) -> bytes:
        return os.read(self._fd, limit)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        os.close(self._fd)


class RealTargetFile:
    """真实目标写入端：顺序写入、显式同步与关闭。"""

    def __init__(self, path: Path) -> None:
        self._fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY, 0o600
        )
        self.sync_calls = 0

    def write(self, data: bytes | memoryview) -> int:
        return os.write(self._fd, data)

    def sync(self) -> None:
        os.fsync(self._fd)
        self.sync_calls += 1

    def close(self) -> None:
        os.close(self._fd)


class StoppingTarget:
    """写入转发到真实目标，并在第 N 块写入后触发停止。"""

    def __init__(self, target: RealTargetFile, stop_after: int) -> None:
        self._target = target
        self._stop_after = stop_after
        self.stop = threading.Event()
        self.written_chunks = 0

    def write(self, data: bytes | memoryview) -> int:
        count = self._target.write(data)
        self.written_chunks += 1
        if self.written_chunks == self._stop_after:
            self.stop.set()
        return count

    def sync(self) -> None:
        self._target.sync()


def _source_bytes() -> bytes:
    return bytes(index % 251 for index in range(_SIZE))


async def _open_session(tmp_path: Path, data: bytes):
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(data)
    return await open_read(
        SourceFile(file_id="9", locator={}, size_bytes=len(data)),
        0,
        _TICKET,
        stream=RealFileStream(source_path),
        no_data_timeout_s=Decimal("30"),
    )


@pytest.mark.parametrize("chunk", [7, 512, _SIZE])
@pytest.mark.parametrize("split", [False, True])
async def test_chunk_and_segment_layouts_produce_identical_bytes(
    tmp_path: Path, chunk: int, split: bool
) -> None:
    data = _source_bytes()
    session = await _open_session(tmp_path, data)
    target_path = tmp_path / f"target-{chunk}-{split}.bin"
    target = RealTargetFile(target_path)
    ranges = [(0, _SIZE // 3), (_SIZE // 3, _SIZE)] if split else [(0, _SIZE)]
    try:
        results = []
        for start, end in ranges:
            spec = SegmentSpec(
                attempt="copy/9",
                round_index=0,
                target_name="copy-0001.part",
                range_start=start,
                range_end=end,
                chunk_size=chunk,
                stop=threading.Event(),
            )
            results.append(
                await asyncio.to_thread(transfer_segment, spec, session, target)
            )
    finally:
        target.close()

    assert all(result.error is None for result in results)
    assert all(result.synced for result in results)
    assert results[-1].processed_end == _SIZE
    assert results[-1].source_ended is True
    assert target.sync_calls == len(results)
    assert target_path.read_bytes() == data


async def test_stop_arrives_mid_segment_on_real_files(tmp_path: Path) -> None:
    data = _source_bytes()
    session = await _open_session(tmp_path, data)
    target_path = tmp_path / "stopped.bin"
    target = RealTargetFile(target_path)
    stopping = StoppingTarget(target, stop_after=5)
    spec = SegmentSpec(
        attempt="copy/9",
        round_index=0,
        target_name="copy-0001.part",
        range_start=0,
        range_end=_SIZE,
        chunk_size=100,
        stop=stopping.stop,
    )
    try:
        result = await asyncio.to_thread(transfer_segment, spec, session, stopping)
    finally:
        target.close()
        session.request_stop()
        await session.wait_stopped()

    assert result.error == "stopped"
    assert result.processed_end == 500
    assert result.synced is False
    assert result.source_ended is False
    assert target.sync_calls == 0
    assert target_path.read_bytes() == data[:500]


@pytest.mark.parametrize("sync_fails", [False, True])
async def test_sync_in_progress_keeps_actual_result(tmp_path: Path, sync_fails: bool) -> None:
    """同步已开始后收到停止请求，仍保留真实同步结果。"""
    sync_started = threading.Event()
    sync_release = threading.Event()
    stop = threading.Event()
    failure = OSError("sync failed")
    data = b"a" * 10
    session = await _open_session(tmp_path, data)
    target_path = tmp_path / "synced.bin"

    class GatedTarget(RealTargetFile):
        def sync(self) -> None:
            sync_started.set()
            assert sync_release.wait(timeout=10)
            if sync_fails:
                raise failure
            super().sync()

    target = GatedTarget(target_path)
    spec = SegmentSpec(
        attempt="copy/9", round_index=0, target_name="copy-0001.part",
        range_start=0, range_end=len(data), chunk_size=10, stop=stop,
    )
    worker = asyncio.create_task(asyncio.to_thread(transfer_segment, spec, session, target))
    try:
        assert await asyncio.to_thread(sync_started.wait, 10)
        stop.set()
    finally:
        sync_release.set()
        try:
            result = await worker
        finally:
            target.close()

    assert result.error == ("sync_failed" if sync_fails else None)
    assert result.failure is (failure if sync_fails else None)
    assert result.synced is (not sync_fails)
    assert result.processed_end == len(data)
    assert target.sync_calls == (0 if sync_fails else 1)
    assert target_path.read_bytes() == data


@pytest.mark.parametrize("write_fails", [False, True])
async def test_real_file_short_writes_keep_content_and_confirmed_prefix(tmp_path, write_fails):
    data = b"abcdef"
    session = await _open_session(tmp_path, data)
    target_path = tmp_path / "short.bin"
    failure = OSError("target failed after partial write")

    class ShortTarget(RealTargetFile):
        calls = 0

        def write(self, data):
            self.calls += 1
            if write_fails and self.calls == 2:
                # 异常前仍可能发生副作用，确认范围只能是此前成功调用的下界。
                super().write(data[:1])
                raise failure
            return super().write(data[:2])

    target = ShortTarget(target_path)
    spec = SegmentSpec("copy/9", 0, "copy.part", 0, 6, 6, threading.Event())
    try:
        result = await asyncio.to_thread(transfer_segment, spec, session, target)
    finally:
        target.close()
        session.request_stop()
        await session.wait_stopped()
    assert result.source_ended is True
    assert result.source_end.bytes_read == 6
    if write_fails:
        assert result.error == "write_failed"
        assert result.failure is failure
        assert result.processed_end == 2
        assert result.write_extent_known is False
        assert result.synced is False
        assert target.sync_calls == 0
        assert target_path.read_bytes() == b"abc"
    else:
        assert result.error is None
        assert result.processed_end == 6
        assert result.write_extent_known is True
        assert result.synced is True
        assert target_path.read_bytes() == data


@pytest.mark.parametrize("read_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_segment_retains_real_read_and_close_results(tmp_path, read_fails, close_fails):
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"abcdef")
    read_failure, close_failure = OSError("read failed"), OSError("close failed")

    class FailingStream(RealFileStream):
        read_calls = 0

        def read(self, limit):
            self.read_calls += 1
            if read_fails and self.read_calls == 2:
                raise read_failure
            return super().read(limit)

        def close(self):
            super().close()
            if close_fails:
                raise close_failure

    session = await open_read(SourceFile("9", {}, 6), 0, _TICKET,
                              stream=FailingStream(source_path), no_data_timeout_s=Decimal("30"))
    target_path = tmp_path / "target.bin"
    target = RealTargetFile(target_path)
    spec = SegmentSpec("copy/9", 0, "copy.part", 0, 6, 2, threading.Event())
    try:
        result = await asyncio.to_thread(transfer_segment, spec, session, target)
    finally:
        target.close()
        session.request_stop()
        ending, = await asyncio.gather(session.wait_stopped(), return_exceptions=True)
    expected_data = b"ab" if read_fails else (b"abcd" if close_fails else b"abcdef")
    assert target_path.read_bytes() == expected_data
    assert result.processed_end == len(expected_data)
    assert result.write_extent_known is True
    assert result.source_ended is (not close_fails)
    assert result.synced is (not read_fails and not close_fails)
    if read_fails or close_fails:
        assert result.error == "read_failed"
        assert target.sync_calls == 0
    else:
        assert result.error is None
    if close_fails:
        assert result.source_close_error is close_failure
        assert ending is close_failure
        if read_fails:
            assert result.failure.exceptions == (read_failure, close_failure)
        else:
            assert result.failure is close_failure
    else:
        assert result.source_end == ending
        assert result.source_close_error is None
        assert result.failure is (read_failure if read_fails else None)


@pytest.mark.parametrize("invalid", [b"cdefg", "cd", None])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_file_task_retains_segment_result_for_invalid_source(tmp_path, invalid, close_fails):
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"abcdef")
    close_failure = OSError("close failed")

    class InvalidStream(RealFileStream):
        read_calls = 0
        close_calls = 0

        def read(self, limit):
            self.read_calls += 1
            return super().read(limit) if self.read_calls == 1 else invalid

        def close(self):
            self.close_calls += 1
            super().close()
            if close_fails:
                raise close_failure

    stream = InvalidStream(source_path)
    session = await open_read(SourceFile("9", {}, 6), 0, _TICKET,
                              stream=stream, no_data_timeout_s=Decimal("30"))
    target_path = tmp_path / "target.bin"
    target = RealTargetFile(target_path)
    executor = FileTaskExecutor(Supervisor())
    owner = create_autospec(ResponsibilityOwner, instance=True)

    def body(stop):
        return transfer_segment(SegmentSpec("copy/9", 0, "copy.part", 0, 6, 2, stop), session, target)

    task = FileTask(FileTaskId("copy/9/segment"), 9, "segment", "copy", body)
    try:
        task_result = await executor.run_file_task(task, owner)
    finally:
        target.close()
        session.request_stop()
        ending, = await asyncio.gather(session.wait_stopped(), return_exceptions=True)
    assert task_result.ran is True and task_result.error is None
    result = task_result.value
    assert result.error == "read_failed"
    assert result.processed_end == 2
    assert result.synced is False
    assert target.sync_calls == 0
    assert target_path.read_bytes() == b"ab"
    assert session.position() == 2
    assert stream.close_calls == 1
    if close_fails:
        assert isinstance(result.failure, ExceptionGroup)
        assert isinstance(result.failure.exceptions[0], ReadSessionError)
        assert result.failure.exceptions[1] is close_failure
        assert result.source_close_error is close_failure
        assert result.source_ended is False
        assert ending is close_failure
    else:
        assert isinstance(result.failure, ReadSessionError)
        assert result.source_end == ending
        assert result.source_ended is True
        assert result.source_end.bytes_read == 2
