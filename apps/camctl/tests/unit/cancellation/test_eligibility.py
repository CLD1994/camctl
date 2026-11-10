"""取消资格决策表的单元测试。

覆盖《设备任务的取消资格》完整分区：终态保持、取消已生效复用原
责任、可靠未启动允许取消（不要求停止能力）、可能启动且无停止能力
拒绝（原任务继续）、已启动或可能启动且支持停止允许、事实不可靠先
核实不猜测。
"""

from __future__ import annotations

import pytest

from camctl.cancellation.rules import (
    CancelEligibility,
    DispatchPhase,
    EligibilityFacts,
    decide_cancel_eligibility,
    may_apply_cancel,
)


def _facts(dispatch, *, terminal=False, cancel_applied=False,
           stop_supported=True, reliable=True):
    return EligibilityFacts(
        terminal=terminal, cancel_applied=cancel_applied,
        dispatch=dispatch, stop_supported=stop_supported,
        facts_reliable=reliable)


class TestDecideCancelEligibility:
    def test_terminal_keeps_state_regardless_of_capability(self):
        for stop in (True, False):
            assert decide_cancel_eligibility(
                _facts(DispatchPhase.STARTED, terminal=True,
                       stop_supported=stop)
            ) is CancelEligibility.TERMINAL

    def test_terminal_takes_precedence_over_applied_cancel(self):
        assert decide_cancel_eligibility(
            _facts(DispatchPhase.STARTED, terminal=True, cancel_applied=True)
        ) is CancelEligibility.TERMINAL

    def test_applied_cancel_reuses_original_responsibility(self):
        assert decide_cancel_eligibility(
            _facts(DispatchPhase.STARTED, cancel_applied=True)
        ) is CancelEligibility.ALREADY_CANCELED

    def test_reliably_not_started_allows_without_stop_capability(self):
        for stop in (True, False):
            assert decide_cancel_eligibility(
                _facts(DispatchPhase.NOT_STARTED, stop_supported=stop)
            ) is CancelEligibility.ALLOW_PRE_START

    def test_unknown_start_without_stop_is_rejected(self):
        """启动在途或发送未知且无停止能力：拒绝，原任务继续。"""
        for phase in (DispatchPhase.START_PENDING, DispatchPhase.STARTED):
            facts = _facts(phase, stop_supported=False)
            assert decide_cancel_eligibility(facts) \
                is CancelEligibility.REJECT_UNSUPPORTED
            assert may_apply_cancel(decide_cancel_eligibility(facts)) is False

    def test_started_with_stop_capability_allows(self):
        for phase in (DispatchPhase.START_PENDING, DispatchPhase.STARTED):
            assert decide_cancel_eligibility(
                _facts(phase, stop_supported=True)
            ) is CancelEligibility.ALLOW_WITH_STOP

    @pytest.mark.parametrize("reliable,phase", [
        (False, DispatchPhase.NOT_STARTED),
        (True, DispatchPhase.UNVERIFIED),
    ])
    def test_unreliable_facts_verify_before_guessing(self, reliable, phase):
        assert decide_cancel_eligibility(
            _facts(phase, reliable=reliable)) is CancelEligibility.UNVERIFIED

    def test_may_apply_covers_only_allowed_partitions(self):
        allowed = {CancelEligibility.ALLOW_PRE_START,
                   CancelEligibility.ALLOW_WITH_STOP}
        for member in CancelEligibility:
            assert may_apply_cancel(member) == (member in allowed)


def test_cancel_status_after_apply_keeps_running_capture_until_real_settlement():
    from camctl.cancellation.models import CancelApplyMode
    from camctl.cancellation.rules import cancel_status_after_apply
    assert cancel_status_after_apply({'status': 1, 'type': 1}, CancelApplyMode.PRE_START) == 6
    assert cancel_status_after_apply({'status': 2, 'type': 1}, CancelApplyMode.PRE_START) is None
    assert cancel_status_after_apply({'status': 2, 'type': 2}, CancelApplyMode.WITH_STOP) is None
    with pytest.raises(ValueError):
        cancel_status_after_apply({'status': 2, 'type': 4}, CancelApplyMode.PRE_START)
    with pytest.raises(ValueError):
        cancel_status_after_apply({'status': 1, 'type': 2}, CancelApplyMode.WITH_STOP)


