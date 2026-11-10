"""基准目录收集及原调用意图的业务资格。"""

from camctl.capture.baseline_models import BaselineState
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding


_OWNERSHIP = enum_for('device_activities.ownership_mode')
_DISPATCH = enum_for('device_activities.dispatch_state')
_OCCUPANCY = enum_for('device_activities.occupancy_state')


def require_baseline_collecting_activity(activity):
    """基准收集要求原活动占用资源且尚未派发。"""
    if (activity['ownership_mode'] != _OWNERSHIP.BASELINE_COMPARISON
            or activity['baseline_state'] != BaselineState.COLLECTING
            or activity['dispatch_state'] != _DISPATCH.NOT_DISPATCHED
            or activity['occupancy_state'] != _OCCUPANCY.HELD):
        raise ValueError('基准活动不满足未派发收集资格')


def require_baseline_read_intent(request, action, activity, expected_directories):
    """原调用意图须属于执行中的同一动作和仍持有的收集活动。"""
    message = '基准调用不属于原动作正在占用的未派发收集活动'
    if (request.action_id != action['id']
            or request.binding != DeviceBinding(action['device_id'], action['driver_id'])
            or request.directories != expected_directories
            or (action['status'], action['execution_started']) not in ((1, 0), (2, 1))
            or action['cancel_requested'] != 0):
        raise ConsistencyError(message)
    try:
        require_baseline_collecting_activity(activity)
    except ValueError as error:
        raise ConsistencyError(message) from error
