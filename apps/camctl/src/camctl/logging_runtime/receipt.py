"""可忽略、可重复等待的日志接纳与实际写入事实。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, InvalidStateError
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from camctl.logging_runtime.service import AdmissionResult

__all__ = ["LogReceipt"]


class LogReceipt:
    """线程安全地保存首份事实；取消等待者不会取消原日志责任。"""

    def __init__(self) -> None:
        self._admission: Future[AdmissionResult] = Future()
        self._write: Future[AdmissionResult] = Future()
        self._lock = threading.RLock()
        self._submission_claimed = False

    def _claim_submission(self) -> bool:
        with self._lock:
            if self._submission_claimed or self._write.done():
                return False
            self._submission_claimed = True
            return True

    def complete_admission(self, result: AdmissionResult) -> None:
        self._complete(self._admission, result)

    def complete_write(self, result: AdmissionResult) -> None:
        self._complete(self._write, result)

    @staticmethod
    def _complete(future, result):
        # Future 在自己的锁内选定唯一事实，并在释放该锁后调用回调。
        try:
            future.set_result(result)
        except InvalidStateError:
            pass

    @property
    def admission_result(self) -> AdmissionResult | None:
        return self._admission.result() if self._admission.done() else None

    @property
    def write_result(self) -> AdmissionResult | None:
        return self._write.result() if self._write.done() else None

    def add_admission_callback(self, callback: Callable[[AdmissionResult], None]) -> None:
        """在结算线程通知；asyncio 调用方自行桥接回所属事件循环。"""
        self._admission.add_done_callback(lambda future: callback(future.result()))

    def add_done_callback(self, callback: Callable[[AdmissionResult], None]) -> None:
        """在结算线程通知实际写入结果。"""
        self._write.add_done_callback(lambda future: callback(future.result()))

    async def wait_admission(self) -> AdmissionResult:
        if self._admission.done():
            return self._admission.result()
        return await asyncio.shield(asyncio.wrap_future(self._admission))

    async def _wait_write(self) -> AdmissionResult:
        if self._write.done():
            return self._write.result()
        return await asyncio.shield(asyncio.wrap_future(self._write))

    def __await__(self):
        return self._wait_write().__await__()
