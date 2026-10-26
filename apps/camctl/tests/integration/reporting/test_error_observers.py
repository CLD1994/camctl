"""报告处理后继续的失败仍交付完整诊断，文件边界由替身隔离。"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.host_files.handoff import HandoffDirectories
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.reporting import publication
from camctl.reporting.messages import MessageProtocolError
from camctl.reporting.maintenance import MaintenanceLimits, ReportMaintenanceContext, ReportPorts, maintain_reports


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
