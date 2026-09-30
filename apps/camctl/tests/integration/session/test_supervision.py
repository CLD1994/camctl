"""S5 监督与取消接手的组件集成测试。

真实事件循环与真实工作线程组合：等待者取消后，实际任务在工作
线程完成并经线程安全通知回到事件循环，接手消费恰好一次，资源
在实际结果保存/通知结束后才释放。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from camctl.session.supervision import OwnedTask, Supervisor

pytestmark = pytest.mark.asyncio

_EVENTS: list[tuple[str, float]] = []


class ThreadOwner:
    """在接手中等待实际结果、记录顺序并释放资源的拥有者。"""

    def __init__(self, events: list[tuple[str, float]], clock: asyncio.AbstractEventLoop) -> None:
        self.events = events
        self.clock = clock
        self.consumptions = 0
        self.released: set[str] = set()

    async def take_over(self, task: OwnedTask) -> None:
        result = await asyncio.shield(task.pending)
        self.consumptions += 1
        self.events.append(("consumed", self.clock.time()))
        assert result == {"kind": "COMPLETED"}
        self.events.append(("released", self.clock.time()))
        self.released.add(task.identity)


@pytest.fixture(autouse=True)
def _reset_events():
    _EVENTS.clear()
    yield
    _EVENTS.clear()


async def test_thread_actual_survives_waiter_cancel() -> None:
    supervisor = Supervisor()
    loop = asyncio.get_running_loop()
    owner = ThreadOwner(_EVENTS, loop)
    token = supervisor.register(owner)
    actual: asyncio.Future = loop.create_future()

    def worker() -> None:
        # 真实线程执行实际任务，经线程安全入口回传结果。
        time.sleep(0.02)

        def finish() -> None:
            _EVENTS.append(("actual-completed", loop.time()))
            actual.set_result({"kind": "COMPLETED"})

        loop.call_soon_threadsafe(finish)

    thread = threading.Thread(target=worker, name="actual-worker")
    thread.start()

    async def waiter() -> None:
        try:
            await asyncio.shield(actual)
        except asyncio.CancelledError:
            _EVENTS.append(("waiter-cancelled", loop.time()))
            supervisor.handoff(
                token,
                OwnedTask(
                    identity="op-thread",
                    stage="db-write",
                    business="capture",
                    resources=("db-main",),
                    pending=actual,
                ),
            )
            raise

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.005)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    # 等待者已取消而实际线程尚未完成：资源未释放、未消费。
    assert owner.released == set()
    assert owner.consumptions == 0

    result = await supervisor.drain_required()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert owner.consumptions == 1
    assert owner.released == {"op-thread"}
    assert [r.identity for r in result.settled] == ["op-thread"]
    # 顺序：等待者取消 → 实际完成 → 消费 → 释放。
    order = [name for name, _ in _EVENTS]
    assert order == ["waiter-cancelled", "actual-completed", "consumed", "released"]
