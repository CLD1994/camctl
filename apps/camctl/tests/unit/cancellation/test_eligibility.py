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
