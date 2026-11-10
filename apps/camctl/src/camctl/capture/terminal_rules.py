"""动作和活动终态对实际采集资格的判断。"""

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.workflow_errors import action_error_id
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES
from camctl.devices.tasks import CaptureResultPolicy

_ACTION_TYPE = enum_for("actions.type")
ACTION_TERMINAL = (3, 4, 5, 6)


def preparation_failure_action_error(action, after):
    if (action.get("status") != 1 or action.get("execution_started") != 0
            or action.get("type") not in CAPTURE_ACTION_TYPES or action.get("cancel_requested") != 0
            or action.get("execution_spec_json") is None
            or after.get("error_code") != action_error_id("capture_failed")):
        return "准备失败要求尚未开始的合法拍摄动作"
    return None


def requires_segment_finish(action):
    """已启动且采用分段结果合同的录像或延时动作须核对原保存。"""
    return (action.get("execution_started") == 1
        and action.get("type") in (_ACTION_TYPE.CAMERA_RECORD, _ACTION_TYPE.CAMERA_TIMELAPSE)
        and action.get("execution_spec_json", {}).get("result_policy")
        in {policy.value for policy in CaptureResultPolicy})


def successful_action_error(action):
    if action.get("cancel_requested") != 0:
        return "取消请求生效的动作不能保存成功终态"
    return None


def activity_capture_action_error(action_id, action):
    if action is None:
        return f"采集判定列要求动作事实: actions#{action_id}"
    if action.get("type") == 2:
        return "录像活动不得携带采集结果或判定依据"
    if not is_json_integer(action.get("type")) or action["type"] not in (1, 3):
        return "采集判定列要求可靠的照片或延时摄影动作类型"
    return None


def activity_capture_transition_error(basis_before, after):
    basis_after = after.get("completion_basis")
    if basis_after != basis_before and basis_after in (2, 3, 4):
        if basis_before not in (None, 1):
            return "采集判定已确定，不能再改判"
        if not after.get("completion_evidence_json"):
            return "确定的采集判定缺少可靠依据"
    return None


def recording_success_error(issues):
    """已知原输入媒体失败不能作为成功录像保存。"""
    if issues:
        return "原输入存在已知媒体失败，不能保存录像成功"
    return None
