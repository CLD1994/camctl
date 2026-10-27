"""通用 Logger、原诊断与真实文件写入共用一次完成事实。"""

import asyncio
from contextlib import asynccontextmanager
import gc
import inspect
import json
import threading
from types import SimpleNamespace

import pytest

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.copies import (
    CopyOutcome, CopyRequest, MarkerStore, deliver_failure_copy,
)
from camctl.logging_runtime.diagnostics import (
    DiagnosticStatus, admit_diagnostic, adopt_diagnostic, diagnostic_record_for,
    diagnostic_scope, error_diagnostics_context, flush_diagnostics, record_error,
    release_diagnostic, reserve_diagnostic,
)
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord

from .test_logger import LOGGER, SYNC_LOGGER, await_receipt, completed, finish_tasks, read_records
from .sync_support import FileSyncProbe


pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def logging_file(path, *, gate=None, level=LogLevel.DEBUG,
                       capacity=64, low=48, high=56, probability=1, debug=False,
                       max_bytes=1 << 20, file_count=1):
    """以真实文件适配器的共同完成值交付追加及持久事实。"""
    file = FileChannel(path, max_bytes=max_bytes, file_count=file_count)
    entered, release = threading.Event(), threading.Event()
    fallback = []

    def writer(record):
        if gate == "before" and not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试未释放追加前门闩")
        result = file.write_record(record)
        if gate == "after_write" and not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试未释放实际写入完成后门闩")
        return result.completion

    channel = LogChannel(capacity=capacity, low_watermark=low, high_watermark=high,
                         level=level, sample_probability=probability,
                         random_source=SimpleNamespace(random=lambda: 0.5),
                         writer=writer, fallback=fallback.append)
    runtime = LogRuntime(channel, level=level, file_closer=file.close)
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    if debug:
        loop.set_debug(True)
    channel.start()
    try:
        with event_logging(channel), error_diagnostics_context(channel, fallback.append):
            yield SimpleNamespace(channel=channel, file=file, entered=entered,
                                  release=release, fallback=fallback, runtime=runtime)
    finally:
        release.set()
        try:
            await completed(asyncio.create_task(close_logging(runtime)))
            assert not channel.listener_alive
        finally:
            if debug:
                loop.set_debug(previous_debug)


async def entered_writer(world):
    assert await asyncio.to_thread(world.entered.wait, 5), "日志线程没有到达写入门闩"


async def receipt_result(receipt):
    task = asyncio.create_task(await_receipt(receipt))
    try:
        return await completed(task)
    finally:
        await finish_tasks(task)


@pytest.mark.parametrize("method", ["error", "critical"])
async def test_important_receipt_waits_for_real_file_and_directory_sync(
        tmp_path, monkeypatch, method):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path, block="file")
    business = []
    async with logging_file(path) as world:
        receipt = getattr(LOGGER, method)("durable-record")
        business.append("necessary-work-completed")
        waiter = asyncio.create_task(await_receipt(receipt))
        try:
            assert await asyncio.to_thread(probe.entered.wait, 5)
            assert business == ["necessary-work-completed"]
            assert not waiter.done()
            assert [record["message"] for record in read_records(path)] == ["durable-record"]
            assert ("file", "completed") not in probe.events
            probe.release.set()
            result = await completed(waiter)
            assert result.delivered is True and result.appended is True
            assert result.durable is True and not result.fallback_written
            assert ("file", "completed") in probe.events
            assert ("directory", "completed") in probe.events
            assert read_records(path)[0]["level"] == method.upper()
        finally:
            probe.release.set()
            await finish_tasks(waiter)
    assert not world.fallback


async def test_info_flush_does_not_claim_or_require_file_durability(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path)
    async with logging_file(path):
        result = await receipt_result(LOGGER.info("ordinary-info"))
        assert result.delivered is True and result.appended is True
        assert result.durable is False and not result.fallback_written
        assert [record["message"] for record in read_records(path)] == ["ordinary-info"]
        assert not any(kind == "file" for kind, _ in probe.events)


