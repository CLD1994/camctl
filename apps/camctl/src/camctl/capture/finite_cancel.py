"""原有限设备任务的取消收场资格。"""
from camctl.devices.tasks import CancelCompletionPolicy


def finite_cancel_policy(action) -> bool:
    """只消费首次固定定义；旧任务保持原取消规则。"""
    return (action['type'] == 3 and action['execution_spec_json'].get(
        'cancel_completion_policy') == CancelCompletionPolicy.FINITE_DEVICE_END.value)


def require_finite_cancel_end(action):
    """取消已生效且原冻结策略允许以有限任务自然结束收场。"""
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.tasks import EndControl, StartReturn, CompletionMode
    spec = action['execution_spec_json']
    if (not finite_cancel_policy(action) or not action['cancel_requested']
            or spec.get('end_control') != int(EndControl.DEVICE)
            or spec.get('start_return_meaning') != int(StartReturn.SENT)
            or spec.get('completion_mode') != int(CompletionMode.TIME_AND_OUTPUTS)
            or spec.get('wait_after_send') is not True or spec.get('duration_based') is not True):
        raise ConsistencyError('有限取消结束要求原冻结策略、有限设备任务和已生效取消')


def require_failed_stop(stop, attempt):
    """有限取消仅在首个停止尝试失败且没有确认效果后继续。"""
    from camctl.contracts.enums import enum_for
    from camctl.contracts.values import ConsistencyError
    if (stop['status'] != int(enum_for('operation_runs.status').FAILED) or stop['attempts_used'] != 1
            or attempt['status'] == int(enum_for('operation_attempts.status').RUNNING)
            or attempt['effect_state'] == int(enum_for('operation_attempts.effect_state').CONFIRMED)):
        raise ConsistencyError('有限取消结束缺少第一次停止失败的终局责任')


def require_capture_sent(outcome, activity_id):
    """原启动结果必须可靠确认同一活动的发送。"""
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.models import AttemptStatus, EffectState
    if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.effect is not EffectState.CONFIRMED
            or not any(o.type == 'timelapse_sent' and o.data.get('activity_id') == str(activity_id) for o in outcome.observations)):
        raise ConsistencyError('有限取消结束缺少原活动的可靠 SENT')
