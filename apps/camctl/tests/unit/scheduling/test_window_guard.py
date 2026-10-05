"""具名 window 守卫按动作保存的窗口核对首次观察时间。"""

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.scheduling import (
    _window_guard,
    register_window_guard,
)
from camctl.persistence.transaction import event_envelope, update_change

_SCHEDULED = 10_000_000
_MAX_DELAY_MS = 2
#: 窗口终点（微秒）：scheduled_at + max_delay_ms * 1000。
_WINDOW_END = _SCHEDULED + _MAX_DELAY_MS * 1000


def _observation(observed, *, first_before=None, scheduled=_SCHEDULED,
                 max_delay=_MAX_DELAY_MS, with_facts=True):
    states = {"actions": {1: {
        "id": 1, "type": 1, "status": 1,
        "scheduled_at": scheduled, "max_delay_ms": max_delay,
        "first_window_observed_at": first_before,
    }}} if with_facts else {"actions": {}}
    row = update_change(
        "actions", 1,
        {"first_window_observed_at": first_before},
        {"first_window_observed_at": observed})
    event = event_envelope(1, 1, 7, 1, (row,), _SCHEDULED)
    context = EventContext(TransactionRange(1, 1, 1), {}, states)
    return event, context


@pytest.fixture
def window_guard(monkeypatch):
    from camctl.history import validators

    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_window_guard()
    return validators.NAMED_GUARDS["window"]


@pytest.mark.parametrize("observed", [_SCHEDULED, _SCHEDULED + 1, _WINDOW_END])
def test_window_guard_accepts_in_window_observation(window_guard, observed):
    event, context = _observation(observed)
    window_guard(event, context)


@pytest.mark.parametrize("observed", [_SCHEDULED - 1, _WINDOW_END + 1])
def test_window_guard_rejects_out_of_window_observation(window_guard, observed):
    event, context = _observation(observed)
    with pytest.raises(EventValidationError):
        window_guard(event, context)


def test_window_guard_rejects_overwriting_first_observation(window_guard):
    event, context = _observation(
        _SCHEDULED + 1, first_before=_SCHEDULED)
    with pytest.raises(EventValidationError):
        window_guard(event, context)


@pytest.mark.parametrize("observed", ["2026", 1.5, True])
def test_window_guard_rejects_non_integer_observation(window_guard, observed):
    event, context = _observation(observed)
    with pytest.raises(EventValidationError):
        window_guard(event, context)


def test_window_guard_requires_persisted_window_facts(window_guard):
    event, context = _observation(_SCHEDULED, with_facts=False)
    with pytest.raises(EventValidationError):
        window_guard(event, context)


def test_window_guard_requires_complete_window_columns(window_guard):
    # 缺少 max_delay_ms 的动作事实不能支撑窗口判断。
    event, context = _observation(_SCHEDULED)
    del context.state_rows["actions"][1]["max_delay_ms"]
    with pytest.raises(EventValidationError):
        window_guard(event, context)
