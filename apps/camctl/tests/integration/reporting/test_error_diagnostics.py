"""真实报告文件线程把完整失败事实交回协程诊断，不开始状态保存。"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.host_files.handoff import HandoffDirectories
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.reporting import publication


@pytest.mark.asyncio
async def test_actual_report_file_thread_returns_original_failure_graph(tmp_path, monkeypatch):
    staging, ready = tmp_path / "staging", tmp_path / "ready"
    staging.mkdir()
    ready.mkdir()
    try:
        raise PermissionError("报告源文件不可写")
    except PermissionError as cause:
        try:
            raise OSError("报告文件打开失败") from cause
        except OSError as error:
            failure = error
    original_open = publication.os.open

    def open_file(path, *args, **kwargs):
        if Path(path).parent == staging / publication.REPORTS_DIRECTORY:
            raise failure
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(publication.os, "open", open_file)
    session = create_autospec(publication.PublicationSession, instance=True)
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        result = await publication.deliver_report(7, b"report payload", HandoffDirectories(staging, ready), session)
        await flush_diagnostics()

    assert result.outcome is publication.DeliveryOutcome.WRITE_FAILED
    assert result.exception is failure
    assert len(diagnostics) == 1
    for fact in ("PermissionError", "报告源文件不可写", "报告文件打开失败", "traceback", str(staging), '"report_id":7'):
        assert fact in diagnostics[0]
    assert not list(ready.iterdir())
    session.prepare.assert_not_awaited()


def _oversize_worker_failure(connection):
    from camctl.reporting import worker
    from camctl.reporting.messages import MAX_MESSAGE_BYTES, JobMessage, encode_message
    error = RuntimeError("x" * MAX_MESSAGE_BYTES + "完整诊断尾部")
    error.__cause__ = PermissionError("原始工作进程错误")
    job = JobMessage("oversize-job", 7, 0, 1, 1, "a" * 32,
        "/state.db", "/staging/report.tmp", "/staging", "/ready", "/processing", 1, 1, 1)
    worker._read_job_metadata = lambda job: (_ for _ in ()).throw(error)
    try:
        connection.send_bytes(encode_message(worker.run_job(job)))
    finally:
        connection.close()


def test_spawn_worker_oversize_diagnostic_reaches_inherited_stderr(capfd):
    import json
    import multiprocessing
    from camctl.reporting.messages import MAX_MESSAGE_BYTES, decode_message
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_oversize_worker_failure, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "工作进程未在期限内交回受限结果"
        message = decode_message(parent.recv_bytes())
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        process.close()

    assert message.diagnostic_fallback == "stderr"
    assert message.diagnostic_size_bytes > MAX_MESSAGE_BYTES
    diagnostic = json.loads(capfd.readouterr().err)
    assert diagnostic["job_id"] == "oversize-job"
    assert diagnostic["diagnostic"]["message"] == "x" * MAX_MESSAGE_BYTES + "完整诊断尾部"
    assert diagnostic["diagnostic"]["cause"]["message"] == "原始工作进程错误"
