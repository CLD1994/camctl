"""活动结束范围与准备尝试的资格边界。"""
import pytest

from camctl.operations.attempts import may_prepare_capture_attempt, may_resolve_output_scope


def test_scope_closure_requires_ended_owned_activity_and_fixed_baseline():
    activity = {'activity_state': 3, 'occupancy_state': 1, 'baseline_state': 3,
                'output_set_finalized_event_id': None}
    assert may_resolve_output_scope(activity)
    for key, value in [('activity_state', 1), ('occupancy_state', 2),
                       ('baseline_state', 1), ('output_set_finalized_event_id', 1)]:
        assert not may_resolve_output_scope({**activity, key: value})


@pytest.mark.parametrize('changed', [
    {'eligible': False}, {'attempt_no': 2}, {'max_attempts': 2}, {'timeout': None}, {'retry_interval': 0},
])
def test_preparation_attempt_requires_one_bounded_call(changed):
    action = {'cancel_requested': 0}
    activity = {'occupancy_state': 1, 'dispatch_state': 1, 'activity_state': 1}
    request = {'eligible': True, 'attempt_no': 1, 'max_attempts': 1, 'timeout': 5, 'retry_interval': None}
    assert may_prepare_capture_attempt(action, activity, **request)
    assert not may_prepare_capture_attempt(action, activity, **{**request, **changed})
