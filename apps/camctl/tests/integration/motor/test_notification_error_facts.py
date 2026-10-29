"""公开通知端口在真实管道上保存原系统异常和已知写入范围。"""
import os
from unittest.mock import Mock

import pytest

from camctl.motor import notification


def test_failed_write_returns_original_exception():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    failures = []
    def write(fd, message):
        try:
            return os.write(fd, message)
        except BrokenPipeError as error:
            failures.append(error)
            raise
    writer = notification.NotificationWriter(write_fd,
        pipe_buf=os.fpathconf(write_fd, 'PC_PIPE_BUF'), write=write)
    try:
        result = writer.send(b'notification\n')
        assert result.kind is notification.WriteKind.FAILED
        assert len(failures) == 1 and result.exception is failures[0]
        assert result.errno == 32
    finally:
        writer.close()


def test_open_failure_retains_original_exception(monkeypatch):
    read_fd, write_fd = os.pipe()
    failure = OSError(9, 'fd configuration failed')
    monkeypatch.setattr(notification.os, 'set_inheritable', Mock(side_effect=failure))
    try:
        writer = notification.open_notification_writer(write_fd)
        assert writer.unavailability.exception is failure
        assert writer.unavailability.errno == 9
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)


def test_partial_write_close_failure_keeps_original_error_and_known_range():
    read_fd, write_fd = os.pipe()
    failure = OSError(5, 'close failed')
    def short_write(fd, message):
        return os.write(fd, message[:3])
    def close(fd):
        os.close(fd)
        raise failure
    writer = notification.NotificationWriter(write_fd,
        pipe_buf=os.fpathconf(write_fd, 'PC_PIPE_BUF'), write=short_write, close=close)
    try:
        with pytest.raises(OSError) as caught:
            writer.send(b'notification\n')
        assert caught.value is failure
        assert not writer.available
        assert os.read(read_fd, 20) == b'not'
        assert os.read(read_fd, 1) == b''
        assert any('written_bytes=3' in note and 'message_bytes=13' in note for note in failure.__notes__)
    finally:
        writer.close()
        os.close(read_fd)
