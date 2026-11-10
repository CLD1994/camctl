"""首次窗口观察的纯业务判定，不依赖事件上下文或存储。"""

import pytest

from camctl.scheduling.rules import (
    WindowObservationError,
    validate_window_observation,
)


_START = 10_000_000
_END = _START + 2_000


def _validate(*, previous=None, observed=_START, scheduled_at=_START, max_delay_ms=2):
    return validate_window_observation(
        action_id=17, previous=previous, observed=observed,
        scheduled_at=scheduled_at, max_delay_ms=max_delay_ms)


@pytest.mark.parametrize("observed", [_START, _START + 1, _END])
def test_observation_within_inclusive_window(observed):
    assert _validate(observed=observed) is None


def test_zero_width_window_accepts_only_scheduled_instant():
    assert _validate(max_delay_ms=0) is None
    for observed in (_START - 1, _START + 1):
        with pytest.raises(WindowObservationError, match="观察时间不在启动窗口内: actions#17"):
            _validate(observed=observed, max_delay_ms=0)


def test_existing_integer_semantics_allow_negative_time_and_integer_subclasses():
    class Timestamp(int):
        pass

    assert _validate(observed=Timestamp(-1), scheduled_at=Timestamp(-1), max_delay_ms=0) is None


@pytest.mark.parametrize("observed", [_START - 1, _END + 1])
def test_outside_window_is_rejected(observed):
    with pytest.raises(WindowObservationError, match="观察时间不在启动窗口内: actions#17"):
        _validate(observed=observed)


@pytest.mark.parametrize("previous", [0, False, _START])
def test_any_existing_observation_cannot_be_overwritten(previous):
    with pytest.raises(WindowObservationError, match="首次观察不能覆盖已有观察时间"):
        _validate(previous=previous)


@pytest.mark.parametrize("observed", [None, True, False, 1.5, "2026"])
def test_observation_requires_integer_microseconds(observed):
    with pytest.raises(WindowObservationError) as error:
        _validate(observed=observed)
    assert str(error.value) == f"观察时间必须是整数微秒: {observed!r}"


@pytest.mark.parametrize("field", ["scheduled_at", "max_delay_ms"])
@pytest.mark.parametrize("value", [None, True, False, 1.5, "2"])
def test_window_requires_both_integer_facts(field, value):
    with pytest.raises(WindowObservationError, match="窗口观察要求动作保存启动窗口: actions#17"):
        _validate(**{field: value})


def test_existing_observation_error_precedes_invalid_new_values():
    with pytest.raises(WindowObservationError, match="首次观察不能覆盖已有观察时间"):
        _validate(previous=0, observed=True, scheduled_at=None)


def test_invalid_observation_precedes_missing_window_facts():
    with pytest.raises(WindowObservationError, match="观察时间必须是整数微秒"):
        _validate(observed=True, scheduled_at=None)


def test_negative_delay_preserves_launch_window_error():
    with pytest.raises(ValueError) as error:
        _validate(max_delay_ms=-1)
    assert type(error.value) is ValueError
    assert str(error.value) == f"窗口终点早于计划时间: {_START - 1000} < {_START}"
