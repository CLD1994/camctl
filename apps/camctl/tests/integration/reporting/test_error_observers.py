"""报告处理后继续的失败仍交付完整诊断，文件边界由替身隔离。"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.host_files.handoff import HandoffDirectories
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.reporting import publication
from camctl.reporting.messages import MessageProtocolError
from camctl.reporting.maintenance import MaintenanceLimits, ReportMaintenanceContext, ReportPorts, maintain_reports
from camctl.persistence.models import DbOutcomeKind


async def _inline(function, *args, **kwargs):
    return function(*args, **kwargs)


def _error():
    try:
        raise PermissionError("报告目录拒绝访问")
    except PermissionError as cause:
        try:
            raise OSError("报告文件写入失败") from cause
        except OSError as error:
            error.add_note("已知报告生成身份=job-7")
            return error


@pytest.mark.asyncio
async def test_report_write_failure_retains_original_exception_graph(monkeypatch):
    error = _error()
    monkeypatch.setattr(publication.asyncio, "to_thread", _inline)
    monkeypatch.setattr(publication.Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    session = create_autospec(publication.PublicationSession, instance=True)
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        result = await publication.deliver_report(7, b"payload", HandoffDirectories(Path("/staging"), Path("/ready")), session)
        await flush_diagnostics()

    assert result.outcome is publication.DeliveryOutcome.WRITE_FAILED
    assert len(diagnostics) == 1
    for fact in ("PermissionError", "报告目录拒绝访问", "报告文件写入失败", "job-7", "traceback", '"report_id":7'):
        assert fact in diagnostics[0]
    session.prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_simple_report_generation_failure_has_diagnostic():
    ports = create_autospec(ReportPorts, instance=True)
    ports.has_new_changes.return_value = True
    ports.generate.side_effect = _error()
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        await maintain_reports(ReportMaintenanceContext(MaintenanceLimits.defaults(), ports))
        await flush_diagnostics()

    assert len(diagnostics) == 1
    assert "报告目录拒绝访问" in diagnostics[0]
    assert "traceback" in diagnostics[0]


@pytest.mark.asyncio
async def test_generation_retains_failed_startup_exception_and_message(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from camctl.reporting.messages import JobMessage, StartupFailedMessage, StartupPhase
    from camctl.reporting.supervisor import WorkerSupervisor
    error = _error()
    message = StartupFailedMessage(StartupPhase.WORK_LOCK, "启动锁失败")
    supervisor = WorkerSupervisor(limits=MaintenanceLimits.defaults(), lock_path="/work.lock")
    monkeypatch.setattr(supervisor, "start", AsyncMock(return_value=SimpleNamespace(
        ready=False, detail="启动失败", failure=message, exception=error, stopped=False)))
    job = JobMessage("job-7", 7, 0, 1, 1, "a" * 32, "/state.db", "/staging/report.tmp",
        "/staging", "/ready", "/processing", 1, 1, 1)

    result = await supervisor.generate(job)

    assert result.exception is error
    assert result.startup_failure is message


@pytest.mark.asyncio
async def test_supervisor_shutdown_send_failure_has_complete_diagnostic(monkeypatch):
    from unittest.mock import AsyncMock
    from camctl.reporting.communication import ChannelClosed, WorkerCommunicator
    from camctl.reporting.supervisor import ProcessHandle, WorkerSupervisor
    supervisor = WorkerSupervisor(limits=MaintenanceLimits.defaults(), lock_path="/work.lock")
    supervisor._process = create_autospec(ProcessHandle, instance=True)
    supervisor._process.is_alive.return_value = True
    channel = create_autospec(WorkerCommunicator, instance=True)
    channel.send.side_effect = _error()
    channel.receive.return_value = ChannelClosed(None)
    supervisor._communicator = channel
    monkeypatch.setattr(supervisor, "_join_within", AsyncMock(return_value=True))
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        await supervisor.stop()
        await flush_diagnostics()

    assert len(diagnostics) == 1
    for fact in ("report_shutdown_send", "报告目录拒绝访问", "traceback"):
        assert fact in diagnostics[0]


@pytest.mark.parametrize("phase", ["receive", "decode", "close"])
def test_worker_control_channel_errors_are_complete_stderr_diagnostics(mocker, phase):
    from multiprocessing.connection import Connection
    from camctl.reporting import worker
    from camctl.reporting.messages import ShutdownMessage, encode_message
    channel = create_autospec(Connection, instance=True)
    channel.recv_bytes.return_value = b"invalid-json" if phase == "decode" else encode_message(ShutdownMessage())
    if phase == "receive":
        channel.recv_bytes.side_effect = _error()
    if phase == "close":
        channel.close.side_effect = _error()
    mocker.patch.object(worker, "install_parent_guard", return_value=None)
    mocker.patch.object(worker, "acquire_work_lock", autospec=True)
    mocker.patch.object(worker, "ensure_runtime_library", autospec=True)
    output = bytearray()
    mocker.patch.object(worker.os, "write", side_effect=lambda fd, data: (output.extend(data), len(data))[1])

    worker.worker_main(channel, 1, "/state.lock", 0)

    assert output
    decoded = output.decode("utf-8")
    assert "traceback" in decoded
    if phase == "decode":
        assert "MessageProtocolError" in decoded
    else:
        assert "PermissionError" in decoded
        assert "report_worker_" + phase in decoded


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, MessageProtocolError])
async def test_explicit_stop_retains_channel_terminal_error_diagnostic(error_type):
    from camctl.reporting.communication import ChannelClosed, WorkerCommunicator
    from camctl.reporting.supervisor import WorkerSupervisor
    supervisor = WorkerSupervisor(limits=MaintenanceLimits.defaults(), lock_path="/work.lock")
    channel = create_autospec(WorkerCommunicator, instance=True)
    failure = error_type("收场通道的实际错误")
    failure.__cause__ = PermissionError("完整原因")
    channel.receive.return_value = ChannelClosed(failure)
    supervisor._communicator = channel
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        await supervisor.stop()
        await flush_diagnostics()

    assert len(diagnostics) == 1
    assert "收场通道的实际错误" in diagnostics[0]
    assert "PermissionError" in diagnostics[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", [
    DbOutcomeKind.NOT_EXECUTED,
    DbOutcomeKind.ROLLED_BACK,
    DbOutcomeKind.UNKNOWN,
])
@pytest.mark.parametrize("moved", [False, True])
async def test_report_file_failure_requires_reliable_failure_store(monkeypatch, store_kind, moved):
    import json
    from unittest.mock import AsyncMock
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    file_error = _error()
    store_error = OSError("failure-store-broke")
    secondary = OSError("failure-store-close-broke")
    session = create_autospec(publication.PublicationSession, instance=True)
    session.prepare.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    session.record_intent.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    session.record_failure.return_value = DbOutcome(store_kind, error=store_error, secondary_errors=(secondary,))
    monkeypatch.setattr(publication.asyncio, "to_thread", _inline)
    monkeypatch.setattr(publication, "_list_stale_ready", lambda *args: ((), None))
    move = AsyncMock(return_value=PublishResult(
        PublishStage.MOVED if moved else PublishStage.NOT_MOVED,
        DirectorySyncStage.FAILED if moved else DirectorySyncStage.NOT_ATTEMPTED,
        moved, "report-file-broke", file_error))
    monkeypatch.setattr(publication, "_move_staged", move)
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        result = await publication.deliver_staged_report(7,
            publication.StagedReport(name=publication.report_file_name(7, "a" * 64), size_bytes=7, sha256="a" * 64),
            HandoffDirectories(Path("/staging"), Path("/ready")), session,
            context={"database_id": "b" * 32})
        await flush_diagnostics()

    assert result.outcome is (publication.DeliveryOutcome.STORE_FAILED
        if store_kind is DbOutcomeKind.ROLLED_BACK else publication.DeliveryOutcome.UNKNOWN)
    saved_error = json.dumps(exception_diagnostic(result.exception), ensure_ascii=False)
    assert "failure-store-broke" in saved_error
    assert "failure-store-close-broke" in saved_error
    assert len(diagnostics) == 2
    assert any("报告目录拒绝访问" in diagnostic for diagnostic in diagnostics)
    assert any("failure-store-broke" in diagnostic and "failure-store-close-broke" in diagnostic
        for diagnostic in diagnostics)
    move.assert_awaited_once()
    session.record_failure.assert_awaited_once()
    session.save_publication.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_directories", [
    ("processing",), ("staging",), ("ready", "processing", "staging"),
])
async def test_recovery_diagnoses_every_executed_directory_observation(monkeypatch, failed_directories):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from camctl.bootstrap import flows
    from camctl.logging_runtime.diagnostics import record_error
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbOutcome
    from camctl.reporting.supervisor import WorkerSupervisor
    from camctl.session.service import StateDbFailure

    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = SimpleNamespace(instance_id="a" * 32)
    database.read.return_value = (7,)
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: 1))
    supervisor = create_autospec(WorkerSupervisor, instance=True)
    flow = flows.report_flow(state_db=Path("/state.db"), staging=Path("/staging"),
        ready=Path("/ready"), processing=Path("/processing"), history=None,
        database=None, supervisor=supervisor, start_actions=False)

    def rows(database, sql, parameters=()):
        return [(2, 1)] if "SELECT status, to_wm FROM reports" in sql else []

    monkeypatch.setattr(flows, "_query_rows", AsyncMock(side_effect=rows))
    save = AsyncMock(return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    monkeypatch.setattr(flows, "_policy_write", save)
    errors = {name: PermissionError(f"{name}-observation-broke") for name in failed_directories}
    for name, error in errors.items():
        error.__cause__ = OSError(f"{name}-original-cause")

    def list_directory(path):
        name = "staging" if Path(path) == Path("/staging/reports") else Path(path).name
        if name in errors:
            raise errors[name]
        return [publication.report_file_name(7, "b" * 64)] if name == "ready" else []

    monkeypatch.setattr(publication.os, "listdir", list_directory)
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        if "ready" in failed_directories:
            with pytest.raises(StateDbFailure) as caught:
                await flow(context)
            record_error(caught.value, stage="outer_session")
        else:
            await flow(context)
        await flush_diagnostics()

    assert len(diagnostics) == len(failed_directories)
    for name in failed_directories:
        matched = [diagnostic for diagnostic in diagnostics if f"{name}-observation-broke" in diagnostic]
        assert len(matched) == 1
        assert f"{name}-original-cause" in matched[0]
        assert '"report_id":7' in matched[0]
        assert "a" * 32 in matched[0]
    supervisor.generate.assert_not_awaited()
    assert save.await_count == (0 if "ready" in failed_directories else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind,moved", [
    (DbOutcomeKind.ROLLED_BACK, True), (DbOutcomeKind.UNKNOWN, False),
])
async def test_failure_store_error_stops_real_report_flow_without_duplicate_diagnostic(monkeypatch, store_kind, moved):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from camctl.bootstrap import flows
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    from camctl.logging_runtime.diagnostics import record_error
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbOutcome
    from camctl.reporting import messages
    from camctl.reporting.messages import ResultSuccessMessage
    from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor
    from camctl.session.service import StateDbFailure

    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = SimpleNamespace(instance_id="a" * 32)
    database.read.side_effect = [(7,), SimpleNamespace(from_wm=0, to_wm=1,
        boundary=HistoryBoundary(1, 1)), ()]
    database.write.side_effect = [DbOutcome(DbOutcomeKind.COMPLETED), DbOutcome(DbOutcomeKind.COMPLETED),
        DbOutcome(store_kind, error=OSError("failure-store-broke"),
                  secondary_errors=(OSError("failure-store-close-broke"),))]
    supervisor = create_autospec(WorkerSupervisor, instance=True)
    supervisor.generate.return_value = WorkerGeneration(GenerationOutcomeKind.SUCCESS, "job-7",
        success=ResultSuccessMessage("job-7", "a" * 32,
            "/staging/reports/" + publication.report_file_name(7, "b" * 64), 7, "b" * 64))
    monkeypatch.setattr(messages, "new_job_id", lambda: "job-7")
    monkeypatch.setattr(flows, "_query_rows", AsyncMock(side_effect=lambda database, sql, parameters=():
        [(1, 1)] if "SELECT status, to_wm FROM reports" in sql else []))
    monkeypatch.setattr(publication.asyncio, "to_thread", _inline)
    monkeypatch.setattr(publication, "_list_stale_ready", lambda *args: ((), None))
    move = AsyncMock(return_value=PublishResult(
        PublishStage.MOVED if moved else PublishStage.NOT_MOVED,
        DirectorySyncStage.FAILED if moved else DirectorySyncStage.NOT_ATTEMPTED,
        moved, "report-file-broke", _error()))
    monkeypatch.setattr(publication, "_move_staged", move)
    failed_callback = AsyncMock()
    flow = flows.report_flow(state_db=Path("/state.db"), staging=Path("/staging"),
        ready=Path("/ready"), processing=Path("/processing"),
        history=SimpleNamespace(entity_batch_size=1, event_batch_size=1),
        database=SimpleNamespace(busy_timeout_ms=1), supervisor=supervisor,
        start_actions=False, on_failure=failed_callback)
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: 1))
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        with pytest.raises(StateDbFailure) as caught:
            await flow(context)
        record_error(caught.value, stage="outer_session")
        await flush_diagnostics()

    assert len(diagnostics) == 2
    assert sum("failure-store-broke" in diagnostic for diagnostic in diagnostics) == 1
    assert sum("报告目录拒绝访问" in diagnostic for diagnostic in diagnostics) == 1
    original = json.dumps(exception_diagnostic(caught.value), ensure_ascii=False)
    assert "failure-store-broke" in original
    assert "failure-store-close-broke" in original
    failed_callback.assert_not_awaited()
    move.assert_awaited_once()
    assert database.write.await_count == 3
