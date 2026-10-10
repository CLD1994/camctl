"""F2 文件任务与默认线程池的组合集成测试。

真实 asyncio.to_thread 默认池执行排队任务；同文件任务串行安排；
等待者取消的任务由真实监督器驱动接手，全部任务取得唯一结果。
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import threading

import pytest

from camctl.host_files.tasks import (
    AsyncFileTask,
    FileTask,
    FileTaskError,
    FileTaskExecutor,
    FileTaskId,
    FileTaskResult,
)
from camctl.session.supervision import OwnedTask, Supervisor
from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork

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


async def test_background_stop_delivers_actual_save_error_after_source_and_connection_close(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"original source")
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as setup:
        setup.execute("CREATE TABLE requests (operation_key TEXT PRIMARY KEY)")
        setup.execute("INSERT INTO requests VALUES ('original-key')")
    executor = FileTaskExecutor(Supervisor())
    entered, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations, failures = [], []

    async def flow(_context):
        connection = sqlite3.connect(database)
        async def body(control):
            try:
                with source.open("rb") as reader:
                    assert reader.read(3) == b"ori"
                    entered.set()
                    await control.requested()
                    stopped.set()
                    await release.wait()
                observations.append("source_closed")
                try:
                    connection.execute("INSERT INTO requests VALUES ('original-key')")
                except sqlite3.IntegrityError as error:
                    failures.append(error)
                    raise
            finally:
                observations.append("actual_body_ended")
        try:
            await executor.run_owned_async_file_task(
                AsyncFileTask(FileTaskId("read-original"), (1,), "read", "原读取", body))
        finally:
            connection.close()
            observations.append("connection_closed")

    background = BackgroundFlow(flow)
    await background(None)
    await entered.wait()
    background.stop_new_work()
    waiter = asyncio.create_task(CombinedLocalWork((background,)).settle())
    await stopped.wait()
    assert not waiter.done() and executor.unfinished_files() == (1,)
    assert observations == []
    release.set()
    with pytest.raises(sqlite3.IntegrityError) as caught:
        await waiter
    assert caught.value is failures[0]
    assert observations == ["source_closed", "actual_body_ended", "connection_closed"]
    assert executor.unfinished_files() == () and background.required_settlements() == 0
    assert source.read_bytes() == b"original source"
    with sqlite3.connect(database) as reopened:
        assert reopened.execute("SELECT operation_key FROM requests").fetchall() == [("original-key",)]


async def test_default_pool_runs_tasks_on_worker_threads() -> None:
    executor = FileTaskExecutor(Supervisor())
    owner = StopAndAwaitOwner()
    loop_thread = threading.get_ident()
    seen_threads: list[int] = []
    overlap = threading.Barrier(2)

    def body(stop: threading.Event, index: int) -> str:
        seen_threads.append(threading.get_ident())
        if index in (1, 2):
            overlap.wait(timeout=10)
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


@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("wrapper_fails", [False, True])
async def test_completed_result_is_consumed_after_waiter_cancel(body_fails, wrapper_fails):
    completed, deliver, wrapper_ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    supervisor = Supervisor()
    owner = StopAndAwaitOwner()
    marker = object()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _, context: unhandled.append(context))

    async def runner(fn):
        result = await asyncio.to_thread(fn)
        completed.set()
        try:
            await deliver.wait()
            if wrapper_fails:
                raise RuntimeError("wrapper failed after completion")
            return result
        finally:
            wrapper_ended.set()

    def body(stop):
        if body_fails:
            raise OSError("file operation failed")
        return marker

    executor = FileTaskExecutor(supervisor, thread_runner=runner)
    waiting = asyncio.create_task(executor.run_file_task(_task("done", 9, body), owner))
    try:
        await asyncio.wait_for(completed.wait(), 5)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert [task.identity for task in supervisor.outstanding()] == ["done"]
        assert executor.unfinished_files() == ()
        with pytest.raises(FileTaskError):
            await executor.run_file_task(_task("done", 9, lambda _: None), owner)
        settled = await asyncio.wait_for(supervisor.drain_required(), 5)
        assert [task.identity for task in settled.settled] == ["done"]
        assert settled.failed == ()
        assert len(owner.results) == 1
        result = owner.results[0][1]
        if body_fails:
            assert result.value is None
            assert "OSError" in result.error and "file operation failed" in result.error
        else:
            assert result.value is marker and result.error is None
        deliver.set()
        await asyncio.wait_for(wrapper_ended.wait(), 5)
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        assert unhandled == []
    finally:
        deliver.set()
        await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 5)
        await asyncio.wait_for(wrapper_ended.wait(), 5)
        loop.set_exception_handler(previous_handler)


async def test_wrapper_failure_during_execution_retains_lease_and_shared_result():
    started, release = threading.Event(), threading.Event()
    supervisor = Supervisor()
    workers = []
    failure = RuntimeError("runner observation failed")
    marker = object()

    async def runner(fn):
        worker = asyncio.create_task(asyncio.to_thread(fn))
        workers.append(worker)
        assert await asyncio.to_thread(started.wait, 5)
        raise failure

    def body(stop):
        started.set()
        assert release.wait(5), "测试未释放文件操作"
        return marker

    class ObservingOwner(StopAndAwaitOwner):
        def __init__(self):
            super().__init__()
            self.received = asyncio.Event()
            self.handle = None

        async def take_over(self, task):
            self.handle = task.pending
            self.received.set()
            await super().take_over(task)

    owner = ObservingOwner()
    executor = FileTaskExecutor(supervisor, thread_runner=runner)
    pending = []
    try:
        with pytest.raises(RuntimeError) as raised:
            await executor.run_file_task(_task("running", 9, body), owner)
        assert raised.value is failure
        assert executor.unfinished_files() == (9,)
        lease = executor.lease_of(FileTaskId("running"))
        assert lease.acquired and not lease.released
        with pytest.raises(FileTaskError):
            await executor.run_file_task(_task("conflict", 9, lambda _: None), owner)
        draining = asyncio.create_task(supervisor.drain_required())
        pending.append(draining)
        await asyncio.wait_for(owner.received.wait(), 5)
        canceled = asyncio.create_task(owner.handle.wait())
        waiting = asyncio.create_task(owner.handle.wait())
        pending.extend([canceled, waiting])
        await asyncio.sleep(0)
        canceled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled
        assert not waiting.done()
        assert not lease.released
        release.set()
        result = await asyncio.wait_for(waiting, 5)
        assert result.value is marker
        assert result is await owner.handle.wait()
        drained = await asyncio.wait_for(draining, 5)
        assert drained.failed == ()
        assert len(owner.results) == 1 and owner.results[0][1] is result
        assert lease.released is True
        assert executor.unfinished_files() == ()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*workers, *pending, return_exceptions=True), 5)


@pytest.mark.parametrize("waiter_canceled", [False, True])
async def test_body_cancellation_always_completes_supervised_result(waiter_canceled):
    started, release = threading.Event(), threading.Event()
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    owner = StopAndAwaitOwner()

    def body(stop):
        started.set()
        assert release.wait(5), "测试未释放文件操作"
        raise asyncio.CancelledError("file operation canceled")

    waiting = asyncio.create_task(executor.run_file_task(_task("body-cancel", 9, body), owner))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        lease = executor.lease_of(FileTaskId("body-cancel"))
        if waiter_canceled:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not lease.released
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        drained = await asyncio.wait_for(supervisor.drain_required(), 5)
        assert drained.failed == ()
        assert [entry.identity for entry in drained.settled] == ["body-cancel"]
        assert len(owner.results) == 1
        result = owner.results[0][1]
        assert result.ran is True and "CancelledError" in result.error
        assert lease.released is True
        assert executor.unfinished_files() == ()
        assert (await executor.run_file_task(_task("following", 9, lambda _: None), owner)).error is None
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 5)
        await asyncio.wait_for(supervisor.drain_required(), 5)


async def test_result_delivery_allows_identity_reuse_but_old_handle_cannot_release_new_identity():
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    handles = []

    class Owner(StopAndAwaitOwner):
        async def take_over(self, task):
            handles.append(task.pending)
            await super().take_over(task)

    owner = Owner()

    def canceled_body(stop):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await executor.run_file_task(_task("reused", 9, canceled_body), owner)
    with pytest.raises(FileTaskError):
        await executor.run_file_task(_task("reused", 9, lambda _: None), owner)
    assert (await executor.run_file_task(_task("different", 9, lambda _: "next"), owner)).value == "next"
    await asyncio.wait_for(supervisor.drain_required(), 5)
    old_handle = handles[0]
    started, release = threading.Event(), threading.Event()

    def following_body(stop):
        started.set()
        assert release.wait(5), "测试未释放后续操作"
        return "following"

    following = asyncio.create_task(executor.run_file_task(_task("reused", 9, following_body), owner))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert (await old_handle.wait()).error is not None
        # 使用另一文件，防止文件互斥掩盖任务身份被错误释放。
        with pytest.raises(FileTaskError):
            await executor.run_file_task(_task("reused", 10, lambda _: None), owner)
        assert executor.unfinished_files() == (9,)
        release.set()
        assert (await asyncio.wait_for(following, 5)).value == "following"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(following, return_exceptions=True), 5)
