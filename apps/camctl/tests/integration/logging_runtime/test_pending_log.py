"""L3 重要日志取消与通道禁用的组件集成测试。

队列满且协程取消：必要收场不被日志等待阻塞，重要记录由自身责
任保留；通道禁用后等待者以失败凭据退出，未写记录不标成功。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import ChannelError, LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


class BlockingWriter:
    """让监听线程停在第一条记录上，直到测试释放。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, record: LogRecord) -> bool:
        self.started.set()
        if not self.release.wait(10):
            raise TimeoutError("测试未在期限内释放日志写入")
        return True


async def _finish_channel(channel, tasks=()):
    try:
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            await asyncio.gather(*done, return_exceptions=True)
            if pending:
                raise TimeoutError("日志等待任务未在期限内结束")
    finally:
        try:
            stopped = await asyncio.to_thread(channel.stop_listener, timeout=10)
            assert stopped
        finally:
            channel.async_close()
            await channel.await_closed()


class TestPendingLog:
    def _channel(self, writer) -> LogChannel:
        return LogChannel(
            capacity=3,
            low_watermark=1,
            high_watermark=2,
            level=LogLevel.DEBUG,
            writer=writer,
        )

    async def test_log_wait_does_not_block_stop(self) -> None:
        writer = BlockingWriter()
        channel = self._channel(writer)
        channel.start()
        waiter = asyncio.create_task(channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="重要记录")))
        entered = asyncio.create_task(asyncio.to_thread(writer.started.wait, 5))
        try:
            done, _ = await asyncio.wait({entered, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                waiter.result()
                pytest.fail("日志等待在写入开始前结束")
            assert entered.result()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert channel.pending_count() == 1
        finally:
            writer.release.set()
            await _finish_channel(channel, (entered, waiter))

    async def test_disable_channel_preserves_inflight_writer_fact(self) -> None:
        writer = BlockingWriter()
        channel = self._channel(writer)
        channel.start()
        waiter = asyncio.create_task(asyncio.wait_for(channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="x")), timeout=5))
        entered = asyncio.create_task(asyncio.to_thread(writer.started.wait, 5))
        try:
            done, _ = await asyncio.wait({entered, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                waiter.result()
                pytest.fail("日志等待在写入开始前结束")
            assert entered.result()
            channel.disable_channel(ChannelError("磁盘不可用"))
            assert not waiter.done()
            result = await channel.alog(LogRecord(level=LogLevel.ERROR, message="y"))
            assert result.decision.kind.value == "channel_failed"
            writer.release.set()
            assert (await waiter).delivered is True
        finally:
            writer.release.set()
            await _finish_channel(channel, (entered, waiter))

    async def test_accepted_record_delivers_receipt(self) -> None:
        written: list[LogRecord] = []

        def writer(record: LogRecord) -> bool:
            written.append(record)
            return True

        channel = self._channel(writer)
        try:
            channel.start()
            result = await channel.deliver_important(
                LogRecord(level=LogLevel.ERROR, message="ok")
            )
            assert result.delivered is True
        finally:
            await _finish_channel(channel)
        assert [r.message for r in written] == ["ok"]
