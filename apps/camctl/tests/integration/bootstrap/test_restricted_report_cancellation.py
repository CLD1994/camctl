"""已派发的一次受限报告在父等待取消后仍交付实际结果。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.logging_runtime.ownership import LoggingOwner
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.reporting.communication import WorkerCommunicator
from camctl.reporting.messages import JobMessage
from camctl.reporting.models import ReportStatus
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.reporting.publication import parse_report_file_name
from camctl.reporting.supervisor import WorkerSupervisor
from camctl.session.locks import SessionLockConflictError, acquire_session

import camctl.bootstrap.lifecycle as lifecycle_module

register_acceptance_guards()
register_report_guards()
register_sync_guard()

pytestmark = pytest.mark.asyncio


class _Catalog:
    """仅供本用例受理 report_status 的静态目录。"""

    def action_types(self):
        return frozenset({"report_status"})

    def device_exists(self, device_id):
        return False

    def driver_id(self, device_id):
        return None

    def device_supports(self, device_id, action_type):
        return False

    def parameter_definition(self, device_id, action_type, parameter_type):
        return None


def _config_for(home: Path):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "clock": {
                "min_plausible_date": "2100-01-01",
                "recheck_delay_s": "0.01",
            },
        },
        ConfigDefaults(),
    )


async def _parsed_input(path: Path):
    body = {
        "request_id": "1",
        "created_at": "2026-01-15 08:00:00",
        "name": "restricted-report-cancellation",
        "actions": [{
            "name": "full-status",
            "type": "report_status",
            "params": {"scope": "full"},
        }],
    }
    path.write_text(json.dumps(body), encoding="utf-8")

    class Reader:
        def read(self, source: str) -> bytes:
            return Path(source).read_bytes()

    return parse_input(await read_input(str(path), Reader()))


def _report_row(db_path: Path, report_id: int):
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT id, status, size_bytes, sha256, last_error_json"
            " FROM reports WHERE id = ?", (report_id,),
        ).fetchone()
    finally:
        connection.close()


def _session_lock_is_held(lock_path: Path) -> bool:
    try:
        lease = acquire_session(lock_path)
    except SessionLockConflictError:
        return True
    lease.close()
    return False


def _connection_is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1").fetchone()
    except sqlite3.ProgrammingError as error:
        return "closed" in str(error).lower()
    return False


async def _wait_for_event(event: asyncio.Event, description: str) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=20)
    except asyncio.TimeoutError:
        pytest.fail(f"等待{description}超时")


@pytest.mark.parametrize(
    "report_generation_fails",
    [False, True],
    ids=["actual-generation-success", "actual-generation-failure"],
)
async def test_started_restricted_report_cancellation_preserves_actual_result(
        tmp_path: Path, monkeypatch, report_generation_fails: bool) -> None:
    home = tmp_path / "runtime"
    home.mkdir()
    cfg = _config_for(home)
    db_path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db_path).outcome is InitOutcome.CREATED

    staging = Path(cfg.paths.staging)
    staging.mkdir(parents=True, exist_ok=True)
    if report_generation_fails:
        report_directory = staging / "reports"
        if report_directory.is_dir():
            assert not any(report_directory.iterdir())
            report_directory.rmdir()
        elif report_directory.exists():
            report_directory.unlink()
        report_directory.write_text("blocks report generation", encoding="utf-8")

    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    report_ready = asyncio.Event()
    release_report = asyncio.Event()
    local_stopped = asyncio.Event()
    startup_sweep_stopped = asyncio.Event()
    parent_job_send_cancelled = asyncio.Event()
    final_log_drain_started = asyncio.Event()

    jobs: list[JobMessage] = []
    runtime_connections = []
    report_connection: sqlite3.Connection | None = None
    retirement_start_release_states: list[bool] = []
    retirement_results: list[tuple[bool, int | None]] = []
    drain_observations: list[dict] = []

    real_open_runtime_connection = lifecycle_module._open_runtime_connection

    def observe_open_runtime_connection(active_deps):
        owned = real_open_runtime_connection(active_deps)
        runtime_connections.append(owned)
        return owned

    monkeypatch.setattr(
        lifecycle_module, "_open_runtime_connection",
        observe_open_runtime_connection,
    )

    real_local_stop = CombinedLocalWork.stop_new_work

    def observe_local_stop(owner):
        real_local_stop(owner)
        local_stopped.set()

    monkeypatch.setattr(CombinedLocalWork, "stop_new_work", observe_local_stop)

    real_cancel_startup = LoggingOwner.cancel_startup_sweep

    def observe_cancel_startup(owner):
        real_cancel_startup(owner)
        startup_sweep_stopped.set()

    monkeypatch.setattr(
        LoggingOwner, "cancel_startup_sweep", observe_cancel_startup,
    )

    real_logging_settle = LoggingOwner.settle

    async def observe_logging_settle(owner):
        nonlocal report_connection
        if jobs and report_connection is not None and not drain_observations:
            job = jobs[0]
            row = await asyncio.to_thread(_report_row, db_path, job.report_id)
            ready_names = tuple(sorted(
                path.name for path in Path(cfg.paths.ready).glob("status-report-*.json")
            ))
            connection_closed = _connection_is_closed(report_connection)
            lock_held = await asyncio.to_thread(
                _session_lock_is_held, Path(deps.session_lock),
            )
            drain_observations.append({
                "row": row,
                "ready_names": ready_names,
                "report_connection_closed": connection_closed,
                "session_lock_held": lock_held,
                "retirement_started": tuple(retirement_start_release_states),
                "report_worker_exitcodes": tuple(
                    code for _, code in retirement_results if code is not None
                ),
            })
            final_log_drain_started.set()
        await real_logging_settle(owner)

    monkeypatch.setattr(LoggingOwner, "settle", observe_logging_settle)

    real_send = WorkerCommunicator.send

    async def observe_send(communicator, message):
        nonlocal report_connection
        await real_send(communicator, message)
        if isinstance(message, JobMessage):
            jobs.append(message)
            if runtime_connections:
                report_connection = runtime_connections[-1].connection
            report_ready.set()
            try:
                await asyncio.wait_for(release_report.wait(), timeout=45)
            except asyncio.CancelledError:
                parent_job_send_cancelled.set()
                raise

    monkeypatch.setattr(WorkerCommunicator, "send", observe_send)

    real_retire = WorkerSupervisor._retire

    async def observe_retire(supervisor):
        retirement_start_release_states.append(release_report.is_set())
        result = await real_retire(supervisor)
        retirement_results.append((release_report.is_set(), result.exitcode))
        return result

    monkeypatch.setattr(WorkerSupervisor, "_retire", observe_retire)

    run_task: asyncio.Task | None = None
    try:
        source = await _parsed_input(tmp_path / "restricted-report.json")
        run_task = asyncio.create_task(
            execute_command(deps, source, poll_interval_s=0.01),
        )

        await _wait_for_event(report_ready, "真实报告任务派发")
        assert len(jobs) == 1
        job = jobs[0]
        assert report_connection is not None
        assert len(runtime_connections) >= 2

        before_cancel = await asyncio.to_thread(_report_row, db_path, job.report_id)
        assert before_cancel is not None
        assert before_cancel[1] != ReportStatus.PUBLISHED.value
        assert not list(Path(cfg.paths.ready).glob("status-report-*.json"))

        assert run_task.cancel("first-parent-cancel") is True
        await asyncio.gather(
            _wait_for_event(local_stopped, "本地新增工作停止"),
            _wait_for_event(startup_sweep_stopped, "启动检查停止"),
        )
        assert not run_task.done()
        assert not final_log_drain_started.is_set()
        assert not parent_job_send_cancelled.is_set()
        assert retirement_start_release_states == []
        assert await asyncio.to_thread(
            _session_lock_is_held, Path(deps.session_lock),
        )

        assert run_task.cancel("repeated-parent-cancel") is True
        # 将第二次取消送入仍被报告门闩阻塞的会话任务，再检查结果消费者尚未收场。
        await asyncio.sleep(0)
        assert not run_task.done()
        assert not parent_job_send_cancelled.is_set()
        assert retirement_start_release_states == []
        assert not drain_observations

        release_report.set()
        await _wait_for_event(final_log_drain_started, "报告后的最终日志收场")
        assert len(drain_observations) == 1
        observation = drain_observations[0]
        assert observation["row"] is not None
        assert observation["row"][0] == job.report_id
        assert observation["report_connection_closed"] is True
        assert observation["session_lock_held"] is True

        if report_generation_fails:
            assert observation["row"][1] == ReportStatus.FAILED.value
            assert observation["row"][4] is not None
            assert observation["ready_names"] == ()
            assert observation["retirement_started"]
            assert all(observation["retirement_started"])
            assert observation["report_worker_exitcodes"]
        else:
            assert observation["row"][1] == ReportStatus.PUBLISHED.value
            assert observation["row"][2] > 0
            assert observation["row"][3] is not None
            assert observation["row"][4] is None
            assert len(observation["ready_names"]) == 1
            # 健康空闲 worker 仍沿 execute_command 的原生命周期关闭。
            assert observation["retirement_started"] == ()
            assert observation["report_worker_exitcodes"] == ()

        done, pending = await asyncio.wait({run_task}, timeout=120)
        assert not pending, "报告与最终本地收场完成后父取消仍未传播"
        assert run_task in done
        with pytest.raises(asyncio.CancelledError) as cancelled:
            run_task.result()
        assert cancelled.value.args == ("first-parent-cancel",)

        assert len(jobs) == 1
        assert not parent_job_send_cancelled.is_set()
        final_row = await asyncio.to_thread(_report_row, db_path, job.report_id)
        assert final_row is not None
        assert final_row[1] == (
            ReportStatus.FAILED.value if report_generation_fails
            else ReportStatus.PUBLISHED.value
        )

        ready_files = sorted(Path(cfg.paths.ready).glob("status-report-*.json"))
        if report_generation_fails:
            assert ready_files == []
        else:
            assert len(ready_files) == 1
            payload = ready_files[0].read_bytes()
            identity = parse_report_file_name(ready_files[0].name)
            assert identity is not None
            assert identity.report_id == job.report_id
            assert identity.sha256 == final_row[3]
            assert len(payload) == final_row[2]
            assert hashlib.sha256(payload).hexdigest() == final_row[3]
            assert isinstance(json.loads(payload), dict)

        assert any(code is not None for _, code in retirement_results)
        assert not await asyncio.to_thread(
            _session_lock_is_held, Path(deps.session_lock),
        )
    finally:
        release_report.set()
        try:
            if run_task is not None and not run_task.done():
                run_task.cancel("test-cleanup")
                done, pending = await asyncio.wait({run_task}, timeout=60)
                if pending:
                    run_task.cancel("test-cleanup-retry")
                    done, pending = await asyncio.wait({run_task}, timeout=30)
                assert not pending, "测试清理未能收场真实 run 任务"
            if run_task is not None and run_task.done():
                try:
                    run_task.result()
                except BaseException:
                    pass
        finally:
            close_runtime(deps)
