"""报告消费者的局部诊断分区：公开存储端口提供结果，实际文件边界注入故障。

本文件不替代默认报告会话与真实数据库的业务协作见证。
"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.host_files.handoff import HandoffDirectories
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.reporting import publication
from camctl.persistence.models import DbOutcomeKind


def _error():
    try:
        raise PermissionError("报告目录拒绝访问")
    except PermissionError as cause:
        try:
            raise OSError("报告文件写入失败") from cause
        except OSError as error:
            error.add_note("已知报告生成身份=job-7")
            return error


def _staged_file_failure(tmp_path, monkeypatch, moved):
    """创建真实文件，只在系统移动或目录同步边界交付原异常。"""
    import hashlib
    import os

    staging, ready = tmp_path / "staging", tmp_path / "ready"
    (staging / "reports").mkdir(parents=True)
    ready.mkdir()
    payload = b"payload"
    digest = hashlib.sha256(payload).hexdigest()
    staged = publication.StagedReport(name=publication.report_file_name(7, digest), size_bytes=len(payload), sha256=digest)
    source = staging / "reports" / staged.name
    source.write_bytes(payload)
    failure = _error()
    calls = []
    if moved:
        actual = os.fsync

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 1:
                raise failure
            return actual(fd)

        monkeypatch.setattr(os, "fsync", fsync)
    else:
        primitive = "rename" if os.name == "nt" else "link"
        actual = getattr(os, primitive)

        def move(source_path, target_path, *args, **kwargs):
            if Path(source_path) == source:
                calls.append((source_path, target_path))
                raise failure
            return actual(source_path, target_path, *args, **kwargs)

        monkeypatch.setattr(os, primitive, move)
    return HandoffDirectories(staging, ready), staged, calls, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", [
    DbOutcomeKind.NOT_EXECUTED,
    DbOutcomeKind.ROLLED_BACK,
    DbOutcomeKind.UNKNOWN,
])
@pytest.mark.parametrize("moved", [False, True])
async def test_report_file_failure_requires_reliable_failure_store(tmp_path, monkeypatch, store_kind, moved):
    import json
    from unittest.mock import AsyncMock
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    store_error = OSError("failure-store-broke")
    secondary = OSError("failure-store-close-broke")
    session = create_autospec(publication.PublicationSession, instance=True)
    session.prepare.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    session.record_intent.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    session.record_failure.return_value = DbOutcome(store_kind, error=store_error, secondary_errors=(secondary,))
    directories, staged, failures, payload = _staged_file_failure(tmp_path, monkeypatch, moved)
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        result = await publication.deliver_staged_report(7, staged, directories, session,
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
    assert len(failures) == 1
    assert (directories.ready / staged.name).exists() is moved
    assert (directories.staging / "reports" / staged.name).exists() is (not moved)
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
    database.read.side_effect = [(), (), (7,), ((2, 1),), ()]
    context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: 1))
    supervisor = create_autospec(WorkerSupervisor, instance=True)
    flow = flows.report_flow(state_db=Path("/state.db"), staging=Path("/staging"),
        ready=Path("/ready"), processing=Path("/processing"), history=None,
        database=None, supervisor=supervisor, start_actions=False)

    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
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
    assert database.write.await_count == (0 if "ready" in failed_directories else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind,moved", [
    (DbOutcomeKind.ROLLED_BACK, True), (DbOutcomeKind.UNKNOWN, False),
])
async def test_failure_store_result_stops_flow_without_duplicate_diagnostic(tmp_path, monkeypatch, store_kind, moved):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from camctl.bootstrap import flows
    from camctl.contracts.history_values import HistoryBoundary
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
    database.read.side_effect = [(), (), (7,), ((1, 1),),
        SimpleNamespace(from_wm=0, to_wm=1, boundary=HistoryBoundary(1, 1)), ()]
    database.write.side_effect = [DbOutcome(DbOutcomeKind.COMPLETED), DbOutcome(DbOutcomeKind.COMPLETED),
        DbOutcome(store_kind, error=OSError("failure-store-broke"),
                  secondary_errors=(OSError("failure-store-close-broke"),))]
    directories, staged, failures, payload = _staged_file_failure(tmp_path, monkeypatch, moved)
    supervisor = create_autospec(WorkerSupervisor, instance=True)
    supervisor.generate.return_value = WorkerGeneration(GenerationOutcomeKind.SUCCESS, "job-7",
        success=ResultSuccessMessage("job-7", "a" * 32,
            str(directories.staging / "reports" / staged.name), staged.size_bytes, staged.sha256))
    monkeypatch.setattr(messages, "new_job_id", lambda: "job-7")
    failed_callback = AsyncMock()
    flow = flows.report_flow(state_db=tmp_path / "state.db", staging=directories.staging,
        ready=directories.ready, processing=tmp_path / "processing",
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
    assert len(failures) == 1
    assert (directories.ready / staged.name).exists() is moved
    assert (directories.staging / "reports" / staged.name).exists() is (not moved)
    assert database.write.await_count == 3
