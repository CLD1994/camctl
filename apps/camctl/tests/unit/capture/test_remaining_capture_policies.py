"""释放、产物初始状态、设备绑定和录像成功使用明确的原事实。"""

import pytest

from camctl.capture.output_file_rules import capture_binding_issue, initial_output_error
from camctl.capture.release_rules import unstarted_release_allowed
from camctl.capture.terminal_rules import recording_success_error
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.history.validators import EventValidationError
from camctl.persistence.repositories.capture import _capture_binding, _observer_binding


def test_unstarted_release_requires_actual_preparation_terminal_and_never_started():
    facts = {"baseline_state": 3, "dispatch_state": 1, "activity_state": 1, "started_at": None}
    action = {"status": 6}
    assert unstarted_release_allowed(facts, action, False)
    assert unstarted_release_allowed(facts | {"baseline_state": 1}, action, True)
    for change in ({"dispatch_state": 2}, {"activity_state": 2}, {"started_at": 9}):
        assert not unstarted_release_allowed(facts | change, action, True)
    assert not unstarted_release_allowed(facts, {"status": 2}, True)
    assert not unstarted_release_allowed(facts | {"baseline_state": 1}, action, 1)


def test_initial_output_state_and_unknown_error_obey_original_order():
    assert initial_output_error({"cleanup_status": 1, "availability": 1}) is None
    assert initial_output_error({"cleanup_status": 1, "availability": 5, "error_json": {}}) is None
    assert initial_output_error({"cleanup_status": 2, "availability": 0}) == "正式登记的初始清理状态必须是未请求"
    assert initial_output_error({"cleanup_status": 1, "availability": 2})
    assert initial_output_error({"cleanup_status": 1, "availability": 5, "error_json": None})


def test_both_binding_entries_share_qualification_but_keep_original_errors():
    action = {"type": int(enum_for("actions.type").CAMERA_RECORD), "device_id": "cam", "driver_id": "driver"}
    assert capture_binding_issue(action) is None
    assert _capture_binding(action) == _observer_binding(action) == ("cam", "driver")
    with pytest.raises(ConsistencyError, match="设备文件观察者必须是拍摄动作"):
        _observer_binding(None)
    with pytest.raises(EventValidationError, match="产物来源和设备文件观察者必须是拍摄动作"):
        _capture_binding(action | {"type": int(enum_for("actions.type").MOTOR_CONTROL)})
    with pytest.raises(ConsistencyError, match="设备文件观察者缺少已保存的设备或驱动绑定"):
        _observer_binding(action | {"driver_id": ""})
    with pytest.raises(EventValidationError, match="拍摄动作缺少已保存的设备或驱动绑定"):
        _capture_binding(action | {"driver_id": ""})


def test_known_media_failure_blocks_recording_success_without_replacing_facts():
    assert recording_success_error(()) is None
    failure = object()
    assert recording_success_error((failure,)) == "原输入存在已知媒体失败，不能保存录像成功"
