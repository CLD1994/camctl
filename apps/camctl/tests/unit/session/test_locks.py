"""S2 稳定锁与启动前提的单元测试。

系统调用以受 LockBackend 协议约束的替身注入；
期望独立来自协议会话锁规则。
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from camctl.session.locks import (
    AdmissionProbeStatus,
    LockError,
    SessionLockConflictError,
    acquire_admission,
    acquire_session,
    lock_file_paths,
    probe_admission,
    release_admission,
)


class FakeLockBackend:
    """受 LockBackend 协议约束的替身：记录句柄与锁状态。"""

    def __init__(self) -> None:
        self.conflict = False
        self.existing_files: set[Path] = set()
        self.open_error: OSError | None = None
        self.unlock_error: OSError | None = None
        self.open_count = 0
        self.unlock_count = 0
        self.open_fds: set[int] = set()
        self.locked_fds: set[int] = set()
        self.inheritable_fds: dict[int, bool] = {}
        self._next_fd = 10

    def open(self, path: Path) -> int:
        self.open_count += 1
        if self.open_error is not None:
            raise self.open_error
        fd = self._next_fd
        self._next_fd += 1
        self.open_fds.add(fd)
        self.inheritable_fds[fd] = True
        return fd

    def try_lock(self, fd: int) -> bool:
        if self.conflict:
            return False
        self.locked_fds.add(fd)
        return True

    def unlock(self, fd: int) -> None:
        if self.unlock_error is not None:
            raise self.unlock_error
        self.unlock_count += 1
        self.locked_fds.discard(fd)

    def close(self, fd: int) -> None:
        self.open_fds.discard(fd)
        self.locked_fds.discard(fd)

    def set_inheritable(self, fd: int, inheritable: bool) -> None:
        self.inheritable_fds[fd] = inheritable


@pytest.fixture()
def backend() -> FakeLockBackend:
    return FakeLockBackend()


def test_lock_error_is_not_free(backend: FakeLockBackend) -> None:
    """非冲突的系统错误不能解释为没有接纳者。"""
    backend.open_error = OSError(errno.EIO, "io error")
    with pytest.raises(LockError):
        probe_admission(Path("/var/locks/admission.lock"), backend=backend)


def test_probe_reports_conflict_as_not_free(backend: FakeLockBackend) -> None:
    backend.conflict = True
    probe = probe_admission(Path("/x/admission.lock"), backend=backend)
    assert probe.status is AdmissionProbeStatus.CONFLICT
    assert probe.is_free is False


def test_probe_acquires_and_releases(backend: FakeLockBackend) -> None:
    path = Path("/x/admission.lock")
    probe = probe_admission(path, backend=backend)
    assert probe.status is AdmissionProbeStatus.ACQUIRED_AND_RELEASED
    assert probe.is_free is True
    assert backend.locked_fds == set()
    assert backend.unlock_count == 1


def test_release_failure_yields_no_success(backend: FakeLockBackend) -> None:
    path = Path("/x/admission.lock")
    lease = acquire_admission(path, backend=backend)
    backend.unlock_error = OSError(errno.EIO, "io error")
    with pytest.raises(LockError):
        release_admission(lease, backend=backend)


def test_existing_unlocked_file_is_acquirable(backend: FakeLockBackend) -> None:
    backend.existing_files = {Path("/x/admission.lock")}
    probe = probe_admission(Path("/x/admission.lock"), backend=backend)
    assert probe.is_free is True


def test_session_conflict_is_distinct_from_lock_error(backend: FakeLockBackend) -> None:
    backend.conflict = True
    with pytest.raises(SessionLockConflictError):
        acquire_session(Path("/x/state.db"), backend=backend)
    assert backend.open_count >= 1


def test_handles_are_not_inheritable(backend: FakeLockBackend) -> None:
    lease = acquire_admission(Path("/x/admission.lock"), backend=backend)
    assert backend.inheritable_fds.get(lease.fd) is False
    lease.close(backend=backend)


def test_lease_close_is_idempotent(backend: FakeLockBackend) -> None:
    lease = acquire_admission(Path("/x/admission.lock"), backend=backend)
    lease.close(backend=backend)
    lease.close(backend=backend)
    assert backend.unlock_count == 1


def test_lock_file_paths_are_stable_per_database() -> None:
    session, admission = lock_file_paths(Path("/srv/camctl/state.db"))
    assert session == Path("/srv/camctl/state.db.session.lock")
    assert admission == Path("/srv/camctl/state.db.admission.lock")
    other_session, _ = lock_file_paths(Path("/srv/camctl/other.db"))
    assert other_session != session


def test_missing_lock_directory_is_reported(backend: FakeLockBackend) -> None:
    backend.open_error = FileNotFoundError(errno.ENOENT, "no directory")
    with pytest.raises(LockError):
        acquire_session(Path("/missing/state.db"), backend=backend)
