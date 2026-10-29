"""公开通信和默认会话交付报告故障，实际文件与存储保存未完成责任。"""

import asyncio
import json
from threading import Event
import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.reporting import publication
from camctl.reporting.messages import MessageProtocolError, ReadyMessage

from ..support.reporting import report_world, run_report_session
from .test_supervisor_transport import _Rig


def _error():
    cause = PermissionError("报告目录拒绝访问")
    error = OSError("报告文件写入失败")
    error.__cause__ = cause
    error.add_note("已知报告生成身份=job-7")
    return error


class _Endpoint:
    """在真实管道的发送或接收边界交付错误，不接触监督者状态。"""

    def __init__(self, connection):
        self.connection = connection
        self.send_error = None
        self.receive_error = None
        self.received = 0
        self.failed = Event()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def send_bytes(self, data):
        if self.send_error is not None:
            raise self.send_error
        return self.connection.send_bytes(data)

    def recv_bytes(self, *args):
        self.received += 1
        if self.received > 1 and self.receive_error is not None:
            self.failed.set()
            raise self.receive_error
        return self.connection.recv_bytes(*args)


@pytest.mark.asyncio
async def test_supervisor_shutdown_send_failure_has_complete_diagnostic():
    rig = _Rig()
    endpoint = _Endpoint(rig.parent_end)
    rig.parent_end = endpoint
    rig.peer.send(ReadyMessage())
    assert (await rig.supervisor.start()).ready
    endpoint.send_error = _error()
    diagnostics = []
    try:
        with error_diagnostics_context(None, diagnostics.append):
            await rig.supervisor.stop()
            await flush_diagnostics()
    finally:
        rig.peer.close()
    assert not rig.process.alive
    assert len(diagnostics) == 1
    for fact in ("report_shutdown_send", "报告目录拒绝访问", "traceback"):
        assert fact in diagnostics[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, MessageProtocolError])
async def test_explicit_stop_retains_channel_terminal_error_diagnostic(error_type):
    rig = _Rig()
    endpoint = _Endpoint(rig.parent_end)
    failure = error_type("收场通道的实际错误")
    failure.__cause__ = PermissionError("完整原因")
    endpoint.receive_error = failure
    rig.parent_end = endpoint
    rig.peer.send(ReadyMessage())
    assert (await rig.supervisor.start()).ready
    rig.peer.send(ReadyMessage())
    assert await asyncio.to_thread(endpoint.failed.wait, 2)
    rig.process.alive = False
    rig.process.exitcode = 0
    diagnostics = []
    try:
        with error_diagnostics_context(None, diagnostics.append):
            await rig.supervisor.stop()
            await flush_diagnostics()
    finally:
        rig.peer.close()
    assert not rig.process.alive
    assert len(diagnostics) == 1
    assert "收场通道的实际错误" in diagnostics[0]
    assert "PermissionError" in diagnostics[0]


def _input(request_id="1"):
    # 无效动作仍经真实受理生成一项必须报告的业务事实。
    return ParsedInput("input.json", {
        "request_id": request_id, "created_at": "2026-01-15 08:00:00",
        "name": "reporting", "actions": [{"name": "sync", "type": "report_status",
            "scheduled_at": "bad", "params": {"scope": "full"}}],
    })


def _rows(world, statement):
    with sqlite3.connect(world.database_path) as connection:
        return connection.execute(statement).fetchall()


