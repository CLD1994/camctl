"""有限自然延时等待对冻结任务与已物化发送结果的判断。"""

from camctl.capture.models import validate_capture_spec
from camctl.contracts.enums import enum_for
from camctl.devices.capture_completion import validate_control_confirmation
from camctl.devices.capture_timing import CaptureTiming
from camctl.devices.tasks import CompletionMode, EndControl, StartReturn


def validate_natural_wait_definition(activity, action):
    if action is None or action["type"] != int(enum_for("actions.type").CAMERA_TIMELAPSE):
        raise ValueError("自然等待不是延时任务")
    spec = validate_capture_spec("camera_timelapse", action["execution_spec_json"])
    if (not spec["duration_based"] or not spec["wait_after_send"]
            or spec["end_control"] != int(EndControl.DEVICE)
            or spec["start_return_meaning"] != int(StartReturn.SENT)
            or spec["completion_mode"] != int(CompletionMode.TIME_AND_OUTPUTS)
            or activity["action_id"] != action["id"]):
        raise ValueError("自然等待缺少原有限自然任务合同")
    return spec


def validate_natural_wait_run(runs):
    if len(runs) != 1 or runs[0]["kind"] != int(enum_for("operation_runs.kind").START):
        raise ValueError("自然等待 START 与原活动不符")
    return runs[0]


def validate_natural_wait_confirmation(activity, attempt):
    validate_control_confirmation({"status": attempt["status"], "effect_state": attempt["effect_state"],
        "result": attempt["result_json"], "error": attempt["error_json"]},
        "timelapse_sent", str(activity["id"]))


def validate_natural_wait_timing(activity, spec, *, occurred_at, complete):
    expected = CaptureTiming(spec["target_duration_ms"], spec.get("startup_margin_ms", 0),
        spec["result_wait_margin_ms"], activity["extra_wait_ms_used"]).deadline_us(activity["sent_at"])
    if (activity["result_wait_margin_ms"] != spec["result_wait_margin_ms"]
            or activity["expected_check_at"] != expected):
        raise ValueError("自然等待改变原启动余量、目标或发送锚点")
    if complete and occurred_at < expected:
        raise ValueError("自然等待实际完成时刻早于原预计时点")
