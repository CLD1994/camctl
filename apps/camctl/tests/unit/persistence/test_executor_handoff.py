"""提交身份在排队、执行和结果交付期间唯一；只用内存协作者。"""

import asyncio
import sqlite3
import threading
from unittest.mock import create_autospec

import pytest
import pytest_asyncio

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (
    DbEnqueueTimeoutError,
    DbExecutorError,
    DbJob,
    DbJobKind,
    DbOutcome,
    DbOutcomeKind,
    DbPriority,
    ReadReceipt,
    WithdrawalStatus,
)
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

pytestmark = pytest.mark.asyncio


@pytest.fixture
def owned():
    return OwnedConnection(
        create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata('camctl', 'a' * 32, 5, '/staging', '/ready', '/processing'),
    )


@pytest_asyncio.fixture
async def executor(owned, monkeypatch):
    executor = DbExecutor(lambda: owned, capacity=1)
    monkeypatch.setattr(executor, '_ensure_thread_locked', lambda: None)
    return executor


@pytest_asyncio.fixture
async def expire_admissions(monkeypatch):
    """以明确同步点发出入队超时，不等待真实时钟。"""
    expired = asyncio.Event()

    async def wait_for_timeout(gate, timeout):
        await expired.wait()
        gate.cancel()
        raise TimeoutError

    monkeypatch.setattr(asyncio, 'wait_for', wait_for_timeout)
    return expired.set


def _job(kind, value, *, key=OperationKey('1' * 32), error=None):
    def execute(owned):
        if error is not None:
            raise error
        return value if kind is DbJobKind.READ else DbOutcome(DbOutcomeKind.COMPLETED, value=value)

    return DbJob(key, str(value), kind, DbPriority.BUSINESS, execute)


def _submit(executor, job):
    submit = executor.submit_read if job.kind is DbJobKind.READ else executor.submit_write
    return submit(job)


def _dispatch(executor):
    job = executor._core.pop_next()
    assert job is not None
    granted = executor._core.admit_next_waiting()
    if granted is not None:
        executor._schedule_grant(granted)
    return job


async def _execute_and_deliver(executor, job, owned):
    executor._execute(job, owned)
    await asyncio.sleep(0)


