"""同一原保存只执行一次，状态应用与真实结果一起交付。"""

import asyncio

import pytest

from camctl.persistence import business
from camctl.persistence.models import DbExecutorError, DbOutcome, DbOutcomeKind, _attach_db_outcome, db_outcome_for
from camctl.persistence.transaction import WriteReceipt, _attach_write_receipt, write_receipt_for

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
    assert raised.value is failure and attempts == ["original"]
    shared.reset()
    with pytest.raises(RuntimeError) as raised:
        await shared.run(save_and_apply)
    assert raised.value is failure and attempts == ["original", "original"]


async def test_completed_save_requires_explicit_next_stage_before_replacing_result():
    shared = business.SharedSave()

    async def intent():
        return "intent"

    async def result():
        return "result"

    assert await shared.run(intent) == "intent"
    assert await shared.run(result) == "intent"
    shared.reset()
    assert await shared.run(result) == "result"


async def test_new_stage_cannot_reset_an_in_progress_original_save():
    shared = business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()

    async def save_and_apply():
        entered.set()
        await release.wait()
        return "original"

    first = asyncio.create_task(shared.run(save_and_apply))
    await entered.wait()
    with pytest.raises(DbExecutorError):
        shared.reset()
    release.set()
    assert await first == "original"


async def test_explicit_recovery_retries_failed_save_once_for_concurrent_waiters():
    shared = business.SharedSave()
    calls = []
    failure = RuntimeError("原结果未知")
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed():
        calls.append("failed")
        raise failure

    with pytest.raises(RuntimeError):
        await shared.run(failed)
    shared.retry_failed()

    async def recovered():
        calls.append("recovered")
        entered.set()
        await release.wait()
        return "saved"

    async def resume():
        shared.retry_failed()
        return await shared.run(recovered)

    first = asyncio.create_task(resume())
    await entered.wait()
    second = asyncio.create_task(resume())
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["saved", "saved"]
    shared.retry_failed()
    assert await shared.run(failed) == "saved"
    assert calls == ["failed", "recovered"]


async def test_explicit_recovery_can_retry_original_task_cancellation():
    shared = business.SharedSave()

    async def canceled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await shared.run(canceled)
    shared.retry_failed()

    async def recovered():
        return "saved"

    assert await shared.run(recovered) == "saved"


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("cancel_count", [0, 1, 2])
async def test_actual_control_error_keeps_original_receipts_for_all_waiters(error_type, cancel_count):
    shared = business.SharedSave()
    entered, release = asyncio.Event(), asyncio.Event()
    actual = error_type("原保存控制错误")
    outcome = DbOutcome(DbOutcomeKind.COMPLETED, value="saved")
    receipt = WriteReceipt("completed", result="saved")
    _attach_db_outcome(actual, outcome)
    _attach_write_receipt(actual, receipt)
    applied = []

    async def save_and_apply():
        entered.set()
        await release.wait()
        applied.append("saved")
        raise actual

    async def receive():
        try:
            await shared.run(save_and_apply)
        except BaseException as error:
            return error

    first = asyncio.create_task(receive())
    await entered.wait()
    second = asyncio.create_task(receive())
    await asyncio.sleep(0)
    for n in range(cancel_count):
        first.cancel(f"等待者取消{n}")
        await asyncio.sleep(0)
    assert not first.done() and not second.done() and not applied
    release.set()
    results = await asyncio.gather(first, second)
    late = await receive()

    assert results[0] is actual and results[1] is actual and late is actual
    assert db_outcome_for(late) is outcome and write_receipt_for(late) is receipt
    assert applied == ["saved"]
    if cancel_count:
        assert actual.__cause__.args == ("等待者取消0",)

    shared.retry_failed()

    async def recovered():
        return "recovered"

    assert await shared.run(recovered) == "recovered"


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
