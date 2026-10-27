"""通用 Logger 与真实诊断拥有者、日志线程和文件通道的组合契约。"""

import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from camctl.logging_runtime import get_logger, get_sync_logger
from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.diagnostics import (
    DiagnosticStatus, acknowledge_diagnostic, adopt_diagnostic,
    diagnostic_record_for, diagnostic_scope, diagnostic_state_for,
    error_diagnostics_context, flush_diagnostics, record_error,
    release_diagnostic, reserve_diagnostic,
)
from camctl.logging_runtime.events import event_logging, log_context, record_event
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogFileStage, LogLevel
from camctl.logging_runtime.one_shot import stderr_logging
from camctl.logging_runtime.ownership import LoggingOwner
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord
from camctl.session.supervision import Supervisor


LOGGER = get_logger("integration.shared")
SYNC_LOGGER = get_sync_logger("integration.shared")


@asynccontextmanager
async def file_logging(path, *, level=LogLevel.DEBUG, full=False):
    """仅在写入边界设置门闩；队列、消费线程和文件追加均使用真实实现。"""
    file_channel = FileChannel(path, max_bytes=1 << 20, file_count=1)
    entered, release = threading.Event(), threading.Event()

    def write(record):
        if full and not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise TimeoutError("测试没有释放文件消费者")
        return file_channel.write_record(record).completion

    channel = LogChannel(capacity=3 if full else 64,
                         low_watermark=1 if full else 48,
                         high_watermark=2 if full else 56,
                         level=level, writer=write)
    runtime = LogRuntime(channel, level=level, file_closer=file_channel.close)
    channel.start()
    try:
        if full:
            await channel.alog(LogRecord(LogLevel.WARNING, "consumer-gate"))
            assert await asyncio.to_thread(entered.wait, 5)
            for number in range(3):
                result = channel.slog(LogRecord(LogLevel.WARNING, f"backlog-{number}"))
                assert result.decision.kind is AdmissionKind.ACCEPTED
        yield channel, release
    finally:
        release.set()
        await close_logging(runtime)
        assert not channel.listener_alive


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def completed(task):
    # 有界观察终态，超时不隐式撤回实际交接。
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done, "日志交接没有在释放条件后完成"
    return task.result()


async def await_receipt(receipt):
    """Task 仅属于测试等待者；生产回执本身不是协程对象。"""
    return await receipt


async def finish_tasks(*tasks):
    done, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel("test cleanup")
    if pending:
        later, pending = await asyncio.wait(pending, timeout=1)
        done.update(later)
    await asyncio.gather(*done, return_exceptions=True)
    assert not pending, f"测试拥有的任务未结束：{pending!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [LogLevel.INFO, LogLevel.DEBUG])
async def test_logger_json_context_and_filtering_share_the_file_channel(tmp_path, level):
    path = tmp_path / "camctl.log"
    fallback = []
    began = datetime.now(timezone.utc)
    async with file_logging(path, level=level) as (channel, _):
        with event_logging(channel), error_diagnostics_context(channel, fallback.append):
            with log_context(stage="device_prepare", action_id=147, device_id="camera-a"):
                record_event("device_prepare", event="started")
                await LOGGER.info('消息 %s\n"quoted"', "完整", extra={"action_id": 148, "attempt": 2})
                await LOGGER.debug("debug-detail")
                SYNC_LOGGER.info("sync-info")
                await LOGGER.log(30, "warning-via-log")
                await LOGGER.error("same-text-error")
                await LOGGER.error("same-text-error")
                await LOGGER.critical("critical-diagnostic")
            await flush_diagnostics()
        assert channel.counters.total_dropped() == 0
    ended = datetime.now(timezone.utc)
    records = read_records(path)
    assert len(records) == (8 if level is LogLevel.DEBUG else 7)
    for record in records:
        assert record["pid"] == os.getpid()
        timestamp = datetime.fromisoformat(record["logged_at_utc"])
        assert timestamp.utcoffset().total_seconds() == 0
        assert began <= timestamp <= ended
        assert record["stage"] == "device_prepare"
        assert record["device_id"] == "camera-a"
    messages = [record for record in records if "logger" in record]
    assert all(record["logger"] == "integration.shared" for record in messages)
    assert all(Path(record["pathname"]).resolve() == Path(__file__).resolve()
               and record["function"] == "test_logger_json_context_and_filtering_share_the_file_channel"
               and record["lineno"] > 0 for record in messages)
    info = next(record for record in messages if record["message"].startswith("消息"))
    assert info["message"] == '消息 完整\n"quoted"'
    assert info["action_id"] == 148 and info["attempt"] == 2
    assert [record["message"] for record in messages].count("same-text-error") == 2
    assert next(record for record in messages if record["message"] == "critical-diagnostic")["level"] == "CRITICAL"
    assert any(record["message"] == "debug-detail" for record in messages) is (level is LogLevel.DEBUG)
    assert not fallback


