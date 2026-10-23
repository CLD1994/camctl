"""纯视频覆盖目标、可靠累计及结束判定。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.segment_values import (
    DefaultScopeState,
    SegmentCursor,
    SegmentKey,
    SegmentSetRef,
    decode_text_enum,
)

__all__ = [
    "CoverageAccumulator",
    "CoverageDecision",
    "CoverageFinishFacts",
    "CoverageMetric",
    "CoverageStatus",
    "CoverageTarget",
    "SegmentDecision",
    "SegmentDecisionScope",
    "SegmentDecisionStatus",
    "VideoMetric",
    "advance_coverage",
    "decision_from_document",
    "finish_coverage",
    "target_frames",
]


class CoverageMetric(StrEnum):
    DURATION_S = "duration_s"
    FRAMES = "frames"


class CoverageStatus(StrEnum):
    NOT_PERFORMED = "not_performed"
    REACHED = "reached"
    CONFIRMED_SHORT = "confirmed_short"
    UNCONFIRMED = "unconfirmed"


class SegmentDecisionStatus(StrEnum):
    """公共来源决策的有限状态；不复用 OSMO 的分段判断状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    CANCELED = "canceled"


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


@dataclass(frozen=True, slots=True)
class SegmentDecisionScope:
    """不展开成员数组的公共默认视频范围。"""

    state: DefaultScopeState
    set_ref: SegmentSetRef
    first_cursor: SegmentCursor | None
    last_cursor: SegmentCursor | None
    member_count: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, DefaultScopeState):
            raise ValueError("公共默认范围状态未登记")
        if not isinstance(self.set_ref, SegmentSetRef):
            raise ValueError("公共默认范围必须引用冻结结果集合")
        if self.state is DefaultScopeState.UNKNOWN:
            if self.first_cursor is not None or self.last_cursor is not None or self.member_count is not None:
                raise ValueError("未知范围不得携带猜测边界或数量")
        elif self.state is DefaultScopeState.EMPTY:
            if self.first_cursor is not None or self.last_cursor is not None or self.member_count != 0:
                raise ValueError("空范围必须明确为零成员且不带游标")
        elif (self.first_cursor is None or self.last_cursor is None
              or type(self.member_count) is not int or self.member_count <= 0
              or self.last_cursor < self.first_cursor):
            raise ValueError("非空范围必须提供有序首尾游标和成员数")


@dataclass(frozen=True, slots=True)
class SegmentDecision:
    """公共分段完成头；设备特化的指标不进入 OSMO SegmentAssessment。"""

    set_ref: SegmentSetRef
    status: SegmentDecisionStatus
    metric: CoverageMetric
    target: Decimal | int
    confirmed: Decimal | int | None
    coverage_status: CoverageStatus
    checked_count: int
    default_scope: SegmentDecisionScope
    failure_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.set_ref, SegmentSetRef):
            raise ValueError("公共分段判断必须引用冻结集合")
        if not isinstance(self.status, SegmentDecisionStatus):
            raise ValueError("公共分段终态未登记")
        if not isinstance(self.metric, CoverageMetric):
            raise ValueError("覆盖量纲未登记")
        if not isinstance(self.coverage_status, CoverageStatus):
            raise ValueError("覆盖状态未登记")
        if not isinstance(self.default_scope, SegmentDecisionScope):
            raise ValueError("公共默认范围类型无效")
        if self.default_scope.set_ref != self.set_ref:
            raise ValueError("决策与默认范围必须引用同一集合")
        if self.metric is CoverageMetric.DURATION_S:
            if (not isinstance(self.target, Decimal) or not self.target.is_finite()
                    or self.target <= 0):
                raise ValueError("时长目标必须是正的精确秒数")
            if self.confirmed is not None and (
                not isinstance(self.confirmed, Decimal) or not self.confirmed.is_finite()
                or self.confirmed < 0
            ):
                raise ValueError("已确认时长必须是非负精确秒数或未知")
        else:
            if type(self.target) is not int or self.target <= 0:
                raise ValueError("帧数目标必须是正整数")
            if self.confirmed is not None and (
                type(self.confirmed) is not int or self.confirmed < 0
            ):
                raise ValueError("已确认帧数必须是非负整数或未知")
        if type(self.checked_count) is not int or self.checked_count < 0:
            raise ValueError("已检查成员数必须是非负整数")
        if self.coverage_status is CoverageStatus.NOT_PERFORMED and (
            self.confirmed is not None or self.checked_count != 0
        ):
            raise ValueError("未执行检查必须保持实际量未知且没有检查成员")
        if self.coverage_status is CoverageStatus.REACHED and (
            self.confirmed is None or self.confirmed < self.target
        ):
            raise ValueError("已达覆盖目标必须携带不低于目标的可靠量")
        if self.coverage_status is CoverageStatus.CONFIRMED_SHORT and (
            self.confirmed is None or self.confirmed >= self.target
        ):
            raise ValueError("确认不足必须携带低于目标的可靠量")
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str) or not self.failure_code.strip()
        ):
            raise ValueError("失败代码必须是非空标识或未知")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("决策原因必须是非空文本或未知")
        if self.status in (SegmentDecisionStatus.SUCCEEDED, SegmentDecisionStatus.CANCELED):
            if self.failure_code is not None:
                raise ValueError("成功或取消决策不得携带失败代码")
        elif self.failure_code is None or self.reason is None:
            raise ValueError("失败或未确认决策必须携带公开原因代码和说明")


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


def _decision_cursor(value: dict | None) -> SegmentCursor | None:
    if value is None:
        return None
    key_document = value["key"]
    key = None if key_document is None else SegmentKey(**key_document)
    return SegmentCursor(key, value["member_identity"])


def decision_from_document(value: dict) -> SegmentDecision:
    """解码持久化的公共有限视频判决，不解释设备特化 assessment。"""
    raw = dict(value)
    if raw.pop("decision_type", None) != "video_coverage_v1":
        raise ValueError("公共分段决策类型未登记")
    raw["set_ref"] = SegmentSetRef(**raw["set_ref"])
    raw["status"] = SegmentDecisionStatus(raw["status"])
    raw["metric"] = CoverageMetric(raw["metric"])
    raw["coverage_status"] = CoverageStatus(raw["coverage_status"])
    if raw["metric"] is CoverageMetric.DURATION_S:
        raw["target"] = Decimal(raw["target"])
        if raw["confirmed"] is not None:
            raw["confirmed"] = Decimal(raw["confirmed"])
    scope = dict(raw["default_scope"])
    scope["state"] = decode_text_enum(
        DefaultScopeState,
        scope["state"],
        column="segment_sets.assessment_json.scope_state",
    )
    scope["set_ref"] = SegmentSetRef(**scope["set_ref"])
    for name in ("first_cursor", "last_cursor"):
        scope[name] = _decision_cursor(scope[name])
    raw["default_scope"] = SegmentDecisionScope(**scope)
    return SegmentDecision(**raw)