@pytest.mark.parametrize('first_kind', list(DbJobKind))
@pytest.mark.parametrize('second_kind', list(DbJobKind))
@pytest.mark.parametrize('stage', ['queued', 'executing', 'awaiting_delivery'])
async def test_duplicate_key_preserves_original_job_and_result(
    executor, owned, first_kind, second_kind, stage,
):
    original = _submit(executor, _job(first_kind, 'original'))
    job = None if stage == 'queued' else _dispatch(executor)
    if stage == 'awaiting_delivery':
        executor._execute(job, owned)
    queued = executor._core.queued_count()

    with pytest.raises(DbExecutorError):
        _submit(executor, _job(second_kind, 'replacement'))

    assert executor._results[OperationKey('1' * 32)] is original
    assert executor._core.queued_count() == queued
    assert not executor._core._admissions
    assert not original.done()
    if stage != 'awaiting_delivery':
        await _execute_and_deliver(executor, job or _dispatch(executor), owned)
    else:
        await asyncio.sleep(0)
    assert original.done() and original.result().value == 'original'
    assert not executor._core.has_queued()
    # 原结果已经交付后，原键可重新提交以核实幂等结果。
    retry = _submit(executor, _job(second_kind, 'verified'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_cancelled_result_still_reserves_key_until_actual_delivery(executor, owned, kind):
    original = _submit(executor, _job(kind, 'original'))
    original.cancel()
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'replacement'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert original.cancelled()
    retry = _submit(executor, _job(kind, 'verified'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('first_kind', list(DbJobKind))
@pytest.mark.parametrize('second_kind', list(DbJobKind))
async def test_duplicate_waiting_for_admission_is_rejected(
    executor, first_kind, second_kind, expire_admissions,
):
    blocker = _submit(executor, _job(DbJobKind.READ, 'blocker', key=OperationKey('2' * 32)))
    original = _submit(executor, _job(first_kind, 'original'))
    await asyncio.sleep(0)
    admission = executor._core._admissions[0]
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(second_kind, 'replacement'))
    assert executor._results[admission.job.key] is original
    assert list(executor._core._admissions) == [admission]
    assert admission.state == admission.PENDING
    await executor.close()
    expire_admissions()
    await asyncio.sleep(0)
    assert blocker.result().kind is DbOutcomeKind.NOT_EXECUTED
    assert original.result().kind is DbOutcomeKind.NOT_EXECUTED
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(second_kind, 'after close'))


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_withdrawn_waiter_late_timeout_preserves_reused_key(
    executor, owned, kind, expire_admissions,
):
    blocker_key = OperationKey('2' * 32)
    _submit(executor, _job(DbJobKind.READ, 'blocker', key=blocker_key))
    original = _submit(executor, _job(kind, 'withdrawn'))
    await asyncio.sleep(0)
    withdrawal = await executor.withdraw(OperationKey('1' * 32))
    assert withdrawal.status is WithdrawalStatus.WITHDRAWN
    assert original.result().kind is DbOutcomeKind.NOT_EXECUTED
    await executor.withdraw(blocker_key)
    retry = _submit(executor, _job(kind, 'verified'))
    expire_admissions()
    await asyncio.sleep(0)
    assert not retry.done()
    assert executor._results[OperationKey('1' * 32)] is retry
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_admission_timeout_releases_key_without_late_execution(
    executor, owned, kind, expire_admissions,
):
    blocker_key = OperationKey('2' * 32)
    _submit(executor, _job(DbJobKind.READ, 'blocker', key=blocker_key))
    original = _submit(executor, _job(kind, 'timed out'))
    await asyncio.sleep(0)
    expire_admissions()
    await asyncio.sleep(0)
    assert original.done()
    assert original.result().kind is DbOutcomeKind.NOT_EXECUTED
    assert isinstance(original.result().error, DbEnqueueTimeoutError)
    await executor.withdraw(blocker_key)
    assert not executor._core.has_queued()
    retry = _submit(executor, _job(kind, 'verified'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_admitted_waiter_late_timeout_keeps_actual_result(
    executor, owned, kind, expire_admissions,
):
    blocker = _submit(executor, _job(DbJobKind.READ, 'blocker', key=OperationKey('2' * 32)))
    original = _submit(executor, _job(kind, 'original'))
    await asyncio.sleep(0)
    first_job = _dispatch(executor)
    expire_admissions()
    await asyncio.sleep(0)
    assert not original.done()
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'replacement'))
    await _execute_and_deliver(executor, first_job, owned)
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert blocker.result().value == 'blocker'
    assert original.result().value == 'original'


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_queued_withdrawal_releases_key_for_new_application(executor, owned, kind):
    original = _submit(executor, _job(kind, 'withdrawn'))
    assert (await executor.withdraw(OperationKey('1' * 32))).status is WithdrawalStatus.WITHDRAWN
    assert original.done() and original.result().kind is DbOutcomeKind.NOT_EXECUTED
    retry = _submit(executor, _job(kind, 'verified'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('kind', list(DbJobKind))
async def test_withdraw_dispatched_keeps_key_reserved(executor, owned, kind):
    original = _submit(executor, _job(kind, 'original'))
    job = _dispatch(executor)
    assert (await executor.withdraw(job.key)).status is WithdrawalStatus.DISPATCHED
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'replacement'))
    await _execute_and_deliver(executor, job, owned)
    assert original.done() and original.result().value == 'original'


@pytest.mark.parametrize('kind', list(DbJobKind))
@pytest.mark.parametrize('stage', ['queued', 'dispatched'])
@pytest.mark.parametrize('close_error', [False, True])
async def test_close_rejects_new_submission_and_preserves_original_receipt(
    executor, owned, monkeypatch, kind, stage, close_error,
):
    original = _submit(executor, _job(kind, 'original'))
    job = _dispatch(executor) if stage == 'dispatched' else None
    executor._thread = create_autospec(threading.Thread, instance=True, spec_set=True)
    joined = asyncio.get_running_loop().create_future()
    join = create_autospec(executor._loop.run_in_executor, return_value=joined)
    monkeypatch.setattr(executor._loop, 'run_in_executor', join)
    closing = asyncio.create_task(executor.close())
    await asyncio.sleep(0)
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'replacement'))
    if job is not None:
        assert executor._results[job.key] is original and not original.done()
        await _execute_and_deliver(executor, job, owned)
        assert original.result().value == 'original'
    else:
        assert original.done() and original.result().kind is DbOutcomeKind.NOT_EXECUTED
    error = RuntimeError('worker shutdown') if close_error else None
    executor._worker_error = error
    joined.set_result(None)
    if close_error:
        with pytest.raises(RuntimeError) as caught:
            await closing
        assert caught.value is error
    else:
        await closing
    join.assert_called_once_with(None, executor._thread.join)
    assert executor._closed and not executor._results
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'after close'))


