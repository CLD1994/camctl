"""基准的单次目录清单由原活动持有，可靠保存后才关闭。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

pytestmark = pytest.mark.asyncio
from support.capture_database import capture_database
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from unittest.mock import AsyncMock

from camctl.capture.baseline import PendingBaseline, PreparationPhase, prepare_baseline
from camctl.capture.baseline_models import BaselineChunkResult, BaselineRef, BaselineState
from camctl.contracts.pages import Page
from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryReader
from camctl.devices.drivers.adb_cameras.directory_scan import AdbDirectoryScan
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.file_identity import FileIdentity
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository


BINDING = DeviceBinding("cam-1", "dji-action6")
ROOTS = ("/DCIM",)
NOW = 1_700_000_000_000_000


def _world(mocker, *, scanning=True):
    directory = create_autospec(AdbCameraDriver if scanning else DirectoryReader, instance=True, spec_set=True)
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    if scanning:
        directory.directory_scan.return_value = scan
    entries = tuple(FileIdentity(BINDING, f"/DCIM/{index:03}.mp4") for index in range(136))
    cursor = DirectoryCursor(BINDING, ROOTS, 0, entries[127].path)
    reads = [DirectoryRead(None, None, Page(entries[:128], cursor)),
             DirectoryRead(None, None, Page(entries[128:], None), call_performed=False)]
    (scan if scanning else directory).read_directory.side_effect = reads
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    repository.preparation_calls_resolved.return_value = True
    state = SimpleNamespace(chunks=0, entries=0)
    repository.baseline_ref.side_effect = lambda *_: BaselineRef(1, BaselineState.COLLECTING,
        11 if state.chunks else None, 10 + state.chunks if state.chunks else None,
        state.chunks, state.entries)

    def append(request, key, owned):
        state.chunks = request.chunk_no
        state.entries += len(request.entries)
        return DbOutcome(DbOutcomeKind.COMPLETED, BaselineChunkResult(1, request.chunk_no, 10 + request.chunk_no))

    repository.append_baseline.side_effect = append
    repository.fix_baseline.side_effect = lambda request, *_: DbOutcome(
        DbOutcomeKind.COMPLETED, BaselineRef(1, BaselineState.FIXED, 11, 12, request.chunk_count, request.entry_count))
    action = {"id": 2, "device_id": BINDING.device_id, "driver_id": BINDING.driver_id,
              "scheduled_at": NOW, "max_delay_ms": 3000, "status": 2, "execution_started": 1, "cancel_requested": 0}
    activity = {"id": 1, "action_id": 2, "ownership_mode": 2, "dispatch_state": 1,
                "occupancy_state": 1, "output_scope_json": {"directories": list(ROOTS)}}
    mocker.patch("camctl.capture.baseline.row_facts", side_effect=lambda db, table, identity: action if table == "actions" else activity)
    runtime = SimpleNamespace(owned=object(), capture=repository, pending_baselines={},
                              baseline_directory=directory, baseline_timeout_s=Decimal("2"), wall_us=lambda: NOW)
    runtime.owned = OwnedConnection(create_autospec(__import__("sqlite3").Connection, instance=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime.database = capture_database(runtime.owned)
    return runtime, directory, scan, state


@pytest.mark.asyncio
async def test_136_paths_use_one_scan_and_close_after_fixed_save(mocker):
    runtime, directory, scan, state = _world(mocker)
    runtime.capture.fix_baseline.side_effect = lambda request, *_: (
        scan.close.assert_not_called() or DbOutcome(DbOutcomeKind.COMPLETED,
            BaselineRef(1, BaselineState.FIXED, 11, 12, 2, 136)))

    result = await prepare_baseline(2, 1, runtime=runtime)

    assert result.phase is PreparationPhase.READY
    assert (state.chunks, state.entries) == (2, 136)
    directory.directory_scan.assert_called_once()
    directory.read_directory.assert_not_awaited()
    assert scan.read_directory.await_count == 2
    scan.close.assert_called_once()
    assert runtime.pending_baselines == {}


@pytest.mark.asyncio
async def test_task_scoped_photo_is_ready_without_scanning_or_creating_baseline_responsibility(mocker):
    from camctl.capture import baseline

    runtime, directory, scan, _ = _world(mocker)
    action = {"id": 2, "type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO)}
    activity = {"id": 1, "action_id": 2,
        "ownership_mode": int(enum_for("device_activities.ownership_mode").TASK_SCOPE)}
    baseline.row_facts.side_effect = (
        lambda connection, table, identity: action if table == "actions" else activity)

    assert (await prepare_baseline(2, 1, runtime=runtime)).phase is PreparationPhase.READY

    assert runtime.pending_baselines == {}
    runtime.capture.baseline_ref.assert_not_called()
    directory.directory_scan.assert_not_called()
    directory.read_directory.assert_not_awaited()
    scan.read_directory.assert_not_awaited()


def _segmented_world(mocker, *, scanning=True):
    import sqlite3
    from dataclasses import replace
    from camctl.capture.baseline_models import BaselineReadCallView, BaselineReadState
    from camctl.contracts.history_values import HistoryBoundary

    runtime, directory, scan, state = _world(mocker, scanning=scanning)
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    db.execute.return_value.fetchone.return_value = None
    runtime.owned = OwnedConnection(db, runtime.owned.metadata)
    runtime.database = capture_database(runtime.owned)
    rows = mocker.patch("camctl.persistence.transaction.row_facts")
    # 两个生产入口从同一原动作及活动事实读取。
    action = {"id": 2, "device_id": BINDING.device_id, "driver_id": BINDING.driver_id,
              "scheduled_at": NOW, "max_delay_ms": 3000, "status": 2, "execution_started": 1, "cancel_requested": 0}
    activity = {"id": 1, "action_id": 2, "ownership_mode": 2,
                "dispatch_state": 1, "occupancy_state": 1, "output_scope_json": {"directories": list(ROOTS)}}
    rows.side_effect = lambda db, table, identity: action if table == "actions" else activity
    runtime.action = AsyncMock(return_value=action)
    views = {}

    def intent(request, key, owned):
        view = BaselineReadCallView(request.read_no, 100 + request.read_no,
            BaselineReadState.INTENDED, request, None, None, None, None, None,
            HistoryBoundary(1, 200))
        views[view.call_id] = view
        return DbOutcome(DbOutcomeKind.COMPLETED, view)

    def returned(request, key, owned):
        view = views[request.call_id]
        page = request.returned.page
        if page is not None and page.items:
            state.chunks += 1
            state.entries += len(page.items)
        return DbOutcome(DbOutcomeKind.COMPLETED, replace(view,
            state=BaselineReadState.RETURNED, result_event_id=150 + view.call_id,
            returned_at=request.returned_at, error=request.error,
            next_cursor=None if page is None else page.next_cursor))

    runtime.capture.save_baseline_read_intent.side_effect = intent
    runtime.capture.save_baseline_read_return.side_effect = returned
    return runtime, directory, scan, state


@pytest.mark.asyncio
async def test_segment_baseline_uses_same_scan_with_original_page_intents(mocker):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = _segmented_world(mocker)
    result = await prepare_segment_baseline(2, 1, runtime)

    assert result.phase is PreparationPhase.READY
    assert (state.chunks, state.entries) == (2, 136)
    assert runtime.capture.save_baseline_read_intent.call_count == 2
    assert runtime.capture.save_baseline_read_return.call_count == 2
    intents = [call.args[0] for call in runtime.capture.save_baseline_read_intent.call_args_list]
    assert [intent.read_no for intent in intents] == [1, 2]
    assert intents[1].cursor == scan.read_directory.call_args_list[1].args[0].cursor
    assert intents[0].call_key != intents[1].call_key
    returns = [call.args[0] for call in runtime.capture.save_baseline_read_return.call_args_list]
    assert [result.returned.call_performed for result in returns] == [True, False]
    directory.directory_scan.assert_called_once()
    directory.read_directory.assert_not_awaited()
    scan.close.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_pending_segment_baseline_uses_real_preparation_route(mocker):
    from camctl.capture.handlers import CaptureRuntime
    from camctl.devices.tasks import CaptureResultPolicy
    import sqlite3

    runtime, directory, scan, state = _segmented_world(mocker)
    action = await runtime.action(2)
    action.update(status=1, execution_started=0,
        execution_spec_json={"result_policy": CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value})
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.fetchone.return_value = (1,) if "FROM device_activities WHERE action_id" in sql else None
        return cursor
    runtime.owned.connection.execute.side_effect = execute
    result = await CaptureRuntime.prepare_baseline(runtime, 2)
    assert result.phase is PreparationPhase.READY
    assert (state.chunks, state.entries) == (2, 136)
    assert runtime.capture.save_baseline_read_intent.call_count == 2
    assert runtime.capture.save_baseline_read_return.call_count == 2
    assert action["status"] == 1 and action["execution_started"] == 0


@pytest.mark.asyncio
async def test_pause_retains_original_scan_and_second_page_cursor(mocker):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = _segmented_world(mocker)
    pending = PendingBaseline(1, BINDING, ROOTS)
    runtime.pending_baselines[2] = pending
    pages = iter(scan.read_directory.side_effect)

    async def read(request, *, stop):
        page = next(pages)
        if request.cursor is None:
            runtime.baseline_directory = None
        return page

    scan.read_directory.side_effect = read
    async def prepare():
        return await prepare_segment_baseline(2, 1, runtime)

    assert (await prepare()).phase is PreparationPhase.WAITING
    assert pending.directory_scan is scan
    scan.close.assert_not_called()
    runtime.baseline_directory = directory
    assert (await prepare()).phase is PreparationPhase.READY
    assert scan.read_directory.await_count == 2
    directory.directory_scan.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("segmented", [False, True])
async def test_last_fix_unknown_keeps_cache_and_retries_only_original_save(mocker, segmented):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = (_segmented_world(mocker) if segmented else _world(mocker))
    save = runtime.capture.fix_baseline.side_effect
    runtime.capture.fix_baseline.side_effect = lambda *_: DbOutcome(DbOutcomeKind.UNKNOWN)
    async def prepare():
        return (await prepare_segment_baseline(2, 1, runtime) if segmented
                else await prepare_baseline(2, 1, runtime=runtime))

    assert (await prepare()).phase is PreparationPhase.PENDING
    first = runtime.capture.fix_baseline.call_args
    scan.close.assert_not_called()
    runtime.capture.fix_baseline.side_effect = save
    assert (await prepare()).phase is PreparationPhase.READY
    assert runtime.capture.fix_baseline.call_args == first
    assert scan.read_directory.await_count == 2
    directory.directory_scan.assert_called_once()
    scan.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("segmented", [False, True])
async def test_read_error_keeps_scan_until_terminal_completion_is_settled(mocker, segmented):
    from camctl.capture.baseline import resume_baseline_settlement
    from camctl.capture.segment_baseline import prepare_segment_baseline
    from camctl.operations.models import ErrorValue

    runtime, directory, scan, state = (_segmented_world(mocker) if segmented else _world(mocker))
    error = ErrorValue("directory_failed", "directory")
    scan.read_directory.side_effect = None
    scan.read_directory.return_value = DirectoryRead(None, error, None)
    result = (await prepare_segment_baseline(2, 1, runtime) if segmented
              else await prepare_baseline(2, 1, runtime=runtime))
    assert result.phase is PreparationPhase.FAILED and result.error == error
    scan.close.assert_not_called()
    mocker.patch("camctl.capture.baseline.row_facts", return_value={"status": 4})
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts",
                 return_value=SimpleNamespace(not_started=False))

    await resume_baseline_settlement(2, runtime=runtime)

    scan.close.assert_called_once()
    assert runtime.pending_baselines == {}


@pytest.mark.parametrize("status", [3, 4, 5, 6])
async def test_terminal_settlement_retries_close_without_repeating_released_save(mocker, status):
    from camctl.capture.baseline import resume_baseline_settlement
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, directory, scan, state = _world(mocker)
    pending = PendingBaseline(1, BINDING, ROOTS, directory_scan=scan)
    runtime.pending_baselines[2] = pending
    mocker.patch("camctl.capture.baseline.row_facts", return_value={"status": status})
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", return_value=SimpleNamespace(
        not_started=True, activity={"ownership_mode": 2, "occupancy_state": 1}))
    runtime.capture.release_occupancy.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))
    scan.close.side_effect = [OSError("close failed"), None]
    with pytest.raises(OSError):
        await resume_baseline_settlement(2, runtime=runtime)
    assert runtime.pending_baselines[2] is pending

    assert (await resume_baseline_settlement(2, runtime=runtime)).phase is PreparationPhase.READY
    runtime.capture.release_occupancy.assert_called_once()
    assert scan.close.call_count == 2
    assert runtime.pending_baselines == {}


@pytest.mark.asyncio
async def test_cold_intended_owner_does_not_create_scan_or_repeat_device_call(mocker):
    from camctl.capture.baseline_models import BaselineReadIntent, BaselineReadCallView, BaselineReadState
    from camctl.capture.baseline_reads import PendingBaselineRead
    from camctl.capture.segment_baseline import prepare_segment_baseline
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.contracts.values import new_operation_key

    runtime, directory, scan, state = _segmented_world(mocker)
    intent = BaselineReadIntent(2, 1, BINDING, ROOTS, None, Decimal("2"),
        runtime.capture.baseline_ref(1, runtime.owned), 1, new_operation_key(), new_operation_key(), NOW)
    view = BaselineReadCallView(1, 1, BaselineReadState.INTENDED, intent,
                               None, None, None, None, None, HistoryBoundary(1, 1))
    owner = PendingBaselineRead(intent, intent_view=view, locally_created=False)
    runtime.pending_baselines[2] = PendingBaseline(1, BINDING, ROOTS, read_owner=owner)

    assert (await prepare_segment_baseline(2, 1, runtime)).phase is PreparationPhase.WAITING

    assert runtime.pending_baselines[2].read_owner is owner
    directory.directory_scan.assert_not_called()
    directory.read_directory.assert_not_awaited()
    scan.read_directory.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_directories_and_owner_replacement_keep_original_scan(mocker):
    from camctl.devices.directory import DirectoryRequest

    runtime, directory, scan, state = _world(mocker)
    pending = PendingBaseline(1, BINDING, ("/DCIM", "/SD/DCIM"))
    cursor = DirectoryCursor(BINDING, pending.directories, 1, None)
    scan.read_directory.side_effect = [DirectoryRead(None, None, Page((), cursor)),
                                       DirectoryRead(None, None, Page((), None))]
    first = DirectoryRequest(BINDING, pending.directories, None, 128, Decimal("2"))
    await pending.read_directory(first, directory=directory, stop=object())
    replacement = create_autospec(AdbCameraDriver, instance=True, spec_set=True)
    second = DirectoryRequest(BINDING, pending.directories, cursor, 128, Decimal("2"))
    await pending.read_directory(second, directory=replacement, stop=object())
    pending.close()

    directory.directory_scan.assert_called_once_with(first)
    replacement.directory_scan.assert_not_called()
    assert scan.read_directory.call_args_list[1].args[0] == second
    scan.close.assert_called_once()


@pytest.mark.asyncio
async def test_segment_unknown_return_save_retains_original_keys_and_scan(mocker):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = _segmented_world(mocker)
    save = runtime.capture.save_baseline_read_return.side_effect
    runtime.capture.save_baseline_read_return.side_effect = lambda *_: DbOutcome(DbOutcomeKind.UNKNOWN)
    assert (await prepare_segment_baseline(2, 1, runtime)).phase is PreparationPhase.PENDING
    first = runtime.capture.save_baseline_read_return.call_args
    assert scan.read_directory.await_count == 1
    scan.close.assert_not_called()
    runtime.capture.save_baseline_read_return.side_effect = save

    assert (await prepare_segment_baseline(2, 1, runtime)).phase is PreparationPhase.READY

    assert runtime.capture.save_baseline_read_return.call_args_list[1] == first
    assert scan.read_directory.await_count == 2
    directory.directory_scan.assert_called_once()
    scan.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("segmented", [False, True])
async def test_ports_without_scan_keep_paged_contract(mocker, segmented):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = (_segmented_world(mocker, scanning=False) if segmented
                                      else _world(mocker, scanning=False))
    result = (await prepare_segment_baseline(2, 1, runtime) if segmented
              else await prepare_baseline(2, 1, runtime=runtime))
    assert result.phase is PreparationPhase.READY
    assert directory.read_directory.await_count == 2
    scan.close.assert_not_called()
    assert state.entries == 136


@pytest.mark.asyncio
async def test_unknown_page_save_keeps_scan_without_reading_again(mocker):
    runtime, directory, scan, state = _world(mocker)
    append = runtime.capture.append_baseline.side_effect
    runtime.capture.append_baseline.side_effect = lambda *_: DbOutcome(DbOutcomeKind.UNKNOWN)
    assert (await prepare_baseline(2, 1, runtime=runtime)).phase is PreparationPhase.PENDING
    owner = runtime.pending_baselines[2]
    assert scan.read_directory.await_count == 1
    scan.close.assert_not_called()
    runtime.capture.append_baseline.side_effect = append

    assert (await prepare_baseline(2, 1, runtime=runtime)).phase is PreparationPhase.READY

    assert directory.directory_scan.call_count == 1
    assert scan.read_directory.await_count == 2
    assert owner.directory_scan is None


@pytest.mark.asyncio
@pytest.mark.parametrize("segmented", [False, True])
async def test_close_failure_preserves_fixed_owner_and_retries_without_device_call(mocker, segmented):
    from camctl.capture.segment_baseline import prepare_segment_baseline

    runtime, directory, scan, state = (_segmented_world(mocker) if segmented else _world(mocker))
    async def prepare():
        return (await prepare_segment_baseline(2, 1, runtime) if segmented
                else await prepare_baseline(2, 1, runtime=runtime))
    scan.close.side_effect = [OSError("close failed"), None]
    with pytest.raises(OSError):
        await prepare()
    assert runtime.pending_baselines[2].directory_scan is scan

    assert (await prepare()).phase is PreparationPhase.READY

    assert scan.read_directory.await_count == 2
    assert runtime.capture.fix_baseline.call_count == 1
    assert scan.close.call_count == 2
    assert runtime.pending_baselines == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_control_preparation_settles_cancel_or_expiry_after_actual_page(
        mocker, segmented, canceled, close_fails):
    from camctl.capture.baseline import resume_baseline_settlement
    from camctl.capture.handlers import _baseline_before_control
    from camctl.capture.segment_baseline import prepare_segment_baseline
    from camctl.devices.tasks import CaptureResultPolicy
    from camctl.scheduling.rules import LaunchWindow

    runtime, directory, scan, state = (_segmented_world(mocker) if segmented else _world(mocker))
    action = await runtime.action(2) if segmented else None
    if not segmented:
        action = {"id": 2, "device_id": BINDING.device_id, "driver_id": BINDING.driver_id,
                  "scheduled_at": NOW, "max_delay_ms": 3000, "status": 2, "execution_started": 1, "cancel_requested": 0}
        activity = {"id": 1, "action_id": 2, "ownership_mode": 2, "dispatch_state": 1,
                    "occupancy_state": 1, "output_scope_json": {"directories": list(ROOTS)}}
        mocker.patch("camctl.capture.baseline.row_facts",
                     side_effect=lambda db, table, identity: action if table == "actions" else activity)
    action["execution_spec_json"] = ({"result_policy": CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value}
                                     if segmented else {})
    action["execution_started"] = 0
    action["status"] = 1
    runtime.action = AsyncMock(return_value=action)
    current_time = [NOW]
    runtime.wall_us = lambda: current_time[0]
    runtime.window_of = lambda _: LaunchWindow(NOW, NOW + 3_000_000)
    pages = iter(scan.read_directory.side_effect)

    async def read(request, *, stop):
        result = next(pages)
        if canceled:
            action["cancel_requested"] = 1
        else:
            current_time[0] = NOW + 4_000_000
        return result

    async def prepare(_):
        return (await prepare_segment_baseline(2, 1, runtime) if segmented
                else await prepare_baseline(2, 1, runtime=runtime))

    async def complete(request):
        scan.close.assert_not_called()
        action["status"] = 6 if canceled else 5

    scan.read_directory.side_effect = read
    runtime.prepare_baseline = prepare
    runtime.save_capture_completion = complete
    async def resume(identity):
        return await resume_baseline_settlement(identity, runtime=runtime)

    runtime.resume_baselines = resume
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts",
                 return_value=SimpleNamespace(not_started=False))
    if segmented:
        mocker.patch("camctl.capture.baseline.row_facts", return_value=action)
    if close_fails:
        scan.close.side_effect = [OSError("close failed"), None]
        with pytest.raises(OSError):
            await _baseline_before_control(runtime, action)
        assert action["status"] == (6 if canceled else 5)
        assert 2 in runtime.pending_baselines
        await runtime.resume_baselines(2)
    else:
        result = await _baseline_before_control(runtime, action)
        assert result.phase == "not_granted"
    assert runtime.pending_baselines == {}
    assert scan.read_directory.await_count == 1
    assert state.entries == 128


@pytest.mark.asyncio
async def test_segment_owner_cannot_reuse_scan_for_another_activity(mocker):
    from camctl.capture.segment_baseline import prepare_segment_baseline
    from camctl.contracts.values import ConsistencyError

    runtime, directory, scan, state = _segmented_world(mocker)
    runtime.pending_baselines[2] = PendingBaseline(1, BINDING, ROOTS, directory_scan=scan)
    runtime.capture.baseline_ref.side_effect = lambda activity_id, _: BaselineRef(
        activity_id, BaselineState.COLLECTING, None, None, 0, 0)

    with pytest.raises(ConsistencyError):
        await prepare_segment_baseline(2, 3, runtime)

    scan.read_directory.assert_not_awaited()
    directory.directory_scan.assert_not_called()
