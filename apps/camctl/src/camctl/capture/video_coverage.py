"""纯视频覆盖目标、可靠累计及结束判定。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "CoverageAccumulator",
    "CoverageDecision",
    "CoverageFinishFacts",
    "CoverageMetric",
    "CoverageStatus",
    "CoverageTarget",
    "VideoMetric",
    "advance_coverage",
    "finish_coverage",
    "target_frames",
]


class CoverageMetric(StrEnum):
    DURATION_S = "duration_s"
    FRAMES = "frames"


class CoverageStatus(StrEnum):
    REACHED = "reached"
    CONFIRMED_SHORT = "confirmed_short"
    UNCONFIRMED = "unconfirmed"


def _metric(value: CoverageMetric | str) -> CoverageMetric:
    try:
        return CoverageMetric(value)
    except (TypeError, ValueError):
        raise ValueError("覆盖量纲必须为 duration_s 或 frames") from None


def _exact_seconds(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{label} 必须为 Decimal 或整数秒")
    seconds = Decimal(value)
    if not seconds.is_finite() or seconds < 0:
        raise ValueError(f"{label} 必须为有限非负秒数")
    return seconds


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("覆盖中断原因必须为非空文本或未知")
    return value


def _value(metric: CoverageMetric, value: object, *, allow_unknown: bool) -> Decimal | int | None:
    if value is None:
        if allow_unknown:
            return None
        raise ValueError("覆盖目标不能未知")
    if metric is CoverageMetric.DURATION_S:
        return _exact_seconds(value, "覆盖时长")
    if type(value) is not int or value < 0:
        raise ValueError("帧数必须为非负整数")
    return value


def _add_seconds_exact(left: Decimal, right: Decimal) -> Decimal:
    """用系数对齐相加，避免 Decimal 当前上下文舍入累计时长。"""
    left_sign, left_digits, left_exponent = left.as_tuple()
    right_sign, right_digits, right_exponent = right.as_tuple()
    exponent = min(left_exponent, right_exponent)

    def coefficient(sign: int, digits: tuple[int, ...], value_exponent: int) -> int:
        value = int("".join(str(digit) for digit in digits))
        if sign:
            value = -value
        return value * (10 ** (value_exponent - exponent))

    total = (
        coefficient(left_sign, left_digits, left_exponent)
        + coefficient(right_sign, right_digits, right_exponent)
    )
    sign = int(total < 0)
    digits = tuple(int(digit) for digit in str(abs(total)))
    return Decimal((sign, digits, exponent))


@dataclass(frozen=True)
class CoverageTarget:
    metric: CoverageMetric
    value: Decimal | int

    def __post_init__(self) -> None:
        metric = _metric(self.metric)
        value = _value(metric, self.value, allow_unknown=False)
        assert value is not None
        if value <= 0:
            raise ValueError("覆盖目标必须大于零")
        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class VideoMetric:
    """单个连续视频段的主视频度量；未知值不会被解释为零。"""

    metric: CoverageMetric
    value: Decimal | int | None
    reason: str | None = None

    def __post_init__(self) -> None:
        metric = _metric(self.metric)
        value = _value(metric, self.value, allow_unknown=True)
        reason = _reason(self.reason)
        if value is not None and reason is not None:
            raise ValueError("可靠覆盖度量不能同时带未知原因")
        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, init=False)
class CoverageAccumulator:
    target: CoverageTarget
    confirmed: Decimal | int
    confirmed_segments: int
    interruption_reason: str | None

    def __init__(
        self,
        target: CoverageTarget,
        confirmed: Decimal | int | None = None,
        confirmed_segments: int = 0,
        interruption_reason: str | None = None,
    ) -> None:
        if not isinstance(target, CoverageTarget):
            raise ValueError("覆盖累计必须有 CoverageTarget")
        initial = (
            Decimal(0) if target.metric is CoverageMetric.DURATION_S else 0
        ) if confirmed is None else _value(target.metric, confirmed, allow_unknown=False)
        assert initial is not None
        if type(confirmed_segments) is not int or confirmed_segments < 0:
            raise ValueError("已确认分段数必须为非负整数")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "confirmed", initial)
        object.__setattr__(self, "confirmed_segments", confirmed_segments)
        object.__setattr__(self, "interruption_reason", _reason(interruption_reason))

    @property
    def reached(self) -> bool:
        return self.confirmed >= self.target.value


@dataclass(frozen=True)
class CoverageFinishFacts:
    """结束时可由编排提供的完整集合事实。"""

    collection_complete: bool
    interruption_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.collection_complete) is not bool:
            raise ValueError("collection_complete 必须为布尔值")
        reason = _reason(self.interruption_reason)
        if self.collection_complete and reason is not None:
            raise ValueError("完整集合不能带中断原因")
        object.__setattr__(self, "interruption_reason", reason)


@dataclass(frozen=True)
class CoverageDecision:
    metric: CoverageMetric
    target: Decimal | int
    confirmed: Decimal | int
    confirmed_segments: int
    status: CoverageStatus
    reason: str | None = None


def target_frames(duration_s: Decimal | int, interval_s: Decimal | int) -> int:
    """按 `floor(D/I)+1` 计算采样位置数，不虚构非整倍终点。"""
    duration = _exact_seconds(duration_s, "采集时长")
    interval = _exact_seconds(interval_s, "采样间隔")
    if interval <= 0:
        raise ValueError("采样间隔必须大于零")
    duration_numerator, duration_denominator = duration.as_integer_ratio()
    interval_numerator, interval_denominator = interval.as_integer_ratio()
    quotient = (
        duration_numerator * interval_denominator
    ) // (
        duration_denominator * interval_numerator
    )
    return quotient + 1


def advance_coverage(
    state: CoverageAccumulator, metric: VideoMetric,
) -> CoverageAccumulator:
    """累计一个连续段；未知或达到后冻结，拒绝跨越后续段。"""
    if not isinstance(state, CoverageAccumulator) or not isinstance(metric, VideoMetric):
        raise ValueError("覆盖累计输入类型不正确")
    if state.reached:
        raise ValueError("覆盖已达到目标，不能再累计后续分段")
    if state.interruption_reason is not None:
        raise ValueError("覆盖前缀已因未知事实中断，不能跳过后续分段")
    if metric.metric is not state.target.metric:
        raise ValueError("覆盖目标与实际度量量纲不一致")
    if metric.value is None:
        return CoverageAccumulator(
            state.target, state.confirmed, state.confirmed_segments,
            metric.reason or "metric_unknown",
        )
    confirmed = (
        _add_seconds_exact(state.confirmed, metric.value)
        if state.target.metric is CoverageMetric.DURATION_S
        else state.confirmed + metric.value
    )
    return CoverageAccumulator(
        state.target,
        confirmed,
        state.confirmed_segments + 1,
    )


def finish_coverage(
    state: CoverageAccumulator, facts: CoverageFinishFacts,
) -> CoverageDecision:
    """仅把完整可靠集合判为确认不足，未知尾部保持未确认。"""
    if not isinstance(state, CoverageAccumulator) or not isinstance(facts, CoverageFinishFacts):
        raise ValueError("覆盖结束输入类型不正确")
    if state.reached:
        status = CoverageStatus.REACHED
        reason = None
    elif state.interruption_reason is not None:
        status = CoverageStatus.UNCONFIRMED
        reason = state.interruption_reason
    elif not facts.collection_complete:
        status = CoverageStatus.UNCONFIRMED
        reason = facts.interruption_reason or "collection_incomplete"
    else:
        status = CoverageStatus.CONFIRMED_SHORT
        reason = None
    return CoverageDecision(
        metric=state.target.metric,
        target=state.target.value,
        confirmed=state.confirmed,
        confirmed_segments=state.confirmed_segments,
        status=status,
        reason=reason,
    )
