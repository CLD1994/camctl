"""报告生成子进程组件集成测试：真实 spawn、管道、锁与分类结果。

真实子进程经 spawn 启动，建立保护、取得工作锁并检查运行库后通
知道绪；冻结依据驱动的任务在子进程内完成生成并返回分类结果。
覆盖成功复用、状态库错误分类回收、锁占用时的启动失败与正常收
尾确认；Linux 父死亡保护按平台边界不在 Windows 验证。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.messages import (
    ErrorKind,
    JobMessage,
    StartupPhase,
    new_job_id,
)
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerSupervisor
from camctl.reporting.worker import (
    acquire_work_lock,
    report_lock_path,
    run_job,
)

from ..history.test_report_scope import _finish_action, _submit
from ..persistence.test_runtime import _create_valid_database
from ..reporting.test_generation import _freeze


@pytest_asyncio.fixture
async def frozen_report(tmp_path):
    """真实受理、完成并冻结一份报告；返回库路径、实例身份与冻结事实。"""
    db_path = tmp_path / "state.db"
    _create_valid_database(db_path)
    owned = open_existing(db_path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await _submit(owned, tmp_path, "1")
        await _finish_action(owned)
        report = _freeze(owned)
    finally:
        owned.connection.close()
    reader = open_existing(db_path, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        instance_id = reader.metadata.instance_id
    finally:
        reader.connection.close()
    yield db_path, instance_id, report, tmp_path


def _job(db_path: Path, instance_id: str, report, tmp_path: Path,
         name: str = "worker-report-1.json", **overrides) -> JobMessage:
    values = dict(
        job_id=new_job_id(),
        report_id=report.report_id,
        from_wm=report.from_wm,
        to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        instance_id=instance_id,
        db_path=str(db_path),
        staging_path=str(tmp_path / "staging" / name),
        staging_root="/srv/camctl/staging", ready_root="/srv/camctl/ready",
        processing_root="/srv/camctl/processing",
        entity_batch_size=16,
        event_batch_size=128,
        busy_timeout_ms=9000,
    )
    values.update(overrides)
    return JobMessage(**values)


def _limits() -> MaintenanceLimits:
    return MaintenanceLimits(
        startup_seconds=20.0, total_seconds=60.0, stop_grace_seconds=10.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["staging_root", "ready_root", "processing_root"])
async def test_worker_rejects_each_changed_binding_before_staging(frozen_report, changed):
    db_path, instance_id, report, home = frozen_report
    job = _job(db_path, instance_id, report, home, **{changed: str(home / "changed")})

    result = run_job(job)

    assert result.error_kind is ErrorKind.REPORT
    assert result.error_code == "configuration_error"
    assert changed.removesuffix("_root") in result.error_message
    assert str(home / "changed") in result.error_message
    assert not Path(job.staging_path).exists()
    assert not (home / "staging").exists()
    assert not (home / "changed").exists()


@pytest.mark.asyncio
async def test_worker_keeps_missing_database_as_state_error(frozen_report):
    db_path, instance_id, report, home = frozen_report
    job = replace(_job(db_path, instance_id, report, home), db_path=str(home / "missing.db"))

    result = run_job(job)

    assert result.error_kind is ErrorKind.STATE
    assert result.error_code == "DatabaseMissingError"
    assert not (home / "missing.db").exists()
    assert not (home / "staging").exists()


@pytest.mark.asyncio
class TestRealWorkerProcess:
    async def test_real_worker_generates_and_reuses(
        self, frozen_report,
    ) -> None:
        db_path, instance_id, report, tmp_path = frozen_report
        supervisor = WorkerSupervisor(
            limits=_limits(), lock_path=str(report_lock_path(db_path)))
        try:
            startup = await supervisor.start()
            assert startup.ready, startup.detail
            assert supervisor.reusable

            first = await supervisor.generate(
                _job(db_path, instance_id, report, tmp_path))
            assert first.kind is GenerationOutcomeKind.SUCCESS, first.detail
            assert first.success is not None
            payload = Path(first.success.path).read_bytes()
            assert len(payload) == first.success.size_bytes
            assert hashlib.sha256(payload).hexdigest() == first.success.sha256
            # 生成成功且进程健康：保留复用。
            assert supervisor.reusable

            second = await supervisor.generate(
                _job(db_path, instance_id, report, tmp_path,
                     name="worker-report-2.json"))
            assert second.kind is GenerationOutcomeKind.SUCCESS
            assert second.success is not None
        finally:
            shutdown = await supervisor.stop()
        assert shutdown.exitcode == 0
        assert not shutdown.forced

    async def test_state_failure_retires_real_worker(self, frozen_report) -> None:
        db_path, instance_id, report, tmp_path = frozen_report
        supervisor = WorkerSupervisor(
            limits=_limits(), lock_path=str(report_lock_path(db_path)))
        try:
            assert (await supervisor.start()).ready
            outcome = await supervisor.generate(
                _job(db_path, instance_id, report, tmp_path, report_id=99))
            assert outcome.kind is GenerationOutcomeKind.STATE_FAILURE
            assert outcome.failure is not None
            assert outcome.failure.error_kind is ErrorKind.STATE
            # 明确失败后子进程被结束，不再复用。
            assert not supervisor.reusable
        finally:
            await supervisor.stop()

    async def test_held_work_lock_fails_startup_then_succeeds_after_release(
        self, frozen_report,
    ) -> None:
        db_path, _instance_id, _report, _tmp = frozen_report
        lock_path = report_lock_path(db_path)
        held = acquire_work_lock(lock_path, 0.1)
        # 较短的启动时限让锁等待失败按时送达，同时容纳真实 spawn 开销。
        bounded = MaintenanceLimits(
            startup_seconds=5.0, total_seconds=60.0, stop_grace_seconds=10.0)
        try:
            supervisor = WorkerSupervisor(
                limits=bounded, lock_path=str(lock_path))
            try:
                startup = await supervisor.start()
                assert not startup.ready
                assert startup.failure is not None
                assert startup.failure.phase is StartupPhase.WORK_LOCK
            finally:
                await supervisor.stop()
        finally:
            held.close()
        supervisor = WorkerSupervisor(limits=_limits(), lock_path=str(lock_path))
        try:
            assert (await supervisor.start()).ready
        finally:
            shutdown = await supervisor.stop()
        assert shutdown.exitcode == 0


def _worker_with_incompatible_runtime(child_end, parent_pid, lock_path):
    """在真实子进程里注入运行库故障，保留父身份保护和工作锁。"""
    import time

    from camctl.persistence.runtime import RuntimeLibraryError
    from camctl.reporting import worker as worker_module

    def incompatible() -> None:
        raise RuntimeLibraryError(
            "Python 实际链接的 SQLite 3.30.1 不满足统一运行条件")

    worker_module.ensure_runtime_library = incompatible
    raise SystemExit(worker_module.worker_main(
        child_end, parent_pid, lock_path, time.monotonic() + 5.0))


class TestStartupFailures:
    """V-03：运行库检查失败的启动报告与退出；责任由主进程按消息保留。"""

    def test_runtime_check_failure_sends_startup_failure(
            self, tmp_path) -> None:
        import multiprocessing
        import os
        from camctl.reporting.messages import decode_message
        context = multiprocessing.get_context("spawn")
        parent_end, child_end = context.Pipe(duplex=True)
        process = context.Process(
            target=_worker_with_incompatible_runtime,
            args=(child_end, os.getpid(), str(tmp_path / "state.db.report.lock")))
        try:
            process.start()
            child_end.close()
            assert parent_end.poll(10.0), "子进程未报告运行库检查失败"
            failure = decode_message(parent_end.recv_bytes())
            assert failure.phase is StartupPhase.RUNTIME_CHECK
            assert "不满足统一运行条件" in failure.reason
            process.join(10.0)
            assert process.exitcode == 3
        finally:
            child_end.close()
            parent_end.close()
            if process.is_alive():
                process.kill()
                process.join(10.0)
            process.close()


class TestRunJob:
    def test_run_job_returns_success_with_file_facts(self, frozen_report) -> None:
        db_path, instance_id, report, tmp_path = frozen_report
        job = _job(db_path, instance_id, report, tmp_path)
        result = run_job(job)
        assert result.job_id == job.job_id
        assert result.size_bytes > 0
        # 文件已按报告身份与摘要改用规范文件名；临时名不再存在。
        generated = Path(result.path).read_bytes()
        assert Path(result.path).name == (
            f"status-report-{report.report_id}-{result.sha256}.json")
        assert not Path(job.staging_path).exists()
        assert hashlib.sha256(generated).hexdigest() == result.sha256
        assert result.size_bytes == len(generated)

    def test_instance_mismatch_is_state_failure(self, frozen_report) -> None:
        db_path, _instance_id, report, tmp_path = frozen_report
        job = _job(db_path, "0" * 32, report, tmp_path)
        result = run_job(job)
        assert result.error_kind is ErrorKind.STATE
        assert result.error_code == "instance_mismatch"

    def test_unknown_report_is_state_failure(self, frozen_report) -> None:
        db_path, instance_id, report, tmp_path = frozen_report
        job = _job(db_path, instance_id, report, tmp_path, report_id=99)
        result = run_job(job)
        assert result.error_kind is ErrorKind.STATE
        assert result.error_code == "ConsistencyError"
