"""SW-04 日志线程取消后的实际结果、必要清理与会话收场。"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.bootstrap import lifecycle
from camctl.logging_runtime import copies
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.copies import MarkerStage, MarkerStore, SaveOutcome
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.messages import ErrorKind, ResultFailureMessage, ResultSuccessMessage
from camctl.reporting.publication import report_file_name
from camctl.reporting.supervisor import (
    GenerationOutcomeKind,
    WorkerGeneration,
    WorkerSupervisor,
)
from camctl.session.locks import SessionLockConflictError, acquire_session
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.supervision import Supervisor

from .test_log_copy_diagnostics import _runtime, _seed_two_pending_reports


def _capture_failure_log_service(monkeypatch) -> dict[str, object]:
    original = lifecycle._failure_log_wiring
    captured: dict[str, object] = {}

    def capture(deps):
        service, factory = original(deps)
        captured["service"] = service
        return service, factory

    monkeypatch.setattr(lifecycle, "_failure_log_wiring", capture)
    return captured


def _session_lock_is_held(path: Path) -> bool:
    try:
        lease = acquire_session(path)
    except SessionLockConflictError:
        return True
    lease.close()
    return False


async def _assert_stays_pending(task: asyncio.Task, *, timeout: float = 0.05) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    assert not task.done()


def _install_controlled_source_stop(monkeypatch, deps):
    """把一个受控软件 source 接入默认 run 的 BackgroundFlow 收场入口。"""
    from camctl.bootstrap.background_flow import BackgroundFlow

    source_started = asyncio.Event()
    source_stopped = asyncio.Event()
    source_wait = asyncio.Event()

    async def controlled_source(_context) -> None:
        source_started.set()
        try:
            await source_wait.wait()
        except asyncio.CancelledError:
            source_stopped.set()
            raise

    owner = BackgroundFlow(controlled_source)
    real_assembly = lifecycle._report_assembly

    def assemble_with_controlled_source(target_deps, failure_log, logging_owner):
        flows, supervisor = real_assembly(
            target_deps, failure_log, logging_owner)
        target_deps.background_flows = (
            *target_deps.background_flows, owner)
        return {"controlled_source": owner, **flows}, supervisor

    monkeypatch.setattr(lifecycle, "_report_assembly", assemble_with_controlled_source)
    return source_started, source_stopped


async def test_cancelled_copy_consumes_closed_result_cleans_once_and_holds_locks(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    captured = _capture_failure_log_service(monkeypatch)
    source_started, source_stopped = _install_controlled_source_stop(
        monkeypatch, deps)
    loop = asyncio.get_running_loop()
    copy_returned = asyncio.Event()
    release_copy = threading.Event()
    write_results: list[tuple[object, str | None]] = []
    staging_targets: list[Path] = []
    cleanup_attempts: list[Path] = []
    marker_delete_calls: list[bool] = []
    generated_reports: list[int] = []
    diagnostics: list[str] = []
    diagnostic_observations: list[tuple[bool, bool, bool]] = []
    diagnostic_finished = asyncio.Event()
    recovery_probe: asyncio.Task | None = None
    run_task: asyncio.Task | None = None

    real_copy = FileChannel.copy_with_write

    def copy_then_wait(channel, record, destination):
        result = real_copy(channel, record, destination)
        write_results.append(result)
        staging_targets.append(Path(destination))
        loop.call_soon_threadsafe(copy_returned.set)
        if not release_copy.wait(timeout=10):
            raise TimeoutError("受控日志复制等待未获释放")
        return result

    monkeypatch.setattr(FileChannel, "copy_with_write", copy_then_wait)

    real_unlink = copies._unlink

    def fail_cancelled_copy_cleanup(path):
        target = Path(path)
        if copies.is_copy_file_name(target.name):
            cleanup_attempts.append(target)
            raise OSError("controlled cancellation cleanup failure")
        return real_unlink(path)

    monkeypatch.setattr(copies, "_unlink", fail_cancelled_copy_cleanup)

    real_marker_delete = MarkerStore.delete

    def track_probe_marker_delete(marker):
        marker_delete_calls.append(True)
        if len(marker_delete_calls) > 1:
            raise AssertionError("取消期间不能开始第二次标记删除")
        return real_marker_delete(marker)

    monkeypatch.setattr(MarkerStore, "delete", track_probe_marker_delete)

    async def fail_report_generation(self, job):
        generated_reports.append(job.report_id)
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code="controlled_copy_cancel",
            error_message="controlled report failure for copy cancellation",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    monkeypatch.setattr(WorkerSupervisor, "generate", fail_report_generation)

    real_emit = lifecycle._emit_log_diagnostic

    async def observe_cleanup_diagnostic(target_deps, message, *, stderr_only=False):
        if "controlled cancellation cleanup failure" in message:
            diagnostics.append(message)
            before = (
                asyncio.get_running_loop() is loop,
                _session_lock_is_held(deps.session_lock),
                recovery_probe is not None and not recovery_probe.done(),
            )
            await real_emit(target_deps, message, stderr_only=stderr_only)
            after = (
                asyncio.get_running_loop() is loop,
                _session_lock_is_held(deps.session_lock),
                recovery_probe is not None and not recovery_probe.done(),
            )
            diagnostic_observations.append(before)
            diagnostic_observations.append(after)
            if recovery_probe is not None and not recovery_probe.done():
                recovery_probe.cancel()
                try:
                    await recovery_probe
                except asyncio.CancelledError:
                    pass
            diagnostic_finished.set()
            return
        await real_emit(target_deps, message, stderr_only=stderr_only)

    monkeypatch.setattr(lifecycle, "_emit_log_diagnostic", observe_cleanup_diagnostic)

    try:
        run_task = asyncio.create_task(lifecycle.execute_command(deps, None))
        await asyncio.wait_for(source_started.wait(), timeout=10)
        await asyncio.wait_for(copy_returned.wait(), timeout=10)

        assert generated_reports == [first_report.report_id]
        assert second_report.report_id != first_report.report_id
        assert len(write_results) == len(staging_targets) == 1
        write_result, copy_error = write_results[0]
        assert copy_error is None
        assert write_result.copy_handles_closed is True
        assert write_result.copy_source_close_error is None
        assert write_result.copy_target_close_error is None

        target = staging_targets[0]
        assert target.parent == Path(deps.config.paths.staging) / "logs"
        assert target.is_file()
        assert target.stat().st_size > 0
        assert b"controlled report failure for copy cancellation" in target.read_bytes()
        marker = MarkerStore(target.parent)
        assert marker.check().state is not None
        assert marker.check().state.stage is MarkerStage.TRIGGERED
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []

        run_task.cancel("copy cancellation primary")
        await _assert_stays_pending(run_task)
        await asyncio.wait_for(source_stopped.wait(), timeout=10)
        assert _session_lock_is_held(deps.session_lock)
        assert not release_copy.is_set()
        assert target.exists()
        assert cleanup_attempts == []
        assert diagnostics == []

        service = captured["service"]

        async def probe_service_lock():
            return await service.on_report_recovered()

        recovery_probe = asyncio.create_task(probe_service_lock())
        await _assert_stays_pending(recovery_probe)
        assert marker_delete_calls == []

        assert run_task.cancel("copy cancellation repeated") is True
        await _assert_stays_pending(run_task)
        assert _session_lock_is_held(deps.session_lock)

        release_copy.set()
        await asyncio.wait_for(diagnostic_finished.wait(), timeout=10)
        assert cleanup_attempts == [target]
        assert len(diagnostics) == 1
        assert len(diagnostic_observations) == 2
        assert all(
            on_main_loop and session_locked and service_locked
            for on_main_loop, session_locked, service_locked
            in diagnostic_observations
        )

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(run_task, timeout=10)
        assert cancelled.value.args == ("copy cancellation primary",)

        assert generated_reports == [first_report.report_id]
        assert marker_delete_calls == []
        assert target.exists()
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        marker_state = marker.check().state
        assert marker_state is not None
        assert marker_state.stage is MarkerStage.TRIGGERED
        assert "controlled cancellation cleanup failure" in diagnostics[0]
        assert _session_lock_is_held(deps.session_lock) is False
    finally:
        release_copy.set()
        if recovery_probe is not None and not recovery_probe.done():
            recovery_probe.cancel()
            try:
                await recovery_probe
            except asyncio.CancelledError:
                pass
        if run_task is not None and not run_task.done():
            run_task.cancel("test cleanup")
            try:
                await asyncio.wait_for(run_task, timeout=10)
            except asyncio.CancelledError:
                pass
        lifecycle.close_runtime(deps)


async def test_cancelled_recovery_delete_consumes_result_and_diagnostic_before_unlock(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    captured = _capture_failure_log_service(monkeypatch)
    source_started, source_stopped = _install_controlled_source_stop(
        monkeypatch, deps)
    logs_dir = Path(deps.config.paths.staging) / "logs"
    marker_path = logs_dir / copies.MARKER_NAME
    marker = MarkerStore(logs_dir)
    created = marker.create("recovery-delete-cancel", "log-copy-recovery-delete-cancel.log")
    assert created.outcome is SaveOutcome.SAVED

    loop = asyncio.get_running_loop()
    sync_started = asyncio.Event()
    release_sync = threading.Event()
    marker_delete_calls: list[bool] = []
    generated_reports: list[int] = []
    diagnostics: list[str] = []
    diagnostic_observations: list[tuple[bool, bool, bool]] = []
    diagnostic_finished = asyncio.Event()
    recovery_probe: asyncio.Task | None = None
    run_task: asyncio.Task | None = None

    real_marker_delete = MarkerStore.delete

    def track_single_marker_delete(store):
        marker_delete_calls.append(True)
        if len(marker_delete_calls) > 1:
            raise AssertionError("取消期间不能重复删除正式标记")
        return real_marker_delete(store)

    monkeypatch.setattr(MarkerStore, "delete", track_single_marker_delete)

    real_sync = copies._fsync_directory

    def pause_marker_directory_sync(path):
        if Path(path).resolve() == logs_dir.resolve():
            loop.call_soon_threadsafe(sync_started.set)
            if not release_sync.wait(timeout=10):
                raise TimeoutError("受控标记目录同步等待未获释放")
            raise OSError("controlled recovery marker directory sync failure")
        return real_sync(path)

    monkeypatch.setattr(copies, "_fsync_directory", pause_marker_directory_sync)

    async def generate_valid_report(self, job):
        generated_reports.append(job.report_id)
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
            )
        )
        canonical = generated.path.with_name(
            report_file_name(job.report_id, generated.sha256))
        os.replace(generated.path, canonical)
        success = ResultSuccessMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            path=str(canonical),
            size_bytes=generated.size_bytes,
            sha256=generated.sha256,
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.SUCCESS,
            job_id=job.job_id,
            success=success,
        )

    monkeypatch.setattr(WorkerSupervisor, "generate", generate_valid_report)

    real_emit = lifecycle._emit_log_diagnostic

    async def observe_delete_diagnostic(target_deps, message, *, stderr_only=False):
        if "controlled recovery marker directory sync failure" in message:
            diagnostics.append(message)
            before = (
                asyncio.get_running_loop() is loop,
                _session_lock_is_held(deps.session_lock),
                recovery_probe is not None and not recovery_probe.done(),
            )
            await real_emit(target_deps, message, stderr_only=stderr_only)
            after = (
                asyncio.get_running_loop() is loop,
                _session_lock_is_held(deps.session_lock),
                recovery_probe is not None and not recovery_probe.done(),
            )
            diagnostic_observations.extend((before, after))
            if recovery_probe is not None and not recovery_probe.done():
                recovery_probe.cancel()
                try:
                    await recovery_probe
                except asyncio.CancelledError:
                    pass
            diagnostic_finished.set()
            return
        await real_emit(target_deps, message, stderr_only=stderr_only)

    monkeypatch.setattr(lifecycle, "_emit_log_diagnostic", observe_delete_diagnostic)

    try:
        run_task = asyncio.create_task(lifecycle.execute_command(deps, None))
        await asyncio.wait_for(source_started.wait(), timeout=10)
        await asyncio.wait_for(sync_started.wait(), timeout=10)

        assert generated_reports
        assert len(marker_delete_calls) == 1
        assert not marker_path.exists()
        run_task.cancel("marker delete cancellation primary")
        await _assert_stays_pending(run_task)
        await asyncio.wait_for(source_stopped.wait(), timeout=10)
        assert _session_lock_is_held(deps.session_lock)
        assert not release_sync.is_set()
        assert diagnostics == []

        service = captured["service"]
        recovery_probe = asyncio.create_task(service.on_report_recovered())
        await _assert_stays_pending(recovery_probe)
        assert marker_delete_calls == [True]

        assert run_task.cancel("marker delete cancellation repeated") is True
        await _assert_stays_pending(run_task)
        assert _session_lock_is_held(deps.session_lock)

        release_sync.set()
        await asyncio.wait_for(diagnostic_finished.wait(), timeout=10)
        assert marker_delete_calls == [True]
        assert not marker_path.exists()
        assert len(diagnostics) == 1
        assert len(diagnostic_observations) == 2
        assert all(
            on_main_loop and session_locked and service_locked
            for on_main_loop, session_locked, service_locked
            in diagnostic_observations
        )

        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(run_task, timeout=10)
        assert cancelled.value.args == ("marker delete cancellation primary",)
        assert marker_delete_calls == [True]
        assert marker.check().presence.value == "absent"
        assert "controlled recovery marker directory sync failure" in diagnostics[0]
        assert _session_lock_is_held(deps.session_lock) is False
    finally:
        release_sync.set()
        if recovery_probe is not None and not recovery_probe.done():
            recovery_probe.cancel()
            try:
                await recovery_probe
            except asyncio.CancelledError:
                pass
            except AssertionError:
                pass
        if run_task is not None and not run_task.done():
            run_task.cancel("test cleanup")
            try:
                await asyncio.wait_for(run_task, timeout=10)
            except asyncio.CancelledError:
                pass
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("exit_path", ["late-clock", "fatal-state"],
                         ids=["late-clock", "fatal-state"])
@pytest.mark.parametrize("parent_cancelled", [False, True], ids=["no-parent", "parent"])
async def test_business_internal_control_still_drains_logs_before_unlock(
        tmp_path: Path, monkeypatch, parent_cancelled: bool,
        exit_path: str) -> None:
    """文件执行器的内部取消原因穿过组合和中间任务后仍先收场日志。"""
    from camctl.bootstrap.background_flow import BackgroundFlow
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.session.service import StateDbFailure, _RequiredPhaseWaiter

    deps = await _runtime(tmp_path)
    loop = asyncio.get_running_loop()
    logs_dir = Path(deps.config.paths.staging) / "logs"
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    file_task_started = asyncio.Event()
    actual_control_raised = asyncio.Event()
    flow_error_observed = asyncio.Event()
    logger_settle_started = asyncio.Event()
    internal_control = asyncio.CancelledError("file owner internal control")
    internal_control.add_note("actual-file-owner-note")
    file_errors: list[BaseException] = []
    diagnostics: list[str] = []
    logger_owners: list[LoggingOwner] = []
    logger_settle_calls: list[bool] = []
    parent_errors: list[asyncio.CancelledError] = []
    run_task: asyncio.Task | None = None

    executor = FileTaskExecutor(Supervisor())

    async def actual_file_operation(control) -> None:
        file_task_started.set()
        await control.requested()
        actual_control_raised.set()
        raise internal_control

    async_file_task = AsyncFileTask(
        task_id=FileTaskId("sw04-internal-local-control"),
        file_ids=(7404,),
        stage="sw04_internal_control",
        business="SW04 内部控制事实收场",
        body=actual_file_operation,
    )

    async def file_flow(context) -> None:
        try:
            await executor.run_owned_async_file_task(async_file_task)
        except BaseException as error:
            file_errors.append(error)
            flow_error_observed.set()
            raise

    file_owner = BackgroundFlow(file_flow)

    async def trigger_session_exit(context) -> None:
        await file_task_started.wait()
        if exit_path == "late-clock":
            raise ClockBecameUntrusted(
                "controlled transition after file task start")
        raise StateDbFailure("controlled state database fatal after file task start")

    real_listdir = copies._listdir

    def block_startup_scan(path):
        target = Path(path)
        if target == logs_dir:
            loop.call_soon_threadsafe(scan_started.set)
            if not release_scan.wait(timeout=10):
                raise TimeoutError("受控启动扫描等待未获释放")
            raise OSError("controlled startup diagnostic after internal control")
        return real_listdir(path)

    monkeypatch.setattr(copies, "_listdir", block_startup_scan)

    real_logger_start = LoggingOwner.start_session

    async def capture_logger(self):
        logger_owners.append(self)
        await real_logger_start(self)

    monkeypatch.setattr(LoggingOwner, "start_session", capture_logger)

    real_logger_settle = LoggingOwner.settle

    async def observe_logger_settle(self):
        logger_settle_calls.append(True)
        logger_settle_started.set()
        await real_logger_settle(self)

    monkeypatch.setattr(LoggingOwner, "settle", observe_logger_settle)

    real_emit = lifecycle._emit_log_diagnostic

    async def gate_startup_diagnostic(target_deps, message, *, stderr_only=False):
        if "controlled startup diagnostic after internal control" in message:
            diagnostics.append(message)
            diagnostic_started.set()
            await release_diagnostic.wait()
        await real_emit(target_deps, message, stderr_only=stderr_only)

    monkeypatch.setattr(lifecycle, "_emit_log_diagnostic", gate_startup_diagnostic)

    real_record_parent = _RequiredPhaseWaiter.record_parent_cancel

    def record_first_parent(self, error):
        if self.parent_cancel is None:
            parent_errors.append(error)
        real_record_parent(self, error)

    monkeypatch.setattr(
        _RequiredPhaseWaiter, "record_parent_cancel", record_first_parent)

    try:
        run_task = asyncio.create_task(lifecycle.execute_command(
            deps,
            None,
            flows={
                "file-owner": file_owner,
                "session-exit": trigger_session_exit,
            },
        ))

        await asyncio.wait_for(scan_started.wait(), timeout=10)
        await asyncio.wait_for(file_task_started.wait(), timeout=10)
        await asyncio.wait_for(flow_error_observed.wait(), timeout=10)
        assert len(file_errors) == 1
        assert isinstance(file_errors[0], asyncio.CancelledError)
        assert file_errors[0].__cause__ is internal_control
        await asyncio.wait_for(actual_control_raised.wait(), timeout=10)
        await asyncio.wait_for(logger_settle_started.wait(), timeout=10)
        await _assert_stays_pending(run_task)
        assert _session_lock_is_held(deps.session_lock)
        assert logger_owners and logger_owners[0].stop_requested()
        assert not release_scan.is_set()
        assert not diagnostic_started.is_set()
        assert diagnostics == []

        release_scan.set()
        await asyncio.wait_for(diagnostic_started.wait(), timeout=10)
        assert _session_lock_is_held(deps.session_lock)
        assert logger_settle_calls == [True]

        if parent_cancelled:
            run_task.cancel("first observer cancellation")
            await _assert_stays_pending(run_task)
            run_task.cancel("repeated observer cancellation")
            await _assert_stays_pending(run_task)
            assert len(parent_errors) == 1
            first_parent = parent_errors[0]
            assert first_parent.args == ("first observer cancellation",)
            assert logger_owners[0].startup_stop_requested()
        else:
            assert parent_errors == []

        release_diagnostic.set()
        if parent_cancelled:
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(run_task, timeout=10)
            assert caught.value is first_parent
            assert any("file owner internal control" in note
                       for note in first_parent.__notes__)
            assert any("actual-file-owner-note" in note
                       for note in first_parent.__notes__)
        else:
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(run_task, timeout=10)
            assert caught.value is internal_control
            assert caught.value.__notes__ == ["actual-file-owner-note"]

        assert logger_settle_calls == [True]
        assert _session_lock_is_held(deps.session_lock) is False
        assert diagnostics and (
            "controlled startup diagnostic after internal control" in diagnostics[0]
        )
    finally:
        release_scan.set()
        release_diagnostic.set()
        if run_task is not None and not run_task.done():
            run_task.cancel("test cleanup")
            try:
                await asyncio.wait_for(run_task, timeout=10)
            except asyncio.CancelledError:
                pass
        lifecycle.close_runtime(deps)
