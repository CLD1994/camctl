"""时钟端口：单调纳秒与精确 UTC 微秒。

本模块只定义调用契约；标准库实际适配由 bootstrap 装配创建，
测试使用替身，本模块不读取实际时钟。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from camctl.contracts.values import UtcMicros


class ClockReadError(RuntimeError):
    """时钟读取失败，不能以默认值或当前估计代替。"""


@runtime_checkable
class MonotonicClock(Protocol):
    """本机单调时钟，只保证同一次运行内的可比性。"""

    def monotonic_ns(self) -> int:
        """返回单调递增的整数纳秒读数。"""
        ...


@runtime_checkable
class ClockPort(MonotonicClock, Protocol):
    """普通执行与时钟资格检查使用的完整时钟端口。"""

    def utc_micros(self) -> UtcMicros:
        """返回当前 UTC 时间的整数微秒。"""
        ...
