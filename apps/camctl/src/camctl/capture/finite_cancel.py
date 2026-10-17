"""原有限设备任务的取消收场资格。"""
from camctl.devices.tasks import CancelCompletionPolicy


def finite_cancel_policy(action) -> bool:
    """只消费首次固定定义；旧任务保持原取消规则。"""
    return (action['type'] == 3 and action['execution_spec_json'].get(
        'cancel_completion_policy') == CancelCompletionPolicy.FINITE_DEVICE_END.value)
