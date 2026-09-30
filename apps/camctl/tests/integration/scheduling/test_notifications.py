"""Q3 通知交接的组件集成测试：真实线程提交与调度协程等待。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from camctl.scheduling.notifications import WakeReason, WorkNotifier

pytestmark = pytest.mark.asyncio


class TestThreadNotification:
    async def test_external_thread_mark_wakes_scheduler(self) -> None:
        notifier = WorkNotifier()
        observed = notifier.snapshot()

        def external_submit() -> None:
            time.sleep(0.01)
            notifier.mark_changed(WakeReason.NEW_WORK)

        thread = threading.Thread(target=external_submit, name="submit-thread")
        thread.start()
        try:
            result = await asyncio.wait_for(
                notifier.wait_changed(observed, deadline=None), timeout=5
            )
        finally:
            thread.join(timeout=5)
        assert result.version > observed.version
        assert WakeReason.NEW_WORK in result.reasons

    async def test_recheck_loop_makes_progress_under_notifications(self) -> None:
        # 调度循环：查工作（无变化）→ 等待 → 线程通知 → 重查退出。
        notifier = WorkNotifier()
        rechecks = 0
        observed = notifier.snapshot()

        def submit_three_times() -> None:
            for _ in range(3):
                time.sleep(0.005)
                notifier.mark_changed(WakeReason.NEW_WORK)

        thread = threading.Thread(target=submit_three_times, name="submit-thread")
        thread.start()
        try:
            while True:
                token = await notifier.wait_changed(observed, deadline=None)
                rechecks += 1
                observed = token
                if observed.version >= 3:
                    break
        finally:
            thread.join(timeout=5)
        assert rechecks >= 1
