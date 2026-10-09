"""启动核实实际在途与取消后迟到观察仍按原事实保存。"""

import asyncio

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)

from .test_cancel_late_start import _cancel
from .test_capture_contract import _NOW
from .test_recording_start_runtime import StartDriver, _configured, _result, _world

pytestmark = pytest.mark.asyncio


class HeldQuery:
    def __init__(self, *, late_error=False):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.outcome = CallOutcome(
            status=AttemptStatus.FAILED if late_error else AttemptStatus.SUCCEEDED,
            error=ErrorValue("transport_timeout", "transport") if late_error else None,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("query_returned", 1, {})),
            observations=(DeviceObservation("activity_status", 1, {"activity_id": "12"}),))

    async def query_state(self, request):
        self.calls.append(request)
        self.entered.set()
        await self.release.wait()
        return DeviceCallResult.from_outcome(self.outcome)


class StopSpy:
    def __init__(self):
        self.calls = []

    async def stop(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
            error=ErrorValue("transport_timeout", "transport"),
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("stop_returned", 1, {}))))


async def _held_confirmation(runtime, *, late_error=False):
    await capture_handler("camera_record")(12, runtime)
    query = HeldQuery(late_error=late_error)
    runtime.state_query = query
    task = asyncio.create_task(capture_handler("camera_record")(12, runtime))
    await asyncio.wait_for(query.entered.wait(), 1)
    return query, task


async def test_cancel_waits_for_actual_start_confirmation_before_stop(tmp_path):
    owned = _world(tmp_path)
    query = task = None
    try:
        runtime = _configured(owned, StartDriver(_result()))
        runtime.evidence = EvidenceRegistry((
            runtime.evidence.contract("operation_returned", 1),
            runtime.evidence.contract("query_returned", 1),
            runtime.evidence.contract("activity_status", 1),
            EvidenceContract("stop_returned", 1, "stop", frozenset()),))
        runtime.stopper = StopSpy()
        query, task = await _held_confirmation(runtime)
        _cancel(owned, 12)
        await capture_handler("camera_record")(12, runtime)
        assert runtime.stopper.calls == []
        assert owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.query_purpose=2").fetchone() == (1,)
    finally:
        if query is not None:
            query.release.set()
            await task
        owned.connection.close()


@pytest.mark.parametrize("late_error", [False, True])
async def test_canceled_confirmation_keeps_actual_observation_and_original_terminals(tmp_path, late_error):
    owned = _world(tmp_path)
    query = task = None
    try:
        driver = StartDriver(_result())
        runtime = _configured(owned, driver)
        query, task = await _held_confirmation(runtime, late_error=late_error)
        _cancel(owned, 12)
        query.release.set()
        await task
        assert owned.connection.execute(
            "SELECT started_at, activity_state FROM device_activities WHERE action_id=12"
        ).fetchone() == (_NOW, 2)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE action_id=12 ORDER BY id"
        ).fetchall() == [(5,), (5,)]
        assert owned.connection.execute(
            "SELECT a.status, a.effect_state FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE r.query_purpose=2"
        ).fetchone() == (3 if late_error else 2, 3)
        assert len(driver.calls) == len(query.calls) == 1
    finally:
        if query is not None and not task.done():
            query.release.set()
            await task
        owned.connection.close()
