"""真实报告监督和会话流程区别状态前提错误与报告文件失败。"""

import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import report_flow
from camctl.contracts.clock import ClockPort
from camctl.contracts.enums import enum_for
from camctl.reporting import generation, worker
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.messages import (
    ErrorKind, JobMessage, ReadyMessage, ResultFailureMessage, ShutdownMessage,
    decode_message, encode_message,
)
from camctl.reporting.supervisor import WorkerSupervisor
from camctl.reporting.worker import run_job
from ..support.bootstrap_capture_cases import bootstrap_case, exhausted_result_world, run_flow_round

from ..capture.result_consumer_fixtures import ResultCatalog
from ..reporting.test_result_error_generation import _freeze, _spec
from ..reporting.test_result_error_state import _file_bytes, _incomplete_history_generator

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


def _context(owned, runtime, spec, supervisor, following):
    from camctl.persistence.directory_switch import canonical_to_path
    metadata = owned.metadata
    cfg = load_config({"paths": {
        "state_db": str(spec.db_path),
        "staging": str(canonical_to_path(metadata.staging_path)),
        "ready": str(canonical_to_path(metadata.ready_path)),
        "processing": str(canonical_to_path(metadata.processing_path)),
    }}, ConfigDefaults())
    clock = create_autospec(ClockPort, instance=True, spec_set=True)
    clock.utc_micros.return_value = runtime.wall_us() + 1
    maintenance = report_flow(
        state_db=spec.db_path, staging=canonical_to_path(metadata.staging_path),
        ready=canonical_to_path(metadata.ready_path),
        processing=canonical_to_path(metadata.processing_path),
        history=cfg.history, database=cfg.database, supervisor=supervisor)
    return dict(clock=clock, catalog=ResultCatalog(),
        flows={"report": maintenance, "following": following})


def _snapshot(owned, report, runtime):
    from camctl.persistence.directory_switch import canonical_to_path
    return (
        owned.connection.execute("SELECT * FROM reports WHERE id=?", (report.report_id,)).fetchone(),
        owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall(),
        _file_bytes(canonical_to_path(owned.metadata.ready_path)),
        _file_bytes(canonical_to_path(owned.metadata.staging_path)),
        tuple(runtime.results.calls), runtime.driver.control.await_count,
    )


async def test_state_result_stops_following_flow_without_report_failure_write(tmp_path, monkeypatch, bootstrap_case):
    owned, runtime, action_id, error = await exhausted_result_world(bootstrap_case, tmp_path)
    rig = None
    try:
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "unused-template.json")
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        rig = _InlineWorker(spec.db_path)
        generate, supplied = _incomplete_history_generator(
            report, action_id, activity_id, error, current_job=lambda: rig.jobs[-1])
        monkeypatch.setattr(worker, "generate_report_file", generate)
        following_calls = []

        async def following(context):
            following_calls.append(context)

        context = _context(owned, runtime, spec, rig.supervisor, following)
        before = _snapshot(owned, report, runtime)
        failure = await run_flow_round(spec.db_path, **context)

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


async def test_report_io_result_records_failure_and_drives_following_flow(tmp_path, monkeypatch, bootstrap_case):
    owned, runtime, _action_id, _error = await exhausted_result_world(bootstrap_case, tmp_path)
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
        failure = await run_flow_round(spec.db_path, **context)

        assert len(rig.jobs) == len(rig.results) == len(syncs) == 1
        result = rig.results[0]
        assert isinstance(result, ResultFailureMessage)
        assert result.error_kind is ErrorKind.REPORT and result.error_code == "OSError"
        assert failure is None and len(following_calls) == 1
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
