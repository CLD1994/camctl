"""真实报告监督和会话流程区别状态前提错误与报告文件失败。"""

import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import report_flow
from camctl.contracts.clock import ClockPort
from camctl.contracts.enums import enum_for
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting import generation
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.messages import (
    ErrorKind, JobMessage, ReadyMessage, ResultFailureMessage, ShutdownMessage,
    decode_message, encode_message,
)
from camctl.reporting.supervisor import WorkerSupervisor
from camctl.reporting.worker import run_job
from camctl.session.service import SessionContext, SessionPaths, _drive_flows

from ..capture.result_consumer_fixtures import ResultCatalog
from ..reporting.test_result_error_generation import _exhausted_world, _freeze, _spec
from ..reporting.test_result_error_state import _file_bytes, _incomplete_history_reader

pytestmark = pytest.mark.asyncio


class _InlineWorker:
    """真实管道与通信线程；发送原 Job 时执行真实 worker 并编码响应。"""

    def __init__(self, db_path):
        self.parent, self.child = multiprocessing.Pipe(duplex=True)
        self.alive = True
        self.jobs, self.results = [], []
        self.process = create_autospec(multiprocessing.Process, instance=True, spec_set=True)
        self.process.is_alive.side_effect = lambda: self.alive
        self.process.exitcode = None
        self.process.terminate.side_effect = self._terminate
        self.process.kill.side_effect = self._terminate
        transport = create_autospec(Connection, instance=True, spec_set=True)
        transport.fileno.side_effect = self.parent.fileno
        transport.recv_bytes.side_effect = self.parent.recv_bytes
        transport.close.side_effect = self.parent.close
        transport.send_bytes.side_effect = self._send
        unused_child = create_autospec(Connection, instance=True, spec_set=True)
        self.supervisor = WorkerSupervisor(
            limits=MaintenanceLimits(startup_seconds=2, total_seconds=5, stop_grace_seconds=0.2),
            lock_path=str(db_path.with_name(f"{db_path.name}.report.lock")),
            spawn=lambda: (self.process, transport, unused_child))
        self.child.send_bytes(encode_message(ReadyMessage()))

    def _terminate(self):
        self.alive = False
        self.process.exitcode = 1
        self.child.close()

    def _send(self, payload):
        message = decode_message(payload)
        if isinstance(message, JobMessage):
            self.jobs.append(message)
            result = run_job(message)
            self.results.append(result)
            self.child.send_bytes(encode_message(result))
        else:
            assert isinstance(message, ShutdownMessage)
            self.alive = False
            self.process.exitcode = 0
            self.child.close()

    async def close(self):
        await self.supervisor.stop()
        self.child.close()
        self.parent.close()


def _unused(*_args, **_kwargs):
    raise AssertionError("本用例只推进已装配 flow，不进入受理、会话锁或工作事实查询")


def _context(owned, runtime, spec, supervisor, following):
    metadata = owned.metadata
    cfg = load_config({"paths": {
        "state_db": str(spec.db_path), "staging": metadata.staging_path,
        "ready": metadata.ready_path, "processing": metadata.processing_path,
    }}, ConfigDefaults())
    clock = create_autospec(ClockPort, instance=True, spec_set=True)
    clock.utc_micros.return_value = runtime.wall_us() + 1
    maintenance = report_flow(
        state_db=spec.db_path, staging=Path(metadata.staging_path),
        ready=Path(metadata.ready_path), processing=Path(metadata.processing_path),
        history=cfg.history, database=cfg.database, supervisor=supervisor)
    return SessionContext(
        mode=CommandMode.RUN, catalog=ResultCatalog(), clock=clock,
        open_connection=lambda: open_existing(spec.db_path, DbOpenMode.EXISTING_RW, DbConfig()),
        acceptance_repository=AcceptanceRepository(), session_repository=SessionRepository(),
        paths=SessionPaths(spec.db_path.with_suffix(".session.lock"), spec.db_path.with_suffix(".admission.lock")),
        clock_policy=_unused, acquire_session=_unused, acquire_admission=_unused,
        facts_query=_unused, flows={"report": maintenance, "following": following})


