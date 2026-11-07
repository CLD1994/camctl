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


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance_failure", [False, True], ids=["media", "maintenance-and-media"])
async def test_local_work_settlement_reports_each_unconfirmed_media_save(monkeypatch, maintenance_failure):
    """本地责任收场保留每份原申请，维护故障不能覆盖媒体故障。"""
    import asyncio
    from decimal import Decimal

    from camctl.capture import media_flow
    from camctl.capture.media import SaveDisposition
    from camctl.capture.media_flow import CaptureProcessingSaves
    from camctl.capture.processing import CheckPhase, CheckResultSave, MediaObservation
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.session.service import StateDbFailure
    from camctl.session.supervision import OwnedTask

    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    owned = create_autospec(OwnedConnection, instance=True, spec_set=True)
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    errors = [sqlite3.OperationalError("first save unknown"),
              sqlite3.OperationalError("second save unknown")]
    repository.save_check_result.side_effect = [
        DbOutcome(DbOutcomeKind.UNKNOWN, error=error) for error in errors]
    database.read.side_effect = [(21,), (22,)]
    database.write.side_effect = lambda callback, **_kwargs: callback(owned)
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    pending = {}
    saves = CaptureProcessingSaves(database, pending)
    work = WorkFileRuntime(staging=Path("/staging"), limits=WorkFileLimits(1, 2),
                           pending_media_results=pending)
    commands = [CheckResultSave(identity, MediaObservation(CheckPhase.COMPLETED, Decimal("200")), 100)
                for identity in (1, 2)]
    for command in commands:
        assert (await saves.save_check_result(command)).disposition is SaveDisposition.UNKNOWN
    calls = repository.save_check_result.call_args_list
    keys = [call.args[1] for call in calls]
    assert [call.args[0] for call in calls] == commands
    assert keys[0] != keys[1]
    assert work.required_settlements() == 2

    if maintenance_failure:
        async def maintenance():
            raise OSError("maintenance failed")

        token = work.supervisor.register(work)
        work.supervisor.handoff(token, OwnedTask(
            "cleanup/9", "delete", "维护清理", ("state_db",),
            asyncio.create_task(maintenance())))

    with pytest.raises(StateDbFailure) as failed:
        await work.settle()
    message = str(failed.value)
    for command, key, error in zip(commands, keys, errors):
        assert (f"processing_id={command.processing_id}, stage=save_check_result, "
                f"key={key}: {error}") in message
    assert ("maintenance failed" in message) is maintenance_failure
    assert failed.value.__cause__ is errors[0]
    assert work.required_settlements() == 2

    # 收场不能把未核实的原结果当作成功，也不能再次读取或保存。
    with pytest.raises(StateDbFailure) as repeated:
        await work.settle()
    assert repeated.value.__cause__ is errors[0]
    assert work.required_settlements() == 2
    assert database.read.await_count == 2
    assert database.write.await_count == 2
    assert repository.save_check_result.call_args_list == calls
