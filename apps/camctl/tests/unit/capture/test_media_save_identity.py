"""待保存媒体申请按精确 JSON 输入核实，保留原申请与原操作键。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.media import SaveDisposition
from camctl.capture.media_flow import CaptureProcessingSaves
from camctl.capture.processing import CheckPhase, CheckResultSave, MediaObservation, ProcessingError
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database

pytestmark = pytest.mark.asyncio


def _saves(monkeypatch):
    from camctl.capture import media_flow
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (1,)
    connection.execute.return_value = cursor
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = connection
    owned.metadata = DatabaseMetadata("camctl", "unit", 5, "staging", "ready", "processing")
    repository = create_autospec(CaptureRepository, instance=True)
    repository.save_check_result.return_value = DbOutcome(
        kind=DbOutcomeKind.UNKNOWN, error=sqlite3.OperationalError("original save unknown"))
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    return CaptureProcessingSaves(inline_business_database(owned), {}), repository


def _request(value):
    return CheckResultSave(1, MediaObservation(CheckPhase.UNCONFIRMED,
        error=ProcessingError("probe_unconfirmed", "probe", {"attempt": value})), 100)


async def test_pending_media_rejects_bool_instead_of_original_json_integer(monkeypatch):
    saves, repository = _saves(monkeypatch)
    first = await saves.save_check_result(_request(1))
    assert first.disposition is SaveDisposition.UNKNOWN
    repository.save_check_result.reset_mock()
    with pytest.raises(ConsistencyError):
        await saves.save_check_result(_request(True))
    repository.save_check_result.assert_not_called()


async def test_pending_media_same_request_uses_original_key_and_releases_after_confirmation(monkeypatch):
    saves, repository = _saves(monkeypatch)
    command = _request(1)
    assert (await saves.save_check_result(command)).disposition is SaveDisposition.UNKNOWN
    original_key = repository.save_check_result.call_args.args[1]
    repository.save_check_result.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED)
    result = await saves.save_check_result(replace(command))
    assert result.disposition is SaveDisposition.SAVED
    assert repository.save_check_result.call_args.args[1] == original_key
    assert not saves.pending


async def test_pending_media_keeps_original_stream_probe_when_save_is_unknown(monkeypatch):
    from decimal import Decimal
    from camctl.host_files.media import MediaProbe, MediaStream
    saves, repository = _saves(monkeypatch)
    probe = MediaProbe(Decimal("75"), None, (
        MediaStream(0, "video", "hevc", 3840, 3840, Decimal("75"), False),), "mp4")
    command = replace(_request(1), source_probe=probe)
    assert (await saves.save_check_result(command)).disposition is SaveDisposition.UNKNOWN
    key = repository.save_check_result.call_args.args[1]
    changed_probe = replace(probe, streams=(replace(probe.streams[0], width=1920),))
    with pytest.raises(ConsistencyError):
        await saves.save_check_result(replace(command, source_probe=changed_probe))
    repository.save_check_result.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED)
    assert (await saves.save_check_result(command)).disposition is SaveDisposition.SAVED
    assert repository.save_check_result.call_args.args[1] == key


@pytest.mark.parametrize("entry", ["complete_repair_output", "save_repair_result"])
async def test_pending_repair_success_keeps_actual_probe_and_original_key(monkeypatch, entry):
    from decimal import Decimal
    from camctl.capture.processing import RepairSuccess, RepairResultSave, RepairOutcome
    from camctl.host_files.media import MediaProbe
    saves, repository = _saves(monkeypatch)
    save = getattr(saves, entry)
    repository_save = getattr(repository, entry)
    repository_save.return_value = DbOutcome(kind=DbOutcomeKind.UNKNOWN)
    probe = MediaProbe(Decimal("60"), None)
    command = (RepairSuccess(1,2,2048,"a"*64,100,output_probe=probe) if entry == "complete_repair_output"
        else RepairResultSave(1,RepairOutcome.SUCCEEDED,100,output_file_id=2,output_probe=probe))
    assert (await save(command)).disposition is SaveDisposition.UNKNOWN
    key = repository_save.call_args.args[1]
    repository_save.reset_mock()
    with pytest.raises(ConsistencyError):
        await save(replace(command,output_probe=replace(probe,duration_s=Decimal("59"))))
    repository_save.assert_not_called()
    repository_save.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED)
    assert (await save(command)).disposition is SaveDisposition.SAVED
    assert repository_save.call_args.args[1] == key
