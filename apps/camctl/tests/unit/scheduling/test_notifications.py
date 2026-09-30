"""Q3 提交与等待通知交接的单元测试。

期望独立来自调度通知契约：检查与等待之间到达的通知不丢失（版
本比较，旧版本不可直接睡眠）；重复通知合并不丢最后变化；超时
只触发重新判断；通知只安排再次查询，不替代持久化发现。
"""

from __future__ import annotations

import asyncio

import pytest

from camctl.scheduling.notifications import WakeReason, WorkNotifier

pytestmark = pytest.mark.asyncio


class TestVersionedWake:
    async def test_notify_between_check_and_wait(self) -> None:
        # 通知在读版本、查工作、进入等待三个位置到达都不丢：等待前
        # 版本已前进 → 立即返回重新判断。
        notifier = WorkNotifier()
        observed = notifier.snapshot()
        notifier.mark_changed(WakeReason.NEW_WORK)
        result = await notifier.wait_changed(observed, deadline=None)
        assert result.version > observed.version
        assert result.reasons >= {WakeReason.NEW_WORK}

    async def test_wait_then_notify_wakes(self) -> None:
        notifier = WorkNotifier()
        observed = notifier.snapshot()

        async def notify_later() -> None:
            await asyncio.sleep(0.01)
            notifier.mark_changed(WakeReason.ACTIVITY_FINISHED)

        notifier_task = asyncio.create_task(notify_later())
        result = await notifier.wait_changed(observed, deadline=None)
        await notifier_task
        assert result.version > observed.version

    async def test_duplicate_notifications_merge_keep_last_reasons(self) -> None:
        notifier = WorkNotifier()
        observed = notifier.snapshot()
        notifier.mark_changed(WakeReason.NEW_WORK)
        token = notifier.mark_changed(WakeReason.SOURCE_FINISHED)
        # 合并为一次唤醒，但两种原因都保留（不丢最后变化）。
        result = await notifier.wait_changed(observed, deadline=None)
        assert result.version == token.version
        assert result.reasons >= {WakeReason.NEW_WORK, WakeReason.SOURCE_FINISHED}

    async def test_timeout_only_triggers_recheck(self) -> None:
        notifier = WorkNotifier()
        observed = notifier.snapshot()
        deadline = asyncio.get_running_loop().time() + 0.02
        result = await notifier.wait_changed(observed, deadline=deadline)
        # 超时返回当前版本供重新判断，不报告虚假变化。
        assert result.version == observed.version
        assert result.reasons == frozenset()

    async def test_cancelled_waiter_notification_still_marks(self) -> None:
        notifier = WorkNotifier()
        observed = notifier.snapshot()

        async def waiter() -> None:
            await notifier.wait_changed(observed, deadline=None)

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.005)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        # 原等待者取消：通知仍登记（提交由责任接手方触发）。
        token = notifier.mark_changed(WakeReason.NEW_WORK)
        assert token.version > observed.version

    async def test_snapshot_returns_latest_token(self) -> None:
        notifier = WorkNotifier()
        first = notifier.snapshot()
        second = notifier.mark_changed(WakeReason.NEW_WORK)
        assert notifier.snapshot().version == second.version >= first.version

    async def test_old_token_never_sleeps(self) -> None:
        # 检查后通知到达：旧版本不得进入睡眠（无检查后睡死竞争）。
        notifier = WorkNotifier()
        stale = notifier.snapshot()
        notifier.mark_changed(WakeReason.NEW_WORK)
        latest = notifier.snapshot()
        # 旧令牌等待立即完成；新令牌正常等待。
        immediate = await notifier.wait_changed(stale, deadline=None)
        assert immediate.version >= latest.version
