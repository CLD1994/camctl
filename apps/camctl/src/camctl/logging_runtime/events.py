"""关键阶段的非阻塞记录及任务间可靠身份上下文。"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic_ns

from camctl.logging_runtime.formatting import encode_record, utc_now
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

_channel = ContextVar("camctl_event_channel", default=None)
_fields = ContextVar("camctl_event_fields", default={})
_fallback = ContextVar("camctl_event_fallback", default=None)


@dataclass(frozen=True)
class LoggingContext:
    channel: object
    fields: dict
    fallback: object


def current_logging_context() -> LoggingContext:
    """取得调用所在任务的绑定；字段字典由调用方独立持有。"""
    return LoggingContext(_channel.get(), dict(_fields.get()), _fallback.get())


def logging_enabled() -> bool:
    return _channel.get() is not None


@contextmanager
def event_logging(channel, *, fallback=None):
    token = _channel.set(channel)
    fallback_token = _fallback.set(fallback)
    try:
        yield
    finally:
        _fallback.reset(fallback_token)
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
    with log_context(stage=stage, **fields):
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


@contextmanager
def monitor_slow_stage(stage: str, *, threshold_s=1.0, progress=None, **fields):
    """只记录超过阈值的等待；长阶段进行中即可观察，保留实际开始时间。"""
    if not logging_enabled():
        yield
        return
    loop = asyncio.get_running_loop()
    began = monotonic_ns()
    started_at = utc_now()
    reported = False

    def details():
        try:
            return {} if progress is None else progress()
        except Exception:
            return {}

    def started():
        nonlocal reported
        reported = True
        record_event(stage, event="started", **{
            **fields, **details(), "started_at_utc": started_at,
            "elapsed_ms": round((monotonic_ns() - began) / 1_000_000, 3)})

    timer = loop.call_later(threshold_s, started)
    error = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        timer.cancel()
        elapsed_ms = round((monotonic_ns() - began) / 1_000_000, 3)
        if not reported and elapsed_ms >= threshold_s * 1000:
            started()
        if reported:
            event = ("returned" if error is None else
                     "cancelled" if isinstance(error, asyncio.CancelledError) else "raised")
            extra = {} if error is None else {"exception_type": type(error).__name__}
            record_event(stage, event=event, **{
                **fields, **details(), **extra, "started_at_utc": started_at,
                "elapsed_ms": elapsed_ms})