@pytest.mark.asyncio
async def test_generation_failure_preserves_responsibility_until_next_default_session(tmp_path):
    world = report_world(tmp_path)
    # 子进程真实写 staging 时遇到目录被普通文件占据。
    blocker = world.staging / publication.REPORTS_DIRECTORY
    blocker.write_bytes(b"occupied")
    first = await run_report_session(world, _input())
    assert not first.succeeded and first.reason == "report_error", first.details
    assert _rows(world, "SELECT status,publication_count FROM reports") == [(5, 0)]
    assert not list(world.ready.glob("status-report-*.json"))
    assert _rows(world, "SELECT COUNT(*) FROM reports") == [(1,)]

    blocker.unlink()
    recovered = await run_report_session(world)
    assert recovered.succeeded, recovered.details
    files = list(world.ready.glob("status-report-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_bytes())["plans"][0]["actions"][0]["status"] == "failed"
    assert _rows(world, "SELECT status,publication_count FROM reports") == [(4, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_directories", [
    ("processing",), ("staging",), ("ready", "processing", "staging"),
])
async def test_recovery_diagnoses_every_executed_directory_observation(tmp_path, monkeypatch, failed_directories):
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.acceptance.service import AcceptanceContext, CommandMode
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.reporting.generation import GenerationSpec, generate_report_file
    from camctl.reporting.models import ReportBytes
    from camctl.reporting.policy import ReportingRepository, record_report_bytes
    from ..support.acceptance import accept_source

    world = report_world(tmp_path)
    accepted = await accept_source(_input(), AcceptanceContext(
        mode=CommandMode.RUN, catalog=build_catalog(world.config, DriverDefinitions({})),
        repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: 1)})()), world.database_path)
    assert accepted.plan_id is not None
    owned = open_existing(world.database_path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        database_id = owned.metadata.instance_id
        frozen = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=1)
        assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
        report = frozen.value.report
        generated = generate_report_file(GenerationSpec(
            world.database_path, report.report_id, report.from_wm, report.to_wm,
            report.boundary.last_event_id, world.staging / "reports/prepared.json"))
        saved = record_report_bytes(new_operation_key(), owned, report.report_id,
            ReportBytes(generated.size_bytes, generated.sha256))
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        # 文件已移动但发布事实尚未提交，下一会话必须核对真实目录。
        generated.path.rename(world.ready / publication.report_file_name(1, generated.sha256))
    finally:
        owned.connection.close()
    original_files = {p.name: p.read_bytes() for p in world.ready.iterdir()}
    observed = []
    original_listdir = publication.os.listdir
    paths = {"ready": world.ready, "processing": world.processing,
             "staging": world.staging / publication.REPORTS_DIRECTORY}

    def list_directory(path):
        name = next((name for name, value in paths.items() if Path(path) == value), None)
        if name in failed_directories:
            observed.append(name)
            failure = PermissionError(f"{name}-observation-broke")
            failure.__cause__ = OSError(f"{name}-original-cause")
            raise failure
        return original_listdir(path)

    monkeypatch.setattr(publication.os, "listdir", list_directory)
    outcome = await run_report_session(world)
    assert outcome.succeeded is ("ready" not in failed_directories), outcome.details
    if "ready" in failed_directories:
        assert outcome.reason == "state_db_error"
    assert sorted(observed) == sorted(failed_directories)
    entries = [json.loads(line) for line in (world.home / "camctl.log").read_text().splitlines()
               if line.startswith("{")]
    diagnostics = [entry for entry in entries if entry.get("stage") == "report_directory_observe"]
    assert len(diagnostics) == len(failed_directories)
    for name in failed_directories:
        matched = [entry for entry in diagnostics if entry["context"]["directory"] == name]
        assert len(matched) == 1
        facts = json.dumps(matched[0], ensure_ascii=False)
        assert f"{name}-observation-broke" in facts
        assert f"{name}-original-cause" in facts
        assert matched[0]["context"]["report_id"] == 1
        assert matched[0]["context"]["database_id"] == database_id
    monkeypatch.setattr(publication.os, "listdir", original_listdir)
    assert {p.name: p.read_bytes() for p in world.ready.iterdir()} == original_files


