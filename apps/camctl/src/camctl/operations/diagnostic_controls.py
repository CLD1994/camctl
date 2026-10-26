"""拍摄调用的正常控制原因；实际传输和收场错误继续保留诊断。"""

from collections.abc import Mapping

from camctl.operations.models import ErrorValue


def is_capture_control_error(error: ErrorValue | Mapping | None) -> bool:
    """只按已定义的错误字段区分控制，不分析工具输出文案。"""
    if isinstance(error, ErrorValue):
        code, stage, details = error.code, error.stage, error.details
    elif isinstance(error, Mapping):
        code, stage, details = error.get("code"), error.get("stage"), error.get("details", {})
    else:
        return False
    if stage == "dispatch":
        return code in {
            "canceled", "window_ended", "before_start", "terminal",
            "occupancy_released", "already_dispatched", "transport_cancelled",
        }
    if stage == "transport" and code == "transport_cancelled":
        return not details.get("signal_failures") and details.get("output_failure") is None
    return False
