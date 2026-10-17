"""精确计时同时约束运行与持久化边界。"""
import pytest
from camctl.contracts.values import MAX_OBJECT_ID
from camctl.devices.capture_timing import CaptureTiming


def test_deadlines_keep_duration_and_independent_margins():
    timing = CaptureTiming(300000, 1000, 30000, 2000)
    assert timing.deadline_us(1000000) == 334000000
    assert timing.deadline_ns(1000000000) == 334000000000
    assert CaptureTiming(10000, 1000).recording_lower_bound_ns(500000000) == 0
    assert CaptureTiming(10000, 1000).recording_lower_bound_ns(11000000000) == 10000000000


@pytest.mark.parametrize("args", [(True, 1000), (1000, True), (0, 1000), (1000, -1), (1000, 0, -1)])
def test_invalid_timing_rejected(args):
    with pytest.raises(ValueError):
        CaptureTiming(*args)


def test_deadline_rejects_persistent_integer_overflow():
    with pytest.raises(ValueError):
        CaptureTiming(MAX_OBJECT_ID, 1000).deadline_us(1000000)
    with pytest.raises(ValueError):
        CaptureTiming(10000, 1000).deadline_ns(MAX_OBJECT_ID)


def test_typed_integer_clock_values_keep_exact_timing():
    from camctl.contracts.values import UtcMicros
    assert CaptureTiming(10000, 1000).deadline_us(UtcMicros(1000000)) == 12000000


def test_wait_reference_preserves_signed_past_and_future_deadlines():
    from camctl.devices.capture_timing import WaitClockReference
    reference = WaitClockReference(3000000, 10000000000)
    assert reference.logical_deadline_ns(2200000) == 9200000000
    assert reference.logical_deadline_ns(4200000) == 11200000000
    assert WaitClockReference(3000000, 100000000).logical_deadline_ns(2200000) == -700000000


@pytest.mark.parametrize('utc,mono', [(True, 1), (1, True), (-1, 1), (1, -1)])
def test_wait_reference_rejects_invalid_clock_sources(utc, mono):
    from camctl.devices.capture_timing import WaitClockReference
    with pytest.raises(ValueError):
        WaitClockReference(utc, mono)
