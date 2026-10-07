"""L6 有序关闭与一次丢弃摘要的单元测试。

关闭次序契约：丢弃计数在生产结束后即为最终值，摘要随停止标记
交给监听线程在消化完已接纳记录后写出（不经接纳队列）；级别过滤
与通道禁用跳过摘要；满载时标记等待入队，不丢弃已接纳记录；重复
关闭不重复汇总；监听线程未完成时如实报告。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


class ProbeWriter:
    """线程安全记录写入条目与线程；可阻塞消费者或注入失败。"""

    def __init__(self) -> None:
        self.entries: list[LogRecord] = []
        self.thread_ids: list[int] = []
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()
        self.fail_messages: set[str] = set()

    def __call__(self, record: LogRecord) -> bool:
        self.gate.wait()
        with self.lock:
            if record.message in self.fail_messages:
                raise OSError(f"注入写入失败: {record.message}")
            self.entries.append(record)
            self.thread_ids.append(threading.get_ident())
        return True

    @property
    def messages(self) -> list[str]:
        with self.lock:
            return [record.message for record in self.entries]


def _channel_and_runtime(
    writer: ProbeWriter,
    *,
    channel_level: LogLevel = LogLevel.DEBUG,
    runtime_level: LogLevel = LogLevel.DEBUG,
    identity: str = "pid=11",
    capacity: int = 8,
    low: int = 2,
    high: int = 4,
) -> tuple[LogChannel, LogRuntime]:
    channel = LogChannel(
        capacity=capacity, low_watermark=low, high_watermark=high,
        level=channel_level, writer=writer,
    )
    runtime = LogRuntime(
        channel=channel, level=runtime_level, identity=identity,
    )
    return channel, runtime


def _make_drops(channel: LogChannel, count: int = 1) -> None:
    """不启动监听线程制造水位丢弃（低级别记录在水位区被抑制）。"""
    for index in range(4 + count):
        channel.slog(LogRecord(level=LogLevel.DEBUG, message=f"drop-{index}"))
    assert channel.counters.total_dropped() >= count


def _summary_entries(writer: ProbeWriter) -> list[LogRecord]:
    return [
        record for record in writer.entries
        if record.level is LogLevel.WARNING and "日志关闭摘要" in record.message
    ]


class TestCloseSummary:
    async def test_close_writes_summary_once_via_listener_thread(self) -> None:
        """丢弃存在时摘要由监听线程写出一次，携带身份与计数。"""
        writer = ProbeWriter()
        channel, runtime = _channel_and_runtime(writer)
        _make_drops(channel, count=2)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.INFO, message="工作记录"))

        first = await close_logging(runtime)
        assert first.listener_stopped is True
        assert first.dropped_total >= 2
        assert first.summary_written is True
        summaries = _summary_entries(writer)
        assert len(summaries) == 1
        # 摘要携带本进程身份与各非零级别计数。
        assert "pid=11" in summaries[0].message
        assert f"debug={channel.counters.debug_dropped}" in summaries[0].message
        # 摘要由监听线程写出（与普通记录同一线程），不经接纳队列。
        work_ids = {
            tid for record, tid in zip(writer.entries, writer.thread_ids)
            if record.message == "工作记录"
        }
        assert work_ids and writer.thread_ids[-1] in work_ids
        assert channel.listener_alive is False

        # 重复关闭不重复汇总。
        again = await close_logging(runtime)
        assert again.summary_written is True
        assert len(_summary_entries(writer)) == 1

    async def test_zero_drops_skips_summary(self) -> None:
        """全部计数为零时不生成摘要。"""
        writer = ProbeWriter()
        channel, runtime = _channel_and_runtime(
            writer, capacity=8, low=1, high=2
        )
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.ERROR, message="重要"))
        result = await close_logging(runtime)
        assert result.dropped_total == 0
        assert result.summary_written is False
        assert _summary_entries(writer) == []

    async def test_level_error_filters_summary(self) -> None:
        """配置级别不允许 WARNING 时不写摘要。"""
        writer = ProbeWriter()
        channel, runtime = _channel_and_runtime(
            writer, runtime_level=LogLevel.ERROR
        )
        _make_drops(channel, count=1)
        channel.start()
        result = await close_logging(runtime)
        assert result.dropped_total >= 1
        assert result.summary_written is False
        assert _summary_entries(writer) == []

    async def test_disabled_channel_skips_summary(self) -> None:
        """文件通道已禁用时跳过摘要，不重新启用通道。"""
        writer = ProbeWriter()
        channel, runtime = _channel_and_runtime(writer)
        _make_drops(channel, count=1)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.INFO, message="正常记录"))
        writer.fail_messages.add("触发失败")
        await channel.alog(LogRecord(level=LogLevel.INFO, message="触发失败"))
        assert channel.pending_count() == 0

        result = await close_logging(runtime)
        assert result.dropped_total >= 1
        assert result.summary_written is False
        assert _summary_entries(writer) == []


class TestStopMarkerUnderPressure:
    async def test_full_queue_marker_waits_and_accepted_records_survive(
        self,
    ) -> None:
        """满载时停止标记等待入队；已接纳记录全部写出后写摘要。"""
        writer = ProbeWriter()
        writer.gate.clear()
        channel, runtime = _channel_and_runtime(
            writer, capacity=4, low=1, high=2
        )
        for index in range(4):
            channel.slog(LogRecord(level=LogLevel.WARNING, message=f"w{index}"))
        # 第 5 条满载丢弃；监听线程取走 w0 后补投一条回到满载，
        # 停止标记因此必须等待消费者腾出容量。
        channel.slog(LogRecord(level=LogLevel.WARNING, message="overflow"))
        assert channel.counters.warning_dropped == 1
        channel.start()
        channel.slog(LogRecord(level=LogLevel.WARNING, message="backfill"))
        assert channel.counters.warning_dropped == 1

        close_task = asyncio.create_task(
            close_logging(runtime, stop_timeout_s=10.0)
        )
        await asyncio.sleep(0.2)
        assert not close_task.done(), "标记应等待满载队列腾出容量"
        writer.gate.set()
        result = await close_task

        assert result.listener_stopped is True
        assert result.summary_written is True
        messages = writer.messages
        for index in range(4):
            assert f"w{index}" in messages
        assert "backfill" in messages
        assert channel.listener_alive is False
        assert len(_summary_entries(writer)) == 1

    async def test_marker_never_fits_reports_unstopped(self) -> None:
        """标记无法入队时如实报告未停止，不写摘要、不永久阻塞。"""
        writer = ProbeWriter()
        writer.gate.clear()
        channel, runtime = _channel_and_runtime(
            writer, capacity=4, low=1, high=2
        )
        for index in range(4):
            channel.slog(LogRecord(level=LogLevel.WARNING, message=f"w{index}"))
        channel.slog(LogRecord(level=LogLevel.WARNING, message="overflow"))
        channel.start()
        channel.slog(LogRecord(level=LogLevel.WARNING, message="backfill"))

        result = await close_logging(runtime, stop_timeout_s=0.5)
        assert result.listener_stopped is False
        assert result.summary_written is False
        assert _summary_entries(writer) == []

        # 收尾：放行消费者并关闭队列，监听线程经关闭结果退出。
        writer.gate.set()
        channel.async_close()
        await asyncio.sleep(0.2)
        assert channel.listener_alive is False
