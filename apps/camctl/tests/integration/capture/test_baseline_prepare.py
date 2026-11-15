"""真实基准仓储与受目录接口约束的准备替身共同推进。"""
import asyncio
import inspect
import threading
from contextlib import nullcontext
from unittest.mock import create_autospec, patch
from decimal import Decimal

import pytest

from camctl.capture import baseline
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.ports import ControlDriver
from camctl.devices.evidence import EvidenceRegistry
from ..support.capture import capture_runtime
from ..support.task_wait import wait_for_signal, settle_tasks
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_baseline_history import _entries
from .baseline_database import database, baseline_activity as _activity, baseline_ref as _ref
from ..scheduling.test_start_action import _SCHEDULED

pytestmark = pytest.mark.asyncio


class Directory:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def read_directory(self, request, *, stop):
        self.calls.append(request)
        result = self.replies.pop(0)(request)
        if inspect.isawaitable(result):
            return await result
        return result


async def _runtime(database, directory, repository=None, *, clock=None):
    driver = create_autospec(ControlDriver, instance=True)
    driver.control.side_effect = AssertionError("目录准备不能派发START")
    factory_repository = (nullcontext() if repository is None else patch(
        "camctl.bootstrap.capture_assembly.CaptureRepository", return_value=repository))
    with factory_repository:
        return await capture_runtime(database, driver=driver, evidence=EvidenceRegistry(()),
            directory=directory, driver_id="dji-action6",
            wall_us=(lambda: _SCHEDULED) if clock is None else clock)


async def test_fixed_baseline_is_ready_without_device_call(database, tmp_path):
    activity_id = await _activity(database, tmp_path)
    directory = Directory([lambda r: DirectoryRead(None, None, Page((), None))])
    runtime = await _runtime(database, directory)
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert (await _ref(database, activity_id)).entry_count == 0
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert len(directory.calls) == 1


async def test_unknown_fix_resumes_original_before_clock_or_device(database, tmp_path):
    activity_id = await _activity(database, tmp_path)
    calls = []
    class Repository(CaptureRepository):
        def fix_baseline(self, request, key, owned):
            calls.append((request, key))
            result = super().fix_baseline(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("固定回执未知")) if len(calls) == 1 else result
    clock_allowed = [True]
    def clock():
        assert clock_allowed[0], "原固定申请不得取得新时刻"
        return _SCHEDULED
    directory = Directory([lambda r: DirectoryRead(None, None, Page((), None))])
    runtime = await _runtime(database, directory, Repository(), clock=clock)
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    clock_allowed[0] = False
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert calls[0] == calls[1] and len(directory.calls) == 1
    stored = await _ref(database, activity_id)
    assert stored.state.name == "FIXED" and stored.entry_count == 0


async def test_multibatch_baseline_saves_every_page_before_next_read(database, tmp_path):
    activity_id = await _activity(database, tmp_path)
    def first(request):
        return DirectoryRead(None, None, Page(_entries("a"), DirectoryCursor(
            request.binding, request.directories, 0, _entries("a")[0].path)))
    async def second(request):
        assert (await _ref(database, activity_id)).first_event_id is not None
        return DirectoryRead(None, None, Page(_entries("b"), None))
    directory = Directory([first, second])
    runtime = await _runtime(database, directory)
    result = await baseline.prepare_baseline(1, activity_id, runtime=runtime)
    assert result.phase is baseline.PreparationPhase.READY
    assert ((await _ref(database, activity_id)).chunk_count, (await _ref(database, activity_id)).entry_count) == (2, 2)
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert len(directory.calls) == 2


async def test_unknown_append_retains_original_request_and_next_cursor(database, tmp_path):
    activity_id = await _activity(database, tmp_path)
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
    directory = Directory([first, lambda r: DirectoryRead(None, None, Page(_entries("b"), None))])
    runtime = await _runtime(database, directory, UnknownRepository())
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.PENDING
    assert len(directory.calls) == 1
    assert (await baseline.prepare_baseline(1, activity_id, runtime=runtime)).phase is baseline.PreparationPhase.READY
    assert calls[0] == calls[1] and len(directory.calls) == 2
    assert (await _ref(database, activity_id)).entry_count == 2


async def test_middle_page_failure_keeps_original_error_and_does_not_retry(database, tmp_path):
    activity_id = await _activity(database, tmp_path)
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "actual_error"})
    def first(request):
        return DirectoryRead(None, None, Page(_entries("a"), DirectoryCursor(
            request.binding, request.directories, 0, _entries("a")[0].path)))
    directory = Directory([first, lambda r: DirectoryRead(None, error, None)])
    runtime = await _runtime(database, directory)
    for _ in range(2):
        result = await baseline.prepare_baseline(1, activity_id, runtime=runtime)
        assert result.phase is baseline.PreparationPhase.FAILED and result.error is error
    assert len(directory.calls) == 2
    assert (await _ref(database, activity_id)).state.name == "COLLECTING"
    assert await database.read(lambda owned: owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts").fetchone()) == (0,)


@pytest.mark.parametrize("cancel_normal", [False, True])
async def test_real_worker_original_and_recovery_consume_one_baseline_chunk(database, tmp_path, cancel_normal):
    activity_id = await _activity(database, tmp_path)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    saves = []

    class Repository(CaptureRepository):
        def append_baseline(self, request, key, owned):
            saves.append((request, key))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise AssertionError("测试未交还原基准保存的执行机会")
            return super().append_baseline(request, key, owned)

    directory = Directory([lambda request: DirectoryRead(None, None, Page(_entries("a"), None))])
    runtime = await _runtime(database, directory, Repository())
    normal = asyncio.create_task(baseline.prepare_baseline(1, activity_id, runtime=runtime))
    recovery = None
    try:
        await wait_for_signal(entered, normal)
        recovery = asyncio.create_task(baseline.prepare_baseline(1, activity_id, runtime=runtime))
        await asyncio.sleep(0)
        if cancel_normal:
            normal.cancel("baseline waiter")
            await asyncio.sleep(0)
            assert not normal.done()
        release.set()
        results = await asyncio.wait_for(
            asyncio.shield(asyncio.gather(normal, recovery, return_exceptions=True)), timeout=30)
        if cancel_normal:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0].phase is baseline.PreparationPhase.READY
        assert results[1].phase is baseline.PreparationPhase.READY
        assert len(saves) == 1 and len(directory.calls) == 1
        stored = await _ref(database, activity_id)
        assert (stored.chunk_count, stored.entry_count) == (1, 1)
        assert await database.read(lambda owned: owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (saves[0][1],)).fetchone()) == (1,)
    finally:
        release.set()
        await settle_tasks(normal, recovery)
