"""F2 默认线程池任务与唯一修改权的单元测试。

等待者取消不释放实际文件资格：已开始任务由监督拥有者接手实际结
果，排队任务撤回后确定不执行。撤回与开始经同一裁决只产生一个结
果；重复停止幂等；同文件最多一个未结束任务。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

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


class GatedRunner:
    """线程执行端口替身：任务先在真实线程排队，闸门释放后才开始。"""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.calls = 0

    async def __call__(self, fn):
        self.calls += 1

        def release_then_run():
            self.gate.wait()
            return fn()

        return await asyncio.to_thread(release_then_run)

    def release(self) -> None:
        self.gate.set()


class RecordingOwner:
    """接手者替身：记录全部登记的任务。"""

    def __init__(self) -> None:
        self.tasks: list[OwnedTask] = []

    async def take_over(self, task: OwnedTask) -> None:
        self.tasks.append(task)


def _task(
    task_id: str,
    file_id: int,
    body,
    *,
    stage: str = "segment",
    business: str = "copy",
) -> FileTask:
    return FileTask(
        task_id=FileTaskId(task_id),
        file_id=file_id,
        stage=stage,
        business=business,
        body=body,
    )


async def test_normal_task_returns_actual_result() -> None:
    executor = FileTaskExecutor(Supervisor())
    result = await executor.run_file_task(
        _task("t1", 7, lambda stop: "done"), RecordingOwner()
    )
    assert isinstance(result, FileTaskResult)
    assert result.task_id == FileTaskId("t1")
    assert result.ran is True
    assert result.value == "done"
    assert result.error is None


async def test_cancel_does_not_release_started_file() -> None:
    """等待者取消后线程仍持有文件；接手者等实际结束才见释放。"""
    runner = GatedRunner()
    runner.release()
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor, thread_runner=runner)
    body_started = threading.Event()
    body_release = threading.Event()
    stop_observed: list[bool] = []

    def body(stop: threading.Event) -> str:
        body_started.set()
        assert body_release.wait(timeout=10)
        stop_observed.append(stop.is_set())
        return "actual"

    owner = RecordingOwner()
    future = asyncio.ensure_future(
        executor.run_file_task(_task("t1", 7, body), owner)
    )
    await asyncio.to_thread(body_started.wait, 10)
    lease = executor.lease_of(FileTaskId("t1"))
    assert lease is not None and lease.acquired and not lease.released

    future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await future

    # 等待者已取消，实际线程仍在执行：修改资格保持。
    assert lease.released is False
    assert executor.unfinished_files() == (7,)
    outstanding = supervisor.outstanding()
    assert [record.identity for record in outstanding] == ["t1"]
    drained = await supervisor.drain_required()
    assert [record.identity for record in drained.settled] == ["t1"]
    assert len(owner.tasks) == 1
    taken = owner.tasks[0]
    assert taken.identity == "t1"
    assert taken.stage == "segment"
    assert taken.business == "copy"
    assert "file:7" in taken.resources

    handle = taken.pending
    handle.request_stop()
    body_release.set()
    result = await handle.wait()
    assert result.ran is True
    assert result.value == "actual"
    assert stop_observed == [True]
    assert lease.released is True
    assert executor.unfinished_files() == ()


async def test_queued_cancel_withdraws_without_execution() -> None:
    """排队中取消：撤回成功，任务确定不执行且互斥立即可用。"""
    runner = GatedRunner()
    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    executed: list[int] = []

    def body(stop: threading.Event) -> str:
        executed.append(1)
        return "x"

    owner = RecordingOwner()
    future = asyncio.ensure_future(
        executor.run_file_task(_task("q1", 3, body), owner)
    )
    await asyncio.sleep(0.05)
    assert runner.calls == 1

    future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await future

    runner.release()
    await asyncio.sleep(0.05)
    assert executed == []
    assert len(owner.tasks) == 0
    assert executor.unfinished_files() == ()

    # 撤回后同文件可立即安排新任务并正常完成。
    follow = await executor.run_file_task(_task("q2", 3, body), owner)
    assert follow.ran is True
    assert follow.value == "x"
    assert executed == [1]


async def test_withdraw_race_start_wins_single_outcome() -> None:
    """开始先裁决时取消不能撤回，只能接手实际结果。"""
    runner = GatedRunner()
    runner.release()
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor, thread_runner=runner)
    body_started = threading.Event()
    body_release = threading.Event()

    def body(stop: threading.Event) -> str:
        body_started.set()
        assert body_release.wait(timeout=10)
        return "late"

    owner = RecordingOwner()
    future = asyncio.ensure_future(
        executor.run_file_task(_task("r1", 5, body), owner)
    )
    await asyncio.to_thread(body_started.wait, 10)
    future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await future
    body_release.set()

    drained = await supervisor.drain_required()
    assert [record.identity for record in drained.settled] == ["r1"]
    handle = owner.tasks[0].pending
    result = await handle.wait()
    assert result.ran is True
    assert result.value == "late"


async def test_repeat_stop_and_stop_after_end_are_idempotent() -> None:
    runner = GatedRunner()
    runner.release()
    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    stop_seen: list[bool] = []
    release = threading.Event()

    def body(stop: threading.Event) -> str:
        assert release.wait(timeout=10)
        stop_seen.append(stop.is_set())
        return "ok"

    owner = RecordingOwner()
    task_id = FileTaskId("s1")
    future = asyncio.ensure_future(
        executor.run_file_task(_task("s1", 9, body), owner)
    )
    await asyncio.sleep(0.05)
    executor.request_stop(task_id)
    executor.request_stop(task_id)
    release.set()
    result = await future
    assert result.ran is True
    assert stop_seen == [True]

    # 已结束任务上重复停止是幂等无操作，不抛错、不重复释放。
    executor.request_stop(task_id)
    assert executor.lease_of(task_id) is None
    again = await executor.run_file_task(_task("s2", 9, body), owner)
    assert again.ran is True


async def test_second_task_on_same_file_is_rejected_until_first_ends() -> None:
    runner = GatedRunner()
    runner.release()
    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    release = threading.Event()

    def body(stop: threading.Event) -> str:
        assert release.wait(timeout=10)
        return "first"

    owner = RecordingOwner()
    first = asyncio.ensure_future(
        executor.run_file_task(_task("a1", 4, body), owner)
    )
    await asyncio.sleep(0.05)
    with pytest.raises(FileTaskError):
        await executor.run_file_task(_task("a2", 4, body), owner)

    release.set()
    assert (await first).value == "first"
    second = await executor.run_file_task(_task("a3", 4, body), owner)
    assert second.ran is True


async def test_duplicate_task_identity_is_rejected() -> None:
    executor = FileTaskExecutor(Supervisor())
    owner = RecordingOwner()
    release = threading.Event()

    def first_body(stop: threading.Event) -> str:
        assert release.wait(timeout=10)
        return "x"

    running = asyncio.ensure_future(
        executor.run_file_task(_task("d1", 6, first_body), owner)
    )
    await asyncio.sleep(0.05)
    with pytest.raises(FileTaskError):
        await executor.run_file_task(_task("d1", 8, lambda stop: "y"), owner)

    release.set()
    assert (await running).value == "x"


async def test_body_exception_is_captured_not_swallowed() -> None:
    executor = FileTaskExecutor(Supervisor())

    def body(stop: threading.Event) -> Any:
        raise RuntimeError("boom")

    result = await executor.run_file_task(_task("e1", 2, body), RecordingOwner())
    assert result.ran is True
    assert result.value is None
    assert result.error is not None
    assert "RuntimeError" in result.error and "boom" in result.error
    assert executor.unfinished_files() == ()
