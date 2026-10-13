"""多进程日志文件写入适配（concurrent-log-handler）。

复用库的文件锁、轮换与追加：轮换失败被记录但不阻断追加（追加
结果独立表达）；追加失败经 handleError 分类捕获，不被库的默认
吞错掩盖。写入只在日志线程执行。
"""

from __future__ import annotations

import logging
import enum
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

__all__ = [
    "ChannelWriteError", "FileChannel", "WriteFailureStage", "WriteResult",
]

_LEVEL_NAMES = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


class WriteFailureStage(str, enum.Enum):
    """记录写入实际失败的操作边界。"""

    FORMAT = "format"
    LOCK_ACQUIRE = "lock_acquire"
    FILE_IO = "file_io"
    LOCK_RELEASE = "lock_release"


@dataclass(frozen=True)
class WriteResult:
    appended: bool
    rotated: bool
    rotation_error: str | None = None
    append_error: str | None = None
    coordination_error: str | None = None
    close_error: str | None = None
    failure_stage: WriteFailureStage | None = None

    @property
    def disables_file_logging(self) -> bool:
        """本结果是否要求停用本进程后续文件日志。"""
        if self.failure_stage is WriteFailureStage.FORMAT:
            return False
        return (self.failure_stage is not None
                or self.coordination_error is not None
                or self.append_error is not None)


class ChannelWriteError(Exception):
    """追加失败：通道应禁用，不能把记录标为写入成功。"""


