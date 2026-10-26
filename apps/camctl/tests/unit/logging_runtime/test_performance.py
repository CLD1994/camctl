"""性能计时使用单调钟与现有日志队列，不改变业务返回或异常。"""

import json
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime import performance
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel


def _payload(channel):
    record = channel.slog.call_args.args[0]
    assert record.level is LogLevel.DEBUG
    return json.loads(record.message.removeprefix("性能计时 "))


def test_elapsed_time_and_original_action_are_delivered_without_file_access(monkeypatch):
    ticks = iter((5_000_000, 8_500_000))
    monkeypatch.setattr(performance, "monotonic_ns", lambda: next(ticks))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with performance.performance_logging(channel):
        with performance.measure_performance("result_page_save", action_id=5, page_no=2):
            pass

    assert _payload(channel) == {
        "stage": "result_page_save", "elapsed_ms": 3.5,
        "action_id": 5, "page_no": 2,
    }
    channel.alog.assert_not_called()


def test_nested_logging_context_restores_previous_channel():
    outer = create_autospec(LogChannel, instance=True, spec_set=True)
    inner = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance.performance_logging(outer):
        with performance.performance_logging(inner):
            performance.record_performance("directory_read", 1, count=10)
        performance.record_performance("metadata_read", 2, count=3)
    performance.record_performance("outside", 3)
    assert outer.slog.call_count == inner.slog.call_count == 1
    assert _payload(inner)["stage"] == "directory_read"
    assert _payload(outer)["stage"] == "metadata_read"


def test_failed_operation_is_timed_without_changing_its_exception(monkeypatch):
    ticks = iter((10, 20))
    monkeypatch.setattr(performance, "monotonic_ns", lambda: next(ticks))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    original = ValueError("original failure")
    with performance.performance_logging(channel):
        with pytest.raises(ValueError) as caught:
            with performance.measure_performance("segment_save", action_id=3):
                raise original
    assert caught.value is original
    assert _payload(channel)["stage"] == "segment_save"


def test_diagnostic_delivery_failure_does_not_change_business_result():
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    channel.slog.side_effect = RuntimeError("logging unavailable")
    with performance.performance_logging(channel):
        with performance.measure_performance("metadata_read"):
            result = 7
    assert result == 7