@pytest.mark.parametrize("preparation", ["created", "rotated"])
async def test_error_syncs_actual_directory_after_info_prepared_path(
        tmp_path, monkeypatch, preparation):
    path = tmp_path / "camctl.log"
    async with logging_file(path, max_bytes=512 if preparation == "rotated" else 1 << 20,
                            file_count=3) as world:
        await receipt_result(LOGGER.info("preparation" + "x" * 600))
        if preparation == "rotated":
            await receipt_result(LOGGER.info("after-info-rotation"))
            assert path.with_name("camctl.log.1").exists()
        probe = FileSyncProbe(monkeypatch, path, block="directory")
        waiter = asyncio.create_task(await_receipt(LOGGER.error("after-info-error")))
        try:
            assert await asyncio.to_thread(probe.entered.wait, 5)
            assert not waiter.done()
            probe.release.set()
            result = await completed(waiter)
            assert result.delivered is True and result.appended is True and result.durable is True
            assert ("file", "completed") in probe.events
            assert ("directory", "completed") in probe.events
        finally:
            probe.release.set()
            await finish_tasks(waiter)
    assert not world.fallback


@pytest.mark.parametrize("sync_kind", ["file", "directory"])
async def test_sync_failure_keeps_append_fact_and_never_rewrites_original(
        tmp_path, monkeypatch, sync_kind):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path, fail=sync_kind)
    original = ValueError("original-sync-error")
    async with logging_file(path) as world:
        receipt = LOGGER.error("sync-failed-record", exc_info=original)
        result = await receipt_result(receipt)
        assert result.delivered is False and result.appended is True
        assert result.durable is False and result.fallback_written is True
        assert await receipt_result(LOGGER.error("outer-message", exc_info=original)) == result
        await flush_diagnostics()
    assert [record["message"] for record in read_records(path)] == ["sync-failed-record"]
    assert probe.events.count((sync_kind, "started")) == 1
    assert (sync_kind, "completed") not in probe.events
    assert len(world.fallback) == 1 and "original-sync-error" in world.fallback[0]


@pytest.mark.parametrize("sync_fails", [False, True])
async def test_disable_during_file_sync_keeps_actual_completion(tmp_path, monkeypatch, sync_fails):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path, block="file", fail="file" if sync_fails else None)
    async with logging_file(path) as world:
        waiter = asyncio.create_task(await_receipt(LOGGER.error("inflight-sync")))
        try:
            assert await asyncio.to_thread(probe.entered.wait, 5)
            world.channel.disable_channel(ChannelError("disabled during original sync"))
            await asyncio.sleep(0)
            assert not waiter.done()
            probe.release.set()
            result = await completed(waiter)
            assert result.appended is True
            assert result.delivered is (not sync_fails) and result.durable is (not sync_fails)
            assert result.fallback_written is sync_fails
        finally:
            probe.release.set()
            await finish_tasks(waiter)
    assert [record["message"] for record in read_records(path)] == ["inflight-sync"]
    assert len(world.fallback) == int(sync_fails)


async def test_normal_shutdown_waits_for_unawaited_important_file_sync(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path, block="file")
    async with logging_file(path) as world:
        receipt = LOGGER.error("shutdown-sync")
        assert await asyncio.to_thread(probe.entered.wait, 5)
        closing = asyncio.create_task(close_logging(world.runtime))
        try:
            await asyncio.sleep(0)
            assert not closing.done() and receipt.write_result is None
            probe.release.set()
            assert (await completed(closing)).listener_stopped
            result = await receipt_result(receipt)
            assert result.delivered is True and result.appended is True and result.durable is True
        finally:
            probe.release.set()
            await finish_tasks(closing)
    assert [record["message"] for record in read_records(path)] == ["shutdown-sync"]
    assert not world.fallback


