"""日志接纳、取消交接与专用线程消费。"""

from __future__ import annotations

import asyncio
import queue
import random
import sys
import threading
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from os import getpid
from typing import Callable

import janus

from camctl.logging_runtime.admission import (
    AdmissionDecision, AdmissionInput, AdmissionKind, DropCounters, LogSource,
    decide_admission, record_drop,
)
from camctl.logging_runtime.models import LogLevel, LogWriteResult
from camctl.logging_runtime.formatting import encode_record, utc_now
from camctl.logging_runtime.receipt import LogReceipt

__all__ = ["AdmissionResult", "ChannelError", "LogChannel", "LogRecord",
           "LogReceipt", "PendingLog", "admission_result_for"]


@dataclass(frozen=True)
class LogRecord:
    level: LogLevel
    message: str
    source: LogSource = LogSource.COROUTINE
    fallback: Callable[[str], bool | None] | None = field(default=None, compare=False, repr=False)
    structured: bool = field(default=False, compare=False, repr=False)
    _logged_at_utc: str = field(default_factory=utc_now, init=False, compare=False, repr=False)
    _pid: int = field(default_factory=getpid, init=False, compare=False, repr=False)

    def rendered_message(self) -> str:
        if self.structured:
            return self.message
        try:
            return encode_record(self.level, {"stage": "runtime", "message": self.message},
                                 logged_at_utc=self._logged_at_utc, pid=self._pid)
        except Exception:
            return self.message


@dataclass(frozen=True)
class AdmissionResult:
    decision: AdmissionDecision
    queue_depth: int | None
    delivered: bool | None = None
    fallback_written: bool = False
    appended: bool = False
    durable: bool = False


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


@dataclass
class _QueuedRecord:
    record: LogRecord
    receipt: LogReceipt = field(default_factory=LogReceipt)
    pending_id: int | None = None
    sample_value: float | None = None
    enqueued: bool = False
    fallback_written: bool | None = None
    fallback_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    admission: AdmissionResult | None = None
    failure: ChannelError | None = None
    failure_deferred: bool = False


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
        self.overflow: deque[_QueuedRecord] = deque()
        self.admission_notifications: deque[_QueuedRecord] = deque()
        self.inflight: _QueuedRecord | None = None
        self.failed_pending: deque[_QueuedRecord] = deque()
        self.failure_active = False
        self.failure_draining = False
        self.failure_done: Future | None = None

    def admit(self, item, *, allow_overflow=True):
        """登记与取出补位共用 Janus 接纳锁，后来者不能抢占空位。"""
        with self._sync_mutex:
            record = item.record
            if _level_rank(record.level) < _level_rank(self.level):
                kind = AdmissionKind.FILTERED_OUT
            elif self._is_shutdown:
                kind = AdmissionKind.CHANNEL_FAILED
                if (allow_overflow and self.failure_active and record.level is LogLevel.ERROR
                        and record.source is LogSource.COROUTINE):
                    item.failure_deferred = True
                    self.failed_pending.append(item)
            elif self._qsize() >= self.maxsize:
                if record.level is LogLevel.ERROR:
                    kind = (AdmissionKind.WAITING if (allow_overflow and record.source is LogSource.COROUTINE)
                            else AdmissionKind.FALLBACK_STDERR)
                    if kind is AdmissionKind.WAITING:
                        self.overflow.append(item)
                else:
                    kind = AdmissionKind.DROPPED
            else:
                try:
                    self._put_internal(item)
                except _Rejected as rejected:
                    kind = rejected.decision.kind
                else:
                    if self._sync_not_empty_waiting:
                        self._sync_not_empty.notify()
                    if self._async_not_empty_waiting:
                        self._notify_async(self._async_not_empty.notify)
                    return item.admission
            result = AdmissionResult(AdmissionDecision(kind, record.level), self._qsize())
            if kind is not AdmissionKind.WAITING:
                item.admission = result
            return result

    def _get(self):
        item = super()._get()
        if isinstance(item, _QueuedRecord) and not self._is_shutdown:
            self.inflight = item
        if self.overflow and not self._is_shutdown:
            promoted = self.overflow.popleft()
            self._put_internal(promoted)
            self.admission_notifications.append(promoted)
        return item

    def publish_admissions(self):
        with self._sync_mutex:
            entries = list(self.admission_notifications)
            self.admission_notifications.clear()
        for entry in entries:
            entry.receipt.complete_admission(entry.admission)

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
            item.admission = AdmissionResult(
                AdmissionDecision(AdmissionKind.ACCEPTED, record.level), self._qsize())
        else:
            super()._put(item)


