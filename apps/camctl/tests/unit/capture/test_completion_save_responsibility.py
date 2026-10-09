"""拍摄完整申请的类型分派、失败责任与可靠完成释放。"""

from types import SimpleNamespace
from unittest.mock import create_autospec, patch
import sqlite3

import pytest

from camctl.capture.handlers import (
    CaptureRuntime, PendingCaptureCompletion, StartCloseRequest, resume_capture_completions,
)
from camctl.capture.media import RecordingFailure
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.enums import enum_for
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.operations.attempts import RetryWaitGate, RunOutcome, StaleRunFinish
from camctl.operations.models import ErrorValue
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


def _start_close_request():
    failure = RecordingFailure("capture_result_unconfirmed", {
        "activity_id": "23", "reason": "start_unknown"})
    return StartCloseRequest(
        StaleRunFinish(("start/23", "query/start/23/23"), RunOutcome.UNCONFIRMED,
            1_000, ErrorValue(failure.code, "execution", failure.details)),
        FinishCapture(23, (), OutputCatalogFacts(23, True), 1_000, failure=failure))


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED])
def test_unreliable_start_close_preserves_both_requests_and_waits(kind):
    # 若把嵌套动作单独保存，或提前清除等待，原复合责任便丢失。
    request = _start_close_request()
    pending = PendingCaptureCompletion(new_operation_key(), request)
    held = {23: pending}
    gate = RetryWaitGate({"start/23": 9, "query/start/23/23": 10, "start/24": 11})
    repository = create_autospec(CaptureRepository, instance=True)
    repository.close_start.return_value = DbOutcome(kind, error=RuntimeError("共同保存未完成"))
    owned = _owned()
    with pytest.raises(ConsistencyError):
        resume_capture_completions(owned, pending_capture_completions=held,
            capture=repository, retry_gate=gate)
    assert held[23] is pending and pending.request is request
    assert gate.anchors == {"start/23": 9, "query/start/23/23": 10, "start/24": 11}
    repository.close_start.assert_called_once_with(request.finish, request.action_finish, pending.key, owned)
    repository.finish_capture.assert_not_called()


def test_start_close_legal_empty_success_releases_only_its_responsibilities():
    # close_start 的空返回值是完整成功；错误要求 CaptureResult 会遗留责任。
    request = _start_close_request()
    pending = PendingCaptureCompletion(new_operation_key(), request)
    held = {23: pending}
    gate = RetryWaitGate({"start/23": 9, "query/start/23/23": 10, "start/24": 11})
    repository = create_autospec(CaptureRepository, instance=True)
    repository.close_start.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    owned = _owned()
    resume_capture_completions(owned, pending_capture_completions=held,
        capture=repository, retry_gate=gate)
    assert held == {} and gate.anchors == {"start/24": 11}
    repository.close_start.assert_called_once_with(request.finish, request.action_finish, pending.key, owned)
    owned.connection.execute.assert_not_called()


def test_new_start_close_cannot_replace_unconfirmed_original_request():
    # 首次保存入口必须拒绝覆盖原完整申请，不能换 key 或决定时刻。
    request = _start_close_request()
    pending = PendingCaptureCompletion(new_operation_key(), request)
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.pending_capture_completions = {23: pending}
    replacement = _start_close_request()
    with pytest.raises(ConsistencyError):
        CaptureRuntime.save_capture_completion(runtime, replacement)
    assert runtime.pending_capture_completions[23] is pending
    assert pending.request is request and pending.request is not replacement
