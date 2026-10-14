"""S2 稳定锁与启动前提的单元测试。

系统调用以受 LockBackend 协议约束的替身注入；
期望独立来自协议会话锁规则。
"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest

from camctl.session.locks import (
    AdmissionLockConflictError,
    AdmissionProbeStatus,
    LockError,
    SessionLockConflictError,
    acquire_admission,
    acquire_session,
    lock_error_facts,
    lock_file_paths,
    probe_admission,
    release_admission,
)


class FakeLockBackend:
    """受 LockBackend 协议约束的替身：记录句柄与锁状态。"""

    def __init__(self) -> None:
        self.conflict = False
        self.existing_files: set[Path] = set()
        self.open_error: BaseException | None = None
        self.inheritable_error: BaseException | None = None
        self.try_lock_error: BaseException | None = None
        self.unlock_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.open_count = 0
        self.unlock_count = 0
        self.close_count = 0
        self.open_fds: set[int] = set()
        self.locked_fds: set[int] = set()
        self.inheritable_fds: dict[int, bool] = {}
        self._next_fd = 10

    def open(self, path: Path) -> int:
        self.calls.append(("open", (path,)))
        self.open_count += 1
        if self.open_error is not None:
            raise self.open_error
        fd = self._next_fd
        self._next_fd += 1
        self.open_fds.add(fd)
        self.inheritable_fds[fd] = True
        return fd

    def try_lock(self, fd: int) -> bool:
        self.calls.append(("try_lock", (fd,)))
        if self.try_lock_error is not None:
            raise self.try_lock_error
        if self.conflict:
            return False
        self.locked_fds.add(fd)
        return True

    def unlock(self, fd: int) -> None:
        self.calls.append(("unlock", (fd,)))
        self.unlock_count += 1
        if self.unlock_error is not None:
            raise self.unlock_error
        self.locked_fds.discard(fd)

    def close(self, fd: int) -> None:
        self.calls.append(("close", (fd,)))
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error
        self.open_fds.discard(fd)
        self.locked_fds.discard(fd)

    def set_inheritable(self, fd: int, inheritable: bool) -> None:
        self.calls.append(("set_inheritable", (fd, inheritable)))
        if self.inheritable_error is not None:
            raise self.inheritable_error
        self.inheritable_fds[fd] = inheritable


@pytest.fixture()
def backend() -> FakeLockBackend:
    return FakeLockBackend()


def _lock_facts_for(error: BaseException):
    facts = getattr(error, "facts", None)
    if facts is None:
        facts = getattr(error, "lock_facts", None)
    assert facts is not None
    return facts


def _injected_error(kind: str, message: str) -> BaseException:
    if kind == "oserror":
        return OSError(errno.EIO, message)
    if kind == "runtime":
        return RuntimeError(message)
    if kind == "control":
        return asyncio.CancelledError(message)
    raise AssertionError(f"unknown injected error kind: {kind}")


@pytest.mark.parametrize("entry", ["acquire", "probe", "lease"])
@pytest.mark.parametrize(
    ("primary_kind", "close_kind"),
    [
        ("oserror", "runtime"),
        ("oserror", "control"),
        ("runtime", "control"),
        ("control", "control"),
    ],
    ids=[
        "oserror-runtime",
        "oserror-control",
        "runtime-control",
        "control-control",
    ],
)
def test_cleanup_exception_priority_preserves_actual_sources(
    backend: FakeLockBackend,
    entry: str,
    primary_kind: str,
    close_kind: str,
) -> None:
    path = Path("/x/state.db.session.lock")
    primary_error = _injected_error(primary_kind, f"{entry} primary")
    close_error = _injected_error(close_kind, f"{entry} close")
    if primary_kind == "control":
        primary_error.add_note("original control note")
    backend.close_error = close_error

    if entry == "acquire":
        backend.inheritable_error = primary_error

        def invoke() -> None:
            acquire_session(path, backend=backend)

        primary_operation = "set_inheritable"
        expected_lock_result = "not_attempted"
        expected_unlock_result = "not_attempted"
    elif entry == "probe":
        backend.unlock_error = primary_error

        def invoke() -> None:
            probe_admission(path, backend=backend)

        primary_operation = "unlock"
        expected_lock_result = "acquired"
        expected_unlock_result = "unconfirmed"
    else:
        lease = acquire_admission(path, backend=backend)
        backend.unlock_error = primary_error

        def invoke() -> None:
            lease.close(backend=backend)

        primary_operation = "unlock"
        expected_lock_result = "acquired"
        expected_unlock_result = "unconfirmed"

    try:
        invoke()
    except BaseException as error:
        caught = error
    else:
        pytest.fail(f"{entry} unexpectedly returned success after injected errors")

    if close_kind == "control" and primary_kind != "control":
        propagated_error = close_error
        secondary_error = primary_error
        expected_operation = "close"
        expected_secondary_operation = primary_operation
    else:
        propagated_error = primary_error
        secondary_error = close_error
        expected_operation = primary_operation
        expected_secondary_operation = "close"

    if isinstance(propagated_error, OSError):
        assert isinstance(caught, LockError)
        assert caught.__cause__ is propagated_error
    else:
        assert caught is propagated_error
    facts = lock_error_facts(caught)
    assert facts is not None
    assert facts.operation.value == expected_operation
    assert facts.path == path
    assert facts.original_error is propagated_error
    assert facts.errno == (
        propagated_error.errno if isinstance(propagated_error, OSError) else None
    )
    assert facts.lock_result.value == expected_lock_result
    assert facts.unlock_result.value == expected_unlock_result
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == expected_secondary_operation
    assert facts.secondary_errors[0].original_error is secondary_error
    assert all(
        item.original_error is not propagated_error
        for item in facts.secondary_errors
    )
    if primary_kind == "control":
        assert caught.__notes__ == ["original control note"]

    assert backend.close_count == 1
    if entry in {"probe", "lease"}:
        assert backend.unlock_count == 1
    if entry == "lease":
        with pytest.raises(BaseException) as repeated:
            lease.close(backend=backend)
        assert repeated.value is caught
        assert backend.unlock_count == 1
        assert backend.close_count == 1


@pytest.mark.parametrize(
    ("acquire", "conflict_type"),
    [
        (acquire_session, SessionLockConflictError),
        (acquire_admission, AdmissionLockConflictError),
    ],
)
@pytest.mark.parametrize("close_kind", ["runtime", "control"])
def test_conflict_close_internal_error_keeps_close_source(
    backend: FakeLockBackend,
    acquire,
    conflict_type: type[LockError],
    close_kind: str,
) -> None:
    path = Path("/x/state.db.lock")
    close_error = _injected_error(close_kind, "conflict close failed")
    backend.conflict = True
    backend.close_error = close_error

    try:
        acquire(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("conflicting acquisition unexpectedly returned a lease")

    assert caught is close_error
    assert not isinstance(caught, conflict_type)
    assert backend.calls == [
        ("open", (path,)),
        ("set_inheritable", (10, False)),
        ("try_lock", (10,)),
        ("close", (10,)),
    ]
    assert backend.close_count == 1
    facts = lock_error_facts(caught)
    assert facts is not None
    assert facts.operation.value == "close"
    assert facts.path == path
    assert facts.original_error is close_error
    assert facts.lock_result.value == "conflict"
    assert facts.close_result.value == "unconfirmed"
    assert facts.secondary_errors == ()


@pytest.mark.parametrize("close_kind", ["runtime", "control"])
def test_probe_conflict_close_internal_error_keeps_close_source(
    backend: FakeLockBackend,
    close_kind: str,
) -> None:
    path = Path("/x/state.db.admission.lock")
    close_error = _injected_error(close_kind, "probe conflict close failed")
    backend.conflict = True
    backend.close_error = close_error

    try:
        probe_admission(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("probe returned a result after its conflicting handle close failed")

    assert caught is close_error
    assert backend.calls == [
        ("open", (path,)),
        ("set_inheritable", (10, False)),
        ("try_lock", (10,)),
        ("close", (10,)),
    ]
    assert backend.unlock_count == 0
    assert backend.close_count == 1
    facts = lock_error_facts(caught)
    assert facts is not None
    assert facts.operation.value == "close"
    assert facts.path == path
    assert facts.original_error is close_error
    assert facts.lock_result.value == "conflict"
    assert facts.unlock_result.value == "not_attempted"
    assert facts.close_result.value == "unconfirmed"
    assert facts.secondary_errors == ()


def test_lock_error_facts_reject_untyped_attributes() -> None:
    lock_error = LockError("invalid lock facts")
    setattr(lock_error, "facts", object())
    internal_error = RuntimeError("invalid internal facts")
    internal_error.lock_facts = object()

    assert lock_error_facts(lock_error) is None
    assert lock_error_facts(internal_error) is None


def test_lock_error_is_not_free(backend: FakeLockBackend) -> None:
    """非冲突的系统错误不能解释为没有接纳者。"""
    path = Path("/var/locks/admission.lock")
    backend.open_error = OSError(errno.EIO, "io error")
    with pytest.raises(LockError) as caught:
        probe_admission(path, backend=backend)

    facts = _lock_facts_for(caught.value)
    assert facts.operation.value == "open"
    assert facts.path == path
    assert facts.original_error is backend.open_error
    assert facts.errno == errno.EIO
    assert facts.close_result.value == "not_attempted"


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
    with pytest.raises(LockError) as caught:
        release_admission(lease, backend=backend)

    facts = _lock_facts_for(caught.value)
    assert caught.value.__cause__ is backend.unlock_error
    assert facts.operation.value == "unlock"
    assert facts.original_error is backend.unlock_error
    assert facts.lock_result.value == "acquired"
    assert facts.unlock_result.value == "unconfirmed"
    assert facts.close_result.value == "confirmed"


def test_existing_unlocked_file_is_acquirable(backend: FakeLockBackend) -> None:
    backend.existing_files = {Path("/x/admission.lock")}
    probe = probe_admission(Path("/x/admission.lock"), backend=backend)
    assert probe.is_free is True


@pytest.mark.parametrize(
    ("acquire", "conflict_type"),
    [
        (acquire_session, SessionLockConflictError),
        (acquire_admission, AdmissionLockConflictError),
    ],
)
def test_acquire_conflict_is_distinct_from_lock_error(
    backend: FakeLockBackend, acquire, conflict_type,
) -> None:
    backend.conflict = True
    path = Path("/x/state.db")
    with pytest.raises(conflict_type) as caught:
        acquire(path, backend=backend)

    assert backend.open_count == 1
    assert backend.close_count == 1
    facts = _lock_facts_for(caught.value)
    assert facts.operation.value == "try_lock"
    assert facts.path == path
    assert facts.original_error is None
    assert facts.lock_result.value == "conflict"
    assert facts.close_result.value == "confirmed"


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


def test_inheritance_failure_closes_opened_handle(backend: FakeLockBackend) -> None:
    path = Path("/x/state.db.session.lock")
    setup_error = OSError(errno.EPERM, "cannot clear inheritable flag")
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.inheritable_error = setup_error
    backend.close_error = close_error

    try:
        acquire_session(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("inheritability failure unexpectedly returned a lease")

    assert backend.calls == [
        ("open", (path,)),
        ("set_inheritable", (10, False)),
        ("close", (10,)),
    ]
    assert isinstance(caught, LockError)
    assert caught.__cause__ is setup_error
    facts = _lock_facts_for(caught)
    assert lock_error_facts(caught) is facts
    assert facts.operation.value == "set_inheritable"
    assert facts.path == path
    assert facts.original_error is setup_error
    assert facts.errno == errno.EPERM
    assert facts.lock_result.value == "not_attempted"
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == "close"
    assert facts.secondary_errors[0].original_error is close_error
    assert facts.secondary_errors[0].errno == errno.EBADF


def test_lock_failure_keeps_close_failure_secondary(backend: FakeLockBackend) -> None:
    path = Path("/x/state.db.session.lock")
    lock_error = OSError(errno.EIO, "lock operation failed")
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.try_lock_error = lock_error
    backend.close_error = close_error

    try:
        acquire_session(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("lock failure unexpectedly returned a lease")

    assert backend.calls == [
        ("open", (path,)),
        ("set_inheritable", (10, False)),
        ("try_lock", (10,)),
        ("close", (10,)),
    ]
    assert isinstance(caught, LockError)
    assert caught.__cause__ is lock_error
    assert caught is not close_error
    facts = _lock_facts_for(caught)
    assert facts.operation.value == "try_lock"
    assert facts.path == path
    assert facts.original_error is lock_error
    assert facts.errno == errno.EIO
    assert facts.lock_result.value == "unconfirmed"
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == "close"
    assert facts.secondary_errors[0].original_error is close_error
    assert facts.secondary_errors[0].errno == errno.EBADF


@pytest.mark.parametrize("conflict", [False, True])
def test_probe_close_failure_never_reports_a_result(
    backend: FakeLockBackend, conflict: bool,
) -> None:
    path = Path("/x/state.db.admission.lock")
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.conflict = conflict
    backend.close_error = close_error

    try:
        probe_admission(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("probe returned a result without confirmed handle close")

    assert backend.calls[-1] == ("close", (10,))
    assert backend.close_count == 1
    assert isinstance(caught, LockError)
    assert caught.__cause__ is close_error
    facts = _lock_facts_for(caught)
    assert facts.operation.value == "close"
    assert facts.path == path
    assert facts.original_error is close_error
    assert facts.errno == errno.EBADF
    assert facts.close_result.value == "unconfirmed"
    assert facts.lock_result.value == ("conflict" if conflict else "acquired")
    assert facts.unlock_result.value == (
        "not_attempted" if conflict else "confirmed"
    )
    assert facts.secondary_errors == ()


def test_probe_unlock_failure_keeps_acquired_fact_and_closes_handle(
    backend: FakeLockBackend,
) -> None:
    path = Path("/x/state.db.admission.lock")
    unlock_error = OSError(errno.EIO, "unlock failed")
    backend.unlock_error = unlock_error

    with pytest.raises(LockError) as caught:
        probe_admission(path, backend=backend)

    assert backend.calls[-2:] == [("unlock", (10,)), ("close", (10,))]
    assert backend.unlock_count == 1
    assert backend.close_count == 1
    assert caught.value.__cause__ is unlock_error
    facts = _lock_facts_for(caught.value)
    assert facts.operation.value == "unlock"
    assert facts.path == path
    assert facts.original_error is unlock_error
    assert facts.errno == errno.EIO
    assert facts.lock_result.value == "acquired"
    assert facts.unlock_result.value == "unconfirmed"
    assert facts.close_result.value == "confirmed"


@pytest.mark.parametrize(
    ("acquire", "conflict_type"),
    [
        (acquire_session, SessionLockConflictError),
        (acquire_admission, AdmissionLockConflictError),
    ],
)
def test_acquire_conflict_keeps_conflict_type_when_close_is_unconfirmed(
    backend: FakeLockBackend, acquire, conflict_type,
) -> None:
    path = Path("/x/state.db.lock")
    backend.conflict = True
    backend.close_error = OSError(errno.EBADF, "close result unavailable")

    try:
        acquire(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("conflicting acquisition unexpectedly returned a lease")

    assert backend.close_count == 1
    assert isinstance(caught, conflict_type)
    facts = _lock_facts_for(caught)
    assert facts.operation.value == "try_lock"
    assert facts.path == path
    assert facts.original_error is None
    assert facts.lock_result.value == "conflict"
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == "close"
    assert facts.secondary_errors[0].original_error is backend.close_error


def test_lease_unlock_and_close_errors_are_both_preserved(
    backend: FakeLockBackend,
) -> None:
    path = Path("/x/state.db.admission.lock")
    lease = acquire_admission(path, backend=backend)
    unlock_error = OSError(errno.EIO, "unlock failed")
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.unlock_error = unlock_error
    backend.close_error = close_error

    try:
        release_admission(lease, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("failed lease release unexpectedly returned successfully")

    assert backend.unlock_count == 1
    assert backend.close_count == 1
    assert isinstance(caught, LockError)
    assert caught.__cause__ is unlock_error
    facts = _lock_facts_for(caught)
    assert facts.operation.value == "unlock"
    assert facts.original_error is unlock_error
    assert facts.errno == errno.EIO
    assert facts.lock_result.value == "acquired"
    assert facts.unlock_result.value == "unconfirmed"
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == "close"
    assert facts.secondary_errors[0].original_error is close_error


def test_failed_lease_close_is_not_retried_or_reported_success(
    backend: FakeLockBackend,
) -> None:
    lease = acquire_admission(Path("/x/state.db.admission.lock"), backend=backend)
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.close_error = close_error

    failures: list[BaseException] = []
    for _ in range(2):
        try:
            lease.close(backend=backend)
        except BaseException as error:
            failures.append(error)

    assert len(failures) == 2
    assert failures[0] is failures[1]
    assert backend.unlock_count == 1
    assert backend.close_count == 1
    facts = _lock_facts_for(failures[0])
    assert facts.operation.value == "close"
    assert facts.original_error is close_error
    assert facts.close_result.value == "unconfirmed"


@pytest.mark.parametrize(
    "internal_error",
    [RuntimeError("lock backend bug"), KeyboardInterrupt("lock interrupted")],
)
def test_internal_lock_error_keeps_identity_and_typed_cleanup_context(
    backend: FakeLockBackend, internal_error: BaseException,
) -> None:
    path = Path("/x/state.db.session.lock")
    close_error = OSError(errno.EBADF, "close result unavailable")
    backend.inheritable_error = internal_error
    backend.close_error = close_error

    try:
        acquire_session(path, backend=backend)
    except BaseException as error:
        caught = error
    else:
        pytest.fail("internal lock failure unexpectedly returned a lease")

    assert caught is internal_error
    assert backend.calls[-1] == ("close", (10,))
    facts = _lock_facts_for(caught)
    assert lock_error_facts(caught) is facts
    assert facts.operation.value == "set_inheritable"
    assert facts.path == path
    assert facts.original_error is internal_error
    assert facts.close_result.value == "unconfirmed"
    assert len(facts.secondary_errors) == 1
    assert facts.secondary_errors[0].operation.value == "close"
    assert facts.secondary_errors[0].original_error is close_error
