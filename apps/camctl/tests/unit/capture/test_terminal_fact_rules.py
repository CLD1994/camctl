"""动作和活动终态只能依据实际动作及采集判定事实。"""

from camctl.capture.terminal_rules import (
    activity_capture_action_error, activity_capture_transition_error,
    preparation_failure_action_error, requires_segment_finish, successful_action_error,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import action_error_id
from camctl.devices.tasks import CaptureResultPolicy


def test_canceled_or_unknown_action_cannot_finish_successfully():
    assert successful_action_error({"cancel_requested": 0}) is None
    assert successful_action_error({"cancel_requested": 1})
    assert successful_action_error({})


def test_segmented_finish_applies_only_to_started_record_or_timelapse():
    policy = next(iter(CaptureResultPolicy)).value
    action = {"execution_started": 1, "type": int(enum_for("actions.type").CAMERA_RECORD),
              "execution_spec_json": {"result_policy": policy}}
    assert requires_segment_finish(action)
    assert not requires_segment_finish(action | {"execution_started": 0})
    assert not requires_segment_finish(action | {"type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO)})
    assert not requires_segment_finish(action | {"execution_spec_json": {}})


def test_preparation_failure_requires_not_started_uncanceled_capture():
    action = {"status": 1, "execution_started": 0, "cancel_requested": 0,
              "type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO), "execution_spec_json": {}}
    after = {"error_code": action_error_id("capture_failed")}
    assert preparation_failure_action_error(action, after) is None
    for change in ({"status": 2}, {"execution_started": 1}, {"cancel_requested": 1},
                   {"type": int(enum_for("actions.type").MOTOR_CONTROL)}, {"execution_spec_json": None}):
        assert preparation_failure_action_error(action | change, after)


def test_activity_capture_only_for_photo_or_timelapse_and_cannot_rejudge():
    assert activity_capture_action_error(7, None)
    assert activity_capture_action_error(7, {"type": int(enum_for("actions.type").CAMERA_RECORD)})
    assert activity_capture_action_error(7, {"type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO)}) is None
    assert activity_capture_transition_error(1, {"completion_basis": 3, "completion_evidence_json": {"method": "actual"}}) is None
    assert activity_capture_transition_error(2, {"completion_basis": 3, "completion_evidence_json": {"method": "actual"}})
    assert activity_capture_transition_error(1, {"completion_basis": 3, "completion_evidence_json": None})
    assert activity_capture_transition_error(2, {"completion_basis": 2}) is None
