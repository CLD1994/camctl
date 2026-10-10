"""实际调用期间保存业务取消，返回后只沿原票据收场。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.flows import _resolve_and_fix
from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, StartCancelAction
from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind
from camctl.contracts.values import new_operation_key
from camctl.contracts.enums import enum_for
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository

from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world, returned
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_timelapse_host_timer import world as host_world

pytestmark = pytest.mark.asyncio


def _cancel(runtime, action_id, mode):
    now = runtime.wall_us()
    instant = datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cancel.json", {
        "request_id": "99", "created_at": instant, "name": "取消本次采集",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}}]}),
        ResultCatalog(), CommandMode.RUN, now), new_operation_key(), runtime.owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    origin, raw = runtime.owned.connection.execute("SELECT id,input_fields_json FROM actions WHERE type=6").fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, now), new_operation_key(), runtime.owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    item, = _resolve_and_fix(runtime.owned, repository, origin, raw, lambda: now)
    applied = repository.apply_cancel_target(ApplyCancelTarget(item, mode, now), new_operation_key(), runtime.owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


@pytest.mark.parametrize("held_phase", ["stop", "failed_results"])
async def test_host_timer_uses_cancel_saved_during_actual_stop_or_failed_result_scan(tmp_path, held_phase):
    owned, runtime, action_id, handler, results, clock = await host_world(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await capture_handler(handler)(action_id, runtime)
        if held_phase == "failed_results":
            runtime.timelapse_deadlines.clear()
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
        _cancel(runtime, action_id, CancelApplyMode.WITH_STOP)
        release.set()
        await task
        assert runtime.action(action_id)["status"] == 6
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
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "photo", start=False)
    entered, release = asyncio.Event(), asyncio.Event()
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(replace(
        returned("operation", "photo_taken", 1), status=AttemptStatus.FAILED,
        effect=EffectState.NO_EFFECT, error=ErrorValue("control_rejected", "device"), observations=()))
    class HeldResults(ResultsDouble):
        async def list_round(self, ticket, **kwargs):
            entered.set()
            await release.wait()
            return await super().list_round(ticket, **kwargs)
    runtime.results = HeldResults({action_id: (_entry("unrelated-photo", kind=FileKind.PHOTO),)})
    task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        _cancel(runtime, action_id, CancelApplyMode.PRE_START)
        release.set()
        await task
        assert runtime.action(action_id)["status"] == 6
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE kind=7").fetchone() == (int(enum_for("operation_runs.status").CANCELED), 1)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        runtime.driver.control.assert_awaited_once()
        assert runtime.results.calls == [action_id]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()