@pytest.mark.parametrize('kind', list(DbJobKind))
@pytest.mark.parametrize('failure', ['callback', 'factory'])
async def test_failure_pending_delivery_preserves_original_handle(executor, owned, kind, failure):
    error = RuntimeError('original failure')
    original = _submit(executor, _job(kind, 'failed', error=error))
    job = _dispatch(executor)
    if failure == 'callback':
        executor._execute(job, owned)
    else:
        executor._deliver_factory_failure(job, error)
    with pytest.raises(DbExecutorError):
        _submit(executor, _job(kind, 'replacement'))
    await asyncio.sleep(0)
    assert original.done()
    receipt = original.result()
    if kind is DbJobKind.READ:
        assert isinstance(receipt, ReadReceipt) and receipt.error.__cause__ is error
    else:
        assert receipt.error is error
    retry = _submit(executor, _job(kind, 'verified'))
    await _execute_and_deliver(executor, _dispatch(executor), owned)
    assert retry.done() and retry.result().value == 'verified'


@pytest.mark.parametrize('kind', list(DbJobKind))
@pytest.mark.parametrize('completion', ['success', 'callback_failure', 'factory_failure'])
async def test_delivered_result_allows_same_key_queued_application_to_be_withdrawn(
    executor, owned, kind, completion,
):
    error = RuntimeError('original failure') if completion != 'success' else None
    original = _submit(executor, _job(kind, 'original', error=error))
    job = _dispatch(executor)
    if completion == 'factory_failure':
        executor._deliver_factory_failure(job, error)
    else:
        executor._execute(job, owned)
    # worker发出交付通知后可能暂未继续；loop已能交付结果并接收同键新申请。
    receipt = await original
    assert receipt.value == ('original' if completion == 'success' else None)
    if error is not None:
        assert receipt.error is error or receipt.error.__cause__ is error
    execute = create_autospec(lambda owned: None)
    new_job = DbJob(job.key, 'new application', kind, DbPriority.BUSINESS, execute)
    retry = _submit(executor, new_job)

    withdrawal = await executor.withdraw(job.key)

    assert withdrawal.status is WithdrawalStatus.WITHDRAWN
    assert retry.done() and retry.result().kind is DbOutcomeKind.NOT_EXECUTED
    assert executor._core.pop_next() is None
    execute.assert_not_called()


@pytest.mark.parametrize('kind', list(DbJobKind))
@pytest.mark.parametrize('completion', ['success', 'callback_failure', 'factory_failure'])
async def test_worker_keeps_original_withdrawal_scope_until_loop_delivers_result(
    executor, owned, monkeypatch, kind, completion,
):
    error = RuntimeError('original failure') if completion != 'success' else None
    if completion == 'factory_failure':
        monkeypatch.setattr(executor, '_factory', create_autospec(lambda: None, side_effect=error))
    original = _submit(executor, _job(kind, 'original', error=error))
    # 在当前进程内驱动worker到退出，交付通知由loop下一轮处理。
    executor._stopping = True
    executor._run()

    assert not original.done()
    withdrawal = await executor.withdraw(OperationKey('1' * 32))
    assert withdrawal.status is WithdrawalStatus.DISPATCHED
    receipt = await original
    assert receipt.value == ('original' if completion == 'success' else None)
    with pytest.raises(DbExecutorError):
        await executor.withdraw(OperationKey('1' * 32))
