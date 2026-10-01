"""C3 录像停止、结果登记与异常恢复的单元测试。

停止时机由锚点加完整时长决定，正常不主动少录；普通、取消及恢复
入口共用原停止预算；调用尚未结束保持资源；停止成功与文件完成保
证分别核对；重启不使用旧进程单调值继续计时。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from camctl.capture.recording import (
    RecordingDecision,
    RecordingFacts,
    RecordingPhase,
    RecordingState,
    decide_recording_next,
)

pytestmark = pytest.mark.asyncio

_STOP_TARGET = 10_000_000_000
_MAX_STOP = 2


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
