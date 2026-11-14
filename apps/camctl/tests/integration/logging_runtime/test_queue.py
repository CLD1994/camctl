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


async def _close_channel(channel):
    try:
        channel.start()
        stopped = await asyncio.to_thread(channel.stop_listener, timeout=10)
        assert stopped
    finally:
        channel.async_close()
        await channel.await_closed()


class TestAtomicAdmission:
    async def test_concurrent_admission_is_atomic(self) -> None:
        writer = CollectingWriter()
        channel = _channel(writer, capacity=8, low=3, high=6)
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
        started = False
        try:
            channel.start()
            thread.start()
            started = True
            await async_producer()
        finally:
            try:
                if started:
                    await asyncio.to_thread(thread.join, 10)
                    assert not thread.is_alive()
            finally:
                await _close_channel(channel)
        for result in results:
            assert result.queue_depth <= 8
        accepted = [r for r in results if r.decision.kind is AdmissionKind.ACCEPTED]
        fallback = [r for r in results if r.decision.kind is AdmissionKind.FALLBACK_STDERR]
        assert len(accepted) + len(fallback) == 60
        assert sum(record.message.startswith("async-") for record in writer.written) == 30
        assert channel.counters.error_dropped == 0

    async def test_info_samples_once_per_record(self) -> None:
        writer = CollectingWriter()
        counting = CountingRandom(value=0.99)
        channel = _channel(writer, capacity=16, low=3, high=6, random_source=counting)
        channel2 = None
        try:
            # 不启动监听：先以警告记录把深度推入中间区 [3,6)。
            for index in range(3):
                await channel.alog(LogRecord(level=LogLevel.WARNING, message=f"warm-{index}"))
            for _ in range(3):
                await channel.alog(LogRecord(level=LogLevel.INFO, message="x"))
            assert counting.calls == 3
            counting2 = CountingRandom()
            channel2 = _channel(writer, capacity=16, low=6, high=12, random_source=counting2)
            await channel2.alog(LogRecord(level=LogLevel.INFO, message="low"))
            assert counting2.calls == 0
        finally:
            try:
                await _close_channel(channel)
            finally:
                if channel2 is not None:
                    await _close_channel(channel2)

    async def test_warning_and_error_survive_middle_zone(self) -> None:
        writer = CollectingWriter()
        counting = CountingRandom()
        channel = _channel(writer, capacity=16, low=1, high=10, random_source=counting)
        try:
            warm = await channel.alog(LogRecord(level=LogLevel.DEBUG, message="warm"))
            assert warm.decision.kind is AdmissionKind.ACCEPTED
            assert warm.queue_depth == 1
            for index in range(4):
                for level, message in ((LogLevel.WARNING, f"w{index}"),
                                       (LogLevel.ERROR, f"e{index}")):
                    result = await channel.alog(LogRecord(level=level, message=message))
                    assert result.decision.kind is AdmissionKind.ACCEPTED
                    assert result.queue_depth == 2 + index * 2 + (level is LogLevel.ERROR)
                    assert 1 <= result.queue_depth - 1 < 10
        finally:
            await _close_channel(channel)
        assert [record.message for record in writer.written] == [
            "warm", *[message for index in range(4) for message in (f"w{index}", f"e{index}")]]

    async def test_accepted_fifo_order_preserved(self) -> None:
        writer = CollectingWriter()
        channel = _channel(writer, capacity=32, low=20, high=24)
        try:
            channel.start()
            for index in range(10):
                await channel.alog(LogRecord(level=LogLevel.ERROR, message=f"m{index}"))
                channel.slog(LogRecord(level=LogLevel.ERROR, message=f"s{index}", source=LogSource.SYNC))
        finally:
            await _close_channel(channel)
        assert [r.message for r in writer.written] == [
            m for index in range(10) for m in (f"m{index}", f"s{index}")
        ]