@pytest.mark.parametrize('mode,action,allowed', [
    ('PRE_START', {'status': 1, 'cancel_requested': 0}, True),
    ('PRE_START', {'status': 2, 'cancel_requested': 1}, False),
    ('WITH_STOP', {'status': 2, 'cancel_requested': 0}, True),
    ('WITH_STOP', {'status': 1, 'cancel_requested': 0}, False),
    ('TERMINAL', {'status': 6, 'cancel_requested': 1}, True),
    ('TERMINAL', {'status': 2, 'cancel_requested': 0}, False),
    ('ALREADY', {'status': 6, 'cancel_requested': 1}, True),
    ('ALREADY', {'status': 2, 'cancel_requested': 0}, False),
])
def test_stored_mode_keeps_its_original_validation_partition(mode, action, allowed):
    from camctl.cancellation.models import CancelApplyMode
    from camctl.cancellation.rules import validate_stored_cancel_mode
    from camctl.contracts.values import ConsistencyError
    if allowed:
        validate_stored_cancel_mode(action, CancelApplyMode[mode])
    else:
        with pytest.raises(ConsistencyError):
            validate_stored_cancel_mode(action, CancelApplyMode[mode])


def test_photo_preview_requires_confirmation_only_for_original_eligible_entry():
    from camctl.cancellation.rules import photo_preview_entry_decision
    from camctl.devices.start_entry import EntryPhase
    running = ({'status': 1, 'result_json': None},)
    def decide(phase, attempts=running, **kw):
        return photo_preview_entry_decision(phase, attempts,
            reserved=kw.pop('reserved', True), allow_pending_reserved=kw.pop('allowed', True), **kw)
    assert decide(EntryPhase.ENTERED) is False
    assert decide(EntryPhase.BLOCKED) is None
    assert decide(EntryPhase.BLOCKED, confirmation_pending=True) is False
    assert decide(EntryPhase.BLOCKED, confirmation_pending=False) is True
    assert decide(EntryPhase.UNKNOWN, allowed=False) is False
    assert decide(EntryPhase.UNKNOWN) is None
    assert decide(EntryPhase.UNKNOWN, confirmation_pending=False) is True
    assert decide(EntryPhase.UNKNOWN, ({'status': 2, 'result_json': {}},), confirmation_pending=False) is False


def test_pre_start_target_and_member_complete_only_after_original_preparation():
    from camctl.cancellation.models import CancelApplyMode, CancelOutcomeChoice
    from camctl.cancellation.rules import pre_start_cancel_finishes_target, immediate_cancel_member_outcome
    pending_photo = {'status': 1, 'type': 1}
    running_photo = {'status': 2, 'type': 1}
    motor = {'status': 2, 'type': 8}
    assert pre_start_cancel_finishes_target(pending_photo)
    assert not pre_start_cancel_finishes_target(running_photo)
    assert pre_start_cancel_finishes_target(motor)
    assert immediate_cancel_member_outcome(CancelApplyMode.PRE_START, pending_photo,
        withdrawal_pending=False, preparation_pending=False) is CancelOutcomeChoice.CANCELED
    assert immediate_cancel_member_outcome(CancelApplyMode.PRE_START, pending_photo,
        withdrawal_pending=False, preparation_pending=True) is None
    assert immediate_cancel_member_outcome(CancelApplyMode.PRE_START, running_photo,
        withdrawal_pending=False, preparation_pending=False) is None
    assert immediate_cancel_member_outcome(CancelApplyMode.PRE_START, motor,
        withdrawal_pending=False, preparation_pending=False) is CancelOutcomeChoice.CANCELED


@pytest.mark.parametrize('withdrawal,preparation,expected', [
    (False, False, True), (True, False, False),
    (False, True, False), (True, True, False),
])
def test_terminal_target_cancel_waits_for_original_withdrawal_and_preparation(withdrawal, preparation, expected):
    from camctl.cancellation.models import CancelApplyMode, CancelOutcomeChoice
    from camctl.cancellation.rules import immediate_cancel_member_outcome
    result = immediate_cancel_member_outcome(CancelApplyMode.TERMINAL, {'status': 6, 'type': 1},
        withdrawal_pending=withdrawal, preparation_pending=preparation)
    assert (result is CancelOutcomeChoice.ALREADY_TERMINAL) is expected
