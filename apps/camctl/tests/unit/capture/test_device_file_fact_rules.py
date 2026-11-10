"""设备文件资格只根据已取得的文件事实和原动作归属判定。"""

import pytest

from camctl.capture.device_file_rules import (
    completion_facts_error, incomplete_facts_error, ownership_claim_error,
    ownership_pair_error, checksum_decision_error, checksum_value_error,
)
from camctl.contracts.enums import enum_for


_ROLE = enum_for("device_files.role")
_OWNERSHIP = enum_for("device_files.ownership_evidence_json.method")
_PAIRING = enum_for("device_files.pairing_evidence_json.method")
_COMPLETION = enum_for("device_files.completion_evidence_json.basis")
_CHECKSUM = enum_for("device_files.checksum_support")


def _claim():
    return {"source_action_id": 3, "role": int(_ROLE.PREVIEW),
        "ownership_evidence_json": {"method": int(_OWNERSHIP.DRIVER_TASK_ASSOCIATION), "observation": {}},
        "original_device_file_id": 8,
        "pairing_evidence_json": {"method": int(_PAIRING.DRIVER_PAIRING), "observation": {}}}


def test_ownership_accepts_one_original_for_same_source():
    before = {"role": int(_ROLE.UNDETERMINED)}
    claim = _claim()
    assert ownership_claim_error(9, before, claim) is None
    assert ownership_pair_error({"role": int(_ROLE.ORIGINAL), "source_action_id": 3}, 3) is None
    assert ownership_pair_error({"role": int(_ROLE.ORIGINAL), "source_action_id": 4}, 3)


@pytest.mark.parametrize("before,change", [
    ({"source_action_id": 2, "role": int(_ROLE.UNDETERMINED)}, {}),
    ({"role": int(_ROLE.ORIGINAL)}, {}),
    ({"role": int(_ROLE.UNDETERMINED)}, {"source_action_id": True}),
    ({"role": int(_ROLE.UNDETERMINED)}, {"role": int(_ROLE.UNDETERMINED)}),
    ({"role": int(_ROLE.UNDETERMINED)}, {"ownership_evidence_json": {}}),
    ({"role": int(_ROLE.UNDETERMINED)}, {"original_device_file_id": 9}),
    ({"role": int(_ROLE.UNDETERMINED)}, {"pairing_evidence_json": None}),
])
def test_ownership_rejects_ambiguous_claim(before, change):
    assert ownership_claim_error(9, before, _claim() | change)


def test_completed_file_requires_reliable_size_or_original_photo_guarantee():
    evidence = {"basis": int(_COMPLETION.DEVICE_GUARANTEE), "observation": {}}
    assert completion_facts_error(10, evidence) is None
    assert completion_facts_error(True, evidence)
    assert completion_facts_error(None, evidence)
    photo = {"basis": int(_COMPLETION.DEVICE_GUARANTEE), "observation": {"photo_start": {}}}
    assert completion_facts_error(None, photo) is None
    photo["basis"] = int(_COMPLETION.TIME_AND_OUTPUTS)
    assert completion_facts_error(None, photo)
    assert incomplete_facts_error(1, {"size_bytes": 1})
    assert incomplete_facts_error(4, {"last_error_json": None})


def test_checksum_requires_resolved_support_and_completed_file():
    facts = {"checksum_support": int(_CHECKSUM.UNDETERMINED), "completion_state": 3}
    assert checksum_decision_error(facts, int(_CHECKSUM.SUPPORTED)) is None
    assert checksum_value_error(facts, int(_CHECKSUM.SUPPORTED), "a" * 64) is None
    assert checksum_value_error(facts, int(_CHECKSUM.SUPPORTED), "A" * 64)
    assert checksum_value_error(facts, int(_CHECKSUM.UNSUPPORTED), "a" * 64)
    assert checksum_value_error(facts, int(_CHECKSUM.UNSUPPORTED), None) is None
    assert checksum_decision_error({"checksum_support": int(_CHECKSUM.SUPPORTED)}, int(_CHECKSUM.UNSUPPORTED))


def test_conflicting_facts_keep_original_failure_order():
    before = {"source_action_id": 2, "role": int(_ROLE.ORIGINAL)}
    assert ownership_claim_error(9, before, _claim() | {"source_action_id": True}) == "文件来源只能从未知一次确认"
    assert completion_facts_error(None, {}) == "完成状态必须携带非负完整大小: None"
    assert incomplete_facts_error(4, {"size_bytes": 1, "last_error_json": None}) == "只有完成状态携带完整大小"
