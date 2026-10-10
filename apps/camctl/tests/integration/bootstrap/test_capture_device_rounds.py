"""A 的实际调用挂起时，B 下一轮停止和业务取消仍可推进。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import capture_flow, cancel_flow, residual_flow, winddown_flow
from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.capture.handlers import SessionRecordingState
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryRead
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.devices.tasks import StartReturn, CompletionMode
from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from .test_baseline_device_progress import TrackedConnection
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_baseline_start import _runtime
from ..capture.test_baseline_prepare import Directory
from ..capture.test_capture_contract import ResultsDouble, _PAGE_EVIDENCE
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, owned
from ..session.test_session import environment, _facts
from ..session.test_local_work_settlement import _assert_locks_held
from camctl.session.service import StateDbFailure, run_session

pytestmark = pytest.mark.asyncio
register_cancellation_guards()
register_outputs_guards()


class CompletedCatalog:
    def __init__(self):
        self.base = _catalog("dji-action6")
    def __getattr__(self, name):
        return getattr(self.base, name)
    def parameter_definition(self, device, action, parameter):
        definition = self.base.parameter_definition(device, action, parameter)
        if action != "camera_timelapse":
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), start_return_meaning=StartReturn.COMPLETED,
            completion_mode=CompletionMode.DEVICE_EVIDENCE, wait_after_send=False,
            result_wait_margin_s=None, start_call_timeout_s=Decimal("1810")))


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.mark.parametrize("held_phase", ["baseline", "completed", "results", "timelapse_results"])
async def test_other_device_stops_in_next_round_while_original_call_is_held(owned, tmp_path, held_phase):
    for request_id, device in (("42", "cam-1"), ("43", "cam-2")):
        action_type = "camera_timelapse" if device == "cam-1" and held_phase in ("completed", "timelapse_results") else "camera_record"
        body = _plan_body(request_id=request_id)
        body["actions"][0].update(type=action_type, device_id=device, params=_params("dji-action6", action_type))
        catalog = CompletedCatalog() if action_type == "camera_timelapse" else _catalog("dji-action6", device_id=device)
        await _accept_plan(owned, tmp_path, body, catalog)
    entered, release, halt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts, stops, reports, connections = [], [], [], []
    anchors, pending_results, pending_scans, pending_baselines = {}, {}, {}, {}
    clock = [0]
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])

    def open_connection():
        actual = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        tracked = TrackedConnection(actual.connection)
        connections.append(tracked)
        return replace(actual, connection=tracked)

    class HeldDirectory:
        async def read_directory(self, request, *, stop):
            entered.set()
            await release.wait()
            return DirectoryRead(None, None, Page((), None))

    class Driver:
        async def control(self, request):
            starts.append(request.binding.device_id)
            if request.binding.device_id == "cam-1" and held_phase in ("completed", "timelapse_results"):
                if held_phase == "completed":
                    entered.set()
                    await release.wait()
                return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.SUCCEEDED,
                    effect=EffectState.CONFIRMED, settlement=Settlement(SettlementBasis.OBSERVED,
                    EvidenceValue("operation_returned", 1, {}))))
            return DeviceCallResult((DeviceObservation("start_confirmed", 1,
                {"activity_id": request.ticket.target_id}),), None)
        async def stop(self, request):
            stops.append(request.binding.device_id)
            return DeviceCallResult((DeviceObservation("stop_confirmed", 1,
                {"activity_id": request.ticket.target_id}),), None)

    class Results(ResultsDouble):
        async def list_page(self, ticket, **kwargs):
            if ticket.target_id == "1" and held_phase in ("results", "timelapse_results"):
                entered.set()
                await release.wait()
            return await super().list_page(ticket, **kwargs)

    driver, results = Driver(), ResultsDouble({})
    held_results = Results({})
    def factory(connection, device):
        directory = HeldDirectory() if device == "cam-1" and held_phase == "baseline" else Directory(
            [lambda request: DirectoryRead(None, None, Page((), None))])
        runtime = _runtime(connection, directory, driver)
        runtime.stopper = driver
        runtime.results = held_results if device == "cam-1" else results
        runtime.evidence = _PAGE_EVIDENCE
        runtime.monotonic_ns = lambda: clock[0] * 1_000_000_000
        runtime.wall_us = lambda: _SCHEDULED + clock[0] * 1_000_000
        runtime.recording_state = SessionRecordingState(runtime, anchors)
        runtime.pending_start_results = pending_results
        runtime.pending_result_scans = pending_scans
        runtime.pending_baselines = pending_baselines
        return runtime

    context = SimpleNamespace(open_connection=open_connection,
        clock=SimpleNamespace(utc_micros=lambda: _SCHEDULED + clock[0] * 1_000_000))
    capture = capture_flow(factory)
    def guarded_factory(connection, device):
        assert not capture.owns_device(device), "原拍摄拥有者交付前不能由恢复入口消费其 holder"
        return factory(connection, device)
    residual = residual_flow(guarded_factory, owns_device=capture.owns_device)
    cancel = cancel_flow(ready=tmp_path / "ready", processing=tmp_path / "processing")
    async def pump():
        while not halt.is_set():
            await capture(context)
            await cancel(context)
            await residual(context)
            reports.append(owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0])
            await asyncio.sleep(.001)
    task = asyncio.create_task(pump())
    try:
        await _until(lambda: "cam-2" in starts)
        if held_phase != "results":
            await entered.wait()
        clock[0] = 10
        if held_phase == "results":
            await entered.wait()
        cancel_body = _plan_body(request_id="44")
        cancel_body["actions"] = [{"name": "cancel-a", "type": "cancel_task",
            "params": {"target": {"action_instance_id": "1"}}}]
        await _accept_plan(owned, tmp_path, cancel_body, _catalog("dji-action6"))
        before = len(reports)
        await _until(lambda: stops.count("cam-2") == 1)
        await _until(lambda: owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (1,))
        await _until(lambda: len(reports) > before)
        assert not release.is_set()
        assert starts.count("cam-2") == stops.count("cam-2") == 1
        assert starts.count("cam-1") == int(held_phase != "baseline")
        assert any(not connection.closed for connection in connections)
        assert capture.required_settlements() >= 1
        await winddown_flow(capture_factory=guarded_factory, wait_cap_s=Decimal("0"),
                            owns_device=capture.owns_device)(context)
    finally:
        halt.set()
        release.set()
        await task
        await capture.settle()
        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6, 1)
        for connection in connections:
            assert connection.closed


@pytest.mark.parametrize("exit_kind", ["state", "cancel"])
async def test_capture_owner_settles_actual_call_before_session_releases_locks(environment, tmp_path, exit_kind):
    context, _, _, _ = environment
    owned = context.open_connection()
    body = _plan_body()
    body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6"))
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_done, connections = [], []
    opener = context.open_connection
    def open_connection():
        current = opener()
        tracked = TrackedConnection(current.connection)
        connections.append(tracked)
        return replace(current, connection=tracked)
    class Directory:
        async def read_directory(self, request, *, stop):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                actual_done.append(True)
                raise
    capture = capture_flow(lambda connection, device: _runtime(connection, Directory(), None))
    async def exit_flow(context):
        if exit_kind == "state" and entered.is_set():
            raise StateDbFailure("original session failure")
    context.open_connection = open_connection
    context.clock = SimpleNamespace(utc_micros=lambda: _SCHEDULED, monotonic_ns=lambda: 0)
    context.facts_query = lambda connection: _facts()
    context.poll_interval_s = .001
    context.flows = {"capture": capture, "exit": exit_flow}
    context.local_work = CombinedLocalWork((capture,))
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if exit_kind == "cancel":
            task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not task.done() and not actual_done
        assert any(not connection.closed for connection in connections)
        _assert_locks_held(context)
        release.set()
        if exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).reason == "state_db_error"
        assert actual_done == [True] and all(connection.closed for connection in connections)
        assert owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()
