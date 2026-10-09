"""取消延时摄影的必要文件核实不能由成功 STOP 代替。"""

from __future__ import annotations

import pytest

from camctl.cancellation.settlement import TargetSettlement
from camctl.capture.handlers import _begin_check_round, _conclude_activity, _stop_call, capture_handler
from camctl.operations.attempts import BeginDisposition
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.repositories.cancellation import CancellationRepository

from ..capture.test_cancel_late_start import _cancel
from ..capture.test_capture_contract import (
    ResultsDouble, StopDouble, _environment, _runtime,
)
from .test_binding_changes import _RecoveryDriver, _recovering_runtime, environment


async def _stopped_canceled_timelapse(home, *, result_started):
    """先保存实际发送、合法取消和成功停止，尚无完整结果集合。"""
    home.mkdir()
    owned = _environment(home, ((13, 3),))
    runtime = _runtime(owned)
    await capture_handler("camera_timelapse")(13, runtime)
    _cancel(owned, 13)
    runtime.stopper = StopDouble("13")
    stopped = await _stop_call(runtime, runtime.action(13), "stop_timelapse")
    assert stopped.phase == "confirmed"
    _conclude_activity(runtime, 13)
    if result_started:
        begin = _begin_check_round(runtime, 13)
        assert begin.disposition is BeginDisposition.GRANTED
        runtime.finish(begin.ticket, CallOutcome(
            status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
            error=ErrorValue("transport_timeout", "transport"), observations=(),
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("results_returned", 1, {}))))
    return owned


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("result_started", [False, True])
async def test_canceled_timelapse_binding_failure_keeps_result_settlement_failure(
        environment, change, result_started):
    _, _, home = environment
    owned = await _stopped_canceled_timelapse(
        home / "stopped", result_started=result_started)
    try:
        stop_before = owned.connection.execute(
            "SELECT * FROM operation_runs WHERE responsibility_key = 'stop/13'").fetchone()
        attempts_before = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
        activity_before = owned.connection.execute(
            "SELECT * FROM device_activities WHERE action_id = 13").fetchone()
        driver = _RecoveryDriver()
        results = ResultsDouble({})
        runtime = _recovering_runtime(owned, home, change, driver, results)

        await capture_handler("camera_timelapse")(13, runtime)

        assert driver.calls == []
        assert results.calls == []
        assert owned.connection.execute(
            "SELECT status, cancel_requested, driver_id FROM actions WHERE id = 13"
        ).fetchone() == (6, 1, "camctl-adb")
        assert owned.connection.execute(
            "SELECT * FROM operation_runs WHERE responsibility_key = 'stop/13'"
        ).fetchone() == stop_before
        assert tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts_before
        assert owned.connection.execute(
            "SELECT * FROM device_activities WHERE action_id = 13").fetchone() == activity_before
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id = 13").fetchone() == (0,)

        settled = await TargetSettlement(
            owned, None, CancellationRepository(), lambda _: "unknown", runtime.wall_us,
        ).settle(13)

        assert settled.complete and settled.failed
    finally:
        owned.connection.close()
