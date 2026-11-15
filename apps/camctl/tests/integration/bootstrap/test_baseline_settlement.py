"""默认共同 flow 保存原基准申请，真实调用与冷恢复分别保留占用。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import sqlite3

import pytest
import pytest_asyncio

from camctl.capture import baseline
from camctl.capture.baseline_models import BaselineReadIntent, BaselineReadReturn
from camctl.capture.handlers import capture_handler
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead, DirectoryCursor
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.repositories.scheduling import ExpireActionRequest, SchedulingRepository, StartActionRequest
from camctl.session.service import StateDbFailure

from ..capture.result_consumer_fixtures import ResultCatalog
from ..capture.test_baseline_history import _entries
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_baseline_prepare import Directory
from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan
from ..support.bootstrap_capture_cases import bootstrap_case, SavedBoundary
from ..support.session import session_config

pytestmark = pytest.mark.asyncio


@pytest.fixture
def owned(tmp_path):
    from pathlib import Path
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig

    config = session_config(tmp_path)
    path = Path(config.paths.state_db)
    assert initialize_state(config, path).outcome is InitOutcome.CREATED
    connection = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        yield connection
    finally:
        connection.connection.close()


@pytest_asyncio.fixture(autouse=True)
async def no_capture_start(bootstrap_case):
    yield
    case = bootstrap_case
    if case.owned is not None:
        assert case.driver.control.call_count == 0
        assert await case.database.read(lambda current: current.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts").fetchone()) == (0,)


class BaselineCatalog(ResultCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(factory(params),
            ownership_mode=2, output_scope={"directories": ["/DCIM"]}))


async def _world(case, tmp_path, directory=None, *, segmented=False):
    directory = directory or Directory([lambda request: DirectoryRead(None, None, Page((), None))])
    runtime = await case.world(tmp_path, directory=directory, default=True,
        catalog=SegmentWaitCatalog() if segmented else BaselineCatalog(), max_delay_ms=5000)
    case.segmented = segmented
    return runtime


async def _prepare(case, runtime):
    if case.segmented:
        return await capture_handler(case.handler)(case.action_id, runtime)
    return await baseline.prepare_baseline(case.action_id, case.activity_id, runtime=runtime)


def _saved(case, name):
    return [(request, key) for method, request, key in case.saves if method == name]


def _call(case):
    return case.owned.connection.execute(
        "SELECT call_key,result_save_key,state,returned_at,error_json FROM baseline_read_calls").fetchone()


async def test_cancel_chain_saves_original_page_before_capture_terminal(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, Directory([lambda request: DirectoryRead(None, None,
        Page(_entries("a", driver="camctl-adb", directory="/DCIM"), None))]))
    def unknown(name, request, key, receipt):
        return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原页回执未知")) if name == "append_baseline" else receipt
    case.after_save = unknown
    assert (await _prepare(case, runtime)).phase is baseline.PreparationPhase.PENDING
    original, key = _saved(case, "append_baseline")[0]
    await case.cancel()
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
    case.after_save = None
    await case.common_round()
    await capture_handler(case.handler)(case.action_id, await case.view())
    await case.common_round()
    assert case.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?",
        (case.action_id,)).fetchone() == (6, 1)
    assert case.owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)
    assert len(case.directory.calls) == 1
    recovered = CaptureRepository().append_baseline(original, key, case.owned)
    assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error


async def test_common_resume_closes_terminal_preparation_after_original_page(tmp_path, bootstrap_case):
    """持久状态消费者：合法终态已经保存，原实际页仍承担回执核实责任。"""
    case = bootstrap_case
    runtime = await _world(case, tmp_path, Directory([lambda request: DirectoryRead(None, None,
        Page(_entries("a", driver="camctl-adb", directory="/DCIM"), None))]))
    case.after_save = lambda name, request, key, receipt: (
        DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原页回执未知")) if name == "append_baseline" else receipt)
    assert (await _prepare(case, runtime)).phase is baseline.PreparationPhase.PENDING
    original, key = _saved(case, "append_baseline")[0]
    await case.cancel()
    # 正式终态仓储建立消费者输入，不代表默认取消流程会先保存终态。
    terminal = CaptureRepository().finish_canceled_capture(FinishCanceledCapture(
        case.action_id, case.clock.wall, unstarted=True), new_operation_key(), case.owned)
    assert terminal.kind is DbOutcomeKind.COMPLETED, terminal.error
    assert case.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?",
        (case.action_id,)).fetchone() == (6, 1)
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
    case.after_save = None
    await case.common_round()
    assert case.owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)
    assert len(case.directory.calls) == 1
    recovered = CaptureRepository().append_baseline(original, key, case.owned)
    assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("canceled", [False, True])
async def test_common_resume_preserves_active_directory_owner(tmp_path, bootstrap_case, segmented, canceled):
    case = bootstrap_case
    entered, released = asyncio.Event(), asyncio.Event()
    class HeldDirectory:
        calls = []
        async def read_directory(self, request, *, stop):
            self.calls.append(request)
            entered.set()
            await released.wait()
            return DirectoryRead(None, None, Page(_entries("a", driver=request.binding.driver_id,
                directory=request.directories[0]), None))
    runtime = await _world(case, tmp_path, HeldDirectory(), segmented=segmented)
    case.boundary = "fix_baseline" if segmented and not canceled else None
    task = asyncio.create_task(_prepare(case, runtime))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if canceled:
            await case.cancel()
        await case.common_round()
        await case.common_round()
        assert not task.done() and len(case.directory.calls) == 1
        assert case.owned.connection.execute("SELECT occupancy_state,dispatch_state FROM device_activities").fetchone() == (1, 1)
        assert case.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
        released.set()
        if segmented and not canceled:
            with pytest.raises(SavedBoundary):
                await asyncio.wait_for(task, 1)
        else:
            result = await asyncio.wait_for(task, 1)
            if not segmented:
                assert result.phase is (baseline.PreparationPhase.ABORTED if canceled else baseline.PreparationPhase.READY)
        if canceled and not segmented:
            await capture_handler(case.handler)(case.action_id, await case.view())
        await case.common_round()
        assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == ((2,) if canceled else (1,))
        assert len(case.directory.calls) == 1
    finally:
        released.set()
        if not task.done():
            await task


async def test_common_resume_retains_real_baseline_save_failure(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, Directory([lambda request: DirectoryRead(None, None,
        Page(_entries("a", driver="camctl-adb", directory="/DCIM"), None))]))
    case.fault.table, case.fault.operation, case.fault.enabled = "history_events", sqlite3.SQLITE_INSERT, True
    assert (await _prepare(case, runtime)).phase is baseline.PreparationPhase.PENDING
    original, key = _saved(case, "append_baseline")[0]
    with pytest.raises(StateDbFailure):
        await case.common_round()
    assert len(case.directory.calls) == 1 and case.fault.hits
    case.fault.enabled = False
    await case.common_round()
    saved = CaptureRepository().append_baseline(original, key, case.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert len(case.directory.calls) == 1


async def test_common_resume_retries_original_segment_read_save(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, segmented=True)
    case.fault.table, case.fault.operation, case.fault.enabled = "baseline_read_calls", sqlite3.SQLITE_UPDATE, True
    with pytest.raises(ConsistencyError):
        await _prepare(case, runtime)
    original, key = _saved(case, "save_baseline_read_return")[0]
    with pytest.raises(StateDbFailure):
        await case.common_round()
    case.fault.enabled = False
    await case.common_round()
    case.boundary = "fix_baseline"
    with pytest.raises(SavedBoundary):
        await _prepare(case, await case.view())
    assert _call(case)[2] == 2 and len(case.directory.calls) == 1
    saved = CaptureRepository().save_baseline_read_return(original, key, case.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error


async def _unstarted(case, tmp_path):
    runtime = await _world(case, tmp_path, segmented=True)
    case.fault.table, case.fault.operation, case.fault.enabled = "baseline_read_calls", sqlite3.SQLITE_INSERT, True
    with pytest.raises(ConsistencyError):
        await _prepare(case, runtime)
    original, key = _saved(case, "save_baseline_read_intent")[0]
    assert _call(case) is None and case.directory.calls == []
    case.fault.enabled = False
    await case.common_round()
    assert _call(case)[:3] == (str(original.call_key), str(original.result_save_key), 1)
    return original, key


async def test_segment_intent_save_recovery_continues_same_unstarted_owner(tmp_path, bootstrap_case):
    case = bootstrap_case
    original, key = await _unstarted(case, tmp_path)
    case.boundary = "fix_baseline"
    with pytest.raises(SavedBoundary):
        await _prepare(case, await case.view())
    assert _call(case)[:3] == (str(original.call_key), str(original.result_save_key), 2)
    assert len(case.directory.calls) == 1


async def test_segment_window_ending_during_intent_save_closes_without_read(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, segmented=True)
    def slow(name, request, key, receipt):
        if name == "save_baseline_read_intent" and receipt.kind is DbOutcomeKind.COMPLETED:
            case.clock.wall += 6_000_000
        return receipt
    case.after_save = slow
    await _prepare(case, runtime)
    assert _call(case)[2:] == (4, None, None)
    assert case.owned.connection.execute("SELECT status FROM actions").fetchone() == (5,)
    await case.common_round()
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
    assert case.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    assert case.directory.calls == []


@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancel_closes_unstarted_segment_intent_before_terminal(tmp_path, bootstrap_case, save_fails):
    case = bootstrap_case
    original, key = await _unstarted(case, tmp_path)
    await case.cancel()
    if save_fails:
        case.fault.table, case.fault.operation, case.fault.enabled = "baseline_read_calls", sqlite3.SQLITE_UPDATE, True
        with pytest.raises(ConsistencyError):
            await capture_handler(case.handler)(case.action_id, await case.view())
        assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
        assert case.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (case.action_id,)).fetchone() == (2,)
        assert _call(case)[2] == 1
        case.fault.enabled = False
    await capture_handler(case.handler)(case.action_id, await case.view())
    await case.common_round()
    assert case.owned.connection.execute("SELECT status FROM actions WHERE id=?", (case.action_id,)).fetchone() == (6,)
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
    assert _call(case) == (str(original.call_key), str(original.result_save_key), 4, None, None)
    assert case.directory.calls == []
    assert case.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


async def test_not_called_unknown_receipt_keeps_original_request_until_cancel_and_release(tmp_path, bootstrap_case):
    case = bootstrap_case
    original, _ = await _unstarted(case, tmp_path)
    await case.cancel()
    first_time = case.clock.wall
    def lose_receipt(name, request, key, receipt):
        if name == "save_baseline_read_not_called":
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原结果回执未知"))
        return receipt
    case.after_save = lose_receipt
    with pytest.raises(ConsistencyError):
        await capture_handler(case.handler)(case.action_id, await case.view())
    assert _call(case)[2:] == (4, None, None)
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
    first, key = _saved(case, "save_baseline_read_not_called")[0]
    assert first.occurred_at == first_time and str(key) == str(original.result_save_key)
    case.after_save = None
    case.clock.wall += 100
    await capture_handler(case.handler)(case.action_id, await case.view())
    await case.common_round()
    assert all(item == (first, key) for item in _saved(case, "save_baseline_read_not_called"))
    assert CaptureRepository().verify_baseline_read_write(first, key, case.owned).state.name == "NOT_CALLED"
    assert case.owned.connection.execute("SELECT status FROM actions").fetchone() == (6,)
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
    assert case.directory.calls == []


async def test_cold_resume_releases_terminal_not_called_scope(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, segmented=True)
    def slow(name, request, key, receipt):
        if name == "save_baseline_read_intent" and receipt.kind is DbOutcomeKind.COMPLETED:
            case.clock.wall += 6_000_000
        return receipt
    case.after_save = slow
    await _prepare(case, runtime)
    assert _call(case)[2:] == (4, None, None)
    assert case.owned.connection.execute("SELECT status FROM actions").fetchone() == (5,)
    await case.reconnect(cold=True)
    await case.common_round()
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
    assert case.directory.calls == []


async def test_start_entry_rejects_not_executed_baseline_save_without_error(tmp_path, bootstrap_case):
    case = bootstrap_case
    runtime = await _world(case, tmp_path, segmented=True)
    case.before_save = lambda name, request, key: (DbOutcome(DbOutcomeKind.NOT_EXECUTED)
        if name == "save_baseline_read_intent" else None)
    with pytest.raises(ConsistencyError):
        await _prepare(case, runtime)
    original, key = _saved(case, "save_baseline_read_intent")[0]
    assert str(key) == str(original.call_key)
    assert _call(case) is None and case.directory.calls == []
    assert case.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


@pytest.mark.parametrize("read_failed", [False, True])
async def test_cold_resume_releases_terminal_unstarted_scope_after_saved_read(tmp_path, bootstrap_case, read_failed):
    case = bootstrap_case
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "read_failed"})
    def returned(request):
        case.clock.wall += 6_000_000
        entries = _entries("a", driver=request.binding.driver_id, directory=request.directories[0])
        return (DirectoryRead(None, error, None) if read_failed else DirectoryRead(None, None,
            Page(entries, DirectoryCursor(request.binding, request.directories, 0, entries[-1].path))))
    runtime = await _world(case, tmp_path, Directory([returned]), segmented=True)
    await _prepare(case, runtime)
    assert _call(case)[2] == 2
    assert (parse_exact_json(_call(case)[4]) if read_failed else _call(case)[4]) == (
        {"code": error.code, "stage": error.stage, "details": dict(error.details)} if read_failed else None)
    await case.reconnect(cold=True)
    await case.common_round()
    assert case.owned.connection.execute("SELECT status FROM actions").fetchone() == (5,)
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
    assert case.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    assert len(case.directory.calls) == 1


async def test_cold_resume_keeps_unreturned_baseline_intent_occupied(tmp_path, bootstrap_case):
    case = bootstrap_case
    await _unstarted(case, tmp_path)
    expired = SchedulingRepository().expire_action(ExpireActionRequest(case.action_id,
        case.clock.wall + 6_000_000, case.clock.wall + 6_000_000), new_operation_key(), case.owned)
    assert expired.kind is DbOutcomeKind.COMPLETED, expired.error
    await case.reconnect(cold=True)
    await case.common_round()
    assert case.owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
    assert _call(case)[2] == 1 and case.directory.calls == []


@pytest.mark.parametrize("local_first", [False, True])
async def test_cold_resume_exhausts_batches_without_taking_local_owner(owned, tmp_path, bootstrap_case, local_first):
    from pathlib import Path
    from unittest.mock import AsyncMock, patch
    from camctl.bootstrap.config import ConfigDefaults, load_config
    from camctl.outputs import read_discovery
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE
    from ..support.bootstrap_capture_cases import StopPort
    case = bootstrap_case
    capture, identities = CaptureRepository(), []
    for number in (1, 2):
        device = f"cam-{number}"
        body = _plan_body(request_id=str(40 + number))
        body["actions"][0].update(type="camera_timelapse", device_id=device,
            params=_params("dji-action6", "camera_timelapse"))
        await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", device_id=device))
        action = owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]
        started = SchedulingRepository().start_action(StartActionRequest(
            action, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        activity = started.value.activity_id
        binding = DeviceBinding(device, "dji-action6")
        scope = parse_exact_json(owned.connection.execute("SELECT output_scope_json FROM device_activities WHERE id=?", (activity,)).fetchone()[0])
        intent = BaselineReadIntent(action, activity, binding, tuple(scope["directories"]), None,
            Decimal("10"), capture.baseline_ref(activity, owned), 1,
            new_operation_key(), new_operation_key(), _SCHEDULED)
        saved = capture.save_baseline_read_intent(intent, intent.call_key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "read_failed"})
        returned = capture.save_baseline_read_return(BaselineReadReturn(saved.value.call_id,
            _SCHEDULED + 1, error, DirectoryRead(None, error, None)), intent.result_save_key, owned)
        assert returned.kind is DbOutcomeKind.COMPLETED, returned.error
        identities.append((action, activity))
    entered, released = asyncio.Event(), asyncio.Event()
    class HeldDirectory:
        calls = []
        async def read_directory(self, request, *, stop):
            self.calls.append(request)
            entered.set()
            await released.wait()
            return DirectoryRead(None, None, Page((), None))
    case.owned, case.path = owned, Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    case.config = load_config({"paths": {"state_db": str(case.path),
        "log_file": str(tmp_path / "camctl.log"),
        "staging": owned.metadata.staging_path, "ready": owned.metadata.ready_path,
        "processing": owned.metadata.processing_path}, "devices": {"cam-1": {
            "kind": "camera", "driver": "dji-action6"}}}, ConfigDefaults())
    case.driver_id, case.catalog = "dji-action6", ResultCatalog()
    case.driver, case.stopper = SimpleNamespace(control=AsyncMock()), StopPort(None)
    case.directory, case.results, case.evidence = HeldDirectory(), None, RESULT_EVIDENCE
    case.clock.wall = int(_SCHEDULED)
    await case.open_database()
    await case.assemble_default()
    task = None
    if local_first:
        runtime = await case.view()
        task = asyncio.create_task(baseline.prepare_baseline(*identities[0], runtime=runtime))
        await asyncio.wait_for(entered.wait(), 1)
    try:
        for action, _activity_id in identities:
            saved = SchedulingRepository().expire_action(ExpireActionRequest(action,
                _SCHEDULED + 6_000_000, _SCHEDULED + 6_000_000), new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        with patch.object(read_discovery, "DEFAULT_BATCH_SIZE", 1):
            await case.common_round()
        assert owned.connection.execute("SELECT occupancy_state FROM device_activities ORDER BY action_id").fetchall() == (
            [(1,), (2,)] if local_first else [(2,), (2,)])
        assert len(case.directory.calls) == int(local_first)
    finally:
        released.set()
        if task is not None:
            await task
            await case.common_round()
            assert owned.connection.execute("SELECT occupancy_state FROM device_activities ORDER BY action_id").fetchall() == [(2,), (2,)]
