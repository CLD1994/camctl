"""同步登记、原日志线程和关闭共同交付实际写入回执。"""

import asyncio
import json
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


@pytest.mark.parametrize("first_written", [False, True])
async def test_disable_keeps_later_fallback_behind_actual_inflight_fact(first_written):
    entered, release = threading.Event(), threading.Event()
    fallback = []

    def writer(record):
        entered.set()
        assert release.wait(5)
        return first_written

    channel = channel_for(writer, fallback=fallback.append)
    first = channel.submit(LogRecord(LogLevel.ERROR, "first"))
    channel.start()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = channel.submit(LogRecord(LogLevel.ERROR, "second"))
        channel.disable_channel(ChannelError("external disable"))
        third = channel.submit(LogRecord(LogLevel.ERROR, "third"))
        assert fallback == []
        assert second.write_result is None and third.write_result is None
        release.set()
        await finish(channel)
        assert (await first).delivered is first_written
        assert (await second).decision.kind is AdmissionKind.ACCEPTED
        assert (await third).decision.kind is AdmissionKind.CHANNEL_FAILED
        assert (await second).fallback_written and (await third).fallback_written
        expected = ["second", "third"] if first_written else ["first", "second", "third"]
        assert [json.loads(message)["message"] for message in fallback] == expected
    finally:
        release.set()
        if channel.listener_alive:
            await finish(channel)


@pytest.mark.parametrize("writer_raises", [False, True])
async def test_consumer_fallback_control_preserves_all_receipts_and_close(writer_raises):
    original = asyncio.CancelledError("stderr control")
    attempts = []

    def writer(record):
        if record.message == "first":
            if writer_raises:
                raise ValueError("file failed")
            return False
        return True

    def fallback(message):
        name = json.loads(message)["message"]
        attempts.append(name)
        if name == "first":
            raise original

    channel = channel_for(writer, fallback=fallback)
    first = channel.submit(LogRecord(LogLevel.ERROR, "first"))
    second = channel.submit(LogRecord(LogLevel.ERROR, "second"))
    channel.start()
    joining = asyncio.create_task(channel.join())
    try:
        done, _ = await asyncio.wait({joining}, timeout=2)
        assert joining in done, "消费侧控制异常不能遗留无法结束的队列责任"
        await joining
        assert (await first).delivered is False
        assert (await first).fallback_written is False
        assert (await second).delivered is (not writer_raises)
        assert (await second).fallback_written is writer_raises
        expected = ["first", "second"] if writer_raises else ["first"]
        assert attempts == expected
        with pytest.raises(asyncio.CancelledError) as raised:
            await close_logging(LogRuntime(channel))
        assert raised.value is original
        assert not channel.listener_alive
    finally:
        joining.cancel()
        await asyncio.gather(joining, return_exceptions=True)
        channel.async_close()
        if channel.listener_alive:
            await asyncio.to_thread(channel.stop_listener)


async def test_writer_control_reaches_close_after_remaining_receipts_finish():
    original = asyncio.CancelledError("writer control")
    fallback = []

    def writer(record):
        raise original

    channel = channel_for(writer, fallback=fallback.append)
    first = channel.submit(LogRecord(LogLevel.ERROR, "first"))
    second = channel.submit(LogRecord(LogLevel.ERROR, "second"))
    channel.start()
    with pytest.raises(asyncio.CancelledError) as raised:
        await close_logging(LogRuntime(channel))
    assert raised.value is original
    assert not channel.listener_alive
    assert (await first).delivered is False and (await second).delivered is False
    assert (await first).fallback_written and (await second).fallback_written
    assert [json.loads(message)["message"] for message in fallback] == ["first", "second"]


async def test_receipt_callbacks_can_complete_each_other_from_two_threads():
    from camctl.logging_runtime.admission import AdmissionDecision
    from camctl.logging_runtime.service import AdmissionResult

    first, second = LogReceipt(), LogReceipt()
    result = AdmissionResult(AdmissionDecision(AdmissionKind.CHANNEL_FAILED, LogLevel.ERROR),
                             None, delivered=False)
    rendezvous = threading.Barrier(2)
    finished = []

    def finish_second(_):
        rendezvous.wait(timeout=2)
        second.complete_admission(result)
        finished.append("first")

    def finish_first(_):
        rendezvous.wait(timeout=2)
        first.complete_write(result)
        finished.append("second")

    first.add_done_callback(finish_second)
    second.add_admission_callback(finish_first)
    threads = [threading.Thread(target=first.complete_write, args=(result,), daemon=True),
               threading.Thread(target=second.complete_admission, args=(result,), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        await asyncio.to_thread(thread.join, 2)
    assert all(not thread.is_alive() for thread in threads)
    assert set(finished) == {"first", "second"}


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_disable_completes_old_admission_before_inflight_writer_returns(cancel_waiter):
    from camctl.logging_runtime.service import admission_result_for

    entered, release = threading.Event(), threading.Event()
    channel = channel_for(lambda record: entered.set() or release.wait(5))
    channel.start()
    first = channel.submit(LogRecord(LogLevel.ERROR, "writing"))
    waiter = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for index in range(3):
            channel.submit(LogRecord(LogLevel.WARNING, str(index)))
        waiter = asyncio.create_task(channel.alog(LogRecord(LogLevel.ERROR, "waiting")))
        await asyncio.sleep(0)
        if cancel_waiter:
            waiter.cancel("original cancellation")
            await asyncio.sleep(0)
        channel.disable_channel(ChannelError("disabled"))
        done, _ = await asyncio.wait({waiter}, timeout=1)
        assert waiter in done, "旧接纳等待应在通道失效时结束，不等待当前 writer"
        if cancel_waiter:
            with pytest.raises(asyncio.CancelledError) as raised:
                await waiter
            result = admission_result_for(raised.value)
            assert raised.value.args == ("original cancellation",)
        else:
            result = await waiter
        assert result.decision.kind is AdmissionKind.CHANNEL_FAILED
        assert first.write_result is None
    finally:
        release.set()
        await finish(channel)
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


async def test_unprintable_writer_error_finishes_receipts_without_formatting_error():
    class UnprintableError(ValueError):
        def __str__(self):
            raise AssertionError("原异常不能再次格式化")

    original = UnprintableError()
    fallback = []

    def writer(record):
        raise original

    channel = channel_for(writer, fallback=fallback.append)
    first = channel.submit(LogRecord(LogLevel.ERROR, "first"))
    second = channel.submit(LogRecord(LogLevel.ERROR, "second"))
    channel.start()
    joining = asyncio.create_task(channel.join())
    try:
        done, _ = await asyncio.wait({joining}, timeout=1)
        assert joining in done, "原异常无法转成字符串时仍须完成全部日志责任"
        await joining
        assert first.write_result.delivered is False and second.write_result.delivered is False
        assert first.write_result.fallback_written and second.write_result.fallback_written
        assert [json.loads(message)["message"] for message in fallback] == ["first", "second"]
        await finish(channel)
    finally:
        joining.cancel()
        await asyncio.gather(joining, return_exceptions=True)
        channel.async_close()
        if channel.listener_alive:
            await asyncio.to_thread(channel.stop_listener)
