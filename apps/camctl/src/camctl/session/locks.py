"""稳定会话锁与接纳锁。

两把锁采用排他内核锁（目标部署为 Linux flock；Windows 开发
环境以 msvcrt 字节范围锁适配同一契约）。锁文件保持稳定，
不通过删除重建释放锁；只有真正的锁冲突表示已有持有者，
其他系统错误单独报告。句柄由所属进程独立持有且不可继承。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class LockError(RuntimeError):
    """锁文件打开、加锁、解锁或句柄管理失败。"""


class SessionLockConflictError(LockError):
    """会话锁已被其他执行会话持有。"""


class AdmissionLockConflictError(LockError):
    """接纳锁已被其他接纳者持有。"""


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

    def close(self, backend: LockBackend | None = None) -> None:
        active = backend if backend is not None else self._backend
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            active.unlock(fd)
        finally:
            active.close(fd)


@dataclass
class SessionLease(_Lease):
    """run/init 持有的会话锁资格。"""


@dataclass
class AdmissionLease(_Lease):
    """接纳锁资格；只能由 run 在规定事务边界内取得和释放。"""


def _open_lock_file(path: Path, backend: LockBackend) -> int:
    try:
        fd = backend.open(path)
    except OSError as error:
        raise LockError(f"锁文件打开失败: {path}: {error}") from error
    backend.set_inheritable(fd, False)
    return fd


def acquire_session(lock_path: Path, backend: LockBackend | None = None) -> SessionLease:
    """非阻塞取得会话锁；冲突与系统错误分别表达。"""
    active = backend if backend is not None else default_backend()
    fd = _open_lock_file(lock_path, active)
    try:
        locked = active.try_lock(fd)
    except OSError as error:
        active.close(fd)
        raise LockError(f"会话锁加锁失败: {lock_path}: {error}") from error
    if not locked:
        active.close(fd)
        raise SessionLockConflictError(f"会话锁已被持有: {lock_path}")
    return SessionLease(fd=fd, path=lock_path, _backend=active)


def acquire_admission(lock_path: Path, backend: LockBackend | None = None) -> AdmissionLease:
    """非阻塞取得接纳锁；用于 run 的接纳阶段及事务内取得。"""
    active = backend if backend is not None else default_backend()
    fd = _open_lock_file(lock_path, active)
    try:
        locked = active.try_lock(fd)
    except OSError as error:
        active.close(fd)
        raise LockError(f"接纳锁加锁失败: {lock_path}: {error}") from error
    if not locked:
        active.close(fd)
        raise AdmissionLockConflictError(f"接纳锁已被持有: {lock_path}")
    return AdmissionLease(fd=fd, path=lock_path, _backend=active)


def probe_admission(lock_path: Path, backend: LockBackend | None = None) -> AdmissionProbe:
    """非阻塞探测接纳锁；取得后立即释放。

    只有真正的锁冲突表示已有接纳者；系统错误抛出 LockError，
    不解释为空闲或占用。
    """
    active = backend if backend is not None else default_backend()
    fd = _open_lock_file(lock_path, active)
    try:
        locked = active.try_lock(fd)
        if locked:
            active.unlock(fd)
    except OSError as error:
        raise LockError(f"接纳锁探测失败: {lock_path}: {error}") from error
    finally:
        active.close(fd)
    if locked:
        return AdmissionProbe(status=AdmissionProbeStatus.ACQUIRED_AND_RELEASED)
    return AdmissionProbe(status=AdmissionProbeStatus.CONFLICT)


def release_admission(lease: AdmissionLease, backend: LockBackend | None = None) -> None:
    """释放接纳锁；释放或关闭失败保留错误，不宣称成功关闭。"""
    try:
        lease.close(backend=backend)
    except OSError as error:
        raise LockError(f"接纳锁释放失败: {lease.path}: {error}") from error
