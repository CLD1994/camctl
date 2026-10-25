"""数据库等待后停止新增维护，原结果保存仍继续。"""

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.values import OperationKey
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.work_files import (
    CleanupResultSave, CleanupScan, PendingWorkFileResult, WorkFileContext,
    WorkFileHistory, WorkFileLimits, WorkFileOutcome, WorkFileSingleOutcome,
    clean_one_work_file, clean_work_files,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database

from .test_work_files import _facts

pytestmark = pytest.mark.asyncio


def make_context():
    database = inline_business_database(OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing")))
    repository = create_autospec(OutputsRepository, instance=True)
    repository.load_work_file_state.return_value = _facts()
    repository.next_cleanup_candidates.return_value = (28,)
    repository.save_cleanup_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repository.save_cleanup_checked.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    context = WorkFileContext(repository, database, Path("/staging"), 1000,
        WorkFileLimits(2, 2), executor=create_autospec(FileTaskExecutor, instance=True),
        history=WorkFileHistory(CleanupScan(0, 28, 2, 2)))
    context.executor.unfinished_files.return_value = ()
    return context


def pause_read(database, description, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = database.read

    async def read(callback, **options):
        if options.get("description") == description:
            entered.set()
            await release.wait()
        return await original(callback, **options)

    monkeypatch.setattr(database, "read", read)
    return entered, release


@pytest.mark.parametrize("history", [False, True])
async def test_stop_during_file_facts_does_not_start_new_task(monkeypatch, history):
    context = make_context()
    stopped = False
    object.__setattr__(context, "stop_requested", lambda: stopped)
    entered, release = pause_read(context.database, "中间文件状态读取", monkeypatch)
    deletion = AsyncMock(return_value=(WorkFileSingleOutcome.DELETED, None, True))
    monkeypatch.setattr("camctl.outputs.work_files._owned_deletion", deletion)
    original_scan = context.history.scan
    task = asyncio.create_task(clean_work_files(context) if history
                               else clean_one_work_file(28, context))
    await asyncio.wait_for(entered.wait(), 1)
    stopped = True
    release.set()
    result = await task

    deletion.assert_not_awaited()
    assert context.processed == {}
    assert context.history.scan == original_scan
    if history:
        assert result.checked == 0
    else:
        assert result is None


@pytest.mark.parametrize("stage", ["outer", "inner"])
async def test_first_maintenance_stop_preserves_unstarted_identity(monkeypatch, stage):
    context = make_context()
    runtime = WorkFileRuntime(staging=Path("/staging"), limits=context.limits)
    runtime.repository = context.repository
    runtime._first_files.add(28)
    runtime.history.finished = True
    runtime.executor = context.executor
    monkeypatch.setattr(runtime, "_discover", AsyncMock())
    entered, release = pause_read(context.database,
        "中间文件首次责任状态读取" if stage == "outer" else "中间文件状态读取", monkeypatch)
    deletion = AsyncMock(return_value=(WorkFileSingleOutcome.DELETED, None, True))
    monkeypatch.setattr("camctl.outputs.work_files._owned_deletion", deletion)
    task = asyncio.create_task(runtime._maintain(SimpleNamespace(
        database=context.database, clock=SimpleNamespace(utc_micros=lambda: 1000))))
    await asyncio.wait_for(entered.wait(), 1)
    runtime.stop_new_work()
    release.set()
    await task

    deletion.assert_not_awaited()
    assert runtime._first_files == {28}
    assert runtime.processed == {}


async def test_stopped_maintenance_still_saves_original_pending_result():
    context = make_context()
    object.__setattr__(context, "stop_requested", lambda: True)
    key = OperationKey("a" * 32)
    request = CleanupResultSave(28, WorkFileOutcome.COMPLETED, None, 1000)
    context.pending_results[28] = PendingWorkFileResult(
        request, key, WorkFileSingleOutcome.DELETED, None)

    async def execute(task):
        return await task.body(None)

    context.executor.run_owned_async_file_task.side_effect = execute
    result = await clean_one_work_file(28, context)

    assert result.outcome is WorkFileSingleOutcome.DELETED
    assert context.pending_results == {}
    assert context.processed == {28: WorkFileSingleOutcome.DELETED}
    context.repository.save_cleanup_result.assert_called_once()
    assert context.repository.save_cleanup_result.call_args.args[:2] == (request, key)
    context.repository.load_work_file_state.assert_not_called()
