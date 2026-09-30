"""可靠调度通知交接。

通知只安排再次查询，不替代持久化发现。共享同步边界维护版本与
原因集合：旧版本不可直接睡眠，检查与等待之间到达的通知由版本
比较保证不丢失；重复通知合并，但原因集合保留最后变化。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

__all__ = ["MonotonicDeadline", "WakeReason", "WakeToken", "WorkNotifier"]

#: 单调截止时刻（事件循环单调秒）；None 表示无期限。
MonotonicDeadline = float | None


class WakeReason(Enum):
    """唤醒原因；由本模块集中定义，不替代持久化发现。"""

    NEW_WORK = "new_work"
    ACTIVITY_FINISHED = "activity_finished"
    SOURCE_FINISHED = "source_finished"
    RETRY_READY = "retry_ready"
    READ_OPPORTUNITY = "read_opportunity"


@dataclass(frozen=True)
class WakeToken:
    """一个观察版本及该版本前到达的原因集合。"""

    version: int
    reasons: frozenset[WakeReason]


class WorkNotifier:
    """进程内版本化唤醒通知。

    mark_changed 可从任意线程调用（外部提交经持久化发现入口形成
    通知）；等待方在事件循环内协作。
    """

    def __init__(self) -> None:
        self._version = 0
        self._reasons: set[WakeReason] = set()
        # 可重入：wait_changed 在持锁分支内读取快照。
        self._lock = threading.RLock()
        self._event: asyncio.Event | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def mark_changed(self, reason: WakeReason) -> WakeToken:
        """登记一次变化并唤醒等待方；原因合并不覆盖。"""
        with self._lock:
            self._version += 1
            self._reasons.add(reason)
            version = self._version
            reasons = frozenset(self._reasons)
            event = self._event
        if event is not None:
            # 唤醒必须线程安全：外部线程不得直接触碰事件循环对象。
            self._wake(event)
        return WakeToken(version=version, reasons=reasons)

    def _wake(self, event: asyncio.Event) -> None:
        loop = self._event_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:  # pragma: no cover - 无等待方登记时无需唤醒
            pass

    def snapshot(self) -> WakeToken:
        with self._lock:
            return WakeToken(version=self._version, reasons=frozenset(self._reasons))

    async def wait_changed(
        self, observed: WakeToken, deadline: MonotonicDeadline = None
    ) -> WakeToken:
        """等待 observed 之后的新变化；超时返回当前版本供重新判断。

        observed 落后于当前版本时立即返回（不睡眠），保证检查与
        等待之间到达的通知不丢失。
        """
        current = self.snapshot()
        if observed.version < current.version:
            return current
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._lock:
            # 边界内重读版本：登记事件前到达的变化仍被版本比较覆盖。
            current = self.snapshot()
            if observed.version < current.version:
                return current
            self._event = event
            self._event_loop = loop
        try:
            if deadline is None:
                await event.wait()
            else:
                timeout = deadline - loop.time()
                if timeout > 0:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=timeout)
                    except TimeoutError:
                        pass
            return self.snapshot()
        finally:
            with self._lock:
                if self._event is event:
                    self._event = None
                    self._event_loop = None
                else:
                    self._wake(event)
