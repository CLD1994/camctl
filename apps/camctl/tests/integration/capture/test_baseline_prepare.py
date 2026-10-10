"""真实基准仓储与受目录接口约束的准备替身共同推进。"""
from types import SimpleNamespace
from decimal import Decimal

import pytest

from camctl.capture import baseline
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_baseline_history import _activity, _entries, _ref
from ..scheduling.test_start_action import _SCHEDULED, owned

pytestmark = pytest.mark.asyncio


class Directory:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def read_directory(self, request, *, stop):
        self.calls.append(request)
        return self.replies.pop(0)(request)


def _runtime(owned, directory, repository=None):
    return SimpleNamespace(owned=owned, capture=repository or CaptureRepository(),
        baseline_directory=directory, pending_baselines={}, wall_us=lambda: _SCHEDULED,
        baseline_timeout_s=Decimal("10"))


async def test_fixed_baseline_is_ready_without_device_call(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    directory = Directory([lambda r: DirectoryRead(None, None, Page((), None))])
    runtime = _runtime(owned, directory)
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert _ref(owned, activity_id).entry_count == 0
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert len(directory.calls) == 1


async def test_unknown_fix_resumes_original_before_clock_or_device(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    calls = []
    class Repository(CaptureRepository):
        def fix_baseline(self, request, key, owned):
            calls.append((request, key))
            result = super().fix_baseline(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("固定回执未知")) if len(calls) == 1 else result
    runtime = _runtime(owned, Directory([lambda r: DirectoryRead(None, None, Page((), None))]), Repository())
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    runtime.wall_us = lambda: pytest.fail("原固定申请不得取得新时刻")
    runtime.baseline_directory = None
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert calls[0] == calls[1] and runtime.pending_baselines == {}


async def test_multibatch_baseline_saves_every_page_before_next_read(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    def first(request):
        return DirectoryRead(None, None, Page(_entries("a"), DirectoryCursor(
            request.binding, request.directories, 0, _entries("a")[0].path)))
    def second(request):
        assert _ref(owned, activity_id).first_event_id is not None
        return DirectoryRead(None, None, Page(_entries("b"), None))
    runtime = _runtime(owned, Directory([first, second]))
    result = await baseline.prepare_baseline(1, activity_id, runtime=runtime)
    assert result.phase is baseline.PreparationPhase.READY
    assert (_ref(owned, activity_id).chunk_count, _ref(owned, activity_id).entry_count) == (2, 2)
    assert runtime.pending_baselines == {}


async def test_unknown_append_retains_original_request_and_next_cursor(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    calls = []
    class UnknownRepository(CaptureRepository):
        def append_baseline(self, request, key, owned):
            calls.append((request, key))
            result = super().append_baseline(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("未取得提交回执")) if len(calls) == 1 else result
    def first(request):
        return DirectoryRead(None, None, Page(_entries("a"), DirectoryCursor(
            request.binding, request.directories, 0, _entries("a")[0].path)))
    runtime = _runtime(owned, Directory([first, lambda r: DirectoryRead(None, None, Page(_entries("b"), None))]), UnknownRepository())
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    assert len(runtime.baseline_directory.calls) == 1
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert calls[0] == calls[1] and len(runtime.baseline_directory.calls) == 2
    assert _ref(owned, activity_id).entry_count == 2


async def test_middle_page_failure_keeps_original_error_and_does_not_retry(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "actual_error"})
    def first(request):
        return DirectoryRead(None, None, Page(_entries("a"), DirectoryCursor(
            request.binding, request.directories, 0, _entries("a")[0].path)))
    runtime = _runtime(owned, Directory([first, lambda r: DirectoryRead(None, error, None)]))
    for _ in range(2):
        result = await baseline.prepare_baseline(1, activity_id, runtime=runtime)
        assert result.phase is baseline.PreparationPhase.FAILED and result.error is error
    assert len(runtime.baseline_directory.calls) == 2
    assert _ref(owned, activity_id).state.name == "COLLECTING"
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
