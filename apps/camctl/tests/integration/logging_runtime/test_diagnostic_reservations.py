"""真实日志拥有者接手副本任务后的原诊断归属，协作者只使用内存。"""

import asyncio
from pathlib import Path

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.diagnostics import (
    DiagnosticStatus, acknowledge_diagnostic, adopt_diagnostic, diagnostic_record_for,
    diagnostic_scope, error_diagnostics_context, flush_diagnostics, record_error,
    reserve_diagnostic,
)
from camctl.logging_runtime.ownership import LoggingOwner
from camctl.logging_runtime.service import AdmissionResult
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio


class MemoryChannel:
    def __init__(self):
        self.records = []

    async def alog(self, record):
        self.records.append(record)
        return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, record.level), 0)


@pytest.mark.parametrize("copy_result", ["written", "suppressed", "failed"])
async def test_parent_cancellation_leaves_original_record_with_real_copy_owner(copy_result):
    channel = MemoryChannel()
    supervisor = Supervisor()
    entered = asyncio.Event()
    release = asyncio.Event()
    copied = []
    states = []

    async def diagnose(text):
        pytest.fail(text)

    # 构造真实责任拥有者，不启动文件服务、线程或访问路径。
    owner = LoggingOwner(logs_dir=Path("/unused-diagnostics"), ownership=object(),
                         supervisor=supervisor, owner_settled=True, diagnose=diagnose)

    async def parent():
        async with diagnostic_scope():
            state = record_error(ValueError("earlier-report-generation"), stage="report_generate")
            states.append(state)
            reservation = reserve_diagnostic(state)
            assert reservation is not None

            async def operation():
                async with diagnostic_scope():
                    assert adopt_diagnostic(reservation) is state
                    entered.set()
                    await release.wait()
                    if copy_result == "written":
                        copied.append(diagnostic_record_for(state))
                        acknowledge_diagnostic(state)
                    elif copy_result == "failed":
                        raise OSError("copy did not append")

            await owner.run_service_operation("report_failure", operation)

    with error_diagnostics_context(channel, lambda text: pytest.fail(text), background=True):
        task = asyncio.create_task(parent())
        try:
            await entered.wait()
            task.cancel("parent canceled while actual copy still running")
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert states[0].status is DiagnosticStatus.HELD
            assert not channel.records
            assert len(supervisor.outstanding()) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            result = await supervisor.drain_required()
            await flush_diagnostics()
    assert not result.failed
    assert states[0].status is DiagnosticStatus.ADMITTED
    if copy_result == "written":
        assert copied == [diagnostic_record_for(states[0])]
        assert not channel.records
    else:
        assert not copied
        assert channel.records == [diagnostic_record_for(states[0])]
