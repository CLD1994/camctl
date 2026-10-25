"""媒体拥有者在异步数据库边界保留真实派发和保存责任。"""

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.media_flow import CaptureProcessingSaves, DriverReadSessions
from camctl.capture.input_copy import RecordingSource
from camctl.operations.models import AttemptTicket
from camctl.capture.processing import (
    CheckPhase, CheckResultSave, MediaObservation, ProcessingDisposition, RepairOutputFile, RepairStart,
)
from camctl.devices.ports import ReadDriver
from camctl.devices.read_session import SourceFile
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import OwnedConnection


@pytest.mark.asyncio
async def test_source_fact_wait_cancel_keeps_undispatched_marker():
    database = create_autospec(BusinessDatabase, instance=True)
    driver = create_autospec(ReadDriver, instance=True)
    entered = asyncio.Event()
    release = asyncio.Event()
    marker = {9: object()}

    async def read(callback, **kwargs):
        entered.set()
        await release.wait()
        return SourceFile("original", {"path": "original"}, 20)

    database.read.side_effect = read
    sessions = DriverReadSessions(database, driver, AttemptTicket(1, "read", "9", "read", 28))
    task = asyncio.create_task(sessions.open_session(30, 0, on_start=lambda: marker.pop(9)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 9 in marker
    driver.open_read.assert_not_called()


@pytest.mark.asyncio
async def test_actual_source_dispatch_releases_marker_after_fact_read():
    database = create_autospec(BusinessDatabase, instance=True)
    driver = create_autospec(ReadDriver, instance=True)
    database.read.return_value = SourceFile("original", {"path": "original"}, 20)
    marker = {9: object()}
    source = create_autospec(RecordingSource, instance=True)
    driver.open_read.side_effect = lambda *args, **kwargs: source if not marker else None
    sessions = DriverReadSessions(database, driver, AttemptTicket(1, "read", "9", "read", 28))
    assert await sessions.open_session(30, 0, on_start=lambda: marker.pop(9)) is source
    assert not marker


@pytest.mark.asyncio
async def test_cancel_waits_for_repair_identity_handoff():
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = (21,)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(kind=DbOutcomeKind.COMPLETED,
                         value=RepairOutputFile(ProcessingDisposition.SAVED, 70, "repaired/70.mp4"))

    database.write.side_effect = write
    pending = {}
    saves = CaptureProcessingSaves(database, pending, file_ids=(60,))
    task = asyncio.create_task(saves.start_repair_output(RepairStart(1, "mp4", 100)))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pending
    assert saves.file_ids == (60, 70)


@pytest.mark.asyncio
async def test_ready_media_input_uses_finite_worker_reads_and_async_digest(monkeypatch):
    from camctl.capture import media_flow
    from camctl.capture.media import CheckExecutionPhase, CheckStep, MediaPolicy, MediaTools
    from camctl.host_files.models import BoundDirectories
    from camctl.persistence.repositories.outputs import OutputsRepository
    from unit.capture.test_input_copy import _granted, _state
    from unit.capture.test_media_execution import _status

    database = create_autospec(BusinessDatabase, instance=True)
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (2,)
    cursor.fetchall.return_value = ()
    owned.connection.execute.return_value = cursor
    database.read.side_effect = lambda callback, **kwargs: callback(owned)
    database.write.side_effect = lambda callback, **kwargs: callback(owned)
    repository = create_autospec(OutputsRepository, instance=True)
    repository.grant_file.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED, value=_granted())
    repository.load_copy_state.return_value = _state(verification_state=3, target_sha256="a" * 64)
    monkeypatch.setattr(media_flow, "OutputsRepository", lambda *args: repository)
    monkeypatch.setattr(media_flow, "load_processing_status", lambda *args: _status(repair_state=2))
    check = create_autospec(media_flow.execute_check)
    check.return_value = CheckStep(CheckExecutionPhase.NOT_REQUIRED)
    monkeypatch.setattr(media_flow, "execute_check", check)

    async def resolve(file_id):
        assert file_id == 30
        return None

    flow = media_flow.MediaFlow(database, BoundDirectories(Path("staging")),
        create_autospec(DriverReadSessions, instance=True), create_autospec(MediaTools, instance=True),
        MediaPolicy(Decimal("1")), lambda: 100, False, digest_for=resolve)
    result = await media_flow.run_recording_media(flow, 21, 1, 30)
    assert result.phase is CheckExecutionPhase.NOT_REQUIRED
    flow.sessions.open_session.assert_not_called()
    assert repository.load_copy_state.call_args.args[1] is owned
    assert repository.grant_file.call_args.args[2] is owned


@pytest.mark.asyncio
async def test_unknown_media_request_uses_new_worker_connection_and_original_key(monkeypatch):
    from camctl.capture import media_flow
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = (21,)
    repository = create_autospec(CaptureRepository, instance=True)
    repository.save_check_result.side_effect = (
        DbOutcome(kind=DbOutcomeKind.UNKNOWN), DbOutcome(kind=DbOutcomeKind.COMPLETED))
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    connections = [create_autospec(OwnedConnection, instance=True) for _ in range(2)]

    async def write(callback, **kwargs):
        return callback(connections.pop(0))

    database.write.side_effect = write
    saves = CaptureProcessingSaves(database, {})
    command = CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), 100)
    await saves.save_check_result(command)
    first = repository.save_check_result.call_args.args
    await saves.save_check_result(command)
    second = repository.save_check_result.call_args.args
    assert first[:2] == second[:2]
    assert first[2] is not second[2]
    assert not saves.pending


@pytest.mark.asyncio
async def test_completed_observer_error_still_hands_off_repair_identity():
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = (21,)
    error = RuntimeError("commit observer failed")
    _attach_db_outcome(error, DbOutcome(kind=DbOutcomeKind.COMPLETED,
        value=RepairOutputFile(ProcessingDisposition.SAVED, 70, "repaired/70.mp4"),
        observer_error=error))
    database.write.side_effect = error
    saves = CaptureProcessingSaves(database, {}, file_ids=(60,))
    with pytest.raises(RuntimeError) as caught:
        await saves.start_repair_output(RepairStart(1, "mp4", 100))
    assert caught.value is error
    assert not saves.pending
    assert saves.file_ids == (60, 70)
