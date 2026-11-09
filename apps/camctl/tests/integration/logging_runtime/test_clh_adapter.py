"""CLH 文件通道的故障分类、锁与资源收场。"""

import json
import logging
import os
from pathlib import Path

import concurrent_log_handler as clh
import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord


def _lock_path(log_path: Path) -> Path:
    directory, name = clh.ConcurrentRotatingFileHandler.baseLockFilename(str(log_path))
    return Path(directory) / name


def _is_lock_file(stream, path: Path) -> bool:
    opened = os.fstat(stream.fileno())
    named = path.stat()
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


@pytest.mark.parametrize("close_fails", [False, True])
def test_copy_preserves_unlock_and_handler_close_errors(
        tmp_path: Path, monkeypatch, close_fails: bool) -> None:
    log_path = tmp_path / "camctl.log"
    lock_path = _lock_path(log_path)
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_unlock = clh.unlock
    real_close = clh.ConcurrentRotatingFileHandler.close
    events = []

    def unlock(stream):
        result = real_unlock(stream)
        if _is_lock_file(stream, lock_path):
            events.append("unlock")
            raise OSError("unlock lost")
        return result

    def close(handler):
        if Path(handler.baseFilename) != log_path:
            return real_close(handler)
        events.append("close")
        result = real_close(handler)
        if close_fails:
            raise OSError("close not confirmed")
        return result

    monkeypatch.setattr(clh, "unlock", unlock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close)
    try:
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="trigger"), destination)
        assert copy_error is None
        assert json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])["message"] == "trigger"
        assert write_result.appended is True
        assert write_result.copy_handles_closed is True
        assert "unlock lost" in write_result.coordination_error
        if close_fails:
            assert "close not confirmed" in write_result.close_error
        else:
            assert write_result.close_error is None
        assert events == ["unlock", "close"]
        assert channel.disabled is True
        assert channel.closed is (not close_fails)
    finally:
        channel.close()
    assert events == ["unlock", "close"]


def test_rotation_failure_retains_real_copy_success(
        tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=16, file_count=3)
    assert channel.write_record(LogRecord(level=LogLevel.INFO, message="before")).appended
    real_link = os.link

    def fail_rotation(source, target, *args, **kwargs):
        if Path(source) == log_path and Path(target) == Path(f"{log_path}.1"):
            raise OSError("rotation denied")
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail_rotation)
    try:
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="trigger"), destination)
        assert write_result.appended is True
        assert write_result.copy_handles_closed is True
        assert write_result.rotation_error is not None
        assert "rotation denied" in write_result.rotation_error
        assert copy_error is None
        assert json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])["message"] == "trigger"
        assert channel.disabled is False
        assert channel.write_record(LogRecord(
            level=LogLevel.ERROR, message="after rotation failure")).appended
        assert channel.disabled is False
    finally:
        channel.close()


@pytest.mark.parametrize("close_fails", [False, True])
def test_lock_failure_disables_channel_and_preserves_close_result(
        tmp_path: Path, monkeypatch, close_fails: bool) -> None:
    log_path = tmp_path / "camctl.log"
    lock_path = _lock_path(log_path)
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_lock = clh.lock
    real_close = clh.ConcurrentRotatingFileHandler.close
    lock_calls = 0
    close_calls = 0

    def fail_lock(stream, flags):
        nonlocal lock_calls
        if _is_lock_file(stream, lock_path):
            lock_calls += 1
            raise OSError("lock acquisition denied")
        return real_lock(stream, flags)

    def close(handler):
        nonlocal close_calls
        if Path(handler.baseFilename) != log_path:
            return real_close(handler)
        close_calls += 1
        result = real_close(handler)
        if close_fails:
            raise OSError("lock cleanup close denied")
        return result

    monkeypatch.setattr(clh, "lock", fail_lock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close)
    try:
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="trigger"), destination)
        assert copy_error is None
        assert write_result.appended is False
        assert write_result.copy_handles_closed is True
        assert write_result.rotated is False
        assert write_result.append_error is None
        # CLH 重试实际锁请求后报告 RuntimeError，而不是原 OSError。
        assert "RuntimeError" in write_result.coordination_error
        assert "Cannot acquire lock" in write_result.coordination_error
        if close_fails:
            assert "lock cleanup close denied" in write_result.close_error
        else:
            assert write_result.close_error is None
        assert not destination.exists()
        assert channel.disabled is True
        assert channel.closed is (not close_fails)
        assert close_calls == 1 and lock_calls > 1
        earlier_lock_calls = lock_calls
        later_write = channel.write_record(LogRecord(level=LogLevel.ERROR, message="later"))
        later_copy, later_copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="later"), destination)
        assert later_write.appended is False
        assert later_copy.appended is False and later_copy_error is None
        assert lock_calls == earlier_lock_calls and close_calls == 1
        assert not destination.exists()
    finally:
        channel.close()


