"""真实读取线程的结束结果与协程等待者协调，不连接实际设备。"""

import asyncio
import threading
from decimal import Decimal

import pytest

from camctl.devices.read_session import SourceFile, SourceStream, open_read
from camctl.host_files.segments import SegmentSpec, WritableFile, transfer_segment
from camctl.operations.models import AttemptTicket

pytestmark = pytest.mark.asyncio


class ControlledStream(SourceStream):
    def __init__(self, *, read_failure=None, cancel_failure=None, close_failure=None, data=b"abcd"):
        self.read_started = threading.Event()
        self.allow_read_return = threading.Event()
        self.close_started = threading.Event()
        self.allow_close_return = threading.Event()
        self.read_failure = read_failure
        self.cancel_failure = cancel_failure
        self.close_failure = close_failure
        self.close_count = 0
        self.data = data

    def read(self, limit):
        self.read_started.set()
        assert self.allow_read_return.wait(5), "测试未释放读取端"
        if self.read_failure is not None:
            raise self.read_failure
        return self.data[:limit]

    def cancel(self):
        if self.cancel_failure is not None:
            raise self.cancel_failure
        self.allow_read_return.set()

    def close(self):
        self.close_count += 1
        self.close_started.set()
        assert self.allow_close_return.wait(5), "测试未释放关闭端"
        if self.close_failure is not None:
            raise self.close_failure


