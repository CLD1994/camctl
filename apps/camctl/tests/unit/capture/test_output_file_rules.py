"""产物承载文件必须保有实际来源、角色与完整内容资格。"""

from camctl.capture.output_file_rules import (
    capture_binding_error, device_output_error, device_source_ownership_error,
    intermediate_output_ownership_error, output_carrier_error, repair_output_error,
)
from camctl.contracts.enums import enum_for


def test_output_requires_one_explicit_file_source():
    assert output_carrier_error({"device_file_id": 1})
    assert output_carrier_error({"device_file_id": None, "intermediate_file_id": None})
    assert output_carrier_error({"device_file_id": 1, "intermediate_file_id": 2})
    assert output_carrier_error({"device_file_id": 1, "intermediate_file_id": None}) is None


def test_device_output_requires_original_owned_complete_source():
    kinds, roles = enum_for("outputs.kind"), enum_for("device_files.role")
    facts = {"source_action_id": 7, "ownership_evidence_json": {}, "role": int(roles.ORIGINAL),
             "completion_state": 3, "original_device_file_id": None, "pairing_evidence_json": None}
    assert device_source_ownership_error(facts, 7) is None
    assert device_output_error(facts, int(kinds.ORIGINAL), 9) is None
    assert device_source_ownership_error(facts | {"source_action_id": 8}, 7)
    assert device_output_error(facts | {"role": int(roles.PREVIEW)}, int(kinds.ORIGINAL), 9)
    assert device_output_error(facts | {"completion_state": 2}, int(kinds.ORIGINAL), 9)
    assert device_output_error(facts | {"original_device_file_id": 10}, int(kinds.ORIGINAL), 9)


def test_intermediate_file_and_repair_must_be_owned_and_complete():
    purposes = enum_for("intermediate_files.purpose")
    retention = enum_for("intermediate_files.retention_state")
    facts = {"owner_action_id": 7, "owner_delivery_id": None, "purpose": int(purposes.REPAIR_OUTPUT),
             "retention_state": int(retention.REQUIRED), "size_bytes": 12, "sha256": "a" * 64}
    assert intermediate_output_ownership_error(facts, 7) is None
    assert repair_output_error(facts) is None
    assert intermediate_output_ownership_error(facts | {"owner_delivery_id": 1}, 7)
    assert repair_output_error(facts | {"retention_state": 2})
    assert repair_output_error(facts | {"sha256": None})


def test_capture_source_requires_saved_device_and_driver():
    capture = int(enum_for("actions.type").CAMERA_RECORD)
    assert capture_binding_error({"type": capture, "device_id": "camera", "driver_id": "driver"}) is None
    assert capture_binding_error({"type": int(enum_for("actions.type").MOTOR_CONTROL)})
    assert capture_binding_error({"type": capture, "device_id": "camera", "driver_id": ""})


def test_photo_promotion_preserves_original_path_exception(monkeypatch):
    from camctl.capture import photo_pair_rules
    from camctl.host_files.models import FilePurpose

    facts = {"id": 9, "purpose": int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             "retention_state": int(enum_for("intermediate_files.retention_state").REQUIRED),
             "cleanup_state": int(enum_for("intermediate_files.cleanup_state").NOT_NEEDED),
             "size_bytes": 1, "sha256": "a" * 64, "relative_path": "bad"}
    source = {"type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO)}
    failure = ValueError("原文件路径错误")

    def fail(purpose, file_id, path):
        assert purpose is FilePurpose.CAPTURE_ORIGINAL and file_id == 9
        raise failure

    monkeypatch.setattr(photo_pair_rules, "validate_relative_file_path", fail)
    error, cause = photo_pair_rules.host_photo_promotion_failure(
        facts, source, int(enum_for("outputs.kind").ORIGINAL))
    assert error == "主机拍摄源路径与稳定身份不一致"
    assert cause is failure
