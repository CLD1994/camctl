"""真实 Janus、生产协程和日志线程的错误交接契约。"""

from __future__ import annotations

import asyncio
import threading

import pytest

from camctl.logging_runtime.admission import AdmissionKind, LogSource
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


def channel_for(writer, *, level=LogLevel.DEBUG):
    return LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                      level=level, writer=writer)


def fill(channel):
    for index in range(3):
        assert channel.slog(LogRecord(LogLevel.WARNING, f"warm-{index}")).decision.kind is AdmissionKind.ACCEPTED


async def advance():
    for _ in range(4):
        await asyncio.sleep(0)


async def finish(channel):
    channel.start()
    await asyncio.to_thread(channel.stop_listener)
    channel.async_close()
    await channel.await_closed()


async def test_error_waits_in_caller_without_thread_pool(monkeypatch):
    written = []
    channel = channel_for(lambda record: written.append(record) or True)
    fill(channel)
    async def forbidden(*args, **kwargs):
        raise AssertionError("日志接纳不能占用默认线程池")
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "to_thread", forbidden)
        task = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "error")))
        await advance()
        waiting = not task.done()
        channel.start()
        result = await task
    await finish(channel)
    assert waiting
    assert result.decision.kind is AdmissionKind.ACCEPTED
    assert channel.counters.error_dropped == 0
    assert [entry.message for entry in written] == ["warm-0", "warm-1", "warm-2", "error"]


@pytest.mark.parametrize("disable", [False, True])
async def test_repeated_cancel_keeps_one_record_and_actual_admission(disable):
    from camctl.logging_runtime.service import admission_result_for
    written = []
    channel = channel_for(lambda record: written.append(record) or True)
    fill(channel)
    task = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "held")))
    await advance()
    task.cancel("original cancel")
    await advance()
    task.cancel("repeated cancel")
    await advance()
    pending = not task.done()
    if disable:
        channel.disable_channel(ChannelError("disabled"))
    else:
        channel.start()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    result = admission_result_for(raised.value)
    await finish(channel)
    assert pending
    assert raised.value.args == ("original cancel",)
    assert result.decision.kind is (AdmissionKind.CHANNEL_FAILED if disable else AdmissionKind.ACCEPTED)
    assert [entry.message for entry in written].count("held") == (0 if disable else 1)
    assert channel.counters.error_dropped == 0


async def test_disable_wakes_all_waiting_error_producers():
    channel = channel_for(lambda record: True)
    fill(channel)
    tasks = [asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, str(index)))) for index in range(3)]
    await advance()
    waiting = all(not task.done() for task in tasks)
    channel.disable_channel(ChannelError("disabled"))
    results = await asyncio.gather(*tasks)
    await finish(channel)
    assert waiting
    assert all(result.decision.kind is AdmissionKind.CHANNEL_FAILED for result in results)
    assert channel.counters.error_dropped == 0


async def test_normal_error_returns_before_writer_finishes():
    started, release = threading.Event(), threading.Event()
    def writer(record):
        started.set()
        assert release.wait(5)
        return True
    channel = channel_for(writer)
    channel.start()
    try:
        result = await channel.alog(LogRecord(LogLevel.ERROR, "accepted"))
        assert result.decision.kind is AdmissionKind.ACCEPTED
        assert result.delivered is None
        assert await asyncio.to_thread(started.wait, 5)
    finally:
        release.set()
        await finish(channel)


async def test_close_finishes_waiting_producer_before_stop_marker():
    written = []
    channel = channel_for(lambda record: written.append(record) or True)
    fill(channel)
    producer = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "last")))
    await advance()
    closing = asyncio.create_task(close_logging(LogRuntime(channel)))
    await advance()
    waiting = not closing.done()
    channel.start()
    await producer
    result = await closing
    assert waiting
    assert result.listener_stopped
    assert [entry.message for entry in written] == ["warm-0", "warm-1", "warm-2", "last"]
    late = await channel.alog(LogRecord(LogLevel.ERROR, "too late"))
    assert late.decision.kind is AdmissionKind.CHANNEL_FAILED


async def test_concurrent_identical_records_keep_separate_write_receipts():
    first_started, second_started = threading.Event(), threading.Event()
    first_release, second_release = threading.Event(), threading.Event()
    calls = []
    def writer(record):
        calls.append(record)
        if len(calls) == 1:
            first_started.set()
            assert first_release.wait(5)
            return True
        second_started.set()
        assert second_release.wait(5)
        return False
    channel = channel_for(writer)
    channel.start()
    record = LogRecord(LogLevel.ERROR, "same")
    first = asyncio.create_task(channel.deliver_important(record))
    second = asyncio.create_task(channel.deliver_important(record))
    try:
        assert await asyncio.to_thread(first_started.wait, 5)
        first_release.set()
        assert (await first).delivered is True
        assert await asyncio.to_thread(second_started.wait, 5)
        assert not second.done()
        second_release.set()
        assert (await second).delivered is False
        assert channel.pending_count() == 0
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await finish(channel)