class LogChannel:
    """同一 Janus 队列连接业务协程、同步来源和专用日志线程。"""

    def __init__(self, *, capacity: int, low_watermark: int, high_watermark: int,
                 level: LogLevel = LogLevel.WARNING, sample_probability: float = 0.1,
                 writer: Callable[[LogRecord], bool | LogWriteResult], random_source=None,
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
        self._channel_error: ChannelError | None = None
        self._closing = False
        self._producers_finished = False
        self._producers = 0
        self._producer_waiters: list[asyncio.Future] = []
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _QueuedRecord] = {}
        self._next_pending_id = 0
        self._listener = threading.Thread(target=self._consume,
                                          name="camctl-log-listener", daemon=True)
        self._listener_started = False
        self._summary_written = False
        self._stop_marker_done = False
        self._consumer_control: BaseException | None = None

    def _result(self, kind, record):
        return AdmissionResult(AdmissionDecision(kind, record.level), self._queue.sync_q.qsize())

    def _filtered(self, record):
        return _level_rank(record.level) < _level_rank(self._level)

    def _begin_producer(self):
        with self._lock:
            if self._producers_finished:
                return False
            self._producers += 1
            return True

    def submit(self, record: LogRecord, *, receipt: LogReceipt | None = None) -> LogReceipt:
        """调用时登记原记录；忽略回执也由原日志线程顺序处理。"""
        receipt = receipt if receipt is not None else LogReceipt()
        if not receipt._claim_submission():
            return receipt
        entry = _QueuedRecord(record, receipt=receipt)
        started = self._begin_producer()
        try:
            self._submit_entry(entry, finished=not started)
        finally:
            if started:
                self._finish_producer()
        return receipt

    def _submit_entry(self, entry, *, finished=False, allow_overflow=True):
        record = entry.record
        result = (self._result(AdmissionKind.CHANNEL_FAILED, record) if finished
                  else self._queue.admit(entry, allow_overflow=allow_overflow))
        if result.decision.kind is AdmissionKind.WAITING:
            return
        entry.admission = result
        if result.decision.kind in (AdmissionKind.DROPPED, AdmissionKind.SAMPLED_OUT):
            with self._lock:
                self._counters = record_drop(self._counters, result.decision)
        entry.receipt.complete_admission(result)
        if entry.failure_deferred:
            entry.failure = self._channel_error
            return
        if result.decision.kind is not AdmissionKind.ACCEPTED:
            fallback_written = False
            try:
                if result.decision.kind is not AdmissionKind.FILTERED_OUT:
                    fallback_written = self._fallback_entry(entry)
            except BaseException as error:
                _attach_admission(error, AdmissionResult(
                    result.decision, result.queue_depth, delivered=False))
                raise
            finally:
                entry.receipt.complete_write(AdmissionResult(
                    result.decision, result.queue_depth, delivered=False,
                    fallback_written=fallback_written))

    def slog(self, record: LogRecord) -> AdmissionResult:
        """同步来源立即接纳；自身 ERROR 仍由协程入口承担等待责任。"""
        # 旧同步接口不能持有尚未入队的重要记录，保持立即 stderr 语义。
        entry = _QueuedRecord(record)
        started = self._begin_producer()
        try:
            self._submit_entry(entry, finished=not started, allow_overflow=False)
            result = entry.receipt.admission_result
            if (record.level is LogLevel.ERROR and not self._filtered(record)
                    and result.decision.kind not in (AdmissionKind.ACCEPTED, AdmissionKind.FILTERED_OUT)):
                return AdmissionResult(AdmissionDecision(AdmissionKind.FALLBACK_STDERR, record.level),
                                       result.queue_depth,
                                       fallback_written=entry.receipt.write_result.fallback_written)
            return result
        finally:
            if started:
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
        """旧接口仅等待实际入队；取消保护保留到入队或通道失效。"""
        return await self._alog(_QueuedRecord(record))

    async def _alog(self, entry: _QueuedRecord) -> AdmissionResult:
        started = self._begin_producer()
        control = None
        try:
            self._submit_entry(entry, finished=not started)
            while True:
                try:
                    result = await entry.receipt.wait_admission()
                    break
                except asyncio.CancelledError as error:
                    if control is None:
                        control = error
            if control is not None:
                _attach_admission(control, result)
                raise control.with_traceback(control.__traceback__)
            return result
        finally:
            if started:
                self._finish_producer()

    async def finish_producers(self) -> None:
        """封闭新提交，原 overflow 仍由日志线程补位并消费。"""
        with self._lock:
            self._producers_finished = True
            if not self._producers:
                return
            waiter = asyncio.get_running_loop().create_future()
            self._producer_waiters.append(waiter)
        await waiter

    async def join(self) -> None:
        """等待原队列及满载重要日志全部完成实际消费记账。"""
        await self._queue.async_q.join()
        with self._queue._sync_mutex:
            failure_done = self._queue.failure_done
        if failure_done is not None:
            await asyncio.shield(asyncio.wrap_future(failure_done))

    async def deliver_important(self, record: LogRecord) -> AdmissionResult:
        """保留特殊交付的取消、异常和独立实际写入凭据。"""
        entry = _QueuedRecord(record)
        with self._pending_lock:
            self._next_pending_id += 1
            pending_id = self._next_pending_id
            entry.pending_id = pending_id
            self._pending[pending_id] = entry
        admission = None
        try:
            try:
                admission = await self._alog(entry)
            except BaseException as error:
                admission = admission_result_for(error)
                raise
            if admission.decision.kind is not AdmissionKind.ACCEPTED:
                return await entry.receipt
            try:
                delivered = await entry.receipt
            except BaseException as error:
                if admission_result_for(error) is None:
                    _attach_admission(error, admission)
                raise
            if entry.failure is not None:
                failure = ChannelError(str(entry.failure))
                failure.__cause__ = entry.failure
                _attach_admission(failure, delivered)
                raise failure
            return delivered
        finally:
            if admission is None or admission.decision.kind is not AdmissionKind.ACCEPTED:
                with self._pending_lock:
                    self._pending.pop(pending_id, None)

    def disable_channel(self, error: ChannelError) -> None:
        """结算未开始写出的责任；正在写入项保留 writer 的实际事实。"""
        with self._queue._sync_mutex:
            self._channel_failed = True
            if self._channel_error is None:
                self._channel_error = error
            self._queue._is_shutdown = True
            entries = [entry for entry in self._queue._queue
                       if isinstance(entry, _QueuedRecord)]
            entries.extend(self._queue.overflow)
            self._queue.overflow.clear()
            entries = [entry for entry in entries if entry.receipt.write_result is None]
            inflight = self._queue.inflight is not None
            if (entries or inflight) and not self._queue.failure_active:
                self._queue.failure_active = True
                self._queue.failure_done = Future()
            for entry in entries:
                if entry.failure is None:
                    entry.failure = self._channel_error
                if not entry.enqueued:
                    entry.admission = AdmissionResult(
                        AdmissionDecision(AdmissionKind.CHANNEL_FAILED, entry.record.level),
                        self._queue._qsize())
                if not entry.failure_deferred:
                    entry.failure_deferred = True
                    self._queue.failed_pending.append(entry)
        self._queue.shutdown(immediate=False)
        self._queue.publish_admissions()
        for entry in entries:
            entry.receipt.complete_admission(entry.admission)
        if not inflight:
            self._drain_failed_entries()

    def _drain_failed_entries(self, *, propagate=True):
        with self._queue._sync_mutex:
            if not self._queue.failure_active or self._queue.failure_draining:
                return
            self._queue.failure_draining = True
        control = None
        while True:
            with self._queue._sync_mutex:
                if not self._queue.failed_pending:
                    self._queue.failure_draining = False
                    self._queue.failure_active = False
                    done = self._queue.failure_done
                    break
                entry = self._queue.failed_pending.popleft()
            try:
                self._complete_entry(entry, written=False)
            except BaseException as error:
                if control is None:
                    control = error
                if not propagate:
                    self._remember_consumer_control(error)
        done.set_result(None)
        if control is not None:
            if propagate:
                raise control.with_traceback(control.__traceback__)

    def _remember_consumer_control(self, error):
        if not isinstance(error, Exception) and self._consumer_control is None:
            self._consumer_control = error

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
                self._drain_failed_entries(propagate=False)
                self._stop_marker_done = True
                break
            try:
                if isinstance(entry, _StopMarker):
                    if entry.summary is not None and not self._channel_failed:
                        try:
                            completion = _write_completion(self._writer(entry.summary), entry.summary.level)
                            self._summary_written = (completion.delivered_for(entry.summary.level)
                                                     or completion.fallback_written)
                        except BaseException as error:
                            completion = _exception_write_completion(error)
                            self._summary_written = (completion.delivered_for(entry.summary.level)
                                                     or completion.fallback_written)
                            self._remember_consumer_control(error)
                    self._stop_marker_done = True
                    break
                self._queue.publish_admissions()
                entry.receipt.complete_admission(entry.admission)
                written = LogWriteResult(appended=False)
                if not self._channel_failed:
                    try:
                        written = _write_completion(self._writer(entry.record), entry.record.level)
                    except BaseException as error:
                        written = _exception_write_completion(error)
                        self._remember_consumer_control(error)
                        failure = ChannelError("日志写入失败")
                        failure.__cause__ = error
                        entry.failure = failure
                        try:
                            self.disable_channel(failure)
                        except BaseException as control:
                            self._remember_consumer_control(control)
                if not written.delivered_for(entry.record.level) and self._channel_failed:
                    entry.failure = self._channel_error
                if not entry.failure_deferred:
                    try:
                        self._complete_entry(entry, written=written)
                    except BaseException as error:
                        self._remember_consumer_control(error)
                self._drain_failed_entries(propagate=False)
            finally:
                with self._queue._sync_mutex:
                    if self._queue.inflight is entry:
                        self._queue.inflight = None
                self._queue.sync_q.task_done()

    def _complete_entry(self, entry, *, written):
        admission = entry.admission
        entry.receipt.complete_admission(admission)
        completion = _write_completion(written, entry.record.level)
        delivered = completion.delivered_for(entry.record.level)
        fallback_written = completion.fallback_written
        try:
            if not delivered and not fallback_written:
                fallback_written = self._fallback_entry(entry)
        except BaseException as error:
            _attach_admission(error, AdmissionResult(
                admission.decision, admission.queue_depth, delivered=False,
                appended=completion.appended, durable=completion.durable))
            raise
        finally:
            entry.receipt.complete_write(AdmissionResult(
                admission.decision, admission.queue_depth, delivered, fallback_written,
                appended=completion.appended, durable=completion.durable))
            with self._pending_lock:
                self._pending.pop(entry.pending_id, None)

    def _fallback_entry(self, entry):
        if entry is None or entry.record.level is not LogLevel.ERROR:
            return False
        with entry.fallback_lock:
            if entry.fallback_written is None:
                entry.fallback_written = False
                entry.fallback_written = self._safe_fallback(entry.record)
            return entry.fallback_written

    def _safe_fallback(self, record):
        if self._fallback_failed or getattr(self._fallback_state, "active", False):
            return False
        self._fallback_state.active = True
        try:
            written = (record.fallback or self._fallback)(record.rendered_message()) is not False
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
        with self._queue._sync_mutex:
            outstanding = bool(self._queue._qsize() or self._queue.overflow
                               or self._queue.inflight is not None)
        if outstanding:
            self.disable_channel(ChannelError("日志队列已关闭"))
        self._queue.close()

    async def await_closed(self):
        await self._queue.wait_closed()
        with self._queue._sync_mutex:
            failure_done = self._queue.failure_done
        if failure_done is not None:
            await asyncio.shield(asyncio.wrap_future(failure_done))
        if self._consumer_control is not None:
            raise self._consumer_control.with_traceback(self._consumer_control.__traceback__)


def _level_rank(level):
    return {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARNING: 2, LogLevel.ERROR: 3}[level]


def _write_completion(result: bool | LogWriteResult, level: LogLevel) -> LogWriteResult:
    if isinstance(result, LogWriteResult):
        return result
    # 既有 bool writer 的 True 已表示满足本级别要求；实际文件 writer 交付完整事实。
    return LogWriteResult(appended=bool(result), durable=bool(result) and level is LogLevel.ERROR)


def _exception_write_completion(error: BaseException) -> LogWriteResult:
    try:
        result = getattr(error, "write_result", None)
        completion = getattr(result, "completion", None)
    except BaseException:
        # 附属属性不能替换 writer 已经交付的原异常。
        completion = None
    return completion if isinstance(completion, LogWriteResult) else LogWriteResult(appended=False)


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
