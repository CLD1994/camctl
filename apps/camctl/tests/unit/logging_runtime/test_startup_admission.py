"""启动记录经过公共接纳入口；测试不启动日志线程。"""

from unittest.mock import Mock

import pytest

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord


@pytest.mark.parametrize("threshold", [LogLevel.WARNING, LogLevel.ERROR])
def test_startup_bypasses_level_filter_but_ordinary_info_does_not(threshold):
    fallback = Mock(return_value=True)
    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=2,
                         level=threshold, writer=Mock(), fallback=fallback)
    ordinary = channel.submit(LogRecord(LogLevel.INFO, "ordinary"))
    startup = channel.submit(LogRecord(LogLevel.INFO, "startup", startup=True))
    assert ordinary.admission_result.decision.kind is AdmissionKind.FILTERED_OUT
    assert startup.admission_result.decision.kind is AdmissionKind.ACCEPTED
    fallback.assert_not_called()


def test_startup_bypasses_sampling_and_high_watermark_then_falls_back_when_full():
    random = Mock()
    random.random.side_effect = AssertionError("启动记录不能采样")
    fallback = Mock(return_value=True)
    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=2,
                         level=LogLevel.INFO, sample_probability=0,
                         writer=Mock(), random_source=random, fallback=fallback)
    for index in range(4):
        receipt = channel.submit(LogRecord(LogLevel.INFO, str(index), startup=True))
        assert receipt.admission_result.decision.kind is AdmissionKind.ACCEPTED
    receipt = channel.submit(LogRecord(LogLevel.INFO, "full", startup=True))
    assert receipt.admission_result.decision.kind is AdmissionKind.FALLBACK_STDERR
    assert receipt.write_result.fallback_written is True
    assert fallback.call_count == 1 and "full" in fallback.call_args.args[0]
    random.random.assert_not_called()


def test_ordinary_info_still_samples_and_drops_at_high_watermark():
    random = Mock()
    random.random.return_value = 0.5
    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=2,
                         level=LogLevel.INFO, sample_probability=0,
                         writer=Mock(), random_source=random)
    assert channel.submit(LogRecord(LogLevel.WARNING, "first")).admission_result.decision.kind is AdmissionKind.ACCEPTED
    assert channel.submit(LogRecord(LogLevel.INFO, "sampled")).admission_result.decision.kind is AdmissionKind.SAMPLED_OUT
    assert channel.submit(LogRecord(LogLevel.WARNING, "second")).admission_result.decision.kind is AdmissionKind.ACCEPTED
    assert channel.submit(LogRecord(LogLevel.INFO, "high")).admission_result.decision.kind is AdmissionKind.DROPPED
    random.random.assert_called_once()


def test_startup_falls_back_after_channel_failure():
    fallback = Mock(return_value=True)
    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=2,
                         level=LogLevel.WARNING, writer=Mock(), fallback=fallback)
    channel.disable_channel(ChannelError("writer unavailable"))
    receipt = channel.submit(LogRecord(LogLevel.INFO, "startup", startup=True))
    assert receipt.admission_result.decision.kind is AdmissionKind.CHANNEL_FAILED
    assert receipt.write_result.fallback_written is True
    assert fallback.call_count == 1 and "startup" in fallback.call_args.args[0]


def test_queued_startup_is_delivered_once_when_channel_fails():
    fallback = Mock(return_value=True)
    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=2,
                         level=LogLevel.WARNING, writer=Mock(), fallback=fallback)
    receipt = channel.submit(LogRecord(LogLevel.INFO, "queued", startup=True))
    channel.disable_channel(ChannelError("writer unavailable"))
    assert receipt.write_result.fallback_written is True
    assert fallback.call_count == 1 and "queued" in fallback.call_args.args[0]