async def _open(stream):
    return await open_read(SourceFile("9", {}, 4), 0,
        AttemptTicket(attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9", run_id=1),
        stream=stream, no_data_timeout_s=Decimal("30"))


@pytest.mark.parametrize("read_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_waiters_observe_actual_close_result_after_reader_returns(read_fails, close_fails):
    read_failure, close_failure = OSError("read failed"), OSError("close failed")
    stream = ControlledStream(read_failure=read_failure if read_fails else None,
                              close_failure=close_failure if close_fails else None)
    session = await _open(stream)
    canceled = asyncio.create_task(session.wait_stopped())
    waiting = asyncio.create_task(session.wait_stopped())
    reading = asyncio.create_task(asyncio.to_thread(session.read_chunk, 4))
    try:
        assert await asyncio.to_thread(stream.read_started.wait, 5)
        canceled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled
        stream.allow_read_return.set()
        assert await asyncio.to_thread(stream.close_started.wait, 5)
        assert not waiting.done()
        assert not reading.done()
        assert session.poll_stopped() is None
        stream.allow_close_return.set()
        read_result, stop_result = await asyncio.wait_for(
            asyncio.gather(reading, waiting, return_exceptions=True), 5)
        if close_fails:
            assert stop_result is close_failure
            with pytest.raises(OSError) as polled:
                session.poll_stopped()
            assert polled.value is close_failure
            if read_fails:
                assert isinstance(read_result, ExceptionGroup)
                assert read_result.exceptions == (read_failure, close_failure)
            else:
                assert read_result is close_failure
        else:
            assert stop_result.stopped is True
            assert session.poll_stopped() == stop_result
            assert stop_result.bytes_read == (0 if read_fails else 4)
            if read_fails:
                assert read_result is read_failure
                assert stop_result.error == "failed"
            else:
                assert read_result.data == b"abcd" and read_result.eof
                assert stop_result.error is None
        assert stream.close_count == 1
    finally:
        stream.allow_read_return.set()
        stream.allow_close_return.set()
        await asyncio.wait_for(asyncio.gather(reading, waiting, canceled, return_exceptions=True), 5)


async def test_failed_stop_request_does_not_close_under_active_read():
    failure = OSError("cancel failed")
    stream = ControlledStream(cancel_failure=failure)
    session = await _open(stream)
    waiting = asyncio.create_task(session.wait_stopped())
    reading = asyncio.create_task(asyncio.to_thread(session.read_chunk, 4))
    try:
        assert await asyncio.to_thread(stream.read_started.wait, 5)
        with pytest.raises(OSError) as raised:
            session.request_stop()
        assert raised.value is failure
        assert not stream.close_started.is_set()
        assert not waiting.done()
        stream.allow_read_return.set()
        stream.allow_close_return.set()
        assert (await asyncio.wait_for(reading, 5)).eof is True
        assert (await asyncio.wait_for(waiting, 5)).stopped is True
        assert stream.close_count == 1
    finally:
        stream.allow_read_return.set()
        stream.allow_close_return.set()
        await asyncio.wait_for(asyncio.gather(reading, waiting, return_exceptions=True), 5)


async def test_idle_stop_keeps_event_loop_available_until_close_returns():
    stream = ControlledStream()
    session = await _open(stream)
    waiting = asyncio.create_task(session.wait_stopped())
    try:
        session.request_stop()
        assert await asyncio.to_thread(stream.close_started.wait, 5)
        assert not waiting.done()
        # 由事件循环释放关闭端；若 request_stop 同步关闭，则无法执行到这里。
        stream.allow_close_return.set()
        assert (await asyncio.wait_for(waiting, 5)).stopped is True
        assert stream.close_count == 1
    finally:
        stream.allow_close_return.set()
        await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 5)


@pytest.mark.parametrize("cancel_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_stop_settles_after_partial_read_without_another_read(cancel_fails, close_fails):
    cancel_failure, close_failure = OSError("cancel failed"), OSError("close failed")
    stream = ControlledStream(data=b"ab", cancel_failure=cancel_failure if cancel_fails else None,
                              close_failure=close_failure if close_fails else None)
    session = await _open(stream)
    reading = asyncio.create_task(asyncio.to_thread(session.read_chunk, 4))
    try:
        assert await asyncio.to_thread(stream.read_started.wait, 5)
        if cancel_fails:
            with pytest.raises(OSError) as raised:
                session.request_stop()
            assert raised.value is cancel_failure
        else:
            session.request_stop()
        stream.allow_read_return.set()
        chunk = await asyncio.wait_for(reading, 5)
        assert chunk.data == b"ab" and not chunk.eof
        assert await asyncio.to_thread(stream.close_started.wait, 5)
        stream.allow_close_return.set()
        if close_fails:
            with pytest.raises(OSError) as raised:
                await asyncio.wait_for(session.wait_stopped(), 5)
            assert raised.value is close_failure
        else:
            end = await asyncio.wait_for(session.wait_stopped(), 5)
            assert (end.stopped, end.bytes_read, end.error) == (True, 2, "stopped")
        assert stream.close_count == 1
    finally:
        stream.allow_read_return.set()
        stream.allow_close_return.set()
        await asyncio.gather(reading, return_exceptions=True)
        # 中途断言失败时也须等待实际收场，关闭异常由 gather 收集。
        try:
            session.request_stop()
        except OSError:
            pass
        await asyncio.wait_for(asyncio.gather(session.wait_stopped(), return_exceptions=True), 5)


async def test_segment_stop_finishes_session_without_next_source_read():
    stream = ControlledStream(data=b"ab")
    stream.allow_read_return.set()
    session = await _open(stream)
    stop = threading.Event()

    class Target(WritableFile):
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)
            stop.set()
            session.request_stop()
            return len(data)

        def sync(self):
            raise AssertionError("取消后的段不能新增同步")

    target = Target()
    spec = SegmentSpec(attempt="copy/9", round_index=0, target_name="copy.part",
                       range_start=0, range_end=4, chunk_size=2, stop=stop)
    transfer = asyncio.create_task(asyncio.to_thread(transfer_segment, spec, session, target))
    try:
        result = await asyncio.wait_for(transfer, 5)
        assert (result.processed_end, result.synced, result.error) == (2, False, "stopped")
        assert result.source_end is None
        assert result.source_close_error is None
        assert target.data == b"ab"
        assert await asyncio.to_thread(stream.close_started.wait, 5)
        stream.allow_close_return.set()
        end = await asyncio.wait_for(session.wait_stopped(), 5)
        assert (end.stopped, end.bytes_read, end.error) == (True, 2, "stopped")
        assert stream.close_count == 1
    finally:
        stream.allow_close_return.set()
        await asyncio.gather(transfer, return_exceptions=True)
        session.request_stop()
        await asyncio.wait_for(asyncio.gather(session.wait_stopped(), return_exceptions=True), 5)
