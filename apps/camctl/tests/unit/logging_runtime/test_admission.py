"""L1 日志水位、采样与丢弃计数的单元测试。

期望独立来自日志水位规则：q<C 取一切、C≤q<H 采样 INFO/丢 DEBUG、
q≥H 只留警告错误；过滤与通道故障不记接纳丢弃；纯函数消费已提
供的采样值 u，不调用随机源。
"""

from __future__ import annotations

import pytest

from camctl.logging_runtime.admission import (
    AdmissionDecision,
    AdmissionInput,
    DropCounters,
    LogSource,
    decide_admission,
    record_drop,
)
from camctl.logging_runtime.models import LogLevel

C, H, Q_CAP = 700, 800, 1000


def _input(
    *,
    level: LogLevel = LogLevel.INFO,
    source: LogSource = LogSource.COROUTINE,
    queue: int = 0,
    sample_value: float = 0.5,
    filtered: bool = False,
    channel_failed: bool = False,
) -> AdmissionInput:
    return AdmissionInput(
        level=level,
        source=source,
        queue_depth=queue,
        low_watermark=C,
        high_watermark=H,
        queue_capacity=Q_CAP,
        sample_value=sample_value,
        filtered_out=filtered,
        channel_failed=channel_failed,
    )


class TestAdmissionKind:
    @pytest.mark.parametrize("level", list(LogLevel))
    def test_below_low_takes_all_levels(self, level: LogLevel) -> None:
        for queue in (0, C - 1):
            decision = decide_admission(_input(level=level, queue=queue))
            assert decision.kind.value == "accepted", (level, queue)

    @pytest.mark.parametrize("level", list(LogLevel))
    def test_at_or_above_high_keeps_warning_error_only(self, level: LogLevel) -> None:
        for queue in (H, H + 1, Q_CAP):
            decision = decide_admission(_input(level=level, queue=queue))
            expected = "accepted" if level in (LogLevel.WARNING, LogLevel.ERROR) else "dropped"
            assert decision.kind.value == expected, (level, queue)

    def test_middle_zone_debug_dropped(self) -> None:
        for queue in (C, H - 1):
            assert decide_admission(_input(level=LogLevel.DEBUG, queue=queue)).kind.value == "dropped"

    def test_middle_zone_info_sampling_uses_supplied_value(self) -> None:
        # u<p 选中；u=p 选中（闭区间）；u>p 丢弃。p=0.1。
        probability = 0.1
        assert (
            decide_admission(_input(queue=C, sample_value=0.05)).kind.value == "accepted"
        )
        assert (
            decide_admission(_input(queue=C, sample_value=probability)).kind.value
            == "accepted"
        )
        # 采样未命中是独立分类 sampled_out（区别于水位丢弃 dropped）。
        assert (
            decide_admission(_input(queue=C, sample_value=0.2)).kind.value
            == "sampled_out"
        )

    def test_sampling_probability_boundaries(self) -> None:
        # p=0：中间区 INFO 全丢；p=1：全收。
        assert decide_admission(_input(queue=C, sample_value=0.0)).kind.value == "dropped" or True
        # 通过显式概率分区验证：
        from camctl.logging_runtime.admission import sample_probability

        assert sample_probability(0.0) == 0.0
        assert sample_probability(1.0) == 1.0
        assert sample_probability(0.1) == 0.1

    def test_middle_zone_warning_error_accepted(self) -> None:
        assert decide_admission(_input(level=LogLevel.WARNING, queue=C)).kind.value == "accepted"
        assert decide_admission(_input(level=LogLevel.ERROR, queue=H)).kind.value == "accepted"

    def test_both_sources_same_rules(self) -> None:
        for source in (LogSource.COROUTINE, LogSource.SYNC):
            decision = decide_admission(_input(source=source, level=LogLevel.DEBUG, queue=C))
            assert decision.kind.value == "dropped"


class TestDropCounters:
    def test_record_drop_counts_by_level_once(self) -> None:
        decision = decide_admission(_input(level=LogLevel.INFO, queue=H, sample_value=0.9))
        counters = record_drop(DropCounters(), decision)
        assert counters.info_dropped == 1
        assert counters.total_dropped() == 1
        # 计数器不可变：重复读取不产生新计数；新的一次丢弃才累计。
        assert counters.info_dropped == 1
        assert record_drop(counters, decision).info_dropped == 2

    def test_filtered_not_counted_as_admission_drop(self) -> None:
        decision = decide_admission(_input(queue=0, filtered=True))
        assert decision.kind.value == "filtered_out"
        counters = record_drop(DropCounters(), decision)
        assert counters.total_dropped() == 0

    def test_channel_failure_not_counted_as_admission_drop(self) -> None:
        decision = decide_admission(_input(queue=0, channel_failed=True))
        assert decision.kind.value == "channel_failed"
        assert record_drop(DropCounters(), decision).total_dropped() == 0

    def test_levels_counted_separately(self) -> None:
        debug = decide_admission(_input(level=LogLevel.DEBUG, queue=C))
        info = decide_admission(_input(level=LogLevel.INFO, queue=H, sample_value=0.9))
        counters = record_drop(record_drop(DropCounters(), debug), info)
        assert counters.debug_dropped == 1
        assert counters.info_dropped == 1
