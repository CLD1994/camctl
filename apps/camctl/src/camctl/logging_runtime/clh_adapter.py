"""多进程日志文件写入适配（concurrent-log-handler）。

复用库的文件锁、打开与格式化，按级别同步并以正式名字轮换。
追加、同步及副本结果独立表达，文件操作由日志线程或受管副本工作
线程串行执行。
"""

from __future__ import annotations

import logging
import enum
import errno
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from camctl.logging_runtime.models import LogLevel, LogWriteResult
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
    FILE_SYNC = "file_sync"
    DIRECTORY_SYNC = "directory_sync"
    DIRECTORY_CLOSE = "directory_close"
    FILE_CLOSE = "file_close"
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
    copy_handles_closed: bool | None = None
    copy_source_close_error: str | None = None
    copy_target_close_error: str | None = None
    durable: bool = False
    sync_error: str | None = None

    @property
    def completion(self) -> LogWriteResult:
        return LogWriteResult(self.appended, self.durable)

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
    sync_error: str | None = None
    resource_close_error: str | None = None
    appended = False
    durable = False
    important = False

    def reset_failures(self, *, important=False) -> None:
        self.rotation_error = None
        self.append_error = None
        self.coordination_error = None
        self.failure_stage = None
        self.sync_error = None
        self.resource_close_error = None
        self.appended = self.durable = False
        self.important = important
        self._prepared = False
        self._rotation_ready = True
        self._control_error = None
        self._failure_error = None

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except BaseException as error:
            self._record_format_failure(error)
            raise

    def _record_format_failure(self, error):
        self._remember_control(error)
        self.failure_stage = WriteFailureStage.FORMAT
        self.append_error = self.append_error or f"format_failed: {_failure_description(error)}"

    def _do_lock(self) -> None:
        try:
            Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            self._record_file_failure(error)
            raise
        try:
            super()._do_lock()
        except BaseException as error:
            self._record_coordination_failure(
                WriteFailureStage.LOCK_ACQUIRE, error)
            raise

    def _do_unlock(self) -> None:
        try:
            super()._do_unlock()
        except BaseException as error:
            self._record_coordination_failure(
                WriteFailureStage.LOCK_RELEASE, error)
            raise

    def _check_stream(self) -> None:
        if not self._prepared:
            try:
                self._prepare_archives()
            except Exception as error:
                if self.failure_stage in (WriteFailureStage.FILE_IO, WriteFailureStage.FILE_CLOSE,
                                          WriteFailureStage.DIRECTORY_SYNC):
                    raise
                self.rotation_error = _failure_description(error)
                self._rotation_ready = False
            self._prepared = True
        try:
            super()._check_stream()
        except Exception as error:
            self._record_file_failure(error)
            raise

    def do_write(self, message: str) -> None:
        # CLH 捕获轮换检查错误后仍会调用 do_write；若该检查已确认
        # 当前日志打开失败，禁止在同一记录中再次尝试打开。
        if self.failure_stage in (WriteFailureStage.FILE_IO, WriteFailureStage.FILE_SYNC,
                                  WriteFailureStage.DIRECTORY_SYNC, WriteFailureStage.FILE_CLOSE):
            raise OSError(self.append_error or self.sync_error or "日志文件准备失败")
        try:
            if self.stream is None or self.stream.closed:
                self.stream = self.do_open()
            message += self.terminator
            try:
                self.stream.write(message)
            except UnicodeError:
                encoding = getattr(self.stream, "encoding", self.encoding or "us-ascii")
                message = message.encode(encoding, self.unicode_error_policy).decode(encoding)
                self.stream.write(message)
            self.flush()
            self.appended = True
            if self.important:
                try:
                    os.fsync(self.stream.fileno())
                except BaseException as error:
                    self._record_sync_failure(WriteFailureStage.FILE_SYNC, error)
                    raise
                self._sync_directory_chain()
                self.durable = True
            if not self._actual_keep_log_stream_open:
                try:
                    self.stream.close()
                except BaseException as error:
                    self._record_close_failure(WriteFailureStage.FILE_CLOSE, error)
                    raise
                finally:
                    self.stream = None
            if self._control_error is not None:
                raise self._control_error
        except BaseException as error:
            self._remember_control(error)
            if self.failure_stage not in (WriteFailureStage.FILE_SYNC,
                                          WriteFailureStage.DIRECTORY_SYNC,
                                          WriteFailureStage.FILE_CLOSE) and not (
                    self.failure_stage is WriteFailureStage.DIRECTORY_CLOSE
                    and error is self._control_error):
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
        if not self._rotation_ready:
            return False
        try:
            return super().shouldRollover(record)
        except Exception as error:
            # 实际文件打开失败由 do_open 分类，不能降格为轮换错误。
            if self.failure_stage is WriteFailureStage.FILE_IO:
                raise
            # 其他轮换判断失败仍允许当前文件追加。
            self.rotation_error = _failure_description(error)
            return False

    def _record_file_failure(self, error: BaseException) -> None:
        self._remember_control(error)
        if self.failure_stage in (WriteFailureStage.FILE_SYNC, WriteFailureStage.DIRECTORY_SYNC,
                                  WriteFailureStage.FILE_CLOSE):
            return
        if self.failure_stage in (None, WriteFailureStage.DIRECTORY_CLOSE, WriteFailureStage.FILE_CLOSE):
            self.failure_stage = WriteFailureStage.FILE_IO
        self.append_error = self.append_error or (
            _failure_description(error))

    def _record_sync_failure(self, stage, error):
        self._remember_control(error)
        self.failure_stage = stage
        self.sync_error = self.sync_error or _failure_description(error)

    def _record_close_failure(self, stage, error):
        self._remember_control(error)
        if self.failure_stage is None:
            self.failure_stage = stage
        self.resource_close_error = self.resource_close_error or _failure_description(error)

    def _remember_control(self, error):
        if self._failure_error is None:
            self._failure_error = error
        if not isinstance(error, Exception) and self._control_error is None:
            self._control_error = error

    def _sync_directory(self, path, *, synced=None):
        if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
            error = OSError(errno.ENOTSUP, "目标环境不支持目录同步")
            self._record_sync_failure(WriteFailureStage.DIRECTORY_SYNC, error)
            raise error
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except BaseException as error:
            self._record_sync_failure(WriteFailureStage.DIRECTORY_SYNC, error)
            raise
        primary = None
        try:
            identity = None
            if synced is not None:
                info = os.fstat(fd)
                identity = info.st_dev, info.st_ino
            if synced is None or identity not in synced:
                os.fsync(fd)
                if synced is not None:
                    synced.add(identity)
        except BaseException as error:
            self._record_sync_failure(WriteFailureStage.DIRECTORY_SYNC, error)
            primary = error
        try:
            os.close(fd)
        except BaseException as error:
            self._record_close_failure(WriteFailureStage.DIRECTORY_CLOSE, error)
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)

    def _sync_directory_chain(self):
        path = Path(self.baseFilename)
        try:
            actual_directory = path.resolve(strict=True).parent
        except BaseException as error:
            self._record_sync_failure(WriteFailureStage.DIRECTORY_SYNC, error)
            raise
        visited, synced = set(), set()
        for directory in (actual_directory, path.parent):
            while directory not in visited:
                self._sync_directory(directory, synced=synced)
                visited.add(directory)
                if directory.parent == directory:
                    break
                directory = directory.parent

    def _move_name(self, source, target):
        os.link(source, target)
        self._sync_directory(Path(self.baseFilename).parent)
        os.unlink(source)
        self._sync_directory(Path(self.baseFilename).parent)

    def _archive_names(self):
        path = Path(self.baseFilename)
        prefix = path.name + "."
        result = []
        for candidate in path.parent.iterdir():
            suffix = candidate.name[len(prefix):] if candidate.name.startswith(prefix) else ""
            if suffix.isascii() and suffix.isdecimal() and int(suffix) > 0 and suffix == str(int(suffix)):
                result.append((int(suffix), candidate))
        return sorted(result)

    def _prepare_archives(self):
        path = Path(self.baseFilename)
        names = ([(0, path)] if path.exists() else []) + self._archive_names()
        identities = {}
        duplicates = []
        for index, name in reversed(names):
            info = name.stat()
            identity = info.st_dev, info.st_ino
            if identity in identities:
                duplicates.append(name)
            else:
                identities[identity] = name
        if duplicates:
            self._sync_directory(path.parent)
            if path in duplicates and self.stream is not None:
                self._close_before_move()
            for name in duplicates:
                os.unlink(name)
                self._sync_directory(path.parent)
        archives = self._archive_names()
        for expected, (index, source) in enumerate(archives, 1):
            if index != expected:
                self._move_name(source, Path(f"{self.baseFilename}.{expected}"))
        archives = self._archive_names()
        for _, name in reversed(archives[self.backupCount:]):
            os.unlink(name)
            self._sync_directory(path.parent)

    def _record_coordination_failure(
        self, stage: WriteFailureStage, error: BaseException,
    ) -> None:
        self._remember_control(error)
        if self.failure_stage is None:
            self.failure_stage = stage
        operation = (
            "coordination_lock_failed"
            if stage is WriteFailureStage.LOCK_ACQUIRE
            else "coordination_unlock_failed"
        )
        detail = f"{operation}: {_failure_description(error)}"
        if self.coordination_error is None:
            self.coordination_error = detail
        elif detail not in self.coordination_error:
            self.coordination_error += f"; {detail}"

    def doRollover(self) -> None:  # noqa: D102 - 覆盖以分类错误
        try:
            if self.stream is not None:
                self._close_before_move()
            if self.backupCount <= 0:
                self.stream = self.do_open("w")
            else:
                archives = self._archive_names()
                for _, name in reversed(archives[max(0, self.backupCount - 1):]):
                    os.unlink(name)
                    self._sync_directory(Path(self.baseFilename).parent)
                for index, source in reversed(self._archive_names()):
                    self._move_name(source, Path(f"{self.baseFilename}.{index + 1}"))
                path = Path(self.baseFilename)
                if path.exists():
                    self._move_name(path, Path(f"{self.baseFilename}.1"))
            self.num_rollovers += 1
        except Exception as error:
            # 轮换失败不掩盖追加：记录错误并继续以当前文件追加。
            self.rotation_error = _failure_description(error)

    def _close_before_move(self):
        try:
            self.stream.close()
        except BaseException as error:
            self._record_close_failure(WriteFailureStage.FILE_CLOSE, error)
            raise
        finally:
            self.stream = None

    def handleError(self, record: logging.LogRecord | None = None) -> None:
        import traceback

        error = sys.exc_info()[1]
        try:
            detail = "".join(traceback.format_exception_only(type(error), error)).strip()
        except BaseException:
            detail = _failure_description(error)
        detail = detail or "写入失败"
        if self.failure_stage in (WriteFailureStage.LOCK_ACQUIRE,
                                  WriteFailureStage.LOCK_RELEASE):
            if self.coordination_error is None:
                self.coordination_error = detail
        elif self.failure_stage not in (WriteFailureStage.FILE_SYNC, WriteFailureStage.DIRECTORY_SYNC,
                                        WriteFailureStage.DIRECTORY_CLOSE, WriteFailureStage.FILE_CLOSE):
            if self.failure_stage is None:
                self.failure_stage = WriteFailureStage.FILE_IO
            self.append_error = self.append_error or detail


