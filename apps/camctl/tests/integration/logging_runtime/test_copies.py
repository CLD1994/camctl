"""L5 独立故障标记与锁内日志副本交付的组合测试。

真实文件、CLH 跨进程锁与交接目录：触发记录在锁内写入并复制，其
他进程的竞争轮换不使副本漏掉触发记录；标记先行、进度更新、抑制
与失败清理、启动清理按规格决策表组合验证；全程不依赖状态库。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from integration.logging_runtime.sync_support import fail_directory_sync
from camctl.logging_runtime.copies import (
    MARKER_NAME,
    CopyOutcome,
    CopyRequest,
    FailureLogService,
    MarkerPresence,
    MarkerStage,
    MarkerStore,
    is_copy_file_name,
    is_marker_temp_name,
    marker_temp_file_name,
    startup_sweep,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

_TRIGGER = "report-failure-trigger-record"


def _channel(log_path: Path, *, max_bytes: int = 256) -> FileChannel:
    return FileChannel(log_path, max_bytes=max_bytes, file_count=3)


def _layout(tmp_path: Path):
    staging = tmp_path / "staging"
    ready = tmp_path / "ready"
    logs_dir = staging / "logs"
    logs_dir.mkdir(parents=True)
    ready.mkdir()
    log_path = tmp_path / "camctl.log"
    return staging, ready, logs_dir, log_path


def _request(staging: Path, ready: Path, channel: FileChannel,
             marker: MarkerStore, message: str = _TRIGGER) -> CopyRequest:
    return CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message=message),
        staging_root=staging, ready_dir=ready,
        channel=channel, marker=marker,
    )


def _capture_copy_destination(channel, monkeypatch):
    """记录本次公开复制调用拥有的目标路径。"""
    destinations = []
    real_copy = channel.copy_with_write

    def capture(record, destination):
        destinations.append(Path(destination))
        return real_copy(record, destination)

    monkeypatch.setattr(channel, "copy_with_write", capture)
    return destinations


def _hold_snapshot(monkeypatch, source_path, destination, entered, release):
    real_copy = shutil.copyfileobj
    snapshots = []

    def copy(source, target, *args, **kwargs):
        source_name = getattr(source, "name", None)
        target_name = getattr(target, "name", None)
        if (isinstance(source_name, (str, os.PathLike))
                and isinstance(target_name, (str, os.PathLike))
                and Path(source_name) == source_path and Path(target_name) == destination):
            snapshots.append(source_path.read_bytes())
            entered.set()
            if not release.wait(timeout=30):
                raise TimeoutError("测试未释放持锁的日志副本操作")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", copy)
    return snapshots


def _start_copy_thread(channel, destination):
    result = Future()

    def copy():
        try:
            result.set_result(channel.copy_with_write(
                LogRecord(level=LogLevel.ERROR, message=_TRIGGER), destination))
        except BaseException as error:
            result.set_exception(error)

    thread = threading.Thread(target=copy, daemon=True)
    thread.start()
    return thread, result


# ---- 锁内写入与复制 --------------------------------------------------


class TestLockedCopy:
    pytestmark = pytest.mark.asyncio
    async def test_copy_contains_all_current_bytes(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        channel.write_record(LogRecord(level=LogLevel.INFO, message="before"))
        destination = logs_dir / "log-copy-r1.log"
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message=_TRIGGER), destination)
        assert copy_error is None and write_result.appended is True
        copied = destination.read_bytes()
        assert _TRIGGER.encode() in copied
        # 副本即复制时点的当前日志文件：触发记录之前的行也在其中。
        assert b"before" in copied
        # 原日志继续留在配置路径。
        assert log_path.exists()
        channel.close()

    async def test_copy_contains_trigger_before_rotation(
            self, tmp_path: Path, monkeypatch) -> None:
        """复制持锁时，另一进程的写入被阻挡，之后完成真实轮换。"""
        import concurrent_log_handler

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path, max_bytes=128)
        assert channel.write_record(LogRecord(
            level=LogLevel.INFO, message="before copy")).appended
        destination = logs_dir / "log-copy-r1.log"
        entered, release = threading.Event(), threading.Event()
        blocked, acquired = threading.Event(), threading.Event()
        snapshots = _hold_snapshot(monkeypatch, log_path, destination, entered, release)
        lock_dir, lock_name = concurrent_log_handler.ConcurrentRotatingFileHandler.baseLockFilename(str(log_path))
        lock_path = str(Path(lock_dir) / lock_name)
        script = r"""
import sys
import concurrent_log_handler as clh
import portalocker
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

path, expected_lock = sys.argv[1:]
real_lock = clh.lock
probed = False

def lock(stream, flags):
    global probed
    if not probed:
        assert str(stream.name) == expected_lock
        try:
            real_lock(stream, flags | portalocker.LOCK_NB)
        except portalocker.exceptions.AlreadyLocked:
            print("blocked", flush=True)
        else:
            portalocker.unlock(stream)
            raise AssertionError("竞争日志锁意外取得成功")
        probed = True
        result = real_lock(stream, flags)
        print("acquired", flush=True)
        return result
    return real_lock(stream, flags)

clh.lock = lock
channel = FileChannel(path, max_bytes=128, file_count=3)
try:
    for index in range(40):
        result = channel.write_record(LogRecord(
            level=LogLevel.INFO, message=f"proc-line-{index:03d}"))
        assert result.appended
finally:
    channel.close()
