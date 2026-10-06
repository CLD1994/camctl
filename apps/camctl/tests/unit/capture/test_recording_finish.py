"""C3 录像停止、结果登记与异常恢复的单元测试。

停止时机由锚点加完整时长决定，正常不主动少录；普通、取消及恢复
入口共用原停止预算；调用尚未结束保持资源；停止成功与文件完成保
证分别核对；重启不使用旧进程单调值继续计时。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from camctl.capture.recording import (
    ReconciliationFacts,
    ReconciliationPhase,
    RecordingDecision,
    RecordingFacts,
    RecordingPhase,
    RecordingState,
    RecoveredControlFacts,
    RecoveredControlReason,
    decide_recording_next,
    decide_recording_reconciliation,
    decide_recovered_control,
)

pytestmark = pytest.mark.asyncio

_STOP_TARGET = 10_000_000_000
_MAX_STOP = 2
_NOW_US = 1_750_000_000_000_000


def _state(**overrides) -> RecordingState:
    values = dict(
        action_terminal=False,
        started_confirmed=True,
        anchor_from_current_session=True,
        stop_target_ns=_STOP_TARGET,
        monotonic_now_ns=_STOP_TARGET - 1,
        stop_confirmed=False,
        file_complete_guaranteed=False,
        stop_attempts_used=0,
        stop_max_attempts=_MAX_STOP,
        stop_in_flight=False,
    )
    values.update(overrides)
    return RecordingState(**values)


class TestPhases:
    async def test_before_target_keeps_recording(self) -> None:
        """未到停止目标继续录制，不主动少录。"""
        decision = decide_recording_next(_state(), RecordingFacts())
        assert decision.phase is RecordingPhase.WAIT_RECORD
        assert decision.new_stop_attempt is False

    async def test_at_target_prepares_stop(self) -> None:
        decision = decide_recording_next(
            _state(monotonic_now_ns=_STOP_TARGET), RecordingFacts()
        )
        assert decision.phase is RecordingPhase.READY_TO_STOP
        assert decision.new_stop_attempt is True

    async def test_in_flight_call_keeps_resources(self) -> None:
        """停止调用尚未结束：保持资源，不叠加新尝试。"""
        decision = decide_recording_next(
            _state(monotonic_now_ns=_STOP_TARGET, stop_in_flight=True),
            RecordingFacts(),
        )
        assert decision.phase is RecordingPhase.STOP_IN_FLIGHT
        assert decision.new_stop_attempt is False

    async def test_stop_confirmed_checks_file_separately(self) -> None:
        """停止成功与文件完成保证分别核对。"""
        decision = decide_recording_next(
            _state(stop_confirmed=True, file_complete_guaranteed=False),
            RecordingFacts(),
        )
        assert decision.phase is RecordingPhase.VERIFY_FILE_COMPLETE
        complete = decide_recording_next(
            _state(stop_confirmed=True, file_complete_guaranteed=True),
            RecordingFacts(),
        )
        assert complete.phase is RecordingPhase.CONTROL_COMPLETE

    async def test_terminal_action_is_not_reopened(self) -> None:
        decision = decide_recording_next(
            _state(action_terminal=True, stop_confirmed=True,
                   file_complete_guaranteed=True),
            RecordingFacts(),
        )
        assert decision.phase is RecordingPhase.ALREADY_TERMINAL

    async def test_not_started_is_not_stopped(self) -> None:
        decision = decide_recording_next(
            _state(started_confirmed=False), RecordingFacts()
        )
        assert decision.phase is RecordingPhase.NOT_RUNNING


class TestStopBudgetShared:
    async def test_stop_uses_original_budget_across_cancel_and_restart(self) -> None:
        """普通、取消及恢复入口共用原停止次数，不刷新预算。"""
        stop_count = 0
        for facts in (
            RecordingFacts(canceled=False),
            RecordingFacts(canceled=True),
            RecordingFacts(canceled=False),
        ):
            state = _state(
                monotonic_now_ns=_STOP_TARGET,
                stop_attempts_used=stop_count,
                stop_in_flight=False,
            )
            decision = decide_recording_next(state, facts)
            if decision.new_stop_attempt:
                stop_count += 1
            assert decision.stop_attempts_used == stop_count
        assert stop_count == _MAX_STOP

    async def test_exhausted_stop_budget_does_not_dispatch(self) -> None:
        decision = decide_recording_next(
            _state(
                monotonic_now_ns=_STOP_TARGET,
                stop_attempts_used=_MAX_STOP,
            ),
            RecordingFacts(),
        )
        assert decision.phase is RecordingPhase.STOP_EXHAUSTED
        assert decision.new_stop_attempt is False

    async def test_restart_does_not_reuse_old_monotonic_clock(self) -> None:
        """重启后锚点来自旧进程单调钟：先对账，不直接计时停止。"""
        decision = decide_recording_next(
            _state(anchor_from_current_session=False, monotonic_now_ns=_STOP_TARGET),
            RecordingFacts(),
        )
        assert decision.phase is RecordingPhase.RECONCILE_REQUIRED
        assert decision.new_stop_attempt is False


class TestCanceledStop:
    """取消生效后立即停止：不等待停止目标，也不依赖本会话锚点。"""

    async def test_cancel_before_target_stops_immediately(self) -> None:
        decision = decide_recording_next(_state(), RecordingFacts(canceled=True))
        assert decision.phase is RecordingPhase.READY_TO_STOP
        assert decision.new_stop_attempt is True

    async def test_cancel_without_session_anchor_still_stops(self) -> None:
        """跨会话锚点只影响计时；取消不计时，仍按剩余预算停止。"""
        decision = decide_recording_next(
            _state(anchor_from_current_session=False,
                   stop_target_ns=None, monotonic_now_ns=None),
            RecordingFacts(canceled=True),
        )
        assert decision.phase is RecordingPhase.READY_TO_STOP
        assert decision.new_stop_attempt is True

    async def test_cancel_respects_in_flight_call(self) -> None:
        decision = decide_recording_next(
            _state(stop_in_flight=True), RecordingFacts(canceled=True))
        assert decision.phase is RecordingPhase.STOP_IN_FLIGHT
        assert decision.new_stop_attempt is False

    async def test_cancel_respects_original_budget(self) -> None:
        decision = decide_recording_next(
            _state(stop_attempts_used=_MAX_STOP), RecordingFacts(canceled=True))
        assert decision.phase is RecordingPhase.STOP_EXHAUSTED
        assert decision.new_stop_attempt is False


class TestReconciliationTiming:
    """跨会话对账按可信计时的判定分区。"""

    async def test_missing_start_time_is_unreliable(self) -> None:
        """启动确认墙钟缺失不能组合计时，不推测已经录够。"""
        decision = decide_recording_reconciliation(ReconciliationFacts(
            started_at_us=None, target_duration_ms=60_000,
            trusted_now_us=_NOW_US))
        assert decision is ReconciliationPhase.TIMING_UNRELIABLE

    async def test_before_target_waits_remainder(self) -> None:
        decision = decide_recording_reconciliation(ReconciliationFacts(
            started_at_us=_NOW_US - 30_000_000,
            target_duration_ms=60_000, trusted_now_us=_NOW_US))
        assert decision is ReconciliationPhase.TIMING_WAIT_REMAINDER

    async def test_at_target_is_satisfied(self) -> None:
        """恰好到达目标即满足（毫秒精确换算，含等号）。"""
        decision = decide_recording_reconciliation(ReconciliationFacts(
            started_at_us=_NOW_US - 60_000_000,
            target_duration_ms=60_000, trusted_now_us=_NOW_US))
        assert decision is ReconciliationPhase.TIMING_SATISFIED


class TestRecoveredControl:
    """恢复停止后控制完成依据与异常多录门槛判定。"""

    def _facts(self, **overrides) -> RecoveredControlFacts:
        values = dict(
            session_anchor=False,
            started_at_us=_NOW_US - 70_000_000,
            stop_confirmed_at_us=_NOW_US,
            target_duration_ms=60_000,
            repair_margin_s=Decimal("10"),
        )
        values.update(overrides)
        return RecoveredControlFacts(**values)

    async def test_session_anchor_is_continuous(self) -> None:
        """本会话锚点的正常停止不超门槛，不按恢复计时判定。"""
        decision = decide_recovered_control(self._facts(session_anchor=True))
        assert decision is not None
        assert decision.reason is RecoveredControlReason.CONTINUOUS
        assert decision.control_elapsed_ns is None

    async def test_at_threshold_is_continuous(self) -> None:
        """恰好达到目标加余量不触发修复（含等号）。"""
        decision = decide_recovered_control(self._facts())
        assert decision is not None
        assert decision.reason is RecoveredControlReason.CONTINUOUS

    async def test_beyond_threshold_is_excess(self) -> None:
        decision = decide_recovered_control(
            self._facts(stop_confirmed_at_us=_NOW_US + 1))
        assert decision is not None
        assert decision.reason is RecoveredControlReason.EXCESS_DURATION
        assert decision.control_elapsed_ns == 70_000_001_000

    async def test_missing_timing_evidence_stays_undecided(self) -> None:
        """计时证据缺失不折叠为连续控制完成。"""
        assert decide_recovered_control(
            self._facts(started_at_us=None)) is None
        assert decide_recovered_control(
            self._facts(stop_confirmed_at_us=None)) is None
        assert decide_recovered_control(
            self._facts(stop_confirmed_at_us=_NOW_US - 80_000_000)) is None
