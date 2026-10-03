"""文件任务等待中断时，未交付的实际结果仍有唯一拥有者。"""

import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.host_files.tasks import FileTask, FileTaskError, FileTaskExecutor, FileTaskId, ThreadRunner
from camctl.session.supervision import OwnerToken, ResponsibilityOwner, Supervisor

pytestmark = pytest.mark.asyncio


def _executor(run):
    supervisor = create_autospec(Supervisor, instance=True)
    supervisor.register.return_value = OwnerToken(1)
    owner = create_autospec(ResponsibilityOwner, instance=True)
    runner = create_autospec(ThreadRunner, instance=True)
    runner.side_effect = run
    return FileTaskExecutor(supervisor, thread_runner=runner), supervisor, owner


def _task(body):
    return FileTask(FileTaskId("file/9/1"), 9, "segment", "copy", body)


async def test_cancel_after_execution_still_hands_over_unreceived_result():
    actual = object()

    async def run(fn):
        result = fn()
        waiting.cancel()
        return result

    executor, supervisor, owner = _executor(run)
    waiting = asyncio.create_task(executor.run_file_task(_task(lambda _: actual), owner))
    with pytest.raises(asyncio.CancelledError):
        await waiting
    supervisor.handoff.assert_called_once()
    handed = supervisor.handoff.call_args.args[1]
    result = await handed.pending.wait()
    assert result.value is actual and result.error is None
    assert result.ran is True
    assert handed.pending.lease.released is True
    assert executor.unfinished_files() == ()


@pytest.mark.parametrize("when", ["submit", "queued"])
async def test_failed_submission_withdraws_and_prevents_late_execution(when):
    failure = RuntimeError("executor unavailable")
    callbacks = []
    executed = []

    async def fail():
        raise failure

    def run(fn):
        callbacks.append(fn)
        if when == "submit":
            raise failure
        return fail()

    executor, supervisor, owner = _executor(run)
    with pytest.raises(RuntimeError) as raised:
        await executor.run_file_task(_task(lambda _: executed.append(True)), owner)
    assert raised.value is failure
    assert executor.unfinished_files() == ()
    assert executor.lease_of(FileTaskId("file/9/1")) is None
    supervisor.handoff.assert_not_called()
    # 迟到的包装器只返回撤回事实，不能执行文件操作。
    assert callbacks[0]().ran is False
    assert executed == []


async def test_wrapper_failure_after_execution_keeps_actual_result():
    actual = object()
    failure = RuntimeError("runner failed")

    async def run(fn):
        fn()
        raise failure

    executor, supervisor, owner = _executor(run)
    with pytest.raises(RuntimeError) as raised:
        await executor.run_file_task(_task(lambda _: actual), owner)
    assert raised.value is failure
    supervisor.handoff.assert_called_once()
    result = await supervisor.handoff.call_args.args[1].pending.wait()
    assert result.value is actual and result.error is None


async def test_none_return_is_successful_execution():
    async def run(fn):
        return fn()

    executor, supervisor, owner = _executor(run)
    result = await executor.run_file_task(_task(lambda _: None), owner)
    assert result.ran is True
    assert result.value is None and result.error is None
    supervisor.handoff.assert_not_called()
    assert executor.unfinished_files() == ()


async def test_body_cancellation_settles_result_before_propagating():
    async def run(fn):
        return fn()

    cancellation = asyncio.CancelledError("body canceled")

    def body(stop):
        raise cancellation

    executor, supervisor, owner = _executor(run)
    with pytest.raises(asyncio.CancelledError):
        await executor.run_file_task(_task(body), owner)
    assert executor.unfinished_files() == ()
    supervisor.handoff.assert_called_once()
    handle = supervisor.handoff.call_args.args[1].pending
    result = await handle.wait()
    assert result.ran is True and "CancelledError" in result.error
    assert handle.lease.released is True


async def test_finished_task_identity_is_reserved_until_result_delivery():
    calls = 0

    async def run(fn):
        nonlocal calls
        result = fn()
        calls += 1
        if calls == 1:
            waiting.cancel()
        return result

    executor, supervisor, owner = _executor(run)
    task = _task(lambda _: "actual")
    waiting = asyncio.create_task(executor.run_file_task(task, owner))
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert executor.unfinished_files() == ()
    assert executor.lease_of(task.task_id) is None
    with pytest.raises(FileTaskError):
        await executor.run_file_task(task, owner)
    assert calls == 1
    different = FileTask(FileTaskId("other"), 9, "segment", "copy", lambda _: "next")
    assert (await executor.run_file_task(different, owner)).value == "next"
    handle = supervisor.handoff.call_args.args[1].pending
    assert (await handle.wait()).value == "actual"
    assert (await executor.run_file_task(task, owner)).value == "actual"
