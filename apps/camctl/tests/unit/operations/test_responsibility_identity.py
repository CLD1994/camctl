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


@pytest.mark.parametrize("identity", [1, 29, 9223372036854775807])
def test_obtain_check_keeps_distinct_owner_key_and_ticket(identity):
    target = AttemptTarget(obtain_item_id=identity)
    assert attempts.operation_responsibility_key(OperationKind.CHECK_FILE_EXISTS, 7, target, None) == f"obtain-exists/{identity}"
    intent = attempts.AttemptIntent("query", 7, OperationKind.CHECK_FILE_EXISTS, target, None,
                                    attempts.AttemptConfig(3), 11)
    assert attempts.ticket_target_id(intent) == str(identity)
    assert attempts.operation_responsibility_key(OperationKind.CHECK_FILE_EXISTS, 7,
                                                AttemptTarget(cleanup_item_id=identity), None) == f"exists/{identity}"


@pytest.mark.parametrize("other", ["cleanup_item_id", "activity_id", "copy_id"])
def test_obtain_check_rejects_another_filled_owner(other):
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(OperationKind.CHECK_FILE_EXISTS, 7,
            AttemptTarget(obtain_item_id=29, **{other:29}), None)


@pytest.mark.parametrize("kind", [kind for kind in OperationKind if kind is not OperationKind.CHECK_FILE_EXISTS])
def test_only_file_check_accepts_obtain_owner(kind):
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(kind, 7, AttemptTarget(obtain_item_id=29),
            QueryPurpose.BEFORE_EXECUTION if kind is OperationKind.QUERY_ACTIVITY else None)


def test_preparation_responsibility_is_bound_to_activity_and_session():
    kind = OperationKind.PREPARE_CAPTURE
    target = AttemptTarget(activity_id=4)
    key = attempts.operation_responsibility_key(kind, 7, target, None, session_key="a" * 32)
    assert key == "prepare/4/" + "a" * 32
    assert attempts.operation_responsibility_key(kind, 7, target, None, session_key="b" * 32) != key


@pytest.mark.parametrize("session_key", [None, "", "a" * 31, "A" * 32, "g" * 32, False])
def test_preparation_rejects_missing_or_invalid_session(session_key):
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(OperationKind.PREPARE_CAPTURE, 7,
            AttemptTarget(activity_id=4), None, session_key=session_key)


def test_other_operations_reject_session_identity():
    with pytest.raises(AttemptConfigError):
        attempts.operation_responsibility_key(OperationKind.START, 7,
            AttemptTarget(activity_id=4), None, session_key="a" * 32)


def test_preparation_intent_has_one_control_call_and_no_retry():
    intent = attempts.AttemptIntent("control", 7, OperationKind.PREPARE_CAPTURE,
        AttemptTarget(activity_id=4), None, attempts.AttemptConfig(1, "5"), 11,
        session_key="a" * 32)
    assert attempts.responsibility_key(intent) == "prepare/4/" + "a" * 32
    assert attempts.ticket_target_id(intent) == "4"


@pytest.mark.parametrize("operation,config", [
    ("query", attempts.AttemptConfig(1, "5")),
    ("control", attempts.AttemptConfig(2, "5")),
    ("control", attempts.AttemptConfig(1)),
    ("control", attempts.AttemptConfig(1, "5", "0")),
])
def test_preparation_rejects_wrong_operation_or_budget(operation, config):
    with pytest.raises(AttemptConfigError):
        attempts.AttemptIntent(operation, 7, OperationKind.PREPARE_CAPTURE,
            AttemptTarget(activity_id=4), None, config, 11, session_key="a" * 32)
