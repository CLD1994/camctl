"""三份默认正式工厂接手实际返回，保留原计时与重试等待。"""

from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.operations.models import ErrorValue
from camctl.contracts.values import ConsistencyError

from ..capture.test_capture_contract import _NOW
from ..capture.test_recording_start_runtime import _result
from ..support.bootstrap_capture_cases import bootstrap_case, SavedBoundary

pytestmark = pytest.mark.asyncio
_RETURNED_AT = _NOW + 2_000_000
_RETURNED_NS = 7_000_000_000


async def _held_start(case, tmp_path, actual, *, maximum=3, interval=2):
    runtime = await case.world(tmp_path, control=actual, default=True, device_options={
        "recording": {"max_start_attempts": maximum, "start_retry_interval_s": Decimal(interval)}})
    original = case.driver.control.return_value

    async def returned(_request):
        case.fault.mode, case.fault.enabled = "commit_before", True
        return original

    case.driver.control.side_effect = returned
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, _RETURNED_NS
    with pytest.raises(ConsistencyError):
        await capture_handler(case.handler)(case.action_id, runtime)
    assert case.driver.control.await_count == 1 and case.fault.hits
    saves = [(request, key) for method, request, key in case.saves if method == "finish_start_result"]
    assert len(saves) == 2 and saves[0] == saves[1]
    assert saves[0][0].outcome.outcome is original.outcome
    assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts").fetchone() == (1, None)
    case.fault.enabled = False
    case.driver.control.side_effect = None
    return saves[0]


@pytest.mark.parametrize("saver", ["residual", "restricted"])
async def test_actual_start_saved_by_other_factory_preserves_ordinary_monotonic_anchor(
        tmp_path, bootstrap_case, saver):
    case = bootstrap_case
    original, key = await _held_start(case, tmp_path,
        _result(confirmed=True, error=ErrorValue("transport_timeout", "transport")))
    case.clock.wall, case.clock.monotonic = _RETURNED_AT + 5_000_000, 12_000_000_000
    case.boundary = "finish_start_result"
    takeover = await case.view(case.factories[1 if saver == "residual" else 2])
    with pytest.raises(SavedBoundary):
        await capture_handler(case.handler)(case.action_id, takeover)
    assert case.owned.connection.execute("SELECT started_at FROM device_activities").fetchone() == (_RETURNED_AT,)
    assert all(request == original and saved_key == key for method, request, saved_key in case.saves
        if method == "finish_start_result")
    case.boundary = "finish_capture_stop"
    case.clock.monotonic = 66_999_999_999
    await capture_handler(case.handler)(case.action_id, await case.view())
    assert case.stopper.calls == []
    case.clock.monotonic = 67_000_000_000
    with pytest.raises(SavedBoundary):
        await capture_handler(case.handler)(case.action_id, await case.view())
    assert len(case.stopper.calls) == case.driver.control.await_count == 1


@pytest.mark.parametrize("saver", ["residual", "restricted"])
@pytest.mark.parametrize("maximum,interval,offset_ns,expected", [
    (3, 2, 1_000_000_000, Decimal("1")),
    (3, 2, 2_000_000_000, None),
    (1, 2, 1_000_000_000, None),
    (3, 0, 1_000_000_000, None),
])
async def test_actual_start_saved_by_other_factory_preserves_ordinary_retry_budget_gate(
        tmp_path, bootstrap_case, saver, maximum, interval, offset_ns, expected):
    case = bootstrap_case
    original, key = await _held_start(case, tmp_path,
        _result(no_effect=True, error=ErrorValue("transport_timeout", "transport")),
        maximum=maximum, interval=interval)
    case.clock.wall, case.clock.monotonic = _RETURNED_AT + 5_000_000, _RETURNED_NS + offset_ns
    case.boundary = "finish_start_result"
    with pytest.raises(SavedBoundary):
        await capture_handler(case.handler)(case.action_id,
            await case.view(case.factories[1 if saver == "residual" else 2]))
    assert case.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
        (f"start/{case.action_id}",)).fetchone() == (1,)
    assert all(request == original and saved_key == key for method, request, saved_key in case.saves
        if method == "finish_start_result")
    if expected is None and maximum > 1:
        with pytest.raises(SavedBoundary):
            await capture_handler(case.handler)(case.action_id, await case.view())
        assert case.driver.control.await_count == 2
    else:
        await capture_handler(case.handler)(case.action_id, await case.view())
        assert case.driver.control.await_count == 1
    if expected is not None:
        case.clock.monotonic += 999_999_999
        await capture_handler(case.handler)(case.action_id, await case.view())
        assert case.driver.control.await_count == 1
        case.clock.monotonic += 1
        with pytest.raises(SavedBoundary):
            await capture_handler(case.handler)(case.action_id, await case.view())
        assert case.driver.control.await_count == 2
