"""同步登记、原日志线程和关闭共同交付实际写入回执。"""

import asyncio
import threading

import pytest

from camctl.logging_runtime.admission import AdmissionKind, LogSource
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


def channel_for(writer, *, fallback=None):
    return LogChannel(capacity=3, low_watermark=1, high_watermark=2,
                      level=LogLevel.DEBUG, writer=writer, fallback=fallback)


async def finish(channel):
    channel.start()
    await close_logging(LogRuntime(channel))


async def test_submit_without_wait_still_writes_and_repeated_wait_does_not_resubmit():
    written = []
    channel = channel_for(lambda record: written.append(record.message) or True)
    receipt = channel.submit(LogRecord(LogLevel.INFO, "immediate"))
    assert receipt.admission_result.decision.kind is AdmissionKind.ACCEPTED
    assert receipt.write_result is None
    await finish(channel)
    assert (await receipt).delivered is True
    assert (await receipt).delivered is True
    assert written == ["immediate"]


async def test_full_error_is_promoted_before_later_submissions_and_old_alog():
    written = []
    channel = channel_for(lambda record: written.append(record.message) or True)
    for index in range(3):
        channel.submit(LogRecord(LogLevel.WARNING, f"warm-{index}"))
    first = channel.submit(LogRecord(LogLevel.ERROR, "first"))
    old = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "old-alog")))
    await asyncio.sleep(0)
    info = channel.submit(LogRecord(LogLevel.INFO, "later-info"))
    warning = channel.submit(LogRecord(LogLevel.WARNING, "later-warning"))
    last = channel.submit(LogRecord(LogLevel.ERROR, "last"))
    assert first.admission_result is None
    assert info.write_result.delivered is False
    assert warning.write_result.delivered is False
    await finish(channel)
    assert (await old).decision.kind is AdmissionKind.ACCEPTED
    assert (await first).delivered and (await last).delivered
    assert written[:-1] == ["warm-0", "warm-1", "warm-2", "first", "old-alog", "last"]
    assert "info=1 warning=1" in written[-1]


async def test_disable_settles_overflow_with_actual_not_admitted_fact_once():
    fallback = []
    channel = channel_for(lambda record: True, fallback=fallback.append)
    for index in range(3):
        channel.submit(LogRecord(LogLevel.WARNING, str(index)))
    receipt = channel.submit(LogRecord(LogLevel.ERROR, "held"))
    channel.disable_channel(ChannelError("disabled"))
    channel.disable_channel(ChannelError("disabled again"))
    result = await receipt
    assert result.decision.kind is AdmissionKind.CHANNEL_FAILED
    assert result.delivered is False and result.fallback_written
    assert len(fallback) == 1 and "held" in fallback[0]
    await finish(channel)


async def test_disable_during_writer_preserves_actual_success():
    entered, release = threading.Event(), threading.Event()
    fallback = []

    def writer(record):
        entered.set()
        assert release.wait(5)
        channel.disable_channel(ChannelError("lock release failed after flush"))
        return True

    channel = channel_for(writer, fallback=fallback.append)
    channel.start()
    receipt = channel.submit(LogRecord(LogLevel.ERROR, "appended"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        channel.disable_channel(ChannelError("external disable"))
        assert receipt.write_result is None
        release.set()
        assert (await receipt).delivered is True
        assert fallback == []
    finally:
        release.set()
        await finish(channel)


async def test_sync_error_full_keeps_immediate_stderr_policy():
    fallback = []
    channel = channel_for(lambda record: True, fallback=fallback.append)
    for index in range(3):
        channel.submit(LogRecord(LogLevel.WARNING, str(index)))
    receipt = channel.submit(LogRecord(LogLevel.ERROR, "vendor", LogSource.SYNC))
    result = await receipt
    assert result.decision.kind is AdmissionKind.FALLBACK_STDERR
    assert result.delivered is False and result.fallback_written
    assert len(fallback) == 1
    await finish(channel)


async def test_supplied_receipt_is_shared_and_never_resubmits():
    written = []
    channel = channel_for(lambda record: written.append(record.message) or True)
    shared = LogReceipt()
    assert channel.submit(LogRecord(LogLevel.ERROR, "first"), receipt=shared) is shared
    assert channel.submit(LogRecord(LogLevel.ERROR, "later"), receipt=shared) is shared
    await finish(channel)
    assert (await shared).delivered is True
    assert written == ["first"]


async def test_close_queue_settles_admitted_and_overflow_without_claiming_write():
    fallback = []
    channel = channel_for(lambda record: True, fallback=fallback.append)
    accepted = channel.submit(LogRecord(LogLevel.ERROR, "admitted"))
    for index in range(2):
        channel.submit(LogRecord(LogLevel.WARNING, str(index)))
    held = channel.submit(LogRecord(LogLevel.ERROR, "held"))
    channel.async_close()
    await channel.await_closed()
    accepted_result, held_result = await accepted, await held
    assert accepted_result.decision.kind is AdmissionKind.ACCEPTED
    assert held_result.decision.kind is AdmissionKind.CHANNEL_FAILED
    assert accepted_result.delivered is False and held_result.delivered is False
    assert accepted_result.fallback_written and held_result.fallback_written
    assert len(fallback) == 2 and "admitted" in fallback[0] and "held" in fallback[1]


async def test_fallback_control_still_settles_claimed_receipt_once():
    original = asyncio.CancelledError("fallback canceled")
    attempts = []

    def fallback(message):
        attempts.append(message)
        raise original

    channel = channel_for(lambda record: True, fallback=fallback)
    channel.disable_channel(ChannelError("disabled"))
    receipt = LogReceipt()
    with pytest.raises(asyncio.CancelledError) as raised:
        channel.submit(LogRecord(LogLevel.ERROR, "held"), receipt=receipt)
    assert raised.value is original
    result = await receipt
    assert result.decision.kind is AdmissionKind.CHANNEL_FAILED
    assert result.delivered is False and result.fallback_written is False
    assert channel.submit(LogRecord(LogLevel.ERROR, "retry"), receipt=receipt) is receipt
    assert len(attempts) == 1
    await finish(channel)
