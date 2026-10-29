"""默认报告流程在父取消后保留原错误，直到实际副本子任务确认交付。"""

import asyncio
import json
from pathlib import Path

import pytest

from camctl.bootstrap.lifecycle import close_runtime, execute_command
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.copies import FailureLogService, MarkerStore, SaveOutcome
from camctl.reporting.messages import ErrorKind, ResultFailureMessage
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor
from ..support.bootstrap_log_cases import runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("written", [False, True])
async def test_report_entry_reserves_prepared_error_before_parent_handoff(
        tmp_path, monkeypatch, written):
    deps = await runtime(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = OSError(5, "先前报告生成或发布错误")
    cause = ValueError("原始报告原因")
    cause.add_note("原始报告备注")
    original.__cause__ = cause
    original.add_note("报告调用备注")
    marker = MarkerStore(Path(deps.config.paths.staging) / "logs")
    if not written:
        assert marker.create("earlier-round", "log-copy-earlier-round.log").outcome is SaveOutcome.SAVED

    ordinary, copied = [], []
    real_write, real_copy = FileChannel.write_record, FileChannel.copy_with_write

    def write(channel, record):
        if "先前报告生成或发布错误" in record.message:
            ordinary.append(record.message)
        return real_write(channel, record)

    def copy(channel, record, destination):
        copied.append(record.message)
        return real_copy(channel, record, destination)

    monkeypatch.setattr(FileChannel, "write_record", write)
    monkeypatch.setattr(FileChannel, "copy_with_write", copy)
    real_failure = FailureLogService.on_report_failure_with_factory

    async def wait_before_receipt(service, factory, *, diagnose=None):
        async def diagnose_after_handoff(receipt, error):
            entered.set()
            await release.wait()
            if diagnose is not None:
                await diagnose(receipt, error)
        return await real_failure(service, factory, diagnose=diagnose_after_handoff)

    monkeypatch.setattr(FailureLogService, "on_report_failure_with_factory", wait_before_receipt)

    async def fail_generation(_supervisor, job):
        return WorkerGeneration(GenerationOutcomeKind.REPORT_FAILURE, job.job_id,
            failure=ResultFailureMessage(job_id=job.job_id, instance_id=job.instance_id,
                error_kind=ErrorKind.REPORT, error_code="ownership_report_failure",
                error_message="先前报告生成或发布错误"), exception=original)

    monkeypatch.setattr(WorkerSupervisor, "generate", fail_generation)
    task = asyncio.create_task(execute_command(deps, None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel("父等待取消")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        assert ordinary == []
        assert len(copied) == int(written)
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, timeout=10)
        assert cancelled.value.args == ("父等待取消",)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        close_runtime(deps)

    assert len(ordinary) == int(not written)
    log = Path(deps.config.paths.log_file).read_text(encoding="utf-8")
    records = [json.loads(line) for line in log.splitlines() if line.startswith("{")]
    original_records = [item for item in records if item.get("stage") == "report_generate"]
    assert len(original_records) == 1
    assert original_records[0]["error"]["cause"]["message"] == str(original)
    assert original_records[0]["error"]["cause"]["cause"]["message"] == str(cause)
    assert "原始报告原因" in log and "原始报告备注" in log and "报告调用备注" in log
    ready = list(Path(deps.config.paths.ready).glob("log-copy-*.log"))
    assert len(ready) == int(written)
    if written:
        assert "先前报告生成或发布错误" in ready[0].read_text(encoding="utf-8")
