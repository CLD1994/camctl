"""C8 录像动作结果判定与执行失败登记的单元测试。

控制完成与时长检查是两类独立成功依据；时长不足与明确媒体错误优
先按失败处理，修复成功不替代采集依据，修复失败不否定已有依据；
处理未结束时保持待定。失败采用公共动作错误码（录像不足、必要处
理后仍无成功依据）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from camctl.capture.media import (
    RecordingFailure,
    RecordingOutcomeFacts,
    RecordingResultKind,
    decide_recording_result,
)

_TARGET_MS = 60_000


def _facts(**overrides: Any) -> RecordingOutcomeFacts:
    values: dict[str, Any] = {
        "processing_id": 5,
        "control_complete": False,
        "check_decision": 3,
        "check_state": 1,
        "check_duration_s": None,
        "check_issues": False,
        "input_unavailable": False,
        "repair_state": 1,
        "target_duration_ms": _TARGET_MS,
    }
    values.update(overrides)
    return RecordingOutcomeFacts(**values)


def _completed(duration: str, **overrides: Any) -> RecordingOutcomeFacts:
    return _facts(check_state=3, check_duration_s=Decimal(duration), **overrides)


# ---- 处理未结束 ----


@pytest.mark.parametrize("check_state", [1, 2])
def test_open_check_keeps_action_pending(check_state) -> None:
    assert decide_recording_result(
        _facts(check_state=check_state)).kind is RecordingResultKind.PENDING


def test_pending_repair_defers_finish_beyond_success_basis() -> None:
    """已取得时长成功依据仍等必要修复最终结果。"""
    result = decide_recording_result(_completed("75", repair_state=3))
    assert result.kind is RecordingResultKind.PENDING


def test_running_repair_defers_finish_with_control_basis() -> None:
    result = decide_recording_result(_facts(
        control_complete=True, check_decision=2, repair_state=4))
    assert result.kind is RecordingResultKind.PENDING


def test_undecided_check_without_basis_keeps_pending() -> None:
    result = decide_recording_result(_facts(check_decision=1))
    assert result.kind is RecordingResultKind.PENDING


# ---- 明确失败证据 ----


def test_short_duration_fails_with_too_short() -> None:
    result = decide_recording_result(_completed("30"))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure == RecordingFailure(
        code="recording_too_short", details={"processing_id": "5"})


def test_repair_success_does_not_replace_capture_result() -> None:
    """修复成品合格但采集依据不满足：动作仍按录像不足失败。"""
    result = decide_recording_result(_completed("30", repair_state=5))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.code == "recording_too_short"


def test_definite_media_issues_fail_with_invalid_media() -> None:
    result = decide_recording_result(_completed("75", check_issues=True))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.code == "recording_processing_failed"
    assert result.failure.details == {
        "processing_id": "5", "reason": "invalid_media"}


def test_input_unavailable_fails_without_running_check() -> None:
    """输入副本无法取得：检查不能执行，按无可用来源失败。"""
    result = decide_recording_result(_facts(input_unavailable=True))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.details == {
        "processing_id": "5", "reason": "source_unavailable"}


def test_space_exhaustion_fails_fast_without_success_basis() -> None:
    """板端空间不足：拉取/核验快速失败，动作按无可用来源失败。

    空间错误经拷贝写入失败分区表达（不等待释放、不跨 run 重试），
    判定层只见输入不可得。
    """
    result = decide_recording_result(_facts(input_unavailable=True))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.code == "recording_processing_failed"
    assert result.failure.details["reason"] == "source_unavailable"


# ---- 成功依据 ----


def test_control_completion_succeeds_without_media_check() -> None:
    result = decide_recording_result(_facts(
        control_complete=True, check_decision=2))
    assert result.kind is RecordingResultKind.SUCCEEDED
    assert result.basis == "control_complete"


def test_control_basis_survives_check_tool_failure() -> None:
    """已有控制成功依据时，检查工具失败不单独否定该依据。"""
    result = decide_recording_result(_facts(
        control_complete=True, check_state=4))
    assert result.kind is RecordingResultKind.SUCCEEDED
    assert result.basis == "control_complete"


@pytest.mark.parametrize("duration", ["60", "75.125"])
def test_check_duration_meeting_target_succeeds(duration) -> None:
    result = decide_recording_result(_completed(duration))
    assert result.kind is RecordingResultKind.SUCCEEDED
    assert result.basis == "check_duration"


def test_repair_failure_does_not_negate_success_basis() -> None:
    result = decide_recording_result(_completed("75", repair_state=6))
    assert result.kind is RecordingResultKind.SUCCEEDED


def test_canceled_repair_with_control_basis_succeeds() -> None:
    result = decide_recording_result(_facts(
        control_complete=True, check_decision=2, repair_state=7))
    assert result.kind is RecordingResultKind.SUCCEEDED


# ---- 无依据的终局失败 ----


def test_failed_check_without_basis_fails() -> None:
    result = decide_recording_result(_facts(check_state=4))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.code == "recording_processing_failed"
    assert result.failure.details == {
        "processing_id": "5", "reason": "check_failed"}


def test_unconfirmed_check_without_basis_fails() -> None:
    result = decide_recording_result(_facts(check_state=5))
    assert result.kind is RecordingResultKind.FAILED
    assert result.failure.details == {
        "processing_id": "5", "reason": "duration_unconfirmed"}


# ---- 登记错误输入 ----


def test_failure_registration_requires_code_and_details() -> None:
    with pytest.raises(ValueError):
        RecordingFailure(code="", details={})
    with pytest.raises(TypeError):
        RecordingFailure(code="recording_too_short", details=None)  # type: ignore[arg-type]
