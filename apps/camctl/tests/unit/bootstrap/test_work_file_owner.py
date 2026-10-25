"""维护轮次按真实监督协议消费已结束责任后才能重入。"""

from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.outputs.work_files import CleanupScan, WorkFileLimits
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


@pytest.mark.asyncio
async def test_completed_maintenance_is_consumed_before_next_round(monkeypatch):
    runtime = WorkFileRuntime(staging=Path("/staging"), limits=WorkFileLimits(1, 2))
    runtime.history.scan = CleanupScan(start_after=0, ceiling=2, after_id=0,
                                      remaining=1, batch_size=1)
    calls = []

    async def maintain(context):
        calls.append(context)

    monkeypatch.setattr(runtime, "_maintain", maintain)
    monkeypatch.setattr(runtime, "_discover", AsyncMock())
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True)
    owned.metadata = DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing")
    database = inline_business_database(owned)
    first = SimpleNamespace(database=database)
    second = SimpleNamespace(database=database)
    original_scan = runtime.history.scan
    await runtime.flow(first)
    await runtime._task

    await runtime.flow(second)
    await runtime.settle()

    assert calls == [first, second]
    assert runtime.supervisor.outstanding() == ()
    assert runtime.history.scan is original_scan
    assert runtime.processed == {}


@pytest.mark.asyncio
async def test_cancel_after_cleanup_commit_applies_pending_and_scan(monkeypatch):
    import asyncio
    from unittest.mock import Mock
    from camctl.contracts.values import OperationKey
    from camctl.outputs.work_files import (
        CleanupResultSave, PendingWorkFileResult, WorkFileOutcome, WorkFileHistory,
        WorkFileSingleOutcome, _save_pending_result,
    )
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository

    owned = OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing"))
    database = inline_business_database(owned)
    repository = Mock(spec=OutputsRepository)
    repository.save_cleanup_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    scan = CleanupScan(start_after=0, ceiling=4, after_id=0, remaining=2, batch_size=2)
    pending = PendingWorkFileResult(CleanupResultSave(3, WorkFileOutcome.COMPLETED,
        None, 1000, advance_cursor=True), OperationKey("a" * 32),
        WorkFileSingleOutcome.DELETED, None, scan)
    context = SimpleNamespace(database=database, repository=repository,
        processed={}, pending_results={3: pending}, history=WorkFileHistory(scan))
    started, release = asyncio.Event(), asyncio.Event()
    original_write = database.write

    async def pause(callback, *, key, description):
        started.set()
        await release.wait()
        return await original_write(callback, key=key, description=description)

    monkeypatch.setattr(database, "write", pause)
    task = asyncio.create_task(_save_pending_result(pending, context))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.pending_results == {}
    assert context.processed == {3: WorkFileSingleOutcome.DELETED}
    assert context.history.scan.after_id == 3
    assert context.history.scan.remaining == 1


@pytest.mark.asyncio
async def test_discovery_applies_new_file_identity_outside_database_callback(monkeypatch):
    from unittest.mock import Mock
    from camctl.contracts.enums import enum_for

    owned = OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing"))
    cursor = Mock(spec=sqlite3.Cursor)
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = [(1,)]
    owned.connection.execute.return_value = cursor
    database = inline_business_database(owned)
    runtime = WorkFileRuntime(staging=Path("/staging"), limits=WorkFileLimits(1, 2))
    active = False

    class LoopFiles(set):
        def add(self, identity):
            assert not active
            super().add(identity)

    runtime._first_files = LoopFiles()
    original_read = database.read

    async def read(callback, *, description):
        def guarded(owned):
            nonlocal active
            active = True
            try:
                return callback(owned)
            finally:
                active = False
        return await original_read(guarded, description=description)

    monkeypatch.setattr(database, "read", read)
    row = SimpleNamespace(table="intermediate_files", row_id=3,
        before=SimpleNamespace(exists=True, values={"retention_state": 1}),
        after=SimpleNamespace(exists=True, values={"retention_state":
            int(enum_for("intermediate_files.retention_state").RELEASABLE)}))
    monkeypatch.setattr("camctl.bootstrap.work_file_assembly.decode_event_row",
                        lambda _stored: SimpleNamespace(event_id=1, rows=(row,)))
    await runtime._discover(database)
    assert runtime._first_files == {3}
    assert runtime._discovery_after == 1
