"""基准收集由已保存动作、绑定和活动事实决定。"""

import pytest

from camctl.capture.baseline_models import BaselineState
from camctl.capture.baseline_qualification import (
    require_baseline_collecting_activity, require_baseline_read_intent,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from .test_baseline_read_models import _intent


def facts():
    action = {'id': 1, 'device_id': 'cam-1', 'driver_id': 'camera-driver',
              'status': 1, 'execution_started': 0, 'cancel_requested': 0}
    activity = {'ownership_mode': enum_for('device_activities.ownership_mode').BASELINE_COMPARISON,
                'baseline_state': BaselineState.COLLECTING,
                'dispatch_state': enum_for('device_activities.dispatch_state').NOT_DISPATCHED,
                'occupancy_state': enum_for('device_activities.occupancy_state').HELD}
    return action, activity


@pytest.mark.parametrize(('status', 'started'), [(1, 0), (2, 1)])
def test_original_action_and_collecting_activity_allow_directory_intent(status, started):
    action, activity = facts()
    action.update(status=status, execution_started=started)
    request = _intent()
    assert require_baseline_read_intent(request, action, activity, request.directories) is None


@pytest.mark.parametrize(('field', 'value'), [
    ('ownership_mode', -1), ('baseline_state', BaselineState.FIXED),
    ('dispatch_state', -1), ('occupancy_state', -1),
])
def test_noncollecting_activity_is_not_allowed_for_plan_or_intent(field, value):
    action, activity = facts()
    activity[field] = value
    with pytest.raises(ValueError, match='未派发收集资格'):
        require_baseline_collecting_activity(activity)
    with pytest.raises(ConsistencyError, match='未派发收集活动'):
        require_baseline_read_intent(_intent(), action, activity, _intent().directories)


@pytest.mark.parametrize(('field', 'value'), [
    ('id', 99), ('device_id', 'other'), ('driver_id', 'other'),
    ('status', 3), ('execution_started', 1), ('cancel_requested', 1),
])
def test_read_intent_rejects_changed_action_or_binding(field, value):
    action, activity = facts()
    action[field] = value
    request = _intent()
    with pytest.raises(ConsistencyError, match='未派发收集活动'):
        require_baseline_read_intent(request, action, activity, request.directories)


def test_read_intent_rejects_other_directory_scope():
    action, activity = facts()
    request = _intent()
    with pytest.raises(ConsistencyError, match='未派发收集活动'):
        require_baseline_read_intent(request, action, activity, ('/data/other',))
