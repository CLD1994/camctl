"""日志专项的公开协作者装配；默认会话与显式模块组合分别由调用者选择。"""

from __future__ import annotations

import os
from pathlib import Path

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.reporting.messages import ErrorKind, ResultFailureMessage, ResultSuccessMessage
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor
from ..bootstrap.test_composition import RecordingCatalog
from .session import parsed_plan, session_config


async def run_with_report_failure(deps, monkeypatch):
    """默认报告流程收到一次已分类的报告生成失败并持久化原错误。"""
    report_called = {"count": 0}

    async def fail_generation(self, job):
        report_called["count"] += 1
        failure = ResultFailureMessage(
            job_id=job.job_id,
            instance_id=job.instance_id,
            error_kind=ErrorKind.REPORT,
            error_code="controlled_report_failure",
            error_message="controlled report generation failure",
        )
        return WorkerGeneration(
            kind=GenerationOutcomeKind.REPORT_FAILURE,
            job_id=job.job_id,
            failure=failure,
        )

    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    outcome = await execute_command(deps, None)
    return outcome, report_called


async def runtime(tmp_path: Path, *, log=None, **options):
    """通过正式拒绝事件建立待报告事实；不把 submit 成功误称计划受理。"""
    config = session_config(tmp_path, **({} if log is None else {"log": log}))
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    submit = build_runtime(CommandMode.SUBMIT, config, catalog=RecordingCatalog())
    source = await parsed_plan(tmp_path, {
            "version": 1,
            "request_id": "sw02-report-trigger",
            "actions": [],
        })
    try:
        accepted = await execute_command(submit, source)
        assert accepted.succeeded is True and accepted.needs_run is True
    finally:
        close_runtime(submit)
    return build_runtime(CommandMode.RUN, config, catalog=RecordingCatalog(), **options)


