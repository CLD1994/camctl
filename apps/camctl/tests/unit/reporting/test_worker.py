"""报告工作进程局部契约的单元测试。

错误分类、父进程守护及工作锁通过进程内调用和内存锁替身验证。
"""

from __future__ import annotations

import sys

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence.runtime import StateDatabaseError
from camctl.reporting.messages import ErrorKind
from camctl.reporting.worker import (
    WorkLockError,
    acquire_work_lock,
    classify_worker_error,
    install_parent_guard,
)


class _FakeLockBackend:
    """内存替身：同一路径只有一个持有者，冲突如实报告。"""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.fail_open = False

    def open(self, path) -> int:
        if self.fail_open:
            raise OSError("打开失败")
        return hash(str(path)) & 0xFFFF

    def try_lock(self, fd: int) -> bool:
        key = str(fd)
        if key in self.held:
            return False
        self.held.add(key)
        return True

    def unlock(self, fd: int) -> None:
        self.held.discard(str(fd))

    def close(self, fd: int) -> None:
        self.held.discard(str(fd))

    def set_inheritable(self, fd: int, inheritable: bool) -> None:
        pass


class TestErrorClassification:
    def test_state_errors_are_distinguished(self) -> None:
        assert classify_worker_error(StateDatabaseError("库错误")) is ErrorKind.STATE
        assert classify_worker_error(ConsistencyError("历史解释失败")) is ErrorKind.STATE

    def test_other_errors_are_report_failures(self) -> None:
        assert classify_worker_error(OSError("磁盘写入失败")) is ErrorKind.REPORT
        assert classify_worker_error(ValueError("输入无效")) is ErrorKind.REPORT


class TestParentGuard:
    def test_guard_is_noop_outside_linux(self) -> None:
        if sys.platform == "linux":
            pytest.skip("非 Linux 平台的守护为无操作")
        assert install_parent_guard(1) is None


class TestWorkLock:
    def test_lock_conflict_times_out_and_release_allows_reacquire(self) -> None:
        backend = _FakeLockBackend()
        first = acquire_work_lock("state.db.report.lock", 0.1, backend=backend)
        try:
            with pytest.raises(WorkLockError, match="占用"):
                acquire_work_lock("state.db.report.lock", 0.1, backend=backend)
        finally:
            first.close()
        second = acquire_work_lock("state.db.report.lock", 0.1, backend=backend)
        second.close()

    def test_open_failure_is_reported_as_error(self) -> None:
        backend = _FakeLockBackend()
        backend.fail_open = True
        with pytest.raises(WorkLockError, match="打开失败"):
            acquire_work_lock("state.db.report.lock", 5.0, backend=backend)
