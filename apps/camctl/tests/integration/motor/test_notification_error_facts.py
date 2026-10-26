"""通知端口把原系统错误交回协程，并保存已知写入范围。"""
from unittest.mock import Mock

import pytest

from camctl.motor import notification


def test_failed_write_returns_original_exception():
    failure = BrokenPipeError(32, 'host stopped reading')
    writer = notification.NotificationWriter(9, pipe_buf=512, write=Mock(side_effect=failure))
    result = writer.send(b'notification\n')
    assert result.kind is notification.WriteKind.FAILED
    assert result.exception is failure


def test_open_failure_retains_original_exception(monkeypatch):
    failure = OSError(9, 'fd configuration failed')
    monkeypatch.setattr(notification.os, 'set_inheritable', Mock(side_effect=failure))
    monkeypatch.setattr(notification.os, 'close', Mock())
    writer = notification.open_notification_writer(9)
    assert writer.unavailability.exception is failure
    assert writer.unavailability.errno == 9


def test_partial_write_close_failure_keeps_original_error_and_known_range():
    failure = OSError(5, 'close failed')
    writer = notification.NotificationWriter(9, pipe_buf=512,
        write=Mock(return_value=3), close=Mock(side_effect=failure))
    with pytest.raises(OSError) as caught:
        writer.send(b'notification\n')
    assert caught.value is failure
    assert not writer.available
    assert any('written_bytes=3' in note and 'message_bytes=13' in note for note in failure.__notes__)
