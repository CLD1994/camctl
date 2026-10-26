"""公共诊断、真实队列与文件通道的交接见证。"""

import asyncio
import json
import threading

import pytest

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.diagnostics import (
    DiagnosticStatus, diagnostic_scope, error_diagnostics_context,
    flush_diagnostics, record_error,
)
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


class GatedFileWriter:
    def __init__(self, file_channel):
        self.file_channel = file_channel
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, record):
        if not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("测试没有释放日志消费者")
        return self.file_channel.write_record(record).appended


async def full_channel(tmp_path):
    file_channel = FileChannel(tmp_path / "camctl.log", max_bytes=1 << 20, file_count=1)
    writer = GatedFileWriter(file_channel)
    channel = LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                         level=LogLevel.DEBUG, writer=writer)
    channel.start()
    await channel.alog(LogRecord(LogLevel.WARNING, "hold-consumer"))
    assert await asyncio.to_thread(writer.entered.wait, 5)
    for number in range(3):
        result = channel.slog(LogRecord(LogLevel.WARNING, f"backlog-{number}"))
        assert result.decision.kind is AdmissionKind.ACCEPTED
    runtime = LogRuntime(channel=channel, file_closer=file_channel.close)
    return channel, writer, runtime


async def test_full_queue_waits_after_cleanup_and_cancel_keeps_single_record(tmp_path):
    channel, writer, runtime = await full_channel(tmp_path)
    fallback = []
    completed_body = asyncio.Event()
    stopped = []
    states = []

    async def producer():
        async with diagnostic_scope():
            states.append(record_error(ValueError("device-read-failed"), stage="read",
                                       details={"stderr_hex": b"known\xff".hex()}))
            stopped.append("device-stop-complete")
            completed_body.set()

    with error_diagnostics_context(channel, fallback.append):
        task = asyncio.create_task(producer())
        try:
            await completed_body.wait()
            await asyncio.sleep(0)
            assert stopped == ["device-stop-complete"]
            assert states[0].status is DiagnosticStatus.HELD
            assert not task.done()
            task.cancel("first cancel")
            await asyncio.sleep(0)
            task.cancel("repeat cancel")
            await asyncio.sleep(0)
            assert not task.done()
            writer.release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 5)
            assert raised.value.args == ("first cancel",)
            assert states[0].status is DiagnosticStatus.ADMITTED
            await flush_diagnostics()
            await close_logging(runtime)
        finally:
            writer.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await close_logging(runtime)
    text = (tmp_path / "camctl.log").read_text(encoding="utf-8")
    payloads = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    assert len(payloads) == 1
    assert payloads[0]["error"]["message"] == "device-read-failed"
    assert payloads[0]["details"] == {"stderr_hex": "6b6e6f776eff", "stderr_text": "known\\xff"}
    assert not fallback
    assert not channel.listener_alive


async def test_body_failure_reaches_stop_before_root_flush_waits_for_queue(tmp_path):
    channel, writer, runtime = await full_channel(tmp_path)
    original = RuntimeError("fatal-session-error")
    fallback = []
    stopped = []
    flushing = None
    with error_diagnostics_context(channel, fallback.append):
        try:
            with pytest.raises(RuntimeError) as raised:
                async with diagnostic_scope():
                    state = record_error(original, stage="session")
                    raise original
            assert raised.value is original
            stopped.append("required-stop")
            assert state.status is DiagnosticStatus.HELD
            flushing = asyncio.create_task(flush_diagnostics())
            await asyncio.sleep(0)
            assert not flushing.done()
            assert stopped == ["required-stop"]
            writer.release.set()
            await asyncio.wait_for(flushing, 5)
            await close_logging(runtime)
        finally:
            writer.release.set()
            if flushing is not None and not flushing.done():
                await asyncio.gather(flushing, return_exceptions=True)
            await close_logging(runtime)
    text = (tmp_path / "camctl.log").read_text(encoding="utf-8")
    assert text.count("fatal-session-error") == 1
    assert state.status is DiagnosticStatus.ADMITTED
    assert not fallback


async def test_cancelled_root_flush_hands_off_all_records_before_close(tmp_path):
    channel, writer, runtime = await full_channel(tmp_path)
    fallback = []
    flushing = None
    with error_diagnostics_context(channel, fallback.append):
        try:
            states = [record_error(ValueError(f"root-error-{number}"), stage="cleanup")
                      for number in range(3)]
            flushing = asyncio.create_task(flush_diagnostics())
            await asyncio.sleep(0)
            flushing.cancel("root close cancellation")
            await asyncio.sleep(0)
            assert not flushing.done()
            writer.release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(flushing, 5)
            assert raised.value.args == ("root close cancellation",)
            assert all(state.status is DiagnosticStatus.ADMITTED for state in states)
            await close_logging(runtime)
        finally:
            writer.release.set()
            if flushing is not None and not flushing.done():
                await asyncio.gather(flushing, return_exceptions=True)
            await close_logging(runtime)
    text = (tmp_path / "camctl.log").read_text(encoding="utf-8")
    assert all(text.count(f"root-error-{number}") == 1 for number in range(3))
    assert not fallback


async def test_admitted_record_uses_command_fallback_when_consumer_cannot_write():
    command_fallback = []
    other_fallback = []
    channel = LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                         writer=lambda record: False, fallback=other_fallback.append)
    runtime = LogRuntime(channel)
    channel.start()
    try:
        with error_diagnostics_context(channel, command_fallback.append):
            async with diagnostic_scope():
                state = record_error(ValueError("original-business-error"), stage="read")
            assert state.status is DiagnosticStatus.ADMITTED
            await close_logging(runtime)
    finally:
        await close_logging(runtime)
    assert len(command_fallback) == 1
    assert json.loads(command_fallback[0])["error"]["message"] == "original-business-error"
    assert not other_fallback
