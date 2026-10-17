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
