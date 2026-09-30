"""日志接纳服务：原子水位接纳、监听线程消费与通道禁用。

同步与异步入口共享同一队列和接纳策略；接纳判定与入队在同一
线程锁内完成（无绕过策略的直通 put）。重要记录只保留一份，等
待者取消时由责任拥有者接手；通道禁用使等待者以失败凭据退出。
"""

from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import janus

from camctl.logging_runtime.admission import (
    AdmissionDecision,
    AdmissionInput,
    AdmissionKind,
    DropCounters,
    LogSource,
    decide_admission,
    record_drop,
)
from camctl.logging_runtime.models import LogLevel

__all__ = [
    "AdmissionResult",
    "ChannelError",
    "LogChannel",
    "LogRecord",
    "PendingLog",
]


@dataclass(frozen=True)
class LogRecord:
    level: LogLevel
    message: str
    source: LogSource = LogSource.COROUTINE


@dataclass(frozen=True)
class AdmissionResult:
    decision: AdmissionDecision
    queue_depth: int
    delivered: bool | None = None  # 重要记录等待实际写结果时使用


class ChannelError(Exception):
    """日志文件通道失效；不再把记录标为写入成功。"""


@dataclass
class PendingLog:
    """一条已接纳、等待实际写入凭据的重要记录（仅一份）。"""

    record: LogRecord
    receipt: asyncio.Future
    enqueued: bool = False


class LogChannel:
    """单进程日志通道：水位接纳 + 专用监听线程顺序消费。"""

    def __init__(
        self,
        *,
        capacity: int,
        low_watermark: int,
        high_watermark: int,
        level: LogLevel = LogLevel.WARNING,
        sample_probability: float = 0.1,
        writer: Callable[[LogRecord], bool],
        random_source: random.Random | None = None,
    ) -> None:
        self._queue: janus.Queue[LogRecord] = janus.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._counters = DropCounters()
        self._channel_failed = False
        self._closing = False
        self._level = level
        self._low = low_watermark
        self._high = high_watermark
        self._probability = sample_probability
        self._writer = writer
        self._random = random_source
        self._pending_lock = threading.Lock()
        self._pending: dict[int, PendingLog] = {}
        self._next_pending_id = 0
        self._listener = threading.Thread(
            target=self._consume, name="camctl-log-listener", daemon=True
        )
        self._listener_started = False

    # -- 接纳入口 -----------------------------------------------------

    def slog(self, record: LogRecord) -> AdmissionResult:
        """同步入口：接纳判定与入队同锁完成。"""
        return self._admit(record)

    async def alog(self, record: LogRecord) -> AdmissionResult:
        """异步入口：与同步入口共享队列和策略。"""
        return await asyncio.to_thread(self._admit, record)

    def _admit(self, record: LogRecord) -> AdmissionResult:
        random_calls = 0
        with self._lock:
            depth = self._queue.sync_q.qsize()
            filtered = self._level.value != "warning" and _level_rank(record.level) < _level_rank(self._level)
            sample_value = 0.0
            if (
                not filtered
                and record.level is LogLevel.INFO
                and self._low <= depth < self._high
            ):
                sample_value = self._random.random() if self._random else random.random()
                random_calls = 1
            decision = decide_admission(
                AdmissionInput(
                    level=record.level,
                    source=record.source,
                    queue_depth=depth,
                    low_watermark=self._low,
                    high_watermark=self._high,
                    queue_capacity=self._queue.sync_q.maxsize,
                    sample_value=sample_value,
                    sample_probability_hint=self._probability,
                    filtered_out=filtered,
                    channel_failed=self._channel_failed,
                )
            )
            if decision.kind is AdmissionKind.ACCEPTED:
                try:
                    self._queue.sync_q.put_nowait(record)
                except Exception:
                    # 满载：按水位丢弃自身普通记录，警告/错误由满载策略处置。
                    decision = AdmissionDecision(AdmissionKind.DROPPED, record.level)
            if decision.kind in (AdmissionKind.DROPPED, AdmissionKind.SAMPLED_OUT):
                self._counters = record_drop(self._counters, decision)
            return AdmissionResult(
                decision=decision,
                queue_depth=self._queue.sync_q.qsize(),
            )

    # -- 重要记录与通道 -----------------------------------------------

    async def deliver_important(self, record: LogRecord) -> AdmissionResult:
        """接纳一条重要记录并等待实际写入凭据。

        等待者取消不取消写入责任：凭据交由接手方消费；通道禁用后
        以失败凭据退出，不把未写记录标为成功。
        """
        loop = asyncio.get_running_loop()
        pending = PendingLog(record=record, receipt=loop.create_future())
        # 先登记等待凭据再入队：监听线程可能在登记前完成消费。
        with self._pending_lock:
            if self._channel_failed:
                pending.receipt.set_exception(ChannelError("日志通道已禁用"))
            else:
                self._next_pending_id += 1
                self._pending[self._next_pending_id] = pending
        admission = await self.alog(record)
        if admission.decision.kind is not AdmissionKind.ACCEPTED:
            with self._pending_lock:
                self._pending.pop(self._next_pending_id, None)
            return admission
        delivered = await asyncio.shield(pending.receipt)
        return AdmissionResult(
            decision=admission.decision,
            queue_depth=admission.queue_depth,
            delivered=delivered,
        )

    def disable_channel(self, error: ChannelError) -> None:
        """禁用文件通道：等待中的重要记录以失败凭据退出。"""
        with self._pending_lock:
            self._channel_failed = True
            for pending in self._pending.values():
                if not pending.receipt.done():
                    pending.receipt.set_exception(error)

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    # -- 监听线程 -----------------------------------------------------

    def start(self) -> None:
        if not self._listener_started:
            self._listener_started = True
            self._listener.start()

    def _consume(self) -> None:
        while True:
            record = self._queue.sync_q.get()
            if record is None:
                break
            if self._channel_failed:
                self._settle_pending(record, False, ChannelError("日志通道已禁用"))
                continue
            try:
                written = self._writer(record)
            except Exception as error:
                self.disable_channel(ChannelError(f"日志写入失败: {error}"))
                written = False
            self._settle_pending(record, written, None)

    def _settle_pending(self, record: LogRecord, written: bool, error: Exception | None) -> None:
        with self._pending_lock:
            matches = [
                (key, pending)
                for key, pending in self._pending.items()
                if pending.record is record or pending.record == record
            ]
            for key, pending in matches:
                if not pending.receipt.done():
                    if error is not None:
                        pending.receipt.set_exception(error)
                    else:
                        pending.receipt.set_result(written)
                del self._pending[key]

    # -- 关闭 ---------------------------------------------------------

    @property
    def counters(self) -> DropCounters:
        return self._counters

    def stop_listener(self, timeout: float | None = None) -> None:
        """停止接纳并结束监听线程；已接纳记录消费完为止。"""
        self._closing = True
        try:
            self._queue.sync_q.put_nowait(None)
        except Exception:
            pass
        if self._listener_started:
            self._listener.join(timeout=timeout)

    def write_direct(self, record: LogRecord) -> bool:
        """由日志线程直接写（关闭摘要），不经队列。"""
        if self._channel_failed:
            return False
        try:
            return bool(self._writer(record))
        except Exception:
            return False

    def async_close(self) -> None:
        self._queue.close()

    def await_closed(self) -> None:
        self._queue.wait_closed()


def _level_rank(level: LogLevel) -> int:
    return {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2, LogLevel.ERROR: 3}[level]
