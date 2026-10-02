"""固定责任键由身份决定，不要求尚未发生的调用配置或时刻。"""

import pytest

from camctl.operations import attempts
from camctl.operations.attempts import AttemptConfigError, AttemptTarget, OperationKind, QueryPurpose


@pytest.mark.parametrize("kind,target,purpose,expected", [
    (OperationKind.START, AttemptTarget(activity_id=4), None, "start/7"),
    (OperationKind.STOP, AttemptTarget(activity_id=4), None, "stop/7"),
    (OperationKind.CHECK_CAPTURE_RESULTS, AttemptTarget(activity_id=4), None, "results/4"),
    (OperationKind.STOP_RESIDUAL, AttemptTarget(activity_id=4), None, "followup/7/4"),
    (OperationKind.READ_FILE, AttemptTarget(copy_id=3), None, "read/3"),
    (OperationKind.DELETE_FILE, AttemptTarget(cleanup_item_id=3), None, "delete/3"),
    (OperationKind.CHECK_FILE_EXISTS, AttemptTarget(cleanup_item_id=3), None, "exists/3"),
    (OperationKind.QUERY_ACTIVITY, AttemptTarget(), QueryPurpose.BEFORE_EXECUTION, "query/preflight/7"),
    (OperationKind.QUERY_ACTIVITY, AttemptTarget(activity_id=4), QueryPurpose.START_CONFIRMATION, "query/start/7/4"),
    (OperationKind.QUERY_ACTIVITY, AttemptTarget(activity_id=4), QueryPurpose.ACTIVITY_OBSERVATION, "query/activity/7/4"),
    (OperationKind.QUERY_ACTIVITY, AttemptTarget(activity_id=4), QueryPurpose.STOP_CONFIRMATION, "query/stop/7/4"),
    (OperationKind.QUERY_ACTIVITY, AttemptTarget(activity_id=4), QueryPurpose.RESIDUAL_STOP_CONFIRMATION, "query/residual/7/4"),
])
def test_responsibility_key_uses_only_fixed_identity(kind, target, purpose, expected):
    assert attempts.operation_responsibility_key(kind, 7, target, purpose) == expected


@pytest.mark.parametrize("kind", list(OperationKind))
@pytest.mark.parametrize("target", [AttemptTarget(activity_id=4, copy_id=3),
                                   AttemptTarget(copy_id=3, cleanup_item_id=2)])
def test_responsibility_key_rejects_mixed_targets(kind, target):
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(kind, 7, target,
            QueryPurpose.START_CONFIRMATION if kind is OperationKind.QUERY_ACTIVITY else None)


@pytest.mark.parametrize("kind,action_id,target,purpose", [
    ("start", 7, AttemptTarget(activity_id=4), None),
    (OperationKind.START, False, AttemptTarget(activity_id=4), None),
    (OperationKind.START, 7, {}, None),
    (OperationKind.START, 7, AttemptTarget(), None),
    (OperationKind.START, 7, AttemptTarget(activity_id=4), QueryPurpose.START_CONFIRMATION),
    (OperationKind.QUERY_ACTIVITY, 7, AttemptTarget(), None),
    (OperationKind.QUERY_ACTIVITY, 7, AttemptTarget(activity_id=4), QueryPurpose.BEFORE_EXECUTION),
])
def test_responsibility_key_rejects_invalid_identity(kind, action_id, target, purpose):
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(kind, action_id, target, purpose)
