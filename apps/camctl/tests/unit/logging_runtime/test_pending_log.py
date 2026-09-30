"""L3 重要日志取消与通道禁用的单元/组件测试。

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
    """永不完成的写入：让监听线程停在第一条记录上。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, record: LogRecord) -> bool:
        self.started.set()
        self.release.wait(timeout=10)
        return True


class TestPendingLog:
    def _channel(self, writer) -> LogChannel:
        return LogChannel(
            capacity=2,
            low_watermark=1,
            high_watermark=2,
            level=LogLevel.DEBUG,
            writer=writer,
        )

    async def test_log_wait_does_not_block_stop(self) -> None:
        writer = BlockingWriter()
        channel = self._channel(writer)
        channel.start()

        async def important_waiter() -> None:
            await channel.deliver_important(
                LogRecord(level=LogLevel.ERROR, message="重要记录")
            )

        waiter = asyncio.create_task(important_waiter())
        await asyncio.get_running_loop().run_in_executor(None, writer.started.wait, 5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # 必要收场可以立即开始：等待者取消不阻塞停止。
        assert channel.pending_count() == 1
        writer.release.set()
        await asyncio.to_thread(channel.stop_listener, timeout=10)

    async def test_disable_channel_exits_waiters_with_failure(self) -> None:
        writer = BlockingWriter()
        channel = self._channel(writer)
        channel.start()
        waiter = asyncio.create_task(
            asyncio.wait_for(
                channel.deliver_important(LogRecord(level=LogLevel.ERROR, message="x")),
                timeout=5,
            )
        )
        await asyncio.get_running_loop().run_in_executor(None, writer.started.wait, 5)
        channel.disable_channel(ChannelError("磁盘不可用"))
        # 等待者以通道失败凭据立即退出（未写记录不标成功）。
        with pytest.raises(ChannelError):
            await waiter
        # 等待中的记录凭据未标成功；通道禁用后续接纳按通道故障分类。
        result = await channel.alog(LogRecord(level=LogLevel.ERROR, message="y"))
        assert result.decision.kind.value == "channel_failed"
        writer.release.set()
        await asyncio.to_thread(channel.stop_listener, timeout=10)

    async def test_accepted_record_delivers_receipt(self) -> None:
        written: list[LogRecord] = []

        def writer(record: LogRecord) -> bool:
            written.append(record)
            return True

        channel = self._channel(writer)
        channel.start()
        result = await channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message="ok")
        )
        assert result.delivered is True
        await asyncio.to_thread(channel.stop_listener, timeout=10)
        assert [r.message for r in written] == ["ok"]
