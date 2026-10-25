"""业务数据库端口只提交完整块，取消后保留唯一实际结果。"""
import asyncio
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcome, DbOutcomeKind, ReadReceipt, WithdrawalResult, WithdrawalStatus
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ports():
    executor = create_autospec(DbExecutor, instance=True, spec_set=True)
    metadata = DatabaseMetadata('camctl', 'a'*32, 5, '/staging', '/ready', '/processing')
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True), metadata)
    return executor, owned


async def test_read_returns_value_from_business_job(ports):
    from camctl.persistence.business import BusinessDatabase
    executor, owned = ports
    future = asyncio.get_running_loop().create_future()
    future.set_result(ReadReceipt(value=('fact',)))
    executor.submit_read.return_value = future
    database = BusinessDatabase(executor, owned.metadata)
    callback = create_autospec(lambda owned: None)
    assert await database.read(callback) == ('fact',)
    job = executor.submit_read.call_args.args[0]
    assert job.priority.value == 'business'
    assert database.metadata is owned.metadata


async def test_write_cancel_waits_for_dispatched_actual_outcome(ports):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import db_outcome_for
    executor, owned = ports
    future = asyncio.get_running_loop().create_future()
    executor.submit_write.return_value = future
    key = OperationKey('b'*32)
    executor.withdraw.return_value = WithdrawalResult(key, WithdrawalStatus.DISPATCHED)
    task = asyncio.create_task(BusinessDatabase(executor, owned.metadata).write(lambda owned: None, key=key))
    await asyncio.sleep(0)
    task.cancel('original cancel')
    await asyncio.sleep(0)
    assert not task.done() and not future.cancelled()
    outcome = DbOutcome(DbOutcomeKind.COMPLETED, value=7)
    future.set_result(outcome)
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert db_outcome_for(caught.value) is outcome


async def test_known_completed_receipt_is_preserved_when_value_cannot_leave_worker(ports):
    from camctl.persistence.business import BusinessDatabase
    executor, owned = ports
    def submit(job):
        outcome = job.execute(owned)
        future = asyncio.get_running_loop().create_future()
        future.set_result(outcome)
        return future
    executor.submit_write.side_effect = submit
    outcome = DbOutcome(DbOutcomeKind.COMPLETED, value=owned.connection)
    from camctl.persistence.models import db_outcome_for, DbExecutorError
    with pytest.raises(DbExecutorError) as caught:
        await BusinessDatabase(executor, owned.metadata).write(lambda owned: outcome,
            key=OperationKey('c'*32))
    actual = db_outcome_for(caught.value)
    assert actual.kind is DbOutcomeKind.COMPLETED and actual.value is None
    assert actual.handoff_error is caught.value
    owned.connection.close.assert_not_called()


@pytest.fixture
def inline_port(ports):
    from support.business_database import inline_business_database
    return inline_business_database(ports[1])


async def test_inline_port_can_be_constructed_by_sync_unit_fixture(inline_port):
    assert await inline_port.read(lambda owned: owned.metadata) is inline_port.metadata


async def test_required_phase_applies_actual_result_before_propagating_first_cancel():
    from camctl.persistence.business import settle_required
    started = asyncio.Event()
    result = asyncio.get_running_loop().create_future()
    applied = []
    async def phase():
        started.set()
        value = await result
        applied.append(value)
        return value
    task = asyncio.create_task(settle_required(phase()))
    await started.wait()
    task.cancel('first cancel')
    await asyncio.sleep(0)
    task.cancel('second cancel')
    await asyncio.sleep(0)
    assert not task.done() and not result.cancelled() and not applied
    result.set_result('saved')
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ('first cancel',) and applied == ['saved']


async def test_required_phase_actual_failure_wins_over_waiter_cancel():
    from camctl.persistence.business import settle_required
    started = asyncio.Event()
    result = asyncio.get_running_loop().create_future()
    failure = RuntimeError('apply failed')
    async def phase():
        started.set()
        await result
        raise failure
    task = asyncio.create_task(settle_required(phase()))
    await started.wait()
    task.cancel('cancel while required')
    await asyncio.sleep(0)
    result.set_result(None)
    with pytest.raises(RuntimeError) as caught:
        await task
    assert caught.value is failure
    assert isinstance(failure.__cause__, asyncio.CancelledError)
    assert failure.__cause__.args == ('cancel while required',)


async def test_required_phase_inner_cancellation_ends_wait():
    from camctl.persistence.business import settle_required
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settle_required(future)


async def test_write_cancel_withdraws_unstarted_job_and_attaches_not_executed(ports):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import db_outcome_for
    executor, owned = ports
    future = asyncio.get_running_loop().create_future()
    executor.submit_write.return_value = future
    key = OperationKey('d'*32)
    async def withdraw(actual_key):
        assert actual_key == key
        future.set_result(DbOutcome(DbOutcomeKind.NOT_EXECUTED))
        return WithdrawalResult(key, WithdrawalStatus.WITHDRAWN)
    executor.withdraw.side_effect = withdraw
    callback = create_autospec(lambda owned: None)
    task = asyncio.create_task(BusinessDatabase(executor, owned.metadata).write(callback, key=key))
    await asyncio.sleep(0)
    task.cancel('queued cancel')
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert db_outcome_for(caught.value).kind is DbOutcomeKind.NOT_EXECUTED
    callback.assert_not_called()


