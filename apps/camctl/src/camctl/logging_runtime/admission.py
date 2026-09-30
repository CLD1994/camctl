"""日志按水位接纳与采样规则（纯函数）。

水位规则：q<C 接纳全部级别；C≤q<H 停止 DEBUG、对 INFO 按概率采
样；q≥H 停止 INFO，剩余位置供警告和错误使用。过滤与通道故障是
独立分类，不计入接纳丢弃。采样消费调用方提供的一次独立随机值，
本模块不访问随机源或队列。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from camctl.logging_runtime.models import LogLevel

__all__ = [
    "AdmissionDecision",
    "AdmissionInput",
    "AdmissionKind",
    "DropCounters",
    "LogSource",
    "decide_admission",
    "record_drop",
    "sample_probability",
]


class AdmissionKind(enum.Enum):
    ACCEPTED = "accepted"
    SAMPLED_OUT = "sampled_out"
    DROPPED = "dropped"
    FILTERED_OUT = "filtered_out"
    CHANNEL_FAILED = "channel_failed"


class LogSource(enum.Enum):
    """日志来源双方：协程侧与同步线程侧遵守同一水位规则。"""

    COROUTINE = "coroutine"
    SYNC = "sync"


@dataclass(frozen=True)
class AdmissionInput:
    """一次接纳判定输入：级别、水位事实、过滤与通道状态及采样值 u。"""

    level: LogLevel
    source: LogSource
    queue_depth: int
    low_watermark: int
    high_watermark: int
    queue_capacity: int
    sample_value: float
    #: 配置的 INFO 采样概率（中间区使用）。
    sample_probability_hint: float = 0.1
    filtered_out: bool = False
    channel_failed: bool = False


@dataclass(frozen=True)
class AdmissionDecision:
    kind: AdmissionKind
    level: LogLevel


def sample_probability(configured: float) -> float:
    """采样概率的合法闭区间 [0, 1]。"""
    return min(1.0, max(0.0, configured))


def decide_admission(log_input: AdmissionInput) -> AdmissionDecision:
    """按水位与采样规则判定一条日志的接纳。

    过滤命中与通道故障优先表达为独立分类；接纳丢弃只来自水位与
    采样，且警告与错误始终保留。
    """
    if log_input.channel_failed:
        return AdmissionDecision(AdmissionKind.CHANNEL_FAILED, log_input.level)
    if log_input.filtered_out:
        return AdmissionDecision(AdmissionKind.FILTERED_OUT, log_input.level)
    level = log_input.level
    if level in (LogLevel.WARNING, LogLevel.ERROR):
        return AdmissionDecision(AdmissionKind.ACCEPTED, level)
    queue = log_input.queue_depth
    if queue < log_input.low_watermark:
        return AdmissionDecision(AdmissionKind.ACCEPTED, level)
    if level is LogLevel.DEBUG:
        return AdmissionDecision(AdmissionKind.DROPPED, level)
    if queue >= log_input.high_watermark:
        return AdmissionDecision(AdmissionKind.DROPPED, level)
    # 中间区的 INFO 采样：u ≤ p 选中。
    probability = sample_probability(log_input.sample_probability_hint)
    if log_input.sample_value <= probability:
        return AdmissionDecision(AdmissionKind.ACCEPTED, level)
    return AdmissionDecision(AdmissionKind.SAMPLED_OUT, level)


@dataclass(frozen=True)
class DropCounters:
    """按级别的接纳丢弃计数；不保存被丢消息内容。"""

    debug_dropped: int = 0
    info_dropped: int = 0
    warning_dropped: int = 0
    error_dropped: int = 0

    def total_dropped(self) -> int:
        return (
            self.debug_dropped
            + self.info_dropped
            + self.warning_dropped
            + self.error_dropped
        )


def record_drop(counters: DropCounters, decision: AdmissionDecision) -> DropCounters:
    """把一次接纳丢弃计入对应级别；过滤与通道故障不计数。"""
    if decision.kind not in (AdmissionKind.DROPPED, AdmissionKind.SAMPLED_OUT):
        return counters
    field = f"{decision.level.value}_dropped"
    return replace(counters, **{field: getattr(counters, field) + 1})
