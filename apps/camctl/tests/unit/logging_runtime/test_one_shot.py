"""一次性命令的日志收场保持原业务结果和控制异常。"""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.logging_runtime import one_shot
from camctl.logging_runtime.lifecycle import CloseResult
from camctl.logging_runtime.service import LogChannel
from camctl.logging_runtime.models import LogLevel, LogWriteResult
from camctl.logging_runtime.service import LogRecord
from camctl.logging_runtime.standard_logging import StandardLoggingInstallation


@pytest.fixture
def world(monkeypatch):
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    factory = Mock(return_value=channel)
    monkeypatch.setattr(one_shot, "LogChannel", factory)
    installation = create_autospec(StandardLoggingInstallation, instance=True, spec_set=True)
    monkeypatch.setattr(one_shot, "install_standard_logging", Mock(return_value=installation))
    close = AsyncMock(return_value=CloseResult(False, 0))
    monkeypatch.setattr(one_shot, "close_logging", close)
    config = SimpleNamespace(level="INFO", queue_capacity=100, queue_low_watermark=50,
        queue_high_watermark=75, info_sample_probability=0.1)
    diagnostics = []
    return config, channel, factory, close, diagnostics


def test_ordinary_close_failure_preserves_business_result(world):
    config, channel, factory, close, diagnostics = world
    original = OSError("close failed")
    close.side_effect = original
    values = []
    with one_shot.stderr_logging(config, StringIO(), command_name="describe",
            on_error=lambda stage, error: diagnostics.append(error)):
        values.append(7)
    assert values == [7] and diagnostics == [original]
    close.assert_awaited_once()


def test_stderr_writer_reports_only_stderr_fact_once(world):
    config, channel, factory, close, diagnostics = world
    stream = StringIO()
    with one_shot.stderr_logging(config, stream, command_name="describe", on_error=Mock()):
        writer = factory.call_args.kwargs["writer"]
        result = writer(LogRecord(LogLevel.ERROR, "stderr-only"))
    assert result == LogWriteResult(appended=False, durable=False, fallback_written=True)
    assert result.delivered_for(LogLevel.ERROR) is False
    assert stream.getvalue().count("stderr-only") == 1
    close.assert_awaited_once()


@pytest.mark.parametrize("failure_stage", ["start", "standard_logging"])
def test_logging_start_failure_keeps_body_and_closes_constructed_runtime(world, failure_stage):
    config, channel, factory, close, diagnostics = world
    original = OSError("logging startup failed")
    if failure_stage == "start":
        channel.start.side_effect = original
    else:
        one_shot.install_standard_logging.side_effect = original
    values = []
    with one_shot.stderr_logging(config, StringIO(), command_name="describe",
            on_error=lambda stage, error: diagnostics.append(error)):
        values.append(7)
    assert values == [7] and diagnostics == [original]
    close.assert_awaited_once()


def test_start_control_still_closes_constructed_runtime(world):
    config, channel, factory, close, diagnostics = world
    original = asyncio.CancelledError("start cancelled")
    channel.start.side_effect = original
    with pytest.raises(asyncio.CancelledError) as caught:
        with one_shot.stderr_logging(config, StringIO(), command_name="describe", on_error=Mock()):
            pytest.fail("启动控制异常后不能进入执行体")
    assert caught.value is original
    close.assert_awaited_once()


@pytest.mark.parametrize("original", [asyncio.CancelledError("body cancelled"),
    KeyboardInterrupt("body interrupted"), BaseExceptionGroup("body controls", [asyncio.CancelledError("child")])])
def test_body_control_keeps_identity_and_secondary_close_control(world, original):
    config, channel, factory, close, diagnostics = world
    secondary = asyncio.CancelledError("close cancelled")
    close.side_effect = secondary
    with pytest.raises(type(original)) as caught:
        with one_shot.stderr_logging(config, StringIO(), command_name="init", on_error=Mock()):
            raise original
    assert caught.value is original
    assert any("close cancelled" in note for note in original.__notes__)


def test_close_control_preserves_prior_ordinary_business_exception(world):
    config, channel, factory, close, diagnostics = world
    original, control = ValueError("business failed"), asyncio.CancelledError("close cancelled")
    close.side_effect = control
    with pytest.raises(asyncio.CancelledError) as caught:
        with one_shot.stderr_logging(config, StringIO(), command_name="init", on_error=Mock()):
            raise original
    assert caught.value is control
    assert any("business failed" in note for note in control.__notes__)


def test_writer_control_returns_to_caller_after_log_shutdown(world):
    config, channel, factory, close, diagnostics = world
    original = asyncio.CancelledError("stderr cancelled")

    class Stream:
        def write(self, value):
            raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        with one_shot.stderr_logging(config, Stream(), command_name="init", on_error=Mock()):
            assert not factory.call_args.kwargs["writer"].write("message")
    assert caught.value is original
    close.assert_awaited_once()


def test_stderr_write_failure_stops_further_writes_without_business_failure(world):
    config, channel, factory, close, diagnostics = world
    stream = Mock()
    stream.write.side_effect = OSError("stderr failed")
    on_error = Mock()
    with one_shot.stderr_logging(config, stream, command_name="init", on_error=on_error):
        writer = factory.call_args.kwargs["writer"]
        assert not writer.write("first")
        assert not writer.write("second")
    stream.write.assert_called_once()
    on_error.assert_not_called()
