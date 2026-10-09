"""准备原页保存及终态占用由默认共同恢复入口处理。"""
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
                           capture_completions={}, capture_retry_gate=None)
    _resume_capture_requests(deps, owned)
    assert owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)
    assert runtime.pending_baselines == {} and len(runtime.baseline_directory.calls) == 1