async def seed_two_pending_reports(deps, tmp_path: Path):
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.reporting.policy import ReportingRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    owned = open_existing(deps.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        first = ReportingRepository().freeze_report(
            new_operation_key(), owned, occurred_at=1_735_689_600_000_000)
        assert first.kind is DbOutcomeKind.COMPLETED
        first_report = first.value.report
    finally:
        owned.connection.close()

    submit = build_runtime(
        CommandMode.SUBMIT, deps.config, catalog=RecordingCatalog())
    source = await parsed_plan(tmp_path, {
            "version": 1,
            "request_id": "sw02-second-report-trigger",
            "actions": [{
                "name": "future status",
                "type": "report_status",
                "scheduled_at": "2099-01-01 00:00:00",
                "params": {"scope": "full"},
            }],
        })
    try:
        accepted = await execute_command(submit, source)
        assert accepted.succeeded is True and accepted.needs_run is True
    finally:
        close_runtime(submit)

    owned = open_existing(deps.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        second = ReportingRepository().freeze_report(
            new_operation_key(), owned, occurred_at=1_735_689_600_000_001)
        assert second.kind is DbOutcomeKind.COMPLETED
        second_report = second.value.report
        pending = owned.connection.execute(
            "SELECT id, status FROM reports ORDER BY id").fetchall()
        assert [row[0] for row in pending] == [
            first_report.report_id, second_report.report_id]
        assert all(row[1] == 1 for row in pending)
        assert second_report.report_id != first_report.report_id
    finally:
        owned.connection.close()
    return first_report, second_report


async def generate_valid_report(_supervisor, job):
    """真实生成、校验和命名，默认报告消费者继续执行实际发布与保存。"""
    from camctl.reporting.generation import GenerationSpec, generate_report_file
    from camctl.reporting.publication import report_file_name

    generated = generate_report_file(GenerationSpec(
        db_path=Path(job.db_path), report_id=job.report_id,
        from_wm=job.from_wm, to_wm=job.to_wm, frozen_event_id=job.frozen_event_id,
        staging_path=Path(job.staging_path), entity_batch_size=job.entity_batch_size,
        event_batch_size=job.event_batch_size))
    canonical = generated.path.with_name(report_file_name(job.report_id, generated.sha256))
    os.replace(generated.path, canonical)
    return WorkerGeneration(GenerationOutcomeKind.SUCCESS, job.job_id,
        success=ResultSuccessMessage(job_id=job.job_id, instance_id=job.instance_id,
            path=str(canonical), size_bytes=generated.size_bytes, sha256=generated.sha256))


def log_session(deps, flows, *, service=None):
    """正式 SessionContext 组合：真实库、锁、日志线程、副本和必要收场。

    此装配只验证传入流程与日志拥有者的协作，不证明默认业务流程工厂。
    """
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.bootstrap.clocks import SystemClock
    from camctl.logging_runtime.clh_adapter import FileChannel
    from camctl.logging_runtime.copies import CopyRequest, FailureLogService, MarkerStore
    from camctl.logging_runtime.diagnostics import diagnostic_record_for, diagnostic_state_for, record_error
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.logging_runtime.service import LogRecord
    from camctl.session.supervision import Supervisor
    from .session import session_context

    logs_dir = Path(deps.config.paths.staging) / 'logs'
    marker = MarkerStore(logs_dir)
    if service is None:
        service = FailureLogService(marker)
    channel = None

    async def diagnose(message):
        result = await deps.log_runtime.channel.deliver_important(LogRecord(LogLevel.ERROR, message))
        assert result.delivered is True

    owner = LoggingOwner(logs_dir=logs_dir, ownership=service.ownership,
        supervisor=Supervisor(), owner_settled=False,
        ordinary_log_path=Path(deps.config.paths.log_file), diagnose=diagnose)
    service.bind_owner(owner)

    def request(error):
        nonlocal channel
        record_error(error, stage='report')
        if channel is None:
            channel = FileChannel(Path(deps.config.paths.log_file),
                max_bytes=deps.config.log.max_size_bytes, file_count=deps.config.log.file_count)
        return CopyRequest(trigger=diagnostic_record_for(diagnostic_state_for(error)),
            staging_root=Path(deps.config.paths.staging), ready_dir=Path(deps.config.paths.ready),
            channel=channel, marker=marker)

    context = session_context(deps.state_db, clock=SystemClock(), catalog=deps.catalog,
        flows=flows, failure_log=service, copy_request_factory=request,
        on_log_diagnostic=diagnose, on_stderr_diagnostic=diagnose,
        logging_owner=owner, on_session_locked=owner.start_session,
        local_work=CombinedLocalWork((*[flow for flow in flows.values() if hasattr(flow, "settle")], owner)))
    def close():
        if channel is not None:
            channel.close()

    return context, service, close


def report_log_session(deps, source):
    """真实报告消费者与日志服务的显式模块组合，不调用默认装配器。"""
    from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
    from camctl.bootstrap.flows import report_flow
    from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
    from camctl.reporting.maintenance import MaintenanceLimits
    from camctl.reporting.worker import report_lock_path
    from camctl.session.service import run_session

    flows = {"controlled_source": source}
    context, service, close = log_session(deps, flows)
    supervisor = WorkerSupervisor(limits=MaintenanceLimits.defaults(),
        lock_path=str(report_lock_path(deps.state_db)))

    async def diagnose(receipt, failure):
        # 此消费者直接交付公开回执，不重建默认入口的格式化算法。
        await context.on_log_diagnostic(repr(receipt) if failure is None else str(failure))

    async def failed(current, error):
        await context.logging_owner.run_service_operation("report_failure",
            lambda: service.on_report_failure_with_factory(
                lambda: current.copy_request_factory(error), diagnose=diagnose))

    async def recovered():
        await context.logging_owner.run_service_operation("report_recovered",
            lambda: service.on_report_recovered(diagnose=diagnose))

    flows["report"] = BackgroundFlow(report_flow(state_db=deps.state_db,
        staging=Path(deps.config.paths.staging), ready=Path(deps.config.paths.ready),
        processing=Path(deps.config.paths.processing), history=deps.config.history,
        database=deps.config.database, supervisor=supervisor,
        on_failure=failed, on_recovered=recovered))
    context.local_work = CombinedLocalWork((*flows.values(), context.logging_owner))

    async def run():
        try:
            with error_diagnostics_context(deps.log_runtime.channel, lambda text: False, background=True):
                try:
                    return await run_session(context, None)
                finally:
                    await flush_diagnostics()
        finally:
            await supervisor.stop()
            close()

    return run(), service


def observe_failure_service(monkeypatch, *, receipts=None, factory_started=None):
    """只观察服务公开调用，实际请求、锁、交付和诊断仍由生产消费者执行。"""
    from camctl.logging_runtime.copies import FailureLogService
    original = FailureLogService.on_report_failure_with_factory

    async def observe(service, factory, *, diagnose=None):
        def request():
            if factory_started is not None:
                factory_started()
            return factory()
        receipt = await original(service, request, diagnose=diagnose)
        if receipts is not None:
            receipts.append(receipt)
        return receipt

    monkeypatch.setattr(FailureLogService, "on_report_failure_with_factory", observe)
