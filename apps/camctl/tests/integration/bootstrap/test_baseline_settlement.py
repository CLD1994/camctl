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
            return DirectoryRead(None, None, Page(_entries("a"), None))

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
