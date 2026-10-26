"""日志接纳、取消交接与专用线程消费。"""

from __future__ import annotations

import asyncio
import queue
import random
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable

import janus

from camctl.logging_runtime.admission import (
    AdmissionDecision, AdmissionInput, AdmissionKind, DropCounters, LogSource,
    decide_admission, record_drop,
)
from camctl.logging_runtime.models import LogLevel

__all__ = ["AdmissionResult", "ChannelError", "LogChannel", "LogRecord",
           "PendingLog", "admission_result_for"]


@dataclass(frozen=True)
class LogRecord:
    level: LogLevel
    message: str
    source: LogSource = LogSource.COROUTINE
    fallback: Callable[[str], bool | None] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class AdmissionResult:
    decision: AdmissionDecision
    queue_depth: int
    delivered: bool | None = None
    fallback_written: bool = False


def admission_result_for(error: BaseException) -> AdmissionResult | None:
    """读取原控制异常携带的实际接纳事实，不推测是否已经入队。"""
    result = getattr(error, "_camctl_log_admission", None)
    return result if isinstance(result, AdmissionResult) else None


def _attach_admission(error: BaseException, result: AdmissionResult) -> None:
    error._camctl_log_admission = result


class ChannelError(Exception):
    """日志文件通道失效；不再把记录标为写入成功。"""


@dataclass(frozen=True)
class _StopMarker:
    summary: LogRecord | None


@dataclass
class PendingLog:
    record: LogRecord
    receipt: asyncio.Future
    enqueued: bool = False
    entry: _QueuedRecord | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _WriteReceipt:
    written: bool
    fallback_written: bool = False


@dataclass
class _QueuedRecord:
    record: LogRecord
    pending_id: int | None = None
    sample_value: float | None = None
    enqueued: bool = False
    fallback_written: bool | None = None
    fallback_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class _Rejected(Exception):
    def __init__(self, decision: AdmissionDecision):
        self.decision = decision


class _AdmissionQueue(janus.Queue):
    """在 Janus 2.0 的持锁插入点裁决水位，拒绝不增加任务计数。"""

    def __init__(self, *, capacity, low, high, level, probability, random_source):
        super().__init__(maxsize=capacity)
        self.low, self.high = low, high
        self.level, self.probability = level, probability
        self.random_source = random_source

    def _put(self, item):
        if isinstance(item, _QueuedRecord):
            record = item.record
            depth = self._qsize()
            if (record.level is LogLevel.INFO and self.low <= depth < self.high
                    and item.sample_value is None):
                item.sample_value = (self.random_source.random() if self.random_source
                                     else random.random())
            decision = decide_admission(AdmissionInput(
                level=record.level, source=record.source, queue_depth=depth,
                low_watermark=self.low, high_watermark=self.high,
                queue_capacity=self.maxsize, sample_value=item.sample_value or 0.0,
                sample_probability_hint=self.probability,
                filtered_out=_level_rank(record.level) < _level_rank(self.level),
            ))
            if decision.kind is not AdmissionKind.ACCEPTED:
                raise _Rejected(decision)
            super()._put(item)
            item.enqueued = True
        else:
            super()._put(item)


