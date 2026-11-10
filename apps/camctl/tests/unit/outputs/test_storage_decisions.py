"""已物化业务事实的保存资格与读取结束判定。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.operations.models import AttemptTicket
from camctl.outputs.cleanup_absence import cleanup_absence_file_complete, cleanup_absence_projection_complete
from camctl.outputs.copy import read_end_completed, validate_read_end_facts
from camctl.outputs.handoff import publishable_copy_error
from camctl.outputs.photo_read import photo_read_attempt_matches


def _read_facts():
    copy = dict(round=2, source_device_file_id=8, source_intermediate_file_id=None,
                target_file_id=9, source_size=10, committed_bytes=10, reset_state=1)
    document = dict(copy_round=2, source_device_file_id=8, source_intermediate_file_id=None,
                    target_file_id=9, source_size=10, committed_bytes=10, reset_state=1,
                    end=dict(stopped=True, bytes_read=10, error=None))
    return copy, document


@pytest.mark.parametrize("field,value", [("copy_round", 1), ("source_device_file_id", 7),
    ("target_file_id", 8), ("committed_bytes", 9), ("reset_state", 2)])
def test_read_end_rejects_changed_identity_or_progress(field, value):
    copy, document = _read_facts()
    validate_read_end_facts(copy, document)
    document[field] = value
    with pytest.raises(ConsistencyError, match="当前轮次、来源或可靠进度"):
        validate_read_end_facts(copy, document)


@pytest.mark.parametrize("end", [dict(stopped=False, bytes_read=10, error=None),
    dict(stopped=True, bytes_read=11, error=None), dict(stopped=True, bytes_read=True, error=None),
    dict(stopped=True, bytes_read=10, error="failed")])
def test_read_end_requires_actual_complete_stop(end):
    assert not read_end_completed(**end, source_size=10)
    assert read_end_completed(stopped=True, bytes_read=10, error=None, source_size=10)


def test_publish_requires_verified_complete_copy_in_order():
    verification = enum_for("file_copies.verification_state")
    assert publishable_copy_error(verification_state=int(verification.MATCHED), committed_bytes=5, source_size=5) is None
    assert publishable_copy_error(verification_state=int(verification.SOURCE_CHECKSUM_UNAVAILABLE), committed_bytes=5, source_size=5) is None
    assert publishable_copy_error(verification_state=0, committed_bytes=4, source_size=5) == "副本未完成字节校验不能准备或发布"
    assert publishable_copy_error(verification_state=int(verification.MATCHED), committed_bytes=4, source_size=5) == "副本字节未完整保存不能准备或发布"


def test_absence_requires_completed_projection_and_original_file():
    assert cleanup_absence_projection_complete(availability=3, cleanup_status=4, cleanup_error=None)
    assert not cleanup_absence_projection_complete(availability=3, cleanup_status=4, cleanup_error={})
    assert cleanup_absence_file_complete(device_file={"presence_state": 3}, intermediate_file=None)
    assert cleanup_absence_file_complete(device_file=None, intermediate_file={"cleanup_state": 4})
    assert not cleanup_absence_file_complete(device_file={"presence_state": 1}, intermediate_file={"cleanup_state": 4})
    assert not cleanup_absence_file_complete(device_file=None, intermediate_file=None)


def _photo_facts():
    ticket = AttemptTicket(2, "read", "7", "read/7", 5)
    copy = dict(round=3, slot_device_id="camera", read_end_event_id=None,
                verification_state=int(enum_for("file_copies.verification_state").NOT_PERFORMED),
                source_sha256=None, target_sha256=None)
    run = dict(kind=int(enum_for("operation_runs.kind").READ_FILE),
               status=int(enum_for("operation_runs.status").ACTIVE), copy_id=7, delivery_id=10,
               action_id=11, attempts_used=2, responsibility_key="read/7")
    attempt = dict(status=int(enum_for("operation_attempts.status").RUNNING),
                   intent_event_id=1, result_event_id=None, copy_round=3)
    return dict(copy_id=7, copy=copy, source=dict(size_bytes=None, sha256=None),
                origin=dict(device_id="camera"), run=run, attempt=attempt,
                delivery=dict(id=10, action_id=11), ticket=ticket)


@pytest.mark.parametrize("object_name,field,value", [
    ("source", "size_bytes", 10), ("run", "responsibility_key", "read/8"),
    ("run", "action_id", 12), ("attempt", "copy_round", 2),
    ("attempt", "result_event_id", 13), ("copy", "slot_device_id", "other"),
    ("copy", "read_end_event_id", 14), ("copy", "source_sha256", "a" * 64),
])
def test_photo_length_requires_original_live_attempt(object_name, field, value):
    facts = _photo_facts()
    assert photo_read_attempt_matches(**facts)
    facts[object_name][field] = value
    assert not photo_read_attempt_matches(**facts)


def _source_facts():
    action = enum_for("actions.status")
    return dict(
        selection=dict(status=int(enum_for("obtain_source_selections.status").FIXED), error_code=None, error_details_json=None),
        owner=dict(type=int(enum_for("actions.type").OBTAIN_ACTION_OUTPUTS), status=int(action.RUNNING),
                   cancel_requested=0, source_resolution_state=int(enum_for("actions.source_resolution_state").FIXED),
                   resolved_source_plan_id=22),
        source=dict(type=int(enum_for("actions.type").CAMERA_TAKE_PHOTO), status=int(action.SUCCEEDED), plan_id=22),
    )


@pytest.mark.parametrize("object_name,field,value", [
    ("selection", "error_code", 1), ("owner", "cancel_requested", 1),
    ("owner", "resolved_source_plan_id", 23), ("source", "status", int(enum_for("actions.status").RUNNING)),
    ("source", "type", int(enum_for("actions.type").OBTAIN_ACTION_OUTPUTS)),
])
def test_obtain_source_must_be_fixed_terminal_and_owned(object_name, field, value):
    from camctl.outputs.sources import obtain_source_ready
    facts = _source_facts()
    assert obtain_source_ready(**facts)
    facts[object_name][field] = value
    assert not obtain_source_ready(**facts)


def test_obtain_member_requires_unresolved_explicit_identity():
    from camctl.outputs.sources import obtain_member_verification_error
    status, basis = enum_for("obtain_items.status"), enum_for("obtain_items.basis")
    item = dict(status=int(status.UNRESOLVED), basis=int(basis.EXPLICIT), requested_output_id=3,
                output_id=None, source_dependency=0, delivery_id=None, error_code=None, error_details_json=None)
    assert obtain_member_verification_error(item) is None
    assert obtain_member_verification_error(item | {"delivery_id": 5}) == "只有尚未建档或结束的取回项能核实或拒绝"
    assert obtain_member_verification_error(item | {"output_id": 3}) == "未核实成员必须是尚未关联产物的显式请求"
    assert obtain_member_verification_error(item | {"requested_output_id": None}) == "显式成员必须保留原请求产物 ID"


@pytest.mark.parametrize("choice_name,state_name,item_name,error_name", [
    ("WITHDRAWN", "WITHDRAWN", "WITHDRAWN", None),
    ("NOT_RETRACTABLE", "NOT_RETRACTABLE", "NOT_RETRACTABLE", None),
    ("FAILED", "FAILED", "FAILED", "delivery_withdrawal_failed"),
    ("UNKNOWN", "UNKNOWN", "FAILED", "delivery_position_unconfirmed"),
])
def test_withdrawal_result_preserves_delivery_and_waiter_meaning(choice_name, state_name, item_name, error_name):
    from camctl.contracts.workflow_errors import item_error_id
    from camctl.outputs.handoff import WithdrawalChoice, withdrawal_item_result, withdrawal_result_state
    choice = WithdrawalChoice[choice_name]
    assert withdrawal_result_state(choice) == int(enum_for("deliveries.withdrawal_state")[state_name])
    expected_error = None if error_name is None else item_error_id("cancel_delivery_items", error_name)
    expected_details = None if error_name is None else {"delivery_id": "7"}
    assert withdrawal_item_result(choice, 7) == (int(enum_for("cancel_delivery_items.status")[item_name]), expected_error, expected_details)
