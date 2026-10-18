"""默认装配与 CLI 对会话锁、收尾错误及同步 control 的端到端验证。"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import application
from camctl.bootstrap import lifecycle
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.logging_runtime import copies
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.supervisor import (
    GenerationOutcomeKind,
    WorkerGeneration,
    WorkerSupervisor,
)
from camctl.capture.recovery import RecoveryBoundary
from camctl.session import locks
from camctl.session import service as session_service
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.service import StateDbFailure


_GATE_TIMEOUT = 10


def _config_for(home: Path, *, recording: bool = False):
    values = {
        "paths": {
            "state_db": str(home / "state.db"),
            "log_file": str(home / "camctl.log"),
            "staging": str(home / "staging"),
            "ready": str(home / "ready"),
            "processing": str(home / "processing"),
        }
    }
    if recording:
        values["devices"] = {
            "cam-1": {
                "kind": "camera",
                "driver": "camctl-adb",
                "recording": {"repair_margin_s": "2"},
            }
        }
    return load_config(values, ConfigDefaults())


def _source(path: Path, request_id: str, *, actions) -> ParsedInput:
    return ParsedInput(
        path=str(path),
        document={
            "request_id": request_id,
            "created_at": "2026-01-15 08:00:00",
            "name": f"plan-{request_id}",
            "actions": list(actions),
        },
    )


def _report_status_action() -> dict[str, object]:
    return {
        "name": "full-status-report",
        "type": "report_status",
        "params": {"scope": "full"},
    }


def _database_image(path: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    with closing(sqlite3.connect(path)) as connection:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE type IN ('table', 'index', 'trigger') ORDER BY type, name"
        ).fetchall()
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )]
        rows = {
            name: connection.execute(
                f'SELECT * FROM "{name}" ORDER BY 1'
            ).fetchall()
            for name in names
        }
    return schema, rows


def _rows(path: Path, sql: str, parameters: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _scalar(path: Path, sql: str, parameters: tuple = ()):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, parameters).fetchone()


def _assert_session_lock_reacquirable(path: Path) -> None:
    lease = locks.acquire_session(path)
    lease.close()


def _seed_startup_log(home: Path) -> Path:
    logs = home / "staging" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    old_copy = logs / "log-copy-before-lock-test.log"
    old_copy.write_bytes(b"keep until session ownership is acquired")
    return old_copy


class _FaultingLockBackend:
    """真实平台锁后端的窄故障注入；未命中的操作仍调用原生实现。"""

    def __init__(
        self,
        operation: str,
        failure: BaseException | None = None,
        *,
        on_try_lock=None,
        on_unlock=None,
    ) -> None:
        self.delegate = locks.default_backend()
        self.operation = operation
        self.failure = failure
        self.on_try_lock = on_try_lock
        self.on_unlock = on_unlock
        self.calls = {name: 0 for name in (
            "open", "try_lock", "unlock", "close")}

    def open(self, path):
        self.calls["open"] += 1
        return self.delegate.open(path)

    def set_inheritable(self, fd, inheritable):
        return self.delegate.set_inheritable(fd, inheritable)

    def try_lock(self, fd):
        self.calls["try_lock"] += 1
        if self.on_try_lock is not None:
            self.on_try_lock()
        if self.operation == "try_lock":
            assert self.failure is not None
            raise self.failure
        return self.delegate.try_lock(fd)

    def unlock(self, fd):
        self.calls["unlock"] += 1
        if self.on_unlock is not None:
            self.on_unlock()
        if self.operation == "unlock":
            assert self.failure is not None
            raise self.failure
        return self.delegate.unlock(fd)

    def close(self, fd):
        self.calls["close"] += 1
        return self.delegate.close(fd)


def _patch_lock_acquisition(
    monkeypatch,
    *,
    session_path: Path,
    session_backend: _FaultingLockBackend | None = None,
    admission_path: Path,
    admission_backend: _FaultingLockBackend | None = None,
) -> None:
    real_session = locks.acquire_session
    real_admission = locks.acquire_admission

    def acquire_session(path, backend=None):
        selected = session_backend if Path(path) == session_path else backend
        return real_session(path, backend=selected)

    def acquire_admission(path, backend=None):
        selected = admission_backend if Path(path) == admission_path else backend
        return real_admission(path, backend=selected)

    monkeypatch.setattr(lifecycle, "acquire_session", acquire_session)
    monkeypatch.setattr(lifecycle, "acquire_admission", acquire_admission)


async def _submit_report_plan(config, tmp_path: Path, request_id: str) -> None:
    deps = build_runtime(CommandMode.SUBMIT, config)
    try:
        outcome = await execute_command(
            deps,
            _source(
                tmp_path / f"{request_id}.json",
                request_id,
                actions=(_report_status_action(),),
            ),
        )
    finally:
        close_runtime(deps)
    assert outcome.succeeded is True, dict(outcome.details)
    assert _scalar(
        Path(config.paths.state_db),
        "SELECT COUNT(*) FROM plans WHERE request_id = ?",
        (request_id,),
    ) == (1,)


def _write_cli_config(config, target: Path) -> Path:
    def quote(value: str) -> str:
        return json.dumps(value)

    target.write_text(
        "\n".join((
            "[paths]",
            f"state_db = {quote(config.paths.state_db)}",
            f"log_file = {quote(config.paths.log_file)}",
            f"staging = {quote(config.paths.staging)}",
            f"ready = {quote(config.paths.ready)}",
            f"processing = {quote(config.paths.processing)}",
            "",
        )),
        encoding="utf-8",
    )
    return target


def _tracked_open_connections(monkeypatch):
    opened = []
    original_open = lifecycle._open_runtime_connection

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            return self.connection.close()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def open_tracked(deps):
        owned = original_open(deps)
        tracked = TrackedConnection(owned.connection)
        opened.append(tracked)
        return replace(owned, connection=tracked)

    monkeypatch.setattr(lifecycle, "_open_runtime_connection", open_tracked)
    return opened


async def _worker_report_failure(self, job):
    failure = ResultFailureMessage(
        job_id=job.job_id,
        instance_id=job.instance_id,
        error_kind=ErrorKind.REPORT,
        error_code="controlled_task3_report_failure",
        error_message="Task3 report failure reached the real failure-copy path",
    )
    return WorkerGeneration(
        kind=GenerationOutcomeKind.REPORT_FAILURE,
        job_id=job.job_id,
        failure=failure,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program_error", [False, True], ids=["system-lock-error", "program-lock-error"])
async def test_default_run_lock_failure_has_zero_business_effects(
        tmp_path: Path, monkeypatch, program_error: bool) -> None:
    from camctl.acceptance.ports import ParameterDefinition

    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy = _seed_startup_log(tmp_path)
    before = _database_image(Path(config.paths.state_db))

    class Catalog:
        def __init__(self):
            self.calls: list[str] = []

        def action_types(self):
            self.calls.append("action_types")
            return frozenset({"camera_take_photo"})

        def device_exists(self, device_id):
            self.calls.append("device_exists")
            return device_id == "cam-1"

        def driver_id(self, device_id):
            self.calls.append("driver_id")
            return "test-stub"

        def device_supports(self, device_id, action_type):
            self.calls.append("device_supports")
            return device_id == "cam-1" and action_type == "camera_take_photo"

        def parameter_definition(self, _device_id, _action_type, parameter_type):
            self.calls.append("parameter_definition")
            return ParameterDefinition(
                schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"type": {"const": parameter_type}},
                    "required": ["type"],
                    "additionalProperties": False,
                },
                defaults={},
            )

    catalog = Catalog()
    action = {
        "name": "must-not-be-accepted",
        "type": "camera_take_photo",
        "device_id": "cam-1",
        "scheduled_at": "2099-01-01 00:00:00",
        "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 1000},
    }
    deps = build_runtime(CommandMode.RUN, config, catalog=catalog)
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    original_error = (
        RuntimeError("controlled session try_lock program failure")
        if program_error else
        OSError(errno.EIO, "controlled session try_lock failure")
    )
    backend = _FaultingLockBackend("try_lock", original_error)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, session_backend=backend,
        admission_path=admission_path,
    )

    session_connections: list[object] = []
    original_open = lifecycle._open_runtime_connection

    def count_open(current):
        owned = original_open(current)
        session_connections.append(owned.connection)
        return owned

    monkeypatch.setattr(lifecycle, "_open_runtime_connection", count_open)
    scans: list[Path] = []
    original_listdir = copies._listdir

    def count_scan(path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", count_scan)
    emergency_calls: list[str] = []
    original_emergency = lifecycle._emergency_settlement_of

    def observe_emergency(current):
        settlement = original_emergency(current)

        async def run_settlement():
            emergency_calls.append("called")
            return await settlement()

        return run_settlement

    monkeypatch.setattr(lifecycle, "_emergency_settlement_of", observe_emergency)
    try:
        outcome = await execute_command(
            deps, _source(
                tmp_path / "unused-plan.json", "710001", actions=(action,)))
    finally:
        close_runtime(deps)

    assert outcome.reason == ("internal_error" if program_error else "lock_error")
    details = dict(outcome.details)
    assert details["stage"] == "locking"
    assert details["path"] == str(session_path)
    if program_error:
        assert "errno" not in details
        facts = locks.lock_error_facts(original_error)
        assert facts is not None
        assert facts.operation is locks.LockOperation.TRY_LOCK
        assert facts.path == session_path
        assert facts.original_error is original_error
        assert "try_lock" in details["message"]
    else:
        assert details["errno"] == errno.EIO
        assert "try_lock" in details["message"]
    assert "transaction_status" not in details
    assert "try_lock" in details["message"]
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 0, "close": 1}
    assert session_connections == []
    assert scans == []
    assert emergency_calls == []
    assert catalog.calls == []
    assert old_copy.read_bytes() == b"keep until session ownership is acquired"
    assert _database_image(Path(config.paths.state_db)) == before


@pytest.mark.skipif(sys.platform != "linux", reason="需要 Linux flock 跨进程冲突语义")
def test_cli_emits_one_lock_error_result_while_real_holder_keeps_session_lock(
        tmp_path: Path) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy = _seed_startup_log(tmp_path)
    before = _database_image(Path(config.paths.state_db))
    config_file = _write_cli_config(config, tmp_path / "config.toml")
    session_path, _admission_path = locks.lock_file_paths(
        Path(config.paths.state_db))
    holder = locks.acquire_session(session_path)
    try:
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[3] / "src")
        previous_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (source_root, previous_pythonpath) if part)
        completed = subprocess.run(
            [sys.executable, "-m", "camctl", "run", "--config", str(config_file)],
            cwd=Path(__file__).resolve().parents[5],
            env=environment,
            text=True,
            capture_output=True,
            timeout=_GATE_TIMEOUT,
            check=False,
        )
        assert completed.returncode == 1
        lines = completed.stdout.splitlines()
        assert len(lines) == 1
        assert completed.stdout.endswith("\n")
        message = json.loads(lines[0])
        assert message["kind"] == "error"
        assert message["body"]["reason"] == "lock_error"
        details = message["body"]["details"]
        assert details["stage"] == "locking"
        assert details["path"] == str(session_path)
        assert "transaction_status" not in details
        assert old_copy.read_bytes() == b"keep until session ownership is acquired"
        assert _database_image(Path(config.paths.state_db)) == before
        with pytest.raises(locks.SessionLockConflictError):
            locks.acquire_session(session_path)
    finally:
        holder.close()
    released = locks.acquire_session(session_path)
    released.close()


@pytest.mark.asyncio
async def test_submit_keeps_acceptance_when_real_admission_holder_conflicts(
        tmp_path: Path) -> None:
    from camctl.acceptance.ports import ParameterDefinition

    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    session_path, admission_path = locks.lock_file_paths(
        Path(config.paths.state_db))
    holder = locks.acquire_admission(admission_path)
    source = _source(
        tmp_path / "conflicting-submit.json", "710002",
        actions=({
            "name": "future action",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": "2099-01-01 00:00:00",
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 1000},
        },),
    )

    class Catalog:
        def action_types(self):
            return frozenset({"camera_take_photo"})

        def device_exists(self, device_id):
            return device_id == "cam-1"

        def driver_id(self, device_id):
            return "test-stub"

        def device_supports(self, device_id, action_type):
            return device_id == "cam-1" and action_type == "camera_take_photo"

        def parameter_definition(self, device_id, action_type, parameter_type):
            return ParameterDefinition(
                schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"type": {"const": "single_shot"}},
                    "required": ["type"],
                    "additionalProperties": False,
                },
                defaults={},
            )

    deps = build_runtime(CommandMode.SUBMIT, config, catalog=Catalog())
    try:
        outcome = await execute_command(deps, source)
        assert locks.probe_admission(admission_path).is_free is False
    finally:
        close_runtime(deps)
        holder.close()

    assert outcome.succeeded is True
    assert outcome.needs_run is False
    assert _scalar(
        Path(config.paths.state_db),
        "SELECT COUNT(*) FROM plans WHERE request_id = ?",
        ("710002",),
    ) == (1,)
    assert _rows(
        Path(config.paths.state_db),
        "SELECT event_type FROM history_events ORDER BY id",
    )
    free = locks.probe_admission(admission_path)
    assert free.is_free is True
    _assert_session_lock_reacquirable(session_path)


@pytest.mark.asyncio
async def test_admission_acquire_failure_keeps_commits_and_skips_ordinary_flows(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    await _submit_report_plan(config, tmp_path, "710003")
    database = Path(config.paths.state_db)
    prior_events = _rows(database, "SELECT * FROM history_events ORDER BY id")
    prior_plan = _rows(database, "SELECT * FROM plans WHERE request_id = ?",
                       ("710003",))
    assert prior_plan
    assert len(prior_plan) == 1
    prior_ack = _scalar(
        database,
        "SELECT acknowledged_wm, acknowledged_report_id"
        " FROM runtime_state WHERE id = 1",
    )
    prior_bound = _scalar(
        database, "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1")
    deps = build_runtime(CommandMode.RUN, config)
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    connection_box: list[sqlite3.Connection] = []
    original_open = lifecycle._open_runtime_connection

    def track_connection(current):
        owned = original_open(current)
        connection_box.append(owned.connection)
        return owned

    monkeypatch.setattr(lifecycle, "_open_runtime_connection", track_connection)
    transaction_observations: list[bool] = []
    failure = OSError(errno.EACCES, "controlled admission acquire failure")
    backend = _FaultingLockBackend(
        "try_lock", failure,
        on_try_lock=lambda: transaction_observations.append(
            connection_box[0].in_transaction),
    )
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, admission_path=admission_path,
        admission_backend=backend,
    )
    ordinary_calls: list[tuple[str, ...]] = []
    capture_calls: list[str] = []
    original_assembly = lifecycle._report_assembly
    from camctl.bootstrap.flows import CaptureFlow

    capture_flows: list[CaptureFlow] = []
    original_capture_call = CaptureFlow.__call__

    async def observe_capture_call(self, context):
        if any(self is flow for flow in capture_flows):
            capture_calls.append("scheduling")
        return await original_capture_call(self, context)

    monkeypatch.setattr(CaptureFlow, "__call__", observe_capture_call)

    def observe_assembly(*args, **kwargs):
        flows, supervisor = original_assembly(*args, **kwargs)
        capture_flows.append(flows["scheduling"])
        return flows, supervisor

    monkeypatch.setattr(lifecycle, "_report_assembly", observe_assembly)
    original_drive_flows = session_service._drive_flows

    async def observe_drive_flows(context):
        ordinary_calls.append(tuple(context.flows))
        return await original_drive_flows(context)

    monkeypatch.setattr(session_service, "_drive_flows", observe_drive_flows)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.reason == "lock_error"
    details = dict(outcome.details)
    assert details["stage"] == "locking"
    assert details["path"] == str(admission_path)
    assert details["errno"] == errno.EACCES
    assert details["transaction_status"] == "not_committed"
    assert outcome.needs_run is None
    assert transaction_observations == [True]
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 0, "close": 1}
    assert ordinary_calls == []
    assert capture_calls == []
    current_events = _rows(database, "SELECT * FROM history_events ORDER BY id")
    assert current_events[:len(prior_events)] == prior_events
    assert _rows(database, "SELECT * FROM plans WHERE request_id = ?",
                 ("710003",)) == prior_plan
    assert _scalar(
        database,
        "SELECT acknowledged_wm, acknowledged_report_id"
        " FROM runtime_state WHERE id = 1",
    ) == prior_ack
    current_bound = _scalar(
        database, "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1")
    assert prior_bound == (None,)
    assert current_bound[0] is not None
    assert current_bound[0] > 0
    assert _scalar(database, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9") == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("report_error", [False, True], ids=["success", "report-error"])
async def test_cleanup_lock_error_does_not_skip_other_resources(
        tmp_path: Path, monkeypatch, report_error: bool) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    if report_error:
        await _submit_report_plan(config, tmp_path, "710004")
        monkeypatch.setattr(WorkerSupervisor, "generate", _worker_report_failure)
    deps = build_runtime(CommandMode.RUN, config)
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    failure = OSError(errno.EIO, "controlled final session unlock error")
    backend = _FaultingLockBackend("unlock", failure)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, session_backend=backend,
        admission_path=admission_path,
    )
    opened = _tracked_open_connections(monkeypatch)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    details = dict(outcome.details)
    assert details["stage"] == "shutdown"
    assert details["path"] == str(session_path)
    assert details["errno"] == errno.EIO
    assert "transaction_status" not in details
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 1, "close": 1}
    _assert_session_lock_reacquirable(session_path)
    assert locks.probe_admission(admission_path).is_free is True
    assert opened and all(connection.close_calls == 1 for connection in opened)
    assert deps.log_runtime.close_attempts == 1
    assert deps.log_runtime.channel.listener_alive is False
    if report_error:
        secondary = details["secondary_errors"]
        assert len(secondary) == 1
        assert secondary[0]["reason"] == "report_error"
        assert deps.failure_channel is not None
        assert deps.failure_channel.closed is True
    else:
        assert "secondary_errors" not in details
        assert deps.failure_channel is None
    assert _scalar(
        Path(config.paths.state_db),
        "SELECT COUNT(*) FROM operation_runs WHERE kind = 9",
    ) == (0,)


@pytest.mark.asyncio
async def test_original_error_survives_later_lock_error(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, config)
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    backend = _FaultingLockBackend(
        "unlock", OSError(errno.EIO, "later session lease release error"))
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, session_backend=backend,
        admission_path=admission_path,
    )

    def fail_work_query(_connection):
        raise StateDbFailure("pre-existing work database failure")

    monkeypatch.setattr(application, "query_work_facts", fail_work_query)
    opened = _tracked_open_connections(monkeypatch)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.reason == "state_db_error"
    details = dict(outcome.details)
    assert "pre-existing work database failure" in repr(details)
    secondary = details["secondary_errors"]
    assert len(secondary) == 1
    assert secondary[0]["reason"] == "lock_error"
    assert secondary[0]["details"]["path"] == str(session_path)
    assert secondary[0]["details"]["errno"] == errno.EIO
    assert all(connection.close_calls == 1 for connection in opened)
    _assert_session_lock_reacquirable(session_path)
    assert _scalar(
        Path(config.paths.state_db),
        "SELECT COUNT(*) FROM operation_runs WHERE kind = 9",
    ) == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program_error", [False, True], ids=["system-lock-error", "program-lock-error"])
async def test_late_clock_lock_failure_uses_original_emergency_qualification(
        tmp_path: Path, monkeypatch, program_error: bool) -> None:
    from camctl.devices.drivers import runtime as drivers_runtime
    from camctl.devices.drivers.registry import DriverRegistry
    from .test_production_flows import _ProductionDriver, _entry_for_driver
    from .test_recording_stop import (
        _RecordCatalog,
        _future_schedule,
        _record_plan,
        _submit_plan,
    )

    config = _config_for(tmp_path, recording=True)
    assert config.devices["cam-1"]["driver"] == "camctl-adb"
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    database = Path(config.paths.state_db)
    driver = _ProductionDriver({})
    registry = DriverRegistry((_entry_for_driver("camctl-adb", driver),))
    entry = registry.entry("camctl-adb")
    assert entry is not None and entry.declaration.result_supported
    monkeypatch.setattr(drivers_runtime, "current_registry", lambda: registry)
    await _submit_plan(
        tmp_path, config, _RecordCatalog(),
        _record_plan("710005", _future_schedule(0)),
    )
    deps = build_runtime(CommandMode.RUN, config, catalog=_RecordCatalog())
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    release_failure = (
        RuntimeError("late-clock admission release program failure")
        if program_error else
        OSError(errno.EIO, "late-clock admission release failure")
    )
    unlock_reached = threading.Event()
    backend = _FaultingLockBackend(
        "unlock", release_failure, on_unlock=unlock_reached.set)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, admission_path=admission_path,
        admission_backend=backend,
    )
    original_assembly = lifecycle._report_assembly
    late_clock_calls: list[str] = []

    async def wait_for_started_recording() -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _GATE_TIMEOUT
        last_row = None
        while loop.time() < deadline:
            with closing(sqlite3.connect(database)) as connection:
                last_row = connection.execute(
                    "SELECT started_at IS NOT NULL, activity_state"
                    " FROM device_activities WHERE id = 1",
                ).fetchone()
            if last_row == (1, 2) and deps.capture_recording_anchors:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            "等待实际录像启动事实及会话计时锚点超时："
            f"activity={last_row!r}, anchors={deps.capture_recording_anchors!r}"
        )

    async def late_clock(_context):
        await wait_for_started_recording()
        late_clock_calls.append("raised")
        raise ClockBecameUntrusted("ordinary flow observed the late clock change")

    def assemble_capture_then_late_clock(*args, **kwargs):
        flows, supervisor = original_assembly(*args, **kwargs)
        ordered = {"scheduling": flows["scheduling"], "late_clock": late_clock}
        ordered.update((name, flow) for name, flow in flows.items()
                       if name != "scheduling")
        return ordered, supervisor

    monkeypatch.setattr(
        lifecycle, "_report_assembly", assemble_capture_then_late_clock)
    generation_calls: list[str] = []

    async def must_not_run_report(_self, job):
        generation_calls.append(job.job_id)
        return await _worker_report_failure(_self, job)

    monkeypatch.setattr(WorkerSupervisor, "generate", must_not_run_report)
    held_during_stop: list[bool] = []
    original_stop = driver.stop

    async def stop_while_session_locked(request):
        try:
            unexpected = locks.acquire_session(session_path)
        except locks.SessionLockConflictError:
            held_during_stop.append(True)
        else:
            held_during_stop.append(False)
            unexpected.close()
        return await original_stop(request)

    driver.stop = stop_while_session_locked
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert unlock_reached.is_set()
    assert late_clock_calls == ["raised"]
    assert deps.capture_recording_anchors
    assert outcome.succeeded is False
    assert outcome.reason == ("internal_error" if program_error else "lock_error")
    details = dict(outcome.details)
    assert details["stage"] == "handoff"
    assert details["path"] == str(admission_path)
    if program_error:
        assert "errno" not in details
        facts = locks.lock_error_facts(release_failure)
        assert facts is not None
        assert facts.operation is locks.LockOperation.UNLOCK
        assert facts.path == admission_path
        assert facts.original_error is release_failure
    else:
        assert details["errno"] == errno.EIO
    assert "transaction_status" not in details
    assert "ordinary flow observed the late clock change" in repr(details)
    assert generation_calls == []
    assert held_during_stop == [True]
    assert [call for call in driver.calls if call[0] == "control"]
    assert [call for call in driver.calls if call[0] == "stop"]
    assert len([call for call in driver.calls if call[0] == "stop"]) == 1
    emergency_runs = _rows(
        Path(config.paths.state_db),
        "SELECT status, attempts_used FROM operation_runs WHERE kind = 9",
    )
    assert len(emergency_runs) == 1
    assert emergency_runs[0][1] == 1
    _assert_session_lock_reacquirable(session_path)
    assert locks.probe_admission(admission_path).is_free is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_first", [False, True], ids=["sync-control-first", "parent-first"])
async def test_lock_control_and_parent_cancel_keep_first_source(
        tmp_path: Path, monkeypatch, parent_first: bool) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    _seed_startup_log(tmp_path)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    session_path = Path(deps.session_lock)
    admission_path = Path(deps.admission_lock)
    control = asyncio.CancelledError("typed admission unlock control")
    control.add_note("original synchronous lock control note")
    unlock_reached = threading.Event()
    backend = _FaultingLockBackend(
        "unlock", control, on_unlock=unlock_reached.set)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, admission_path=admission_path,
        admission_backend=backend,
    )
    original_assembly = lifecycle._report_assembly

    async def late_clock(_context):
        raise ClockBecameUntrusted("late clock enters required local cleanup")

    def assemble_late_clock_first(*args, **kwargs):
        flows, supervisor = original_assembly(*args, **kwargs)
        return {"late_clock": late_clock, **flows}, supervisor

    monkeypatch.setattr(lifecycle, "_report_assembly", assemble_late_clock_first)
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()
    final_settlement_started = asyncio.Event()
    original_settle = session_service._settle_local_work

    async def gated_settle(context, *, stop_new=True, exclude_owner=None, waiter=None):
        if exclude_owner is not None:
            transition_started.set()
            if parent_first:
                await asyncio.wait_for(
                    release_transition.wait(), timeout=_GATE_TIMEOUT)
        else:
            final_settlement_started.set()
        return await original_settle(
            context, stop_new=stop_new, exclude_owner=exclude_owner, waiter=waiter)

    monkeypatch.setattr(session_service, "_settle_local_work", gated_settle)
    listdir_started = threading.Event()
    release_listdir = threading.Event()
    original_listdir = copies._listdir

    def blocked_listdir(path):
        listdir_started.set()
        if not release_listdir.wait(timeout=_GATE_TIMEOUT):
            raise TimeoutError("startup-sweep test gate was not released")
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)
    opened = _tracked_open_connections(monkeypatch)
    run_task = asyncio.create_task(execute_command(deps, None))
    try:
        assert await asyncio.to_thread(listdir_started.wait, _GATE_TIMEOUT)
        if parent_first:
            await asyncio.wait_for(transition_started.wait(), timeout=_GATE_TIMEOUT)
            run_task.cancel("parent cancelled before synchronous lock control")
            await asyncio.sleep(0)
            assert not run_task.done()
            release_transition.set()
        else:
            await asyncio.wait_for(transition_started.wait(), timeout=_GATE_TIMEOUT)
        assert await asyncio.to_thread(unlock_reached.wait, _GATE_TIMEOUT)
        await asyncio.wait_for(final_settlement_started.wait(), timeout=_GATE_TIMEOUT)
        if not parent_first:
            run_task.cancel("parent cancelled after synchronous lock control")
            await asyncio.sleep(0)
        assert not run_task.done()
        try:
            duplicate = locks.acquire_session(session_path)
        except locks.SessionLockConflictError:
            pass
        else:
            duplicate.close()
            pytest.fail("日志实际责任仍在途时会话锁已释放")
        release_listdir.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(run_task, timeout=_GATE_TIMEOUT)
    finally:
        release_transition.set()
        release_listdir.set()
        if not run_task.done():
            run_task.cancel("Task3 lock-control cleanup")
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True),
                timeout=_GATE_TIMEOUT,
            )
        close_runtime(deps)

    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 1, "close": 1}
    facts = locks.lock_error_facts(control)
    assert facts is not None
    assert facts.operation is locks.LockOperation.UNLOCK
    assert facts.path == admission_path
    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    if parent_first:
        assert raised.value is not control
        assert raised.value.args == ("parent cancelled before synchronous lock control",)
        assert "typed admission unlock control" in notes
        assert "original synchronous lock control note" in notes
    else:
        assert raised.value is control
        assert "parent cancelled after synchronous lock control" in notes
        assert "original synchronous lock control note" in notes
    assert "late clock enters required local cleanup" in notes
    assert opened and all(connection.close_calls == 1 for connection in opened)
    _assert_session_lock_reacquirable(session_path)
    assert locks.probe_admission(admission_path).is_free is True
    assert _scalar(
        Path(config.paths.state_db),
        "SELECT COUNT(*) FROM operation_runs WHERE kind = 9",
    ) == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_first", [False, True],
                         ids=["control-first", "ordinary-error-first"])
async def test_body_success_keeps_first_worker_close_control(
        tmp_path: Path, monkeypatch, ordinary_first: bool) -> None:
    config = _config_for(tmp_path)
    database = Path(config.paths.state_db)
    assert initialize_state(config, database).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, config)
    session_path = Path(deps.session_lock)
    runtime = deps.log_runtime
    assert runtime is not None

    first_control = (RuntimeError("first worker ordinary close error")
                     if ordinary_first else
                     asyncio.CancelledError("first worker close control"))
    first_control.add_note("original first worker close marker")
    later_control = asyncio.CancelledError("later restricted worker close control")
    later_control.add_note("later worker close marker")
    controls = (first_control, later_control)
    body_outcomes = []
    stop_observations = []
    generation_calls: list[str] = []
    real_run_session = lifecycle.run_session

    async def observe_real_body(context, source):
        outcome = await real_run_session(context, source)
        body_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(lifecycle, "run_session", observe_real_body)
    real_stop = WorkerSupervisor.stop

    async def close_then_control(supervisor):
        stopped = await real_stop(supervisor)
        index = len(stop_observations)
        stop_observations.append((
            supervisor, stopped,
            bool(body_outcomes and body_outcomes[-1].succeeded),
        ))
        if index < len(controls):
            raise controls[index]
        return stopped

    monkeypatch.setattr(WorkerSupervisor, "stop", close_then_control)

    async def unexpected_generation(_supervisor, job):
        generation_calls.append(job.job_id)
        raise AssertionError("该场景不应进入报告生成")

    monkeypatch.setattr(WorkerSupervisor, "generate", unexpected_generation)
    file_close_calls: list[int] = []
    real_file_closer = runtime.file_closer
    assert real_file_closer is not None

    def close_real_log_file():
        file_close_calls.append(1)
        real_file_closer()

    runtime.file_closer = close_real_log_file
    channel_close_calls: list[int] = []
    real_channel_close = runtime.channel.async_close

    def close_real_log_channel():
        channel_close_calls.append(1)
        return real_channel_close()

    runtime.channel.async_close = close_real_log_channel
    opened = _tracked_open_connections(monkeypatch)
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(execute_command(deps, None), _GATE_TIMEOUT)
    finally:
        if not deps.closed:
            close_runtime(deps)

    assert raised.value is (later_control if ordinary_first else first_control)
    assert [outcome.succeeded for outcome in body_outcomes] == [True]
    assert len(stop_observations) == 2
    assert stop_observations[0][0] is not stop_observations[1][0]
    assert all(observation[2] for observation in stop_observations)
    assert all(observation[1] is not None for observation in stop_observations)
    assert generation_calls == []
    notes = tuple(getattr(raised.value, "__notes__", ()))
    rendered_notes = "\n".join(notes)
    assert "original first worker close marker" in rendered_notes
    assert rendered_notes.count("later worker close marker") == 1
    assert len(notes) <= 6
    assert deps.closed is True
    assert runtime.close_attempts == 1
    assert runtime.listener_stopped is True
    assert runtime.channel.listener_alive is False
    assert file_close_calls == [1]
    assert channel_close_calls == [1]
    assert opened and all(connection.close_calls == 1 for connection in opened)
    _assert_session_lock_reacquirable(session_path)
    assert _scalar(database, "SELECT COUNT(*) FROM plans") == (0,)


@pytest.mark.parametrize("close_control", [False, True],
                         ids=["ordinary-close-error", "later-close-control"])
def test_cli_notification_close_keeps_original_control(
        tmp_path: Path, monkeypatch, close_control: bool) -> None:
    from camctl import cli
    from camctl.motor import notification

    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    config_file = _write_cli_config(config, tmp_path / "notification-close.toml")
    session_path, admission_path = locks.lock_file_paths(
        Path(config.paths.state_db))
    control = asyncio.CancelledError("original session lock control")
    control.add_note("original session control note")
    backend = _FaultingLockBackend("try_lock", control)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, session_backend=backend,
        admission_path=admission_path,
    )
    built = []
    real_build = lifecycle.build_runtime

    def observe_runtime(*args, **kwargs):
        deps = real_build(*args, **kwargs)
        built.append(deps)
        return deps

    monkeypatch.setattr(lifecycle, "build_runtime", observe_runtime)
    close_error = (asyncio.CancelledError("later notification close control")
                   if close_control else
                   OSError(errno.EIO, "notification close error"))
    read_fd, write_fd = os.pipe()
    close_calls = []

    def close_then_fail(fd):
        close_calls.append(fd)
        assert built[-1].closed is True
        assert built[-1].log_runtime.channel.listener_alive is False
        os.close(fd)
        raise close_error

    writer = notification.NotificationWriter(
        write_fd, pipe_buf=os.fpathconf(write_fd, "PC_PIPE_BUF"),
        close=close_then_fail)

    def open_writer(fd):
        assert fd == write_fd
        return writer

    monkeypatch.setattr(notification, "open_notification_writer", open_writer)
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            cli.main(["run", "--config", str(config_file),
                      "--host-notification-fd", str(write_fd)], stdout, stderr)
    finally:
        os.close(read_fd)
        if not close_calls:
            os.close(write_fd)
        for deps in built:
            close_runtime(deps)

    assert raised.value is control
    assert close_calls == [write_fd]
    assert writer.available is False
    with pytest.raises(OSError) as closed:
        os.fstat(write_fd)
    assert closed.value.errno == errno.EBADF
    assert stdout.getvalue() == ""
    assert str(close_error) in stderr.getvalue()
    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert "original session control note" in notes
    assert str(close_error) in notes
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 0, "close": 1}


def test_close_runtime_fallback_closes_channel_after_handler_error(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.SUBMIT, config)
    runtime = deps.log_runtime
    assert runtime.close_attempts == 0
    closer_error = OSError(errno.EIO, "fallback file handler close marker")
    real_file_closer = runtime.file_closer
    file_close_calls: list[int] = []
    channel_close_calls: list[int] = []
    real_async_close = runtime.channel.async_close

    def fail_after_real_handler_close():
        file_close_calls.append(1)
        assert real_file_closer is not None
        real_file_closer()
        raise closer_error

    def track_async_close():
        channel_close_calls.append(1)
        return real_async_close()

    runtime.file_closer = fail_after_real_handler_close
    runtime.channel.async_close = track_async_close
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    close_error: BaseException | None = None
    try:
        close_runtime(deps)
    except BaseException as error:
        close_error = error
    finally:
        if not channel_close_calls:
            real_async_close()
        if not runtime.channel.listener_alive:
            runtime.channel.async_close = real_async_close

    assert close_error is None
    assert deps.closed is True
    assert runtime.close_attempts == 1
    assert runtime.listener_stopped is True
    assert runtime.channel.listener_alive is False
    assert file_close_calls == [1]
    assert channel_close_calls == [1]
    assert "fallback file handler close marker" in stderr.getvalue()


@pytest.mark.parametrize("cancel_log_wait", [False, True])
def test_cli_control_closes_copy_channel_without_reclassification(
        tmp_path: Path, monkeypatch, cancel_log_wait: bool) -> None:
    from camctl import cli

    config = _config_for(tmp_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    asyncio.run(_submit_report_plan(config, tmp_path, "710006"))
    config_file = _write_cli_config(config, tmp_path / "cli-control.toml")
    control = asyncio.CancelledError("first session unlock control")
    control.add_note("primary control note before logger cleanup")
    session_path, admission_path = locks.lock_file_paths(
        Path(config.paths.state_db))
    backend = _FaultingLockBackend("unlock", control)
    _patch_lock_acquisition(
        monkeypatch, session_path=session_path, session_backend=backend,
        admission_path=admission_path,
    )
    monkeypatch.setattr(WorkerSupervisor, "generate", _worker_report_failure)
    built = []
    original_build = lifecycle.build_runtime
    logger_close_error = OSError(errno.EIO, "later ordinary logger close error")
    copy_close_error = OSError(errno.EBADF, "later failure-copy handler close error")
    logger_close_calls: list[int] = []
    async_close_calls: list[int] = []
    copy_close_calls: list[int] = []
    logger_cleanup: list[tuple[object, object]] = []
    close_wait_parent = []
    original_close_log = lifecycle._close_log_runtime

    async def track_close_wait(*args, **kwargs):
        close_wait_parent[:] = [asyncio.get_running_loop(), asyncio.current_task()]
        return await original_close_log(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_close_log_runtime", track_close_wait)

    def capture_runtime(*args, **kwargs):
        deps = original_build(*args, **kwargs)
        built.append(deps)
        if deps.mode is CommandMode.RUN:
            runtime = deps.log_runtime
            assert runtime is not None
            real_closer = runtime.file_closer
            real_async_close = runtime.channel.async_close
            logger_cleanup.append((real_closer, real_async_close))
            real_stop = runtime.channel.stop_listener

            def cancel_during_real_stop(*args, **kwargs):
                if cancel_log_wait and close_wait_parent:
                    loop, parent = close_wait_parent
                    loop.call_soon_threadsafe(parent.cancel, "later log close wait cancel")
                return real_stop(*args, **kwargs)

            runtime.channel.stop_listener = cancel_during_real_stop

            def fail_logger_close_after_real_close():
                logger_close_calls.append(1)
                assert real_closer is not None
                real_closer()
                raise logger_close_error

            def track_async_close():
                async_close_calls.append(1)
                return real_async_close()

            runtime.file_closer = fail_logger_close_after_real_close
            runtime.channel.async_close = track_async_close
        return deps

    monkeypatch.setattr(lifecycle, "build_runtime", capture_runtime)
    opened = _tracked_open_connections(monkeypatch)
    original_channel_close = FileChannel.close

    def fail_copy_close_after_real_close(channel):
        deps = built[-1] if built else None
        if (deps is not None and deps.failure_channel is channel):
            copy_close_calls.append(1)
            original_channel_close(channel)
            raise copy_close_error
        return original_channel_close(channel)

    monkeypatch.setattr(FileChannel, "close", fail_copy_close_after_real_close)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)

    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            cli.main(["run", "--config", str(config_file)], stdout, stderr)
    finally:
        if built:
            deps = built[-1]
            if not deps.closed:
                with suppress(BaseException):
                    close_runtime(deps)
            if deps.log_runtime is not None and logger_cleanup:
                runtime = deps.log_runtime
                real_closer, real_async_close = logger_cleanup[-1]
                if runtime.channel.listener_alive:
                    with suppress(BaseException):
                        runtime.channel.stop_listener(0.1)
                if not async_close_calls:
                    with suppress(BaseException):
                        real_async_close()
                if not logger_close_calls and real_closer is not None:
                    with suppress(BaseException):
                        real_closer()
            if (deps.failure_channel is not None
                    and not deps.failure_channel.closed):
                with suppress(BaseException):
                    original_channel_close(deps.failure_channel)

    assert raised.value is control
    assert built
    deps = built[-1]
    assert deps.failure_channel is not None
    assert deps.failure_channel.closed is True
    copies_ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(copies_ready) == 1
    assert b"Task3 report failure reached the real failure-copy path" in (
        copies_ready[0].read_bytes())
    assert deps.closed is True
    assert deps.log_runtime.close_attempts == 1
    assert deps.log_runtime.channel.listener_alive is False
    assert logger_close_calls == [1]
    assert async_close_calls == [1]
    assert copy_close_calls == [1]
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 1, "close": 1}
    assert opened and all(connection.close_calls == 1 for connection in opened)
    _assert_session_lock_reacquirable(session_path)
    assert locks.probe_admission(admission_path).is_free is True
    assert stdout.getvalue() == ""
    diagnostics = stderr.getvalue()
    assert "later ordinary logger close error" in diagnostics
    assert "later failure-copy handler close error" in diagnostics
    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert "primary control note before logger cleanup" in notes
    assert "report_error" in notes
    assert "later ordinary logger close error" in notes
    assert "later failure-copy handler close error" in notes
