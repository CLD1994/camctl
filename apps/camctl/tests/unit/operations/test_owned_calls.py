"""工具拥有范围在取消后交付原结果，不开始下一项副作用。"""

import asyncio

import pytest

from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.process import execute_tool
from .test_process import FakeProcess, FakeStop, _spec, _ready, _within

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancel_holds_actual_result_until_original_save_finishes(save_fails):
    process = FakeProcess(b"actual response")
    save_entered, save_release = asyncio.Event(), asyncio.Event()
    saved = []
    failure = ValueError("original state save failed")

    @owned_tool_call
    async def operation():
        raw = await execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(process))
        save_entered.set()
        await save_release.wait()
        if save_fails:
            raise failure
        saved.append(raw)

    task = asyncio.create_task(operation())
    try:
        await _within(lambda: process._exited is not None)
        task.cancel()
        await _within(lambda: process.terminate_requests == 1)
        process.exit(exit_code=3)
        await save_entered.wait()
        task.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
        save_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        if save_fails:
            assert caught.value.__cause__ is failure
        else:
            assert len(saved) == 1 and saved[0].output == b"actual response"
            assert saved[0].error == "cancelled" and saved[0].exit.exit_code == 3
    finally:
        process.exit(exit_code=0)
        save_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_nested_owner_delivers_current_result_before_next_operation_can_begin():
    process = FakeProcess(b"original response")
    saved, started = [], []

    @owned_tool_call
    async def call_and_save():
        raw = await execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(process))
        saved.append(raw)

    @owned_tool_call
    async def operation():
        await call_and_save()
        started.append("next attempt")

    task = asyncio.create_task(operation())
    await _within(lambda: process._exited is not None)
    task.cancel()
    await _within(lambda: process.terminate_requests == 1)
    process.exit(exit_code=0)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(saved) == 1 and started == []