async def test_warning_configuration_filters_lower_levels():
    channel = channel_for(lambda record: True, level=LogLevel.WARNING)
    try:
        for level in (LogLevel.DEBUG, LogLevel.INFO):
            result = await channel.alog(LogRecord(level, "filtered"))
            assert result.decision.kind is AdmissionKind.FILTERED_OUT
        assert channel.counters.total_dropped() == 0
    finally:
        await finish(channel)


async def test_cancel_racing_with_successful_put_does_not_enqueue_twice(monkeypatch):
    from camctl.logging_runtime.service import admission_result_for
    written = []
    channel = channel_for(lambda record: written.append(record) or True)
    fill(channel)
    real_put = channel._queue.async_q.put
    accepted = asyncio.Event()
    control_errors = []
    async def put_then_cancel_boundary(entry):
        await real_put(entry)
        accepted.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            control_errors.append(error)
            raise
    monkeypatch.setattr(channel._queue.async_q, "put", put_then_cancel_boundary)
    task = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "race")))
    await advance()
    channel.start()
    await accepted.wait()
    task.cancel("after put")
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    await finish(channel)
    assert raised.value is control_errors[0]
    assert admission_result_for(raised.value).decision.kind is AdmissionKind.ACCEPTED
    assert [record.message for record in written].count("race") == 1


@pytest.mark.parametrize("failed_before", [False, True])
async def test_sync_error_falls_back_once_when_full_or_disabled(failed_before):
    fallback, written = [], []
    channel = channel_for(lambda record: written.append(record) or True)
    fill(channel)
    if failed_before:
        channel.disable_channel(ChannelError("no file"))
    record = LogRecord(LogLevel.ERROR, "complete error", LogSource.SYNC, fallback=fallback.append)
    result = channel.slog(record)
    assert result.decision.kind is AdmissionKind.FALLBACK_STDERR
    assert result.fallback_written
    assert fallback == ["complete error"]
    assert channel.counters.error_dropped == 0
    await finish(channel)
    assert "complete error" not in [entry.message for entry in written]
    assert fallback == ["complete error"]


async def test_accepted_standard_error_and_disabled_queue_retain_complete_diagnostics():
    import logging
    from camctl.logging_runtime.standard_logging import install_standard_logging
    root = logging.RootLogger(logging.DEBUG)
    manager = logging.Manager(root)
    root.manager = manager
    fallback = []
    def failed_writer(record):
        raise OSError("file failed")
    channel = channel_for(failed_writer)
    route = install_standard_logging(channel, fallback.append, manager=manager)
    logger = manager.getLogger("vendor")
    try:
        try:
            raise ValueError("original exception")
        except ValueError:
            logger.exception("first diagnostic")
        logger.critical("second diagnostic")
        channel.start()
        await channel.join()
        assert channel.disabled
        assert len(fallback) == 2
        assert "first diagnostic" in fallback[0]
        assert "original exception" in fallback[0]
        assert "traceback" in fallback[0]
        assert "second diagnostic" in fallback[1]
        assert "CRITICAL/50" in fallback[1]
        assert channel.counters.error_dropped == 0
    finally:
        route.close()
        await finish(channel)


async def test_failed_stderr_is_not_retried_or_logged_recursively():
    attempts = []
    def unavailable(message):
        attempts.append(message)
        raise OSError("stderr failed")
    channel = LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                         writer=lambda record: True, fallback=unavailable)
    fill(channel)
    first = channel.slog(LogRecord(LogLevel.ERROR, "first", LogSource.SYNC))
    second = channel.slog(LogRecord(LogLevel.ERROR, "second", LogSource.SYNC))
    assert not first.fallback_written and not second.fallback_written
    assert attempts == ["first"]
    await finish(channel)


@pytest.mark.parametrize("raises", [False, True])
async def test_important_receipt_exposes_single_stderr_handoff(raises):
    from camctl.logging_runtime.service import admission_result_for
    fallback = []
    def writer(record):
        if raises:
            raise OSError("write failed")
        return False
    channel = LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                         writer=writer, fallback=fallback.append)
    channel.start()
    try:
        try:
            result = await channel.deliver_important(LogRecord(LogLevel.ERROR, "important error"))
        except ChannelError as error:
            result = admission_result_for(error)
        assert result.delivered is False
        assert result.fallback_written
        assert fallback == ["important error"]
    finally:
        await finish(channel)
    assert fallback == ["important error"]
