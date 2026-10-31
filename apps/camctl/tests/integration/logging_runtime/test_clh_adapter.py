"""CLH 协调释放错误不得覆盖已经完成的日志副本事实。"""

import json
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord


@pytest.mark.parametrize("close_fails", [False, True])
def test_copy_preserves_unlock_and_handler_close_errors(
        tmp_path: Path, monkeypatch, close_fails: bool) -> None:
    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    close_calls = 0
    original_close = handler.close

    monkeypatch.setattr(handler, "_do_lock", lambda: None)
    monkeypatch.setattr(
        handler, "_do_unlock",
        lambda: (_ for _ in ()).throw(OSError("unlock lost")))

    def close_with_optional_error():
        nonlocal close_calls
        close_calls += 1
        original_close()
        if close_fails and close_calls == 1:
            raise OSError("close not confirmed")

    monkeypatch.setattr(handler, "close", close_with_optional_error)

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
    # 故障位于复制之后；已写出的副本仍是实际文件，不应重新复制。
    assert close_calls == 1
    assert channel.disabled is True
    assert channel.closed is (not close_fails)
    channel.close()


def test_rotation_failure_retains_real_copy_success(
        tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=16, file_count=3)
    handler = channel._handler

    monkeypatch.setattr(handler, "shouldRollover", lambda record: True)
    monkeypatch.setattr(
        handler, "doRollover",
        lambda: (_ for _ in ()).throw(OSError("rotation denied")))

    write_result, copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="trigger"), destination)

    assert write_result.appended is True
    assert write_result.copy_handles_closed is True
    assert write_result.rotation_error is not None
    assert "rotation denied" in write_result.rotation_error
    assert copy_error is None
    assert json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])["message"] == "trigger"
    assert channel.disabled is False
    later = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="after rotation failure"))
    assert later.appended is True
    assert channel.disabled is False
    channel.close()


@pytest.mark.parametrize("close_fails", [False, True])
def test_lock_failure_disables_channel_and_preserves_close_result(
        tmp_path: Path, monkeypatch, close_fails: bool) -> None:
    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-r1.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    lock_calls = 0
    close_calls = 0
    original_close = handler.close

    def fail_lock():
        nonlocal lock_calls
        lock_calls += 1
        raise OSError("lock acquisition denied")

    def close_with_optional_error():
        nonlocal close_calls
        close_calls += 1
        original_close()
        if close_fails:
            raise OSError("lock cleanup close denied")

    monkeypatch.setattr(handler, "_do_lock", fail_lock)
    monkeypatch.setattr(handler, "close", close_with_optional_error)

    write_result, copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="trigger"), destination)

    assert copy_error is None
    assert write_result.appended is False
    assert write_result.copy_handles_closed is True
    assert write_result.rotated is False
    assert write_result.append_error is None
    assert "lock acquisition denied" in write_result.coordination_error
    assert destination.exists() is False
    assert channel.disabled is True
    assert channel.closed is (not close_fails)
    assert close_calls == 1

    later_write = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="later"))
    later_copy, later_copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="later"), destination)
    channel.close()

    assert later_write.appended is False
    assert later_copy.appended is False
    assert later_copy_error is None
    assert lock_calls == 1
    assert close_calls == 1
    assert destination.exists() is False


def test_copy_format_failure_does_not_disable_file_channel(
        tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-format.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    original_format = handler.format
    lock_calls = 0

    def fail_format(record):
        raise ValueError("permission denied while formatting")

    def count_lock():
        nonlocal lock_calls
        lock_calls += 1

    monkeypatch.setattr(handler, "format", fail_format)
    monkeypatch.setattr(handler, "_do_lock", count_lock)

    failed, copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="trigger"), destination)

    assert failed.appended is False
    assert failed.copy_handles_closed is True
    assert failed.rotated is False
    assert failed.append_error is not None
    assert "permission denied" in failed.append_error
    assert getattr(failed, "failure_stage", None).value == "format"
    assert copy_error is None
    assert lock_calls == 0
    assert destination.exists() is False
    assert channel.disabled is False

    monkeypatch.setattr(handler, "format", original_format)
    monkeypatch.setattr(handler, "_do_lock", lambda: None)
    monkeypatch.setattr(handler, "_do_unlock", lambda: None)
    succeeded, copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="later"), destination)
    assert succeeded.appended is True
    assert succeeded.copy_handles_closed is True
    assert copy_error is None
    assert json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])["message"] == "later"
    assert channel.disabled is False
    channel.close()


