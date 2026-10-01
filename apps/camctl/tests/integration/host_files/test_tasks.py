"""F2 文件任务与默认线程池的组合集成测试。

真实 asyncio.to_thread 默认池执行排队任务；同文件任务串行安排；
等待者取消的任务由真实监督器驱动接手，全部任务取得唯一结果。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from camctl.host_files.tasks import (
    FileTask,
    FileTaskError,
    FileTaskExecutor,
    FileTaskId,
    FileTaskResult,
)
from camctl.session.supervision import OwnedTask, Supervisor

pytestmark = pytest.mark.asyncio


class StopAndAwaitOwner:
    """接手者：请求停止并等待任务实际结束。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, FileTaskResult]] = []

    async def take_over(self, task: OwnedTask) -> None:
        handle = task.pending
        handle.request_stop()
        self.results.append((task.identity, await handle.wait()))


def _task(task_id: str, file_id: int, body) -> FileTask:
    return FileTask(
        task_id=FileTaskId(task_id),
        file_id=file_id,
        stage="segment",
        business="copy",
        body=body,
    )


async def test_default_pool_runs_tasks_on_worker_threads() -> None:
    executor = FileTaskExecutor(Supervisor())
    owner = StopAndAwaitOwner()
    loop_thread = threading.get_ident()
    seen_threads: list[int] = []

    def body(stop: threading.Event, index: int) -> str:
        seen_threads.append(threading.get_ident())
        return f"p{index}"

    futures = [
        asyncio.ensure_future(
            executor.run_file_task(
                _task(f"c{i}", i, lambda stop, i=i: body(stop, i)), owner
            )
        )
        for i in range(1, 7)
    ]
    results = [await future for future in futures]
    assert [result.value for result in results] == [f"p{i}" for i in range(1, 7)]
    assert len(seen_threads) == 6
    assert loop_thread not in seen_threads
    # 默认池由多个工作线程服务并发任务。
    assert len(set(seen_threads)) >= 2
    assert executor.unfinished_files() == ()


async def test_same_file_tasks_run_serially_across_segments() -> None:
    executor = FileTaskExecutor(Supervisor())
    owner = StopAndAwaitOwner()
    loop_thread = threading.get_ident()
    holding = threading.Event()
    release = threading.Event()
    segment_threads: list[int] = []

    def first_body(stop: threading.Event) -> str:
        holding.set()
        assert release.wait(timeout=10)
        segment_threads.append(threading.get_ident())
        return "seg1"

    first = asyncio.ensure_future(
        executor.run_file_task(_task("seg-1", 9, first_body), owner)
    )
    assert await asyncio.to_thread(holding.wait, 10)
    with pytest.raises(FileTaskError):
        await executor.run_file_task(
            _task("seg-1b", 9, lambda stop: "early"), owner
        )

    release.set()
    assert (await first).value == "seg1"

    second = await executor.run_file_task(
        _task("seg-2", 9, lambda stop: (segment_threads.append(threading.get_ident()), "seg2")[1]),
        owner,
    )
    assert second.value == "seg2"
    assert len(segment_threads) == 2
    assert loop_thread not in segment_threads


async def test_cancelled_and_normal_tasks_settle_uniquely() -> None:
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    owner = StopAndAwaitOwner()
    started = {name: threading.Event() for name in ("t1", "t2")}

    def cancellable_body(stop: threading.Event, name: str) -> str:
        started[name].set()
        assert stop.wait(timeout=10)
        return f"{name}-stopped"

    cancelled_futures = {
        name: asyncio.ensure_future(
            executor.run_file_task(
                _task(name, index, lambda stop, name=name: cancellable_body(stop, name)),
                owner,
            )
        )
        for index, name in enumerate(("t1", "t2"), start=1)
    }
    normal_futures = {
        name: asyncio.ensure_future(
            executor.run_file_task(
                _task(name, index, lambda stop, name=name: f"{name}-done"), owner
            )
        )
        for index, name in enumerate(("t3", "t4"), start=3)
    }

    for name, future in cancelled_futures.items():
        assert await asyncio.to_thread(started[name].wait, 10)
        future.cancel()
        with pytest.raises(asyncio.CancelledError):
            await future

    for name, future in normal_futures.items():
        assert (await future).value == f"{name}-done"

    drained = await supervisor.drain_required()
    assert [record.identity for record in drained.settled] == ["t1", "t2"]
    assert [record.identity for record in drained.failed] == []
    assert sorted(identity for identity, _ in owner.results) == ["t1", "t2"]
    for identity, result in owner.results:
        assert result.ran is True
        assert result.value == f"{identity}-stopped"
    assert executor.unfinished_files() == ()
    assert executor.lease_of(FileTaskId("t1")) is None