@pytest.mark.asyncio
async def test_failure_store_sql_error_stops_default_report_session(tmp_path):
    world = report_world(tmp_path)
    with sqlite3.connect(world.database_path) as connection:
        connection.execute("CREATE TRIGGER report_failure_refused BEFORE UPDATE ON reports "
            "WHEN NEW.status=5 BEGIN SELECT RAISE(ABORT,'failure-store-broke'); END")
    blocker = world.staging / publication.REPORTS_DIRECTORY
    blocker.write_bytes(b"occupied")
    result = await run_report_session(world, _input())
    assert not result.succeeded and result.reason == "state_db_error", result.details
    assert _rows(world, "SELECT status,publication_count FROM reports") == [(1, 0)]
    assert not list(world.ready.glob("status-report-*.json"))
    entries = [json.loads(line) for line in (world.home / "camctl.log").read_text().splitlines()
               if line.startswith("{")]
    failures = [entry for entry in entries if entry.get("stage") == "report"]
    assert len(failures) == 1
    assert "failure-store-broke" in json.dumps(failures[0], ensure_ascii=False)
    generations = [entry for entry in entries if entry.get("stage") == "report_generate"]
    assert len(generations) == 1
    assert "FileExistsError" in json.dumps(generations[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_restricted_default_session_publishes_once_and_exits_with_clock_error(tmp_path):
    from dataclasses import replace
    from datetime import date
    from decimal import Decimal
    from camctl.acceptance.service import AcceptanceContext, CommandMode
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..support.acceptance import accept_source

    world = report_world(tmp_path)
    accepted = await accept_source(_input(), AcceptanceContext(
        mode=CommandMode.RUN, catalog=build_catalog(world.config, DriverDefinitions({})),
        repository=AcceptanceRepository(),
        clock=type("Clock", (), {"utc_micros": staticmethod(lambda: 1)})()), world.database_path)
    assert accepted.plan_id is not None
    restricted = replace(world.config, clock=replace(world.config.clock,
        min_plausible_date=date.max, recheck_delay_s=Decimal(0), recheck_count=1))
    outcome = await run_report_session(world, config=restricted)
    assert not outcome.succeeded and outcome.reason == "clock_invalid", outcome.details
    assert _rows(world, "SELECT status,publication_count FROM reports") == [(4, 1)]
    assert len(list(world.ready.glob("status-report-*.json"))) == 1


@pytest.mark.asyncio
async def test_new_committed_input_retries_failed_report_in_same_default_session(tmp_path, monkeypatch):
    from camctl.acceptance.service import AcceptanceContext, CommandMode
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..support.acceptance import accept_source

    world = report_world(tmp_path)
    original_listdir = publication.os.listdir
    accepted = []

    def list_directory(path):
        if Path(path) == world.ready and not accepted:
            context = AcceptanceContext(CommandMode.RUN,
                build_catalog(world.config, DriverDefinitions({})), AcceptanceRepository(),
                clock=type("Clock", (), {"utc_micros": staticmethod(lambda: 2)})())
            # 文件工作线程中的外部提交，真实受理事务与当前会话并行。
            accepted.append(asyncio.run(accept_source(_input("2"), context, world.database_path)))
            raise _error()
        return original_listdir(path)

    monkeypatch.setattr(publication.os, "listdir", list_directory)
    result = await run_report_session(world, _input())
    assert result.succeeded, result.details
    assert len(accepted) == 1 and accepted[0].plan_id == 2
    files = list(world.ready.glob("status-report-*.json"))
    assert len(files) == 1
    document = json.loads(files[0].read_bytes())
    assert {plan["request_id"] for plan in document["plans"]} == {"1", "2"}
    assert document["from_wm"] == 0 and document["to_wm"] == 4
    assert _rows(world, "SELECT COUNT(*) FROM reports") == [(2,)]
    entries = [json.loads(line) for line in (world.home / "camctl.log").read_text().splitlines()
               if line.startswith("{")]
    failures = [entry for entry in entries if entry.get("stage") == "report_file_list"]
    assert len(failures) == 1
    facts = json.dumps(failures[0], ensure_ascii=False)
    assert "报告文件写入失败" in facts and "报告目录拒绝访问" in facts