def _snapshot(owned, report, runtime):
    return (
        owned.connection.execute("SELECT * FROM reports WHERE id=?", (report.report_id,)).fetchone(),
        owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall(),
        _file_bytes(Path(owned.metadata.ready_path)),
        _file_bytes(Path(owned.metadata.staging_path)),
        tuple(runtime.results.calls), runtime.driver.control.await_count,
    )


async def test_state_result_stops_following_flow_without_report_failure_write(tmp_path, monkeypatch):
    owned, runtime, action_id, error = await _exhausted_world(tmp_path, "timelapse", False)
    rig = None
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "unused-template.json")
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        reader, supplied = _incomplete_history_reader(spec, report, action_id, activity_id, error)
        monkeypatch.setattr(generation, "HistoryRepository",
                            create_autospec(HistoryRepository, spec_set=True, return_value=reader))
        rig = _InlineWorker(spec.db_path)
        following_calls = []

        async def following(context):
            following_calls.append(context)

        context = _context(owned, runtime, spec, rig.supervisor, following)
        before = _snapshot(owned, report, runtime)
        failure = await _drive_flows(context)

        assert len(rig.jobs) == len(rig.results) == 1
        assert supplied == [report.boundary]
        result = rig.results[0]
        assert isinstance(result, ResultFailureMessage)
        assert result.error_kind is ErrorKind.STATE
        assert failure is not None
        assert not following_calls
        assert not rig.supervisor.reusable and not rig.process.is_alive()
        assert not Path(rig.jobs[0].staging_path).exists()
        assert _snapshot(owned, report, runtime) == before
    finally:
        if rig is not None:
            await rig.close()
        owned.connection.close()


async def test_report_io_result_records_failure_and_drives_following_flow(tmp_path, monkeypatch):
    owned, runtime, _action_id, _error = await _exhausted_world(tmp_path, "timelapse", False)
    rig = None
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "unused-template.json")
        rig = _InlineWorker(spec.db_path)
        syncs = []

        def failed_fsync(fd):
            payload = Path(rig.jobs[-1].staging_path).read_bytes()
            assert payload
            syncs.append((fd, payload))
            raise OSError("report fsync refused")

        monkeypatch.setattr(generation.os, "fsync", failed_fsync)
        following_calls = []

        async def following(context):
            following_calls.append(context)

        context = _context(owned, runtime, spec, rig.supervisor, following)
        before = _snapshot(owned, report, runtime)
        failure = await _drive_flows(context)

        assert len(rig.jobs) == len(rig.results) == len(syncs) == 1
        result = rig.results[0]
        assert isinstance(result, ResultFailureMessage)
        assert result.error_kind is ErrorKind.REPORT and result.error_code == "OSError"
        assert failure is None and following_calls == [context]
        assert not rig.supervisor.reusable and not rig.process.is_alive()
        job = rig.jobs[0]
        assert not Path(job.staging_path).exists()
        status, last_error, frozen, from_wm, to_wm, size, sha, publications = owned.connection.execute(
            "SELECT status,last_error_json,frozen_event_id,from_wm,to_wm,size_bytes,sha256,publication_count"
            " FROM reports WHERE id=?", (report.report_id,)).fetchone()
        assert status == int(enum_for("reports.status").FAILED)
        assert "OSError" in json.loads(last_error)["error"]
        assert (frozen, from_wm, to_wm) == (report.boundary.last_event_id, report.from_wm, report.to_wm)
        assert (size, sha, publications) == (None, None, 0)
        after = _snapshot(owned, report, runtime)
        assert after[1][:len(before[1])] == before[1]
        assert len(after[1]) > len(before[1]), "真实报告失败责任必须可靠写入历史"
        assert after[2:] == before[2:]
    finally:
        if rig is not None:
            await rig.close()
        owned.connection.close()
