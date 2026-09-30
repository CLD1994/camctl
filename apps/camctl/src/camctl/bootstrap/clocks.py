"""系统时钟适配：精确 UTC 微秒与单调纳秒。"""

from __future__ import annotations

import time

__all__ = ["SystemClock"]


class SystemClock:
    """标准库时钟适配；读取失败由调用方按时钟错误处理。"""

    def utc_micros(self) -> int:
        return time.time_ns() // 1_000

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()
