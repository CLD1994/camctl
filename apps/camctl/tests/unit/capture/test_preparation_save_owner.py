"""正常准备和恢复加入原保存阶段，实际结果只应用一次。"""

import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.capture import preparation
from camctl.capture.preparation import (
    PendingPreparation, _end, _save_owner, advance_preparation,
    resume_preparation_saves,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome
from camctl.persistence.repositories.scheduling import ReservePreparationOutcome, ReservePreparationResult
from camctl.persistence.models import (
    DbOutcome, DbOutcomeKind, _attach_db_outcome, db_outcome_for,
)

from .test_preparation import _world

pytestmark = pytest.mark.asyncio

_PHASES = ("intent", "result", "reservation", "release", "ending")


def _saving_world(mocker, phase):
    runtime, _action, ticket = _world(mocker)
    async def write(callback, **kwargs):
        return callback(runtime.owned)
    runtime.database.write = create_autospec(runtime.database.write, side_effect=write)
    cache = runtime.preparation_cache
    owner = None
    if phase in ("intent", "result"):
        owner = PendingPreparation(1, 7, runtime.session_key, object(),
            runtime.evidence, runtime.preparation_driver)
        if phase == "result":
            owner.ticket, owner.finish = ticket, object()
        cache.owners[1] = owner
        normal = lambda: _save_owner(owner, runtime.database, runtime.operations)
    elif phase == "reservation":
        preparation._activity.return_value = None
        runtime.scheduling.reserve_preparation.return_value = DbOutcome(
            DbOutcomeKind.COMPLETED, ReservePreparationResult(ReservePreparationOutcome.REJECTED, reason="device_busy"))
        normal = lambda: advance_preparation(runtime, 1)
    else:
        runtime.capture.release_occupancy.return_value = DbOutcome(
            DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))
        runtime.scheduling.fail_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
        normal = lambda: _end(runtime, 1,
            "release" if phase == "release" else "fail_preparation", object())

    async def resume():
        return await resume_preparation_saves(runtime.database, cache,
            operations=runtime.operations, scheduling=runtime.scheduling, capture=runtime.capture)

    return runtime, owner, normal, resume


def _pause_writes(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    original = runtime.database.write

    async def write(callback, **kwargs):
        writes.append(kwargs["key"])
        entered.set()
        await release.wait()
        return await original(callback, **kwargs)

    runtime.database.write = write
    return entered, release, writes


async def _allow_join():
    # 外层必要阶段和原保存阶段分别调度；只推进事件循环，不推进业务时钟。
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize("cancel_waiter", [None, "normal", "resume"])
async def test_normal_and_resume_share_original_save_until_late_result(mocker, phase, cancel_waiter):
    runtime, owner, normal, resume = _saving_world(mocker, phase)
    entered, release, writes = _pause_writes(runtime)
    first = asyncio.create_task(normal())
    await entered.wait()
    recovery = asyncio.create_task(resume())
    await _allow_join()
    cancelled = first if cancel_waiter == "normal" else recovery if cancel_waiter == "resume" else None
    if cancelled is not None:
        cancelled.cancel("stop original waiter")
        await _allow_join()
    waiting_for_actual = cancelled is None or not cancelled.done()
    release.set()
    results = await asyncio.gather(first, recovery, return_exceptions=True)

    assert len(writes) == 1, "原申请在真实回执返回前只能提交一次"
    assert waiting_for_actual
    for task, result in zip((first, recovery), results):
        if task is cancelled:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert not isinstance(result, BaseException), result
    if owner is not None:
        assert owner.ticket is not None
        assert owner.saved is (phase == "result")
    else:
        assert not runtime.preparation_cache.reservations
        assert not runtime.preparation_cache.endings
        assert not runtime.preparation_cache.request_saves


async def test_actual_result_after_intent_phase_starts_its_own_required_save(mocker):
    runtime, owner, normal, resume = _saving_world(mocker, "intent")
    await normal()
    assert owner.ticket is not None and not owner.saved
    owner.actual = runtime.preparation_driver.prepare_capture.return_value
    owner.returned_at = 1000

    await asyncio.gather(normal(), resume())

    assert owner.saved and owner.successful
    runtime.operations.begin_attempt.assert_called_once()
    runtime.operations.finish_attempt.assert_called_once()
    assert runtime.operations.finish_attempt.call_args.args[1] == owner.result_key


@pytest.mark.parametrize("phase", _PHASES)
async def test_completed_notification_error_applies_original_before_propagation(mocker, phase):
    runtime, owner, normal, resume = _saving_world(mocker, phase)
    original = runtime.database.write
    failure = RuntimeError("结果已提交，通知交付失败")
    writes = []

    async def write(callback, **kwargs):
        writes.append(kwargs["key"])
        receipt = await original(callback, **kwargs)
        _attach_db_outcome(failure, receipt)
        raise failure

    runtime.database.write = write
    with pytest.raises(RuntimeError) as caught:
        await normal()
    assert caught.value is failure
    if owner is not None:
        assert owner.ticket is not None
        assert owner.saved is (phase == "result")
    else:
        assert not runtime.preparation_cache.reservations
        assert not runtime.preparation_cache.endings
    await resume()
    assert len(writes) == 1


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED])
async def test_uncommitted_or_unknown_keeps_original_request_key_and_classification(mocker, phase, kind):
    runtime, owner, normal, resume = _saving_world(mocker, phase)
    receipt = DbOutcome(kind, error=RuntimeError("原保存未可靠完成"))
    keys = []

    async def write(callback, **kwargs):
        keys.append(kwargs["key"])
        return receipt

    runtime.database.write = write
    with pytest.raises(ConsistencyError) as caught:
        await normal()
    assert db_outcome_for(caught.value) is receipt
    cache = runtime.preparation_cache
    if owner is not None:
        assert cache.owners[1] is owner and not owner.saved
        assert owner.ticket is None if phase == "intent" else owner.ticket is not None
        original = owner
    else:
        held = cache.reservations if phase == "reservation" else cache.endings
        original = held[1]
    with pytest.raises(ConsistencyError):
        await resume()
    assert len(keys) == 2 and keys[0] == keys[1]
    if owner is None:
        assert held[1] is original


