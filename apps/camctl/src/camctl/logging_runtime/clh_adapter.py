"""多进程日志文件写入适配（concurrent-log-handler）。

复用库的文件锁、轮换与追加：轮换失败被记录但不阻断追加（追加
结果独立表达）；追加失败经 handleError 分类捕获，不被库的默认
吞错掩盖。写入只在日志线程执行。
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

__all__ = ["ChannelWriteError", "FileChannel", "WriteResult"]

_LEVEL_NAMES = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


@dataclass(frozen=True)
class WriteResult:
    appended: bool
    rotated: bool
    rotation_error: str | None = None
    append_error: str | None = None


class ChannelWriteError(Exception):
    """追加失败：通道应禁用，不能把记录标为写入成功。"""


class _TrackingHandler(ConcurrentRotatingFileHandler):
    """分类记录轮换与追加内部错误的通道处理器。"""

    rotation_error: str | None = None
    append_error: str | None = None

    def doRollover(self) -> None:  # noqa: D102 - 覆盖以分类错误
        try:
            super().doRollover()
        except Exception as error:
            # 轮换失败不掩盖追加：记录错误并继续以当前文件追加。
            self.rotation_error = str(error)

    def handleError(self, record: logging.LogRecord | None = None) -> None:
        import traceback

        self.append_error = "".join(
            traceback.format_exception_only(sys.exc_info()[0], sys.exc_info()[1])
        ).strip() or "写入失败"


class FileChannel:
    """CLH 文件通道：线程串行写入、按大小轮换、保留数量含活动文件。"""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        file_count: int,
        level: LogLevel = LogLevel.DEBUG,
    ) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._handler = _TrackingHandler(
            filename=str(path),
            maxBytes=max_bytes,
            backupCount=max(0, file_count - 1),
        )
        self._handler.setLevel(logging.DEBUG)

    def write_record(self, record: LogRecord) -> WriteResult:
        """写入一条记录；只由日志线程调用。"""
        with self._lock:
            self._handler.rotation_error = None
            self._handler.append_error = None
            entry = logging.LogRecord(
                name="camctl",
                level=_LEVEL_NAMES[record.level],
                pathname=__file__,
                lineno=0,
                msg=record.message.rstrip("\n"),
                args=(),
                exc_info=None,
            )
            try:
                self._handler.emit(entry)
            except Exception as error:  # pragma: no cover - emit 内部已捕获
                return WriteResult(
                    appended=False, rotated=True, append_error=str(error)
                )
            self._handler.flush()
            rotation_error = self._handler.rotation_error
            append_error = self._handler.append_error
            return WriteResult(
                appended=append_error is None,
                rotated=True,
                rotation_error=rotation_error,
                append_error=append_error,
            )

    def close(self) -> None:
        with self._lock:
            self._handler.close()