async def test_shutdown_summary_uses_typed_file_completion_when_file_is_closed(tmp_path):
    path = tmp_path / "camctl.log"
    async with logging_file(path, gate="before", capacity=3, low=1, high=2) as world:
        await world.channel.alog(LogRecord(LogLevel.WARNING, "consumer-gate"))
        await entered_writer(world)
        for number in range(3):
            assert world.channel.slog(LogRecord(LogLevel.WARNING, f"backlog-{number}")).decision.kind is AdmissionKind.ACCEPTED
        assert (await receipt_result(LOGGER.info("dropped-info"))).decision.kind is AdmissionKind.DROPPED
        world.release.set()
        await completed(asyncio.create_task(world.channel.join()))
        world.file.close()
        result = await completed(asyncio.create_task(close_logging(world.runtime)))
        assert result.dropped_total == 1
        assert result.summary_written is False and result.listener_stopped is True
    assert [record["message"] for record in read_records(path)] == [
        "consumer-gate", "backlog-0", "backlog-1", "backlog-2"]
    assert not world.fallback


@pytest.mark.parametrize("rejection", ["disabled", "finished"])
async def test_important_delivery_rejection_retains_one_stderr_fact(tmp_path, rejection):
    path = tmp_path / "camctl.log"
    async with logging_file(path) as world:
        if rejection == "disabled":
            world.channel.disable_channel(ChannelError("controlled file failure"))
        else:
            await world.channel.finish_producers()
        result = await completed(asyncio.create_task(world.channel.deliver_important(
            LogRecord(LogLevel.ERROR, "rejected-important"))))
        assert result.delivered is False and result.appended is False and result.durable is False
        assert result.fallback_written is True
    assert not path.exists()
    assert len(world.fallback) == 1 and "rejected-important" in world.fallback[0]


@pytest.mark.parametrize("facts_shape", ["opaque", "getter_failure"])
async def test_writer_control_with_unusable_facts_settles_receipt_and_keeps_original(facts_shape):
    base = KeyboardInterrupt if facts_shape == "opaque" else asyncio.CancelledError

    class OriginalControl(base):
        @property
        def write_result(self):
            if facts_shape == "getter_failure":
                raise RuntimeError("write facts getter failed")
            return object()

    original = OriginalControl("original writer control")
    fallback = []

    def writer(record):
        raise original

    channel = LogChannel(capacity=4, low_watermark=1, high_watermark=3,
                         writer=writer, fallback=fallback.append)
    runtime = LogRuntime(channel)
    channel.start()
    waiter = asyncio.create_task(await_receipt(channel.submit(
        LogRecord(LogLevel.ERROR, "original-control-record"))))
    closing = None

    async def close_and_capture():
        try:
            await close_logging(runtime)
        except BaseException as error:
            return error
        pytest.fail("关闭没有交付原日志线程控制异常")

    try:
        result = await completed(waiter)
        assert result.delivered is False and result.appended is False and result.durable is False
        assert result.fallback_written is True
        closing = asyncio.create_task(close_and_capture())
        assert await completed(closing) is original
        assert not channel.listener_alive
    finally:
        if closing is None:
            closing = asyncio.create_task(close_and_capture())
        await finish_tasks(waiter, closing)
    assert len(fallback) == 1 and "original-control-record" in fallback[0]


async def test_ignored_mixed_logger_calls_submit_in_order_and_shutdown_drains(tmp_path, recwarn):
    path = tmp_path / "camctl.log"
    async with logging_file(path) as world:
        before = asyncio.all_tasks()
        LOGGER.info("first-info")
        LOGGER.error("second-error")
        LOGGER.warning("third-warning")
        SYNC_LOGGER.info("fourth-sync-info")
        assert asyncio.all_tasks() == before, "每条日志不得创建后台 Task"
    gc.collect()
    assert [record["message"] for record in read_records(path)] == [
        "first-info", "second-error", "third-warning", "fourth-sync-info"]
    assert not world.fallback
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]


