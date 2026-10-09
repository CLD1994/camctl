"""拍摄完整申请的类型分派、失败责任与可靠完成释放。"""

from types import SimpleNamespace
from unittest.mock import create_autospec, patch
import sqlite3

import pytest

from camctl.capture.handlers import PendingCaptureCompletion, resume_capture_completions
from camctl.capture.media import RecordingFailure
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.enums import enum_for
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, CaptureResult, FinishBindingFailure, FinishCanceledCapture,
    FinishCapture, FinishRecordingResults,
)
from camctl.persistence.repositories.operations import OperationRepository


def _requests():
    capture = FinishCapture(23, (), OutputCatalogFacts(23, True), 1_000)
    return (
        pytest.param(capture, "finish_capture", id="ordinary"),
        pytest.param(FinishCanceledCapture(23, 1_000), "finish_canceled_capture", id="canceled"),
        pytest.param(FinishBindingFailure(23, 1_000,
            RecordingFailure("device_binding_unavailable", {
                "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"})),
            "finish_binding_failure", id="binding"),
        pytest.param(FinishRecordingResults(capture, 71), "finish_recording_results", id="recording"),
    )


def _owned():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchall.return_value = []
    connection.execute.return_value = cursor
    return SimpleNamespace(connection=connection)


@pytest.mark.parametrize("command,method", _requests())
@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED])
def test_unreliable_completion_keeps_original_request(command, method, kind):
    # 提前移除申请、错误类型路由或重新生成操作键都会破坏保存责任。
    repository = create_autospec(CaptureRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), command)
    held = {23: pending}
    getattr(repository, method).return_value = DbOutcome(kind, error=RuntimeError("保存尚未完成"))
    owned = _owned()
    with pytest.raises(ConsistencyError):
        resume_capture_completions(owned, pending_capture_completions=held, capture=repository)
    assert held == {23: pending} and held[23] is pending
    getattr(repository, method).assert_called_once_with(command, pending.key, owned)


@pytest.mark.parametrize("command,method", _requests())
def test_completed_without_business_result_keeps_original_request(command, method):
    repository = create_autospec(CaptureRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), command)
    held = {23: pending}
    getattr(repository, method).return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    with pytest.raises(ConsistencyError):
        resume_capture_completions(_owned(), pending_capture_completions=held, capture=repository)
    assert held[23] is pending


@pytest.mark.parametrize("command,method", _requests())
def test_reliable_confirmation_releases_original_request(command, method):
    repository = create_autospec(CaptureRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), command)
    held = {23: pending}
    status = 6 if isinstance(command, FinishCanceledCapture) else 4 if isinstance(command, FinishBindingFailure) else 3
    getattr(repository, method).return_value = DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(status, 2, ()))
    owned = _owned()
    resume_capture_completions(owned, pending_capture_completions=held, capture=repository)
    assert held == {}
    getattr(repository, method).assert_called_once_with(command, pending.key, owned)


def test_unknown_request_type_is_diagnosed_before_saving():
    repository = create_autospec(CaptureRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), SimpleNamespace(action_id=23))
    held = {23: pending}
    with pytest.raises(ConsistencyError):
        resume_capture_completions(_owned(), pending_capture_completions=held, capture=repository)
    assert held[23] is pending and repository.mock_calls == []


def test_original_request_cannot_be_submitted_for_another_action():
    repository = create_autospec(CaptureRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(23, 1_000))
    held = {24: pending}
    with pytest.raises(ConsistencyError):
        resume_capture_completions(_owned(), pending_capture_completions=held, capture=repository)
    assert held[24] is pending and repository.mock_calls == []


def test_actual_read_still_running_keeps_completion_and_forbids_flow_finish():
    # 若删掉实际 RUNNING 守卫，业务取消会提前结束仍在读取的流程。
    repository = create_autospec(CaptureRepository, instance=True)
    operations = create_autospec(OperationRepository, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(23, 1_000))
    held = {23: pending}
    repository.finish_canceled_capture.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))
    owned = _owned()
    owned.connection.execute.return_value.fetchall.return_value = [
        (9, int(enum_for("operation_runs.kind").READ_FILE), 23, "read/5", 5, 1),
    ]
    with patch("camctl.capture.handlers.OperationRepository", return_value=operations):
        with pytest.raises(ConsistencyError):
            resume_capture_completions(owned, pending_capture_completions=held, capture=repository)
    assert held[23] is pending and pending.input_reads is None
    operations.finish_stale_runs.assert_not_called()
