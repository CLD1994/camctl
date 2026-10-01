"""异常录像检查与内部修复的决策规则。

修复门槛 = duration_s + 固定余量秒数 T；可信计时严格超过门槛才
触发修复，恰好相等不触发。仅下界证据时下界超门槛即确认多录，
未超不能证明无需修复；计时不足按未知进入原片检查。修复决定只
表达处理分支，不替代采集结果；源文件停止与写完确认前不读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

__all__ = [
    "MediaDecision",
    "MediaKind",
    "MediaPolicy",
    "RecordingEvidence",
    "decide_media_processing",
]


class MediaKind(Enum):
    """媒体文件的用途身份；原片、修复输入、临时输出与成品分开。"""

    ORIGINAL = "original"
    RECORDING_INPUT = "recording_input"
    TEMP_OUTPUT = "temp_output"
    REPAIRED = "repaired"


@dataclass(frozen=True)
class MediaPolicy:
    """修复余量：固定秒数，不随目标时长按比例变化。"""

    repair_margin_s: Decimal

    def __post_init__(self) -> None:
        value = self.repair_margin_s
        if isinstance(value, float):
            try:
                value = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError(f"余量必须是非负有限秒数: {self.repair_margin_s!r}") from error
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError(f"余量必须是非负有限秒数: {self.repair_margin_s!r}")
        object.__setattr__(self, "repair_margin_s", value)


@dataclass(frozen=True)
class RecordingEvidence:
    """修复判定的已保存证据。

    confirmed_recorded_s 是同段连续录像的已确认录制时长；
    recorded_lower_bound_s 仅为下界证据。两者互斥提供。
    """

    recording_completed_normally: bool
    recording_abnormal: bool
    duration_s: Decimal
    confirmed_recorded_s: Decimal | None
    recorded_lower_bound_s: Decimal | None
    timing_insufficient: bool
    stop_confirmed: bool
    source_file_complete: bool
    facts_readable: bool


class MediaDecision(Enum):
    """媒体处理分支；不携带采集成功结论。"""

    NORMAL_COMPLETION = "normal_completion"
    REPAIR_REQUIRED = "repair_required"
    NO_REPAIR_WITHIN_THRESHOLD = "no_repair_within_threshold"
    TIMING_UNKNOWN_CHECK_ORIGINAL = "timing_unknown_check_original"
    WAIT_STOP_AND_FILE = "wait_stop_and_file"
    CONFIG_ERROR = "config_error"


def decide_media_processing(
    evidence: RecordingEvidence, policy: MediaPolicy
) -> MediaDecision:
    """按多录门槛与计时证据决定处理分支。"""
    if not evidence.facts_readable:
        return MediaDecision.CONFIG_ERROR
    if evidence.recording_completed_normally and not evidence.recording_abnormal:
        return MediaDecision.NORMAL_COMPLETION
    threshold = evidence.duration_s + policy.repair_margin_s
    if evidence.confirmed_recorded_s is not None:
        if evidence.confirmed_recorded_s > threshold:
            return _gate_repair_start(evidence)
        return MediaDecision.NO_REPAIR_WITHIN_THRESHOLD
    if evidence.recorded_lower_bound_s is not None:
        if evidence.recorded_lower_bound_s > threshold:
            return _gate_repair_start(evidence)
        return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL
    if evidence.timing_insufficient:
        return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL
    return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL


def _gate_repair_start(evidence: RecordingEvidence) -> MediaDecision:
    """已确认多录：停止及源文件写完确认前不读取仍在写入的文件。"""
    if evidence.stop_confirmed and evidence.source_file_complete:
        return MediaDecision.REPAIR_REQUIRED
    return MediaDecision.WAIT_STOP_AND_FILE
