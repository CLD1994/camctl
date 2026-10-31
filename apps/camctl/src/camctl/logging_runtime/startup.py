"""在命令日志上下文中交付启动事实。"""

import sys

from camctl.logging_runtime.events import current_logging_context
from camctl.logging_runtime.formatting import encode_record
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.version import installed_version


def _fallback(context, message):
    try:
        if context.fallback is not None:
            context.fallback(message)
        else:
            sys.stderr.write(message.rstrip("\n") + "\n")
            sys.stderr.flush()
    except Exception:
        pass  # 诊断通道失效不改变业务结果。


def record_startup():
    context = current_logging_context()
    try:
        version = installed_version()
    except Exception as error:
        version = None
        try:
            diagnostic = encode_record(LogLevel.ERROR, {
                **context.fields, "stage": "command", "event": "version_failed",
                "message": "无法读取 camctl 发行版本", "exception": exception_diagnostic(error),
            })
            if context.channel is None:
                _fallback(context, diagnostic)
            else:
                context.channel.slog(LogRecord(LogLevel.ERROR, diagnostic,
                                               fallback=context.fallback, structured=True))
        except Exception:
            # 诊断自身不可用时仍尝试交付原错误事实。
            try:
                detail = repr(error)
            except Exception:
                detail = type(error).__name__
            _fallback(context, f"无法读取 camctl 发行版本: {detail}")
    message = None
    try:
        message = encode_record(LogLevel.INFO, {
            **context.fields, "stage": "command", "event": "started", "version": version,
        })
        record = LogRecord(LogLevel.INFO, message, fallback=context.fallback,
                           structured=True, startup=True)
        if context.channel is None:
            _fallback(context, message)
        else:
            context.channel.submit(record)
    except Exception:
        # 启动辅助记录不能阻挡业务及必要收场。
        if message is not None:
            _fallback(context, message)
