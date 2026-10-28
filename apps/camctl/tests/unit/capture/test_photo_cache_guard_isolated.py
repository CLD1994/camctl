"""单角色缓存事件独立检查完整 F 前状态。"""
import importlib.util
import sys
from enum import IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    class Purpose(IntEnum): CAPTURE_ORIGINAL = 1; CAPTURE_PREVIEW = 2
    class Retention(IntEnum): REQUIRED = 1; PROMOTED = 2; RELEASABLE = 3
    class Cleanup(IntEnum): NOT_NEEDED = 1; COMPLETED = 2
    class Action(IntEnum): CAMERA_TAKE_PHOTO = 1
    class ActionStatus(IntEnum): RUNNING = 2
    enums = {"intermediate_files.purpose": Purpose, "intermediate_files.retention_state": Retention,
             "intermediate_files.cleanup_state": Cleanup, "actions.type": Action, "actions.status": ActionStatus}
    modules = {
        "camctl.acceptance.definitions": {"read_action_spec": lambda action: action["execution_spec_json"]},
        "camctl.contracts.enums": {"enum_for": lambda name: enums[name]},
        "camctl.contracts.history_values": {"HistoryBoundary": object},
        "camctl.contracts.json_values": {"json_equal": lambda left, right: left == right},
        "camctl.contracts.values": {"ConsistencyError": ValueError, "ObjectId": int, "UtcMicros": int},
        "camctl.capture.photo_format": {"saved_photo_format": lambda action: "raw"},
        "camctl.host_files.photo_formats": {"photo_file_extension": lambda purpose, fmt: "3fr"},
        "camctl.host_files.models": {"FilePurpose": Purpose},
        "camctl.host_files.paths": {"HOST_PHOTO_PURPOSES": (Purpose.CAPTURE_ORIGINAL, Purpose.CAPTURE_PREVIEW),
            "relative_file_path": lambda purpose, identity, extension: f"cache/{identity}.{extension}",
            "validate_relative_file_path": lambda *args: None},
        "camctl.history.host_photo_request": {"HostPhotoComplete": object, "PrepareHostPhoto": object,
                                               "parse_host_photo_request": lambda *args, **kwargs: None},
        "camctl.history.photo_cache_request": {"parse_photo_cache_request": lambda raw: SimpleNamespace(**raw)},
        "camctl.history.events": {"branch_of": lambda *args: ("LIFECYCLE", {}), "reason_for_branch": lambda *args: 2},
        "camctl.history.validators": {"EventValidationError": type("EventValidationError", (ValueError,), {})},
        "camctl.persistence.models": {"DbOutcome": object, "DbOutcomeKind": object},
        "camctl.persistence.runtime": {"require_host_photo_format": lambda *args: None},
        "camctl.persistence.row_history": {"read_row_values_at_boundary": lambda *args, **kwargs: None},
        "camctl.persistence.transaction": {name: object for name in ("CommandPlan", "TransactionError", "TransactionScope",
            "commit_operation", "event_envelope", "next_row_id", "row_change", "row_facts", "saved_transaction_events", "update_change")},
    }
    for name, attributes in modules.items():
        stub = ModuleType(name)
        stub.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, stub)
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/host_capture.py"
    name = "camctl.persistence.repositories._isolated_photo_cache_guard"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def proposed(failure):
    error = {"message": "original archive error"} if failure else None
    request = {"action_id": 1, "file_id": 2, "device_file_id": 3, "result_page_event_id": 4,
        "selected": "camera", "size_bytes": 100, "sha256": "a" * 64, "occurred_at": 50,
        "archive_error": error}
    before = {"last_error_json": None} if failure else {"size_bytes": None, "sha256": None}
    after = ({"last_error_json": {"stage": "photo_cache_archive", "diagnostic": error}} if failure
             else {"size_bytes": 100, "sha256": "a" * 64})
    row = SimpleNamespace(table="intermediate_files", row_id=2,
        before=SimpleNamespace(exists=True, values=before), after=SimpleNamespace(exists=True, values=after))
    event = SimpleNamespace(event_type=26, reason=2, event_id=60, occurred_at=50,
                            evidence={"photo_cache_request": request}, rows=(row,))
    return event


@pytest.mark.parametrize("existing,proposed_failure", [
    ("success", False), ("success", True), ("failure", False), ("failure", True),
])
def test_no_second_cache_outcome_after_either_saved_result(api, existing, proposed_failure):
    file = {"id": 2, "owner_action_id": 1, "owner_delivery_id": None, "purpose": 1,
            "retention_state": 1, "cleanup_state": 1, "relative_path": "cache/2.3fr",
            "size_bytes": 100 if existing == "success" else None,
            "sha256": "a" * 64 if existing == "success" else None,
            "last_error_json": {"stage": "photo_cache_archive"} if existing == "failure" else None}
    before = dict(file)
    action = {"id": 1, "type": 1, "status": 2, "execution_spec_json": {"output_storage": "device"}}
    context = SimpleNamespace(state_rows={"actions": {1: action}, "intermediate_files": {2: file}},
        photo_result_reads=SimpleNamespace(verify_cache=lambda *args, **kwargs: "original"))
    with pytest.raises(ValueError):
        api.validate_host_photo_event(proposed(proposed_failure), context)
    assert file == before


def test_fresh_role_cache_result_is_accepted(api):
    file = {"id": 2, "owner_action_id": 1, "owner_delivery_id": None, "purpose": 1,
            "retention_state": 1, "cleanup_state": 1, "relative_path": "cache/2.3fr",
            "size_bytes": None, "sha256": None, "last_error_json": None}
    action = {"id": 1, "type": 1, "status": 2, "execution_spec_json": {"output_storage": "device"}}
    context = SimpleNamespace(state_rows={"actions": {1: action}, "intermediate_files": {2: file}},
        photo_result_reads=SimpleNamespace(verify_cache=lambda *args, **kwargs: "original"))
    api.validate_host_photo_event(proposed(False), context)
