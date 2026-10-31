"""同一机身的实际工具调用互斥；工具负责结束后才交回结果。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from time import monotonic_ns


class CameraToolGate:
    def __init__(self, *, monotonic_ns=monotonic_ns, start_error=TimeoutError):
        self._locks = {}
        self._now = monotonic_ns
        self._start_error = start_error

    async def run(self, resource_key, transport, spec, stop, *, interrupted=None):
        lock = self._locks.setdefault(resource_key, asyncio.Lock())
        began = self._now()
        try:
            async with asyncio.timeout(None if spec.timeout_s is None else float(spec.timeout_s)):
                await lock.acquire()
        except TimeoutError as error:
            raise self._start_error("等待相机 USB 调用机会时原期限已耗尽") from error
        except asyncio.CancelledError as cancellation:
            raise cancellation from self._start_error("等待相机 USB 调用机会时取消，工具尚未启动")
        try:
            actual = spec
            if spec.timeout_s is not None:
                remaining = spec.timeout_s - Decimal(self._now() - began) / 1_000_000_000
                if remaining <= 0:
                    raise self._start_error("取得相机 USB 调用机会时原期限已耗尽")
                actual = replace(spec, timeout_s=remaining)
            if interrupted is not None:
                cancellation = interrupted()
                if cancellation is not None:
                    raise cancellation from self._start_error("相机 USB 调用派发前取消，工具尚未启动")
            return await transport.run(actual, stop)
        finally:
            lock.release()
