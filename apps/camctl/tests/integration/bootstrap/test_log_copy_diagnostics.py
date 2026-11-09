"""默认 run 装配中的日志副本诊断、回退与原会话结果。"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
from pathlib import Path

import concurrent_log_handler as clh
import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import (
    build_runtime, close_runtime, execute_command,
)
from camctl.logging_runtime import copies
from camctl.logging_runtime.clh_adapter import FileChannel, WriteResult
from camctl.logging_runtime.copies import MarkerStage, SaveOutcome
from integration.logging_runtime.sync_support import FileSyncProbe, fail_directory_sync
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.supervisor import (
    GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor,
)

from .test_composition import RecordingCatalog


from ..support.bootstrap_log_cases import (
    runtime as _runtime, seed_two_pending_reports as _seed_two_pending_reports,
    run_with_report_failure as _run_with_report_failure, generate_valid_report,
    observe_failure_service,
)
from ..support.distribution import prepare_project_metadata


def _lock_path(log_path: Path) -> Path:
    directory, name = clh.ConcurrentRotatingFileHandler.baseLockFilename(str(log_path))
    return Path(directory) / name


def _is_lock_file(stream, path: Path) -> bool:
    opened = os.fstat(stream.fileno())
    named = path.stat()
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


def _session_failures(text):
    """读取实际通道中的会话失败事实；stderr 也可能含非 JSON 诊断。"""
    records = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if isinstance(record, dict) and record.get("event") == "session_failed":
            records.append(record)
    return records


def _assert_report_failure(text):
    failures = _session_failures(text)
    assert len(failures) == 1
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["command"] == "run"
    assert failures[0]["reason"] == "report_error"
    assert isinstance(failures[0]["details"], dict)


async def test_report_copy_sync_failure_keeps_original_receipt_and_consumes_failure(
        tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime import diagnostics

    prepare_project_metadata(tmp_path, monkeypatch)

    deps = await _runtime(tmp_path)
    path = Path(deps.config.paths.log_file)
    before = ([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
              if path.exists() else [])
    probe = FileSyncProbe(monkeypatch, path, fail="file")
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    states, requests, receipts = [], [], []
    original_record_error = diagnostics.record_error
    original_deliver = copies.FailureLogService.on_report_failure_with_factory

    def observe_error(error, **kwargs):
        state = original_record_error(error, **kwargs)
        if all(state is not captured for captured in states):
            states.append(state)
        return state

    async def observe_delivery(service, factory, *, diagnose=None):
        def request():
            value = factory()
            requests.append(value)
            return value

        receipt = await original_deliver(service, request, diagnose=diagnose)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(diagnostics, "record_error", observe_error)
    monkeypatch.setattr(copies.FailureLogService, "on_report_failure_with_factory", observe_delivery)
    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)
    assert outcome.reason == "report_error" and report_called["count"] == 1
    assert len(requests) == len(receipts) == 1
    copied = receipts[0]
    assert copied.outcome is copies.CopyOutcome.COPY_FAILED
    assert copied.write_result.appended is True and copied.write_result.durable is False
    assert "controlled original file sync failure" in copied.write_result.sync_error
    state, = [state for state in states
              if diagnostics.diagnostic_record_for(state) is requests[0].trigger]
    shared = diagnostics.submit_diagnostic(state)
    assert diagnostics.submit_diagnostic(state) is shared
    assert shared.write_result.delivered is False and shared.write_result.appended is True
    assert shared.write_result.durable is False and shared.write_result.fallback_written is False
    assert probe.events.count(("file", "started")) == 1
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[:len(before)] == before
    trigger = json.loads(requests[0].trigger.rendered_message())
    assert [record for record in records[len(before):]
            if record.get("stage") == "report_generate"] == [trigger]
    assert "controlled report generation failure" in json.dumps(records[-1])
    assert deps.log_path_unsafe is True and deps.log_runtime.path_unsafe is True
    assert deps.log_runtime.channel.disabled is True
    assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
    diagnostic = stderr.getvalue()
    assert "报告失败的日志副本诊断" in diagnostic
    assert "controlled_report_failure" in diagnostic
    assert "controlled report generation failure" in diagnostic
    assert "controlled original file sync failure" in diagnostic
    assert "appended=True" in diagnostic and "durable=False" in diagnostic
    _assert_report_failure(diagnostic)
    assert stdout.getvalue() == ""


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_default_run_records_marker_progress_failure(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    logs_dir = Path(deps.config.paths.staging).expanduser() / "logs"
    fail_directory_sync(monkeypatch, logs_dir, call=2,
                        message="bootstrap progress sync failed")

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    marker = Path(deps.config.paths.staging) / "logs" / copies.MARKER_NAME
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["stage"] == MarkerStage.PUBLISHED.value
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert "marker_progress[copied]" in log
    assert "placed_unsynced" in log
    assert "bootstrap progress sync failed" in log
    assert state["round_id"] in log and state["copy_name"] in log


async def test_default_run_keeps_published_copy_when_progress_temp_cleanup_fails(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    logs_dir = Path(deps.config.paths.staging).expanduser() / "logs"
    original_open, original_write = os.open, os.write
    original_fsync, original_close, original_unlink = os.fsync, os.close, os.unlink
    write_calls = {"count": 0}
    marker_fds: dict[int, Path] = {}
    failed_temp: dict[str, Path] = {}
    closed_temps: list[Path] = []
    cleanup_observations = []
    primary_error = OSError("progress marker write failed after sync")

    def track_marker_temp(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        candidate = Path(path)
        if candidate.parent == logs_dir and copies.is_marker_temp_name(candidate.name):
            marker_fds[fd] = candidate
            write_calls["count"] += 1
        return fd

    def fail_copied_progress_after_real_write(fd, data):
        written = original_write(fd, data)
        if fd in marker_fds and write_calls["count"] == 2:
            assert written == len(data)
            assert json.loads(bytes(data))["stage"] == "copied"
            original_fsync(fd)
            failed_temp["path"] = marker_fds[fd]
            raise primary_error
        return written

    def close_marker_temp(fd):
        path = marker_fds.pop(fd, None)
        result = original_close(fd)
        if path is not None:
            closed_temps.append(path)
        return result

    def fail_owned_temp_cleanup(path, *args, **kwargs):
        if Path(path) == failed_temp.get("path"):
            cleanup_observations.append((Path(path) in closed_temps, sys.exception()))
            raise OSError("progress temp cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", track_marker_temp)
    monkeypatch.setattr(os, "write", fail_copied_progress_after_real_write)
    monkeypatch.setattr(os, "close", close_marker_temp)
    monkeypatch.setattr(os, "unlink", fail_owned_temp_cleanup)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert write_calls["count"] == 3
    assert len(cleanup_observations) == 1
    assert cleanup_observations[0][0] is True
    assert cleanup_observations[0][1] is primary_error
    assert failed_temp["path"].is_file()
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()
    marker_path = (Path(deps.config.paths.staging).expanduser()
                   / "logs" / copies.MARKER_NAME)
    marker_state = json.loads(marker_path.read_text(encoding="utf-8"))
    temp_state = json.loads(failed_temp["path"].read_bytes())
    assert marker_state["stage"] == MarkerStage.PUBLISHED.value
    assert temp_state["stage"] == MarkerStage.COPIED.value
    assert (temp_state["round_id"], temp_state["copy_name"]) == (
        marker_state["round_id"], marker_state["copy_name"])
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert "marker_progress[copied]: outcome=failed_not_created" in log
    assert "progress marker write failed after sync" in log
    assert "progress temp cleanup denied" in log
    assert failed_temp["path"].name in log
    assert "temp_cleanup=failed" in log


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_default_run_reports_saved_marker_close_error_and_keeps_report_result(
        tmp_path: Path, monkeypatch) -> None:
    import os

    deps = await _runtime(tmp_path)
    logs_dir = (Path(deps.config.paths.staging).expanduser() / "logs").resolve()
    real_open = os.open
    real_fsync = os.fsync
    real_close = os.close
    marker_directory_fds: set[int] = set()
    fsync_calls: list[int] = []
    fsynced_fds: set[int] = set()
    close_error = OSError("marker directory close failed after sync")
    close_injections = {"count": 0}
    close_followed_sync = {"value": False}

    def track_marker_directory(path, flags, *args):
        fd = real_open(path, flags, *args)
        if Path(path).resolve() == logs_dir:
            marker_directory_fds.add(fd)
        return fd

    def sync_marker_directory(fd: int) -> None:
        if fd in marker_directory_fds:
            fsync_calls.append(fd)
            fsynced_fds.add(fd)
        real_fsync(fd)

    def close_marker_directory(fd: int) -> None:
        if fd in marker_directory_fds and close_injections["count"] == 0:
            marker_directory_fds.remove(fd)
            real_close(fd)
            close_injections["count"] += 1
            close_followed_sync["value"] = fd in fsynced_fds
            raise close_error
        marker_directory_fds.discard(fd)
        real_close(fd)

    monkeypatch.setattr(copies.os, "open", track_marker_directory)
    monkeypatch.setattr(copies.os, "fsync", sync_marker_directory)
    monkeypatch.setattr(copies.os, "close", close_marker_directory)
    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert close_injections["count"] == 1
    assert fsync_calls
    assert close_followed_sync["value"] is True
    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert "marker_create: outcome=saved" in log
    assert "marker directory close failed after sync" in log
    assert "report_error" in log


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_default_recovery_reports_delete_close_error_after_confirmed_sync(
        tmp_path: Path, monkeypatch) -> None:
    import os
    from camctl.logging_runtime.copies import MarkerStore, SaveOutcome

    deps = await _runtime(tmp_path)
    logs_dir = Path(deps.config.paths.staging) / "logs"
    marker = MarkerStore(logs_dir)
    assert marker.create("recovery-close", "log-copy-recovery-close.log").outcome is SaveOutcome.SAVED
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    directory_fds: set[int] = set()
    synced_fds: set[int] = set()
    close_followed_sync = []

    def open_directory(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == logs_dir:
            directory_fds.add(fd)
        return fd

    def sync_directory(fd):
        result = real_fsync(fd)
        if fd in directory_fds:
            synced_fds.add(fd)
        return result

    def close_directory_then_report_error(fd):
        tracked = fd in directory_fds
        if tracked:
            directory_fds.remove(fd)
        real_close(fd)
        if tracked:
            close_followed_sync.append(fd in synced_fds)
            synced_fds.discard(fd)
            raise OSError("recovery directory close result unconfirmed")

    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "fsync", sync_directory)
    monkeypatch.setattr(os, "close", close_directory_then_report_error)
    monkeypatch.setattr(WorkerSupervisor, "generate", generate_valid_report)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)
    assert outcome.succeeded is True
    assert close_followed_sync == [True]
    assert list(Path(deps.config.paths.ready).glob("status-report-*.json"))
    assert marker.check().presence.value == "absent"
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert "报告日志标记恢复删除诊断" in log and "outcome=deleted" in log
    assert "recovery directory close result unconfirmed" in log


async def test_recovery_exception_diagnostic_formats_error_once(
        tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime.copies import MarkerStore
    deps = await _runtime(tmp_path)

    def failed_delete(_marker):
        raise RuntimeError("recovery delete raised unexpectedly")

    monkeypatch.setattr(MarkerStore, "delete", failed_delete)
    monkeypatch.setattr(WorkerSupervisor, "generate", generate_valid_report)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)
    assert outcome.succeeded is True
    assert list(Path(deps.config.paths.ready).glob("status-report-*.json"))
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert log.count("outcome=unknown; error=RuntimeError: recovery delete raised unexpectedly") == 1
    assert "error=error:" not in log


@pytest.mark.parametrize("check_failure", ["stat", "read", "invalid"])
async def test_default_run_records_marker_progress_check_failure(
        tmp_path: Path, monkeypatch, check_failure: str) -> None:
    deps = await _runtime(tmp_path)
    marker_path = Path(deps.config.paths.staging).expanduser() / "logs" / copies.MARKER_NAME
    cause = ("marker_content_invalid" if check_failure == "invalid"
             else f"progress {check_failure} denied")
    if check_failure == "stat":
        original = Path.stat
        calls = {"count": 0}

        def fail_progress_stat(path, *args, **kwargs):
            if Path(path) == marker_path:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise PermissionError(cause)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_progress_stat)
        expected_presence = "check_error"
    elif check_failure == "read":
        original = Path.read_bytes
        calls = {"count": 0}

        def fail_progress_read(path):
            if Path(path) == marker_path:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError(cause)
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", fail_progress_read)
        expected_presence = "exists"
    else:
        original = Path.read_bytes
        calls = {"count": 0}

        def return_invalid_progress(path):
            if Path(path) == marker_path:
                calls["count"] += 1
                if calls["count"] == 1:
                    saved = original(path)
                    try:
                        path.write_bytes(b"{invalid progress marker")
                        return original(path)
                    finally:
                        path.write_bytes(saved)
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", return_invalid_progress)
        expected_presence = "exists"

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["stage"] == MarkerStage.PUBLISHED.value
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()
    log_path = Path(deps.config.paths.log_file)
    assert log_path.exists()
    log = log_path.read_text(encoding="utf-8")
    assert "marker_progress[copied]: outcome=failed_not_created" in log
    assert f"marker_check: presence={expected_presence}" in log
    assert cause in log


async def test_first_report_copy_precedes_later_report_state_failure(
        tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from camctl.session.service import StateDbFailure
    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    second_started, release_second = asyncio.Event(), asyncio.Event()
    generated = []

    async def generate(_supervisor, job):
        generated.append(job.report_id)
        if len(generated) == 1:
            return WorkerGeneration(GenerationOutcomeKind.REPORT_FAILURE, job.job_id,
                failure=ResultFailureMessage(job_id=job.job_id, instance_id=job.instance_id,
                    error_kind=ErrorKind.REPORT, error_code="first_report_failed",
                    error_message="first report actual failure detail"))
        second_started.set()
        await release_second.wait()
        raise StateDbFailure("second report state database failed")

    monkeypatch.setattr(WorkerSupervisor, "generate", generate)
    task = asyncio.create_task(execute_command(deps, None))
    try:
        await asyncio.wait_for(second_started.wait(), timeout=10)
        delivered = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
        assert len(delivered) == 1
        content = delivered[0].read_bytes()
        assert b"first report actual failure detail" in content
        assert any(json.loads(line).get("context", {}).get("report_id") == first_report.report_id
                   for line in content.decode().splitlines() if line.startswith("{"))
        release_second.set()
        outcome = await asyncio.wait_for(task, timeout=10)
        assert outcome.reason == "state_db_error"
    finally:
        release_second.set()
        if not task.done():
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
        close_runtime(deps)
    assert generated == [first_report.report_id, second_report.report_id]
    assert "second report state database failed" in Path(deps.config.paths.log_file).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure_stage", ["factory", "delivery"])
async def test_default_run_suspends_copy_attempt_exception_until_recovery(
        tmp_path: Path, monkeypatch, failure_stage: str) -> None:
    from camctl.logging_runtime.copies import FailureLogService, deliver_failure_copy
    from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
    from camctl.session.service import run_session
    from ..support.bootstrap_log_cases import log_session

    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    attempts = {"factory": 0, "delivery": 0}
    captured = []
    original_init = FailureLogService.__init__
    original_failure = FailureLogService.on_report_failure_with_factory

    async def controlled_delivery(request):
        attempts["delivery"] += 1
        if attempts["delivery"] == 1:
            raise RuntimeError("controlled delivery exception")
        return await deliver_failure_copy(request)

    def service_init(service, marker, **kwargs):
        if failure_stage == "delivery":
            kwargs["deliver"] = controlled_delivery
        original_init(service, marker, **kwargs)
        captured.append(service)

    async def failure(service, factory, *, diagnose=None):
        def controlled_factory():
            attempts["factory"] += 1
            if failure_stage == "factory" and attempts["factory"] == 1:
                raise RuntimeError("controlled factory exception")
            return factory()
        return await original_failure(service, controlled_factory, diagnose=diagnose)

    monkeypatch.setattr(FailureLogService, "__init__", service_init)
    monkeypatch.setattr(FailureLogService, "on_report_failure_with_factory", failure)
    try:
        outcome, called = await _run_with_report_failure(deps, monkeypatch)
        assert outcome.reason == "report_error" and called["count"] == 2
        assert first_report.report_id != second_report.report_id
        assert attempts == {"factory": 1, "delivery": int(failure_stage == "delivery")}
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []

        # 后半段只证明公开服务跨一次真实恢复后的新会话协作，不替代默认恢复工厂测试。
        next_deps = build_runtime(CommandMode.RUN, deps.config, catalog=RecordingCatalog())
        async def next_report(_context):
            raise RuntimeError("new round report failure")
        context, service, close = log_session(next_deps, {"report": next_report}, service=captured[0])
        recovery = []
        async def recover(_database):
            recovery.append(await service.on_report_recovered())
        context.on_session_open = recover
        try:
            with error_diagnostics_context(next_deps.log_runtime.channel, lambda text: False, background=True):
                try:
                    result = await run_session(context, None)
                finally:
                    await flush_diagnostics()
            assert result.reason == "report_error"
            assert len(recovery) == 1 and recovery[0].outcome.value in {"deleted", "not_present"}
        finally:
            close()
            close_runtime(next_deps)
        assert attempts == {"factory": 2, "delivery": 2 if failure_stage == "delivery" else 0}
        ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
        assert len(ready) == 1 and b"new round report failure" in ready[0].read_bytes()
    finally:
        close_runtime(deps)


@pytest.mark.parametrize("close_fails", [False, True])
async def test_default_lock_failure_disables_file_logging(
        tmp_path: Path, monkeypatch, close_fails: bool) -> None:

    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    generated: list[int] = []
    attempts = {"factory": 0, "copy": 0, "lock": 0, "close": 0}

    async def fail_generation(self, job):
        generated.append(job.report_id)
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code=f"controlled_failure_{job.report_id}",
            error_message=f"report {job.report_id} actual error",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    original_init = FileChannel.__init__

    def count_helper(channel, *args, **kwargs):
        attempts["factory"] += 1
        original_init(channel, *args, **kwargs)

    original_copy = FileChannel.copy_with_write
    original_lock = clh.lock
    original_close = clh.ConcurrentRotatingFileHandler.close
    active = threading.local()
    primary_path = Path(deps.config.paths.log_file).expanduser()
    lock_path = _lock_path(primary_path)

    def fail_lock(stream, flags):
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            attempts["lock"] += 1
            raise OSError("controlled lock acquisition failure")
        return original_lock(stream, flags)

    def close_with_optional_error(handler):
        if not getattr(active, "copy", False) or Path(handler.baseFilename) != primary_path:
            return original_close(handler)
        attempts["close"] += 1
        result = original_close(handler)
        if close_fails:
            raise OSError("controlled helper close failure")
        return result

    def lock_failure_copy(channel, record, destination):
        attempts["copy"] += 1
        active.copy = True
        try:
            return original_copy(channel, record, destination)
        finally:
            active.copy = False

    monkeypatch.setattr(clh, "lock", fail_lock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close_with_optional_error)
    monkeypatch.setattr(FileChannel, "__init__", count_helper)
    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    monkeypatch.setattr(FileChannel, "copy_with_write", lock_failure_copy)

    try:
        outcome = await execute_command(deps, None)

        assert outcome.reason == "report_error"
        assert generated == [first_report.report_id, second_report.report_id]
        assert attempts["factory"] == attempts["copy"] == attempts["close"] == 1
        assert attempts["lock"] > 1
        assert "Cannot acquire lock" in stderr.getvalue()
        assert deps.failure_channel.disabled is True
        assert deps.failure_channel.closed is (not close_fails)
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.path_unsafe is True
        assert deps.log_runtime.channel.disabled is True
        assert "coordination_lock_failed" in stderr.getvalue()
        if close_fails:
            assert "coordination_close_failed" in stderr.getvalue()
        _assert_report_failure(stderr.getvalue())
        assert stdout.getvalue() == ""
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        log_path = Path(deps.config.paths.log_file)
        if log_path.exists():
            log = log_path.read_text(encoding="utf-8")
            assert "报告失败的日志副本诊断" not in log
            assert not _session_failures(log)
    finally:
        close_runtime(deps)


@pytest.mark.parametrize("failure_step", ["open", "append", "flush"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_default_current_log_io_failure_disables_file_logging(
        tmp_path: Path, monkeypatch, failure_step: str,
        close_fails: bool) -> None:
    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    generated: list[int] = []
    attempts = {
        "factory": 0, "copy": 0, "lock": 0, "open": 0,
        "append": 0, "flush": 0, "unlock": 0, "close": 0,
    }
    receipts = []

    async def fail_generation(self, job):
        generated.append(job.report_id)
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code=f"controlled_failure_{job.report_id}",
            error_message=f"report {job.report_id} actual error",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )


    original_copy = FileChannel.copy_with_write
    original_open = clh.ConcurrentRotatingFileHandler.do_open
    original_lock, original_unlock = clh.lock, clh.unlock
    original_close = clh.ConcurrentRotatingFileHandler.close
    active = threading.local()
    primary_path = Path(deps.config.paths.log_file).expanduser()
    lock_path = _lock_path(primary_path)

    class FaultingStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, message):
            attempts["append"] += 1
            if failure_step == "append":
                raise OSError("controlled current log append failure")
            return self.stream.write(message)

        def flush(self):
            attempts["flush"] += 1
            if failure_step == "flush" and attempts["flush"] == 1:
                raise OSError("controlled current log flush failure")
            return self.stream.flush()

        def close(self):
            return self.stream.close()

    def tracked_lock(stream, flags):
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            attempts["lock"] += 1
        return original_lock(stream, flags)

    def tracked_unlock(stream):
        result = original_unlock(stream)
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            attempts["unlock"] += 1
        return result

    def fail_open(handler, *args, **kwargs):
        if not getattr(active, "copy", False) or Path(handler.baseFilename) != primary_path:
            return original_open(handler, *args, **kwargs)
        attempts["open"] += 1
        if failure_step == "open":
            raise OSError("controlled current log open failure")
        return FaultingStream(original_open(handler, *args, **kwargs))

    def close_with_optional_error(handler):
        if not getattr(active, "copy", False) or Path(handler.baseFilename) != primary_path:
            return original_close(handler)
        attempts["close"] += 1
        result = original_close(handler)
        if close_fails:
            raise OSError("controlled current log handler close failure")
        return result

    def fail_file_operation(channel, record, destination):
        attempts["copy"] += 1
        active.copy = True
        try:
            return original_copy(channel, record, destination)
        finally:
            active.copy = False

    monkeypatch.setattr(clh, "lock", tracked_lock)
    monkeypatch.setattr(clh, "unlock", tracked_unlock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", fail_open)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close_with_optional_error)
    observe_failure_service(monkeypatch, receipts=receipts, factory_started=lambda: attempts.__setitem__("factory", attempts["factory"] + 1))
    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    monkeypatch.setattr(FileChannel, "copy_with_write", fail_file_operation)

    try:
        outcome = await execute_command(deps, None)

        assert outcome.reason == "report_error"
        assert generated == [first_report.report_id, second_report.report_id]
        assert receipts
        assert receipts[0].outcome is copies.CopyOutcome.COPY_FAILED
        assert receipts[0].write_result.appended is False
        assert getattr(receipts[0].write_result, "failure_stage", None).value == "file_io"
        assert attempts["factory"] == attempts["copy"] == 1
        assert attempts["lock"] == attempts["close"] == 1
        assert attempts["unlock"] == 1
        assert attempts[failure_step] == 1
        assert deps.failure_channel.disabled is True
        assert deps.failure_channel.closed is (not close_fails)
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.path_unsafe is True
        assert deps.log_runtime.channel.disabled is True
        assert f"controlled current log {failure_step} failure" in stderr.getvalue()
        if close_fails:
            assert "file_handler_close_failed" in stderr.getvalue()
        _assert_report_failure(stderr.getvalue())
        assert stdout.getvalue() == ""
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        log_path = Path(deps.config.paths.log_file)
        if log_path.exists():
            log = log_path.read_text(encoding="utf-8")
            assert "报告失败的日志副本诊断" not in log
            assert not _session_failures(log)
    finally:
        close_runtime(deps)


async def test_default_format_failure_keeps_file_logging_available(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    copy_calls = {"count": 0}
    lock_calls = {"count": 0}
    original_copy = FileChannel.copy_with_write
    original_format, original_lock = logging.Formatter.format, clh.lock
    active = threading.local()
    lock_path = _lock_path(Path(deps.config.paths.log_file).expanduser())

    def fail_format(formatter, entry):
        if getattr(active, "copy", False):
            raise ValueError("permission denied while formatting")
        return original_format(formatter, entry)

    def count_lock(stream, flags):
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            lock_calls["count"] += 1
        return original_lock(stream, flags)

    def copy_with_format_failure(channel, record, destination):
        copy_calls["count"] += 1
        active.copy = True
        try:
            return original_copy(channel, record, destination)
        finally:
            active.copy = False

    monkeypatch.setattr(logging.Formatter, "format", fail_format)
    monkeypatch.setattr(clh, "lock", count_lock)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy_with_format_failure)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)

        assert outcome.reason == "report_error"
        assert report_called["count"] == 1
        assert copy_calls["count"] == 1
        assert lock_calls["count"] == 0
        assert deps.failure_channel.disabled is False
        assert deps.log_path_unsafe is False
        assert deps.log_runtime.path_unsafe is False
        assert deps.log_runtime.channel.disabled is False
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        assert stdout.getvalue() == ""
        log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
        assert "copy_write_stage=format" in log
        assert "permission denied while formatting" in log
        _assert_report_failure(log)
        assert stderr.getvalue() == ""
    finally:
        close_runtime(deps)


async def test_default_replica_io_failure_keeps_ordinary_log_available(
        tmp_path: Path, monkeypatch) -> None:
    import shutil


    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    receipts = []


    original_copyfileobj = shutil.copyfileobj

    def fail_replica_write(source, target, length=0):
        if Path(target.name).name.startswith("log-copy-"):
            raise OSError("controlled replica write denied")
        return original_copyfileobj(source, target, length)

    observe_failure_service(monkeypatch, receipts=receipts)
    monkeypatch.setattr(shutil, "copyfileobj", fail_replica_write)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)

        assert outcome.reason == "report_error"
        assert report_called["count"] == 1
        assert len(receipts) == 1
        assert receipts[0].outcome is copies.CopyOutcome.COPY_FAILED
        assert receipts[0].write_result.appended is True
        assert "controlled replica write denied" in receipts[0].copy_error
        assert getattr(receipts[0].write_result, "failure_stage", None) is None
        assert deps.failure_channel.disabled is False
        assert deps.log_path_unsafe is False
        assert deps.log_runtime.channel.disabled is False
        assert deps.log_runtime.path_unsafe is False
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        log_path = Path(deps.config.paths.log_file)
        log = log_path.read_bytes()
        assert b"controlled report generation failure" in log
        assert b"controlled replica write denied" in log
        _assert_report_failure(log.decode("utf-8"))
        assert stdout.getvalue() == ""
        assert stderr.getvalue() == ""
    finally:
        close_runtime(deps)


async def test_default_format_failure_keeps_listener_for_later_record(
        tmp_path: Path, monkeypatch) -> None:
    import logging

    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    original_format = logging.Formatter.format

    def fail_one_record(formatter, record):
        try:
            document = json.loads(record.getMessage())
        except json.JSONDecodeError:
            return original_format(formatter, record)
        if isinstance(document, dict) and document.get("message") == "controlled ordinary format failure":
            raise ValueError("permission denied while formatting ordinary log")
        return original_format(formatter, record)

    monkeypatch.setattr(logging.Formatter, "format", fail_one_record)
    try:
        first = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR,
                      message="controlled ordinary format failure"))
        assert first.delivered is False
        assert deps.log_runtime.channel.disabled is False
        assert deps.log_path_unsafe is False
        assert deps.log_runtime.path_unsafe is False

        monkeypatch.setattr(logging.Formatter, "format", original_format)
        later = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="later ordinary record"))

        assert later.delivered is True
        assert deps.log_runtime.channel.disabled is False
        assert deps.log_path_unsafe is False
        log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
        assert "later ordinary record" in log
        assert "controlled ordinary format failure" not in log
        assert "普通日志写入诊断" in stderr.getvalue()
        assert "permission denied while formatting ordinary log" in stderr.getvalue()
        assert stdout.getvalue() == ""
    finally:
        close_runtime(deps)


async def test_default_file_io_failure_stops_listener_and_uses_stderr(
        tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import ChannelError, LogRecord

    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    target = Path(deps.config.paths.log_file).expanduser()
    original_open = clh.ConcurrentRotatingFileHandler.do_open
    attempts = {"open": 0}

    def fail_log_open(handler, *args, **kwargs):
        if Path(handler.baseFilename) == target:
            attempts["open"] += 1
            raise OSError("ordinary current log open denied")
        return original_open(handler, *args, **kwargs)

    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", fail_log_open)
    try:
        with pytest.raises(ChannelError, match="共享文件日志 I/O 或协调失败"):
            await deps.log_runtime.channel.deliver_important(
                LogRecord(level=LogLevel.ERROR, message="ordinary I/O trigger"))

        assert attempts["open"] == 1
        assert deps.log_runtime.channel.disabled is True
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.path_unsafe is True
        assert "普通日志写入诊断" in stderr.getvalue()
        assert "ordinary current log open denied" in stderr.getvalue()
        assert stdout.getvalue() == ""

        later = await deps.log_runtime.channel.submit(
            LogRecord(level=LogLevel.ERROR, message="later ordinary record"))
        assert later.decision.kind.value == "channel_failed"
        assert later.fallback_written is True
        assert attempts["open"] == 1
        fallback = [json.loads(line) for line in stderr.getvalue().splitlines()
                    if line.startswith("{")]
        later_records = [item for item in fallback if item.get("message") == "later ordinary record"]
        assert len(later_records) == 1 and later_records[0]["level"] == "ERROR"
    finally:
        close_runtime(deps)


async def test_default_unsafe_log_blocks_later_copy_factory(
        tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import ChannelError, LogRecord

    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    primary_path = str(Path(deps.config.paths.log_file).expanduser())
    attempts = {
        "open": 0, "append": 0, "factory": 0,
        "helper_create": 0, "copy": 0,
    }
    original_open = clh.ConcurrentRotatingFileHandler.do_open
    before = Path(primary_path).read_bytes()
    original_file_channel_init = FileChannel.__init__
    original_copy = FileChannel.copy_with_write

    def fail_first_primary_open(handler, *args, **kwargs):
        if handler.baseFilename == primary_path:
            attempts["open"] += 1
            if attempts["open"] == 1:
                raise OSError("controlled prior ordinary log open failure")
        stream = original_open(handler, *args, **kwargs)
        if handler.baseFilename != primary_path:
            return stream
        class ObservedStream:
            def __getattr__(self, name):
                return getattr(stream, name)
            def write(self, message):
                result = stream.write(message)
                attempts["append"] += 1
                return result
        return ObservedStream()

    def count_helper_create(channel, *args, **kwargs):
        attempts["helper_create"] += 1
        return original_file_channel_init(channel, *args, **kwargs)

    def count_copy(channel, record, destination):
        attempts["copy"] += 1
        return original_copy(channel, record, destination)

    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", fail_first_primary_open)
    monkeypatch.setattr(FileChannel, "__init__", count_helper_create)
    monkeypatch.setattr(FileChannel, "copy_with_write", count_copy)

    async def fail_generation(self, job):
        generated.append(job.report_id)
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code=f"controlled_failure_{job.report_id}",
            error_message=f"report {job.report_id} actual error",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    generated: list[int] = []


    observe_failure_service(monkeypatch, factory_started=lambda: attempts.__setitem__("factory", attempts["factory"] + 1))
    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)

    try:
        with pytest.raises(ChannelError, match="共享文件日志 I/O 或协调失败"):
            await deps.log_runtime.channel.deliver_important(
                LogRecord(level=LogLevel.ERROR,
                          message="prior ordinary log record"))
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.channel.disabled is True

        outcome = await execute_command(deps, None)

        assert outcome.reason == "report_error"
        assert generated == [first_report.report_id, second_report.report_id]
        assert attempts == {
            "open": 1, "append": 0, "factory": 1,
            "helper_create": 0, "copy": 0,
        }
        assert deps.failure_channel is None
        assert Path(primary_path).read_bytes() == before
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        assert "controlled prior ordinary log open failure" in stderr.getvalue()
        diagnostics = [json.loads(line) for line in stderr.getvalue().splitlines()
                       if line.startswith("{")]
        assert {item.get("context", {}).get("report_id") for item in diagnostics
                if item.get("stage") == "report_generate"} == {
                    first_report.report_id, second_report.report_id}
        assert f"report {first_report.report_id} actual error" in stderr.getvalue()
        _assert_report_failure(stderr.getvalue())
        assert stdout.getvalue() == ""
    finally:
        close_runtime(deps)


async def test_default_rotation_failure_is_reported_once_and_keeps_writes(
        tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    def fail_rollover_check(self, record):
        raise OSError("controlled rotation failure")

    monkeypatch.setattr(
        clh.ConcurrentRotatingFileHandler, "shouldRollover", fail_rollover_check)
    try:
        first = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="first rotation record"))
        second = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="second rotation record"))

        assert first.delivered is True
        assert second.delivered is True
        assert deps.log_runtime.channel.disabled is False
        assert deps.log_path_unsafe is False
        log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
        assert "first rotation record" in log
        assert "second rotation record" in log
        assert stderr.getvalue().count("普通日志写入诊断：stage=rotation") == 1
        assert "controlled rotation failure" in stderr.getvalue()
        assert stdout.getvalue() == ""
    finally:
        close_runtime(deps)


async def test_default_run_records_copy_and_cleanup_failures(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    destination_box: dict[str, Path] = {}
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    def failed_copy(self, record, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(record.message.encode() + b"\n")
        destination_box["path"] = destination
        return (WriteResult(
            appended=True, rotated=True,
            rotation_error="rotation denied",
            copy_handles_closed=True,
            durable=True,
        ), "snapshot read failed")

    original_unlink = os.unlink

    def fail_copy_cleanup(path, *args, **kwargs):
        if Path(path) == destination_box.get("path"):
            raise OSError("staging copy cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(FileChannel, "copy_with_write", failed_copy)
    monkeypatch.setattr(os, "unlink", fail_copy_cleanup)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert destination_box["path"].is_file()
    assert destination_box["path"].read_bytes().endswith(b"\n")
    assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
    assert deps.log_path_unsafe is False
    assert deps.log_runtime.channel.disabled is False
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    assert "outcome=copy_failed" in log
    assert destination_box["path"].name in log
    assert "rotation denied" in log
    assert "snapshot read failed" in log
    assert "staging copy cleanup denied" in log
    assert stderr.getvalue() == ""
    assert stdout.getvalue() == ""


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_unavailable_log_falls_back_to_stderr_without_recursion(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    logs_dir = Path(deps.config.paths.staging).expanduser() / "logs"
    fail_directory_sync(monkeypatch, logs_dir, call=2,
                        message="diagnostic trigger progress failed")
    original_write = FileChannel.write_record
    diagnostic_writes = {"count": 0}

    def fail_diagnostic_log(self, record):
        if "报告失败的日志副本诊断" in record.message:
            diagnostic_writes["count"] += 1
            raise OSError("ordinary log unavailable")
        return original_write(self, record)

    monkeypatch.setattr(FileChannel, "write_record", fail_diagnostic_log)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert diagnostic_writes["count"] == 1
    assert "报告失败的日志副本诊断" in stderr.getvalue()
    assert "diagnostic trigger progress failed" in stderr.getvalue()
    assert stdout.getvalue() == ""
    assert len(list(Path(deps.config.paths.ready).glob("log-copy-*.log"))) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不提供目录同步")
async def test_both_log_diagnostic_and_stderr_failure_preserve_report_outcome(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    logs_dir = Path(deps.config.paths.staging).expanduser() / "logs"
    fail_directory_sync(monkeypatch, logs_dir, call=2,
                        message="diagnostic trigger progress failed")
    original_write = FileChannel.write_record
    diagnostic_writes = {"count": 0}

    def fail_diagnostic_log(self, record):
        if "报告失败的日志副本诊断" in record.message:
            diagnostic_writes["count"] += 1
            raise OSError("ordinary diagnostic log unavailable")
        return original_write(self, record)

    monkeypatch.setattr(FileChannel, "write_record", fail_diagnostic_log)
    original_copy = FileChannel.copy_with_write
    copy_attempts = {"count": 0}

    def record_copy_attempt(self, record, destination):
        copy_attempts["count"] += 1
        return original_copy(self, record, destination)

    monkeypatch.setattr(FileChannel, "copy_with_write", record_copy_attempt)

    class BrokenStderr:
        def __init__(self):
            self.calls = 0
            self.messages: list[str] = []

        def write(self, message):
            self.calls += 1
            self.messages.append(message)
            raise OSError("stderr closed")

        def flush(self):
            raise OSError("stderr closed")

    stderr = BrokenStderr()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert copy_attempts["count"] == 1
    assert diagnostic_writes["count"] == 1
    assert stderr.calls == 1
    assert deps.stderr_diagnostic_failed is True
    assert len(stderr.messages) == 1
    assert "报告失败的日志副本诊断" in stderr.messages[0]
    assert "diagnostic trigger progress failed" in stderr.messages[0]
    assert stdout.getvalue() == ""
    marker_path = (Path(deps.config.paths.staging).expanduser()
                   / "logs" / copies.MARKER_NAME)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["stage"] == MarkerStage.PUBLISHED.value
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()


async def test_unconfirmed_copy_unlock_uses_stderr_directly(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    def copy_with_uncertain_unlock(self, record, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(record.message.encode() + b"\n")
        return (WriteResult(
            appended=True, rotated=True,
            coordination_error="coordination_unlock_failed: unlock lost",
            close_error="coordination_close_failed: close unknown",
            copy_handles_closed=True,
            durable=True,
        ), None)

    monkeypatch.setattr(FileChannel, "copy_with_write", copy_with_uncertain_unlock)
    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert "coordination_unlock_failed" in stderr.getvalue()
    assert "coordination_close_failed" in stderr.getvalue()
    assert stdout.getvalue() == ""
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report generation failure" in ready_files[0].read_bytes()
    log_path = Path(deps.config.paths.log_file)
    if log_path.exists():
        log = log_path.read_text(encoding="utf-8")
        assert "报告失败的日志副本诊断" not in log


async def test_unknown_copy_close_disables_later_file_logging(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    def copy_with_uncertain_close(self, record, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(record.message.encode() + b"\n")
        return (WriteResult(
            appended=True, rotated=True,
            coordination_error="coordination_unlock_failed: unlock unknown",
            close_error="coordination_close_failed: close unknown",
            copy_handles_closed=True,
            durable=True,
        ), None)

    monkeypatch.setattr(FileChannel, "copy_with_write", copy_with_uncertain_close)
    try:
        outcome, report_called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert report_called["count"] == 1
    assert "coordination_close_failed" in stderr.getvalue()
    _assert_report_failure(stderr.getvalue())
    assert stdout.getvalue() == ""
    log_path = Path(deps.config.paths.log_file)
    assert not log_path.exists() or not _session_failures(log_path.read_text(
        encoding="utf-8"))
    assert deps.log_runtime.path_unsafe is True
    assert deps.log_runtime.channel.disabled is True


async def test_default_copy_program_error_keeps_original_channel_failure_facts(
        tmp_path: Path, monkeypatch) -> None:
    import shutil


    deps = await _runtime(tmp_path)
    first_report, second_report = await _seed_two_pending_reports(deps, tmp_path)
    stderr = io.StringIO()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)
    generated: list[int] = []
    factories: list[bool] = []
    receipts = []
    observed_program_errors: list[BaseException] = []
    program_error = RuntimeError("controlled snapshot program exception")

    async def fail_generation(self, job):
        generated.append(job.report_id)
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code=f"controlled_failure_{job.report_id}",
            error_message=f"report {job.report_id} actual error",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )


    original_copy = FileChannel.copy_with_write
    original_unlock = clh.unlock
    original_close = clh.ConcurrentRotatingFileHandler.close
    original_snapshot = shutil.copyfileobj
    active = threading.local()
    primary_path = Path(deps.config.paths.log_file).expanduser()
    lock_path = _lock_path(primary_path)

    def unlock_then_fail(stream):
        result = original_unlock(stream)
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            raise OSError("controlled copy unlock failure")
        return result

    def close_then_fail(handler):
        result = original_close(handler)
        if getattr(active, "copy", False) and Path(handler.baseFilename) == primary_path:
            raise OSError("controlled copy handler close failure")
        return result

    def fail_snapshot_program(source, target, *args, **kwargs):
        if (getattr(active, "copy", False)
                and Path(source.name) == primary_path
                and Path(target.name).parent == Path(deps.config.paths.staging) / "logs"):
            raise program_error
        return original_snapshot(source, target, *args, **kwargs)

    def fail_copy_program_with_unlock_error(channel, record, destination):
        active.copy = True
        try:
            return original_copy(channel, record, destination)
        except BaseException as failure:
            observed_program_errors.append(failure)
            raise
        finally:
            active.copy = False

    monkeypatch.setattr(clh, "unlock", unlock_then_fail)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", close_then_fail)
    monkeypatch.setattr(shutil, "copyfileobj", fail_snapshot_program)
    observe_failure_service(monkeypatch, receipts=receipts, factory_started=lambda: factories.append(True))
    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    monkeypatch.setattr(
        FileChannel, "copy_with_write", fail_copy_program_with_unlock_error)

    def contains_identity(error: BaseException, expected: BaseException) -> bool:
        if error is expected:
            return True
        if isinstance(error, BaseExceptionGroup):
            return any(contains_identity(item, expected)
                       for item in error.exceptions)
        return False

    def write_result_from_exception(error: BaseException):
        result = getattr(error, "write_result", None)
        if result is not None:
            return result
        if isinstance(error, BaseExceptionGroup):
            for item in error.exceptions:
                result = write_result_from_exception(item)
                if result is not None:
                    return result
        return None

    try:
        outcome = await execute_command(deps, None)

        assert outcome.reason == "report_error"
        assert generated == [first_report.report_id, second_report.report_id]
        assert len(observed_program_errors) == 1
        assert contains_identity(observed_program_errors[0], program_error)
        receipt_result = (
            receipts[0].write_result
            if receipts and receipts[0].write_result is not None else None)
        write_result = receipt_result or write_result_from_exception(
            observed_program_errors[0])
        assert write_result is not None
        assert write_result.failure_stage is not None
        assert write_result.failure_stage.value == "lock_release"
        assert write_result.coordination_error is not None
        assert "controlled copy unlock failure" in write_result.coordination_error
        assert write_result.close_error is not None
        assert "controlled copy handler close failure" in write_result.close_error
        assert write_result.disables_file_logging is True
        assert len(factories) == 1
        assert deps.failure_channel.disabled is True
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.path_unsafe is True
        assert deps.log_runtime.channel.disabled is True
        assert "controlled snapshot program exception" in stderr.getvalue()
        assert "controlled copy unlock failure" in stderr.getvalue()
        assert "controlled copy handler close failure" in stderr.getvalue()
        _assert_report_failure(stderr.getvalue())
        assert stdout.getvalue() == ""
        assert list(Path(deps.config.paths.ready).glob("log-copy-*.log")) == []
        log_path = Path(deps.config.paths.log_file)
        if log_path.exists():
            log = log_path.read_text(encoding="utf-8")
            assert "报告失败的日志副本诊断" not in log
            assert not _session_failures(log)
    finally:
        close_runtime(deps)


async def test_failure_wiring_does_not_reopen_failed_helper_channel(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    await _seed_two_pending_reports(deps, tmp_path)
    channels, results, unclosed_handlers = [], [], []
    original_init, original_copy = FileChannel.__init__, FileChannel.copy_with_write
    original_unlock = clh.unlock
    original_close = clh.ConcurrentRotatingFileHandler.close
    active = threading.local()
    primary_path = Path(deps.config.paths.log_file).expanduser()
    lock_path = _lock_path(primary_path)

    def create(channel, *args, **kwargs):
        original_init(channel, *args, **kwargs)
        channels.append(channel)

    def unlock_then_fail(stream):
        result = original_unlock(stream)
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            raise OSError("unlock result unavailable")
        return result

    def fail_close(handler):
        if getattr(active, "copy", False) and Path(handler.baseFilename) == primary_path:
            unclosed_handlers.append(handler)
            raise OSError("close result unavailable")
        return original_close(handler)

    def copy(channel, record, destination):
        active.copy = True
        try:
            result = original_copy(channel, record, destination)
            results.append(result)
            return result
        finally:
            active.copy = False

    monkeypatch.setattr(clh, "unlock", unlock_then_fail)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "close", fail_close)
    monkeypatch.setattr(FileChannel, "__init__", create)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy)
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    try:
        outcome, called = await _run_with_report_failure(deps, monkeypatch)
        assert outcome.reason == "report_error" and called["count"] == 2
        assert len(channels) == len(results) == 1
        result, copy_error = results[0]
        assert result.appended is True and copy_error is None
        assert result.coordination_error is not None and result.close_error is not None
        assert channels[0].disabled is True and channels[0].closed is False
        assert "unlock result unavailable" in stderr.getvalue()
        assert "close result unavailable" in stderr.getvalue()
        assert result.copy_handles_closed is True
        ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
        assert len(ready) == 1
        assert b"controlled report generation failure" in ready[0].read_bytes()
    finally:
        for handler in unclosed_handlers:
            original_close(handler)
        close_runtime(deps)


async def test_default_failure_wiring_shares_marker_owner(tmp_path: Path, monkeypatch) -> None:
    from camctl.logging_runtime.copies import MarkerStore
    deps = await _runtime(tmp_path)
    await _seed_two_pending_reports(deps, tmp_path)
    creations, deliveries = [], []
    original_create, original_copy = MarkerStore.create, FileChannel.copy_with_write

    def create(marker, trigger_id, copy_file):
        creations.append((trigger_id, copy_file))
        return original_create(marker, trigger_id, copy_file)

    def copy(channel, record, destination):
        deliveries.append(Path(destination))
        return original_copy(channel, record, destination)

    monkeypatch.setattr(MarkerStore, "create", create)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy)
    try:
        outcome, called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)
    assert outcome.reason == "report_error" and called["count"] == 2
    assert len(creations) == len(deliveries) == 1
    marker = MarkerStore(Path(deps.config.paths.staging) / "logs").check().state
    assert marker is not None and marker.stage is MarkerStage.PUBLISHED
    ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready) == 1 and ready[0].name == deliveries[0].name
    assert marker.copy_name == ready[0].name


async def test_stderr_failure_disables_repeated_diagnostic_writes(
        tmp_path: Path, monkeypatch) -> None:
    deps = await _runtime(tmp_path)
    await _seed_two_pending_reports(deps, tmp_path)
    from camctl.logging_runtime.copies import MarkerStore
    # 实际标记创建失败先产生副本诊断，原 report_error 随后还要结束会话。
    def fail_marker(_marker, *_args):
        raise OSError("controlled marker write failure")
    monkeypatch.setattr(MarkerStore, "create", fail_marker)
    original_write = FileChannel.write_record
    failed_records = []
    def fail_diagnostic(channel, record):
        if "报告失败的日志副本诊断" in record.message:
            failed_records.append(record)
            raise OSError("ordinary log closed")
        return original_write(channel, record)
    monkeypatch.setattr(FileChannel, "write_record", fail_diagnostic)

    class BrokenStderr:
        def __init__(self):
            self.messages = []
        def write(self, message):
            self.messages.append(message)
            raise OSError("stderr closed")
        def flush(self):
            raise OSError("stderr closed")
    broken = BrokenStderr()
    monkeypatch.setattr(sys, "stderr", broken)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    try:
        outcome, called = await _run_with_report_failure(deps, monkeypatch)
    finally:
        close_runtime(deps)
    assert outcome.reason == "report_error" and called["count"] == 2
    assert len(failed_records) == len(broken.messages) == 1
    assert "controlled marker write failure" in broken.messages[0]
    assert stdout.getvalue() == ""
    assert not list(Path(deps.config.paths.ready).glob("log-copy-*.log"))


async def test_current_copy_and_temp_survive_overlapping_sweep(
        tmp_path: Path, monkeypatch) -> None:
    """默认 run 的同一归属者排除扫描时正在写入的副本和进度临时名。"""
    import asyncio
    import threading

    from camctl.capture.recovery import RecoveryBoundary

    deps = await _runtime(tmp_path, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    first_report, _second_report = await _seed_two_pending_reports(deps, tmp_path)
    logs_dir = Path(deps.config.paths.staging) / "logs"
    old_copy = logs_dir / "log-copy-old-overlap.log"
    old_temp = logs_dir / "failure-marker.json.old-overlap.tmp"
    old_copy.write_bytes(b"old copy")
    old_temp.write_bytes(b"old marker temp")

    loop = asyncio.get_running_loop()
    listdir_started = asyncio.Event()
    copied_temp_ready = asyncio.Event()
    sweep_finished = asyncio.Event()
    release_listdir = threading.Event()
    release_copied_temp = threading.Event()
    listed_names: list[tuple[str, ...]] = []
    current_copy_names: list[str] = []
    copied_temp_paths: list[Path] = []
    lstat_paths: list[Path] = []
    unlink_paths: list[Path] = []
    original_listdir = os.listdir
    original_sweep = copies.startup_sweep
    original_replace = os.replace
    original_copy = FileChannel.copy_with_write
    original_lstat = Path.lstat
    original_unlink = os.unlink

    def blocked_listdir(path: Path):
        if Path(path) == logs_dir:
            loop.call_soon_threadsafe(listdir_started.set)
            if not release_listdir.wait(timeout=10):
                raise TimeoutError("startup listdir test gate was not released")
            names = original_listdir(path)
            listed_names.append(tuple(names))
            return names
        return original_listdir(path)

    def block_copied_progress_temp(source, target, *args, **kwargs):
        source, target = Path(source), Path(target)
        if (target == logs_dir / copies.MARKER_NAME
                and json.loads(source.read_bytes())["stage"] == "copied"):
            copied_temp_paths.append(source)
            loop.call_soon_threadsafe(copied_temp_ready.set)
            if not release_copied_temp.wait(timeout=10):
                raise TimeoutError("copied marker temp test gate was not released")
        return original_replace(source, target, *args, **kwargs)

    def observe_sweep(*args, **kwargs):
        result = original_sweep(*args, **kwargs)
        loop.call_soon_threadsafe(sweep_finished.set)
        return result

    def record_copy(channel, record, destination):
        result = original_copy(channel, record, destination)
        current_copy_names.append(Path(destination).name)
        return result

    def record_lstat(path: Path, *args, **kwargs):
        if path.parent == logs_dir:
            lstat_paths.append(path)
        return original_lstat(path, *args, **kwargs)

    def record_unlink(path, *args, **kwargs):
        if Path(path).parent == logs_dir:
            unlink_paths.append(Path(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "listdir", blocked_listdir)
    monkeypatch.setattr(os, "replace", block_copied_progress_temp)
    monkeypatch.setattr(copies, "startup_sweep", observe_sweep)
    monkeypatch.setattr(FileChannel, "copy_with_write", record_copy)
    monkeypatch.setattr(Path, "lstat", record_lstat)
    monkeypatch.setattr(os, "unlink", record_unlink)

    async def fail_generation(self, job):
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code="controlled_overlap_failure",
            error_message="controlled report failure during startup scan",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    run_task = asyncio.create_task(execute_command(deps, None))
    try:
        await asyncio.wait_for(listdir_started.wait(), timeout=10)
        await asyncio.wait_for(copied_temp_ready.wait(), timeout=10)
        assert len(current_copy_names) == 1
        current_copy = logs_dir / current_copy_names[0]
        copied_temp = copied_temp_paths[0]
        assert current_copy.is_file()
        assert copied_temp.is_file()

        release_listdir.set()
        await asyncio.wait_for(sweep_finished.wait(), timeout=10)
        assert listed_names
        assert current_copy_names[0] in listed_names[0]
        assert copied_temp.name in listed_names[0]
        assert current_copy not in lstat_paths
        assert copied_temp not in lstat_paths
        assert len(unlink_paths) == 2
        assert set(unlink_paths) == {old_copy, old_temp}
        assert not old_copy.exists()
        assert not old_temp.exists()
        assert current_copy.is_file()
        assert copied_temp.is_file()

        release_copied_temp.set()
        outcome = await asyncio.wait_for(run_task, timeout=10)
    finally:
        release_listdir.set()
        release_copied_temp.set()
        if not run_task.done():
            run_task.cancel("test cleanup")
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True), timeout=10)
        close_runtime(deps)

    assert outcome.reason == "report_error"
    assert first_report.report_id > 0
    assert len(current_copy_names) == 1
    ready_files = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready_files) == 1
    assert b"controlled report failure during startup scan" in ready_files[0].read_bytes()
    marker = json.loads((logs_dir / copies.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["stage"] == MarkerStage.PUBLISHED.value


async def test_write_result_disables_log_before_followup_wait(
        tmp_path: Path, monkeypatch) -> None:
    """真实写入结果先停用原通道，再等待有资格的副本清理。"""
    import asyncio
    import threading

    from camctl.capture.recovery import RecoveryBoundary

    deps = await _runtime(tmp_path, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    await _seed_two_pending_reports(deps, tmp_path)
    diagnosed = asyncio.Event()
    class ObservedStderr(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "parallel startup diagnostic after write result" in text:
                diagnosed.set()
            return result
    stderr = ObservedStderr()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", stdout)

    loop = asyncio.get_running_loop()
    cleanup_started = asyncio.Event()
    release_cleanup = threading.Event()
    release_scan = threading.Event()
    scan_started = asyncio.Event()
    logs_dir = Path(deps.config.paths.staging) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    original_listdir = os.listdir

    def startup_failure(path):
        if Path(path) == logs_dir:
            loop.call_soon_threadsafe(scan_started.set)
            if not release_scan.wait(timeout=10):
                raise TimeoutError("startup scan gate was not released")
            raise OSError("parallel startup diagnostic after write result")
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", startup_failure)
    cleanup_names: list[str] = []
    helper_channels: list[FileChannel] = []
    log_deliveries: list[str] = []
    original_unlink = os.unlink
    original_file_channel_init = FileChannel.__init__
    original_open = clh.ConcurrentRotatingFileHandler.do_open
    original_copy = FileChannel.copy_with_write
    original_lock = clh.lock
    active = threading.local()
    original_deliver = deps.log_runtime.channel.deliver_important
    primary_path = Path(deps.config.paths.log_file).expanduser()
    lock_path = _lock_path(primary_path)
    lock_acquired = threading.Event()
    copy_results = []

    def count_file_channel_init(channel, *args, **kwargs):
        original_file_channel_init(channel, *args, **kwargs)
        helper_channels.append(channel)

    def copy_with_open_failure(channel, record, destination):
        active.copy = True
        try:
            result = original_copy(channel, record, destination)
            copy_results.append(result)
            return result
        finally:
            active.copy = False

    def track_copy_lock(stream, flags):
        result = original_lock(stream, flags)
        if getattr(active, "copy", False) and _is_lock_file(stream, lock_path):
            lock_acquired.set()
        return result

    def fail_copy_log_open(handler, *args, **kwargs):
        if getattr(active, "copy", False) and Path(handler.baseFilename) == primary_path:
            assert lock_acquired.is_set()
            raise OSError("controlled copy channel open failure")
        return original_open(handler, *args, **kwargs)

    def block_copy_cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == logs_dir and copies.is_copy_file_name(path.name):
            cleanup_names.append(path.name)
            loop.call_soon_threadsafe(cleanup_started.set)
            if not release_cleanup.wait(timeout=10):
                raise TimeoutError("copy cleanup test gate was not released")
        return original_unlink(path, *args, **kwargs)

    async def count_log_delivery(record):
        log_deliveries.append(record.message)
        return await original_deliver(record)

    monkeypatch.setattr(FileChannel, "__init__", count_file_channel_init)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy_with_open_failure)
    monkeypatch.setattr(clh, "lock", track_copy_lock)
    monkeypatch.setattr(clh.ConcurrentRotatingFileHandler, "do_open", fail_copy_log_open)
    monkeypatch.setattr(os, "unlink", block_copy_cleanup)
    monkeypatch.setattr(
        deps.log_runtime.channel, "deliver_important", count_log_delivery)

    async def fail_generation(self, job):
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code="controlled_write_result_failure",
            error_message="report failure for write-result timing",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    run_task = asyncio.create_task(execute_command(deps, None))
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=10)
        assert lock_acquired.is_set() and len(copy_results) == 1
        write_result, copy_error = copy_results[0]
        assert write_result.failure_stage.value == "file_io"
        assert write_result.appended is False and copy_error is None
        assert deps.log_path_unsafe is True
        assert deps.log_runtime.path_unsafe is True
        assert deps.log_runtime.channel.disabled is True
        assert len(helper_channels) == 1
        assert deps.failure_channel is helper_channels[0]
        deliveries_before_diagnostic = tuple(log_deliveries)

        await asyncio.wait_for(scan_started.wait(), timeout=10)
        release_scan.set()
        await asyncio.wait_for(diagnosed.wait(), timeout=10)

        assert "parallel startup diagnostic after write result" in stderr.getvalue()
        assert stdout.getvalue() == ""
        assert tuple(log_deliveries) == deliveries_before_diagnostic
        assert len(helper_channels) == 1
        assert cleanup_names and len(cleanup_names) == 1
        assert not run_task.done()
        release_cleanup.set()
        outcome = await asyncio.wait_for(run_task, timeout=10)
    finally:
        release_scan.set()
        release_cleanup.set()
        if not run_task.done():
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True), timeout=10)
        close_runtime(deps)

    assert deps.log_path_unsafe is True
    assert deps.failure_channel is not None
    assert outcome.reason == "report_error"


async def test_default_report_copy_publishes_while_ordinary_error_queue_is_full(
        tmp_path: Path, monkeypatch) -> None:
    """真实队列满时仍交付副本，随后结束队列消费且原错误只写一次。"""
    import asyncio
    import threading
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    deps = await _runtime(tmp_path, log={"queue_capacity": 4,
        "queue_low_watermark": 1, "queue_high_watermark": 2})
    loop = asyncio.get_running_loop()
    blocked, copied = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    original_write, original_copy = FileChannel.write_record, FileChannel.copy_with_write

    def write(channel, record):
        if record.message == "queue blocker":
            loop.call_soon_threadsafe(blocked.set)
            if not release.wait(timeout=10):
                raise TimeoutError("ordinary queue gate was not released")
        return original_write(channel, record)

    def copy(channel, record, destination):
        result = original_copy(channel, record, destination)
        loop.call_soon_threadsafe(copied.set)
        return result

    monkeypatch.setattr(FileChannel, "write_record", write)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy)
    channel = deps.log_runtime.channel
    task = None
    try:
        first = await channel.alog(LogRecord(LogLevel.ERROR, "queue blocker"))
        assert first.decision.kind.value == "accepted"
        await asyncio.wait_for(blocked.wait(), timeout=10)
        for index in range(4):
            admitted = await channel.alog(LogRecord(LogLevel.ERROR, f"queued error {index}"))
            assert admitted.decision.kind.value == "accepted"
        assert admitted.queue_depth == 4
        task = asyncio.create_task(_run_with_report_failure(deps, monkeypatch))
        await asyncio.wait_for(copied.wait(), timeout=10)
        # copy_with_write 返回后还要由正式消费者保存进度并发布；等实际 ready 文件。
        deadline = loop.time() + 10
        ready = []
        while not ready and loop.time() < deadline:
            ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
            if not ready:
                await asyncio.sleep(0.01)
        assert len(ready) == 1
        assert b"controlled report generation failure" in ready[0].read_bytes()
        assert not task.done()
        release.set()
        outcome, called = await asyncio.wait_for(task, timeout=10)
        assert outcome.reason == "report_error" and called["count"] == 1
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
        close_runtime(deps)
    content = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    records = [json.loads(line) for line in content.splitlines() if line.startswith("{")]
    assert len([record for record in records if record.get("stage") == "report_generate"]) == 1
    assert content.count("queue blocker") == 1
    for index in range(4):
        assert content.count(f"queued error {index}") == 1
