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
from camctl.persistence.runtime import OwnedConnection


def _saves(monkeypatch):
    from camctl.capture import media_flow
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (1,)
    connection.execute.return_value = cursor
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = connection
    repository = create_autospec(CaptureRepository, instance=True)
    repository.save_check_result.return_value = DbOutcome(
        kind=DbOutcomeKind.UNKNOWN, error=sqlite3.OperationalError("original save unknown"))
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    return CaptureProcessingSaves(owned, {}), repository


def _request(value):
    return CheckResultSave(1, MediaObservation(CheckPhase.UNCONFIRMED,
        error=ProcessingError("probe_unconfirmed", "probe", {"attempt": value})), 100)


def test_pending_media_rejects_bool_instead_of_original_json_integer(monkeypatch):
    saves, repository = _saves(monkeypatch)
    first = saves.save_check_result(_request(1))
    assert first.disposition is SaveDisposition.UNKNOWN
    repository.save_check_result.reset_mock()
    with pytest.raises(ConsistencyError):
        saves.save_check_result(_request(True))
    repository.save_check_result.assert_not_called()


def test_pending_media_same_request_uses_original_key_and_releases_after_confirmation(monkeypatch):
    saves, repository = _saves(monkeypatch)
    command = _request(1)
    assert saves.save_check_result(command).disposition is SaveDisposition.UNKNOWN
    original_key = repository.save_check_result.call_args.args[1]
    repository.save_check_result.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED)
    result = saves.save_check_result(replace(command))
    assert result.disposition is SaveDisposition.SAVED
    assert repository.save_check_result.call_args.args[1] == original_key
    assert not saves.pending
