"""媒体保存原键必须核实完整原申请，不能只比较事件类型与时刻。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.processing import (
    CheckPhase, CheckResultSave, MediaObservation, ProcessingDisposition, ProcessingError, RepairBasis,
    RepairDecisionChoice, RepairDecisionSave, RepairOutcome, RepairReason,
    RepairResultSave, RepairStart, RepairSuccess,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCapture, register_capture_guards
from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import TransactionError, saved_transaction_events

from .test_media_execution import _NOW, _seed_pending_repair
from .media_retry_fixtures import media_pipeline as pipeline  # noqa: F401


pytestmark = pytest.mark.asyncio


async def _original_request(owned, branch):
    repository = CaptureRepository()
    if branch in ("check", "decision"):
        started = repository.save_check_result(
            CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), _NOW + 1),
            new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        check = CheckResultSave(1,
            MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")), _NOW + 2)
        if branch == "check":
            return repository.save_check_result, check
        checked = repository.save_check_result(check, new_operation_key(), owned)
        assert checked.kind is DbOutcomeKind.COMPLETED, checked.error
        return repository.save_repair_decision, RepairDecisionSave(1, RepairDecisionChoice.PENDING,
            RepairBasis(RepairReason.THRESHOLD_REACHED, 60000, Decimal("70"), Decimal("75.125")), _NOW + 3)
    await _seed_pending_repair(owned)
    if branch == "repair_failed":
        return repository.save_repair_result, RepairResultSave(1, RepairOutcome.FAILED, _NOW + 4,
            error=ProcessingError("tool_failed", "repair", {"error": "original tool result"}))
    start = RepairStart(1, "mp4", _NOW + 4)
    if branch == "start":
        return repository.start_repair_output, start
    started = repository.start_repair_output(start, new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    return repository.complete_repair_output, RepairSuccess(
        1, started.value.file_id, 250, "a" * 64, _NOW + 5)


def _changed(command, change):
    if change == "processing":
        return replace(command, processing_id=2)
    if change == "time":
        return replace(command, occurred_at=command.occurred_at + 1)
    if change == "media":
        return replace(command, media=MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("76")))
    if change == "basis":
        return replace(command, basis=replace(command.basis, threshold_s=Decimal("71")))
    if change == "decision":
        return replace(command, decision=RepairDecisionChoice.NOT_NEEDED,
            basis=RepairBasis(RepairReason.BELOW_THRESHOLD, 60000, Decimal("80"), Decimal("75.125")))
    if change == "error":
        return replace(command, error=ProcessingError("tool_failed", "repair", {"error": "different tool result"}))
    if change == "phase":
        return replace(command, phase=RepairOutcome.CANCELED, error=None)
    if change == "extension":
        return replace(command, extension="mkv")
    if change == "output":
        return replace(command, output_file_id=701)
    if change == "size":
        return replace(command, size_bytes=251)
    if change == "digest":
        return replace(command, sha256="b" * 64)
    raise AssertionError(change)


@pytest.mark.parametrize(("branch", "change"), [
    ("check", "processing"), ("check", "media"), ("check", "time"),
    ("decision", "processing"), ("decision", "basis"), ("decision", "decision"), ("decision", "time"),
    ("repair_failed", "processing"), ("repair_failed", "error"), ("repair_failed", "phase"), ("repair_failed", "time"),
    ("start", "processing"), ("start", "extension"), ("start", "time"),
    ("complete", "processing"), ("complete", "output"), ("complete", "size"), ("complete", "digest"), ("complete", "time"),
])
async def test_media_original_key_rejects_changed_full_request(pipeline, branch, change):
    register_capture_guards()
    register_outputs_guards()
    owned = pipeline[0]
    save, command = await _original_request(owned, branch)
    key = new_operation_key()
    result = save(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    before = tuple(owned.connection.iterdump())
    repeated = save(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert tuple(owned.connection.iterdump()) == before
    changed = save(_changed(command, change), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(changed.error, TransactionError), changed.error
    assert tuple(owned.connection.iterdump()) == before


async def test_check_running_key_cannot_be_reused_as_actual_observation(pipeline):
    register_capture_guards()
    owned = pipeline[0]
    repository, key = CaptureRepository(), new_operation_key()
    started = repository.save_check_result(
        CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), _NOW + 1), key, owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    before = tuple(owned.connection.iterdump())
    result = repository.save_check_result(CheckResultSave(1,
        MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")), _NOW + 1), key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("branch", ["check", "complete"])
async def test_media_original_key_rejects_missing_original_owner_link(pipeline, branch):
    register_capture_guards()
    register_outputs_guards()
    owned = pipeline[0]
    save, command = await _original_request(owned, branch)
    key = new_operation_key()
    result = save(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    original = saved_transaction_events(owned.connection, key)
    first_id = original[0]["transaction"].first_event_id
    links = owned.connection.execute(
        "SELECT entity_type,entity_id FROM entity_event_links WHERE event_id=?", (first_id,)).fetchall()
    assert len(links) == 1
    owned.connection.execute("DELETE FROM entity_event_links WHERE event_id=?", (first_id,))
    before = tuple(owned.connection.iterdump())
    repeated = save(command, key, owned)
    assert repeated.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(repeated.error, ConsistencyError), repeated.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("branch", ["check_running", "start", "complete"])
async def test_media_original_key_uses_original_boundary_after_later_progress(pipeline, branch):
    """原 F 之后的合法推进不替代原申请的判定依据。"""
    owned, _roots, source_id = pipeline
    repository, key = CaptureRepository(), new_operation_key()
    if branch == "check_running":
        save = repository.save_check_result
        command = CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), _NOW + 1)
        changed = replace(command, media=MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")))
    else:
        save, command = await _original_request(owned, branch)
        changed = _changed(command, "extension" if branch == "start" else "digest")
    original = save(command, key, owned)
    assert original.kind is DbOutcomeKind.COMPLETED, original.error
    original_events = saved_transaction_events(owned.connection, key)
    original_boundary = original_events[0]["transaction"].last_event_id
    if branch == "check_running":
        advanced = repository.save_check_result(CheckResultSave(1,
            MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")), _NOW + 2),
            new_operation_key(), owned)
    elif branch == "start":
        advanced = repository.complete_repair_output(RepairSuccess(
            1, original.value.file_id, 250, "a" * 64, _NOW + 5), new_operation_key(), owned)
    else:
        advanced = repository.finish_capture(FinishCapture(1, (
            OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=source_id), True),
            OutputDraft(OutputKind.REPAIRED, FileReference(intermediate_file_id=command.output_file_id),
                True, sha256=command.sha256, original_batch_file_id=source_id),
        ), OutputCatalogFacts(1, True), _NOW + 6), new_operation_key(), owned)
    assert advanced.kind is DbOutcomeKind.COMPLETED, advanced.error
    assert owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] > original_boundary
    before = tuple(owned.connection.iterdump())
    repeated = save(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value.disposition is ProcessingDisposition.ALREADY
    if branch == "start":
        assert (repeated.value.file_id, repeated.value.relative_path) == (
            original.value.file_id, original.value.relative_path)
    assert tuple(owned.connection.iterdump()) == before
    rejected = save(changed, key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError), rejected.error
    assert tuple(owned.connection.iterdump()) == before
