"""媒体与同步文件任务共用占用及结果责任；执行体不访问外部资源。"""

import asyncio

import pytest

from camctl.host_files import tasks
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio


class Owner:
    def __init__(self):
        self.tasks = []

    async def take_over(self, task):
        self.tasks.append(task)


def job(identity, file_ids, body):
    return tasks.AsyncFileTask(tasks.FileTaskId(identity), file_ids, "media", "recording", body)


async def test_async_task_reserves_all_files_until_actual_end():
    executor = tasks.FileTaskExecutor(Supervisor())
    started, release = asyncio.Event(), asyncio.Event()

    async def body(control):
        started.set()
        await release.wait()
        return "artifact"

    waiting = asyncio.create_task(executor.run_async_file_task(job("m", (1, 2), body), Owner()))
    try:
        await started.wait()
        assert executor.unfinished_files() == (1, 2)
        for identity in (1, 2):
            with pytest.raises(tasks.FileTaskError):
                await executor.run_file_task(
                    tasks.FileTask(tasks.FileTaskId(f"f{identity}"), identity, "write", "copy", lambda stop: None),
                    Owner(),
                )
    finally:
        release.set()
    result = await waiting
    assert result.ran is True and result.value == "artifact" and result.error is None
    assert executor.unfinished_files() == ()


async def test_async_admission_conflict_has_no_partial_file_reservation():
    supervisor = Supervisor()
    executor = tasks.FileTaskExecutor(supervisor)
    started, release = asyncio.Event(), asyncio.Event()

    async def body(control):
        started.set()
        await release.wait()

    waiting = asyncio.create_task(executor.run_async_file_task(job("first", (2,), body), Owner()))
    try:
        await started.wait()
        with pytest.raises(tasks.FileTaskError):
            await executor.run_async_file_task(job("second", (1, 2), body), Owner())
        assert executor.unfinished_files() == (2,)
    finally:
        release.set()
        await waiting


async def test_cancelled_waiter_hands_over_stop_and_actual_result():
    supervisor, owner = Supervisor(), Owner()
    executor = tasks.FileTaskExecutor(supervisor)
    started, release = asyncio.Event(), asyncio.Event()

    async def body(control):
        started.set()
        await control.requested()
        await release.wait()
        return control.stop_requested

    waiting = asyncio.create_task(executor.run_async_file_task(job("m", (1, 2), body), owner))
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert executor.unfinished_files() == (1, 2)
    assert supervisor.outstanding()[0].resources == ("file:1", "file:2")
    await supervisor.drain_required()
    handle = owner.tasks[0].pending
    handle.request_stop()
    release.set()
    result = await handle.wait()
    assert result.ran is True and result.value is True and result.error is None
    assert executor.unfinished_files() == ()


async def test_async_result_not_received_reserves_identity():
    supervisor, owner = Supervisor(), Owner()
    executor = tasks.FileTaskExecutor(supervisor)

    async def body(control):
        waiting.cancel()
        return "finished"

    waiting = asyncio.create_task(executor.run_async_file_task(job("same", (1,), body), owner))
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert executor.unfinished_files() == ()
    with pytest.raises(tasks.FileTaskError):
        await executor.run_async_file_task(job("same", (1,), body), owner)
    await supervisor.drain_required()
    result = await owner.tasks[0].pending.wait()
    assert result.value == "finished"

    async def next_body(control):
        return "next"

    result = await executor.run_async_file_task(job("same", (1,), next_body), owner)
    assert result.value == "next"


async def test_queued_async_cancel_prevents_body_even_when_scheduler_resumes():
    loop = asyncio.get_running_loop()
    gate = asyncio.Event()
    calls = []
    supervisor = Supervisor()
    executor = tasks.FileTaskExecutor(supervisor)

    async def body(control):
        calls.append("executed")

    async def delayed(coroutine):
        await gate.wait()
        return await coroutine

    previous = loop.get_task_factory()
    waiting = loop.create_task(executor.run_async_file_task(job("queued", (1, 2), body), Owner()))
    loop.set_task_factory(lambda loop, coroutine, **kwargs: asyncio.Task(delayed(coroutine), loop=loop, **kwargs))
    try:
        await asyncio.sleep(0)
    finally:
        loop.set_task_factory(previous)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()
    gate.set()
    await asyncio.sleep(0)
    assert calls == []


async def test_async_body_failure_retains_diagnostic_and_releases_files():
    executor = tasks.FileTaskExecutor(Supervisor())

    async def body(control):
        raise OSError("media worker failure")

    result = await executor.run_async_file_task(job("error", (1, 2), body), Owner())
    assert result.ran is True and result.value is None
    assert "OSError" in result.error and "media worker failure" in result.error
    assert executor.unfinished_files() == ()


async def test_queued_sync_task_blocks_overlapping_media_task():
    gate = asyncio.Event()
    admitted = asyncio.Event()

    async def runner(body):
        admitted.set()
        await gate.wait()
        return body()

    executor = tasks.FileTaskExecutor(Supervisor(), thread_runner=runner)
    waiting = asyncio.create_task(executor.run_file_task(
        tasks.FileTask(tasks.FileTaskId("write"), 2, "write", "copy", lambda stop: None), Owner(),
    ))

    async def media_body(control):
        raise AssertionError("存在文件冲突时不能派发")

    try:
        await admitted.wait()
        with pytest.raises(tasks.FileTaskError):
            await executor.run_async_file_task(job("media", (1, 2), media_body), Owner())
        assert executor.unfinished_files() == (2,)
    finally:
        gate.set()
        await waiting


@pytest.mark.parametrize("file_ids", [(), (1, 1), (True,), (1.0,), (0,), (-1,), [1, 2]])
async def test_async_file_set_is_valid_and_immutable(file_ids):
    async def body(control):
        pass

    with pytest.raises(tasks.FileTaskError):
        job("bad", file_ids, body)


async def test_boolean_file_identity_is_rejected_by_sync_entry():
    with pytest.raises(tasks.FileTaskError):
        tasks.FileTask(tasks.FileTaskId("bad"), True, "write", "copy", lambda stop: None)
