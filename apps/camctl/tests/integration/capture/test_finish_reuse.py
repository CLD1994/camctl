"""R4 完成登记原键重送与终态后新键恢复的组件集成测试。

原键重送核实原事务身份、事实时刻、终态分支与产物集合后恢复首
次结果和文件身份；事实不同的重送按操作身份冲突拒绝。动作已终
态后的新键不重新登记、不改写既有终态：输入与既有登记一致时恢
复结果，追加或改写被拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.capture.media import RecordingFailure
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCapture,
    FinishDisposition,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.transaction import TransactionError

from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from .test_recording_finish import _environment, _finish, _original_draft, _NOW

register_operation_guards()
register_capture_guards()


def _repository() -> CaptureRepository:
    return CaptureRepository()


def _finish_two() -> FinishCapture:
    """两份原片草稿（文件 11 与 12），用于集合不一致反例。"""
    command = _finish()
    return FinishCapture(
        action_id=command.action_id,
        drafts=(command.drafts[0], _original_draft(12)),
        catalog_facts=command.catalog_facts,
        occurred_at=command.occurred_at,
    )


def _finish_failed(code: str = "recording_too_short",
                   details: dict | None = None) -> FinishCapture:
    command = _finish()
    return FinishCapture(
        action_id=command.action_id,
        drafts=command.drafts,
        catalog_facts=command.catalog_facts,
        occurred_at=command.occurred_at,
        failure=RecordingFailure(
            code=code, details=details or {"processing_id": "1"}),
    )


def _assert_completed(outcome):
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value


def test_same_key_resend_recovers_first_result(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    first = _assert_completed(repository.finish_capture(_finish(), key, owned))
    assert first.disposition is FinishDisposition.SAVED
    events_before = _events(owned)
    dump_before = tuple(owned.connection.iterdump())

    again = _assert_completed(repository.finish_capture(_finish(), key, owned))
    assert again.disposition is FinishDisposition.ALREADY
    assert again.output_ids == first.output_ids
    assert (again.action_status, again.plan_status) == (
        first.action_status, first.plan_status)
    assert _events(owned) == events_before
    assert tuple(owned.connection.iterdump()) == dump_before


def test_same_key_resend_rejects_different_moment(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    _assert_completed(repository.finish_capture(_finish(), key, owned))
    command = _finish()
    later = FinishCapture(
        action_id=command.action_id, drafts=command.drafts,
        catalog_facts=command.catalog_facts, occurred_at=_NOW + 5)
    outcome = repository.finish_capture(later, key, owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)


def test_same_key_resend_rejects_different_outputs(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    _assert_completed(repository.finish_capture(_finish(), key, owned))
    outcome = repository.finish_capture(_finish_two(), key, owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)


def test_same_key_resend_rejects_branch_mismatch(tmp_path: Path) -> None:
    """首次保存成功终态：同一操作键不能改按失败终态重送。"""
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    _assert_completed(repository.finish_capture(_finish(), key, owned))
    outcome = repository.finish_capture(_finish_failed(), key, owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)


def test_new_key_after_terminal_recovers_without_reregistering(
        tmp_path: Path) -> None:
    """提交未知后的新键：动作已终态时不重新登记，恢复既有身份。"""
    owned = _environment(tmp_path)
    repository = _repository()
    first = _assert_completed(
        repository.finish_capture(_finish(), new_operation_key(), owned))
    events_before = _events(owned)

    recovered = _assert_completed(
        repository.finish_capture(_finish(), new_operation_key(), owned))
    assert recovered.disposition is FinishDisposition.ALREADY
    assert recovered.output_ids == first.output_ids
    assert _events(owned) == events_before


def test_new_key_after_terminal_rejects_appended_outputs(
        tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = _repository()
    _assert_completed(
        repository.finish_capture(_finish(), new_operation_key(), owned))
    dump_before = tuple(owned.connection.iterdump())
    outcome = repository.finish_capture(_finish_two(), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == dump_before


def test_failure_finish_resend_and_recovery(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    first = _assert_completed(
        repository.finish_capture(_finish_failed(), key, owned))
    assert first.action_status == 4

    again = _assert_completed(
        repository.finish_capture(_finish_failed(), key, owned))
    assert again.disposition is FinishDisposition.ALREADY
    assert again.output_ids == first.output_ids

    recovered = _assert_completed(
        repository.finish_capture(
            _finish_failed(), new_operation_key(), owned))
    assert recovered.disposition is FinishDisposition.ALREADY
    assert recovered.output_ids == first.output_ids


def test_same_key_resend_rejects_different_failure_details(
        tmp_path: Path) -> None:
    """同一错误码但 details 不同：首次事实权威，重送按身份冲突拒绝。"""
    owned = _environment(tmp_path)
    repository = _repository()
    key = new_operation_key()
    _assert_completed(repository.finish_capture(
        _finish_failed(code="recording_processing_failed",
                       details={"processing_id": "1", "reason": "check_failed"}),
        key, owned))
    outcome = repository.finish_capture(
        _finish_failed(code="recording_processing_failed",
                       details={"processing_id": "2", "reason": "check_failed"}),
        key, owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)


def test_batch_preview_registration_resend_recovers(tmp_path: Path) -> None:
    """同批配对登记的重送恢复：原片与预览身份按输入次序恢复。"""
    owned = _environment(tmp_path)
    owned.connection.execute(
        "UPDATE device_files SET original_device_file_id=11,"
        " pairing_evidence_json='{}' WHERE id=12")
    owned.connection.commit()
    repository = _repository()
    key = new_operation_key()
    command = FinishCapture(
        action_id=1,
        drafts=(_original_draft(11), OutputDraft(
            kind=OutputKind.PREVIEW,
            file=FileReference(device_file_id=12),
            file_complete=True,
            original_batch_file_id=11,
        )),
        catalog_facts=OutputCatalogFacts(action_id=1, ownership_confirmed=True),
        occurred_at=_NOW,
    )
    first = _assert_completed(repository.finish_capture(command, key, owned))
    assert len(first.output_ids) == 2
    again = _assert_completed(repository.finish_capture(command, key, owned))
    assert again.disposition is FinishDisposition.ALREADY
    assert again.output_ids == first.output_ids


def test_failure_recovery_rejects_different_error(tmp_path: Path) -> None:
    """已保存失败终态：新键按其他错误码或成功分支重送被拒绝。"""
    owned = _environment(tmp_path)
    repository = _repository()
    _assert_completed(
        repository.finish_capture(
            _finish_failed(code="recording_processing_failed",
                           details={"processing_id": "1", "reason": "check_failed"}),
            new_operation_key(), owned))
    dump_before = tuple(owned.connection.iterdump())
    outcome = repository.finish_capture(
        _finish_failed(), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError)
    success = repository.finish_capture(_finish(), new_operation_key(), owned)
    assert success.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == dump_before


def _events(owned) -> int:
    return owned.connection.execute(
        "SELECT COUNT(*) FROM history_events").fetchone()[0]