@pytest.mark.parametrize("method", ["info", "error"])
async def test_optional_wait_confirms_file_flush_after_business_continues(tmp_path, method):
    path = tmp_path / "camctl.log"
    business = []
    async with logging_file(path, gate="before") as world:
        receipt = getattr(LOGGER, method)("frozen-record")
        assert inspect.isawaitable(receipt) and not inspect.iscoroutine(receipt)
        business.append("necessary-work-completed")
        waiter = asyncio.create_task(await_receipt(receipt))
        try:
            await entered_writer(world)
            assert business == ["necessary-work-completed"]
            assert not waiter.done()
            assert not path.exists() or path.read_bytes() == b""
            world.release.set()
            result = await completed(waiter)
            assert result.decision.kind is AdmissionKind.ACCEPTED
            assert result.delivered is True and not result.fallback_written
            assert result.appended is True and result.durable is (method == "error")
            assert [record["message"] for record in read_records(path)] == ["frozen-record"]
        finally:
            world.release.set()
            await finish_tasks(waiter)


async def test_cancelled_receipt_waiter_does_not_cancel_other_waiter_or_write(tmp_path, monkeypatch):
    path = tmp_path / "camctl.log"
    probe = FileSyncProbe(monkeypatch, path, block="file")
    async with logging_file(path) as world:
        receipt = LOGGER.error("shared-write")
        cancelled = asyncio.create_task(await_receipt(receipt))
        survivor = asyncio.create_task(await_receipt(receipt))
        try:
            assert await asyncio.to_thread(probe.entered.wait, 5)
            assert not cancelled.done() and not survivor.done()
            cancelled.cancel("first cancellation")
            cancelled.cancel("repeated cancellation")
            with pytest.raises(asyncio.CancelledError):
                await completed(cancelled)
            assert not survivor.done()
            probe.release.set()
            result = await completed(survivor)
            assert result.delivered is True and result.appended is True and result.durable is True
            assert await receipt_result(receipt) == result
        finally:
            probe.release.set()
            await finish_tasks(cancelled, survivor)
    assert [record["message"] for record in read_records(path)] == ["shared-write"]
    assert not world.fallback


async def test_full_logger_error_keeps_order_with_original_diagnostic_and_alog(tmp_path):
    path = tmp_path / "camctl.log"
    async with logging_file(path, gate="before", capacity=3, low=1, high=2, debug=True) as world:
        await world.channel.alog(LogRecord(LogLevel.WARNING, "consumer-gate"))
        await entered_writer(world)
        for number in range(3):
            assert world.channel.slog(LogRecord(LogLevel.WARNING, f"backlog-{number}")).decision.kind is AdmissionKind.ACCEPTED
        first = LOGGER.error("first-error")
        state = record_error(ValueError("original-error"), stage="original-consumer")
        original = asyncio.create_task(admit_diagnostic(state))
        await asyncio.sleep(0)
        legacy = asyncio.create_task(world.channel.alog(LogRecord(LogLevel.ERROR, "legacy-error")))
        await asyncio.sleep(0)
        info = LOGGER.info("later-info")
        warning = LOGGER.warning("later-warning")
        last = LOGGER.error("last-error")
        try:
            assert (await receipt_result(info)).decision.kind is AdmissionKind.DROPPED
            assert (await receipt_result(warning)).decision.kind is AdmissionKind.DROPPED
            assert not original.done() and not legacy.done()
            world.release.set()
            assert (await receipt_result(first)).delivered is True
            await completed(original)
            assert (await completed(legacy)).decision.kind is AdmissionKind.ACCEPTED
            assert (await receipt_result(last)).delivered is True
            await flush_diagnostics()
            records = read_records(path)
        finally:
            world.release.set()
            await finish_tasks(original, legacy)
    assert [record.get("message", record.get("error", {}).get("message")) for record in records] == [
        "consumer-gate", "backlog-0", "backlog-1", "backlog-2",
        "first-error", "original-error", "legacy-error", "last-error"]
    assert world.channel.counters.error_dropped == 0 and not world.fallback


