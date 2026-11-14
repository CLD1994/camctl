"""已派发的一次受限报告在父等待取消后仍交付实际结果。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.reporting.communication import WorkerCommunicator
from camctl.reporting.messages import JobMessage
from camctl.reporting.models import ReportStatus
from camctl.reporting.publication import parse_report_file_name
from camctl.session.locks import SessionLockConflictError, acquire_session

from ._connection_tracking import _tracked_open_connections



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
    parent_job_send_cancelled = asyncio.Event()
    jobs: list[JobMessage] = []
    previous_children = set(multiprocessing.active_children())
    report_children = []
    connections = _tracked_open_connections(monkeypatch, db_path)
    real_send = WorkerCommunicator.send

    async def observe_send(communicator, message):
        await real_send(communicator, message)
        if isinstance(message, JobMessage):
            jobs.append(message)
            report_children.extend(set(multiprocessing.active_children()) - previous_children)
            report_ready.set()
            try:
                await asyncio.wait_for(release_report.wait(), timeout=45)
            except asyncio.CancelledError:
                parent_job_send_cancelled.set()
                raise

    monkeypatch.setattr(WorkerCommunicator, "send", observe_send)

    run_task: asyncio.Task | None = None
    try:
        source = await _parsed_input(tmp_path / "restricted-report.json")
        run_task = asyncio.create_task(
            execute_command(deps, source, poll_interval_s=0.01),
        )

        await _wait_for_event(report_ready, "真实报告任务派发")
        assert len(jobs) == 1
        job = jobs[0]
        assert len(report_children) == 1
        report_child = report_children[0]
        assert connections
        assert all(connection.close_calls == 0 for connection in connections)

        assert run_task.cancel("first-parent-cancel") is True
        await asyncio.sleep(0)
        assert not run_task.done()
        assert not parent_job_send_cancelled.is_set()
        assert await asyncio.to_thread(
            _session_lock_is_held, Path(deps.session_lock),
        )
        assert connections and all(connection.close_calls == 0 for connection in connections)

        assert run_task.cancel("repeated-parent-cancel") is True
        await asyncio.sleep(0)
        assert not run_task.done()
        assert not parent_job_send_cancelled.is_set()
        assert await asyncio.to_thread(
            _session_lock_is_held, Path(deps.session_lock),
        )
        assert connections and all(connection.close_calls == 0 for connection in connections)
        release_report.set()
        done, pending = await asyncio.wait({run_task}, timeout=120)
        assert not pending, "报告与最终本地收场完成后父取消仍未传播"
        assert run_task in done
        with pytest.raises(asyncio.CancelledError) as cancelled:
            run_task.result()
        assert cancelled.value.args == ("first-parent-cancel",)

        assert len(jobs) == 1
        assert not parent_job_send_cancelled.is_set()
        assert connections and all(
            connection.close_calls == 1 and connection.unusable_after_close
            for connection in connections)
        final_row = await asyncio.to_thread(_report_row, db_path, job.report_id)
        assert final_row is not None
        assert final_row[1] == (
            ReportStatus.FAILED.value if report_generation_fails
            else ReportStatus.PUBLISHED.value
        )

        ready_files = sorted(Path(cfg.paths.ready).glob("status-report-*.json"))
        if report_generation_fails:
            error = json.loads(final_row[4])["error"]
            assert "FileExistsError" in error
            assert str(staging / "reports") in error
            assert ready_files == []
        else:
            assert final_row[2] > 0 and final_row[3] is not None
            assert final_row[4] is None
            assert len(ready_files) == 1
            payload = ready_files[0].read_bytes()
            identity = parse_report_file_name(ready_files[0].name)
            assert identity is not None
            assert identity.report_id == job.report_id
            assert identity.sha256 == final_row[3]
            assert len(payload) == final_row[2]
            assert hashlib.sha256(payload).hexdigest() == final_row[3]
            assert isinstance(json.loads(payload), dict)

        assert report_child.exitcode is not None
        assert not report_child.is_alive()
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
