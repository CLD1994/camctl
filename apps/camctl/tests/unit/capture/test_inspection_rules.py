"""检查输入资格使用已经取得的原片和读取事实。"""

import pytest

from camctl.capture.inspection_rules import inspection_member_is_eligible
from camctl.contracts.enums import enum_for


def _valid():
    return (
        6, 7, 8,
        {"action_id": 6, "state": int(enum_for("segment_sets.state").MEMBERS_FIXED)},
        {"set_id": 7, "file_id": 8, "role": int(enum_for("segment_members.role").ORIGINAL),
         "ownership": int(enum_for("segment_members.ownership").OWNED),
         "completion": int(enum_for("segment_members.completion").COMPLETE)},
        {"source_action_id": 6, "role": int(enum_for("device_files.role").ORIGINAL),
         "completion_state": int(enum_for("device_files.completion_state").COMPLETE),
         "presence_state": int(enum_for("device_files.presence_state").PRESENT), "size_bytes": 5},
    )


def test_fixed_original_member_is_eligible():
    assert inspection_member_is_eligible(*_valid())


@pytest.mark.parametrize("index,key,value", [
    (3, "action_id", 9), (3, "state", 0), (4, "set_id", 9), (4, "file_id", 9),
    (4, "role", 0), (4, "ownership", 0), (4, "completion", 0),
    (5, "source_action_id", 9), (5, "role", 0), (5, "completion_state", 0),
    (5, "presence_state", 0), (5, "size_bytes", None),
])
def test_each_mismatched_source_fact_is_ineligible(index, key, value):
    facts = _valid()
    facts[index][key] = value
    assert not inspection_member_is_eligible(*facts)


def test_binding_failure_keeps_original_read_error_and_rejects_other_stage():
    from camctl.capture.inspection_rules import binding_read_error, binding_result_blocked
    from camctl.contracts.values import ConsistencyError

    error = {"code": "device_binding_unavailable", "details": {"reason": "original"}}
    document = {"failure": {"code": "read_failed", "read_error": error}}
    assert binding_read_error(document) is error
    assert binding_read_error({"failure": {"code": "read_failed"}}) is None
    with pytest.raises(ConsistencyError):
        binding_read_error({"failure": {"code": "probe_failed", "read_error": error}})
    assert not binding_result_blocked({"status": 2, "cancel_requested": 0})
    assert binding_result_blocked({"status": 2, "cancel_requested": 1})
    assert binding_result_blocked({"status": 3, "cancel_requested": 0})
