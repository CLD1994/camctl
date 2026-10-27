"""通用日志入口：调用时冻结并提交，回执可等待实际写入。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
import sys
from types import TracebackType

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind, LogSource
from camctl.logging_runtime.events import current_logging_context
from camctl.logging_runtime.formatting import log_metadata
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult, LogRecord
from camctl.operations.error_diagnostics import exception_diagnostic

__all__ = ["Logger", "SyncLogger", "get_logger", "get_sync_logger"]

_LEVELS = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARNING,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.ERROR,
}
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "level", "logged_at_utc", "pid", "logger", "message", "asctime", "function",
    "exception", "stack_info",
}


def _level_number(level) -> int:
    if isinstance(level, LogLevel):
        return getattr(logging, level.name)
    if type(level) is int and level in _LEVELS:
        return level
    raise ValueError("日志级别必须为 LogLevel 或 DEBUG、INFO、WARNING、ERROR、CRITICAL 数值")


def _exception_info(exc_info):
    if isinstance(exc_info, BaseException):
        return type(exc_info), exc_info, exc_info.__traceback__
    if exc_info is True:
        return sys.exc_info()
    if exc_info is False or exc_info is None:
        return None
    if (isinstance(exc_info, tuple) and len(exc_info) == 3
            and (exc_info[0] is None or isinstance(exc_info[0], type)
                 and issubclass(exc_info[0], BaseException))
            and (exc_info[1] is None or isinstance(exc_info[1], BaseException))
            and (exc_info[2] is None or isinstance(exc_info[2], TracebackType))):
        return exc_info
    raise TypeError("exc_info 必须为布尔值、异常对象或异常三元组")


def _caller(stacklevel, stack_info):
    if type(stacklevel) is not int or stacklevel < 1:
        raise ValueError("stacklevel 必须为大于等于 1 的整数")
    frame = sys._getframe(1)
    try:
        while frame is not None:
            if frame.f_globals.get("__name__") != __name__:
                stacklevel -= 1
                if stacklevel == 0 or frame.f_back is None:
                    break
            frame = frame.f_back
        if frame is None:
            return "(unknown file)", 0, "(unknown function)", None
        pathname, lineno, function = frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name
        stack = None
        if stack_info:
            stack = []
            current = frame
            try:
                while current is not None:
                    stack.append({"filename": current.f_code.co_filename,
                                  "lineno": current.f_lineno, "function": current.f_code.co_name})
                    current = current.f_back
            finally:
                del current
            stack.reverse()
        return pathname, lineno, function, stack
    finally:
        del frame


def _safe_repr(value):
    try:
        return repr(value)
    except Exception:
        kind = type(value)
        return f"<{kind.__module__}.{kind.__qualname__}>"


def _format_error(error):
    from camctl.logging_runtime.diagnostics import diagnostic_snapshot
    try:
        return diagnostic_snapshot(error, structured=False)
    except Exception:
        return {"type": f"{type(error).__module__}.{type(error).__qualname__}"}


def _encode(document):
    try:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    except Exception as error:
        # 不支持的附加对象仍保留完整 repr；其他字段继续保持 JSON 结构。
        try:
            document = {**document, "diagnostic_format_error": _format_error(error)}
            return json.dumps(document, ensure_ascii=False, separators=(",", ":"), default=_safe_repr)
        except Exception:
            # 循环容器等不能编码时，只降级该字段，不丢弃其余可编码事实。
            safe = {}
            for key, value in document.items():
                try:
                    safe[key] = json.loads(json.dumps(value, ensure_ascii=False, default=_safe_repr))
                except Exception:
                    safe[key] = _safe_repr(value)
            return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))


def _snapshot(document):
    # 统一保留错误字节及已知操作结构；媒体 stdout 只保留字节数。
    from camctl.logging_runtime.diagnostics import diagnostic_snapshot
    try:
        return diagnostic_snapshot(document)
    except Exception as error:
        try:
            snapshot = diagnostic_snapshot(document, structured=False)
        except Exception:
            snapshot = {}
            for name, value in document.items():
                try:
                    snapshot.update(diagnostic_snapshot({name: value}, structured=False))
                except Exception as field_error:
                    # 不能安全解释的字段只保留类型和实际编码错误，不对原操作作 repr。
                    kind = type(value)
                    snapshot[name] = {"type": f"{kind.__module__}.{kind.__qualname__}",
                                      "diagnostic_format_error": _format_error(field_error)}
        return {**snapshot, "diagnostic_format_error": _format_error(error)}


def _prepare(name, number, message, args, *, context, extra, exc_info, stack_info,
             stacklevel, source):
    if extra is not None and not isinstance(extra, Mapping):
        raise TypeError("extra 必须为映射")
    if extra is not None:
        collision = _RESERVED.intersection(extra)
        if collision:
            raise KeyError(f"extra 不能覆盖日志字段: {sorted(collision)!r}")
    information = _exception_info(exc_info)
    pathname, lineno, function, stack = _caller(stacklevel, stack_info)
    fields = {**context.fields, **(extra or {})}
    fields.setdefault("stage", "runtime")
    fields.update(logger=name, pathname=pathname, lineno=lineno, function=function)
    try:
        original = logging.LogRecord(name, number, pathname, lineno, message, args,
                                     information, func=function)
        fields["message"] = original.getMessage()
    except Exception as error:
        fields.update(message=message if isinstance(message, str) else
                      f"<{type(message).__module__}.{type(message).__qualname__}>",
                      message_arguments=args, message_format_error=_format_error(error))
        if not isinstance(message, str):
            fields["message_value"] = message
    error = information[1] if information is not None else None
    if error is not None:
        try:
            fields["exception"] = exception_diagnostic(error)
        except Exception as format_error:
            try:
                fields["exception"] = exception_diagnostic(error, include_message=False)
            except Exception:
                fields["exception"] = _snapshot({"exception": error})["exception"]
            fields["exception_format_error"] = _format_error(format_error)
        # 显式三元组可以指定不同于异常当前 traceback 的原栈。
        fields["exception"]["traceback"] = [
            {"filename": frame.f_code.co_filename, "lineno": line, "function": frame.f_code.co_name}
            for frame, line in _walk_traceback(information[2])]
    if stack is not None:
        fields["stack_info"] = stack
    metadata = log_metadata(_LEVELS[number])
    metadata["level"] = logging.getLevelName(number)
    record = LogRecord(_LEVELS[number], _encode(_snapshot({**fields, **metadata})), source=source,
                       fallback=context.fallback, structured=True)
    return record, error


def _walk_traceback(traceback):
    while traceback is not None:
        yield traceback.tb_frame, traceback.tb_lineno
        traceback = traceback.tb_next


def _submit_low(channel, record) -> LogReceipt:
    if channel is not None:
        try:
            return channel.submit(record)
        except Exception:
            # 低级别辅助日志不改变所属业务结果。
            pass
    receipt = LogReceipt()
    result = AdmissionResult(AdmissionDecision(AdmissionKind.CHANNEL_FAILED, record.level),
                             None, delivered=False)
    receipt.complete_admission(result)
    receipt.complete_write(result)
    return receipt


@dataclass(frozen=True, slots=True)
class Logger:
    """仅持有名称；每次调用使用调用任务的日志上下文。"""

    name: str

    def debug(self, message, *args, **kwargs):
        return self.log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        return self.log(logging.INFO, message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        return self.log(logging.WARNING, message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        return self.log(logging.ERROR, message, *args, **kwargs)

    def critical(self, message, *args, **kwargs):
        return self.log(logging.CRITICAL, message, *args, **kwargs)

    def exception(self, message, *args, exc_info=True, **kwargs):
        return self.log(logging.ERROR, message, *args, exc_info=exc_info, **kwargs)

    def log(self, level, message, *args, extra=None, exc_info=False, stack_info=False,
            stacklevel=1, state=None) -> LogReceipt:
        number = _level_number(level)
        if number < logging.ERROR and state is not None:
            raise ValueError("低于 ERROR 的日志不能持有诊断状态")
        context = current_logging_context()
        record, error = _prepare(self.name, number, message, args, context=context,
            extra=extra, exc_info=exc_info, stack_info=stack_info,
            stacklevel=stacklevel, source=LogSource.COROUTINE)
        if number < logging.ERROR:
            return _submit_low(context.channel, record)
        # 包初始化及普通操作模块的导入不加载诊断组件，避免形成导入环。
        from camctl.logging_runtime.diagnostics import prepare_log_diagnostic, submit_diagnostic
        prepared = prepare_log_diagnostic(record, error=error, state=state,
            channel=context.channel, fallback=context.fallback)
        return submit_diagnostic(prepared)


@dataclass(frozen=True, slots=True)
class SyncLogger:
    """同步代码仅投递低于 ERROR 的辅助日志。"""

    name: str

    def debug(self, message, *args, **kwargs):
        return self.log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        return self.log(logging.INFO, message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        return self.log(logging.WARNING, message, *args, **kwargs)

    def log(self, level, message, *args, extra=None, exc_info=False, stack_info=False,
            stacklevel=1, state=None) -> LogReceipt:
        number = _level_number(level)
        if number >= logging.ERROR:
            raise ValueError("同步日志入口不能投递 ERROR 或 CRITICAL")
        if state is not None:
            raise ValueError("同步日志入口不能持有诊断状态")
        context = current_logging_context()
        record, _ = _prepare(self.name, number, message, args, context=context,
            extra=extra, exc_info=exc_info, stack_info=stack_info,
            stacklevel=stacklevel, source=LogSource.SYNC)
        return _submit_low(context.channel, record)


def get_logger(name: str) -> Logger:
    if not isinstance(name, str):
        raise TypeError("日志名称必须为字符串")
    return Logger(name)


def get_sync_logger(name: str) -> SyncLogger:
    if not isinstance(name, str):
        raise TypeError("日志名称必须为字符串")
    return SyncLogger(name)
