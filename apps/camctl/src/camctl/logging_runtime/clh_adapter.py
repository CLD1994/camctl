"""多进程日志文件写入适配（concurrent-log-handler）。

复用库的文件锁、轮换与追加：轮换失败被记录但不阻断追加（追加
结果独立表达）；追加失败经 handleError 分类捕获，不被库的默认
吞错掩盖。写入只在日志线程执行。
"""

from __future__ import annotations

import logging
import os
import shutil
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


def _describe_copy_failure(failure: BaseException) -> str:
    return f"copy_failed: {type(failure).__name__}: {failure}"


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
            entry = self._build_entry(record)
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

    def copy_with_write(self, record: LogRecord,
                        destination: Path) -> tuple[WriteResult, str | None]:
        """跨进程锁内追加记录并复制当前日志文件到目标。

        复刻库 emit 的锁内次序（轮换检查、写入），随后在同一锁内
        复制当前文件全部字节；其他进程在此期间既不能轮换也不能追
        加，副本因此必然包含触发记录。只由日志线程调用。
        """
        import shutil

        with self._lock:
            handler = self._handler
            handler.rotation_error = None
            handler.append_error = None
            entry = self._build_entry(record)
            try:
                message = handler.format(entry)
            except Exception as error:
                return (WriteResult(appended=False, rotated=True,
                                    append_error=f"format_failed: {error}"),
                        None)
            try:
                handler._do_lock()
            except Exception as error:
                return (WriteResult(appended=False, rotated=True,
                                    append_error=f"lock_failed: {error}"),
                        None)
            copy_error: str | None = None
            try:
                try:
                    handler._check_stream()
                    try:
                        if handler.shouldRollover(entry):
                            handler.doRollover()
                    except Exception as error:
                        handler.rotation_error = str(error)
                    handler.do_write(message)
                    handler.flush()
                except Exception as error:
                    handler.append_error = str(error)
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with open(self._path, "rb") as source:
                        with open(destination, "wb") as target:
                            shutil.copyfileobj(source, target)
                            target.flush()
                            os.fsync(target.fileno())
                except OSError as error:
                    copy_error = _describe_copy_failure(error)
            finally:
                try:
                    handler._do_unlock()
                except Exception:
                    pass
            return (WriteResult(
                appended=handler.append_error is None,
                rotated=True,
                rotation_error=handler.rotation_error,
                append_error=handler.append_error,
            ), copy_error)

    def _build_entry(self, record: LogRecord) -> logging.LogRecord:
        return logging.LogRecord(
            name="camctl",
            level=_LEVEL_NAMES[record.level],
            pathname=__file__,
            lineno=0,
            msg=record.message.rstrip("\n"),
            args=(),
            exc_info=None,
        )

    def close(self) -> None:
        with self._lock:
            self._handler.close()
