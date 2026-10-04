"""C8 异常原片检查及内部修复的决策单元测试。

修复门槛 = duration_s + 固定余量 T；严格超过才触发，恰好相等不
触发；仅下界证据时下界超门槛即确认多录，未超不能证明无需修复；
计时不足按未知进入原片检查；正常完成不触发异常修复；修复决定
不替代采集结果。

第二段覆盖处理状态推进命令与检查结果分类：决定与依据的配对、
媒体观察的组合约束、结果命令的文件与错误互斥，以及门槛分类在
边界上的精确划分。
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
from camctl.capture.processing import (
    CheckBasis,
    CheckDecisionChoice,
    CheckDecisionSave,
    CheckDurationClass,
    CheckPhase,
    CheckReason,
    CheckResultSave,
    DiscardPhase,
    DiscardProgressSave,
    MediaObservation,
    ProcessingDisposition,
    ProcessingError,
    RepairBasis,
    RepairDecisionChoice,
    RepairDecisionSave,
    RepairOutcome,
    RepairReason,
    RepairResultSave,
    SourceFileSave,
    classify_check_duration,
    repair_basis_from_check,
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


# ---- 处理状态推进命令与检查结果分类 ----


def _basis(**overrides) -> CheckBasis:
    values = dict(
        reason=CheckReason.INSUFFICIENT_TIMING,
        target_duration_ms=60_000,
        control_elapsed_ns=None,
        continuity_evidence=None,
    )
    values.update(overrides)
    return CheckBasis(**values)


def _error(code: str = "tool_failed") -> ProcessingError:
    return ProcessingError(code=code, stage="media", details={"exit": 1})


class TestProcessingError:
    async def test_rejects_blank_code_and_non_mapping_details(self) -> None:
        with pytest.raises(ValueError):
            ProcessingError(code="", stage="media", details={})
        with pytest.raises(ValueError):
            ProcessingError(code="probe_failed", stage="", details={})
        with pytest.raises(TypeError):
            ProcessingError(code="probe_failed", stage="media", details=[("a", 1)])

    async def test_as_json_keeps_protocol_shape(self) -> None:
        assert _error().as_json() == {
            "code": "tool_failed", "stage": "media", "details": {"exit": 1}}


class TestCheckBasis:
    async def test_rejects_invalid_numbers_and_flags(self) -> None:
        with pytest.raises(ValueError):
            _basis(target_duration_ms=0)
        with pytest.raises(TypeError):
            _basis(target_duration_ms=True)
        with pytest.raises(ValueError):
            _basis(control_elapsed_ns=-1)
        with pytest.raises(TypeError):
            _basis(control_elapsed_ns=True)

    async def test_continuity_evidence_must_be_non_empty_tuple(self) -> None:
        with pytest.raises(ValueError):
            _basis(continuity_evidence=())
        with pytest.raises(TypeError):
            _basis(continuity_evidence=["anchor"])

    async def test_as_json_omits_unknown_members(self) -> None:
        assert _basis().as_json() == {
            "reason": 2, "target_duration_ms": 60_000}
        assert _basis(
            control_elapsed_ns=70_500_000_000,
            continuity_evidence=("anchor", "same-segment"),
        ).as_json() == {
            "reason": 2, "target_duration_ms": 60_000,
            "control_elapsed_ns": 70_500_000_000,
            "continuity_evidence": ["anchor", "same-segment"]}


class TestRepairBasis:
    async def test_threshold_required_for_threshold_reasons(self) -> None:
        with pytest.raises(ValueError):
            RepairBasis(
                reason=RepairReason.BELOW_THRESHOLD,
                target_duration_ms=60_000, threshold_s=None)
        with pytest.raises(ValueError):
            RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=60_000, threshold_s=None)

    async def test_threshold_forbidden_for_other_reasons(self) -> None:
        for reason in (RepairReason.NO_USABLE_INPUT, RepairReason.CANCELED):
            with pytest.raises(ValueError):
                RepairBasis(
                    reason=reason, target_duration_ms=60_000,
                    threshold_s=Decimal("70"))

    async def test_seconds_must_be_finite_non_negative(self) -> None:
        with pytest.raises(ValueError):
            RepairBasis(
                reason=RepairReason.BELOW_THRESHOLD,
                target_duration_ms=60_000, threshold_s=Decimal("-1"))
        with pytest.raises(ValueError):
            RepairBasis(
                reason=RepairReason.BELOW_THRESHOLD,
                target_duration_ms=60_000, threshold_s=Decimal("NaN"))
        with pytest.raises(ValueError):
            RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=60_000, threshold_s=Decimal("70"),
                actual_duration_s=Decimal("-0.5"))

    async def test_as_json_keeps_exact_decimals(self) -> None:
        basis = RepairBasis(
            reason=RepairReason.THRESHOLD_REACHED,
            target_duration_ms=60_000, threshold_s=Decimal("70"),
            actual_duration_s=Decimal("75.125"))
        assert basis.as_json() == {
            "reason": 2, "target_duration_ms": 60_000,
            "threshold_s": Decimal("70"),
            "actual_duration_s": Decimal("75.125")}


class TestMediaObservation:
    async def test_completed_requires_duration_without_error(self) -> None:
        with pytest.raises(ValueError):
            MediaObservation(CheckPhase.COMPLETED, duration_s=None)
        with pytest.raises(ValueError):
            MediaObservation(
                CheckPhase.COMPLETED, duration_s=Decimal("65"), error=_error())
        assert MediaObservation(
            CheckPhase.COMPLETED, duration_s=Decimal("0")).as_json() == {
            "check_status": "completed",
            "duration": {"status": "available", "seconds": Decimal("0")}}

    async def test_failed_and_unconfirmed_require_error(self) -> None:
        for phase in (CheckPhase.FAILED, CheckPhase.UNCONFIRMED):
            with pytest.raises(ValueError):
                MediaObservation(phase, duration_s=None)

    async def test_running_has_no_conclusion(self) -> None:
        with pytest.raises(ValueError):
            MediaObservation(CheckPhase.RUNNING, duration_s=Decimal("65"))
        with pytest.raises(ValueError):
            MediaObservation(CheckPhase.RUNNING, error=_error())

    async def test_issues_allowed_with_completed(self) -> None:
        observation = MediaObservation(
            CheckPhase.COMPLETED, duration_s=Decimal("65"),
            issues=(ProcessingError(
                "invalid_media", "media", {"at_s": "1"}),))
        document = observation.as_json()
        assert document["issues"] == [
            {"code": "invalid_media", "stage": "media", "details": {"at_s": "1"}}]

    async def test_unknown_duration_uses_status_object(self) -> None:
        document = MediaObservation(
            CheckPhase.UNCONFIRMED, error=_error("check_failed")).as_json()
        assert document["duration"] == {"status": "unknown"}
        assert "issues" not in document

    async def test_duration_must_be_finite_non_negative(self) -> None:
        with pytest.raises(ValueError):
            MediaObservation(
                CheckPhase.COMPLETED, duration_s=Decimal("-1"))


class TestDecisionCommands:
    _AT = 1

    async def test_check_decision_pairs_reason_with_choice(self) -> None:
        assert CheckDecisionSave(
            1, CheckDecisionChoice.REQUIRED, _basis(),
            occurred_at=self._AT).decision is CheckDecisionChoice.REQUIRED
        assert CheckDecisionSave(
            1, CheckDecisionChoice.NOT_NEEDED,
            _basis(reason=CheckReason.CONTINUOUS_CONTROL_COMPLETE),
            occurred_at=self._AT,
        ).decision is CheckDecisionChoice.NOT_NEEDED
        assert CheckDecisionSave(
            1, CheckDecisionChoice.NOT_NEEDED,
            _basis(reason=CheckReason.EXCESS_DURATION_CHECK),
            occurred_at=self._AT,
        ).decision is CheckDecisionChoice.NOT_NEEDED

    async def test_check_decision_rejects_mismatched_pairs(self) -> None:
        with pytest.raises(ValueError):
            CheckDecisionSave(
                1, CheckDecisionChoice.REQUIRED,
                _basis(reason=CheckReason.CONTINUOUS_CONTROL_COMPLETE),
                occurred_at=self._AT)
        with pytest.raises(ValueError):
            CheckDecisionSave(
                1, CheckDecisionChoice.NOT_NEEDED, _basis(),
                occurred_at=self._AT)

    async def test_repair_decision_pairs_reason_with_state(self) -> None:
        RepairDecisionSave(
            1, RepairDecisionChoice.PENDING, RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=60_000, threshold_s=Decimal("70")),
            occurred_at=self._AT)
        RepairDecisionSave(
            1, RepairDecisionChoice.NOT_NEEDED, RepairBasis(
                reason=RepairReason.NO_USABLE_INPUT,
                target_duration_ms=60_000),
            occurred_at=self._AT)
        RepairDecisionSave(
            1, RepairDecisionChoice.CANCELED, RepairBasis(
                reason=RepairReason.CANCELED, target_duration_ms=60_000),
            occurred_at=self._AT)

    async def test_repair_decision_rejects_mismatched_pairs(self) -> None:
        with pytest.raises(ValueError):
            RepairDecisionSave(
                1, RepairDecisionChoice.PENDING, RepairBasis(
                    reason=RepairReason.BELOW_THRESHOLD,
                    target_duration_ms=60_000, threshold_s=Decimal("70")),
                occurred_at=self._AT)
        with pytest.raises(ValueError):
            RepairDecisionSave(
                1, RepairDecisionChoice.CANCELED, RepairBasis(
                    reason=RepairReason.THRESHOLD_REACHED,
                    target_duration_ms=60_000, threshold_s=Decimal("70")),
                occurred_at=self._AT)


class TestResultCommands:
    _AT = 1

    async def test_repair_success_requires_output_file(self) -> None:
        with pytest.raises(ValueError):
            RepairResultSave(1, RepairOutcome.SUCCEEDED, occurred_at=self._AT)
        with pytest.raises(ValueError):
            RepairResultSave(
                1, RepairOutcome.SUCCEEDED, output_file_id=700,
                error=_error(), occurred_at=self._AT)

    async def test_repair_failure_requires_error_without_file(self) -> None:
        with pytest.raises(ValueError):
            RepairResultSave(1, RepairOutcome.FAILED, occurred_at=self._AT)
        with pytest.raises(ValueError):
            RepairResultSave(
                1, RepairOutcome.FAILED, output_file_id=700,
                error=_error("repair_failed"), occurred_at=self._AT)

    async def test_repair_transitional_phases_carry_no_facts(self) -> None:
        for phase in (RepairOutcome.RUNNING, RepairOutcome.CANCELED):
            RepairResultSave(1, phase, occurred_at=self._AT)
            with pytest.raises(ValueError):
                RepairResultSave(
                    1, phase, output_file_id=700, occurred_at=self._AT)
            with pytest.raises(ValueError):
                RepairResultSave(
                    1, phase, error=_error(), occurred_at=self._AT)

    async def test_discard_failure_requires_error(self) -> None:
        for phase in (DiscardPhase.FAILED, DiscardPhase.UNKNOWN):
            with pytest.raises(ValueError):
                DiscardProgressSave(1, phase, occurred_at=self._AT)
        for phase in (DiscardPhase.PENDING, DiscardPhase.RUNNING,
                      DiscardPhase.COMPLETED):
            DiscardProgressSave(1, phase, occurred_at=self._AT)
            with pytest.raises(ValueError):
                DiscardProgressSave(
                    1, phase, error=_error(), occurred_at=self._AT)

    async def test_source_file_requires_positive_ids(self) -> None:
        with pytest.raises(Exception):
            SourceFileSave(0, 11, 1)
        with pytest.raises(Exception):
            SourceFileSave(1, 0, 1)

    async def test_check_result_carries_phase_from_observation(self) -> None:
        command = CheckResultSave(
            1, MediaObservation(
                CheckPhase.COMPLETED, duration_s=Decimal("65")), occurred_at=1)
        assert command.media.phase is CheckPhase.COMPLETED


class TestClassifyCheckDuration:
    async def test_partitions_are_exact_at_boundaries(self) -> None:
        margin = Decimal("10")
        assert classify_check_duration(
            Decimal("60"), margin, Decimal("59.999")) \
            is CheckDurationClass.SHORT
        assert classify_check_duration(
            Decimal("60"), margin, Decimal("60")) \
            is CheckDurationClass.WITHIN
        assert classify_check_duration(
            Decimal("60"), margin, Decimal("65")) \
            is CheckDurationClass.WITHIN
        assert classify_check_duration(
            Decimal("60"), margin, Decimal("70")) \
            is CheckDurationClass.WITHIN
        assert classify_check_duration(
            Decimal("60"), margin, Decimal("70.001")) \
            is CheckDurationClass.OVER

    async def test_repair_basis_follows_check_classification(self) -> None:
        basis = repair_basis_from_check(
            Decimal("60"), Decimal("10"), Decimal("75.125"), 60_000)
        assert basis.reason is RepairReason.THRESHOLD_REACHED
        assert basis.threshold_s == Decimal("70")
        assert basis.actual_duration_s == Decimal("75.125")
        within = repair_basis_from_check(
            Decimal("60"), Decimal("10"), Decimal("65"), 60_000)
        assert within.reason is RepairReason.BELOW_THRESHOLD
        assert within.actual_duration_s == Decimal("65")
        short = repair_basis_from_check(
            Decimal("60"), Decimal("10"), Decimal("30"), 60_000)
        assert short.reason is RepairReason.BELOW_THRESHOLD
        assert short.actual_duration_s == Decimal("30")


class TestProcessingDisposition:
    async def test_disposition_members_are_distinct(self) -> None:
        assert ProcessingDisposition.SAVED is not ProcessingDisposition.ALREADY
