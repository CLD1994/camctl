"""流程终态和重试间隔共同决定能否建立等待。"""
from camctl.contracts.enums import enum_for
from camctl.operations.attempts import retry_wait_violation


def test_retry_wait_requires_pending_or_active_and_saved_interval():
    status = enum_for('operation_runs.status')
    assert retry_wait_violation(int(status.PENDING), 0) is None
    assert retry_wait_violation(int(status.ACTIVE), 1) is None
    assert retry_wait_violation(int(status.SUCCEEDED), 1) is not None
    assert retry_wait_violation(int(status.PENDING), None) is not None
