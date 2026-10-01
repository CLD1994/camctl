"""C8 异常原片检查及内部修复的决策单元测试。

修复门槛 = duration_s + 固定余量 T；严格超过才触发，恰好相等不
触发；仅下界证据时下界超门槛即确认多录，未超不能证明无需修复；
计时不足按未知进入原片检查；正常完成不触发异常修复；修复决定
不替代采集结果。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.capture.media import (
    MediaDecision,
    MediaKind,
    MediaPolicy,
    RecordingEvidence,
    decide_media_processing,
)

pytestmark = pytest.mark.asyncio


def _evidence(**overrides) -> RecordingEvidence:
    values = dict(
        recording_completed_normally=False,
        recording_abnormal=True,
        duration_s=Decimal("60"),
        confirmed_recorded_s=None,
        recorded_lower_bound_s=None,
        timing_insufficient=False,
        stop_confirmed=True,
        source_file_complete=True,
        facts_readable=True,
    )
    values.update(overrides)
    return RecordingEvidence(**values)


_POLICY = MediaPolicy(repair_margin_s=Decimal("10"))


class TestPolicy:
    async def test_margin_must_be_finite_non_negative(self) -> None:
        with pytest.raises(ValueError):
            MediaPolicy(repair_margin_s=Decimal("-1"))
        with pytest.raises(ValueError):
            MediaPolicy(repair_margin_s=float("nan"))
        assert MediaPolicy(repair_margin_s=Decimal("0")).repair_margin_s == 0


class TestThreshold:
    async def test_strictly_over_threshold_triggers_repair(self) -> None:
        decision = decide_media_processing(
            _evidence(confirmed_recorded_s=Decimal("70.001")), _POLICY
        )
        assert decision is MediaDecision.REPAIR_REQUIRED

    async def test_exactly_at_threshold_does_not_trigger(self) -> None:
        decision = decide_media_processing(
            _evidence(confirmed_recorded_s=Decimal("70")), _POLICY
        )
        assert decision is MediaDecision.NO_REPAIR_WITHIN_THRESHOLD

    async def test_below_threshold_stops(self) -> None:
        decision = decide_media_processing(
            _evidence(confirmed_recorded_s=Decimal("65")), _POLICY
        )
        assert decision is MediaDecision.NO_REPAIR_WITHIN_THRESHOLD

    async def test_normal_completion_never_repairs(self) -> None:
        decision = decide_media_processing(
            _evidence(recording_completed_normally=True, recording_abnormal=False),
            _POLICY,
        )
        assert decision is MediaDecision.NORMAL_COMPLETION

    async def test_margin_is_fixed_seconds(self) -> None:
        long = decide_media_processing(
            _evidence(duration_s=Decimal("3600"), confirmed_recorded_s=Decimal("3605")),
            _POLICY,
        )
        assert long is MediaDecision.NO_REPAIR_WITHIN_THRESHOLD


class TestLowerBound:
    async def test_lower_bound_over_threshold_confirms(self) -> None:
        decision = decide_media_processing(
            _evidence(recorded_lower_bound_s=Decimal("71")), _POLICY
        )
        assert decision is MediaDecision.REPAIR_REQUIRED

    async def test_lower_bound_under_threshold_proves_nothing(self) -> None:
        """下界未超门槛不能证明实际录制未超。"""
        decision = decide_media_processing(
            _evidence(recorded_lower_bound_s=Decimal("65")), _POLICY
        )
        assert decision is MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL


class TestInsufficientEvidence:
    async def test_timing_insufficient_checks_original(self) -> None:
        decision = decide_media_processing(
            _evidence(timing_insufficient=True), _POLICY
        )
        assert decision is MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL

    async def test_unreadable_facts_are_config_error(self) -> None:
        decision = decide_media_processing(
            _evidence(facts_readable=False), _POLICY
        )
        assert decision is MediaDecision.CONFIG_ERROR

    async def test_repair_waits_for_stop_and_file_completion(self) -> None:
        """已触发修复但停止或文件写完未确认：不读取仍在写入的源文件。"""
        decision = decide_media_processing(
            _evidence(
                confirmed_recorded_s=Decimal("80"),
                stop_confirmed=False,
                source_file_complete=False,
            ),
            _POLICY,
        )
        assert decision is MediaDecision.WAIT_STOP_AND_FILE


class TestResultSeparation:
    async def test_repair_decision_does_not_replace_capture_result(self) -> None:
        """修复决定不携带采集成功结论；两者分别保存。"""
        decision = decide_media_processing(
            _evidence(confirmed_recorded_s=Decimal("80")), _POLICY
        )
        assert decision is MediaDecision.REPAIR_REQUIRED
        assert not hasattr(decision, "capture_success")

    async def test_media_kinds_are_distinct(self) -> None:
        assert MediaKind.ORIGINAL is not MediaKind.REPAIRED
        assert MediaKind.ORIGINAL is not MediaKind.TEMP_OUTPUT