@pytest.mark.asyncio
async def test_shared_logger_isolates_tasks_and_real_file_worker_context(tmp_path):
    path = tmp_path / "camctl.log"
    ready = asyncio.Queue()
    proceed = asyncio.Event()
    fallback = []
    main_thread = threading.get_ident()

    async def diagnose(text):
        pytest.fail(text)

    owner = LoggingOwner(logs_dir=tmp_path, ownership=object(), supervisor=Supervisor(),
                         owner_settled=True, diagnose=diagnose)

    def in_file_worker(action_id):
        assert threading.get_ident() != main_thread
        SYNC_LOGGER.info("file-worker-%s", action_id)
        return action_id

    async def command(action_id):
        with log_context(stage="file_copy", action_id=action_id):
            await ready.put(action_id)
            await proceed.wait()
            await LOGGER.info("task-%s", action_id)
            assert await owner.owned_call(LogFileStage.COPY_WRITE,
                                          lambda: in_file_worker(action_id)) == action_id

    async with file_logging(path) as (channel, _):
        with event_logging(channel), error_diagnostics_context(channel, fallback.append):
            tasks = [asyncio.create_task(command(identity)) for identity in (101, 202)]
            try:
                assert {await asyncio.wait_for(ready.get(), 5),
                        await asyncio.wait_for(ready.get(), 5)} == {101, 202}
                proceed.set()
                await completed(asyncio.gather(*tasks))
            finally:
                proceed.set()
                await finish_tasks(*tasks)
            await LOGGER.info("outside-task")
            await flush_diagnostics()
        await owner.settle()
    records = read_records(path)
    assert len(records) == 5
    for identity in (101, 202):
        matching = [record for record in records if record.get("action_id") == identity]
        assert {record["message"] for record in matching} == {
            f"task-{identity}", f"file-worker-{identity}"}
        assert all(record["stage"] == "file_copy" for record in matching)
    outside = next(record for record in records if record["message"] == "outside-task")
    assert outside["stage"] == "runtime" and "action_id" not in outside
    assert not fallback


