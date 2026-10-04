"""取消发起者自身被取消的决策表单元测试。

覆盖《进度与取消生效顺序》与《后一个取消动作的等待范围》：自身终
态保持、自身取消生效后停止新增影响并以 canceled 收场、未确认事务
先核实；后一个取消的等待范围只由自身直接及有效关联目标决定，不沿
原取消对象递归扩大。
"""

from __future__ import annotations

from camctl.cancellation.models import (
    CancelApplyMode,
    FixedCancelSet,
    FixedTarget,
    ResolvedTargets,
    TargetFacts,
    CancellationEffect,
    SelectionBasis,
)
from camctl.cancellation.rules import (
    CancelOriginFacts,
    OriginCancelDecision,
    decide_origin_cancel,
)
from camctl.cancellation.targets import prepare_cancel_set

_C1 = 50
_A = 11


class TestDecideOriginCancel:
    def test_not_canceled_origin_continues(self):
        assert decide_origin_cancel(CancelOriginFacts(
            origin_terminal=False, origin_cancel_applied=False,
            pending_transactions=False)) is OriginCancelDecision.CONTINUE

    def test_terminal_takes_precedence_over_applied_cancel(self):
        assert decide_origin_cancel(CancelOriginFacts(
            origin_terminal=True, origin_cancel_applied=True,
            pending_transactions=False)) is OriginCancelDecision.KEEP_TERMINAL

    def test_applied_cancel_settles_as_canceled(self):
        assert decide_origin_cancel(CancelOriginFacts(
            origin_terminal=False, origin_cancel_applied=True,
            pending_transactions=False)) is OriginCancelDecision.SETTLE_CANCELED

    def test_unconfirmed_transactions_verify_first(self):
        assert decide_origin_cancel(CancelOriginFacts(
            origin_terminal=False, origin_cancel_applied=False,
            pending_transactions=True)) is OriginCancelDecision.VERIFY_FIRST

    def test_unreliable_facts_verify_first(self):
        assert decide_origin_cancel(CancelOriginFacts(
            origin_terminal=False, origin_cancel_applied=False,
            pending_transactions=False, facts_reliable=False)
        ) is OriginCancelDecision.VERIFY_FIRST


class TestWaitScopeDoesNotExpand:
    def test_second_cancel_does_not_expand_scope(self):
        """C2 只取消 C1：等待范围只有 C1，不沿 C1 的目标 A 递归。

        固定集合由直接目标与自动预览关联决定；C1 曾经取消过 A 不进
        入 C2 的集合。
        """
        fixed = prepare_cancel_set(
            60, ResolvedTargets(
                direct=(TargetFacts(action_id=_C1, terminal=False,
                                   may_cancel=True),),
                auto_candidates=()))
        assert isinstance(fixed, FixedCancelSet)
        assert [target.action_id for target in fixed.targets] == [_C1]
        assert all(target.basis is SelectionBasis.DIRECT
                   for target in fixed.targets)

    def test_scope_with_both_origin_and_target_reuses_original(self):
        """C2 同时包含 C1 与 A：两者都是 C2 的直接目标，A 不经 C1。"""
        fixed = prepare_cancel_set(
            60, ResolvedTargets(
                direct=(TargetFacts(action_id=_C1, terminal=True,
                                   may_cancel=True),
                        TargetFacts(action_id=_A, terminal=False,
                                   may_cancel=True)),
                auto_candidates=()))
        assert isinstance(fixed, FixedCancelSet)
        assert [target.action_id for target in fixed.targets] == [_A, _C1]
        # C1 已终态：按既有终态处理；A 独立判定，各自保留依据。
        assert fixed.targets[0].cancellation_effect \
            is CancellationEffect.NOT_APPLIED