def test_copy_format_failure_does_not_disable_file_channel(
        tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "camctl.log"
    lock_path = _lock_path(log_path)
    destination = tmp_path / "staging" / "logs" / "log-copy-format.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_format, real_lock = logging.Formatter.format, clh.lock
    lock_calls = []

    def fail_format(formatter, record):
        if "trigger" in str(record.msg):
            raise ValueError("permission denied while formatting")
        return real_format(formatter, record)

    def count_lock(stream, flags):
        if _is_lock_file(stream, lock_path):
            lock_calls.append(True)
        return real_lock(stream, flags)

    monkeypatch.setattr(logging.Formatter, "format", fail_format)
    monkeypatch.setattr(clh, "lock", count_lock)
    try:
        failed, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="trigger"), destination)
        assert failed.appended is False and failed.copy_handles_closed is True
        assert failed.rotated is False
        assert "permission denied" in failed.append_error
        assert failed.failure_stage.value == "format"
        assert copy_error is None and lock_calls == []
        assert not destination.exists() and channel.disabled is False
        succeeded, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="later"), destination)
        assert succeeded.appended is True and succeeded.copy_handles_closed is True
        assert copy_error is None and len(lock_calls) == 1
        assert json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])["message"] == "later"
        assert channel.disabled is False
    finally:
        channel.close()


class _FaultingStream:
    def __init__(self, stream, failure_step, attempts):
        self.stream = stream
        self.failure_step = failure_step
        self.attempts = attempts

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, message):
        self.attempts["append"] += 1
        if self.failure_step == "append":
            raise OSError("permission denied appending current log")
        return self.stream.write(message)

    def flush(self):
        self.attempts["flush"] += 1
        if self.failure_step == "flush" and self.attempts["flush"] == 1:
            raise OSError("permission denied flushing current log")
        return self.stream.flush()

    def close(self):
        return self.stream.close()


@pytest.mark.parametrize("failure_step", ["open", "append", "flush"])
def test_current_log_io_failure_disables_after_unlock(
        tmp_path: Path, monkeypatch, failure_step: str) -> None:
    log_path = tmp_path / "camctl.log"
    lock_path = _lock_path(log_path)
    destination = tmp_path / "staging" / "logs" / "log-copy-io.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_open = clh.ConcurrentRotatingFileHandler.do_open
    real_unlock = clh.unlock
    real_close = clh.ConcurrentRotatingFileHandler.close
    events = []
    attempts = {"open": 0, "append": 0, "flush": 0}

    def open_file(handler, *args, **kwargs):
        if Path(handler.baseFilename) != log_path:
            return real_open(handler, *args, **kwargs)
        attempts["open"] += 1
        if failure_step == "open":
            raise OSError("permission denied opening current log")
        return _FaultingStream(real_open(handler, *args, **kwargs), failure_step, attempts)

    def unlock(stream):
        result = real_unlock(stream)
        if _is_lock_file(stream, lock_path):
            events.append("unlock")
        return result

    def close(handler):
        if Path(handler.baseFilename) == log_path:
            events.append("close")
        return real_close(handler)

    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", open_file)
    monkeypatch.setattr(clh, "unlock", unlock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close)
    try:
        result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message="trigger"), destination)
        assert result.appended is False and result.copy_handles_closed is True
        assert result.append_error is not None and result.failure_stage.value == "file_io"
        assert result.coordination_error is None and copy_error is None
        assert not destination.exists()
        assert channel.disabled is True and channel.closed is True
        assert attempts[failure_step] == 1
        assert events == ["unlock", "close"]
    finally:
        channel.close()
    assert events == ["unlock", "close"]


@pytest.mark.parametrize("failure_step", ["open", "append", "flush"])
def test_ordinary_file_io_failure_is_typed_and_disables_without_retry(
        tmp_path: Path, monkeypatch, failure_step: str) -> None:
    log_path = tmp_path / "camctl.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_open = clh.ConcurrentRotatingFileHandler.do_open
    attempts = {"open": 0, "append": 0, "flush": 0}

    def open_file(handler, *args, **kwargs):
        if Path(handler.baseFilename) != log_path:
            return real_open(handler, *args, **kwargs)
        attempts["open"] += 1
        if failure_step == "open":
            raise OSError("ordinary open denied")
        return _FaultingStream(real_open(handler, *args, **kwargs), failure_step, attempts)

    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", open_file)
    try:
        failed = channel.write_record(LogRecord(level=LogLevel.ERROR, message="ordinary trigger"))
        assert failed.appended is False and failed.failure_stage.value == "file_io"
        assert failed.append_error is not None
        assert channel.disabled is True and channel.closed is True
        if failure_step == "open":
            assert attempts["open"] == 1 and attempts["append"] == 0
        else:
            assert attempts[failure_step] == 1
        opens_before_later = attempts["open"]
        assert channel.write_record(LogRecord(level=LogLevel.ERROR, message="later")).appended is False
        assert attempts["open"] == opens_before_later
    finally:
        channel.close()


def test_ordinary_format_failure_keeps_file_channel_available(
        tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "camctl.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    real_format = logging.Formatter.format

    def fail_format(formatter, record):
        if "format trigger" in str(record.msg):
            raise ValueError("permission denied while formatting")
        return real_format(formatter, record)

    monkeypatch.setattr(logging.Formatter, "format", fail_format)
    try:
        failed = channel.write_record(LogRecord(level=LogLevel.ERROR, message="format trigger"))
        assert failed.appended is False and failed.failure_stage.value == "format"
        assert "permission denied" in failed.append_error
        assert channel.disabled is False
        assert channel.write_record(LogRecord(level=LogLevel.ERROR, message="later ordinary record")).appended
        assert channel.disabled is False
    finally:
        channel.close()
