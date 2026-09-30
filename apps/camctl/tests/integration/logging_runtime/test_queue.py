"""L2 原子接纳与线程消费的组件集成测试。

真实同步/异步生产者竞争：队列不超容量，水位分区正确；采样只调
用一次随机源；满载时警告/错误处置与 FIFO 顺序验证。
"""

from __future__ import annotations

import asyncio
import random
import threading

import pytest

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord, LogSource

pytestmark = pytest.mark.asyncio


class CollectingWriter:
    def __init__(self) -> None:
        self.written: list[LogRecord] = []
        self.lock = threading.Lock()

    def __call__(self, record: LogRecord) -> bool:
        with self.lock:
            self.written.append(record)
        return True


class CountingRandom:
    def __init__(self, value: float = 0.99) -> None:
        self.value = value
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self.value


def _channel(writer, *, capacity=8, low=3, high=6, random_source=None) -> LogChannel:
    return LogChannel(
        capacity=capacity,
        low_watermark=low,
        high_watermark=high,
        level=LogLevel.DEBUG,
        sample_probability=0.5,
        writer=writer,
        random_source=random_source,
    )


class TestAtomicAdmission:
    async def test_concurrent_admission_is_atomic(self) -> None:
        writer = CollectingWriter()
        channel = _channel(writer, capacity=8, low=100, high=200)
        channel.start()
        results: list = []
        lock = threading.Lock()

        def sync_producer() -> None:
            for index in range(30):
                result = channel.slog(
                    LogRecord(level=LogLevel.ERROR, message=f"sync-{index}", source=LogSource.SYNC)
                )
                with lock:
                    results.append(result)

        async def async_producer() -> None:
            for index in range(30):
                result = await channel.alog(
                    LogRecord(level=LogLevel.ERROR, message=f"async-{index}")
                )
                with lock:
                    results.append(result)

        thread = threading.Thread(target=sync_producer)
        thread.start()
        await async_producer()
        thread.join(timeout=10)
        await asyncio.to_thread(channel.stop_listener, timeout=10)
        for result in results:
            assert result.queue_depth <= 8
        accepted = [r for r in results if r.decision.kind is AdmissionKind.ACCEPTED]
        dropped = [r for r in results if r.decision.kind is AdmissionKind.DROPPED]
        assert len(accepted) + len(dropped) == 60
        assert channel.counters.error_dropped == len(dropped)

    async def test_info_samples_once_per_record(self) -> None:
        writer = CollectingWriter()
        counting = CountingRandom(value=0.99)
        channel = _channel(writer, capacity=16, low=3, high=6, random_source=counting)
        # 不启动监听：先以警告记录把深度推入中间区 [3,6)。
        for index in range(3):
            await channel.alog(LogRecord(level=LogLevel.WARNING, message=f"warm-{index}"))
        # 中间区三条 INFO：每条恰一次采样。
        for _ in range(3):
            await channel.alog(LogRecord(level=LogLevel.INFO, message="x"))
        assert counting.calls == 3
        # 非采样区（低水位）不调用随机源。
        counting2 = CountingRandom()
        channel2 = _channel(writer, capacity=16, low=50, high=100, random_source=counting2)
        await channel2.alog(LogRecord(level=LogLevel.INFO, message="low"))
        assert counting2.calls == 0
        # 排空两个通道并确认 FIFO 保留已接纳记录。
        channel.start()
        channel2.start()
        await asyncio.to_thread(channel.stop_listener, timeout=10)
        await asyncio.to_thread(channel2.stop_listener, timeout=10)

    async def test_warning_and_error_survive_middle_zone(self) -> None:
        writer = CollectingWriter()
        counting = CountingRandom()
        channel = _channel(writer, capacity=16, low=1, high=4, random_source=counting)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.DEBUG, message="warm"))
        for index in range(4):
            result = await channel.alog(LogRecord(level=LogLevel.WARNING, message=f"w{index}"))
            assert result.decision.kind is AdmissionKind.ACCEPTED
            result = await channel.alog(LogRecord(level=LogLevel.ERROR, message=f"e{index}"))
            assert result.decision.kind is AdmissionKind.ACCEPTED
        await asyncio.to_thread(channel.stop_listener, timeout=10)
        levels = {record.level for record in writer.written}
        assert LogLevel.WARNING in levels and LogLevel.ERROR in levels

    async def test_accepted_fifo_order_preserved(self) -> None:
        writer = CollectingWriter()
        channel = _channel(writer, capacity=32, low=100, high=200)
        channel.start()
        for index in range(10):
            await channel.alog(LogRecord(level=LogLevel.ERROR, message=f"m{index}"))
            channel.slog(LogRecord(level=LogLevel.ERROR, message=f"s{index}", source=LogSource.SYNC))
        await asyncio.to_thread(channel.stop_listener, timeout=10)
        assert [r.message for r in writer.written] == [
            m for index in range(10) for m in (f"m{index}", f"s{index}")
        ]
