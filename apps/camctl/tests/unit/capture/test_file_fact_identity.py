"""文件子事实的原申请使用精确 JSON 值，可靠响应只复用于同一输入。"""

from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import (
    FileCompletionSave, FileObservationSave, FilePresenceSave,
    ObservationDisposition, ObservationOutcome, OwnershipSave,
)
from camctl.capture.handlers import (
    CaptureRuntime, DeviceControlPort, FileFactStage, PendingFileFact,
    PendingFileObservation, ResultFilesPort,
    _register_observed,
)
from camctl.capture.result_inputs import ObservedFile
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.persistence.runtime import OwnedConnection


def _runtime_with_saved_ownership():
    repository = create_autospec(CaptureRepository, instance=True)
    runtime = CaptureRuntime(
        owned=create_autospec(OwnedConnection, instance=True),
        scheduling=create_autospec(SchedulingRepository, instance=True),
        operations=create_autospec(OperationRepository, instance=True),
        capture=repository,
        timelapse=create_autospec(TimelapseRepository, instance=True),
        driver=create_autospec(DeviceControlPort, instance=True),
        results=create_autospec(ResultFilesPort, instance=True),
        evidence=None,
        wall_us=lambda: 100,
        monotonic_ns=lambda: 1000,
        window_of=lambda action: None,
        wait_config=lambda action: None,
    )
    command = OwnershipSave(
        file_id=3, source_action_id=1, method=1, role=2,
        observation={"flag": True, "camera": {"channel": 1}}, occurred_at=100,
    )
    response = ObservationOutcome(ObservationDisposition.SAVED, file_id=3)
    fact = PendingFileFact(command, new_operation_key(), response)
    runtime.pending_file_observations[(1, "original")] = PendingFileObservation(
        FileObservationSave(1, "original", {"path": "original.mp4"}, 100),
        new_operation_key(), (), {}, facts={FileFactStage.OWNERSHIP: fact},
    )
    return runtime, repository, fact


def test_saved_file_fact_rejects_json_integer_replacing_original_boolean():
    runtime, repository, original = _runtime_with_saved_ownership()
    changed = replace(original.command,
                      observation={"flag": 1, "camera": {"channel": 1}})

    with pytest.raises(ConsistencyError):
        runtime._save_file_fact((1, "original"), FileFactStage.OWNERSHIP, changed)

    assert runtime.pending_file_observations[(1, "original")].facts[
        FileFactStage.OWNERSHIP] is original
    repository.save_file_ownership.assert_not_called()


def test_saved_file_fact_reuses_response_when_only_json_member_order_changes():
    runtime, repository, original = _runtime_with_saved_ownership()
    reordered = replace(original.command,
                        observation={"camera": {"channel": 1}, "flag": True})

    runtime._save_file_fact((1, "original"), FileFactStage.OWNERSHIP, reordered)

    assert runtime.pending_file_observations[(1, "original")].facts[
        FileFactStage.OWNERSHIP] is original
    repository.save_file_ownership.assert_not_called()


@pytest.mark.parametrize("stage,command,field", [
    (FileFactStage.PRESENCE, FilePresenceSave(3, 2, 100, locator={"flag": True}), "locator"),
    (FileFactStage.PRESENCE, FilePresenceSave(3, 2, 100, error={
        "code": "device_error", "stage": "device", "details": {"flag": True}}), "error"),
    (FileFactStage.OWNERSHIP, OwnershipSave(3, 1, 1, 3, {"listed": "preview"}, 100,
        paired_device_file_id=9, pairing_observation={"flag": True}), "pairing_observation"),
    (FileFactStage.COMPLETION, FileCompletionSave(3, 3, 100, basis=1,
        observation={"flag": True}, size_bytes=41), "observation"),
    (FileFactStage.COMPLETION, FileCompletionSave(3, 3, 100, basis=1,
        observation={"complete": True}, size_bytes=41, locator={"flag": True}), "locator"),
    (FileFactStage.COMPLETION, FileCompletionSave(3, 4, 100, error={
        "code": "device_error", "stage": "device", "details": {"flag": True}}), "error"),
])
def test_saved_file_fact_rejects_changed_json_type_in_each_request(stage, command, field):
    runtime, repository, _ = _runtime_with_saved_ownership()
    original = PendingFileFact(command, new_operation_key(),
                              ObservationOutcome(ObservationDisposition.SAVED, 3))
    runtime.pending_file_observations[(1, "original")].facts.clear()
    runtime.pending_file_observations[(1, "original")].facts[stage] = original
    changed_value = ({"code": "device_error", "stage": "device", "details": {"flag": 1}}
                     if field == "error" else {"flag": 1})

    with pytest.raises(ConsistencyError):
        runtime._save_file_fact((1, "original"), stage, replace(command, **{field: changed_value}))

    assert runtime.pending_file_observations[(1, "original")].facts[stage] is original
    repository.save_file_presence.assert_not_called()
    repository.save_file_ownership.assert_not_called()
    repository.save_file_completion.assert_not_called()


def test_saved_file_fact_rejects_changed_non_json_fact_time():
    runtime, repository, original = _runtime_with_saved_ownership()

    with pytest.raises(ConsistencyError):
        runtime._save_file_fact((1, "original"), FileFactStage.OWNERSHIP,
                               replace(original.command, occurred_at=101))

    assert runtime.pending_file_observations[(1, "original")].facts[
        FileFactStage.OWNERSHIP] is original
    repository.save_file_ownership.assert_not_called()


@pytest.mark.parametrize("field", ["locator", "evidence"])
def test_file_discovery_rejects_json_integer_replacing_original_boolean(field):
    runtime, repository, _ = _runtime_with_saved_ownership()
    entry = ObservedFile("original", {"flag": True}, {"flag": True}, False, None)
    original = PendingFileObservation(
        FileObservationSave(1, "original", entry.locator, 100), new_operation_key(),
        (entry,), {},
    )
    runtime.pending_file_observations[(1, "original")] = original

    with pytest.raises(ConsistencyError):
        _register_observed(runtime, 1, (replace(entry, **{field: {"flag": 1}}),),
                           occurred_at=100)

    assert runtime.pending_file_observations[(1, "original")] is original
    repository.save_file_observation.assert_not_called()
