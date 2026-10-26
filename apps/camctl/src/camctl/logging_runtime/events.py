"""关键阶段的非阻塞记录及任务间可靠身份上下文。"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic_ns

from camctl.logging_runtime.formatting import encode_record
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

_channel = ContextVar("camctl_event_channel", default=None)
_fields = ContextVar("camctl_event_fields", default={})


def logging_enabled() -> bool:
    return _channel.get() is not None


@contextmanager
def event_logging(channel):
    token = _channel.set(channel)
    try:
        yield
    finally:
        _channel.reset(token)


@contextmanager
def log_context(**fields):
    token = _fields.set({**_fields.get(), **fields})
    try:
        yield
    finally:
        _fields.reset(token)


def record_event(stage: str, *, event: str = "returned", level=LogLevel.INFO, **fields) -> None:
    channel = _channel.get()
    if channel is None:
        return
    try:
        payload = {**_fields.get(), **fields, "stage": stage, "event": event}
        message = encode_record(level, payload)
        channel.slog(LogRecord(level, message, structured=True))
    except Exception:
        # 辅助记录不改变业务结果，不等待空位或重送。
        return


@contextmanager
def measure_stage(stage: str, *, level: LogLevel | None = LogLevel.INFO, **fields):
    """记录实际执行体的起止；返回不表示设备或业务已经成功。"""
    with log_context(**fields):
        if level is None or not logging_enabled():
            yield
            return
        began = monotonic_ns()
        record_event(stage, event="started", level=level)
        try:
            yield
        except BaseException as error:
            event = "cancelled" if isinstance(error, asyncio.CancelledError) else "raised"
            record_event(stage, event=event, level=level,
                elapsed_ms=round((monotonic_ns() - began) / 1_000_000, 3),
                exception_type=type(error).__name__)
            raise
        else:
            record_event(stage, event="returned", level=level,
                elapsed_ms=round((monotonic_ns() - began) / 1_000_000, 3))
