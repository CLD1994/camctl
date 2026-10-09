"""启动可靠无效果最终失败的占用释放与独立保留条件。"""

import asyncio

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ActivityReleaseSave
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import ReleaseOutcome

from .test_cancel_late_start import _cancel
from .test_capture_contract import _NOW
from .test_recording_start_runtime import StartDriver, _configured, _result, _world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("permanent", [False, True])
async def test_no_effect_final_start_releases_only_when_all_conditions_hold(tmp_path, permanent):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(no_effect=True, error=ErrorValue(
            "device_start_failed" if permanent else "device_busy",
            "execution" if permanent else "device")))
        runtime = _configured(owned, driver, maximum=3 if permanent else 1)
        await capture_handler("camera_record")(12, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (4,)
        assert owned.connection.execute(
            "SELECT dispatch_state, activity_state, occupancy_state"
            " FROM device_activities WHERE action_id=12").fetchone() == (4, 1, 2)
        result_txn = owned.connection.execute(
            "SELECT e.transaction_id FROM operation_attempts a"
            " JOIN history_events e ON e.id=a.result_event_id").fetchone()[0]
        release_txn = owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE event_type=13"
            " AND json_extract(body_json,'$.reason')=3").fetchone()[0]
        assert result_txn == release_txn
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_unknown_start_failure_keeps_occupancy_without_proving_release(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(error=ErrorValue("transport_timeout", "transport")))
        runtime = _configured(owned, driver)
        await capture_handler("camera_record")(12, runtime)
        await capture_handler("camera_record")(12, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (4,)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (1,)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_lowered_start_budget_releases_original_no_effect_activity_without_new_call(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(no_effect=True, error=ErrorValue("device_busy", "device")))
        original = _configured(owned, driver, maximum=3)
        await capture_handler("camera_record")(12, original)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (2,)
        current = _configured(owned, driver, maximum=1)
        await capture_handler("camera_record")(12, current)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (4,)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (2,)
        assert len(driver.calls) == 1
        assert owned.connection.execute(
            "SELECT status, attempts_used FROM operation_runs WHERE responsibility_key='start/12'"
        ).fetchone() == (4, 1)
    finally:
        owned.connection.close()


async def test_unresolved_output_preparation_keeps_occupancy_without_any_start(tmp_path):
    owned = _world(tmp_path)
    try:
        # 原活动尚在目录基准准备阶段，没有启动意图，不绕过准备
        # 门禁派发；可靠 NOT_DISPATCHED 仍不能解除所属输出范围。
        owned.connection.execute(
            "UPDATE device_activities SET ownership_mode=2, baseline_state=2 WHERE action_id=12")
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        result = runtime.capture.release_occupancy(
            ActivityReleaseSave(12, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.outcome is ReleaseOutcome.REJECTED
        assert result.value.reason == "scope_limited"
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (1,)
        assert runtime.driver.calls == []
    finally:
        owned.connection.close()


async def test_actual_start_call_keeps_occupancy_until_its_result_arrives(tmp_path):
    owned = _world(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None

    class HeldStart:
        async def control(self, request):
            entered.set()
            await release.wait()
            return DeviceCallResult.from_outcome(_result(
                no_effect=True, error=ErrorValue("device_busy", "device")))

    try:
        runtime = _configured(owned, HeldStart())
        task = asyncio.create_task(capture_handler("camera_record")(12, runtime))
        await asyncio.wait_for(entered.wait(), 1)
        _cancel(owned, 12)
        await capture_handler("camera_record")(12, runtime)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (1,)
        assert owned.connection.execute("SELECT status FROM operation_attempts").fetchone() == (1,)
        release.set()
        await task
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE action_id=12").fetchone() == (2,)
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        owned.connection.close()
