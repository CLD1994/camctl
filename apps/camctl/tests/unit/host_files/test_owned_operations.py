"""完整文件操作的嵌套资格与停止，不访问任何实际文件。"""

import asyncio

import pytest

from camctl.host_files.tasks import (
    AsyncFileTask, FileTaskError, FileTaskExecutor, FileTaskId,
)
from camctl.session.supervision import Supervisor


def _task(identity, files, body):
    return AsyncFileTask(FileTaskId(identity), files, "copy", "原文件责任", body)


@pytest.mark.asyncio
async def test_nested_operation_reuses_original_lease_and_control():
    executor = FileTaskExecutor(Supervisor())
    controls = []

    async def nested(control):
        controls.append(control)
        assert executor.unfinished_files() == (1, 2)
        return "saved original result"

    async def parent(control):
        controls.append(control)
        return await executor.run_owned_async_file_task(_task("segment", (1,), nested))

    result = await executor.run_owned_async_file_task(_task("read", (1, 2), parent))

    assert result == "saved original result"
    assert len(controls) == 2 and controls[0] is controls[1]
    assert executor.unfinished_files() == ()


@pytest.mark.asyncio
async def test_parallel_child_cannot_inherit_parent_file_qualification():
    executor = FileTaskExecutor(Supervisor())
    entered = []

    async def nested(_control):
        entered.append(True)

    async def parent(_control):
        child = asyncio.create_task(executor.run_owned_async_file_task(
            _task("parallel", (1,), nested)))
        with pytest.raises(FileTaskError):
            await child
        assert executor.unfinished_files() == (1,)

    await executor.run_owned_async_file_task(_task("read", (1,), parent))

    assert entered == []
    assert executor.unfinished_files() == ()


@pytest.mark.asyncio
async def test_owned_cancellation_requests_stop_but_waits_actual_result():
    executor = FileTaskExecutor(Supervisor())
    entered, release = asyncio.Event(), asyncio.Event()
    controls, saved = [], []

    async def body(control):
        controls.append(control)
        entered.set()
        await release.wait()
        saved.append("actual result saved")

    running = asyncio.create_task(executor.run_owned_async_file_task(
        _task("read", (1,), body)))
    try:
        await entered.wait()
        running.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert controls[0].stop_requested
        assert not running.done()
        assert executor.unfinished_files() == (1,)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert saved == ["actual result saved"]
        assert executor.unfinished_files() == ()
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_cancellation_preserves_actual_save_failure_as_original_cause():
    executor = FileTaskExecutor(Supervisor())
    entered, release = asyncio.Event(), asyncio.Event()
    failure = ValueError("original save failed")

    async def body(control):
        entered.set()
        await release.wait()
        assert control.stop_requested
        raise failure

    running = asyncio.create_task(executor.run_owned_async_file_task(
        _task("read", (1,), body)))
    await entered.wait()
    running.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not running.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await running
    assert caught.value.__cause__ is failure
    assert executor.unfinished_files() == ()


@pytest.mark.asyncio
async def test_nested_media_task_keeps_outer_result_owner_and_file_lease():
    from unittest.mock import create_autospec
    from camctl.session.supervision import ResponsibilityOwner

    executor = FileTaskExecutor(Supervisor())
    tool_owner = create_autospec(ResponsibilityOwner, instance=True)
    saved = []
    controls = []

    async def tool(control):
        controls.append(control)
        assert executor.unfinished_files() == (1, 2)
        return "actual tool result"

    async def operation(control):
        controls.append(control)
        result = await executor.run_async_file_task(_task("media-tool", (1,), tool), tool_owner)
        assert result.ran
        assert result.value == "actual tool result"
        saved.append(result.value)
        assert executor.unfinished_files() == (1, 2)
        return "saved media result"

    assert await executor.run_owned_async_file_task(_task("media-check", (1, 2), operation)) == "saved media result"
    assert saved == ["actual tool result"]
    assert controls[0] is controls[1]
    tool_owner.take_over.assert_not_called()
    assert executor.unfinished_files() == ()
