"""O5 原尝试恢复及取消结果接手的单元测试。

只有意图且主机收场已可靠完成的尝试按原责任核实，不重发；已确认
失败、可续传与可靠未派发分别处理不混用；恢复依据只保存实际掌握
的事实，不补造原超时、退出、时刻或配置；取消后到达的可靠结果仍
被保存。
"""

from __future__ import annotations

import pytest

from camctl.operations.recovery import (
    CancelOutcome,
    RecoveryClass,
    RecoveryFacts,
    RecoveryOutcome,
    recover_attempt,
    settle_cancelled_call,
)

pytestmark = pytest.mark.asyncio


def _facts(**overrides) -> RecoveryFacts:
    values = dict(
        has_intent=True,
        has_saved_result=False,
        result_failed_confirmed=False,
        host_settlement_complete=True,
        read_progress_saved=False,
        target_length_known=False,
        dispatch_prevented_confirmed=False,
        read_error_confirmed=False,
    )
    values.update(overrides)
    return RecoveryFacts(**values)


class TestRecoverAttempt:
    async def test_unknown_attempt_does_not_redispatch(self) -> None:
        """只有意图且主机收场已可靠完成：核实原责任，不重发。"""
        decision = recover_attempt(_facts())
        assert decision.recovery is RecoveryClass.UNKNOWN_NEEDS_VERIFICATION
        assert decision.redispatch is False
        assert decision.verify_original is True
        assert decision.save_basis is True

    async def test_saved_result_is_not_recovered(self) -> None:
        """已保存结束结果的尝试不再进入恢复：保留原事实。"""
        decision = recover_attempt(_facts(has_saved_result=True))
        assert decision.recovery is RecoveryClass.ALREADY_SETTLED
        assert decision.redispatch is False
        assert decision.verify_original is False

    async def test_confirmed_failure_is_final(self) -> None:
        """已确认失败：不恢复、不重发；后续按剩余资格另行判断。"""
        decision = recover_attempt(
            _facts(
                has_saved_result=True,
                result_failed_confirmed=True,
            )
        )
        assert decision.recovery is RecoveryClass.ALREADY_SETTLED

    async def test_resumable_read_keeps_original_identity(self) -> None:
        """可续传读取：沿原身份继续，不重发、不新建尝试。"""
        decision = recover_attempt(
            _facts(
                read_progress_saved=True,
                target_length_known=True,
            )
        )
        assert decision.recovery is RecoveryClass.UNKNOWN_NEEDS_VERIFICATION
        assert decision.redispatch is False

    async def test_confirmed_read_error_is_not_missing_result(self) -> None:
        """已确认读取错误与未保存读取失败分开：前者不进入未知恢复。"""
        decision = recover_attempt(
            _facts(read_error_confirmed=True, has_saved_result=True)
        )
        assert decision.recovery is RecoveryClass.ALREADY_SETTLED

    async def test_not_dispatched_confirmed_never_redispatches(self) -> None:
        """可靠未派发：不重发；已提交次数保留。"""
        decision = recover_attempt(
            _facts(dispatch_prevented_confirmed=True, has_saved_result=True)
        )
        assert decision.recovery is RecoveryClass.ALREADY_SETTLED
        assert decision.redispatch is False

    async def test_incomplete_host_settlement_waits_for_settlement(self) -> None:
        """主机收场未完成：先收场，不进入业务恢复分支。"""
        decision = recover_attempt(_facts(host_settlement_complete=False))
        assert decision.recovery is RecoveryClass.WAIT_HOST_SETTLEMENT
        assert decision.redispatch is False
        assert decision.verify_original is False

    async def test_no_intent_is_not_a_call(self) -> None:
        """没有意图的流程不进入调用恢复。"""
        decision = recover_attempt(_facts(has_intent=False))
        assert decision.recovery is RecoveryClass.NO_INTENT_RECORDED


class TestSettleCancelledCall:
    async def test_reliable_result_after_cancel_is_saved(self) -> None:
        """取消后到达的可靠结果仍保存；等待者消失不丢弃事实。"""
        outcome = RecoveryOutcome(local_exit_code=0)
        settled = await settle_cancelled_call(
            CancelOutcome(cancelled=True, final_outcome=outcome)
        )
        assert settled.saved is True
        assert settled.outcome is outcome

    async def test_cancelled_call_without_result_keeps_unknown(self) -> None:
        """取消触发终止但没有可靠结果：保存未知，不补造退出。"""
        settled = await settle_cancelled_call(
            CancelOutcome(cancelled=True, final_outcome=None)
        )
        assert settled.saved is True
        assert settled.outcome is None

    async def test_normal_completion_is_saved_as_is(self) -> None:
        outcome = RecoveryOutcome(local_signal=9)
        settled = await settle_cancelled_call(
            CancelOutcome(cancelled=False, final_outcome=outcome)
        )
        assert settled.saved is True
        assert settled.outcome is outcome
