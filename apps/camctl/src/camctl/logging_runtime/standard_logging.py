"""命令期间的第三方标准 logging 路由与完整诊断准备。"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable

from camctl.logging_runtime.admission import LogSource
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord
from camctl.operations.error_diagnostics import exception_diagnostic

__all__ = ["StandardLoggingInstallation", "install_standard_logging", "uninstall_standard_logging"]


def _is_own(name: str) -> bool:
    return name == "camctl" or name.startswith("camctl.")


class _Fallback:
    def __init__(self, callback):
        self.callback = callback
        self.failed = False
        self.local = threading.local()

    def __call__(self, message):
        if self.failed or getattr(self.local, "active", False):
            return False
        self.local.active = True
        try:
            written = self.callback(message) is not False
            if not written:
                self.failed = True
            return written
        except Exception:
            self.failed = True
            return False
        finally:
            self.local.active = False


def _with_exception_and_stack(record: logging.LogRecord, message: str, *, fallback=False) -> str:
    if record.exc_info is not None and record.exc_info[1] is not None:
        try:
            encoded = json.dumps(exception_diagnostic(record.exc_info[1]),
                                 ensure_ascii=False, separators=(",", ":"))
        except Exception as format_error:
            if not fallback:
                raise
            # 消息无法格式化时仍沿共同编码器保存整个原异常图。
            facts = exception_diagnostic(record.exc_info[1], include_message=False)
            try:
                encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                encoded = repr(facts)
            encoded += (f"\n异常格式化错误: {type(format_error).__name__} "
                        f"args={format_error.args!r}")
        message += "\n异常诊断: " + encoded
    elif record.exc_text:
        message += "\n" + record.exc_text
    if record.stack_info:
        message += "\n" + record.stack_info
    return message


def _prepare(record: logging.LogRecord, fallback) -> LogRecord:
    """从原记录取得完整异常图，不修改 msg、args 或 exc_info。"""
    level = (LogLevel.ERROR if record.levelno >= logging.ERROR else
             LogLevel.WARNING if record.levelno >= logging.WARNING else
             LogLevel.INFO if record.levelno >= logging.INFO else LogLevel.DEBUG)
    message = (f"[{record.levelname}/{record.levelno}] {record.name} "
               f"{record.pathname}:{record.lineno} {record.getMessage()}")
    message = _with_exception_and_stack(record, message)
    return LogRecord(level, message, LogSource.SYNC, fallback=fallback)


class _StandardHandler(logging.Handler):
    def __init__(self, channel, fallback):
        super().__init__(logging.DEBUG)
        self.channel, self.fallback = channel, fallback
        self.local = threading.local()

    def emit(self, record):
        if _is_own(record.name) or getattr(self.local, "active", False):
            return
        self.local.active = True
        prepared = None
        try:
            prepared = _prepare(record, self.fallback)
            # 同步满载与后续消费失败由同一记录携带的 fallback 负责。
            self.channel.slog(prepared)
        except Exception as error:
            if record.levelno >= logging.ERROR:
                if prepared is not None:
                    message = prepared.message
                else:
                    message = _with_exception_and_stack(
                        record, f"[{record.levelname}/{record.levelno}] {record.name} "
                        f"{record.pathname}:{record.lineno} msg={record.msg!r} args={record.args!r}",
                        fallback=True)
                diagnostic = LogRecord(
                    LogLevel.ERROR, message + f"\n日志接纳错误: {type(error).__name__}: {error}")
                self.fallback(diagnostic.rendered_message())
        finally:
            self.local.active = False


@dataclass
class _LoggerState:
    logger: logging.Logger
    handlers: list[logging.Handler]
    filters: list[logging.Filter]
    level: int
    propagate: bool
    disabled: bool

    @classmethod
    def capture(cls, logger):
        return cls(logger, list(logger.handlers), list(logger.filters),
                   logger.level, logger.propagate, logger.disabled)

    def restore(self):
        self.logger.handlers = self.handlers
        self.logger.filters = self.filters
        self.logger.setLevel(self.level)
        self.logger.propagate = self.propagate
        self.logger.disabled = self.disabled


class StandardLoggingInstallation:
    """持有安装前日志配置；close 幂等恢复且不关闭原处理器。"""

    def __init__(self, manager, states, disabled, handler):
        self._manager, self._states = manager, states
        self._disabled, self.handler = disabled, handler
        self._closed = False

    def close(self) -> None:
        logging._acquireLock()
        try:
            if self._closed:
                return
            self._closed = True
            for state in reversed(self._states):
                state.restore()
            self._manager.disable = self._disabled
            self._manager._clear_cache()
        finally:
            logging._releaseLock()
        self.handler.close()


def install_standard_logging(channel: LogChannel, fallback: Callable[[str], bool | None], *,
                             manager: logging.Manager | None = None) -> StandardLoggingInstallation:
    """将现有第三方 Logger 统一接入一个 root Handler，结束后恢复。"""
    manager = manager if manager is not None else logging.root.manager
    handler = _StandardHandler(channel, _Fallback(fallback))
    logging._acquireLock()
    try:
        own = manager.getLogger("camctl")
        loggers = [manager.root, *(logger for logger in list(manager.loggerDict.values())
                                  if isinstance(logger, logging.Logger) and not _is_own(logger.name)), own]
        states = [_LoggerState.capture(logger) for logger in loggers]
        disabled = manager.disable
        manager.disable = logging.NOTSET
        for logger in loggers:
            logger.handlers = []
            logger.filters = []
            logger.setLevel(logging.DEBUG if logger is manager.root else logging.NOTSET)
            logger.propagate = logger is not manager.root
            logger.disabled = False
        own.propagate = False
        own.addHandler(logging.NullHandler())
        manager.root.addHandler(handler)
        manager._clear_cache()
    finally:
        logging._releaseLock()
    return StandardLoggingInstallation(manager, states, disabled, handler)


def uninstall_standard_logging(installation: StandardLoggingInstallation) -> None:
    installation.close()