"""
        competitor = None
        output_lines = []
        reader = None
        copy_thread, copy_result = _start_copy_thread(channel, destination)
        try:
            assert entered.wait(10), "副本没有进入持锁复制"
            competitor = subprocess.Popen(
                [sys.executable, "-c", script, str(log_path), lock_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(Path(__file__).parents[3]))

            def read_events():
                for line in competitor.stdout:
                    output_lines.append(line.strip())
                    if line.strip() == "blocked":
                        blocked.set()
                    elif line.strip() == "acquired":
                        acquired.set()

            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()
            assert blocked.wait(10), "子进程未证实文件锁竞争"
            assert not acquired.is_set()
            assert not copy_result.done()
            release.set()
            write_result, copy_error = copy_result.result(timeout=30)
            assert competitor.wait(timeout=30) == 0, competitor.stderr.read()
            reader.join(timeout=10)
            assert not reader.is_alive()
            assert output_lines == ["blocked", "acquired"]
            assert copy_error is None and write_result.appended is True
            copied = destination.read_bytes()
            assert snapshots == [copied]
            assert _TRIGGER.encode() in copied
            for line in copied.splitlines():
                json.loads(line)
            assert copied.endswith(b"\n")
            archives = [Path(f"{log_path}.{index}") for index in (1, 2)]
            assert any(item.is_file() and any(
                json.loads(line)["message"].startswith("proc-line-")
                for line in item.read_text().splitlines()) for item in archives)
            assert json.loads(log_path.read_text().splitlines()[-1])["message"] == "proc-line-039"
            assert destination.read_bytes() == copied
        finally:
            release.set()
            if competitor is not None:
                if competitor.poll() is None:
                    competitor.kill()
                    competitor.wait(timeout=10)
                if reader is not None:
                    reader.join(timeout=10)
                competitor.stdout.close()
                competitor.stderr.close()
            copy_thread.join(timeout=10)
            if not copy_thread.is_alive():
                channel.close()
            assert not copy_thread.is_alive(), "日志副本线程未结束"

    async def test_records_stay_complete_during_copy(
            self, tmp_path: Path, monkeypatch) -> None:
        """同一通道的线程在副本结束后继续完整写入。"""
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path, max_bytes=512)
        assert channel.write_record(LogRecord(
            level=LogLevel.INFO, message="before copy")).appended
        destination = logs_dir / "log-copy-r1.log"
        entered, release, write_started = threading.Event(), threading.Event(), threading.Event()
        snapshots = _hold_snapshot(monkeypatch, log_path, destination, entered, release)

        real_write = channel.write_record

        def observe_write(record):
            write_started.set()
            return real_write(record)

        monkeypatch.setattr(channel, "write_record", observe_write)

        def write_same_channel():
            return [channel.write_record(LogRecord(
                level=LogLevel.INFO, message=f"churn-{index:04d}" + "x" * 40))
                    for index in range(12)]

        writer_result = Future()
        writer = None

        def run_writer():
            try:
                writer_result.set_result(write_same_channel())
            except BaseException as error:
                writer_result.set_exception(error)

        copy_thread, copy_result = _start_copy_thread(channel, destination)
        try:
            assert entered.wait(10), "副本没有进入持锁复制"
            writer = threading.Thread(target=run_writer, daemon=True)
            writer.start()
            assert write_started.wait(10)
            assert not writer_result.done() and not copy_result.done()
            release.set()
            write_result, copy_error = copy_result.result(timeout=30)
            assert all(result.appended for result in writer_result.result(timeout=30))
            assert copy_error is None and write_result.appended is True
            copied = destination.read_bytes()
            assert snapshots == [copied]
            assert _TRIGGER.encode() in copied
            for line in copied.splitlines():
                record = json.loads(line)
                assert record["message"].startswith(("before copy", "churn-", _TRIGGER, "camctl"))
                assert record["level"] in ("INFO", "ERROR")
            assert copied.endswith(b"\n")
            archives = [Path(f"{log_path}.{index}") for index in (1, 2)]
            assert any(item.is_file() and any(
                json.loads(line)["message"].startswith("churn-")
                for line in item.read_text().splitlines()) for item in archives)
            assert json.loads(log_path.read_text().splitlines()[-1])["message"].startswith("churn-0011")
            assert destination.read_bytes() == copied
        finally:
            release.set()
            copy_thread.join(timeout=10)
            if writer is not None:
                writer.join(timeout=10)
            if not copy_thread.is_alive() and (writer is None or not writer.is_alive()):
                channel.close()
            assert not copy_thread.is_alive(), "日志副本线程未结束"
            assert writer is None or not writer.is_alive(), "日志写入线程未结束"

    @pytest.mark.parametrize(("target_error_type", "source_error_type", "kind"), [
        (OSError, RuntimeError, "group"),
        (RuntimeError, asyncio.CancelledError, "target"),
        (OSError, asyncio.CancelledError, "source"),
    ])
    async def test_copy_close_exceptions_keep_ordered_primary_and_write_result(
            self, tmp_path: Path, monkeypatch,
            target_error_type, source_error_type, kind: str) -> None:
        """真实快照句柄的关闭异常保留主对象／次序和typed通道事实。"""
        import builtins

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        channel.write_record(LogRecord(level=LogLevel.INFO, message="before"))
        destination = logs_dir / "log-copy-close-order.log"
        close_calls: list[str] = []
        actual_open = builtins.open
        target_error = target_error_type("target close failure")
        source_error = source_error_type("source close failure")

        class CloseFaultStream:
            def __init__(self, stream, side: str) -> None:
                self._stream = stream
                self._side = side
                self._attempted = False

            def close(self):
                close_calls.append(self._side)
                assert not self._attempted, f"{self._side} close retried"
                self._attempted = True
                self._stream.close()
                if self._side == "target":
                    raise target_error
                raise source_error

            def __getattr__(self, name):
                return getattr(self._stream, name)

        def open_with_close_fault(path, mode="r", *args, **kwargs):
            stream = actual_open(path, mode, *args, **kwargs)
            if not isinstance(path, (str, os.PathLike)):
                return stream
            try:
                candidate = Path(path)
            except TypeError:
                return stream
            if candidate == log_path and mode == "rb":
                return CloseFaultStream(stream, "source")
            if candidate == destination and mode == "wb":
                return CloseFaultStream(stream, "target")
            return stream

        monkeypatch.setattr(builtins, "open", open_with_close_fault)
        try:
            with pytest.raises(BaseException) as caught:
                channel.copy_with_write(
                    LogRecord(level=LogLevel.ERROR, message=_TRIGGER),
                    destination,
                )

            if kind == "group":
                assert isinstance(caught.value, ExceptionGroup)
                assert caught.value.exceptions == (target_error, source_error)
                result = caught.value.write_result
            elif kind == "target":
                assert caught.value is target_error
                result = target_error.write_result
                assert any("source close failure" in note
                           for note in target_error.__notes__)
            else:
                assert caught.value is source_error
                assert source_error.__cause__ is target_error
                result = source_error.write_result

            assert result.copy_handles_closed is False
            assert result.copy_target_close_error is not None
            assert "target close failure" in result.copy_target_close_error
            assert result.copy_source_close_error is not None
            assert "source close failure" in result.copy_source_close_error
            assert result.disables_file_logging is False
            assert sorted(close_calls) == ["source", "target"]
            assert destination.exists() and _TRIGGER.encode() in destination.read_bytes()
            assert channel.write_record(LogRecord(
                level=LogLevel.INFO, message="after close failure")).appended
        finally:
            channel.close()


# ---- 完整交付链 ------------------------------------------------------

class TestDelivery:
    pytestmark = pytest.mark.asyncio
    async def test_full_delivery_publishes_copy(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        channel.write_record(LogRecord(level=LogLevel.INFO, message="context"))
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        receipt = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.PUBLISHED
        assert is_copy_file_name(receipt.copy_name)
        published = ready / receipt.copy_name
        copied = published.read_bytes()
        assert _TRIGGER.encode() in copied and b"context" in copied
        # 标记保留并记录发布进度；准备位置无副本残留。
        check = marker.check()
        assert check.presence is MarkerPresence.EXISTS
        assert check.state.stage is MarkerStage.PUBLISHED
        assert check.state.copy_name == receipt.copy_name
        leftovers = [p.name for p in logs_dir.iterdir()
                     if is_copy_file_name(p.name)]
        assert leftovers == []
        # 原日志继续可用。
        after = channel.write_record(LogRecord(level=LogLevel.INFO,
                                               message="after"))
        assert after.appended is True
        channel.close()

    async def test_repeated_failure_is_suppressed(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        first = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.PUBLISHED
        second = await deliver_failure_copy(
            _request(staging, ready, channel, marker,
                     message="later failure differs"))
        assert second.outcome is CopyOutcome.SUPPRESSED
        assert second.copy_name == first.copy_name
        assert [p.name for p in ready.iterdir()] == [first.copy_name]
        channel.close()

    async def test_invalid_marker_content_still_suppresses(
            self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        (logs_dir / MARKER_NAME).write_bytes(b"{broken")
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        receipt = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.SUPPRESSED
        assert receipt.copy_name is None
        assert list(ready.iterdir()) == []
        channel.close()

    async def test_marker_create_failure_stops_before_copy(
            self, tmp_path: Path, monkeypatch) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime import copies
        original_open = os.open

        def fail_marker_open(path, flags, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == logs_dir and is_marker_temp_name(candidate.name):
                raise OSError("disk full")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fail_marker_open)
        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.MARKER_CREATE_FAILED
        assert list(ready.iterdir()) == []
        assert [p.name for p in logs_dir.iterdir()] == []
        channel.close()

    async def test_publish_failure_cleans_staging_copy(
            self, tmp_path: Path, monkeypatch) -> None:
        from camctl.host_files.handoff import (
            HandoffDirectories, PublishResult, PublishStage, ReadyName)
        from camctl.host_files.io import DirectorySyncStage
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)

        async def rejected(source, directories, target):
            return PublishResult(
                PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED,
                False, "target_exists")

        from camctl.logging_runtime import copies
        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker), publish=rejected)
        assert receipt.outcome is CopyOutcome.COPY_FAILED
        # 失败清理：准备位置无副本；标记保留故障持续。
        assert [p.name for p in logs_dir.iterdir()] == [MARKER_NAME]
        assert list(ready.iterdir()) == []
        channel.close()

    @pytest.mark.parametrize("failed_closes", [
        ("source",), ("target",), ("source", "target"),
    ])
    async def test_copy_close_error_preserves_staging_and_marker(
            self, tmp_path: Path, monkeypatch,
            failed_closes: tuple[str, ...]) -> None:
        """真实副本句柄关闭未确认时保留 staging 与正式标记。"""
        import builtins

        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        destinations = _capture_copy_destination(channel, monkeypatch)
        close_calls: list[str] = []
        actual_open = builtins.open

        class CloseFaultStream:
            def __init__(self, stream, side: str) -> None:
                self._stream = stream
                self._side = side
                self._close_attempted = False

            def __enter__(self):
                return self

            def close(self):
                close_calls.append(self._side)
                if self._close_attempted:
                    raise AssertionError(f"{self._side} close retried")
                self._close_attempted = True
                self._stream.close()
                if self._side in failed_closes:
                    raise OSError(f"{self._side} close failed")

            def __exit__(self, exc_type, exc, traceback):
                if not self._close_attempted:
                    self.close()
                return False

            def __getattr__(self, name):
                return getattr(self._stream, name)

        def open_with_close_fault(path, mode="r", *args, **kwargs):
            stream = actual_open(path, mode, *args, **kwargs)
            if not isinstance(path, (str, os.PathLike)):
                return stream
            try:
                candidate = Path(path)
            except TypeError:
                return stream
            if candidate == log_path and mode == "rb":
                return CloseFaultStream(stream, "source")
            if destinations and candidate == destinations[0] and mode == "wb":
                return CloseFaultStream(stream, "target")
            return stream

        monkeypatch.setattr(builtins, "open", open_with_close_fault)
        try:
            receipt = await copies.deliver_failure_copy(
                _request(staging, ready, channel, marker))

            assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
            assert receipt.copy_name is not None
            assert (logs_dir / receipt.copy_name).exists()
            assert (logs_dir / MARKER_NAME).exists()
            assert sorted(close_calls) == sorted(("target", "source"))
            assert receipt.write_result is not None
            assert receipt.write_result.disables_file_logging is False
            assert receipt.write_result.copy_handles_closed is False
            assert ((receipt.write_result.copy_source_close_error is not None)
                    is ("source" in failed_closes))
            assert ((receipt.write_result.copy_target_close_error is not None)
                    is ("target" in failed_closes))
            for side in failed_closes:
                error = getattr(
                    receipt.write_result, f"copy_{side}_close_error")
                assert f"{side} close failed" in error
            # 副本句柄关闭错误不代表普通日志文件通道已失效。
            later = channel.write_record(
                LogRecord(level=LogLevel.INFO, message="after copy close error"))
            assert later.appended is True
            assert channel.disabled is False
        finally:
            channel.close()

    async def test_copy_operation_and_both_close_errors_are_preserved(
            self, tmp_path: Path, monkeypatch) -> None:
        """复制主错误与源、目标 close 错误分别诊断且不触发 unlink。"""
        import builtins
        import shutil

        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        destinations = _capture_copy_destination(channel, monkeypatch)
        close_calls: list[str] = []
        actual_open = builtins.open

        class CloseFaultStream:
            def __init__(self, stream, side: str) -> None:
                self._stream = stream
                self._side = side
                self._close_attempted = False

            def __enter__(self):
                return self

            def close(self):
                close_calls.append(self._side)
                if self._close_attempted:
                    raise AssertionError(f"{self._side} close retried")
                self._close_attempted = True
                self._stream.close()
                raise OSError(f"{self._side} close failed")

            def __exit__(self, exc_type, exc, traceback):
                if not self._close_attempted:
                    self.close()
                return False

            def __getattr__(self, name):
                return getattr(self._stream, name)

        def open_with_close_fault(path, mode="r", *args, **kwargs):
            stream = actual_open(path, mode, *args, **kwargs)
            if not isinstance(path, (str, os.PathLike)):
                return stream
            try:
                candidate = Path(path)
            except TypeError:
                return stream
            if candidate == log_path and mode == "rb":
                return CloseFaultStream(stream, "source")
            if destinations and candidate == destinations[0] and mode == "wb":
                return CloseFaultStream(stream, "target")
            return stream

        def fail_copy(source, target, *args, **kwargs):
            raise OSError("copy operation failed")

        monkeypatch.setattr(builtins, "open", open_with_close_fault)
        monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
        try:
            receipt = await copies.deliver_failure_copy(
                _request(staging, ready, channel, marker))

            assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
            assert receipt.copy_name is not None
            assert (logs_dir / receipt.copy_name).exists()
            assert (logs_dir / MARKER_NAME).exists()
            assert sorted(close_calls) == sorted(("target", "source"))
            assert receipt.copy_error is not None
            assert "copy operation failed" in receipt.copy_error
            assert receipt.write_result is not None
            assert receipt.write_result.disables_file_logging is False
            assert receipt.write_result.copy_handles_closed is False
            assert receipt.write_result.copy_source_close_error is not None
            assert "source close failed" in receipt.write_result.copy_source_close_error
            assert receipt.write_result.copy_target_close_error is not None
            assert "target close failed" in receipt.write_result.copy_target_close_error
        finally:
            channel.close()

    @pytest.mark.parametrize("failure_kind", ["program", "worker_cancel"])
    async def test_confirmed_closed_snapshot_exception_cleans_own_staging_once(
            self, tmp_path: Path, monkeypatch,
            failure_kind: str) -> None:
        """线程主体异常后，已确认关闭且未发布的本次 staging 必须清理一次。"""
        import builtins
        import shutil

        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        destinations = _capture_copy_destination(channel, monkeypatch)
        primary: BaseException = (
            asyncio.CancelledError("snapshot worker cancellation")
            if failure_kind == "worker_cancel" else
            RuntimeError("snapshot worker program failure")
        )
        close_calls: list[str] = []
        cleanup_calls: list[Path] = []
        actual_open = builtins.open
        original_unlink = os.unlink

        class CloseCountingStream:
            def __init__(self, stream, side: str) -> None:
                self._stream = stream
                self._side = side

            def close(self):
                result = self._stream.close()
                close_calls.append(self._side)
                return result

            def __getattr__(self, name):
                return getattr(self._stream, name)

        def open_counting_copy_handles(path, mode="r", *args, **kwargs):
            stream = actual_open(path, mode, *args, **kwargs)
            if not isinstance(path, (str, os.PathLike)):
                return stream
            try:
                candidate = Path(path)
            except TypeError:
                return stream
            if candidate == log_path and mode == "rb":
                return CloseCountingStream(stream, "source")
            if destinations and candidate == destinations[0] and mode == "wb":
                return CloseCountingStream(stream, "target")
            return stream

        def fail_snapshot_copy(_source, _target, *args, **kwargs):
            raise primary

        def observe_cleanup(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == logs_dir and is_copy_file_name(candidate.name):
                assert close_calls == ["target", "source"]
                cleanup_calls.append(candidate)
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", open_counting_copy_handles)
        monkeypatch.setattr(shutil, "copyfileobj", fail_snapshot_copy)
        monkeypatch.setattr(os, "unlink", observe_cleanup)
        try:
            if failure_kind == "worker_cancel":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await copies.deliver_failure_copy(
                        _request(staging, ready, channel, marker))
                assert caught.value is primary
                result = getattr(primary, "write_result", None)
            else:
                receipt = await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker))
                assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
                assert receipt.copy_error is not None
                assert "snapshot worker program failure" in receipt.copy_error
                result = receipt.write_result

            assert result is not None
            assert result.copy_handles_closed is True
            assert result.copy_source_close_error is None
            assert result.copy_target_close_error is None
            assert sorted(close_calls) == ["source", "target"]
            assert len(cleanup_calls) == 1
            assert not cleanup_calls[0].exists()
            assert marker.check().presence is MarkerPresence.EXISTS
            assert list(ready.iterdir()) == []
        finally:
            channel.close()

    async def test_failed_delivery_is_not_retried(
            self, tmp_path: Path) -> None:
        """发布失败清理副本后：同一故障的后续失败不再复制或投递。"""
        from camctl.host_files.handoff import PublishResult, PublishStage
        from camctl.host_files.io import DirectorySyncStage
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)

        async def rejected(source, directories, target):
            return PublishResult(
                PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED,
                False, "target_exists")

        from camctl.logging_runtime import copies
        failed = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker), publish=rejected)
        assert failed.outcome is CopyOutcome.COPY_FAILED
        # 标记保留：后续失败被抑制，不重新复制、不递归。
        later = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker,
                     message="second failure in same round"))
        assert later.outcome is CopyOutcome.SUPPRESSED
        assert later.copy_name == failed.copy_name
        assert list(ready.iterdir()) == []
        assert [p.name for p in logs_dir.iterdir()] == [MARKER_NAME]
        channel.close()

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
    @pytest.mark.parametrize("failed_progress_call,failed_stage", [
        (2, MarkerStage.COPIED),
        (3, MarkerStage.PUBLISHED),
    ])
    async def test_real_progress_sync_failure_keeps_published_bytes(
            self, tmp_path: Path, monkeypatch, failed_progress_call: int,
            failed_stage: MarkerStage) -> None:
        """真实 marker 替换已放置时目录同步失败，不改写发布文件事实。"""
        from camctl.host_files.handoff import PublishStage
        from camctl.logging_runtime import copies
        from camctl.logging_runtime.copies import SaveOutcome

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        fail_directory_sync(monkeypatch, logs_dir, call=failed_progress_call,
                            message="injected progress directory sync failure")

        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker))

        assert receipt.outcome is CopyOutcome.PUBLISHED
        assert receipt.stage is MarkerStage.PUBLISHED
        assert tuple(stage for stage, _ in receipt.marker_progress) == (
            MarkerStage.COPIED, MarkerStage.PUBLISHED)
        saved = dict(receipt.marker_progress)[failed_stage]
        assert saved.outcome is SaveOutcome.PLACED_UNSYNCED
        assert "injected progress directory sync failure" in saved.error
        assert len(list(ready.iterdir())) == 1
        published = ready / receipt.copy_name
        payload = published.read_bytes()
        assert _TRIGGER.encode() in payload
        assert receipt.write_result.appended is True
        assert receipt.copy_error is None
        channel.close()

    @pytest.mark.parametrize("cleanup_mode", ["removed", "already_absent"])
    async def test_copied_progress_exception_cleans_confirmed_staging(
            self, tmp_path: Path, monkeypatch, cleanup_mode: str) -> None:
        """COPIED 进度异常结束交付时，关闭已确认的准备副本只清理一次。"""
        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        primary = RuntimeError("injected copied progress failure")
        original_update = marker.update_stage
        original_unlink = os.unlink
        cleanup_calls: list[Path] = []
        publisher_calls: list[Path] = []

        def fail_copied_progress(stage, **kwargs):
            if stage is MarkerStage.COPIED:
                raise primary
            return original_update(stage, **kwargs)

        def controlled_unlink(path, *args, **kwargs):
            path = Path(path)
            if path.parent == logs_dir and is_copy_file_name(path.name):
                cleanup_calls.append(path)
                if cleanup_mode == "already_absent":
                    original_unlink(path, *args, **kwargs)
                    raise FileNotFoundError(path)
            return original_unlink(path, *args, **kwargs)

        async def publisher(source, _directories, _target):
            publisher_calls.append(Path(source))
            raise AssertionError("publish must not follow COPIED exception")

        monkeypatch.setattr(marker, "update_stage", fail_copied_progress)
        monkeypatch.setattr(os, "unlink", controlled_unlink)
        try:
            with pytest.raises(RuntimeError) as caught:
                await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker),
                    publish=publisher,
                )

            assert caught.value is primary
            from camctl.logging_runtime.copies import CopyReceipt
            receipt = getattr(primary, "copy_receipt", None)
            assert isinstance(receipt, CopyReceipt)
            assert receipt.file_stage.value == "marker_progress_copied"
            assert receipt.write_result.copy_handles_closed is True
            assert receipt.publish_result is None
            assert receipt.copy_location.value == "absent"
            assert receipt.cleanup_attempted is True
            assert receipt.cleanup_error is None
            assert len(cleanup_calls) == 1
            assert not cleanup_calls[0].exists()
            assert publisher_calls == []
            assert marker.check().presence is MarkerPresence.EXISTS
        finally:
            channel.close()

    @pytest.mark.parametrize("cleanup_failure", [
        OSError("unlink OSError"),
        RuntimeError("unlink program failure"),
        asyncio.CancelledError("unlink worker cancellation"),
    ])
    async def test_cleanup_failure_preserves_primary_and_receipt(
            self, tmp_path: Path, monkeypatch,
            cleanup_failure: BaseException) -> None:
        """清理失败不替换进度主异常，也不把仍可能存在的文件记为 ABSENT。"""
        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        primary = RuntimeError("primary copied progress failure")
        original_update = marker.update_stage
        original_unlink = os.unlink
        cleanup_calls: list[Path] = []
        publisher_calls: list[Path] = []

        def fail_copied_progress(stage, **kwargs):
            if stage is MarkerStage.COPIED:
                raise primary
            return original_update(stage, **kwargs)

        def fail_cleanup(path, *args, **kwargs):
            path = Path(path)
            if path.parent == logs_dir and is_copy_file_name(path.name):
                cleanup_calls.append(path)
                raise cleanup_failure
            return original_unlink(path, *args, **kwargs)

        async def publisher(source, _directories, _target):
            publisher_calls.append(Path(source))
            raise AssertionError("publish must not follow COPIED exception")

        monkeypatch.setattr(marker, "update_stage", fail_copied_progress)
        monkeypatch.setattr(os, "unlink", fail_cleanup)
        try:
            with pytest.raises(RuntimeError) as caught:
                await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker),
                    publish=publisher,
                )

            assert caught.value is primary
            from camctl.logging_runtime.copies import CopyReceipt
            receipt = getattr(primary, "copy_receipt", None)
            assert isinstance(receipt, CopyReceipt)
            assert receipt.file_stage.value == "marker_progress_copied"
            assert receipt.publish_result is None
            assert receipt.copy_location.value == "staging"
            assert receipt.cleanup_attempted is True
            assert cleanup_failure.args[0] in (receipt.cleanup_error or "")
            assert len(cleanup_calls) == 1
            assert cleanup_calls[0].exists()
            assert publisher_calls == []
            assert any("cleanup" in note
                       for note in getattr(primary, "__notes__", ()))
        finally:
            channel.close()

    @pytest.mark.parametrize("termination", ["copy_error", "not_moved"])
    @pytest.mark.parametrize(
        "cleanup_type", [RuntimeError, asyncio.CancelledError])
    async def test_cleanup_unexpected_exception_after_resultized_error_is_propagated(
            self, tmp_path: Path, monkeypatch, termination: str,
            cleanup_type) -> None:
        """结果化复制或 NOT_MOVED 错误的清理异常不能被回执吞掉。"""
        from camctl.logging_runtime import copies
        from camctl.host_files.handoff import PublishResult, PublishStage
        from camctl.host_files.io import DirectorySyncStage

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        original_copy = channel.copy_with_write
        original_unlink = os.unlink
        cleanup_failure = cleanup_type("cleanup after returned copy error")
        cleanup_calls: list[Path] = []

        def return_copy_error(record, destination):
            result, copy_error = original_copy(record, destination)
            assert copy_error is None
            assert result.copy_handles_closed is True
            if termination == "copy_error":
                return result, "controlled resultized copy error"
            return result, copy_error

        async def not_moved(_source, _directories, _target):
            return PublishResult(
                PublishStage.NOT_MOVED,
                DirectorySyncStage.NOT_ATTEMPTED,
                False,
                "controlled NOT_MOVED result",
            )

        def fail_cleanup(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == logs_dir and is_copy_file_name(candidate.name):
                cleanup_calls.append(candidate)
                raise cleanup_failure
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(channel, "copy_with_write", return_copy_error)
        monkeypatch.setattr(os, "unlink", fail_cleanup)
        try:
            with pytest.raises(cleanup_type) as caught:
                kwargs = {"publish": not_moved} if termination == "not_moved" else {}
                await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker), **kwargs)

            assert caught.value is cleanup_failure
            receipt = getattr(cleanup_failure, "copy_receipt", None)
            from camctl.logging_runtime.copies import CopyReceipt
            assert isinstance(receipt, CopyReceipt)
            assert receipt.outcome is CopyOutcome.COPY_FAILED
            assert receipt.file_stage.value == "copy_cleanup"
            if termination == "copy_error":
                assert receipt.copy_error == "controlled resultized copy error"
                assert receipt.publish_result is None
            else:
                assert receipt.publish_result.stage is PublishStage.NOT_MOVED
                assert receipt.publish_error == "controlled NOT_MOVED result"
            assert receipt.copy_location.value == "staging"
            assert receipt.cleanup_attempted is True
            assert "cleanup after returned copy error" in receipt.cleanup_error
            assert len(cleanup_calls) == 1 and cleanup_calls[0].exists()
            assert list(ready.iterdir()) == []
            assert marker.check().presence is MarkerPresence.EXISTS
        finally:
            channel.close()

    @pytest.mark.parametrize(
        ("stop_after", "cleanup_type"), [
            ("copy_write", RuntimeError),
            ("copy_write", asyncio.CancelledError),
            ("copied_progress", RuntimeError),
            ("copied_progress", asyncio.CancelledError),
        ])
    async def test_cleanup_unexpected_exception_during_stopped_delivery_is_propagated(
            self, tmp_path: Path, monkeypatch, stop_after: str,
            cleanup_type) -> None:
        """写入或 COPIED 后停止仍清理一次，并保留各自已达阶段。"""
        from camctl.logging_runtime import copies
        from camctl.host_files.handoff import PublishResult, PublishStage
        from camctl.host_files.io import DirectorySyncStage

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        original_update = marker.update_stage
        original_unlink = os.unlink
        stopped = {"value": False}
        cleanup_failure = cleanup_type("cleanup after ordinary stop")
        cleanup_calls: list[Path] = []
        publish_calls: list[Path] = []

        original_copy = channel.copy_with_write

        def stop_after_copy_write(record, destination):
            result = original_copy(record, destination)
            if stop_after == "copy_write":
                stopped["value"] = True
            return result

        def stop_after_copied_progress(stage, **kwargs):
            result = original_update(stage, **kwargs)
            if (stop_after == "copied_progress"
                    and stage is MarkerStage.COPIED):
                stopped["value"] = True
            return result

        def fail_cleanup(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == logs_dir and is_copy_file_name(candidate.name):
                cleanup_calls.append(candidate)
                raise cleanup_failure
            return original_unlink(path, *args, **kwargs)

        async def publisher(source, _directories, _target):
            publish_calls.append(Path(source))
            return PublishResult(
                PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)

        monkeypatch.setattr(channel, "copy_with_write", stop_after_copy_write)
        monkeypatch.setattr(marker, "update_stage", stop_after_copied_progress)
        monkeypatch.setattr(os, "unlink", fail_cleanup)
        try:
            with pytest.raises(cleanup_type) as caught:
                await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker),
                    publish=publisher,
                    stop_requested=lambda: stopped["value"],
                )

            assert caught.value is cleanup_failure
            receipt = getattr(cleanup_failure, "copy_receipt", None)
            from camctl.logging_runtime.copies import CopyReceipt
            assert isinstance(receipt, CopyReceipt)
            assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
            assert receipt.file_stage.value == "copy_cleanup"
            expected_progress = ((MarkerStage.COPIED,)
                                 if stop_after == "copied_progress" else ())
            assert tuple(stage for stage, _ in receipt.marker_progress) == (
                expected_progress)
            assert receipt.publish_result is None
            assert receipt.copy_location.value == "staging"
            assert receipt.cleanup_attempted is True
            assert "cleanup after ordinary stop" in receipt.cleanup_error
            assert len(cleanup_calls) == 1 and cleanup_calls[0].exists()
            assert publish_calls == []
            assert list(ready.iterdir()) == []
            expected_marker_stage = (
                MarkerStage.COPIED if stop_after == "copied_progress"
                else MarkerStage.TRIGGERED)
            assert marker.check().state.stage is expected_marker_stage
        finally:
            channel.close()

    @pytest.mark.parametrize(
        ("primary_stage", "primary_type", "cleanup_type", "expected"), [
            ("copy_write", OSError, RuntimeError, "group"),
            ("copied_progress", OSError, RuntimeError, "group"),
            ("copy_write", OSError, asyncio.CancelledError, "control"),
            ("copied_progress", OSError, asyncio.CancelledError, "control"),
            ("copy_write", OSError, OSError, "resultized"),
            ("copied_progress", OSError, OSError, "primary"),
            ("copy_write", RuntimeError, RuntimeError, "primary"),
            ("copied_progress", RuntimeError, RuntimeError, "primary"),
            ("copy_write", asyncio.CancelledError, RuntimeError, "primary"),
            ("copied_progress", asyncio.CancelledError, RuntimeError, "primary"),
        ])
    async def test_cleanup_exception_priority_after_raw_primary(
            self, tmp_path: Path, monkeypatch, primary_stage: str,
            primary_type, cleanup_type, expected: str) -> None:
        """COPY_WRITE 与 COPIED 的 raw 异常按原对象及次因顺序收场。"""
        from camctl.logging_runtime import copies
        from camctl.logging_runtime.copies import CopyReceipt

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        original_copy = channel.copy_with_write
        original_update = marker.update_stage
        original_unlink = os.unlink
        primary = primary_type(f"raw {primary_stage} primary")
        cleanup_failure = cleanup_type("cleanup raw-primary secondary")
        cleanup_calls: list[Path] = []

        def raw_copy_write_error(record, destination):
            result, copy_error = original_copy(record, destination)
            assert copy_error is None
            assert result.copy_handles_closed is True
            primary.write_result = result
            raise primary

        def fail_progress(stage, **kwargs):
            if stage is MarkerStage.COPIED:
                raise primary
            return original_update(stage, **kwargs)

        def fail_cleanup(path, *args, **kwargs):
            candidate = Path(path)
            if candidate.parent == logs_dir and is_copy_file_name(candidate.name):
                cleanup_calls.append(candidate)
                raise cleanup_failure
            return original_unlink(path, *args, **kwargs)

        if primary_stage == "copy_write":
            monkeypatch.setattr(channel, "copy_with_write", raw_copy_write_error)
        else:
            monkeypatch.setattr(marker, "update_stage", fail_progress)
        monkeypatch.setattr(os, "unlink", fail_cleanup)
        try:
            if expected == "group":
                with pytest.raises(ExceptionGroup) as caught:
                    await copies.deliver_failure_copy(
                        _request(staging, ready, channel, marker))
                assert caught.value.exceptions == (primary, cleanup_failure)
                propagated = caught.value
            elif expected == "control":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await copies.deliver_failure_copy(
                        _request(staging, ready, channel, marker))
                assert caught.value is cleanup_failure
                assert cleanup_failure.__cause__ is primary
                assert any(str(primary) in note
                           for note in cleanup_failure.__notes__)
                propagated = cleanup_failure
            elif expected == "primary":
                with pytest.raises(type(primary)) as caught:
                    await copies.deliver_failure_copy(
                        _request(staging, ready, channel, marker))
                assert caught.value is primary
                assert any("cleanup raw-primary secondary" in note
                           for note in primary.__notes__)
                propagated = primary
            else:
                receipt = await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker))
                assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
                assert receipt.file_stage.value == "copy_write"
                assert receipt.cleanup_error is not None
                assert "cleanup raw-primary secondary" in receipt.cleanup_error
                assert receipt.write_result.copy_handles_closed is True
                assert receipt.cleanup_attempted is True
                assert receipt.copy_location.value == "staging"
                propagated = None

            if propagated is not None:
                receipt = getattr(propagated, "copy_receipt", None)
                assert isinstance(receipt, CopyReceipt)
                assert receipt.file_stage.value == (
                    "copy_write" if primary_stage == "copy_write"
                    else "marker_progress_copied")
                assert receipt.cleanup_attempted is True
                assert receipt.copy_location.value == "staging"
                assert "cleanup raw-primary secondary" in receipt.cleanup_error
                if primary_stage == "copy_write":
                    assert receipt.write_result.copy_handles_closed is True
                else:
                    assert receipt.marker_create is not None
            assert len(cleanup_calls) == 1 and cleanup_calls[0].exists()
            assert list(ready.iterdir()) == []
            assert marker.check().presence is MarkerPresence.EXISTS
        finally:
            channel.close()

    async def test_published_progress_exception_preserves_moved_receipt(
            self, tmp_path: Path, monkeypatch) -> None:
        """MOVED 后 PUBLISHED 进度异常保留 ready 事实，不回删副本。"""
        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        primary = RuntimeError("injected published progress failure")
        original_update = marker.update_stage
        cleanup_calls: list[Path] = []
        original_unlink = os.unlink
        progress_failed = False

        def fail_published_progress(stage, **kwargs):
            nonlocal progress_failed
            if stage is MarkerStage.PUBLISHED:
                progress_failed = True
                raise primary
            return original_update(stage, **kwargs)

        def observe_unlink(path, *args, **kwargs):
            candidate = Path(path)
            if (progress_failed and candidate.parent == logs_dir
                    and is_copy_file_name(candidate.name)):
                cleanup_calls.append(candidate)
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(marker, "update_stage", fail_published_progress)
        monkeypatch.setattr(os, "unlink", observe_unlink)
        try:
            with pytest.raises(RuntimeError) as caught:
                await copies.deliver_failure_copy(
                    _request(staging, ready, channel, marker))

            assert caught.value is primary
            from camctl.logging_runtime.copies import CopyReceipt
            receipt = getattr(primary, "copy_receipt", None)
            assert isinstance(receipt, CopyReceipt)
            assert receipt.file_stage.value == "marker_progress_published"
            assert receipt.publish_result.stage.value == "moved"
            assert receipt.copy_location.value == "ready"
            assert receipt.cleanup_attempted is False
            assert receipt.cleanup_error is None
            assert len(list(ready.iterdir())) == 1
            assert cleanup_calls == []
        finally:
            channel.close()

    async def test_started_publish_exception_preserves_unknown_location(
            self, tmp_path: Path) -> None:
        """发布已经开始但没有可靠结果时保留 staging，不能猜测位置。"""
        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        primary = RuntimeError("injected started publish failure")
        attempted_sources: list[Path] = []

        async def failed_publish(source, _directories, _target):
            attempted_sources.append(Path(source))
            raise primary

        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker),
            publish=failed_publish,
        )

        assert receipt.outcome is CopyOutcome.COPY_UNKNOWN
        assert receipt.file_stage.value == "copy_publish"
        assert receipt.publish_result is None
        assert receipt.copy_location.value == "unknown"
        assert receipt.cleanup_attempted is False
        assert len(attempted_sources) == 1
        assert attempted_sources[0].exists()
        assert list(ready.iterdir()) == []
        channel.close()


# ---- 恢复与服务 ------------------------------------------------------

class TestRecovery:
    pytestmark = pytest.mark.asyncio
    async def test_recovery_deletes_marker_and_allows_new_round(
            self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        service = FailureLogService(marker)
        first = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.PUBLISHED
        deletion = await service.on_report_recovered()
        assert deletion.outcome.value == "deleted"
        assert marker.check().presence is MarkerPresence.ABSENT
        # 新一轮故障重新取得首次尝试：新副本身份。
        second = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert second.outcome is CopyOutcome.PUBLISHED
        assert second.round_id != first.round_id
        assert sorted(p.name for p in ready.iterdir()) == sorted(
            [first.copy_name, second.copy_name])
        channel.close()

    async def test_recovery_waits_for_original_progress_before_new_round(
            self, tmp_path: Path, monkeypatch) -> None:
        """副本更新临时文件期间，恢复和新失败在同一拥有者下排队。"""
        import json

        from camctl.logging_runtime import copies

        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        service = FailureLogService(marker)
        original_replace = os.replace
        progress_temp_ready = threading.Event()
        release_progress = threading.Event()
        paused = {"done": False}

        def pause_after_writing_progress_temp(source, target, *args, **kwargs):
            source, target = Path(source), Path(target)
            if target == logs_dir / MARKER_NAME:
                state = json.loads(source.read_bytes())
                if state["stage"] == MarkerStage.COPIED.value and not paused["done"]:
                    paused["done"] = True
                    progress_temp_ready.set()
                    if not release_progress.wait(timeout=10):
                        raise TimeoutError("test did not release marker progress")
            return original_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "replace", pause_after_writing_progress_temp)

        first_task = asyncio.create_task(service.on_report_failure(
            _request(staging, ready, channel, marker, "first report failure")))
        assert await asyncio.to_thread(progress_temp_ready.wait, 5)
        temps = [path for path in logs_dir.iterdir()
                 if is_marker_temp_name(path.name)]
        assert len(temps) == 1

        recovery_started = asyncio.Event()

        async def recover():
            recovery_started.set()
            return await service.on_report_recovered()

        recovery_task = asyncio.create_task(recover())
        await recovery_started.wait()
        assert not recovery_task.done()
        next_failure_task = asyncio.create_task(service.on_report_failure(
            _request(staging, ready, channel, marker, "next report failure")))
        release_progress.set()

        first, deletion, second = await asyncio.gather(
            first_task, recovery_task, next_failure_task)

        assert first.outcome is CopyOutcome.PUBLISHED
        assert deletion.outcome.value == "deleted"
        assert second.outcome is CopyOutcome.PUBLISHED
        assert second.round_id != first.round_id
        final = marker.check()
        assert final.state.round_id == second.round_id
        assert final.state.copy_name == second.copy_name
        assert final.state.stage is MarkerStage.PUBLISHED
        assert len(list(ready.iterdir())) == 2
        assert not [path for path in logs_dir.iterdir()
                    if is_marker_temp_name(path.name)]
        channel.close()

    async def test_suspension_lifted_by_recovery(
            self, tmp_path: Path, monkeypatch) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime import copies
        real_deliver = copies.deliver_failure_copy

        async def failing_marker(request, **kwargs):
            from camctl.logging_runtime.copies import CopyReceipt
            return CopyReceipt(CopyOutcome.MARKER_UNAVAILABLE,
                               error="stat failed")

        service = FailureLogService(marker, deliver=failing_marker)
        first = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.MARKER_UNAVAILABLE
        # 同次运行内不再尝试。
        second = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert second.outcome is CopyOutcome.SUPPRESSED
        await service.on_report_recovered()
        # 恢复解除暂停：真实交付可以再次进行。
        service_ok = FailureLogService(marker, deliver=real_deliver)
        receipt = await service_ok.on_report_failure(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.PUBLISHED
        channel.close()


# ---- 启动清理 --------------------------------------------------------

class TestStartupSweep:
    def test_sweep_cleans_old_copies_and_temps(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        old_copy = logs_dir / "log-copy-old.log"
        old_copy.write_bytes(b"half")
        temp = logs_dir / marker_temp_file_name("stale")
        temp.write_bytes(b"{")
        marker = logs_dir / MARKER_NAME
        marker.write_bytes(b'{"round_id":"r","copy_name":"log-copy-x.log",'
                           b'"stage":"copied"}')
        stranger = logs_dir / "notes.txt"
        stranger.write_bytes(b"keep")
        result = startup_sweep(logs_dir, active_names=())
        assert result.error is None and result.failures == ()
        assert not old_copy.exists() and not temp.exists()
        assert marker.exists() and stranger.exists()

    def test_sweep_deletes_only_regular_files_without_following_links(
            self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        regular = logs_dir / "log-copy-old.log"
        regular.write_bytes(b"old")
        target = tmp_path / "link-target"
        target.write_bytes(b"keep")
        link = logs_dir / "log-copy-link.log"
        link.symlink_to(target)
        directory = logs_dir / marker_temp_file_name("directory")
        directory.mkdir()

        result = startup_sweep(logs_dir)

        assert result.error is None and result.failures == ()
        assert not regular.exists()
        assert link.is_symlink() and target.read_bytes() == b"keep"
        assert directory.is_dir()

    def test_sweep_excludes_active_copy(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        active = logs_dir / "log-copy-mine.log"
        active.write_bytes(b"writing")
        result = startup_sweep(logs_dir, active_names=("log-copy-mine.log",))
        assert result.error is None
        assert active.exists()

    def test_sweep_reports_directory_error(
            self, tmp_path: Path, monkeypatch) -> None:
        from camctl.logging_runtime import copies

        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        original_listdir = os.listdir

        def deny_listing(path):
            if Path(path) == logs_dir:
                raise PermissionError("logs directory denied")
            return original_listdir(path)

        monkeypatch.setattr(os, "listdir", deny_listing)
        result = startup_sweep(logs_dir)
        assert result.error is not None

    def test_sweep_treats_missing_directory_as_completed_absence(
            self, tmp_path: Path) -> None:
        missing = tmp_path / "staging" / "logs"
        result = startup_sweep(missing)
        assert result.error is None
        assert result.failures == ()