def _failure_description(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"
    except BaseException:
        try:
            return type(error).__name__
        except BaseException:
            return "日志错误描述不可用"


def _join_errors(*errors):
    return "; ".join(error for error in errors if error) or None


def _describe_copy_failure(failure: BaseException) -> str:
    return f"copy_failed: {_failure_description(failure)}"


def _copy_close_failure(failure: BaseException) -> str:
    return f"copy_close_failed: {_failure_description(failure)}"


def _attach_write_result(failure: BaseException,
                         result: WriteResult) -> None:
    """把已形成的原日志事实附到同一异常对象，不替换主异常。"""
    try:
        setattr(failure, "write_result", result)
    except BaseException:
        try:
            failure.add_note(
                "camctl_write_result_unavailable: 原日志通道已形成写入结果"
            )
        except BaseException:
            pass


def _add_copy_close_notes(primary: BaseException,
                          close_errors: tuple[BaseException, ...]) -> None:
    for error in close_errors:
        if error is primary:
            continue
        try:
            primary.add_note(_copy_close_failure(error))
        except BaseException:
            return


def _raise_copy_failure(
    primary: BaseException | None,
    close_errors: tuple[BaseException, ...],
    result: WriteResult,
) -> str | None:
    """保留主异常对象；只有 OSError 可按副本失败回执返回。"""
    if primary is None:
        if not close_errors:
            return None
        primary = close_errors[0]
        trailing = close_errors[1:]
        if isinstance(primary, OSError):
            cancellation = next(
                (error for error in trailing
                 if not isinstance(error, Exception)), None)
            if cancellation is not None:
                _add_copy_close_notes(cancellation, tuple(
                    error for error in close_errors if error is not cancellation))
                _attach_write_result(cancellation, result)
                raise cancellation.with_traceback(
                    cancellation.__traceback__) from primary
            if all(isinstance(error, OSError) for error in trailing):
                return None
            _attach_write_result(primary, result)
            group = ExceptionGroup(
                "日志副本句柄关闭均失败",
                [primary, *trailing],
            )
            _attach_write_result(group, result)
            raise group
        _add_copy_close_notes(primary, trailing)
        _attach_write_result(primary, result)
        raise primary.with_traceback(primary.__traceback__)

    if isinstance(primary, OSError):
        if not close_errors or all(
                isinstance(error, OSError) for error in close_errors):
            return _describe_copy_failure(primary)
        cancellation = next(
            (error for error in close_errors if not isinstance(error, Exception)),
            None,
        )
        if cancellation is not None:
            _add_copy_close_notes(cancellation, (primary, *tuple(
                error for error in close_errors if error is not cancellation)))
            _attach_write_result(cancellation, result)
            raise cancellation.with_traceback(cancellation.__traceback__) from primary
        errors = [primary, *close_errors]
        group = ExceptionGroup("日志副本操作与句柄关闭均失败", errors)
        _attach_write_result(group, result)
        raise group

    _add_copy_close_notes(primary, close_errors)
    _attach_write_result(primary, result)
    raise primary.with_traceback(primary.__traceback__)


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
            self._handler.reset_failures(important=record.level is LogLevel.ERROR)
            if self._disabled:
                return self._finish_result(WriteResult(
                    appended=False, rotated=False,
                    append_error="file_channel_disabled",
                ))
            try:
                entry = self._build_entry(record)
            except BaseException as error:
                self._handler._record_format_failure(error)
                return self._finish_result(WriteResult(
                    appended=False, rotated=False, append_error=self._handler.append_error,
                    failure_stage=WriteFailureStage.FORMAT), error)
            primary = None
            try:
                self._handler.emit(entry)
            except BaseException as error:
                self._handler._remember_control(error)
                if isinstance(error, Exception):
                    self._handler._record_file_failure(error)
                else:
                    primary = error
            handler = self._handler
            failure_stage = handler.failure_stage
            if failure_stage is not None and failure_stage is not WriteFailureStage.FORMAT:
                close_kind = (
                    "coordination" if handler.coordination_error is not None
                    else "file_handler")
                close_error = self._disable_and_close(close_kind)
            else:
                close_error = None
            result = WriteResult(
                appended=handler.appended,
                rotated=failure_stage is not WriteFailureStage.FORMAT,
                rotation_error=handler.rotation_error,
                append_error=handler.append_error,
                coordination_error=handler.coordination_error,
                close_error=_join_errors(handler.resource_close_error, close_error),
                failure_stage=failure_stage,
                durable=handler.durable,
                sync_error=handler.sync_error,
            )
            return self._finish_result(result, primary)

    def copy_with_write(self, record: LogRecord,
                        destination: Path) -> tuple[WriteResult, str | None]:
        """跨进程锁内追加记录并复制当前日志文件到目标。

        复刻库 emit 的锁内次序（轮换检查、写入），随后在同一锁内
        复制当前文件全部字节；其他进程在此期间既不能轮换也不能追
        加，副本因此包含触发记录。普通写入与副本工作线程共用本通道锁。
        """
        import shutil

        with self._lock:
            handler = self._handler
            handler.reset_failures(important=record.level is LogLevel.ERROR)
            if self._disabled:
                return (self._finish_result(WriteResult(
                    appended=False, rotated=False,
                    append_error="file_channel_disabled",
                    copy_handles_closed=True,
                )), None)
            try:
                entry = self._build_entry(record)
                message = handler.format(entry)
            except BaseException as error:
                handler._record_format_failure(error)
                return (self._finish_result(WriteResult(
                    appended=False, rotated=False,
                    append_error=handler.append_error,
                    failure_stage=WriteFailureStage.FORMAT,
                    copy_handles_closed=True,
                ), error), None)
            try:
                handler._do_lock()
            except BaseException as error:
                if handler.failure_stage is None:
                    handler._record_coordination_failure(WriteFailureStage.LOCK_ACQUIRE, error)
                else:
                    handler._remember_control(error)
                close_error = self._disable_and_close("coordination")
                return (self._finish_result(WriteResult(
                    appended=False,
                    rotated=False,
                    append_error=handler.append_error,
                    coordination_error=handler.coordination_error,
                    close_error=_join_errors(handler.resource_close_error, close_error),
                    failure_stage=handler.failure_stage,
                    copy_handles_closed=True,
                ), error), None)

            copy_error: str | None = None
            coordination_error: str | None = None
            close_error: str | None = None
            file_io_failed = False
            primary_copy_error: BaseException | None = None
            source = None
            target = None
            source_close_exception: BaseException | None = None
            target_close_exception: BaseException | None = None
            try:
                try:
                    handler._check_stream()
                    try:
                        if handler.shouldRollover(entry):
                            handler.doRollover()
                    except Exception as error:
                        if handler.failure_stage is WriteFailureStage.FILE_IO:
                            raise
                        handler.rotation_error = _failure_description(error)
                    handler.do_write(message)
                except Exception as error:
                    file_io_failed = True
                    handler._record_file_failure(error)
                except BaseException as error:
                    handler._remember_control(error)
                    primary_copy_error = error
                    file_io_failed = True
                if not file_io_failed:
                    try:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = open(self._path, "rb")
                        target = open(destination, "wb")
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                    except BaseException as error:
                        handler._remember_control(error)
                        primary_copy_error = error
                    finally:
                        if target is not None:
                            try:
                                target.close()
                            except BaseException as error:
                                handler._remember_control(error)
                                target_close_exception = error
                        if source is not None:
                            try:
                                source.close()
                            except BaseException as error:
                                handler._remember_control(error)
                                source_close_exception = error
            finally:
                try:
                    handler._do_unlock()
                except BaseException as error:
                    handler._record_coordination_failure(
                        WriteFailureStage.LOCK_RELEASE, error)
                    coordination_error = handler.coordination_error
                    # 协调释放失败后不重试锁；保留既有追加和复制事实，
                    # 禁用辅助通道并只尝试一次关闭。
                    close_error = self._disable_and_close(
                        "file_handler" if file_io_failed else "coordination")
            if (file_io_failed or handler.failure_stage in (
                    WriteFailureStage.DIRECTORY_CLOSE, WriteFailureStage.FILE_CLOSE)) and not self._disabled:
                close_error = self._disable_and_close("file_handler")

            close_errors = tuple(error for error in (
                target_close_exception, source_close_exception) if error is not None)
            copy_handles_closed = not close_errors
            write_result = WriteResult(
                appended=handler.appended,
                rotated=True,
                rotation_error=handler.rotation_error,
                append_error=handler.append_error,
                coordination_error=coordination_error,
                close_error=_join_errors(handler.resource_close_error, close_error),
                failure_stage=handler.failure_stage,
                copy_handles_closed=copy_handles_closed,
                copy_source_close_error=(
                    None if source_close_exception is None else
                    _copy_close_failure(source_close_exception)),
                copy_target_close_error=(
                    None if target_close_exception is None else
                    _copy_close_failure(target_close_exception)),
                durable=handler.durable,
                sync_error=handler.sync_error,
            )
            if primary_copy_error is not None or close_errors:
                try:
                    copy_error = _raise_copy_failure(primary_copy_error, close_errors, write_result)
                except BaseException as error:
                    self._finish_result(write_result, error, propagate=True)
            return (self._finish_result(write_result), copy_error)

    def _finish_result(self, result, primary=None, *, propagate=False):
        """所有出口先交付真实事实，再传播本次操作的原首控制异常。"""
        handler = self._handler
        if primary is not None:
            handler._remember_control(primary)
        # 副本端口已经按操作及句柄关闭次序选定主异常，不再改选。
        error = primary if propagate else handler._control_error
        if error is not None:
            original = handler._failure_error
            try:
                if (original is not None and original is not error
                        and error.__cause__ is None and error.__context__ is None):
                    error.__context__ = original
            except BaseException:
                pass
            _attach_write_result(error, result)
            raise error.with_traceback(error.__traceback__)
        return result

    def _disable_and_close(self, close_kind: str) -> str | None:
        """失效后禁用通道并只尝试一次关闭；调用者持有 ``_lock``。"""
        if self._close_attempted:
            self._disabled = True
            return None
        self._disabled = True
        self._close_attempted = True
        try:
            self._handler.close()
        except BaseException as error:
            self._handler._remember_control(error)
            return (
                f"{close_kind}_close_failed: "
                f"{_failure_description(error)}"
            )
        self._closed = True
        return None

    def _build_entry(self, record: LogRecord) -> logging.LogRecord:
        return logging.LogRecord(
            name="camctl",
            level=_LEVEL_NAMES[record.level],
            pathname=__file__,
            lineno=0,
            msg=record.rendered_message().rstrip("\n"),
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
