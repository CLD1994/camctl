"""报告生成子进程：保护、工作锁、运行库检查与任务循环。

子进程先建立父进程死亡保护并核对原父身份，再在启动时限内有界
等待取得报告工作锁，检查实际 SQLite 运行库，全部通过后才通知
就绪。任务循环一次处理一份报告：核验数据库身份后按冻结依据生
成 staging 文件并同步，随后返回携带任务身份的结果；错误分类保
留报告失败与状态库错误的区别。结果发出后文件不再修改；成功与
否由主进程按消息判定，不以进程退出码代替。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from camctl.contracts.history_values import BoundaryError
from camctl.contracts.json_values import JsonParseError
from camctl.contracts.public_projection import PublicProjectionError
from camctl.contracts.values import ConsistencyError
from camctl.history.events import HistoryEventError
from camctl.history.replay import ReplayError
from camctl.persistence.runtime import (
    DbConfig,
    DbOpenMode,
    StateDatabaseError,
    ensure_runtime_library,
    open_existing,
)
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.messages import (
    MAX_MESSAGE_BYTES,
    ErrorKind,
    JobMessage,
    MessageProtocolError,
    ReadyMessage,
    ResultFailureMessage,
    ResultSuccessMessage,
    ShutdownMessage,
    StartupFailedMessage,
    StartupPhase,
    decode_message,
    encode_message,
)
from camctl.session.locks import LockBackend, default_backend

__all__ = [
    "WorkLockError",
    "acquire_work_lock",
    "classify_worker_error",
    "install_parent_guard",
    "report_lock_path",
    "run_job",
    "worker_main",
]

_LOCK_POLL_SECONDS = 0.05
#: 子进程在启动时限前预留的报告余量：锁等待失败仍能按时送达。
_STARTUP_REPORT_MARGIN = 0.5
#: 状态库或历史解释错误：结果按状态库错误分类向上传递。
_STATE_ERRORS = (
    StateDatabaseError,
    ConsistencyError,
    PublicProjectionError,
    BoundaryError,
    HistoryEventError,
    ReplayError,
    JsonParseError,
)


class WorkLockError(RuntimeError):
    """报告工作锁打开、加锁失败或等待超时。"""


def report_lock_path(database_path: Path) -> Path:
    """状态库对应的稳定报告工作锁文件；删除或替换不取得资格。"""
    return database_path.with_name(f"{database_path.name}.report.lock")


def install_parent_guard(expected_parent_pid: int) -> str | None:
    """在 Linux 上设置父进程死亡信号并核对原父身份。

    目标部署为 Linux（PR_SET_PDEATHSIG=SIGKILL，内核在原父线程
    结束时发送强杀信号）；其他平台为无操作。设置前后都核对原父
    进程身份，覆盖设置前原父已结束的竞争分支。返回失败原因；
    返回 None 表示保护已建立或不适用于本平台。
    """
    if sys.platform != "linux":
        return None
    import ctypes
    import signal

    actual = os.getppid()
    if actual != expected_parent_pid:
        return f"原父进程身份不符: {actual} != {expected_parent_pid}"
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_PDEATHSIG = 1
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        return f"PR_SET_PDEATHSIG 设置失败: errno {ctypes.get_errno()}"
    actual = os.getppid()
    if actual != expected_parent_pid:
        return f"设置后原父进程身份已改变: {actual} != {expected_parent_pid}"
    return None


class _WorkLockHold:
    """取得的报告工作锁；关闭句柄即释放。"""

    def __init__(self, fd: int, path: Path, backend: LockBackend) -> None:
        self.fd = fd
        self.path = path
        self._backend = backend

    def close(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            self._backend.unlock(fd)
        finally:
            self._backend.close(fd)


def acquire_work_lock(
    lock_path: Path,
    wait_seconds: float,
    *,
    backend: LockBackend | None = None,
    clock: Any = time.monotonic,
) -> _WorkLockHold:
    """在启动时限内有界等待取得报告工作锁。

    真正的锁冲突在时限内重试等待；超过时限、打开或加锁发生其他
    系统错误时分别以明确的 WorkLockError 原因失败，不解释为锁
    空闲或普通占用。
    """
    active = backend if backend is not None else default_backend()
    try:
        fd = active.open(Path(lock_path))
    except OSError as error:
        raise WorkLockError(f"报告工作锁文件打开失败: {lock_path}: {error}") from error
    try:
        active.set_inheritable(fd, False)
        deadline = clock() + wait_seconds
        while True:
            try:
                locked = active.try_lock(fd)
            except OSError as error:
                raise WorkLockError(
                    f"报告工作锁加锁失败: {lock_path}: {error}") from error
            if locked:
                return _WorkLockHold(fd, Path(lock_path), active)
            if clock() >= deadline:
                raise WorkLockError(
                    f"报告工作锁被其他报告进程占用，等待 {wait_seconds} 秒超时")
            time.sleep(_LOCK_POLL_SECONDS)
    except BaseException:
        if fd >= 0:
            active.close(fd)
        raise


def classify_worker_error(error: BaseException) -> ErrorKind:
    """状态库与历史解释错误区别于普通报告失败。"""
    return ErrorKind.STATE if isinstance(error, _STATE_ERRORS) else ErrorKind.REPORT


def run_job(job: JobMessage) -> ResultSuccessMessage | ResultFailureMessage:
    """执行一次生成任务并返回携带任务身份的分类结果。

    先核验实际打开的数据库实例身份，再按冻结依据生成并同步
    staging 文件；成功结果在文件写入、摘要计算与同步完成后发
    出。任何失败都转换为分类结果，不向主进程抛出异常。
    """
    observed = job.instance_id
    try:
        observed = _read_instance_id(job.db_path, job.busy_timeout_ms)
        if observed != job.instance_id:
            return ResultFailureMessage(
                job_id=job.job_id, instance_id=observed,
                error_kind=ErrorKind.STATE, error_code="instance_mismatch",
                error_message=(
                    f"数据库实例身份与任务不符: {observed} != {job.instance_id}"))
        generated = generate_report_file(
            GenerationSpec(
                db_path=Path(job.db_path),
                report_id=job.report_id,
                from_wm=job.from_wm,
                to_wm=job.to_wm,
                frozen_event_id=job.frozen_event_id,
                staging_path=Path(job.staging_path),
                entity_batch_size=job.entity_batch_size,
                event_batch_size=job.event_batch_size,
            ),
        )
        return ResultSuccessMessage(
            job_id=job.job_id, instance_id=observed,
            path=str(generated.path), size_bytes=generated.size_bytes,
            sha256=generated.sha256)
    except BaseException as error:
        return ResultFailureMessage(
            job_id=job.job_id, instance_id=observed,
            error_kind=classify_worker_error(error),
            error_code=type(error).__name__,
            error_message=str(error)[:1024] or type(error).__name__)


def _read_instance_id(db_path: str, busy_timeout_ms: int) -> str:
    config = DbConfig(busy_timeout_ms=busy_timeout_ms)
    owned = open_existing(Path(db_path), DbOpenMode.EXISTING_RO, config)
    try:
        return owned.metadata.instance_id
    finally:
        owned.connection.close()


def worker_main(
    connection: Any,
    expected_parent_pid: int,
    lock_path: str,
    startup_deadline_monotonic: float,
) -> int:
    """子进程入口：保护、锁、运行库检查通过后进入任务循环。

    启动失败发送对应阶段与原因后退出；通道结束或收到退出指令时
    释放锁并关闭端点。返回值仅供诊断，主进程以消息判定结果。
    """
    try:
        guard_reason = install_parent_guard(expected_parent_pid)
        if guard_reason is not None:
            _try_send_startup_failure(
                connection, StartupPhase.PARENT_GUARD, guard_reason)
            return 3
        remaining = max(
            startup_deadline_monotonic - time.monotonic() - _STARTUP_REPORT_MARGIN,
            0.0)
        try:
            lock = acquire_work_lock(Path(lock_path), remaining)
        except WorkLockError as error:
            _try_send_startup_failure(connection, StartupPhase.WORK_LOCK, str(error))
            return 3
        try:
            try:
                ensure_runtime_library()
            except StateDatabaseError as error:
                _try_send_startup_failure(
                    connection, StartupPhase.RUNTIME_CHECK, str(error))
                return 3
            connection.send_bytes(encode_message(ReadyMessage()))
            while True:
                try:
                    payload = connection.recv_bytes(MAX_MESSAGE_BYTES + 1)
                except (EOFError, OSError, ValueError):
                    return 0
                try:
                    message = decode_message(payload)
                except MessageProtocolError:
                    return 0
                if isinstance(message, ShutdownMessage):
                    return 0
                if isinstance(message, JobMessage):
                    connection.send_bytes(encode_message(run_job(message)))
                    continue
                # 主进程不会发送就绪或结果：按通道不可靠结束。
                return 0
        finally:
            lock.close()
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _try_send_startup_failure(
    connection: Any, phase: StartupPhase, reason: str
) -> None:
    try:
        connection.send_bytes(encode_message(
            StartupFailedMessage(phase=phase, reason=reason[:256])))
    except (OSError, ValueError, MessageProtocolError):
        pass  # 通道不可用时启动失败无法送达，主进程按超时收场。
