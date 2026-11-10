"""整批竞争与成员结束的纯决策契约，不装配数据库或包资源。"""

import pytest

from camctl.outputs.batches import BatchOutcome, decide_batch_grant, read_member_finished


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_batch_parallel_capability_requires_boolean(value):
    from camctl.outputs.batches import BatchRequest
    with pytest.raises(ValueError):
        BatchRequest(1, "camera", 10, capture_read_parallel_supported=value)


@pytest.mark.parametrize("holder,ready,first,expected", [
    (None, False, None, BatchOutcome.WAIT),
    (None, True, 2, BatchOutcome.WAIT),
    (None, True, 1, BatchOutcome.GRANTED),
    (1, False, 2, BatchOutcome.HELD),
    (2, True, 1, BatchOutcome.WAIT),
])
def test_batch_preserves_holder_before_new_priority(holder, ready, first, expected):
    assert decide_batch_grant(action_id=1, holder_action_id=holder,
        ready=ready, first_action_id=first) is expected


@pytest.mark.parametrize("terminal,copy,verified,ended,retry,unsettled,expected", [
    (False, False, False, False, False, False, False),
    (False, True, False, True, False, False, False),
    (False, True, True, False, False, False, False),
    (False, True, True, True, False, False, True),
    (False, True, True, True, True, False, False),
    (True, True, True, True, False, True, False),
    (True, False, False, False, False, False, True),
])
def test_member_waits_for_verification_retry_and_actual_settlement(
        terminal, copy, verified, ended, retry, unsettled, expected):
    assert read_member_finished(item_terminal=terminal, has_copy=copy,
        verification_complete=verified, read_ended=ended, retry_wait=retry,
        unsettled=unsettled) is expected


def test_batch_action_mode_preserves_canceled_cleanup_responsibility():
    from camctl.outputs.batches import BatchActionMode, classify_batch_action
    action = {'type': 5, 'status': 2, 'cancel_requested': 0, 'scheduled_at': 10, 'target_selection_state': 2}
    assert classify_batch_action(action, 9) is BatchActionMode.NOT_READY
    assert classify_batch_action(action, 10) is BatchActionMode.CLEANUP
    assert classify_batch_action({**action, 'cancel_requested': 1}, 10) is BatchActionMode.CLEANUP_RESIDUAL
    assert classify_batch_action({**action, 'type': 4, 'cancel_requested': 1}, 10) is BatchActionMode.NOT_READY