class LogChannel:
    """同一 Janus 队列连接业务协程、同步来源和专用日志线程。"""

    def __init__(self, *, capacity: int, low_watermark: int, high_watermark: int,
                 level: LogLevel = LogLevel.WARNING, sample_probability: float = 0.1,
                 writer: Callable[[LogRecord], bool], random_source=None,
                 fallback: Callable[[str], bool | None] | None = None) -> None:
        if not 0 < low_watermark < high_watermark < capacity:
            raise ValueError("日志水位必须满足 0 < low < high < capacity")
        self._queue = _AdmissionQueue(
            capacity=capacity, low=low_watermark, high=high_watermark,
            level=level, probability=sample_probability, random_source=random_source)
        self._level = level
        self._writer = writer
        self._fallback = fallback or _write_stderr
        self._fallback_state = threading.local()
        self._fallback_failed = False
        self._lock = threading.Lock()
        self._counters = DropCounters()
        self._channel_failed = False
        self._closing = False
        self._producers_finished = False
        self._producers = 0
        self._producer_waiters: list[asyncio.Future] = []
        self._pending_lock = threading.Lock()
        self._pending: dict[int, PendingLog] = {}
        self._next_pending_id = 0
        self._listener = threading.Thread(target=self._consume,
                                          name="camctl-log-listener", daemon=True)
        self._listener_started = False
        self._summary_written = False
        self._stop_marker_done = False

    def _result(self, kind, record):
        return AdmissionResult(AdmissionDecision(kind, record.level), self._queue.sync_q.qsize())

    def _filtered(self, record):
        return _level_rank(record.level) < _level_rank(self._level)

    def slog(self, record: LogRecord) -> AdmissionResult:
        """同步来源立即接纳；ERROR 无位置或通道失效时完整转交 stderr。"""
        with self._lock:
            finished = self._producers_finished
            if not finished:
                self._producers += 1
        try:
            result = (self._result(AdmissionKind.CHANNEL_FAILED, record) if finished
                      else self._admit(_QueuedRecord(record)))
            if (record.level is LogLevel.ERROR and not self._filtered(record)
                    and result.decision.kind not in (AdmissionKind.ACCEPTED, AdmissionKind.FILTERED_OUT)):
                return AdmissionResult(AdmissionDecision(AdmissionKind.FALLBACK_STDERR, record.level),
                                       result.queue_depth, fallback_written=self._safe_fallback(record))
            return result
        finally:
            if not finished:
                self._finish_producer()

    def _finish_producer(self):
        with self._lock:
            self._producers -= 1
            waiters = self._producer_waiters if self._producers == 0 else []
            if waiters:
                self._producer_waiters = []
        for waiter in waiters:
            _settle_receipt(waiter, written=True)

    async def alog(self, record: LogRecord) -> AdmissionResult:
        """自身 ERROR 在当前协程异步等待位置，成功仅表示入队。"""
        return await self._alog(_QueuedRecord(record))

    async def _alog(self, entry: _QueuedRecord) -> AdmissionResult:
        record = entry.record
        with self._lock:
            if self._producers_finished:
                return self._result(AdmissionKind.CHANNEL_FAILED, record)
            self._producers += 1
        control = None
        try:
            result = self._admit(entry)
            if result.decision.kind is AdmissionKind.WAITING:
                while True:
                    try:
                        # Janus 取消发生在等待处；实际插入后没有新的 await。
                        # 同一 entry 的实际插入事实同时用于取消竞争裁决。
                        await self._queue.async_q.put(entry)
                        result = self._result(AdmissionKind.ACCEPTED, record)
                        break
                    except asyncio.CancelledError as error:
                        if control is None:
                            control = error
                        if entry.enqueued:
                            result = self._result(AdmissionKind.ACCEPTED, record)
                            break
                    except janus.AsyncQueueShutDown:
                        result = self._result(AdmissionKind.CHANNEL_FAILED, record)
                        break
            if control is not None:
                _attach_admission(control, result)
                raise control.with_traceback(control.__traceback__)
            return result
        finally:
            self._finish_producer()

    def _admit(self, entry):
        record = entry.record
        if self._filtered(record):
            return self._result(AdmissionKind.FILTERED_OUT, record)
        try:
            self._queue.sync_q.put_nowait(entry)
            result = self._result(AdmissionKind.ACCEPTED, record)
        except _Rejected as rejected:
            result = AdmissionResult(rejected.decision, self._queue.sync_q.qsize())
        except queue.Full:
            kind = (AdmissionKind.WAITING if record.source is LogSource.COROUTINE
                    else AdmissionKind.FALLBACK_STDERR) if record.level is LogLevel.ERROR else AdmissionKind.DROPPED
            result = self._result(kind, record)
        except janus.SyncQueueShutDown:
            result = self._result(AdmissionKind.CHANNEL_FAILED, record)
        if result.decision.kind in (AdmissionKind.DROPPED, AdmissionKind.SAMPLED_OUT):
            with self._lock:
                self._counters = record_drop(self._counters, result.decision)
        return result

    async def finish_producers(self) -> None:
        """停止新增生产，等待已经开始的协程交付唯一入队结果。"""
        with self._lock:
            self._producers_finished = True
            if not self._producers:
                return
            waiter = asyncio.get_running_loop().create_future()
            self._producer_waiters.append(waiter)
        await waiter

    async def join(self) -> None:
        """等待已接纳记录的实际消费记账。"""
        await self._queue.async_q.join()

    async def deliver_important(self, record: LogRecord) -> AdmissionResult:
        """特殊诊断等待实际写盘；每次调用独占自己的交付凭据。"""
        pending = PendingLog(record, asyncio.get_running_loop().create_future())
        # 取消后的凭据仍由消费侧结算；读取异常避免无人等待时产生警告。
        pending.receipt.add_done_callback(_observe_receipt)
        with self._pending_lock:
            self._next_pending_id += 1
            pending_id = self._next_pending_id
            entry = _QueuedRecord(record, pending_id)
            pending.entry = entry
            self._pending[pending_id] = pending
        admission = None
        try:
            try:
                admission = await self._alog(entry)
            except BaseException as error:
                admission = admission_result_for(error)
                raise
            if admission.decision.kind is not AdmissionKind.ACCEPTED:
                return admission
            pending.enqueued = True
            try:
                delivered = await asyncio.shield(pending.receipt)
            except BaseException as error:
                if admission_result_for(error) is None:
                    _attach_admission(error, admission)
                raise
            return AdmissionResult(admission.decision, admission.queue_depth,
                                   delivered.written, delivered.fallback_written)
        finally:
            if admission is None or admission.decision.kind is not AdmissionKind.ACCEPTED:
                with self._pending_lock:
                    self._pending.pop(pending_id, None)
                if not pending.receipt.done():
                    pending.receipt.cancel()

    def disable_channel(self, error: ChannelError) -> None:
        """非立即关闭保留消费记账，同时唤醒所有尚未入队的生产者。"""
        self._channel_failed = True
        self._queue.shutdown(immediate=False)
        with self._pending_lock:
            pending_records = list(self._pending.values())
            self._pending.clear()
        for pending in pending_records:
            fallback_written = (self._fallback_entry(pending.entry)
                                if pending.entry.enqueued else False)
            failure = ChannelError(str(error))
            failure.__cause__ = error
            _attach_admission(failure, AdmissionResult(
                AdmissionDecision(AdmissionKind.ACCEPTED if pending.entry.enqueued
                                  else AdmissionKind.CHANNEL_FAILED, pending.record.level),
                self._queue.sync_q.qsize(), delivered=False, fallback_written=fallback_written))
            _settle_receipt(pending.receipt, error=failure)

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    def start(self) -> None:
        if not self._listener_started:
            self._listener_started = True
            self._listener.start()

    def _consume(self) -> None:
        while True:
            try:
                entry = self._queue.sync_q.get()
            except janus.SyncQueueShutDown:
                self._stop_marker_done = True
                break
            try:
                if isinstance(entry, _StopMarker):
                    if entry.summary is not None and not self._channel_failed:
                        try:
                            self._summary_written = bool(self._writer(entry.summary))
                        except Exception:
                            self._summary_written = False
                    self._stop_marker_done = True
                    break
                record = entry.record
                written = False
                if not self._channel_failed:
                    try:
                        written = self._writer(record)
                    except Exception as error:
                        self.disable_channel(ChannelError(f"日志写入失败: {error}"))
                fallback_written = self._fallback_entry(entry) if not written else False
                self._settle_pending(entry, written, fallback_written)
            finally:
                self._queue.sync_q.task_done()

    def _settle_pending(self, entry, written, fallback_written):
        with self._pending_lock:
            pending = self._pending.pop(entry.pending_id, None)
        if pending is not None:
            _settle_receipt(pending.receipt, written=_WriteReceipt(written, fallback_written))

    def _fallback_entry(self, entry):
        if entry is None or entry.record.level is not LogLevel.ERROR:
            return False
        with entry.fallback_lock:
            if entry.fallback_written is None:
                entry.fallback_written = self._safe_fallback(entry.record)
            return entry.fallback_written

    def _safe_fallback(self, record):
        if self._fallback_failed or getattr(self._fallback_state, "active", False):
            return False
        self._fallback_state.active = True
        try:
            written = (record.fallback or self._fallback)(record.message) is not False
            if not written:
                self._fallback_failed = True
            return written
        except Exception:
            self._fallback_failed = True
            return False
        finally:
            self._fallback_state.active = False

    @property
    def counters(self) -> DropCounters:
        with self._lock:
            return self._counters

    def stop_listener(self, timeout=None, *, summary=None) -> bool:
        """在生产结束后放入停止标记；线程 join 由调用方移出事件循环。"""
        if not self._closing:
            self._closing = True
            if not self._listener_started:
                self._stop_marker_done = True
                return True
            if not self._channel_failed:
                try:
                    self._queue.sync_q.put(_StopMarker(summary), timeout=timeout)
                except queue.Full:
                    self._closing = False
                    return False
                except janus.SyncQueueShutDown:
                    pass
        if self._listener_started:
            self._listener.join()
        return self._stop_marker_done

    @property
    def summary_written(self):
        return self._summary_written

    @property
    def listener_alive(self):
        return self._listener.is_alive()

    @property
    def disabled(self):
        return self._channel_failed

    def async_close(self):
        self._queue.close()

    async def await_closed(self):
        await self._queue.wait_closed()


def _level_rank(level):
    return {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2, LogLevel.ERROR: 3}[level]


def _observe_receipt(receipt):
    if not receipt.cancelled():
        receipt.exception()


def _write_stderr(message):
    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def _settle_receipt(receipt, *, written=None, error=None):
    def settle():
        if not receipt.done():
            if error is None:
                receipt.set_result(written)
            else:
                receipt.set_exception(error)
    receipt.get_loop().call_soon_threadsafe(settle)