async def test_write_delivery_race_retains_original_future_and_typed_receipt(ports):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbExecutorError, db_outcome_for
    from camctl.persistence.transaction import WriteReceipt, _attach_write_receipt, write_receipt_for
    executor, owned = ports
    future = asyncio.get_running_loop().create_future()
    executor.submit_write.return_value = future
    key = OperationKey('e'*32)
    delivered = asyncio.Event()
    async def withdraw(key):
        delivered.set()
        raise DbExecutorError('已完成，结果等待投递')
    executor.withdraw.side_effect = withdraw
    task = asyncio.create_task(BusinessDatabase(executor, owned.metadata).write(lambda owned: None, key=key))
    await asyncio.sleep(0)
    task.cancel('first cancel')
    await delivered.wait()
    task.cancel('later cancel')
    await asyncio.sleep(0)
    control = asyncio.CancelledError('kernel cancel')
    receipt = WriteReceipt('completed', result=8, error=control)
    _attach_write_receipt(control, receipt)
    outcome = DbOutcome(DbOutcomeKind.COMPLETED, value=8, control=control)
    future.set_result(outcome)
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value is control and db_outcome_for(control) is outcome
    assert write_receipt_for(control) is receipt
    assert isinstance(control.__cause__, asyncio.CancelledError)
    assert control.__cause__.args == ('first cancel',)


@pytest.mark.parametrize('error_type', ['runtime', 'directory'])
async def test_open_preserves_factory_error_and_closes_executor(ports, monkeypatch, error_type):
    from camctl.persistence import business
    from camctl.persistence.models import DbReadError
    from camctl.persistence.runtime import RuntimeLibraryError, DirectoryBindingError
    executor, _ = ports
    failure = (RuntimeLibraryError if error_type == 'runtime' else DirectoryBindingError)('binding')
    wrapped = DbReadError('open failed')
    wrapped.__cause__ = failure
    future = asyncio.get_running_loop().create_future()
    future.set_result(ReadReceipt(error=wrapped))
    executor.submit_read.return_value = future
    constructor = create_autospec(DbExecutor, return_value=executor)
    monkeypatch.setattr(business, 'DbExecutor', constructor)
    factory = create_autospec(lambda: None)
    with pytest.raises(type(failure)) as caught:
        await business.BusinessDatabase.open(factory)
    assert caught.value is failure
    executor.close.assert_awaited_once_with()


@pytest.mark.parametrize('kind', list(DbOutcomeKind))
async def test_direct_live_write_value_is_scrubbed_without_changing_receipt(ports, kind):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbExecutorError, db_outcome_for
    executor, owned = ports
    def submit(job):
        future = asyncio.get_running_loop().create_future()
        future.set_result(job.execute(owned))
        return future
    executor.submit_write.side_effect = submit
    with pytest.raises(DbExecutorError) as caught:
        await BusinessDatabase(executor, owned.metadata).write(
            lambda owned: DbOutcome(kind, value=owned), key=OperationKey('f'*32))
    outcome = db_outcome_for(caught.value)
    assert outcome.kind is kind and outcome.value is None


@pytest.mark.parametrize('value_kind', ['connection', 'cursor', 'iterator', 'async_iterator', 'awaitable'])
async def test_read_rejects_direct_live_values_in_worker(inline_port, ports, value_kind):
    from camctl.persistence.models import DbExecutorError
    owned = ports[1]
    async def delayed():
        return 1
    async def streamed():
        yield 1
    value = {'connection': owned.connection,
             'cursor': create_autospec(sqlite3.Cursor, instance=True),
             'iterator': iter([1]), 'async_iterator': None, 'awaitable': None}[value_kind]
    if value_kind == 'awaitable':
        value = delayed()
    elif value_kind == 'async_iterator':
        value = streamed()
    try:
        with pytest.raises(DbExecutorError):
            await inline_port.read(lambda owned: value)
    finally:
        if value_kind == 'awaitable':
            value.close()
        elif value_kind == 'async_iterator':
            await value.aclose()


async def test_cancelled_close_waits_for_shared_actual_close(ports):
    from camctl.persistence.business import BusinessDatabase
    executor, owned = ports
    entered = asyncio.Event()
    finish = asyncio.get_running_loop().create_future()
    async def close():
        entered.set()
        await finish
    executor.close.side_effect = close
    database = BusinessDatabase(executor, owned.metadata)
    task = asyncio.create_task(database.close())
    await entered.wait()
    task.cancel('close cancellation')
    await asyncio.sleep(0)
    assert not task.done() and not finish.cancelled()
    finish.set_result(None)
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ('close cancellation',)
    await database.close()
    executor.close.assert_awaited_once_with()
