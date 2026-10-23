"""准备原页保存及终态占用由默认共同恢复入口处理。"""
import asyncio
from types import SimpleNamespace

import pytest

from camctl.bootstrap.lifecycle import _resume_capture_requests
from camctl.capture import baseline
from camctl.contracts.pages import Page
from camctl.contracts.values import new_operation_key
from camctl.devices.directory import DirectoryRead
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from ..capture.test_baseline_history import _activity, _entries
from ..capture.test_baseline_prepare import Directory, _runtime
from ..scheduling.test_start_action import _SCHEDULED, owned

pytestmark = pytest.mark.asyncio


async def test_common_resume_closes_terminal_preparation_after_original_page(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    calls = []
    class Repository(CaptureRepository):
        def append_baseline(self, request, key, owned):
            calls.append((request, key))
            result = super().append_baseline(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原页回执未知")) if len(calls) == 1 else result
    runtime = _runtime(owned, Directory([lambda r: DirectoryRead(None, None, Page(_entries("a"), None))]), Repository())
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
    owned.connection.commit()
    result = CaptureRepository().finish_canceled_capture(FinishCanceledCapture(1, _SCHEDULED, unstarted=True), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
    deps = SimpleNamespace(capture_baselines=runtime.pending_baselines, capture_result_closes={},
                           capture_completions={}, capture_retry_gate=None, capture_result_scans={},
                           capture_segment_writes={})
    _resume_capture_requests(deps, owned)
    assert owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)
    assert runtime.pending_baselines == {} and len(runtime.baseline_directory.calls) == 1


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("canceled", [False, True])
async def test_common_resume_preserves_active_directory_owner(
    owned, tmp_path, segmented, canceled
):
    """跨流程检查不能把未返回的目录调用当成保存故障或终态收场。"""
    entered, released = asyncio.Event(), asyncio.Event()

    class HeldDirectory:
        calls = 0

        async def read_directory(self, request, *, stop):
            self.calls += 1
            entered.set()
            await released.wait()
            entries = _entries(
                "a", device=request.binding.device_id, driver=request.binding.driver_id,
                directory=request.directories[0],
            )
            return DirectoryRead(None, None, Page(entries, None))

    directory = HeldDirectory()
    if segmented:
        from ..capture.result_consumer_fixtures import consumer_world
        from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog

        current, runtime, action_id, _ = await consumer_world(
            tmp_path / "segmented", "record", catalog=SegmentWaitCatalog(), start=False
        )
        runtime.baseline_directory = directory
        prepare = runtime.prepare_baseline(action_id)
    else:
        current, action_id = owned, 1
        activity_id = await _activity(current, tmp_path)
        runtime = _runtime(current, directory)
        prepare = baseline.prepare_baseline(action_id, activity_id, runtime=runtime)
    deps = SimpleNamespace(
        capture_baselines=runtime.pending_baselines, capture_result_closes={},
        capture_completions={}, capture_retry_gate=None, capture_result_scans={},
        capture_segment_writes={},
    )
    task = asyncio.create_task(prepare)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if canceled:
            if segmented:
                from ..capture.test_record_segment_scope_recovery import _cancel

                _cancel(current, runtime, action_id)
            else:
                current.connection.execute(
                    "UPDATE actions SET cancel_requested=1 WHERE id=?", (action_id,)
                )
                current.connection.commit()
                saved = runtime.capture.finish_canceled_capture(
                    FinishCanceledCapture(action_id, runtime.wall_us(), unstarted=True),
                    new_operation_key(), current,
                )
                assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        _resume_capture_requests(deps, current)
        _resume_capture_requests(deps, current)
        if segmented:
            runtime.resume_baselines(action_id)
        assert not task.done()
        assert directory.calls == 1
        assert current.connection.execute(
            "SELECT occupancy_state,dispatch_state FROM device_activities"
        ).fetchone() == (1, 1)
        assert current.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts"
        ).fetchone() == (0,)
        released.set()
        result = await asyncio.wait_for(task, 1)
        assert result.phase is (
            baseline.PreparationPhase.ABORTED if canceled else baseline.PreparationPhase.READY
        )
        if segmented and canceled:
            from camctl.capture.handlers import _finish_canceled_capture

            _finish_canceled_capture(runtime, action_id, unstarted=True)
        _resume_capture_requests(deps, current)
        assert runtime.pending_baselines == {}
        assert current.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == ((2,) if canceled else (1,))
        assert directory.calls == 1
    finally:
        released.set()
        await task
        if segmented:
            current.connection.close()


async def test_common_resume_retains_real_baseline_save_failure(owned, tmp_path):
    """真正未完成的原保存仍然阻止后续业务，保留原申请和操作键。"""
    from camctl.contracts.values import ConsistencyError

    activity_id = await _activity(owned, tmp_path)

    class UnavailableSave(CaptureRepository):
        def append_baseline(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("save unavailable"))

    runtime = _runtime(
        owned, Directory([lambda request: DirectoryRead(None, None, Page(_entries("a"), None))]),
        UnavailableSave(),
    )
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    original = runtime.pending_baselines[1].saves.pending
    # 共同入口使用生产仓储；注入同一真实保存故障，不能因此放行或换键。
    from unittest.mock import patch

    with patch.object(CaptureRepository, "append_baseline", UnavailableSave.append_baseline):
        with pytest.raises(ConsistencyError):
            _resume_capture_requests(SimpleNamespace(capture_baselines=runtime.pending_baselines), owned)
    assert runtime.pending_baselines[1].saves.pending == original
    assert len(runtime.baseline_directory.calls) == 1


async def test_common_resume_retries_original_segment_read_save(tmp_path):
    from unittest.mock import patch

    from camctl.contracts.values import ConsistencyError
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog

    class UnavailableReturn(CaptureRepository):
        def save_baseline_read_return(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("return unavailable"))

    current, runtime, action_id, _ = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    runtime.capture = UnavailableReturn()
    deps = SimpleNamespace(
        capture_baselines=runtime.pending_baselines, capture_result_closes={},
        capture_completions={}, capture_retry_gate=None, capture_result_scans={},
        capture_segment_writes={},
    )
    try:
        assert (await runtime.prepare_baseline(action_id)).phase is baseline.PreparationPhase.PENDING
        owner = runtime.pending_baselines[action_id].read_owner
        original = owner.saves.pending
        with patch.object(CaptureRepository, "save_baseline_read_return", UnavailableReturn.save_baseline_read_return):
            with pytest.raises(ConsistencyError):
                _resume_capture_requests(deps, current)
        assert owner.saves.pending == original
        _resume_capture_requests(deps, current)
        runtime.capture = CaptureRepository()
        assert (await runtime.prepare_baseline(action_id)).phase is baseline.PreparationPhase.READY
        assert current.connection.execute(
            "SELECT COUNT(*) FROM baseline_read_calls"
        ).fetchone() == (1,)
        assert len(runtime.baseline_directory.calls) == 1
    finally:
        current.connection.close()


async def _unstarted_segment_intent(tmp_path):
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog

    class UnavailableIntent(CaptureRepository):
        def save_baseline_read_intent(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("intent unavailable"))

    current, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    runtime.capture = UnavailableIntent()
    assert (await runtime.prepare_baseline(action_id)).phase is baseline.PreparationPhase.PENDING
    original = runtime.pending_baselines[action_id].read_owner.saves.pending
    deps = SimpleNamespace(
        capture_baselines=runtime.pending_baselines, capture_result_closes={},
        capture_completions={}, capture_retry_gate=None, capture_result_scans={},
        capture_segment_writes={},
    )
    _resume_capture_requests(deps, current)
    runtime.capture = CaptureRepository()
    return current, runtime, action_id, handler, original


async def test_segment_intent_save_recovery_continues_same_unstarted_owner(tmp_path):
    current, runtime, action_id, _, original = await _unstarted_segment_intent(tmp_path)
    try:
        assert (await runtime.prepare_baseline(action_id)).phase is baseline.PreparationPhase.READY
        assert current.connection.execute(
            "SELECT call_key,result_save_key,state FROM baseline_read_calls"
        ).fetchone() == (str(original.request.call_key), str(original.request.result_save_key), 2)
        assert len(runtime.baseline_directory.calls) == 1
    finally:
        current.connection.close()


async def test_segment_window_ending_during_intent_save_closes_without_read(tmp_path):
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog
    from camctl.capture.handlers import capture_handler

    current, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    now = [runtime.wall_us()]

    class SlowIntent(CaptureRepository):
        def save_baseline_read_intent(self, request, key, owned):
            saved = super().save_baseline_read_intent(request, key, owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            now[0] += 6_000_000
            return saved

    runtime.capture = SlowIntent()
    runtime.wall_us = lambda: now[0]
    try:
        assert (await runtime.prepare_baseline(action_id)).phase is baseline.PreparationPhase.ABORTED
        assert current.connection.execute(
            "SELECT state,returned_at,error_json FROM baseline_read_calls"
        ).fetchone() == (3, None, None)
        await capture_handler(handler)(action_id, runtime)
        assert current.connection.execute("SELECT status FROM actions").fetchone() == (5,)
        runtime.resume_baselines(action_id)
        assert current.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert current.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
        assert runtime.baseline_directory.calls == []
    finally:
        current.connection.close()


@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancel_closes_unstarted_segment_intent_before_terminal(tmp_path, save_fails):
    from camctl.capture.handlers import _finish_canceled_capture
    from camctl.contracts.values import ConsistencyError
    from ..capture.test_record_segment_scope_recovery import _cancel

    current, runtime, action_id, _, original = await _unstarted_segment_intent(tmp_path)
    _cancel(current, runtime, action_id)

    class UnavailableClose(CaptureRepository):
        def save_baseline_read_unconfirmed(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("close unavailable"))

    try:
        if save_fails:
            runtime.capture = UnavailableClose()
            with pytest.raises(ConsistencyError):
                _finish_canceled_capture(runtime, action_id, unstarted=True)
            assert current.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
            assert current.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
            pending = runtime.pending_baselines[action_id].read_owner.saves.pending
            assert str(pending.key) == str(original.request.result_save_key)
            runtime.capture = CaptureRepository()
        _finish_canceled_capture(runtime, action_id, unstarted=True)
        assert current.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (6,)
        assert current.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert current.connection.execute(
            "SELECT call_key,result_save_key,state,returned_at,error_json FROM baseline_read_calls"
        ).fetchone() == (str(original.request.call_key), str(original.request.result_save_key), 3, None, None)
        assert runtime.baseline_directory.calls == []
        assert current.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    finally:
        current.connection.close()


async def test_start_entry_rejects_not_executed_baseline_save_without_error(tmp_path):
    from camctl.capture.handlers import capture_handler
    from camctl.contracts.values import ConsistencyError
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog

    class NotExecuted(CaptureRepository):
        def save_baseline_read_intent(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.NOT_EXECUTED)

    current, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    runtime.capture = NotExecuted()
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        pending = runtime.pending_baselines[action_id].read_owner.saves.pending
        assert str(pending.key) == str(pending.request.call_key)
        assert runtime.baseline_directory.calls == []
        assert current.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    finally:
        current.connection.close()


@pytest.mark.parametrize("read_failed", [False, True])
async def test_cold_resume_releases_terminal_unstarted_scope_after_saved_read(tmp_path, read_failed):
    from camctl.capture.handlers import capture_handler
    from camctl.devices.directory import DirectoryCursor
    from camctl.operations.models import ErrorValue
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_segment_scope_recovery import SegmentWaitCatalog

    current, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    now = [runtime.wall_us()]

    def finished_page(request):
        now[0] += 6_000_000
        if read_failed:
            return DirectoryRead(None, ErrorValue("adb_directory_failed", "device_file_access", {"reason": "read_failed"}), None)
        entries = _entries("a", device=request.binding.device_id,
                           driver=request.binding.driver_id, directory=request.directories[0])
        return DirectoryRead(None, None, Page(entries, DirectoryCursor(
            request.binding, request.directories, 0, entries[-1].path)))

    runtime.wall_us = lambda: now[0]
    runtime.baseline_directory = Directory([finished_page])
    try:
        await runtime.prepare_baseline(action_id)
        # 模拟下一次进程：保留数据库中的原页及返回，丢弃上一会话内存拥有者。
        runtime.pending_baselines.clear()
        await capture_handler(handler)(action_id, runtime)
        assert current.connection.execute("SELECT status FROM actions").fetchone() == (5,)
        deps = SimpleNamespace(
            capture_baselines={}, capture_result_closes={}, capture_completions={},
            capture_retry_gate=None, capture_result_scans={}, capture_segment_writes={},
        )
        _resume_capture_requests(deps, current)
        assert current.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert current.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
        assert len(runtime.baseline_directory.calls) == 1
        assert deps.capture_baselines == {}
    finally:
        current.connection.close()


async def test_cold_resume_keeps_unreturned_baseline_intent_occupied(tmp_path):
    from camctl.persistence.repositories.scheduling import ExpireActionRequest

    current, runtime, action_id, _, _ = await _unstarted_segment_intent(tmp_path)
    try:
        runtime.save_capture_completion(
            ExpireActionRequest(action_id, runtime.wall_us() + 6_000_000,
                                runtime.wall_us() + 6_000_000)
        )
        runtime.pending_baselines.clear()
        deps = SimpleNamespace(
            capture_baselines={}, capture_result_closes={}, capture_completions={},
            capture_retry_gate=None, capture_result_scans={}, capture_segment_writes={},
        )
        _resume_capture_requests(deps, current)
        assert current.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
        assert current.connection.execute("SELECT state FROM baseline_read_calls").fetchone() == (1,)
    finally:
        current.connection.close()


@pytest.mark.parametrize("local_first", [False, True])
async def test_cold_resume_exhausts_batches_without_taking_local_owner(owned, tmp_path, local_first):
    from decimal import Decimal
    from unittest.mock import patch

    from camctl.capture.baseline_models import BaselineReadIntent, BaselineReadReturn
    from camctl.contracts.json_values import parse_exact_json
    from camctl.devices.bindings import DeviceBinding
    from camctl.operations.models import ErrorValue
    from camctl.persistence.repositories.scheduling import ExpireActionRequest, SchedulingRepository
    from camctl.outputs import read_discovery

    capture = CaptureRepository()
    identities = []
    for number in (1, 2):
        device = f"cam-{number}"
        activity_id = await _activity(owned, tmp_path, request_id=str(40 + number), device_id=device)
        action_id = owned.connection.execute("SELECT action_id FROM device_activities WHERE id=?", (activity_id,)).fetchone()[0]
        binding = DeviceBinding(device, "dji-action6")
        scope = owned.connection.execute(
            "SELECT output_scope_json FROM device_activities WHERE id=?", (activity_id,)
        ).fetchone()[0]
        directories = tuple(parse_exact_json(scope)["directories"])
        intent = BaselineReadIntent(action_id, activity_id, binding, directories, None,
            Decimal("10"), capture.baseline_ref(activity_id, owned), 1,
            new_operation_key(), new_operation_key(), _SCHEDULED)
        saved = capture.save_baseline_read_intent(intent, intent.call_key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "read_failed"})
        returned = capture.save_baseline_read_return(BaselineReadReturn(
            saved.value.call_id, _SCHEDULED + 1, error, DirectoryRead(None, error, None)),
            intent.result_save_key, owned)
        assert returned.kind is DbOutcomeKind.COMPLETED, returned.error
        expired = SchedulingRepository().expire_action(
            ExpireActionRequest(action_id, _SCHEDULED + 6_000_000, _SCHEDULED + 6_000_000),
            new_operation_key(), owned)
        assert expired.kind is DbOutcomeKind.COMPLETED, expired.error
        identities.append((action_id, activity_id, binding, directories))
    pending = {}
    first, activity_id, binding, directories = identities[0]
    if local_first:
        pending[first] = baseline.PendingBaseline(activity_id, binding, directories, in_call=True)
    original = pending.get(first)
    deps = SimpleNamespace(
        capture_baselines=pending, capture_result_closes={}, capture_completions={},
        capture_retry_gate=None, capture_result_scans={}, capture_segment_writes={},
    )
    with patch.object(read_discovery, "DEFAULT_BATCH_SIZE", 1):
        _resume_capture_requests(deps, owned)
    assert owned.connection.execute(
        "SELECT occupancy_state FROM device_activities ORDER BY action_id"
    ).fetchall() == ([(1,), (2,)] if local_first else [(2,), (2,)])
    assert pending == ({first: original} if local_first else {})