class _TrackingHandler(ConcurrentRotatingFileHandler):
    """分类记录轮换与追加内部错误的通道处理器。"""

    rotation_error: str | None = None
    append_error: str | None = None
    coordination_error: str | None = None
    failure_stage: WriteFailureStage | None = None

    def reset_failures(self) -> None:
        self.rotation_error = None
        self.append_error = None
        self.coordination_error = None
        self.failure_stage = None

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except Exception as error:
            self.failure_stage = WriteFailureStage.FORMAT
            self.append_error = f"format_failed: {type(error).__name__}: {error}"
            raise

    def _do_lock(self) -> None:
        try:
            super()._do_lock()
        except Exception as error:
            self._record_coordination_failure(
                WriteFailureStage.LOCK_ACQUIRE, error)
            raise

    def _do_unlock(self) -> None:
        try:
            super()._do_unlock()
        except Exception as error:
            self._record_coordination_failure(
                WriteFailureStage.LOCK_RELEASE, error)
            raise

    def _check_stream(self) -> None:
        try:
            super()._check_stream()
        except Exception as error:
            self._record_file_failure(error)
            raise

    def do_write(self, message: str) -> None:
        # CLH 捕获轮换检查错误后仍会调用 do_write；若该检查已确认
        # 当前日志打开失败，禁止在同一记录中再次尝试打开。
        if (self.failure_stage is WriteFailureStage.FILE_IO
                and self.append_error is not None):
            raise OSError(self.append_error)
        try:
            super().do_write(message)
        except Exception as error:
            self._record_file_failure(error)
            raise

    def do_open(self, mode: str | None = None):
        try:
            return super().do_open(mode)
        except Exception as error:
            self._record_file_failure(error)
            raise

    def flush(self) -> None:
        try:
            super().flush()
        except Exception as error:
            self._record_file_failure(error)
            raise

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        try:
            return super().shouldRollover(record)
        except Exception as error:
            # 实际文件打开失败由 do_open 分类，不能降格为轮换错误。
            if self.failure_stage is WriteFailureStage.FILE_IO:
                raise
            # 其他轮换判断失败仍允许当前文件追加。
            self.rotation_error = f"{type(error).__name__}: {error}"
            return False

    def _record_file_failure(self, error: BaseException) -> None:
        if self.failure_stage is None:
            self.failure_stage = WriteFailureStage.FILE_IO
        self.append_error = self.append_error or (
            f"{type(error).__name__}: {error}")

    def _record_coordination_failure(
        self, stage: WriteFailureStage, error: BaseException,
    ) -> None:
        if self.failure_stage is None:
            self.failure_stage = stage
        operation = (
            "coordination_lock_failed"
            if stage is WriteFailureStage.LOCK_ACQUIRE
            else "coordination_unlock_failed"
        )
        detail = f"{operation}: {type(error).__name__}: {error}"
        if self.coordination_error is None:
            self.coordination_error = detail
        elif detail not in self.coordination_error:
            self.coordination_error += f"; {detail}"

    def doRollover(self) -> None:  # noqa: D102 - 覆盖以分类错误
        try:
            super().doRollover()
        except Exception as error:
            # 轮换失败不掩盖追加：记录错误并继续以当前文件追加。
            self.rotation_error = str(error)

    def handleError(self, record: logging.LogRecord | None = None) -> None:
        import traceback

        detail = "".join(
            traceback.format_exception_only(sys.exc_info()[0], sys.exc_info()[1])
        ).strip() or "写入失败"
        if self.failure_stage in (WriteFailureStage.LOCK_ACQUIRE,
                                  WriteFailureStage.LOCK_RELEASE):
            if self.coordination_error is None:
                self.coordination_error = detail
        else:
            if self.failure_stage is None:
                self.failure_stage = WriteFailureStage.FILE_IO
            self.append_error = self.append_error or detail


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
        self._disabled = False
        self._closed = False
        self._close_attempted = False

    def write_record(self, record: LogRecord) -> WriteResult:
        """写入一条记录；只由日志线程调用。"""
        with self._lock:
            if self._disabled:
                return WriteResult(
                    appended=False, rotated=False,
                    append_error="file_channel_disabled",
                )
            self._handler.reset_failures()
            entry = self._build_entry(record)
            try:
                self._handler.emit(entry)
            except Exception as error:  # pragma: no cover - emit 内部已捕获
                self._handler._record_file_failure(error)
            handler = self._handler
            failure_stage = handler.failure_stage
            if failure_stage is not None and failure_stage is not WriteFailureStage.FORMAT:
                close_kind = (
                    "coordination" if handler.coordination_error is not None
                    else "file_handler")
                close_error = self._disable_and_close(close_kind)
            else:
                close_error = None
            return WriteResult(
                appended=(handler.append_error is None and failure_stage not in (
                    WriteFailureStage.FORMAT,
                    WriteFailureStage.LOCK_ACQUIRE,
                    WriteFailureStage.FILE_IO,
                )),
                rotated=failure_stage is not WriteFailureStage.FORMAT,
                rotation_error=handler.rotation_error,
                append_error=handler.append_error,
                coordination_error=handler.coordination_error,
                close_error=close_error,
                failure_stage=failure_stage,
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
            if self._disabled:
                return (WriteResult(
                    appended=False, rotated=False,
                    append_error="file_channel_disabled",
                ), None)
            handler = self._handler
            handler.reset_failures()
            entry = self._build_entry(record)
            try:
                message = handler.format(entry)
            except Exception as error:
                handler.failure_stage = WriteFailureStage.FORMAT
                handler.append_error = handler.append_error or (
                    f"format_failed: {type(error).__name__}: {error}")
                return (WriteResult(appended=False, rotated=False,
                                    append_error=handler.append_error,
                                    failure_stage=WriteFailureStage.FORMAT),
                        None)
            try:
                handler._do_lock()
            except Exception as error:
                handler._record_coordination_failure(
                    WriteFailureStage.LOCK_ACQUIRE, error)
                close_error = self._disable_and_close("coordination")
                return (WriteResult(
                    appended=False,
                    rotated=False,
                    coordination_error=handler.coordination_error,
                    close_error=close_error,
                    failure_stage=WriteFailureStage.LOCK_ACQUIRE,
                ), None)
            copy_error: str | None = None
            coordination_error: str | None = None
            close_error: str | None = None
            file_io_failed = False
            try:
                try:
                    handler._check_stream()
                    try:
                        if handler.shouldRollover(entry):
                            handler.doRollover()
                    except Exception as error:
                        if handler.failure_stage is WriteFailureStage.FILE_IO:
                            raise
                        handler.rotation_error = f"{type(error).__name__}: {error}"
                    handler.do_write(message)
                    handler.flush()
                except Exception as error:
                    file_io_failed = True
                    handler._record_file_failure(error)
                if not file_io_failed:
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
                except Exception as error:
                    handler._record_coordination_failure(
                        WriteFailureStage.LOCK_RELEASE, error)
                    coordination_error = handler.coordination_error
                    # 协调释放失败后不重试锁；保留既有追加和复制事实，
                    # 禁用辅助通道并只尝试一次关闭。
                    close_error = self._disable_and_close(
                        "file_handler" if file_io_failed else "coordination")
            if file_io_failed and not self._disabled:
                close_error = self._disable_and_close("file_handler")
            return (WriteResult(
                appended=not file_io_failed and handler.append_error is None,
                rotated=True,
                rotation_error=handler.rotation_error,
                append_error=handler.append_error,
                coordination_error=coordination_error,
                close_error=close_error,
                failure_stage=handler.failure_stage,
            ), copy_error)

    def _disable_and_close(self, close_kind: str) -> str | None:
        """失效后禁用通道并只尝试一次关闭；调用者持有 ``_lock``。"""
        if self._close_attempted:
            self._disabled = True
            return None
        self._disabled = True
        self._close_attempted = True
        try:
            self._handler.close()
        except Exception as error:
            return (
                f"{close_kind}_close_failed: "
                f"{type(error).__name__}: {error}"
            )
        self._closed = True
        return None

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
            if self._close_attempted:
                return
            self._disabled = True
            self._close_attempted = True
            self._handler.close()
            self._closed = True

    @property
    def disabled(self) -> bool:
        """通道是否不可复用；包含关闭结果未知的情况。"""
        with self._lock:
            return self._disabled

    @property
    def closed(self) -> bool:
        """关闭调用是否正常返回；异常后资源状态保持未知。"""
        with self._lock:
            return self._closed
