"""S2 稳定锁与启动前提的组件集成测试。

真实文件与内核锁；用独立子进程验证互斥、异常退出释放及
句柄不被继承。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from camctl.session.locks import (
    AdmissionLockConflictError,
    AdmissionProbeStatus,
    SessionLockConflictError,
    acquire_admission,
    acquire_session,
    lock_file_paths,
    probe_admission,
    release_admission,
)

_HOLD_LOCK_SCRIPT = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    from camctl.session.locks import acquire_admission

    lease = acquire_admission(Path(sys.argv[1]))
    sys.stdout.write("HELD\\n")
    sys.stdout.flush()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(0.05)
    """
)


def test_mutual_exclusion_across_processes(tmp_path: Path) -> None:
    admission_path = lock_file_paths(tmp_path / "state.db")[1]
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_SCRIPT, str(admission_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "HELD"
        probe = probe_admission(admission_path)
        assert probe.status is AdmissionProbeStatus.CONFLICT
        assert probe.is_free is False
        try:
            acquire_admission(admission_path)
            raise AssertionError("子进程持锁时本进程不应取得接纳锁")
        except AdmissionLockConflictError:
            pass
    finally:
        child.kill()
        child.wait(timeout=30)


def test_lock_released_after_abnormal_exit(tmp_path: Path) -> None:
    admission_path = lock_file_paths(tmp_path / "state.db")[1]
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_SCRIPT, str(admission_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "HELD"
    child.kill()
    child.wait(timeout=30)
    probe = probe_admission(admission_path)
    assert probe.status is AdmissionProbeStatus.ACQUIRED_AND_RELEASED


def test_session_lock_conflicts_with_running_session(tmp_path: Path) -> None:
    session_path = lock_file_paths(tmp_path / "state.db")[0]
    lease = acquire_session(session_path)
    try:
        try:
            acquire_session(session_path)
            raise AssertionError("同一状态库不应有第二个会话")
        except SessionLockConflictError:
            pass
    finally:
        lease.close()
    fresh = acquire_session(session_path)
    fresh.close()


def test_handles_are_not_inherited_by_children(tmp_path: Path) -> None:
    admission_path = lock_file_paths(tmp_path / "state.db")[1]
    lease = acquire_admission(admission_path)
    try:
        inherited = subprocess.run(
            [sys.executable, "-c", _HOLD_LOCK_SCRIPT, str(admission_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=40,
        )
        # 父进程持有非继承句柄时，子进程必须冲突并快速失败，
        # 而不是继承资格后挂起至超时。
        assert "HELD" not in inherited.stdout
    finally:
        lease.close()


def test_probe_leaves_no_residual_lock(tmp_path: Path) -> None:
    admission_path = lock_file_paths(tmp_path / "state.db")[1]
    assert probe_admission(admission_path).is_free is True
    lease = acquire_admission(admission_path)
    lease.close()
    assert probe_admission(admission_path).is_free is True
    assert admission_path.exists()


def test_release_admission_closes_handle(tmp_path: Path) -> None:
    admission_path = lock_file_paths(tmp_path / "state.db")[1]
    lease = acquire_admission(admission_path)
    release_admission(lease)
    assert probe_admission(admission_path).is_free is True
