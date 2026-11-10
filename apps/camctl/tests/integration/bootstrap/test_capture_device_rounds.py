"""A 的实际调用挂起时，B 下一轮停止和业务取消仍可推进。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import capture_flow, cancel_flow, residual_flow, winddown_flow
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.tasks import StartReturn, CompletionMode
from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.session.clock import ClockCheckInput
from camctl.session.service import StateDbFailure, run_session

from .test_baseline_device_progress import TrackedConnection
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_capture_contract import ResultsDouble, _PAGE_EVIDENCE
from ..scheduling.test_start_action import _SCHEDULED
from ..session.test_local_work_settlement import _assert_locks_held
from ..support.database import business_database
from ..support.session import accept_plan, query_database, session_config, session_context

pytestmark = pytest.mark.asyncio


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


async def _until(predicate, *, task=None):
    async with asyncio.timeout(2):
        while not predicate():
            if task is not None and task.done():
                await task
                raise AssertionError("流程已结束但等待条件未成立")
            await asyncio.sleep(.001)


async def _tracked_database(path, connections):
    def factory():
        actual = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        tracked = TrackedConnection(actual.connection)
        connections.append(tracked)
        return replace(actual, connection=tracked)
    return await BusinessDatabase.open(factory)


def _capture_factory(config, driver, results, clock):
    entry = DriverEntry("dji-action6", driver, DriverDeclaration(
        control_supported=True, stop_supported=hasattr(driver, "stop"), query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False, directory_supported=True), _PAGE_EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    return session_capture_assembly(
        devices=config.devices, drivers=DriverRegistry((entry,)),
        results=results, staging=Path(config.paths.staging),
        wait_config=execution_wait_config, wall_us=clock.utc_micros,
        monotonic_ns=clock.monotonic_ns)


@pytest.mark.parametrize("held_phase", ["baseline", "completed", "results", "timelapse_results"])
async def test_other_device_stops_in_next_round_while_original_call_is_held(tmp_path, held_phase):
    config = session_config(tmp_path, devices={device: {
        "kind": "camera", "driver": "dji-action6"} for device in ("cam-1", "cam-2")})
    assert initialize_state(config, tmp_path / "state.db").outcome is InitOutcome.CREATED
    entered, release, halt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts, stops, reports, connections = [], [], [], []
    seconds = [0]
    clock = SimpleNamespace(utc_micros=lambda: _SCHEDULED + seconds[0] * 1_000_000,
                            monotonic_ns=lambda: seconds[0] * 1_000_000_000)

    class Driver:
        async def read_directory(self, request, *, stop):
            if request.binding.device_id == "cam-1" and held_phase == "baseline":
                entered.set()
                await release.wait()
            return DirectoryRead(None, None, Page((), None))

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

    database = await _tracked_database(tmp_path / "state.db", connections)
    driver, results = Driver(), Results({})
    factory = _capture_factory(config, driver, results, clock)
    capture = capture_flow(factory)

    async def guarded_factory(database, device):
        assert not capture.owns_device(device), "原拍摄拥有者交付前不能由恢复入口消费其结果"
        return await factory(database, device)

    residual = residual_flow(guarded_factory, owns_device=capture.owns_device)
    cancel = cancel_flow(staging=tmp_path / "staging", ready=tmp_path / "ready", processing=tmp_path / "processing")
    context = SimpleNamespace(database=database, clock=clock)
    task = None
    try:
        for request_id, device in (("42", "cam-1"), ("43", "cam-2")):
            action_type = "camera_timelapse" if device == "cam-1" and held_phase in ("completed", "timelapse_results") else "camera_record"
            body = _plan_body(request_id=request_id)
            body["actions"][0].update(type=action_type, device_id=device, params=_params("dji-action6", action_type))
            catalog = CompletedCatalog() if action_type == "camera_timelapse" else _catalog("dji-action6", device_id=device)
            await accept_plan(database, tmp_path, catalog, body, clock)

        async def pump():
            while not halt.is_set():
                await capture(context)
                await cancel(context)
                await residual(context)
                reports.append(await database.read(
                    lambda owned: owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]))
                await asyncio.sleep(.001)

        task = asyncio.create_task(pump())
        await _until(lambda: "cam-2" in starts, task=task)
        if held_phase != "results":
            await _until(entered.is_set, task=task)
        seconds[0] = 10
        if held_phase == "results":
            await _until(entered.is_set, task=task)
        cancel_body = _plan_body(request_id="44")
        cancel_body["actions"] = [{"name": "cancel-a", "type": "cancel_task",
            "params": {"target": {"action_instance_id": "1"}}}]
        await accept_plan(database, tmp_path, _catalog("dji-action6"), cancel_body, clock)
        before = len(reports)
        await _until(lambda: stops.count("cam-2") == 1, task=task)
        await _until(lambda: query_database(tmp_path / "state.db",
            "SELECT cancel_requested FROM actions WHERE id=1") == (1,), task=task)
        await _until(lambda: len(reports) > before, task=task)
        assert not release.is_set()
        assert starts.count("cam-2") == stops.count("cam-2") == 1
        assert starts.count("cam-1") == int(held_phase != "baseline")
        assert connections and all(not connection.closed for connection in connections)
        assert capture.required_settlements() >= 1
        await winddown_flow(capture_factory=guarded_factory, wait_cap_s=Decimal("0"),
                            owns_device=capture.owns_device)(context)
    finally:
        halt.set()
        release.set()
        try:
            if task is not None:
                await task
            await capture.settle()
            assert query_database(tmp_path / "state.db",
                "SELECT status,cancel_requested FROM actions WHERE id=1") == (6, 1)
        finally:
            await database.close()
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("exit_kind", ["state", "cancel"])
async def test_capture_owner_settles_actual_call_before_session_releases_locks(tmp_path, exit_kind):
    config = session_config(tmp_path, devices={"cam-1": {"kind": "camera", "driver": "dji-action6"}})
    assert initialize_state(config, tmp_path / "state.db").outcome is InitOutcome.CREATED
    catalog = _catalog("dji-action6")
    clock = SimpleNamespace(utc_micros=lambda: _SCHEDULED, monotonic_ns=lambda: 0)
    async with business_database(tmp_path / "state.db") as database:
        body = _plan_body()
        body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"))
        await accept_plan(database, tmp_path, catalog, body, clock)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_done, connections = [], []

    class Driver:
        async def read_directory(self, request, *, stop):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                actual_done.append(True)
                raise

        async def control(self, request):
            pytest.fail("目录实际调用尚未完成时不能发送START")

    capture = capture_flow(_capture_factory(config, Driver(), ResultsDouble({}), clock))

    async def exit_flow(context):
        if exit_kind == "state" and entered.is_set():
            raise StateDbFailure("original session failure")

    context = session_context(config.paths.state_db, catalog=catalog, clock=clock,
        open_database=lambda: _tracked_database(tmp_path / "state.db", connections),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=None, min_plausible_micros=_SCHEDULED - 1,
            recheck_delay_s=0.0, recheck_count=1),
        flows={"capture": capture, "exit": exit_flow},
        local_work=CombinedLocalWork((capture,)), poll_interval_s=.001)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if exit_kind == "cancel":
            task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not task.done() and not actual_done
        assert connections and all(not connection.closed for connection in connections)
        _assert_locks_held(context)
        release.set()
        if exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).reason == "state_db_error"
        assert actual_done == [True] and all(connection.closed for connection in connections)
        assert query_database(tmp_path / "state.db", "SELECT cancel_requested FROM actions WHERE id=1") == (0,)
        assert query_database(tmp_path / "state.db", "SELECT COUNT(*) FROM operation_attempts") == (0,)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
