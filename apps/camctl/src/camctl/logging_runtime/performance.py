"""将各处理阶段的单调钟耗时投递到当前命令的辅助日志队列。"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from time import monotonic_ns

from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord


_channel = ContextVar("camctl_performance_channel", default=None)


def performance_enabled() -> bool:
    """只判断是否存在当前命令的辅助日志目标，不执行投递。"""
    return _channel.get() is not None


@contextmanager
def performance_logging(channel):
    """限定当前命令及其子任务的投递目标，结束时恢复原上下文。"""
    token = _channel.set(channel)
    try:
        yield
    finally:
        _channel.reset(token)


def record_performance(stage: str, elapsed_ns: int, **fields) -> None:
    """普通 DEBUG 诊断不等待文件写入，投递失败不改变业务结果。"""
    channel = _channel.get()
    if channel is None:
        return
    message = "性能计时 " + json.dumps(
        {"stage": stage, "elapsed_ms": round(elapsed_ns / 1_000_000, 3), **fields},
        ensure_ascii=False, separators=(",", ":"),
    )
    try:
        channel.slog(LogRecord(level=LogLevel.DEBUG, message=message))
    except Exception:
        # 普通辅助诊断投递失败不授予或撤销业务资格，也不重试投递。
        return


@contextmanager
def measure_performance(stage: str, **fields):
    """记录实际调用至返回或抛错的经过时间，不截获业务异常。"""
    began = monotonic_ns()
    try:
        yield
    finally:
        record_performance(stage, monotonic_ns() - began, **fields)