async def test_logger_binds_original_admitted_record_before_its_flush(tmp_path):
    path = tmp_path / "camctl.log"
    original = ValueError("first-record-facts")
    async with logging_file(path, gate="before") as world:
        state = record_error(original, stage="original-consumer", details={"attempt": 3})
        assert await admit_diagnostic(state) is state
        assert state.status is DiagnosticStatus.ADMITTED
        await entered_writer(world)
        receipt = LOGGER.error("later-message", exc_info=original)
        waiter = asyncio.create_task(await_receipt(receipt))
        try:
            await asyncio.sleep(0)
            assert not waiter.done(), "ADMITTED 不能被当作追加与 flush 完成"
            world.release.set()
            assert (await completed(waiter)).delivered is True
            await flush_diagnostics()
        finally:
            world.release.set()
            await finish_tasks(waiter)
    records = read_records(path)
    assert len(records) == 1
    assert records[0]["stage"] == "original-consumer"
    assert records[0]["error"]["message"] == "first-record-facts"
    assert records[0]["details"] == {"attempt": 3}
    assert not world.fallback


@pytest.mark.parametrize("case", ["filtered", "sampled", "disabled"])
async def test_logger_receipt_reports_non_write_outcome(tmp_path, case):
    path = tmp_path / "camctl.log"
    options = ({"level": LogLevel.WARNING} if case == "filtered" else
               {"gate": "before", "capacity": 4, "low": 1, "high": 3,
                "probability": 0} if case == "sampled" else {})
    async with logging_file(path, **options) as world:
        if case == "sampled":
            await world.channel.alog(LogRecord(LogLevel.WARNING, "consumer-gate"))
            await entered_writer(world)
            world.channel.slog(LogRecord(LogLevel.WARNING, "backlog"))
        if case == "disabled":
            world.channel.disable_channel(ChannelError("controlled file failure"))
            receipt = LOGGER.error("receipt-outcome")
        else:
            receipt = LOGGER.info("receipt-outcome")
        result = await receipt_result(receipt)
        assert result.delivered is False
        assert result.appended is False and result.durable is False
        assert result.decision.kind is {"filtered": AdmissionKind.FILTERED_OUT,
                                        "sampled": AdmissionKind.SAMPLED_OUT,
                                        "disabled": AdmissionKind.CHANNEL_FAILED}[case]
        assert result.fallback_written is (case == "disabled")
    assert not path.exists() or "receipt-outcome" not in path.read_text(encoding="utf-8")
    assert len(world.fallback) == int(case == "disabled")


async def test_disable_after_durable_write_keeps_writer_success_fact(tmp_path):
    path = tmp_path / "camctl.log"
    async with logging_file(path, gate="after_write") as world:
        waiter = asyncio.create_task(await_receipt(LOGGER.error("already-flushed")))
        try:
            await entered_writer(world)
            assert [record["message"] for record in read_records(path)] == ["already-flushed"]
            world.channel.disable_channel(ChannelError("disabled while writer returns"))
            await asyncio.sleep(0)
            assert not waiter.done()
            world.release.set()
            result = await completed(waiter)
            assert result.delivered is True and not result.fallback_written
            assert result.appended is True and result.durable is True
        finally:
            world.release.set()
            await finish_tasks(waiter)
    assert not world.fallback


@pytest.mark.parametrize("copy_result", ["written", "copy_failed", "suppressed", "not_started", "unwritten",
                                        "file_sync_failed", "directory_sync_failed"])
