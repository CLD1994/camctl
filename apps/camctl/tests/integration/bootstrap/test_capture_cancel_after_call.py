"""实际调用期间提交正式取消，返回后沿原票据保存事实并结束原用途。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.bootstrap.flows import cancel_flow
from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.devices.ports import DeviceCallResult, ResultDriver, StopDriver
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue

from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world, returned
from ..capture.test_capture_contract import ResultsDouble, _entry, _PAGE_EVIDENCE
from ..capture.test_result_page_history import _BINDING, _CURSOR, _actual, _entry as result_entry
from ..capture.test_timelapse_host_timer import HostCatalog
from ..support.capture import capture_runtime
from ..support.session import accept_plan

pytestmark = pytest.mark.asyncio


async def _cancel(runtime, action_id, tmp_path):
    now = runtime.wall_us()
    instant = datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await accept_plan(runtime.database, tmp_path, ResultCatalog(), {
        "request_id": "99", "created_at": instant, "name": "取消本次采集",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}}]},
        SimpleNamespace(utc_micros=lambda: now))
    await cancel_flow(ready=tmp_path / "ready", processing=tmp_path / "processing")(
        SimpleNamespace(database=runtime.database, clock=SimpleNamespace(utc_micros=lambda: now)))


async def _host_world(tmp_path):
    results = create_autospec(ResultDriver, instance=True)

    async def read(request, batch):
        cursor = None if "cursor" not in request.params else _CURSOR
        number = 1 if cursor is None else 2
        outcome = _actual(request.ticket, entries=[result_entry("a" if number == 1 else "b")],
            cursor=cursor, next_cursor=_CURSOR if number == 1 else None,
            finalized=number == 2)
        return DeviceCallResult.from_outcome(outcome)

    results.list_results.side_effect = read
    listing = DriverResultListing(results, _BINDING, _PAGE_EVIDENCE)
    stopper = create_autospec(StopDriver, instance=True)
    async def stop(request):
        return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", request.ticket.target_id))
    stopper.stop.side_effect = stop
    clock = [5_000_000_000]
    options = dict(results=listing, evidence=_PAGE_EVIDENCE, stopper=stopper,
                   monotonic_ns=lambda: clock[0])
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False,
        runtime_options=options)
    return owned, runtime, action_id, handler, results, clock, options


@pytest.mark.parametrize("held_phase", ["stop", "failed_results"])
async def test_host_timer_uses_cancel_saved_during_actual_stop_or_failed_result_scan(
        tmp_path, capture_resources, held_phase):
    owned, runtime, action_id, handler, results, clock, options = await _host_world(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await capture_handler(handler)(action_id, runtime)
        if held_phase == "failed_results":
            # 新正式工厂没有前次连续计时依据；保守停止后按实际文件核实收场。
            await capture_resources.restart_database(owned)
            runtime = await capture_runtime(owned, driver=runtime.driver,
                staging=tmp_path / "staging", wall_us=runtime.wall_us, **options)
            actual = results.list_results.side_effect
            async def list_results(request, batch):
                entered.set()
                await release.wait()
                return await actual(request, batch)
            results.list_results.side_effect = list_results
        else:
            async def stop(request):
                entered.set()
                await release.wait()
                return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", request.ticket.target_id))
            runtime.stopper.stop.side_effect = stop
            clock[0] = 15_000_000_000
        task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
        await asyncio.wait_for(entered.wait(), 1)
        await _cancel(runtime, action_id, tmp_path)
        release.set()
        await task
        assert (await runtime.action(action_id))["status"] == 6
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
        assert results.list_results.await_count == 2
    finally:
        release.set()
        if "task" in locals():
            await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def test_photo_no_effect_cancel_saved_during_result_call_closes_original_purpose(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldResults(ResultsDouble):
        async def list_round(self, ticket, **kwargs):
            entered.set()
            await release.wait()
            return await super().list_round(ticket, **kwargs)

    results = HeldResults({1: (_entry("unrelated-photo", kind=FileKind.PHOTO),)})
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "photo", start=False,
        runtime_options={"results": results, "evidence": _PAGE_EVIDENCE})
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(replace(
        returned("operation", "photo_taken", 1), status=AttemptStatus.FAILED,
        effect=EffectState.NO_EFFECT, error=ErrorValue("control_rejected", "device"), observations=()))
    task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await _cancel(runtime, action_id, tmp_path)
        release.set()
        await task
        assert (await runtime.action(action_id))["status"] == 6
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE kind=7").fetchone() == (int(enum_for("operation_runs.status").CANCELED), 1)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        runtime.driver.control.assert_awaited_once()
        assert results.calls == [action_id]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()
