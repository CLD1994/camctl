"""主机到期停止与失去连续计时依据分别保存实际事实和动作结果。"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
import pytest_asyncio

from camctl.capture.handlers import capture_handler
from camctl.capture.handlers import _listing_round
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.bindings import BindingResult, BindingStatus
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.devices.tasks import EndControl, StartReturn
from camctl.capture.models import HostTimerStopSave
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptConfig, RunFinish, RunOutcome
from camctl.operations.models import EffectState, ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned, _NOW
from .test_result_device_completion import DeviceCompletionCatalog, _EVIDENCE, _COMPLETION, device_pages
from ..support import result_cases
from ..support.capture import capture_factory
from ..support.native_capture_cases import request_cancel, native_fault_world


@pytest_asyncio.fixture
async def native_result_case(capture_resources):
    case = result_cases.ResultCase(capture_resources)
    try:
        yield case
    finally:
        await case.close()


def native_device_pages(case_support, **options):
    ports = SimpleNamespace()
    options.setdefault("completion_page", None)
    driver = device_pages(ports, **options)
    case_support.results.delegate = ports.results
    return driver


pytestmark = pytest.mark.asyncio


class HostCatalog(DeviceCompletionCatalog):
    def __init__(self, meaning=StartReturn.SENT, *, baseline=False, end_control=EndControl.HOST_TIMER, startup_margin_ms=None):
        self.meaning = meaning
        self.baseline = baseline
        self.end_control = end_control
        self.startup_margin_ms = startup_margin_ms

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type == "camera_record" and self.baseline:
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), ownership_mode=2,
                output_scope={"directories": ["/DCIM"]}))
        if action_type != "camera_timelapse":
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), target_duration_s=Decimal("10"),
            end_control=self.end_control, start_return_meaning=self.meaning, startup_margin_ms=self.startup_margin_ms,
            ownership_mode=2 if self.baseline else 1,
            output_scope={"directories": ["/DCIM"]} if self.baseline else {"task": "own"},
            product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                "exact_count": None, "require_pairing": False},)))


async def world(tmp_path, *, meaning=StartReturn.SENT, case="video", consumer="timelapse", completion_page=None,
                baseline=False, end_control=EndControl.HOST_TIMER, startup_margin_ms=None, catalog=None):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, consumer,
        independent_activity=True, catalog=catalog or HostCatalog(meaning, baseline=baseline, end_control=end_control, startup_margin_ms=startup_margin_ms), start=False)
    if baseline:
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from .test_baseline_prepare import Directory
        runtime.baseline_directory = Directory([lambda request: DirectoryRead(None, None, Page((), None))])
    results = device_pages(runtime, completion_page=completion_page, case=case)
    runtime.evidence = EvidenceRegistry(tuple(RESULT_EVIDENCE.contract(name, 1) for name in (
        "operation_returned", "timelapse_sent", "start_confirmed", "stop_returned", "stop_confirmed")) + (
        _EVIDENCE.contract("result_files_listed", 2), _EVIDENCE.contract("results_returned", 1),
        _EVIDENCE.contract("timelapse_completed", 1)))
    stop = create_autospec(StopDriver, instance=True)
    stop.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", 1))
    runtime.stopper = stop
    clock = [5_000_000_000]
    runtime.monotonic_ns = lambda: clock[0]
    return owned, runtime, action_id, handler, results, clock


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
async def test_waits_until_original_host_target_without_listing_or_stop(tmp_path, meaning, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(meaning), start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support)
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.monotonic = 14_999_999_999
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        runtime.stopper.stop.assert_not_awaited()
        results.list_results.assert_not_awaited()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
@pytest.mark.parametrize("case,status,reason", [
    ("video", 3, None), ("empty", 4, "no_outputs"), ("wrong_kind", 4, "invalid_outputs")])
async def test_host_target_stop_then_qualifies_result(tmp_path, meaning, case, status, reason, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(meaning), start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support, case=case)
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.monotonic = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == status
        if reason is not None:
            assert json.loads(row[1]) == {"activity_id": "1", "reason": reason}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state,expected_check_at,wait_completed_event_id FROM device_activities").fetchone() == (3, 10_000_000_000, 2, None, None)
        stop_event, stop_txn, stop_time = owned.connection.execute(
            "SELECT h.id,h.transaction_id,h.occurred_at FROM history_events h"
            " JOIN operation_attempts a ON a.result_event_id=h.id JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone()
        ends = [(txn, time, json.loads(body)) for txn, time, body in owned.connection.execute(
            "SELECT transaction_id,occurred_at,body_json FROM history_events WHERE event_type=13")
            if any(row["after"]["values"].get("activity_state") == 3 for row in json.loads(body)["rows"])]
        assert len(ends) == 1 and ends[0][:2] == (stop_txn, stop_time)
        assert ends[0][2]["evidence"]["observation"] == {
            "stop_result_event_id": stop_event, "control_elapsed_ns": 10_000_000_000}
        capture = json.loads(owned.connection.execute("SELECT capture_json FROM device_activities").fetchone()[0])
        assert "elapsed_s" not in capture and "captured_count" not in capture
        runtime.stopper.stop.assert_awaited_once()
        assert runtime.stopper.stop.call_args.args[0].operation == "stop_timelapse"
        assert results.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize("end_control", [EndControl.HOST_TIMER, EndControl.DEVICE])
async def test_business_cancel_before_target_keeps_original_canceled_result(tmp_path, baseline, end_control, native_result_case):
    case_support = native_result_case
    catalog = HostCatalog(baseline=baseline, end_control=end_control)
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=catalog, start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support, completion_page=None)
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.wall = _NOW + 2_000_000
        case_support.clock.monotonic = 7_000_000_000
        await request_cancel(runtime, action_id, case_support.clock.wall, catalog)
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (6, None)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (
            3, 2_000_000_000 if end_control is EndControl.HOST_TIMER else None)
        assert owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is None
        case_support.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("unknown_files", [False, True])
async def test_actual_goal_completion_after_unknown_duration_still_carries_its_guarantee(tmp_path, unknown_files, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False,
        evidence_contracts=(_COMPLETION,))
    native_device_pages(case_support, completion_page=1,
        case="unknown_format" if unknown_files else "video")
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime = await case_support.reopen(owned, cold=True)
        await capture_handler(handler)(action_id, runtime)
        if unknown_files:
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
            assert runtime.capture.read_result_completion(action_id, owned) is not None
            native_device_pages(case_support, completion_page=None)
            case_support.clock.monotonic += 3_000_000_000
            await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT control_elapsed_ns FROM device_activities").fetchone() == (None,)
        case_support.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def reopen_fault_database(world, capture_resources):
    """先等待文件责任并关闭故障 worker，再用原工厂连接新 worker。"""
    from camctl.persistence.business import BusinessDatabase

    previous = world.database
    previous_thread = world.connection_threads[-1]
    assert previous_thread.is_alive()
    await capture_resources.close()
    await previous.close()
    assert not previous_thread.is_alive()
    world.database = await BusinessDatabase.open(world.connect)
    assert world.connection_threads[-1] is not previous_thread
    assert world.connection_threads[-1].is_alive()
    return await world.factory(world.database)


@pytest.mark.parametrize("binding", [BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH])
async def test_held_stop_is_saved_before_new_binding_failure(tmp_path, binding, capture_resources, monkeypatch):
    from camctl.contracts.workflow_errors import registered_error
    from camctl.operations.result_format import result_document
    original_save = CaptureRepository.finish_host_timer_stop
    saves = []

    def record_save(repository, finish, stop, key, owned):
        saves.append((finish, stop, key))
        return original_save(repository, finish, stop, key, owned)

    monkeypatch.setattr(CaptureRepository, "finish_host_timer_stop", record_save)
    world = await native_fault_world(tmp_path, HostCatalog())
    owned, action_id = world.owned, world.action_id
    try:
        await capture_handler(world.handler)(action_id, world.runtime)
        world.mono[0] = 15_000_000_000
        world.fault.arm(world.stopper, "persistent")
        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(action_id, world.runtime)
        assert world.fault.injected and len(world.stopper.calls) == 1
        assert owned.connection.execute("SELECT a.status,a.result_event_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone() == (1, None)
        world.fault.enabled = False
        if binding is BindingStatus.DEVICE_MISSING:
            world.devices.pop("cam-1")
        else:
            world.devices["cam-1"]["driver"] = "other"
        saves_before_reopen = len(saves)
        assert saves_before_reopen > 0
        runtime = await reopen_fault_database(world, capture_resources)
        await capture_handler(world.handler)(action_id, runtime)
        assert len(saves) > saves_before_reopen and all(save == saves[0] for save in saves)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (3, 10_000_000_000)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (4, registered_error("device_binding_unavailable")["action_error_id"])
        result_event, = owned.connection.execute("SELECT result_event_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone()
        assert owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=?", (result_event,)).fetchone() == (world.wall[0],)
        result_json, = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE result_event_id=?", (result_event,)).fetchone()
        assert json.loads(result_json) == result_document(world.stopper.actual[0])
        assert len(world.stopper.calls) == 1
        world.result_driver.list_results.assert_not_awaited()
    finally:
        await capture_resources.close()
        await world.database.close()
        owned.connection.close()


async def test_typed_stop_confirmation_does_not_require_a_generic_observation_name(tmp_path):
    contract = EvidenceContract("native_stop_returned", 1, "stop",
        frozenset({"activity_id"}), identity_field="activity_id")
    evidence = EvidenceRegistry((*(RESULT_EVIDENCE.contract(name, 1)
        for name in ("operation_returned", "timelapse_sent", "stop_returned")),
        _EVIDENCE.contract("result_files_listed", 2),
        _EVIDENCE.contract("results_returned", 1), contract))
    clock = [5_000_000_000]
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(replace(
        returned("stop", "stop_confirmed", 1), observations=(
            DeviceObservation(contract.type, 1, {"activity_id": "1"}),)))
    ports = SimpleNamespace()
    device_pages(ports, completion_page=None)

    async def assemble(owned, *, driver):
        factory = await capture_factory(owned, driver=driver, evidence=evidence,
            results=ports.results, stopper=stopper, wall_us=lambda: _NOW,
            monotonic_ns=lambda: clock[0],
            check_config=AttemptConfig(3, Decimal("1.25"), Decimal("2")))
        return await factory(owned)

    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False, runtime_factory=assemble)
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (3, 10_000_000_000)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("duration_known", [False, True])
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_stop_save_preserves_original_result_time_and_duration(tmp_path, duration_known, committed, capture_resources, monkeypatch):
    from camctl.operations.result_format import result_document
    original_save = CaptureRepository.finish_host_timer_stop
    saves = []

    def record_save(repository, finish, stop, key, owned):
        saves.append((finish, stop, key))
        return original_save(repository, finish, stop, key, owned)

    monkeypatch.setattr(CaptureRepository, "finish_host_timer_stop", record_save)
    world = await native_fault_world(tmp_path, HostCatalog(), committed=committed)
    owned, action_id = world.owned, world.action_id
    try:
        await capture_handler(world.handler)(action_id, world.runtime)
        if duration_known:
            world.mono[0] = 15_000_000_000
        else:
            from camctl.persistence.business import BusinessDatabase
            await capture_resources.close()
            await world.database.close()
            world.database = await BusinessDatabase.open(world.connect)
            world.factory = await world.build_factory(world.database)
            world.runtime = await world.factory(world.database)
        if committed:
            world.fault.arm(world.stopper)
        else:
            world.fault.arm(world.stopper, "persistent")
        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(action_id, world.runtime)
        assert world.fault.injected and len(world.stopper.calls) == 1
        stop_state = owned.connection.execute("SELECT a.status,a.result_event_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone()
        if committed:
            assert stop_state[0] != 1 and stop_state[1] is not None
            assert world.fault.held_reads > 0
        else:
            assert stop_state == (1, None)
        assert saves and all(save == saves[0] for save in saves)
        world.fault.enabled = False
        if committed:
            finish, stop, key = saves[0]
            for changed in (HostTimerStopSave(None if duration_known else 10_000_000_000),
                            HostTimerStopSave(10_000_000_001)):
                rejected = original_save(CaptureRepository(), finish, changed, key, owned)
                assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        world.wall[0] = 1_750_009_000_000_000
        world.mono[0] = 99_000_000_000
        world.devices["cam-1"]["recording"] = {"max_stop_attempts": 1,
            "stop_timeout_s": Decimal("0.01"), "stop_retry_interval_s": Decimal("0")}
        saves_before_reopen = len(saves)
        assert saves_before_reopen > 0
        runtime = await reopen_fault_database(world, capture_resources)
        await capture_handler(world.handler)(action_id, runtime)
        assert len(saves) > saves_before_reopen and all(save == saves[0] for save in saves)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3 if duration_known else 4,)
        result_event, result_json = owned.connection.execute("SELECT a.result_event_id,a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone()
        assert json.loads(result_json) == result_document(world.stopper.actual[0])
        assert owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=?", (result_event,)).fetchone() == (_NOW,)
        assert owned.connection.execute("SELECT control_elapsed_ns FROM device_activities").fetchone() == (10_000_000_000 if duration_known else None,)
        assert len(world.stopper.calls) == 1
        world.driver.control.assert_awaited_once()
    finally:
        await capture_resources.close()
        await world.database.close()
        owned.connection.close()


@pytest.mark.parametrize("duration_known", [False, True])
async def test_stop_exhaustion_keeps_unknown_activity_and_complete_files(tmp_path, duration_known, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False,
        evidence_contracts=(_COMPLETION,),
        assembly_options={"stop_config": AttemptConfig(2, Decimal("1"), Decimal("0"))})
    native_device_pages(case_support)
    case_support.stopper.stop.return_value = DeviceCallResult.from_outcome(replace(
        returned("stop", "stop_confirmed", 1), effect=EffectState.UNKNOWN, observations=()))
    try:
        await capture_handler(handler)(action_id, runtime)
        if duration_known:
            case_support.clock.monotonic = 15_000_000_000
        else:
            runtime = await case_support.reopen(owned, cold=True)
        for _ in range(3):
            await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4
        assert json.loads(row[1]) == {"activity_id": "1", "reason": "completion_unknown" if duration_known else "duration_unknown"}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state FROM device_activities").fetchone() == (1, None, 1)
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert case_support.stopper.stop.await_count == 2
    finally:
        owned.connection.close()


async def test_duration_unknown_preserves_complete_files_when_listing_exhausts(tmp_path, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False,
        evidence_contracts=(_COMPLETION,),
        assembly_options={"check_config": AttemptConfig(1, Decimal("1"), Decimal("0"))})
    native_device_pages(case_support, case="read_error")
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime = await case_support.reopen(owned, cold=True)
        await capture_handler(handler)(action_id, runtime)
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4 and json.loads(row[1])["reason"] == "duration_unknown"
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (1,)
        assert owned.connection.execute("SELECT a.error_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=7").fetchone()[0] is not None
    finally:
        owned.connection.close()


async def test_stop_fact_keeps_occupancy_until_files_are_qualified(tmp_path, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(), start=False,
        evidence_contracts=(_COMPLETION,))
    native_device_pages(case_support, case="unknown_format")
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.monotonic = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state FROM device_activities").fetchone() == (3, 10_000_000_000, 1)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is not None
    finally:
        owned.connection.close()


@pytest.mark.parametrize("baseline", [False, True])
async def test_restart_without_continuous_duration_stops_and_keeps_complete_files(tmp_path, baseline, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(baseline=baseline), start=False,
        evidence_contracts=(_COMPLETION,))
    native_device_pages(case_support)
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime = await case_support.reopen(owned, cold=True)
        case_support.clock.monotonic = 100_000_000
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4
        assert json.loads(row[1]) == {"activity_id": "1", "reason": "duration_unknown"}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,completion_basis,occupancy_state FROM device_activities").fetchone() == (3, None, 1, 2)
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is None
        case_support.driver.control.assert_awaited_once()
        case_support.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_output_finalization_keeps_original_page_and_failed_goal(tmp_path, committed, capture_resources, monkeypatch):
    from camctl.persistence.business import BusinessDatabase

    world = await native_fault_world(tmp_path, HostCatalog(baseline=True), committed=committed)
    owned, action_id = world.owned, world.action_id
    original = CaptureRepository.finish_result_check
    saves = []
    signal = SimpleNamespace(calls=[])

    def record_save(repository, finish, confirm, key, owned, **options):
        saves.append((finish, confirm, key, options))
        if len(saves) == 1:
            if committed:
                world.fault.arm(signal)
            else:
                world.fault.arm(signal, "persistent")
            signal.calls.append(True)
        return original(repository, finish, confirm, key, owned, **options)

    monkeypatch.setattr(CaptureRepository, "finish_result_check", record_save)
    try:
        await capture_handler(world.handler)(action_id, world.runtime)
        await capture_resources.close()
        await world.database.close()
        world.database = await BusinessDatabase.open(world.connect)
        world.factory = await world.build_factory(world.database)
        world.runtime = await world.factory(world.database)
        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(action_id, world.runtime)
        assert world.fault.injected and saves and all(save == saves[0] for save in saves)
        finish, confirm, key, options = saves[0]
        finalized_page = options["finalized_page"]
        assert finalized_page is not None
        world.fault.enabled = False
        if committed:
            head = CaptureRepository().read_result_pages(finish.ticket, None, 1, owned).items[0].ref
            for changed in (None, head, replace(finalized_page, page_no=99)):
                rejected = original(CaptureRepository(), finish, confirm, key, owned, finalized_page=changed)
                assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        world.wall[0] = 1_750_009_000_000_000
        world.devices["cam-1"]["result_check"] = {"max_attempts": 1,
            "call_timeout_s": Decimal("0.01"), "retry_interval_s": Decimal("0")}
        saves_before_reopen = len(saves)
        assert saves_before_reopen > 0
        runtime = await reopen_fault_database(world, capture_resources)
        await capture_handler(world.handler)(action_id, runtime)
        assert len(saves) > saves_before_reopen and all(save == saves[0] for save in saves)
        status, details = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert status == 4 and json.loads(details)["reason"] == "duration_unknown"
        finalized, occupancy, basis = owned.connection.execute(
            "SELECT output_set_finalized_event_id,occupancy_state,completion_basis FROM device_activities").fetchone()
        assert finalized is not None and (occupancy, basis) == (2, 1)
        event_time, transaction_id = owned.connection.execute(
            "SELECT occurred_at,transaction_id FROM history_events WHERE id=?", (finalized,)).fetchone()
        assert event_time == finish.occurred_at
        assert owned.connection.execute("SELECT transaction_id FROM history_events WHERE id=(SELECT result_event_id"
            " FROM operation_attempts WHERE run_id=? AND attempt_no=?)", (finish.ticket.run_id,
            finish.ticket.attempt_id)).fetchone() == (transaction_id,)
        assert world.result_driver.list_results.await_count == 2
        assert len(world.stopper.calls) == 1
    finally:
        await capture_resources.close()
        await world.database.close()
        owned.connection.close()


async def test_output_finalization_refuses_unowned_original_page_members(tmp_path, native_result_case):
    from camctl.capture.result_pages import ResultPageSave
    from camctl.contracts.values import new_operation_key
    from camctl.operations.validation import validate_outcome
    from camctl.operations.attempts import AttemptFinish
    from ..support.result_cases import begin_result_attempt
    from .test_result_page_history import _CURSOR, _actual, _entry

    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(baseline=True), start=False,
        evidence_contracts=(_COMPLETION,))
    try:
        await capture_handler(handler)(action_id, runtime)
        ticket = begin_result_attempt(owned, action_id,
            AttemptConfig(3, Decimal("1.25"), Decimal("2")), case_support.clock.wall)
        repository = CaptureRepository()
        for page_no, cursor, next_cursor, name in (
                (1, None, _CURSOR, "a"), (2, _CURSOR, None, "b")):
            outcome = _actual(ticket, entries=[_entry(name)], cursor=cursor,
                next_cursor=next_cursor, finalized=page_no == 2)
            page = ResultPageSave(ticket, page_no, cursor,
                validate_outcome(ticket, outcome, case_support.evidence), case_support.clock.wall)
            saved = repository.save_result_page(page, new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        last = repository.read_last_result_page(ticket, owned)
        finish = AttemptFinish(ticket, validate_outcome(ticket, outcome, case_support.evidence),
            case_support.clock.wall, run_finish=RunFinish(RunOutcome.SUCCEEDED))
        receipt = repository.finish_result_check(finish, None, new_operation_key(), owned,
            finalized_page=last.ref)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(receipt.error, ConsistencyError)
        assert "未确认归属" in str(receipt.error)
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)
        assert owned.connection.execute("SELECT status FROM operation_attempts WHERE run_id=?",
            (ticket.run_id,)).fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [None, False, True])
async def test_finalization_repository_reuses_original_closed_page(tmp_path, committed, capture_resources, monkeypatch):
    """验证仓储的独立末页交接，不将仓储前态解释为正常会话的中间态。"""
    from camctl.capture.result_pages import OutputSetFinalizationSave
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.business import BusinessDatabase

    class PagesSaved(Exception):
        pass

    world = await native_fault_world(tmp_path, HostCatalog(baseline=True), committed=committed is True)
    owned, action_id = world.owned, world.action_id
    original_finish = CaptureRepository.finish_result_check
    captured = []

    def capture_input(repository, finish, confirm, key, owned, **options):
        captured.append((finish, confirm, key, options))
        raise PagesSaved

    try:
        await capture_handler(world.handler)(action_id, world.runtime)
        await capture_resources.close()
        await world.database.close()
        world.database = await BusinessDatabase.open(world.connect)
        world.factory = await world.build_factory(world.database)
        world.runtime = await world.factory(world.database)
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "finish_result_check", capture_input)
            with pytest.raises(ConsistencyError):
                await capture_handler(world.handler)(action_id, world.runtime)
        assert captured and all(item == captured[0] for item in captured)
        finish, confirm, finish_key, options = captured[0]
        last = options["finalized_page"]
        assert last is not None and confirm is not None
        receipt = await world.database.write(lambda db: original_finish(
            CaptureRepository(), finish, confirm, finish_key, db,
            completion_page=options.get("completion_page"), finalized_page=None),
            key=finish_key, description="保存原结果与结论")
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)
        assert owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (finish.ticket.run_id,)).fetchone() == (3,)
        request = OutputSetFinalizationSave(action_id, last, finish.occurred_at)
        key = new_operation_key()
        signal = SimpleNamespace(calls=[])
        if committed is not None:
            if committed:
                world.fault.arm(signal)
            else:
                world.fault.arm(signal, "persistent")
            signal.calls.append(True)
        result = await world.database.write(lambda db: CaptureRepository().finalize_output_set(request, key, db),
            key=key, description="交接原末页集合事实")
        if committed is not None:
            assert world.fault.injected and result.kind is DbOutcomeKind.UNKNOWN
            world.fault.enabled = False
            result = await world.database.write(lambda db: CaptureRepository().finalize_output_set(request, key, db),
                key=key, description="重送原末页集合事实")
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        head = CaptureRepository().read_result_pages(finish.ticket, None, 1, owned).items[0].ref
        for changed in (replace(request, page=head), replace(request, action_id=action_id + 1),
                        replace(request, occurred_at=request.occurred_at + 1)):
            rejected = CaptureRepository().finalize_output_set(changed, key, owned)
            assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        event_time, = owned.connection.execute("SELECT occurred_at FROM history_events"
            " WHERE id=(SELECT output_set_finalized_event_id FROM device_activities)").fetchone()
        assert event_time == finish.occurred_at
        assert world.result_driver.list_results.await_count == 2
        assert len(world.stopper.calls) == 1
    finally:
        await capture_resources.close()
        await world.database.close()
        owned.connection.close()


async def test_baseline_recording_stops_then_finalizes_scope_and_succeeds(tmp_path, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "record",
        independent_activity=True, catalog=HostCatalog(baseline=True), start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support)
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (3, None)
        activity = owned.connection.execute("SELECT activity_state,occupancy_state,output_set_finalized_event_id,completion_basis"
            " FROM device_activities").fetchone()
        assert activity[:2] == (3, 2) and activity[2] is not None and activity[3] is None
        assert runtime.capture.read_result_completion(action_id, owned) is None
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        case_support.stopper.stop.assert_awaited_once()
        assert results.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["timelapse", "cancel"])
async def test_unknown_collection_keeps_baseline_scope_after_actual_stop(tmp_path, consumer, native_result_case):
    case_support = native_result_case
    catalog = HostCatalog(baseline=True)
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=catalog, start=False,
        evidence_contracts=(_COMPLETION,),
        assembly_options={"check_config": AttemptConfig(1, Decimal("1"), Decimal("0"))})
    results = native_device_pages(case_support)
    original = results.list_results.side_effect

    async def incomplete(request, batch):
        response = await original(request, batch)
        observation, = response.outcome.observations
        return DeviceCallResult.from_outcome(replace(response.outcome, observations=(replace(
            observation, data={**observation.data, "set_finalized": False}),)))

    results.list_results.side_effect = incomplete
    try:
        await capture_handler(handler)(action_id, runtime)
        if consumer == "cancel":
            await request_cancel(runtime, action_id, case_support.clock.wall, catalog)
        else:
            runtime = await case_support.reopen(owned, cold=True)
        for _ in range(3):
            await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
            6 if consumer == "cancel" else 4,)
        assert owned.connection.execute("SELECT activity_state,occupancy_state,output_set_finalized_event_id"
            " FROM device_activities").fetchone() == (3, 1, None)
        case_support.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["timelapse", "record"])
async def test_stopped_finalized_scope_has_identical_forward_reverse_and_snapshot_history(tmp_path, consumer, native_result_case):
    from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import branch_of, business_columns, event_type_name
    from camctl.history.replay import EntityImage, RestoreSeed, restore
    from camctl.history.snapshots import SnapshotRef, decode_snapshot, prepare_snapshot
    from camctl.history.validators import ValidatedEvent
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.repositories.history import SqliteSnapshotStore

    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, consumer,
        independent_activity=True, catalog=HostCatalog(baseline=True), start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    store = SqliteSnapshotStore(path, executor)
    ref = SnapshotRef(1, action_id)
    try:
        await capture_handler(handler)(action_id, runtime)
        start_image, = await store.load_entity_images((ref,))
        await store.save_snapshots((prepare_snapshot(start_image),))
        if consumer == "record":
            case_support.clock.monotonic = 65_000_000_000
        else:
            runtime = await case_support.reopen(owned, cold=True)
        await capture_handler(handler)(action_id, runtime)
        end_image, = await store.load_entity_images((ref,))
        finalized, = owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities"
            " WHERE action_id=?", (action_id,)).fetchone()
        boundary = HistoryBoundary(*owned.connection.execute(
            "SELECT t.id,t.last_event_id FROM history_events h JOIN history_transactions t"
            " ON t.id=h.transaction_id WHERE h.id=?", (finalized,)).fetchone())
        assert start_image.boundary.last_event_id < boundary.last_event_id < end_image.boundary.last_event_id
        content, = owned.connection.execute("SELECT content FROM entity_snapshots WHERE entity_type=1"
            " AND entity_id=? AND boundary_event_id=?", (action_id, start_image.boundary.last_event_id)).fetchone()
        header, snapshot_rows = decode_snapshot(content, expect_ref=ref)

        def seed_image(records, count):
            full = {(row.table, row.row_id): dict(row.values) for row in records}
            business = {key: {name: value for name, value in values.items()
                if name in business_columns(key[0])} for key, values in full.items()}
            return EntityImage(1, action_id, True, business,
                full[("actions", action_id)]["last_event_id"], count)

        members = {(row.table, row.row_id) for row in end_image.rows}
        events = []
        for raw in owned.connection.execute(
            "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,h.clock_status,h.change_seq,h.body_json"
            " FROM history_events h JOIN entity_event_links l ON l.event_id=h.id"
            " WHERE l.entity_type=1 AND l.entity_id=? ORDER BY h.id", (action_id,)):
            event = decode_event_row(raw)
            owners = {(row.table, row.row_id): (1, action_id) for row in event.rows
                if (row.table, row.row_id) in members}
            events.append(ValidatedEvent(event, event_type_name(event.event_type),
                branch_of(event.event_type, event.reason)[0], ((1, action_id),), owners))
        forward = restore(RestoreSeed(EntityImage(1, action_id, False, {}, 0, 0), INITIAL_BOUNDARY),
            events, boundary)
        snapshot = restore(RestoreSeed(seed_image(snapshot_rows, header["change_count"]), start_image.boundary),
            events, boundary)
        reverse = restore(RestoreSeed(seed_image(end_image.rows, end_image.change_count), end_image.boundary),
            events, boundary)
        assert forward.rows == snapshot.rows == reverse.rows
        activity = forward.rows[("device_activities", 1)]
        assert activity["activity_state"] == 3 and activity["output_set_finalized_event_id"] == finalized
        assert activity["completion_evidence_json"] is None
        assert activity["control_elapsed_ns"] is None
        assert activity["occupancy_state"] == 1
        original_stop, = owned.connection.execute("SELECT a.result_event_id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?",
            (f"stop/{action_id}",)).fetchone()
        assert any(values["result_event_id"] == original_stop for (table, _), values in forward.rows.items()
            if table == "operation_attempts")
    finally:
        await executor.close()
        owned.connection.close()


async def test_host_timer_sent_margin_preserves_raw_control_elapsed(tmp_path, native_result_case):
    case_support = native_result_case
    owned, runtime, action_id, handler = await case_support.world(tmp_path, "timelapse",
        independent_activity=True, catalog=HostCatalog(startup_margin_ms=1000), start=False,
        evidence_contracts=(_COMPLETION,))
    results = native_device_pages(case_support)
    try:
        await capture_handler(handler)(action_id, runtime)
        case_support.clock.monotonic = 15000000000
        await capture_handler(handler)(action_id, runtime)
        runtime.stopper.stop.assert_not_awaited()
        case_support.clock.monotonic = 16000000000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute('SELECT control_elapsed_ns FROM device_activities').fetchone() == (11000000000,)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (action_id,)).fetchone() == (3,)
    finally:
        owned.connection.close()