@pytest.mark.parametrize("phase", ["reservation", "ending"])
async def test_old_snapshot_does_not_resubmit_or_delete_replacement_request(mocker, phase):
    runtime, _owner, _normal, resume = _saving_world(mocker, phase)
    cache = runtime.preparation_cache
    held = cache.reservations if phase == "reservation" else cache.endings
    def request():
        return ((object(), new_operation_key()) if phase == "reservation"
                else ("fail_preparation", object(), new_operation_key()))
    held[1], held[2] = request(), request()
    original_second = held[2]
    entered, release, writes = _pause_writes(runtime)
    task = asyncio.create_task(resume())
    await entered.wait()
    replacement = request()
    held[2] = replacement
    release.set()
    await task

    assert original_second[-1] not in writes
    assert held[2] is replacement
    await resume()
    assert not held and writes[-1] == replacement[-1]
    assert not cache.request_saves


@pytest.mark.parametrize("phase", ["reservation", "ending"])
async def test_late_original_completion_keeps_new_request_for_same_action(mocker, phase):
    runtime, _owner, normal, resume = _saving_world(mocker, phase)
    entered, release, writes = _pause_writes(runtime)
    task = asyncio.create_task(normal())
    await entered.wait()
    cache = runtime.preparation_cache
    held = cache.reservations if phase == "reservation" else cache.endings
    original = held[1]
    replacement = ((object(), new_operation_key()) if phase == "reservation"
                   else ("fail_preparation", object(), new_operation_key()))
    held[1] = replacement
    release.set()
    await task

    assert held[1] is replacement and writes == [original[-1]]
    await resume()
    assert not held and writes == [original[-1], replacement[-1]]
    assert not cache.request_saves


@pytest.mark.parametrize("phase", ["reservation", "ending"])
async def test_replaced_request_before_stage_starts_releases_stage_registration(mocker, phase):
    runtime, _owner, _normal, _resume = _saving_world(mocker, phase)
    cache = runtime.preparation_cache
    held = cache.reservations if phase == "reservation" else cache.endings
    original = ((object(), new_operation_key()) if phase == "reservation"
                else ("fail_preparation", object(), new_operation_key()))
    held[1] = original
    task = asyncio.create_task(preparation._save_cached_request(
        runtime.database, cache, 1, original=original, ending=phase != "reservation",
        scheduling=runtime.scheduling, capture=runtime.capture))
    await asyncio.sleep(0)
    assert original[-1] in cache.request_saves
    replacement = ((object(), new_operation_key()) if phase == "reservation"
                   else ("fail_preparation", object(), new_operation_key()))
    held[1] = replacement

    assert await task is None
    assert held[1] is replacement
    assert not cache.request_saves
    runtime.database.write.assert_not_called()