@pytest.mark.parametrize("failure_step", ["open", "append", "flush"])
def test_current_log_io_failure_disables_after_unlock(
        tmp_path: Path, monkeypatch, failure_step: str) -> None:
    import concurrent_log_handler

    log_path = tmp_path / "camctl.log"
    destination = tmp_path / "staging" / "logs" / "log-copy-io.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    events: list[str] = []
    original_lock = handler._do_lock
    original_unlock = handler._do_unlock
    original_clh_open = concurrent_log_handler._open
    original_write = handler.do_write
    original_flush = handler.flush
    original_close = handler.close

    def tracked_lock():
        events.append("lock")
        return original_lock()

    def tracked_unlock():
        events.append("unlock")
        return original_unlock()

    def tracked_open(file, *args, **kwargs):
        is_current_log = str(file) == handler.baseFilename
        if is_current_log:
            events.append("open")
        if is_current_log and failure_step == "open":
            raise OSError("permission denied opening current log")
        return original_clh_open(file, *args, **kwargs)

    def tracked_write(message):
        events.append("append")
        if failure_step == "append":
            raise OSError("permission denied appending current log")
        return original_write(message)

    flush_calls = 0

    def tracked_flush():
        nonlocal flush_calls
        flush_calls += 1
        events.append("flush")
        if failure_step == "flush" and flush_calls == 1:
            raise OSError("permission denied flushing current log")
        return original_flush()

    def tracked_close():
        events.append("close")
        return original_close()

    monkeypatch.setattr(handler, "_do_lock", tracked_lock)
    monkeypatch.setattr(handler, "_do_unlock", tracked_unlock)
    if failure_step == "open":
        monkeypatch.setattr(concurrent_log_handler, "_open", tracked_open)
    monkeypatch.setattr(handler, "do_write", tracked_write)
    monkeypatch.setattr(handler, "flush", tracked_flush)
    monkeypatch.setattr(handler, "close", tracked_close)

    result, copy_error = channel.copy_with_write(
        LogRecord(level=LogLevel.ERROR, message="trigger"), destination)

    assert result.appended is False
    assert result.copy_handles_closed is True
    assert result.append_error is not None
    assert getattr(result, "failure_stage", None).value == "file_io"
    assert result.coordination_error is None
    assert copy_error is None
    assert destination.exists() is False
    assert channel.disabled is True
    assert channel.closed is True
    assert events.index("unlock") < events.index("close")
    assert events.count("close") == 1
    assert events.count("unlock") == 1
    channel.close()
    assert events.count("close") == 1


@pytest.mark.parametrize("failure_step", ["open", "append", "flush"])
def test_ordinary_file_io_failure_is_typed_and_disables_without_retry(
        tmp_path: Path, monkeypatch, failure_step: str) -> None:
    import concurrent_log_handler

    log_path = tmp_path / "camctl.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    original_open = concurrent_log_handler._open
    attempts = {"open": 0, "append": 0, "flush": 0}

    class FaultingStream:
        def __init__(self, stream):
            self._stream = stream

        def __getattr__(self, name):
            return getattr(self._stream, name)

        def write(self, message):
            attempts["append"] += 1
            if failure_step == "append":
                raise OSError("ordinary append denied")
            return self._stream.write(message)

        def flush(self):
            attempts["flush"] += 1
            if failure_step == "flush":
                raise OSError("ordinary flush denied")
            return self._stream.flush()

        def close(self):
            return self._stream.close()

    def controlled_open(file, *args, **kwargs):
        if str(file) != handler.baseFilename:
            return original_open(file, *args, **kwargs)
        attempts["open"] += 1
        if failure_step == "open":
            raise OSError("ordinary open denied")
        return FaultingStream(original_open(file, *args, **kwargs))

    monkeypatch.setattr(concurrent_log_handler, "_open", controlled_open)
    failed = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="ordinary trigger"))

    assert failed.appended is False
    assert failed.failure_stage.value == "file_io"
    assert failed.append_error is not None
    assert channel.disabled is True
    assert channel.closed is True
    if failure_step == "open":
        # CLH 先在轮换尺寸检查中打开失败；该结果阻止 emit 随后再开文件。
        assert attempts["open"] == 1
        assert attempts["append"] == 0
    else:
        assert attempts[failure_step] == 1
    opens_before_later = attempts["open"]
    later = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="later"))
    assert later.appended is False
    assert attempts["open"] == opens_before_later
    channel.close()


def test_ordinary_format_failure_keeps_file_channel_available(
        tmp_path: Path, monkeypatch) -> None:
    import logging

    log_path = tmp_path / "camctl.log"
    channel = FileChannel(log_path, max_bytes=1024, file_count=3)
    handler = channel._handler
    handler.setFormatter(logging.Formatter("%(message)s"))
    style = handler.formatter._style
    original_format = style.format

    def fail_format(record):
        raise ValueError("permission denied while formatting")

    monkeypatch.setattr(style, "format", fail_format)
    failed = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="format trigger"))

    assert failed.appended is False
    assert failed.failure_stage.value == "format"
    assert "permission denied" in failed.append_error
    assert channel.disabled is False

    monkeypatch.setattr(style, "format", original_format)
    later = channel.write_record(
        LogRecord(level=LogLevel.ERROR, message="later ordinary record"))
    assert later.appended is True
    assert channel.disabled is False
    channel.close()
