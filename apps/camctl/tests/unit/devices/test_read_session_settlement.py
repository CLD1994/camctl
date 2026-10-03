"""读取及关闭的实际结果必须成为可重复观察的结束事实。"""

import asyncio
import gc
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.devices.read_session import ReadSessionError, SourceFile, SourceStream, open_read
from camctl.operations.models import AttemptTicket

pytestmark = pytest.mark.asyncio


async def _session(stream):
    return await open_read(
        SourceFile("9", {}, 4), 0,
        AttemptTicket(attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9", run_id=1),
        stream=stream, no_data_timeout_s=Decimal("1"),
    )


async def test_read_failure_closes_source_and_retains_partial_byte_count():
    stream = create_autospec(SourceStream, instance=True)
    failure = OSError("read failed")
    stream.read.side_effect = [b"ab", failure]
    session = await _session(stream)
    assert session.read_chunk(2).data == b"ab"
    with pytest.raises(OSError) as raised:
        session.read_chunk(2)
    assert raised.value is failure
    stream.close.assert_called_once_with()
    end = await session.wait_stopped()
    assert (end.stopped, end.bytes_read, end.error) == (True, 2, "failed")


@pytest.mark.parametrize("invalid", [b"cde", "cd", None, 0, bytearray(b"cd")])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_invalid_source_return_cannot_advance_position(invalid, close_fails):
    stream = create_autospec(SourceStream, instance=True)
    stream.read.side_effect = [b"ab", invalid]
    close_failure = OSError("close failed")
    if close_fails:
        stream.close.side_effect = close_failure
    session = await _session(stream)
    assert session.read_chunk(2).data == b"ab"
    if close_fails:
        with pytest.raises(ExceptionGroup) as raised:
            session.read_chunk(2)
        assert isinstance(raised.value.exceptions[0], ReadSessionError)
        assert raised.value.exceptions[1] is close_failure
        with pytest.raises(OSError) as closed:
            await session.wait_stopped()
        assert closed.value is close_failure
    else:
        with pytest.raises(ReadSessionError):
            session.read_chunk(2)
        end = await session.wait_stopped()
        assert (end.stopped, end.bytes_read, end.error) == (True, 2, "failed")
    assert session.position() == 2
    stream.close.assert_called_once_with()


@pytest.mark.parametrize("finish", ["eof", "stop"])
async def test_close_failure_remains_observable_and_cannot_confirm_stop(finish):
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b"abcd"
    failure = OSError("close failed")
    stream.close.side_effect = failure
    session = await _session(stream)
    if finish == "eof":
        with pytest.raises(OSError) as raised:
            session.read_chunk(4)
        assert raised.value is failure
    else:
        session.request_stop()
        with pytest.raises(OSError) as stopped:
            await session.wait_stopped()
        assert stopped.value is failure
    # 已结束的错误会话不能以普通 stopped 响应掩盖关闭失败。
    with pytest.raises(OSError) as later:
        session.read_chunk(1)
    assert later.value is failure
    for _ in range(2):
        with pytest.raises(OSError) as stopped:
            await session.wait_stopped()
        assert stopped.value is failure
    stream.close.assert_called_once_with()


async def test_read_and_close_failures_are_both_retained():
    stream = create_autospec(SourceStream, instance=True)
    read_failure, close_failure = OSError("read failed"), OSError("close failed")
    stream.read.side_effect = read_failure
    stream.close.side_effect = close_failure
    session = await _session(stream)
    with pytest.raises(ExceptionGroup) as raised:
        session.read_chunk(4)
    assert raised.value.exceptions == (read_failure, close_failure)
    with pytest.raises(OSError) as stopped:
        await session.wait_stopped()
    assert stopped.value is close_failure
    stream.close.assert_called_once_with()


@pytest.mark.parametrize("close_fails", [False, True])
async def test_cancel_failure_without_active_read_still_settles_source(close_fails):
    stream = create_autospec(SourceStream, instance=True)
    cancel_failure, close_failure = OSError("cancel failed"), OSError("close failed")
    stream.cancel.side_effect = cancel_failure
    if close_fails:
        stream.close.side_effect = close_failure
    session = await _session(stream)
    with pytest.raises(OSError) as raised:
        session.request_stop()
    assert raised.value is cancel_failure
    if close_fails:
        with pytest.raises(OSError) as stopped:
            await session.wait_stopped()
        assert stopped.value is close_failure
    else:
        assert (await session.wait_stopped()).stopped is True
    stream.close.assert_called_once_with()


async def test_canceled_waiter_does_not_cancel_shared_completion():
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b"abcd"
    session = await _session(stream)
    first = asyncio.create_task(session.wait_stopped())
    second = asyncio.create_task(session.wait_stopped())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    stream.close.assert_not_called()
    assert session.read_chunk(4).eof is True
    end = await second
    assert end == await session.wait_stopped()
    assert (end.stopped, end.bytes_read, end.error) == (True, 4, None)


async def test_stop_after_successful_completion_does_not_touch_closed_source():
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b"abcd"
    session = await _session(stream)
    session.read_chunk(4)
    session.request_stop()
    stream.cancel.assert_not_called()
    stream.close.assert_called_once_with()
    assert (await session.wait_stopped()).error is None


async def test_repeated_stop_does_not_report_close_failure_at_request_entry():
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b"abcd"
    failure = OSError("close failed")
    stream.close.side_effect = failure
    session = await _session(stream)
    with pytest.raises(OSError):
        session.read_chunk(4)
    session.request_stop()
    session.request_stop()
    stream.cancel.assert_not_called()
    stream.close.assert_called_once_with()
    with pytest.raises(OSError) as stopped:
        await session.wait_stopped()
    assert stopped.value is failure


async def test_abandoned_waiter_keeps_close_failure_observed():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))

    async def scenario():
        stream = create_autospec(SourceStream, instance=True)
        stream.read.return_value = b"abcd"
        stream.close.side_effect = OSError("close failed")
        session = await _session(stream)
        waiting = asyncio.create_task(session.wait_stopped())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        with pytest.raises(OSError):
            session.read_chunk(4)
        with pytest.raises(OSError):
            await session.wait_stopped()

    try:
        await scenario()
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous)
