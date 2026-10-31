"""将各处理阶段的单调钟耗时投递到当前命令的辅助日志队列。"""

from contextlib import contextmanager
from time import monotonic_ns

from camctl.logging_runtime.events import (
    event_logging as performance_logging, logging_enabled as performance_enabled, record_event,
)
from camctl.logging_runtime.models import LogLevel


def record_performance(stage: str, elapsed_ns: int, **fields) -> None:
    """普通 DEBUG 诊断不等待文件写入，投递失败不改变业务结果。"""
    record_event(stage, event="timing", level=LogLevel.DEBUG,
                 elapsed_ms=round(elapsed_ns / 1_000_000, 3), **fields)


@contextmanager
def measure_performance(stage: str, **fields):
    """记录实际调用至返回或抛错的经过时间，不截获业务异常。"""
    began = monotonic_ns()
    try:
        yield
    finally:
        record_performance(stage, monotonic_ns() - began, **fields)
