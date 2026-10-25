"""同一原保存只执行一次，状态应用与真实结果一起交付。"""

import asyncio

import pytest

from camctl.persistence import business

pytestmark = pytest.mark.asyncio


async def test_concurrent_waiters_receive_one_save_after_state_application():
    shared = business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()
    applied = []

    async def save_and_apply():
        entered.set()
        await release.wait()
        applied.append("saved")
        return "original"

    first = asyncio.create_task(shared.run(save_and_apply))
    await entered.wait()
    second = asyncio.create_task(shared.run(save_and_apply))
    await asyncio.sleep(0)
    assert not applied and not first.done() and not second.done()
    release.set()

    assert await asyncio.gather(first, second) == ["original", "original"]
    assert applied == ["saved"]


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_waiter_waits_for_shared_save_without_canceling_other_waiter(cancel_count):
    shared = business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()
    applied = []

    async def save_and_apply():
        entered.set()
        await release.wait()
        applied.append("saved")
        return "original"

    first = asyncio.create_task(shared.run(save_and_apply))
    await entered.wait()
    second = asyncio.create_task(shared.run(save_and_apply))
    await asyncio.sleep(0)
    for _ in range(cancel_count):
        first.cancel()
        await asyncio.sleep(0)
    assert not first.done() and not applied
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == "original" and applied == ["saved"]


async def test_actual_save_failure_is_shared_before_later_original_request_retry():
    shared = business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("原保存未完成")
    attempts = []

    async def save_and_apply():
        attempts.append("original")
        entered.set()
        await release.wait()
        raise failure

    first = asyncio.create_task(shared.run(save_and_apply))
    await entered.wait()
    second = asyncio.create_task(shared.run(save_and_apply))
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == [failure, failure] and attempts == ["original"]
    assert isinstance(failure.__cause__, asyncio.CancelledError)

    with pytest.raises(RuntimeError) as raised:
        await shared.run(save_and_apply)
    assert raised.value is failure and attempts == ["original", "original"]


async def test_completed_save_allows_same_owner_to_start_its_next_stage():
    shared = business.SharedSave()

    async def intent():
        return "intent"

    async def result():
        return "result"

    assert await shared.run(intent) == "intent"
    assert await shared.run(result) == "result"


async def test_different_owners_do_not_wait_for_each_others_save():
    first, second = business.SharedSave(), business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held():
        entered.set()
        await release.wait()

    async def independent():
        return "saved"

    pending = asyncio.create_task(first.run(held))
    await entered.wait()
    assert await second.run(independent) == "saved"
    assert not pending.done()
    release.set()
    await pending
