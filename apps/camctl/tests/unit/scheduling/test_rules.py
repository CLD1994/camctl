"""Q1 时间资格与统一候选排序的单元测试。

窗口判断两端包含、0 宽窗口单点有效；窗口结束后的处理按已可靠
取得的启动事实独立分区，不能统一判过期。候选排序使用计划时间、
类型、受理顺序及稳定身份，与输入和唤醒顺序无关。
"""

from __future__ import annotations

import random

from camctl.scheduling.order import (
    ActionCandidate,
    FileCandidate,
    ProductCandidate,
    ProductKind,
    action_key,
    file_key,
    order_actions,
    order_files,
    order_products,
    product_key,
)
from camctl.scheduling.rules import (
    ExpirationReason,
    LaunchFact,
    LaunchWindow,
    ScheduleDecision,
    ScheduleFacts,
    WindowPhase,
    decide,
    expiration_reason,
    window_phase,
)

_NOW = 1_750_000_000_000_000


class TestWindowPhase:
    def test_boundaries_are_inclusive(self) -> None:
        window = LaunchWindow(scheduled_at=1000, window_end=2000)
        assert window_phase(window, 999) is WindowPhase.BEFORE_START
        assert window_phase(window, 1000) is WindowPhase.IN_WINDOW
        assert window_phase(window, 1500) is WindowPhase.IN_WINDOW
        assert window_phase(window, 2000) is WindowPhase.IN_WINDOW
        assert window_phase(window, 2001) is WindowPhase.AFTER_WINDOW

    def test_zero_width_window_is_single_point(self) -> None:
        window = LaunchWindow(scheduled_at=2000, window_end=2000)
        assert window_phase(window, 1999) is WindowPhase.BEFORE_START
        assert window_phase(window, 2000) is WindowPhase.IN_WINDOW
        assert window_phase(window, 2001) is WindowPhase.AFTER_WINDOW


def _facts(
    *,
    launch_fact: LaunchFact,
    phase_at: int,
    window: LaunchWindow | None = None,
    conditions_ready: bool = True,
) -> ScheduleFacts:
    return ScheduleFacts(
        window=window,
        trusted_wall_now=phase_at,
        launch_fact=launch_fact,
        conditions_ready=conditions_ready,
    )


class TestDecideByLaunchFact:
    """窗口结束后的处理按启动事实独立分区，不能统一判过期。"""

    WINDOW = LaunchWindow(scheduled_at=1000, window_end=2000)

    def test_confirmed_start_continues_capture_after_window(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.START_CONFIRMED,
                phase_at=3000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.CONTINUE_CAPTURE

    def test_in_flight_call_keeps_original_responsibility(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.CALL_IN_FLIGHT,
                phase_at=3000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.CONTINUE_ORIGINAL

    def test_unknown_intent_needs_finite_verification(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NEEDS_FINITE_VERIFICATION,
                phase_at=3000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.CONTINUE_ORIGINAL

    def test_exhausted_verification_fails_unconfirmed(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.VERIFICATION_EXHAUSTED_UNKNOWN,
                phase_at=3000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.UNCONFIRMED_FAILED

    def test_unattempted_after_window_expires(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=3000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.EXPIRED


class TestDecideByTime:
    WINDOW = LaunchWindow(scheduled_at=1000, window_end=2000)

    def test_before_start_waits(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=999,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.WAIT_UNTIL_START

    def test_in_window_ready_is_eligible(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=1000,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.ELIGIBLE

    def test_in_window_unready_waits_for_conditions(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=1500,
                conditions_ready=False,
                window=self.WINDOW,
            )
        )
        assert decision is ScheduleDecision.WAIT_CONDITIONS

    def test_timeless_action_is_not_blocked_by_window(self) -> None:
        decision = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=9_999_999,
                window=None,
            )
        )
        assert decision is ScheduleDecision.ELIGIBLE
        waiting = decide(
            _facts(
                launch_fact=LaunchFact.NOT_ATTEMPTED_OR_NO_EFFECT,
                phase_at=9_999_999,
                window=None,
                conditions_ready=False,
            )
        )
        assert waiting is ScheduleDecision.WAIT_CONDITIONS


class TestExpirationReason:
    def test_reason_depends_on_persisted_observation(self) -> None:
        assert (
            expiration_reason(first_window_observed_at=None)
            is ExpirationReason.WINDOW_MISSED
        )
        assert (
            expiration_reason(first_window_observed_at=_NOW)
            is ExpirationReason.WINDOW_EXHAUSTED
        )


def _action(action_id: int, scheduled_at: int, plan_id: int, input_index: int):
    return ActionCandidate(
        action_id=action_id,
        scheduled_at=scheduled_at,
        plan_id=plan_id,
        input_index=input_index,
    )


class TestCandidateOrder:
    def test_order_is_independent_of_wakeup(self) -> None:
        candidates = [
            _action(11, scheduled_at=2_000, plan_id=1, input_index=0),
            _action(12, scheduled_at=1_000, plan_id=2, input_index=3),
            _action(13, scheduled_at=1_000, plan_id=1, input_index=1),
            _action(14, scheduled_at=1_000, plan_id=1, input_index=0),
        ]
        expected = [14, 13, 12, 11]
        for seed in range(5):
            shuffled = list(candidates)
            random.Random(seed).shuffle(shuffled)
            assert [c.action_id for c in order_actions(shuffled)] == expected

    def test_product_order_prefers_obtain_at_same_time(self) -> None:
        obtain = ProductCandidate(
            action_id=21,
            scheduled_at=1_000,
            plan_id=2,
            input_index=0,
            kind=ProductKind.OBTAIN,
        )
        cleanup = ProductCandidate(
            action_id=22,
            scheduled_at=1_000,
            plan_id=1,
            input_index=0,
            kind=ProductKind.DELETE,
        )
        assert [c.action_id for c in order_products([cleanup, obtain])] == [21, 22]
        # 计划时间更早的清理仍然先行。
        earlier = ProductCandidate(
            action_id=23,
            scheduled_at=999,
            plan_id=9,
            input_index=0,
            kind=ProductKind.DELETE,
        )
        assert [c.action_id for c in order_products([obtain, earlier, cleanup])] == [
            23,
            21,
            22,
        ]

    def test_file_order_uses_entry_index_within_action(self) -> None:
        base = _action(31, scheduled_at=1_000, plan_id=1, input_index=0)
        other = _action(32, scheduled_at=2_000, plan_id=1, input_index=0)
        entries = [
            FileCandidate(action=other, entry_index=0),
            FileCandidate(action=base, entry_index=2),
            FileCandidate(action=base, entry_index=1),
        ]
        assert [c.entry_index for c in order_files(entries)] == [1, 2, 0]

    def test_keys_are_total_and_stable(self) -> None:
        action = _action(41, scheduled_at=1, plan_id=1, input_index=0)
        assert action_key(action) == (1, 1, 0)
        cleanup = ProductCandidate(
            action_id=42, scheduled_at=1, plan_id=1, input_index=0,
            kind=ProductKind.DELETE,
        )
        obtain = ProductCandidate(
            action_id=43, scheduled_at=1, plan_id=1, input_index=0,
            kind=ProductKind.OBTAIN,
        )
        assert product_key(obtain) < product_key(cleanup)


def test_file_key_extends_action_key() -> None:
    base = _action(51, scheduled_at=1_000, plan_id=1, input_index=0)
    first = FileCandidate(action=base, entry_index=0)
    second = FileCandidate(action=base, entry_index=1)
    assert file_key(first) < file_key(second)
