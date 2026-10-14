"""稳定会话锁与接纳锁。

两把锁采用排他内核锁（目标部署为 Linux flock；Windows 开发
环境以 msvcrt 字节范围锁适配同一契约）。锁文件保持稳定，
不通过删除重建释放锁；只有真正的锁冲突表示已有持有者，
其他系统错误单独报告。句柄由所属进程独立持有且不可继承。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, cast


class LockOperation(Enum):
    """锁端口实际执行的单个操作。"""

    OPEN = "open"
    SET_INHERITABLE = "set_inheritable"
    TRY_LOCK = "try_lock"
    UNLOCK = "unlock"
    CLOSE = "close"


class LockAcquisitionResult(Enum):
    """内核锁取得结果；异常结果保持未确认。"""

    NOT_ATTEMPTED = "not_attempted"
    ACQUIRED = "acquired"
    CONFLICT = "conflict"
    UNCONFIRMED = "unconfirmed"


class LockActionResult(Enum):
    """解锁或关闭操作的实际结果。"""

    NOT_ATTEMPTED = "not_attempted"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class _LockErrorFacts:
    """锁操作错误与句柄收场事实；错误后的关闭不推断句柄仍然打开。"""

    operation: LockOperation
    path: Path
    original_error: BaseException | None
    lock_result: LockAcquisitionResult
    unlock_result: LockActionResult
    close_result: LockActionResult
    secondary_errors: tuple[_LockErrorFacts, ...] = ()

    @property
    def errno(self) -> int | None:
        return (
            self.original_error.errno
            if isinstance(self.original_error, OSError)
            else None
        )


class LockError(RuntimeError):
    """锁文件打开、加锁、解锁或句柄管理失败。"""

    def __init__(
        self,
        message: str,
        *,
        facts: _LockErrorFacts | None = None,
    ) -> None:
        super().__init__(message)
        self.facts = facts


class SessionLockConflictError(LockError):
    """会话锁已被其他执行会话持有。"""


class AdmissionLockConflictError(LockError):
    """接纳锁已被其他接纳者持有。"""


_LOCK_FACTS_ATTRIBUTE = "lock_facts"


def lock_error_facts(error: BaseException) -> _LockErrorFacts | None:
    """读取锁模块附加的 typed 事实，包括未转换的内部控制异常。"""
    if isinstance(error, LockError) and isinstance(error.facts, _LockErrorFacts):
        return error.facts
    facts = getattr(error, _LOCK_FACTS_ATTRIBUTE, None)
    return facts if isinstance(facts, _LockErrorFacts) else None


def _attach_lock_facts(error: BaseException, facts: _LockErrorFacts) -> None:
    if isinstance(error, LockError):
        error.facts = facts
    setattr(error, _LOCK_FACTS_ATTRIBUTE, facts)


def _facts(
    operation: LockOperation,
    path: Path,
    original_error: BaseException | None,
    *,
    lock_result: LockAcquisitionResult = LockAcquisitionResult.NOT_ATTEMPTED,
    unlock_result: LockActionResult = LockActionResult.NOT_ATTEMPTED,
    close_result: LockActionResult = LockActionResult.NOT_ATTEMPTED,
    secondary_errors: tuple[_LockErrorFacts, ...] = (),
) -> _LockErrorFacts:
    return _LockErrorFacts(
        operation=operation,
        path=path,
        original_error=original_error,
        lock_result=lock_result,
        unlock_result=unlock_result,
        close_result=close_result,
        secondary_errors=secondary_errors,
    )


def _close_error_fact(path: Path, error: BaseException) -> _LockErrorFacts:
    return _facts(
        LockOperation.CLOSE,
        path,
        error,
        close_result=LockActionResult.UNCONFIRMED,
    )


def _operation_failure(
    message: str,
    error: BaseException,
    facts: _LockErrorFacts,
) -> BaseException:
    if isinstance(error, OSError):
        return LockError(message, facts=facts)
    _attach_lock_facts(error, facts)
    return error


def _raise_operation_failure(
    message: str,
    error: BaseException,
    facts: _LockErrorFacts,
) -> None:
    failure = _operation_failure(message, error, facts)
    if failure is error:
        raise error
    raise failure from error


@dataclass(frozen=True)
class _LockFailureEvent:
    operation: LockOperation
    error: BaseException


def _failure_event_facts(path: Path, event: _LockFailureEvent) -> _LockErrorFacts:
    if event.operation is LockOperation.TRY_LOCK:
        return _facts(
            event.operation,
            path,
            event.error,
            lock_result=LockAcquisitionResult.UNCONFIRMED,
        )
    if event.operation is LockOperation.UNLOCK:
        return _facts(
            event.operation,
            path,
            event.error,
            lock_result=LockAcquisitionResult.ACQUIRED,
            unlock_result=LockActionResult.UNCONFIRMED,
        )
    if event.operation is LockOperation.CLOSE:
        return _close_error_fact(path, event.error)
    return _facts(event.operation, path, event.error)


def _select_operation_failure(
    message: str,
    path: Path,
    events: tuple[_LockFailureEvent, ...],
    *,
    lock_result: LockAcquisitionResult,
    unlock_result: LockActionResult,
    close_result: LockActionResult,
) -> tuple[BaseException, BaseException]:
    """统一选择操作及其收场错误中的主错误。"""
    if not events:
        raise ValueError("至少需要一个锁操作错误事实")
    primary_index = next(
        (
            index
            for index, event in enumerate(events)
            if not isinstance(event.error, Exception)
        ),
        0,
    )
    primary = events[primary_index]
    secondary_errors: list[_LockErrorFacts] = []
    seen_errors = {id(primary.error)}
    for index, event in enumerate(events):
        if index == primary_index or id(event.error) in seen_errors:
            continue
        seen_errors.add(id(event.error))
        secondary_errors.append(_failure_event_facts(path, event))
    facts = _facts(
        primary.operation,
        path,
        primary.error,
        lock_result=lock_result,
        unlock_result=unlock_result,
        close_result=close_result,
        secondary_errors=tuple(secondary_errors),
    )
    failure = _operation_failure(
        f"{message}: {primary.error}",
        primary.error,
        facts,
    )
    return primary.error, failure


def _raise_selected_failure(cause: BaseException, failure: BaseException) -> None:
    if failure is cause:
        raise cause
    raise failure from cause


def _try_close(
    backend: LockBackend,
    fd: int,
) -> tuple[LockActionResult, BaseException | None]:
    try:
        backend.close(fd)
    except BaseException as error:
        return LockActionResult.UNCONFIRMED, error
    return LockActionResult.CONFIRMED, None


def _close_after_failure(
    path: Path,
    fd: int,
    backend: LockBackend,
    message: str,
    error: BaseException,
    operation: LockOperation,
    *,
    lock_result: LockAcquisitionResult = LockAcquisitionResult.NOT_ATTEMPTED,
    unlock_result: LockActionResult = LockActionResult.NOT_ATTEMPTED,
) -> None:
    close_result, close_error = _try_close(backend, fd)
    events = [_LockFailureEvent(operation, error)]
    if close_error is not None:
        events.append(_LockFailureEvent(LockOperation.CLOSE, close_error))
    cause, failure = _select_operation_failure(
        message,
        path,
        tuple(events),
        lock_result=lock_result,
        unlock_result=unlock_result,
        close_result=close_result,
    )
    _raise_selected_failure(cause, failure)


class LockBackend(Protocol):
    """内核锁操作端口；生产实现按平台选择。"""

    def open(self, path: Path) -> int: ...
    def try_lock(self, fd: int) -> bool: ...
    def unlock(self, fd: int) -> None: ...
    def close(self, fd: int) -> None: ...
    def set_inheritable(self, fd: int, inheritable: bool) -> None: ...


class _PosixLockBackend:
    """Linux flock 排他、非阻塞锁。"""

    def open(self, path: Path) -> int:
        import fcntl  # POSIX only

        _ = fcntl  # 保持导入局部，Windows 下不加载
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)

    def try_lock(self, fd: int) -> bool:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def unlock(self, fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self, fd: int) -> None:
        os.close(fd)

    def set_inheritable(self, fd: int, inheritable: bool) -> None:
        os.set_inheritable(fd, inheritable)


class _WindowsLockBackend:
    """msvcrt 字节范围锁，与 flock 同契约的非阻塞排他锁。"""

    def open(self, path: Path) -> int:
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY | os.O_NOINHERIT, 0o644)

    def try_lock(self, fd: int) -> bool:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (13, 36, 33):  # EACCES / EDEADLOCK / EDEADLK
                return False
            raise
        return True

    def unlock(self, fd: int) -> None:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def close(self, fd: int) -> None:
        os.close(fd)

    def set_inheritable(self, fd: int, inheritable: bool) -> None:
        os.set_inheritable(fd, inheritable)


def default_backend() -> LockBackend:
    return _WindowsLockBackend() if sys.platform == "win32" else _PosixLockBackend()


def lock_file_paths(database_path: Path) -> tuple[Path, Path]:
    """状态库对应的稳定锁文件对：会话锁与接纳锁。"""
    return (
        database_path.with_name(f"{database_path.name}.session.lock"),
        database_path.with_name(f"{database_path.name}.admission.lock"),
    )


class AdmissionProbeStatus(Enum):
    """接纳探测的非阻塞结果。"""

    CONFLICT = "conflict"
    ACQUIRED_AND_RELEASED = "acquired_and_released"


@dataclass(frozen=True)
class AdmissionProbe:
    """探测结果；is_free 只在真正取得并释放后为真。"""

    status: AdmissionProbeStatus

    @property
    def is_free(self) -> bool:
        return self.status is AdmissionProbeStatus.ACQUIRED_AND_RELEASED


@dataclass
class _Lease:
    """持有的锁句柄；关闭即释放资格，重复关闭不重复处理。"""

    fd: int
    path: Path
    _backend: LockBackend
    _close_confirmed: bool = field(default=False, init=False, repr=False)
    _close_failure: BaseException | None = field(default=None, init=False, repr=False)

    def close(self, backend: LockBackend | None = None) -> None:
        active = backend if backend is not None else self._backend
        if self._close_failure is not None:
            raise self._close_failure
        if self._close_confirmed or self.fd < 0:
            return
        fd, self.fd = self.fd, -1

        events: list[_LockFailureEvent] = []
        unlock_result = LockActionResult.NOT_ATTEMPTED
        try:
            active.unlock(fd)
        except BaseException as error:
            events.append(_LockFailureEvent(LockOperation.UNLOCK, error))
            unlock_result = LockActionResult.UNCONFIRMED
        else:
            unlock_result = LockActionResult.CONFIRMED

        try:
            active.close(fd)
        except BaseException as error:
            close_result = LockActionResult.UNCONFIRMED
            events.append(_LockFailureEvent(LockOperation.CLOSE, error))
        else:
            close_result = LockActionResult.CONFIRMED
            self._close_confirmed = True

        if events:
            cause, failure = _select_operation_failure(
                f"锁租约释放失败: {self.path}",
                self.path,
                tuple(events),
                lock_result=LockAcquisitionResult.ACQUIRED,
                unlock_result=unlock_result,
                close_result=close_result,
            )
            self._close_failure = failure
            _raise_selected_failure(cause, failure)


@dataclass
class SessionLease(_Lease):
    """run/init 持有的会话锁资格。"""


@dataclass
class AdmissionLease(_Lease):
    """接纳锁资格；只能由 run 在规定事务边界内取得和释放。"""


def _open_lock_file(path: Path, backend: LockBackend) -> int:
    try:
        fd = backend.open(path)
    except BaseException as error:
        facts = _facts(LockOperation.OPEN, path, error)
        _raise_operation_failure(f"锁文件打开失败: {path}: {error}", error, facts)
    try:
        backend.set_inheritable(fd, False)
    except BaseException as error:
        _close_after_failure(
            path,
            fd,
            backend,
            f"锁文件不可继承设置失败: {path}",
            error,
            LockOperation.SET_INHERITABLE,
        )
    return fd


def _acquire(
    lock_path: Path,
    backend: LockBackend | None,
    *,
    lease_type: type[_Lease],
    conflict_type: type[LockError],
    label: str,
) -> _Lease:
    active = backend if backend is not None else default_backend()
    fd = _open_lock_file(lock_path, active)
    try:
        locked = active.try_lock(fd)
    except BaseException as error:
        _close_after_failure(
            lock_path,
            fd,
            active,
            f"{label}加锁失败: {lock_path}",
            error,
            LockOperation.TRY_LOCK,
            lock_result=LockAcquisitionResult.UNCONFIRMED,
        )
    if not locked:
        close_result, close_error = _try_close(active, fd)
        if close_error is not None and not isinstance(close_error, OSError):
            facts = _facts(
                LockOperation.CLOSE,
                lock_path,
                close_error,
                lock_result=LockAcquisitionResult.CONFLICT,
                close_result=close_result,
            )
            _attach_lock_facts(close_error, facts)
            raise close_error
        secondary_errors = (
            (_close_error_fact(lock_path, close_error),)
            if close_error is not None
            else ()
        )
        facts = _facts(
            LockOperation.TRY_LOCK,
            lock_path,
            None,
            lock_result=LockAcquisitionResult.CONFLICT,
            close_result=close_result,
            secondary_errors=secondary_errors,
        )
        raise conflict_type(f"{label}已被持有: {lock_path}", facts=facts)
    return lease_type(fd=fd, path=lock_path, _backend=active)


def acquire_session(lock_path: Path, backend: LockBackend | None = None) -> SessionLease:
    """非阻塞取得会话锁；冲突与系统错误分别表达。"""
    return cast(
        SessionLease,
        _acquire(
            lock_path,
            backend,
            lease_type=SessionLease,
            conflict_type=SessionLockConflictError,
            label="会话锁",
        ),
    )


def acquire_admission(lock_path: Path, backend: LockBackend | None = None) -> AdmissionLease:
    """非阻塞取得接纳锁；用于 run 的接纳阶段及事务内取得。"""
    return cast(
        AdmissionLease,
        _acquire(
            lock_path,
            backend,
            lease_type=AdmissionLease,
            conflict_type=AdmissionLockConflictError,
            label="接纳锁",
        ),
    )


def probe_admission(lock_path: Path, backend: LockBackend | None = None) -> AdmissionProbe:
    """非阻塞探测接纳锁；取得后立即释放。

    只有真正的锁冲突表示已有接纳者；系统错误抛出 LockError，
    不解释为空闲或占用。
    """
    active = backend if backend is not None else default_backend()
    fd = _open_lock_file(lock_path, active)
    lock_result = LockAcquisitionResult.NOT_ATTEMPTED
    unlock_result = LockActionResult.NOT_ATTEMPTED
    events: list[_LockFailureEvent] = []

    try:
        locked = active.try_lock(fd)
    except BaseException as error:
        locked = False
        lock_result = LockAcquisitionResult.UNCONFIRMED
        events.append(_LockFailureEvent(LockOperation.TRY_LOCK, error))
    else:
        if locked:
            lock_result = LockAcquisitionResult.ACQUIRED
            try:
                active.unlock(fd)
            except BaseException as error:
                unlock_result = LockActionResult.UNCONFIRMED
                events.append(_LockFailureEvent(LockOperation.UNLOCK, error))
            else:
                unlock_result = LockActionResult.CONFIRMED
        else:
            lock_result = LockAcquisitionResult.CONFLICT

    close_result, close_error = _try_close(active, fd)
    if close_error is not None:
        events.append(_LockFailureEvent(LockOperation.CLOSE, close_error))
    if events:
        cause, failure = _select_operation_failure(
            f"接纳锁探测失败: {lock_path}",
            lock_path,
            tuple(events),
            lock_result=lock_result,
            unlock_result=unlock_result,
            close_result=close_result,
        )
        _raise_selected_failure(cause, failure)

    if lock_result is LockAcquisitionResult.ACQUIRED:
        return AdmissionProbe(status=AdmissionProbeStatus.ACQUIRED_AND_RELEASED)
    return AdmissionProbe(status=AdmissionProbeStatus.CONFLICT)


def release_admission(lease: AdmissionLease, backend: LockBackend | None = None) -> None:
    """释放接纳锁；释放或关闭失败保留错误，不宣称成功关闭。"""
    lease.close(backend=backend)
