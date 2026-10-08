"""发送边界只允许一次完整写入，不把不完整效果改写为成功。"""
import errno
from decimal import Decimal
from unittest.mock import Mock

import pytest

from camctl.contracts.values import ObjectId
from camctl.motor.notification import (
    NotificationWriter, WriteKind, encode_motor_notification,
)


@pytest.mark.parametrize("position,encoded", [
    (-2147483648, b"-2147483648"), (2147483647, b"2147483647"),
    (0, b"0"), (Decimal("1.0"), b"1"), (Decimal("1e2"), b"100"),
])
def test_encode_exact_position(position, encoded):
    assert encode_motor_notification(ObjectId(12), position) == (
        b'{"type":"motor_control","action_instance_id":"12",'
        b'"params":{"position":' + encoded + b'}}\n')


@pytest.mark.parametrize("position", [True, None, "1", 1.5,
    Decimal("1.0000000000000000001"), -2147483649, 2147483648])
def test_encode_rejects_nonrepresentable_position(position):
    with pytest.raises(ValueError):
        encode_motor_notification(ObjectId(12), position)


def make_writer(**kwargs):
    write = Mock(spec=lambda fd, data: None)
    close = Mock(spec=lambda fd: None)
    writer = NotificationWriter(9, pipe_buf=4096, write=write, close=close)
    return writer, write, close


def test_complete_write_means_local_success():
    writer, write, _ = make_writer()
    write.return_value = 4
    result = writer.send(b"abc\n")
    assert result.kind is WriteKind.WRITTEN
    assert result.written_bytes == 4
    write.assert_called_once_with(9, b"abc\n")


@pytest.mark.parametrize("number,reason", [
    (errno.EAGAIN, "would_block"), (errno.EPIPE, "broken_pipe"),
    (errno.EINTR, "os_error"), (errno.EIO, "os_error"),
])
def test_os_failure_is_not_retried(number, reason):
    writer, write, _ = make_writer()
    write.side_effect = OSError(number, "write failed")
    result = writer.send(b"abc\n")
    assert result.kind is WriteKind.FAILED
    assert result.reason == reason
    assert result.errno == number
    assert result.written_bytes == 0
    write.assert_called_once()


def test_short_write_disables_channel_before_next_message():
    writer, write, close = make_writer()
    write.return_value = 2
    result = writer.send(b"abc\n")
    assert result.kind is WriteKind.PARTIAL
    assert result.reason == "short_write"
    assert result.written_bytes == 2
    assert not writer.available
    next_result = writer.send(b"def\n")
    assert next_result.kind is WriteKind.UNAVAILABLE
    assert next_result.reason == "disabled_after_partial_write"
    write.assert_called_once()
    close.assert_called_once_with(9)
    writer.close()
    close.assert_called_once()


def test_message_exceeding_atomic_limit_never_writes():
    writer, write, _ = make_writer()
    result = writer.send(b"a" * 4096 + b"\n")
    assert result.kind is WriteKind.FAILED
    assert result.reason == "message_too_large"
    assert result.written_bytes == 0
    write.assert_not_called()


def test_disconnected_writer_cannot_send():
    writer = NotificationWriter(None)
    result = writer.send(b"abc\n")
    assert result.kind is WriteKind.UNAVAILABLE
    assert result.reason == "not_connected"


def test_close_releases_owned_descriptor_once():
    writer, write, close = make_writer()
    writer.close()
    writer.close()
    assert not writer.available
    assert writer.send(b"abc\n").kind is WriteKind.UNAVAILABLE
    write.assert_not_called()
    close.assert_called_once_with(9)