@pytest.mark.asyncio
async def test_full_queue_repeated_cancel_preserves_error_and_independent_stop(tmp_path):
    path = tmp_path / "camctl.log"
    original = RuntimeError("original-device-error")
    original.__cause__ = ValueError("original-cause")
    original.add_note("original-note")
    facts = asyncio.Queue()
    stopped = []
    fallback = []
    receipts = []

    async def producer():
        try:
            raise original
        except RuntimeError:
            facts.put_nowait(original)
            receipt = LOGGER.exception("device-diagnostic", extra={"action_id": 17})
            receipts.append(receipt)
            await receipt

    async def necessary_stop():
        error = await facts.get()
        stopped.append(error)

    async with file_logging(path, full=True) as (channel, release):
        with event_logging(channel), error_diagnostics_context(channel, fallback.append):
            task = asyncio.create_task(producer())
            stop = asyncio.create_task(necessary_stop())
            try:
                await completed(stop)
                assert stopped == [original] and not task.done()
                await completed(asyncio.create_task(await_receipt(LOGGER.info("full-async-info"))))
                SYNC_LOGGER.warning("full-sync-warning")
                assert channel.counters.info_dropped == 1
                assert channel.counters.warning_dropped == 1
                assert channel.counters.error_dropped == 0
                task.cancel("first cancel")
                task.cancel("repeated cancel")
                with pytest.raises(asyncio.CancelledError):
                    await completed(task)
                assert stopped == [original]
                release.set()
                result = await completed(asyncio.create_task(await_receipt(receipts[0])))
                assert result.delivered is True
                state = diagnostic_state_for(original)
                assert state.status is DiagnosticStatus.ADMITTED
                assert record_error(original, stage="outer-consumer") is state
                await flush_diagnostics()
            finally:
                release.set()
                await finish_tasks(task, stop)
    diagnostics = [record for record in read_records(path) if record.get("logger") == "integration.shared"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["message"] == "device-diagnostic"
    assert diagnostics[0]["exception"]["message"] == "original-device-error"
    assert diagnostics[0]["exception"]["traceback"]
    assert diagnostics[0]["exception"]["cause"]["message"] == "original-cause"
    assert diagnostics[0]["exception"]["notes"] == ["original-note"]
    assert not fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("disable", [False, True])
async def test_logger_waits_for_original_diagnostic_already_being_admitted(tmp_path, disable):
    path = tmp_path / "camctl.log"
    original = ValueError("original-first-record")
    fallback = []
    async with file_logging(path, full=True) as (channel, release):
        with event_logging(channel), error_diagnostics_context(channel, fallback.append, background=True):
            state = record_error(original, stage="original-consumer", details={"attempt": 3})
            await asyncio.sleep(0)
            waiting = asyncio.create_task(await_receipt(LOGGER.error("outer-message", exc_info=original)))
            try:
                await asyncio.sleep(0)
                assert not waiting.done()
                if disable:
                    channel.disable_channel(ChannelError("controlled channel failure"))
                release.set()
                await completed(waiting)
                assert state.status is (DiagnosticStatus.FALLBACK if disable else DiagnosticStatus.ADMITTED)
                assert record_error(original, stage="outer-consumer") is state
                await flush_diagnostics()
            finally:
                release.set()
                await flush_diagnostics()
                await finish_tasks(waiting)
    records = read_records(path) + [json.loads(message) for message in fallback]
    diagnostics = [record for record in records if "error" in record]
    assert len(diagnostics) == 1
    assert diagnostics[0]["stage"] == "original-consumer"
    assert diagnostics[0]["error"]["message"] == "original-first-record"
    assert diagnostics[0]["details"] == {"attempt": 3}
    assert "outer-message" not in path.read_text(encoding="utf-8")
    assert len(fallback) == int(disable)


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_written", [False, True])
async def test_logger_respects_original_copy_reservation(tmp_path, copy_written):
    path = tmp_path / "camctl.log"
    original = OSError("original-copy-error")
    fallback = []
    async with file_logging(path) as (channel, _):
        with event_logging(channel), error_diagnostics_context(channel, fallback.append):
            state = record_error(original, stage="report-generation")
            reservation = reserve_diagnostic(state)
            assert reservation is not None
            waiting = asyncio.create_task(await_receipt(LOGGER.exception("outer-message", exc_info=original)))
            try:
                await asyncio.sleep(0)
                assert not waiting.done()
                if copy_written:
                    async with diagnostic_scope():
                        assert adopt_diagnostic(reservation) is state
                        result = await channel.deliver_important(diagnostic_record_for(state))
                        assert result.delivered is True
                        acknowledge_diagnostic(state)
                else:
                    release_diagnostic(reservation)
                    await flush_diagnostics()
                await completed(waiting)
                assert state.status is DiagnosticStatus.ADMITTED
                await flush_diagnostics()
            finally:
                release_diagnostic(reservation)
                await flush_diagnostics()
                await finish_tasks(waiting)
    records = read_records(path)
    assert len(records) == 1
    assert records[0]["stage"] == "report-generation"
    assert records[0]["error"]["message"] == "original-copy-error"
    assert "outer-message" not in path.read_text(encoding="utf-8")
    assert not fallback


def test_one_shot_logger_writes_complete_json_only_to_stderr():
    err, out = StringIO(), StringIO()
    failures = []
    config = SimpleNamespace(level="INFO", queue_capacity=64, queue_low_watermark=48,
                             queue_high_watermark=56, info_sample_probability=1)

    async def command():
        await LOGGER.info("one-shot-info")
        result = await LOGGER.error("one-shot-error")
        assert result.delivered is False
        assert result.appended is False and result.durable is False
        assert result.fallback_written is True
        SYNC_LOGGER.warning("one-shot-sync")
        return {"command_result": "kept"}

    with redirect_stdout(out), stderr_logging(config, err, command_name="describe",
            on_error=lambda stage, error: failures.append((stage, error))):
        assert asyncio.run(command()) == {"command_result": "kept"}
    records = [json.loads(line) for line in err.getvalue().splitlines()]
    assert [record["message"] for record in records] == [
        "one-shot-info", "one-shot-error", "one-shot-sync"]
    assert all(record["command_name"] == "describe" and record["stage"] == "runtime"
               and record["logger"] == "integration.shared" for record in records)
    assert out.getvalue() == "" and not failures
