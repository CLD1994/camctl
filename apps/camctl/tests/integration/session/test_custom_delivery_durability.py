"""公开会话保留自定义副本交付的原日志追加与持久事实。"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.contracts.clock import ClockPort
from camctl.logging_runtime import get_logger
from camctl.logging_runtime.clh_adapter import WriteFailureStage, WriteResult
from camctl.logging_runtime.copies import (
    CopyChannel, CopyOutcome, CopyReceipt, CopyRequest, FailureLogService, MarkerStore,
    deliver_failure_copy,
)
from camctl.logging_runtime.diagnostics import (
    diagnostic_record_for, diagnostic_state_for, error_diagnostics_context,
    flush_diagnostics, record_error,
)
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import LogChannel
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.session import CloseDecision
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.session.clock import ClockCheckInput
from camctl.session.locks import AdmissionLease, SessionLease
from camctl.session.service import (
    AcceptanceRepositoryPort, SessionContext, SessionPaths, SessionRepositoryPort, run_session,
)
from camctl.session.work import WorkDecision, WorkDecisionKind, WorkFacts

pytestmark = pytest.mark.asyncio


def session_world():
    """内联交付数据库端口结果；本场景不需要数据库、文件或后台线程。"""
    metadata = DatabaseMetadata("camctl", "a" * 32, 1, "/staging", "/ready", "/processing")
    owned = OwnedConnection(
        create_autospec(sqlite3.Connection, instance=True, spec_set=True), metadata)
    template = BusinessDatabase(
        create_autospec(DbExecutor, instance=True, spec_set=True), metadata)
    database = create_autospec(template, instance=True, spec_set=True)
    database.metadata = metadata
    database.read.side_effect = lambda callback, **_kwargs: callback(owned)
    database.write.side_effect = lambda callback, **_kwargs: callback(owned)
    repository = create_autospec(SessionRepositoryPort, instance=True, spec_set=True)
    session = create_autospec(SessionLease, instance=True, spec_set=True)
    admission = create_autospec(AdmissionLease, instance=True, spec_set=True)
    clock = create_autospec(ClockPort, instance=True, spec_set=True)
    clock.utc_micros.return_value = 1000

    def acquire(command, _key, _owned):
        command.acquire_and_register()
        return DbOutcome(DbOutcomeKind.COMPLETED)

    def close(command, _key, _owned):
        command.release_admission()
        return DbOutcome(DbOutcomeKind.COMPLETED,
            CloseDecision(True, WorkDecision(WorkDecisionKind.EXIT_REPORT_ERROR)))

    repository.acquire_admission.side_effect = acquire
    repository.close_admission.side_effect = close
    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=clock,
        open_database=AsyncMock(return_value=database),
        acceptance_repository=create_autospec(
            AcceptanceRepositoryPort, instance=True, spec_set=True),
        session_repository=repository,
        paths=SessionPaths("/session.lock", "/admission.lock"),
        clock_policy=lambda _: ClockCheckInput(1000, 0, 0, 1),
        acquire_session=lambda: session, acquire_admission=lambda: admission,
        facts_query=lambda _: WorkFacts(0, 0, False, True, False, False, False, False),
    )
    return context, database, session, admission


@pytest.mark.parametrize("result_shape", [
    "receipt", "exception_receipt", "exception_write_result",
])
@pytest.mark.parametrize("durable", [False, True], ids=["sync_failed", "copy_failed"])
async def test_ownerless_custom_delivery_keeps_original_completion(result_shape, durable):
    context, database, session, admission = session_world()
    original = OSError(5, "report file failed", "/reports/report-41.json")
    fault = ("copy failed after durable original: " if durable else "file fsync failed: ")
    fault += "完整错误\n" + "x" * 2048
    write_result = WriteResult(
        appended=True, rotated=False, durable=durable, copy_handles_closed=True,
        sync_error=None if durable else fault,
        failure_stage=None if durable else WriteFailureStage.FILE_SYNC,
    )
    copy_receipt = CopyReceipt(
        CopyOutcome.COPY_FAILED, write_result=write_result,
        copy_error=fault if durable else None,
    )
    marker = create_autospec(MarkerStore, instance=True, spec_set=True)
    copy_channel = create_autospec(CopyChannel, instance=True, spec_set=True)
    log_channel = create_autospec(LogChannel, instance=True, spec_set=True)
    log_channel.submit.side_effect = AssertionError("已经追加的原诊断不能重新投递")
    requests, receipts, events, diagnostics, fallback = [], [], [], [], []
    logger = get_logger(__name__)

    async def report(_context):
        record_error(original, stage="report_generate", context={"report_id": 41})
        raise original

    def request_factory(error):
        assert error is original
        trigger = diagnostic_record_for(diagnostic_state_for(error))
        return CopyRequest(trigger, Path("/staging"), Path("/ready"), copy_channel, marker)

    async def custom_delivery(request):
        requests.append(request)
        receipt = logger.error("报告错误继续由原副本持有", exc_info=original)
        assert isinstance(receipt, LogReceipt)
        assert receipt.write_result is None
        receipts.append(receipt)
        if result_shape == "receipt":
            return copy_receipt
        failure = RuntimeError(fault)
        if result_shape == "exception_receipt":
            failure.copy_receipt = copy_receipt
        else:
            failure.write_result = write_result
        raise failure

    async def stderr_diagnostic(message):
        events.append("stderr")
        diagnostics.append(message)

    async def log_diagnostic(message):
        events.append("log")
        diagnostics.append(message)

    context.flows = {"report": report}
    deliver = create_autospec(deliver_failure_copy, spec_set=True, side_effect=custom_delivery)
    context.failure_log = FailureLogService(marker, deliver=deliver)
    context.copy_request_factory = request_factory
    context.on_log_path_unsafe = lambda: events.append("unsafe")
    context.on_stderr_diagnostic = stderr_diagnostic
    context.on_log_diagnostic = log_diagnostic
    assert context.logging_owner is None

    with error_diagnostics_context(log_channel, fallback.append):
        outcome = await run_session(context, None)
        repeated = logger.error("外层再次观察同一报告错误", exc_info=original)
        assert repeated is receipts[0]
        completed = await repeated
        assert await receipts[0] is completed
        await flush_diagnostics()

    assert outcome.succeeded is False
    assert outcome.reason == "report_error"
    assert len(requests) == len(receipts) == 1
    assert requests[0].trigger is diagnostic_record_for(diagnostic_state_for(original))
    recorded = json.loads(requests[0].trigger.message)
    assert recorded["stage"] == "report_generate"
    assert recorded["context"]["report_id"] == 41
    assert "report file failed" in requests[0].trigger.message
    assert "/reports/report-41.json" in requests[0].trigger.message
    assert completed.appended is True
    assert completed.durable is durable
    assert completed.delivered is durable
    assert completed.fallback_written is False
    assert fallback == []
    deliver.assert_awaited_once()
    log_channel.submit.assert_not_called()
    copy_channel.copy_with_write.assert_not_called()
    assert events == (["log"] if durable else ["unsafe", "stderr"])
    assert len(diagnostics) == 1
    assert "report file failed" in diagnostics[0]
    assert "/reports/report-41.json" in diagnostics[0]
    assert "appended=True" in diagnostics[0]
    assert f"durable={durable}" in diagnostics[0]
    assert fault in diagnostics[0]
    if not durable:
        assert WriteFailureStage.FILE_SYNC.value in diagnostics[0]
    database.close.assert_awaited_once()
    session.close.assert_called_once()
    admission.close.assert_called()
