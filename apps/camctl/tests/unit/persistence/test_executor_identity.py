"""用可控队列交错核对原申请的结果、撤回和原键重用，不启动线程。"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, create_autospec

import pytest
import pytest_asyncio

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (
    DbExecutorError, DbJob, DbJobKind, DbOutcome, DbOutcomeKind,
    DbPriority, WithdrawalStatus,
)
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

pytestmark = pytest.mark.asyncio


def _job(key, *, kind=DbJobKind.WRITE, value="original", outcome=DbOutcomeKind.COMPLETED):
    return DbJob(key, value, kind, DbPriority.BUSINESS,
                 lambda owned: DbOutcome(outcome, value=value) if kind is DbJobKind.WRITE else value)


@pytest_asyncio.fixture
async def controlled(monkeypatch):
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    executor = DbExecutor(lambda: owned, capacity=1)
    monkeypatch.setattr(executor, "_ensure_thread_locked", lambda: None)
    # 入队时限另用下方的真实 _await_admission 和可控超时检查。
    monkeypatch.setattr(executor, "_await_admission", AsyncMock())
    return executor, owned


@pytest.mark.parametrize("phase", ["admission", "queued", "dispatched", "awaiting_delivery"])
@pytest.mark.parametrize("duplicate_kind", [DbJobKind.WRITE, DbJobKind.READ])
async def test_duplicate_in_flight_key_preserves_original_operation(controlled, phase, duplicate_kind):
    executor, owned = controlled
    key = OperationKey("1" * 32)
    if phase == "admission":
        executor.submit_write(_job(OperationKey("2" * 32), value="blocker"))
    original = executor.submit_write(_job(key))
    if phase in ("dispatched", "awaiting_delivery"):
        job = executor._core.pop_next()
        if phase == "awaiting_delivery":
            executor._execute(job, owned)
    queued = executor._core.queued_count()
    submit = executor.submit_write if duplicate_kind is DbJobKind.WRITE else executor.submit_read
    with pytest.raises(DbExecutorError):
        submit(_job(key, kind=duplicate_kind, value="different input"))
    assert executor._core.queued_count() == queued
    if phase == "awaiting_delivery":
        assert (await original).value == "original"
    else:
        if phase in ("admission", "queued"):
            assert (await executor.withdraw(key)).status is WithdrawalStatus.WITHDRAWN
            assert (await original).kind is DbOutcomeKind.NOT_EXECUTED
        else:
            executor._execute(job, owned)
            assert (await original).value == "original"
    await executor.close()


async def test_unknown_result_allows_original_key_verification_after_delivery(controlled):
    executor, owned = controlled
    key = OperationKey("3" * 32)
    original = executor.submit_write(_job(key, outcome=DbOutcomeKind.UNKNOWN))
    executor._execute(executor._core.pop_next(), owned)
    assert (await original).kind is DbOutcomeKind.UNKNOWN
    verification = executor.submit_write(_job(key, value="verified"))
    executor._execute(executor._core.pop_next(), owned)
    assert (await verification).value == "verified"
    await executor.close()


async def test_late_admission_timeout_does_not_settle_reused_key(controlled, monkeypatch):
    executor, _ = controlled
    executor.submit_write(_job(OperationKey("4" * 32), value="blocker"))
    key = OperationKey("5" * 32)
    original = executor.submit_write(_job(key))
    old_admission = executor._core._admissions[-1]
    await executor.withdraw(key)
    assert (await original).kind is DbOutcomeKind.NOT_EXECUTED
    replacement = executor.submit_write(_job(key, value="replacement"))
    monkeypatch.setattr(asyncio, "wait_for", AsyncMock(side_effect=TimeoutError))
    await DbExecutor._await_admission(executor, old_admission)
    assert not replacement.done(), "旧入队截止不得结束复用原键的新申请"
    await executor.withdraw(key)
    assert (await replacement).kind is DbOutcomeKind.NOT_EXECUTED
    await executor.close()


async def test_late_withdrawal_cannot_cancel_new_operation_with_reused_key(controlled):
    executor, owned = controlled
    key = OperationKey("6" * 32)
    original = executor.submit_write(_job(key))
    executor._execute(executor._core.pop_next(), owned)
    assert (await original).value == "original"
    replacement = executor.submit_write(_job(key, value="replacement"))
    with pytest.raises(DbExecutorError):
        await executor.withdraw(key, expected_result=original)
    assert not replacement.done()
    executor._execute(executor._core.pop_next(), owned)
    assert (await replacement).value == "replacement"
    await executor.close()


async def test_result_delivery_clears_old_dispatch_before_original_key_reuse(controlled):
    executor, owned = controlled
    key = OperationKey("7" * 32)
    original = executor.submit_write(_job(key))
    executor._execute(executor._core.pop_next(), owned)
    assert (await original).value == "original"
    replacement = executor.submit_write(_job(key, value="replacement"))
    withdrawn = await executor.withdraw(key, expected_result=replacement)
    assert withdrawn.status is WithdrawalStatus.WITHDRAWN
    assert (await replacement).kind is DbOutcomeKind.NOT_EXECUTED
    assert executor._core.queued_count() == 0
    await executor.close()


async def test_business_cancel_withdraws_only_its_original_result(controlled):
    from camctl.persistence.business import BusinessDatabase

    executor, owned = controlled
    key = OperationKey("8" * 32)
    withdraw = create_autospec(executor.withdraw, wraps=executor.withdraw)
    original = asyncio.get_running_loop().create_future()
    executor.submit_write = create_autospec(executor.submit_write, return_value=original)
    executor.withdraw = withdraw

    async def cancel_original(key, **kwargs):
        original.set_result(DbOutcome(DbOutcomeKind.NOT_EXECUTED))
        return None

    withdraw.side_effect = cancel_original
    task = asyncio.create_task(BusinessDatabase(executor, owned.metadata).write(
        lambda connection: DbOutcome(DbOutcomeKind.COMPLETED), key=key))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    withdraw.assert_awaited_once_with(key, expected_result=original)
    await executor.close()