async def test_original_copy_owner_settles_logger_receipt_once(tmp_path, monkeypatch, copy_result):
    path = tmp_path / "camctl.log"
    if copy_result == "unwritten":
        path.mkdir()
    staging, ready = tmp_path / "staging", tmp_path / "ready"
    logs = staging / "logs"
    logs.mkdir(parents=True)
    ready.mkdir()
    marker = MarkerStore(logs)
    if copy_result == "suppressed":
        marker.create("earlier", "log-copy-earlier.log")
    if copy_result == "copy_failed":
        def fail_copy(source, target, length=0):
            raise OSError("controlled copy failure after original append")
        monkeypatch.setattr("shutil.copyfileobj", fail_copy)
    original = OSError("original-report-error")
    sync_failure = copy_result in ("file_sync_failed", "directory_sync_failed")
    probe = (FileSyncProbe(monkeypatch, path, fail=copy_result.removesuffix("_sync_failed"))
             if sync_failure else None)
    async with logging_file(path) as world:
        state = record_error(original, stage="report-generation")
        reservation = reserve_diagnostic(state)
        assert reservation is not None
        receipt = LOGGER.error("outer-message", exc_info=original)
        waiter = asyncio.create_task(await_receipt(receipt))
        try:
            await asyncio.sleep(0)
            assert not waiter.done()
            assert path.is_dir() if copy_result == "unwritten" else not path.exists()
            if copy_result == "not_started":
                release_diagnostic(reservation)
                await flush_diagnostics()
            else:
                async with diagnostic_scope():
                    assert adopt_diagnostic(reservation) is state
                    copied = await deliver_failure_copy(CopyRequest(
                        trigger=diagnostic_record_for(state), staging_root=staging,
                        ready_dir=ready, channel=world.file, marker=marker))
                    assert copied.outcome is {
                        "written": CopyOutcome.PUBLISHED,
                        "copy_failed": CopyOutcome.COPY_FAILED,
                        "suppressed": CopyOutcome.SUPPRESSED,
                        "unwritten": CopyOutcome.COPY_FAILED,
                        "file_sync_failed": CopyOutcome.COPY_FAILED,
                        "directory_sync_failed": CopyOutcome.COPY_FAILED}[copy_result]
                    if copy_result in ("written", "copy_failed"):
                        assert copied.write_result.appended is True
                        assert copied.write_result.durable is True
                        assert (await completed(waiter)).delivered is True
                    elif sync_failure:
                        assert copied.write_result.appended is True
                        assert copied.write_result.durable is False
                        original_sync_error = f"controlled original {probe.fail} sync failure"
                        assert original_sync_error in copied.write_result.sync_error
                        assert original_sync_error in copied.error
                        observed = await completed(waiter)
                        assert observed.delivered is False and observed.appended is True
                        assert observed.durable is False
                    elif copy_result == "suppressed":
                        assert copied.write_result is None
                        assert not waiter.done()
                    else:
                        assert copied.write_result.appended is False
                        assert not waiter.done()
                await flush_diagnostics()
            result = await completed(waiter)
            successful = copy_result != "unwritten" and not sync_failure
            assert result.delivered is successful and result.durable is successful
            assert result.appended is (copy_result != "unwritten")
            assert result.fallback_written is (copy_result == "unwritten")
            if successful:
                assert state.status is DiagnosticStatus.ADMITTED
            assert await receipt_result(receipt) == result
        finally:
            release_diagnostic(reservation)
            await flush_diagnostics()
            await finish_tasks(waiter)
    if copy_result == "unwritten":
        records = [json.loads(message) for message in world.fallback]
        assert path.is_dir()
    else:
        records = read_records(path)
    assert len(records) == 1
    assert records[0]["stage"] == "report-generation"
    assert records[0]["error"]["message"] == "original-report-error"
    assert len(world.fallback) == int(copy_result == "unwritten")
    if sync_failure:
        assert probe.events.count((probe.fail, "started")) == 1
        assert not list(ready.iterdir())
        assert not list(logs.glob("log-copy-*.log"))
    if copy_result == "written":
        assert read_records(ready / copied.copy_name) == records
