"""生产报告入口在父取消后把原诊断留给实际副本子任务。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.clh_adapter import WriteResult
from camctl.logging_runtime.copies import CopyOutcome, CopyReceipt
from camctl.logging_runtime.diagnostics import (
    diagnostic_record_for, diagnostic_scope, diagnostic_state_for,
    error_diagnostics_context, flush_diagnostics, record_error,
)
from camctl.logging_runtime.ownership import LoggingOwner
from camctl.logging_runtime.service import AdmissionResult
from camctl.session.service import _trigger_failure_log
from camctl.session.supervision import Supervisor


class Channel:
    def __init__(self):
        self.records = []

    async def alog(self, record):
        self.records.append(record)
        return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [False, True])
async def test_report_entry_reserves_prepared_error_before_parent_handoff(written):
    channel = Channel()
    supervisor = Supervisor()
    entered, release = asyncio.Event(), asyncio.Event()
    copied = []
    original = OSError(5, "先前报告生成或发布错误")

    async def diagnose(_message):
        return None

    owner = LoggingOwner(logs_dir=Path("/unused"), ownership=object(),
                         supervisor=supervisor, owner_settled=True, diagnose=diagnose)

    class Service:
        async def on_report_failure_with_factory(self, factory, *, diagnose):
            request = factory()
            entered.set()
            await release.wait()
            if written:
                copied.append(request.trigger)
                receipt = CopyReceipt(CopyOutcome.PUBLISHED,
                    write_result=WriteResult(True, False, copy_handles_closed=True))
            else:
                receipt = CopyReceipt(CopyOutcome.SUPPRESSED)
            await diagnose(receipt, None)
            return receipt

    def factory(error):
        return SimpleNamespace(trigger=diagnostic_record_for(diagnostic_state_for(error)))

    context = SimpleNamespace(failure_log=Service(), copy_request_factory=factory,
                              logging_owner=owner)

    async def parent():
        async with diagnostic_scope():
            record_error(original, stage="report_generate")
            await _trigger_failure_log(context, original)

    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        task = asyncio.create_task(parent())
        try:
            await entered.wait()
            task.cancel("父等待取消")
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert not channel.records
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            settled = await supervisor.drain_required()
            await flush_diagnostics()
    assert not settled.failed
    original_record = diagnostic_record_for(diagnostic_state_for(original))
    assert (copied, channel.records) == (([original_record], []) if written else ([], [original_record]))
