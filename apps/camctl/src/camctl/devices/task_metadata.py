"""从实际任务定义生成面向用户的固定任务说明。"""

from camctl.devices.tasks import CancelCompletionPolicy, CaptureTask


def cancellation_description(task: CaptureTask) -> str:
    """说明该任务在启动前后可采用的取消方式及其有限收场边界。"""
    if task.action_type == "camera_take_photo":
        return "拍摄启动前可以取消；拍摄启动后不支持取消。"
    if task.cancel_completion_policy is CancelCompletionPolicy.FINITE_DEVICE_END:
        return ("录像启动后可以提出取消；若设备未确认停止，仍按原有限采集目标等待设备自然结束，"
                "因此不保证立即停止。")
    if task.stop_supported is True:
        return "录像启动后支持取消，并沿现有设备停止确认流程收场。"
    return "任务启动后不支持取消。"
