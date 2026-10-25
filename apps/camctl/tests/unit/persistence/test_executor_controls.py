"""执行器在本进程内驱动单项worker分支，不启动线程。"""
import asyncio
import sqlite3
import threading
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbJob, DbJobKind, DbOutcome, DbOutcomeKind, DbPriority, ReadReceipt
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.persistence.transaction import WriteReceipt, _attach_write_receipt

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('kind', ['rolled_back', 'unknown', 'completed'])
async def test_worker_consumes_control_and_delivers_actual_receipt(kind):
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata('camctl', 'a'*32, 5, '/staging', '/ready', '/processing'))
    executor = DbExecutor(lambda: owned)
    control = KeyboardInterrupt('worker control')
    _attach_write_receipt(control, WriteReceipt(kind, result=9, error=control))
    def execute(owned):
        raise control
    job = DbJob(OperationKey('a'*32), 'control', DbJobKind.WRITE, DbPriority.BUSINESS, execute)
    future = asyncio.get_running_loop().create_future()
    executor._results[job.key] = future
    escaped = None
    try:
        keep = executor._execute(job, owned)
    except BaseException as error:
        escaped = error
    assert escaped is None, 'worker不能退出并遗留Future'
    outcome = await future
    assert outcome.kind is DbOutcomeKind(kind) and outcome.control is control
    assert keep is (None if kind == 'unknown' else owned)


@pytest.fixture
def owned():
    return OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata('camctl', 'a'*32, 5, '/staging', '/ready', '/processing'))


@pytest.mark.parametrize('priority,kind,observed', [
    (DbPriority.BUSINESS, DbOutcomeKind.COMPLETED, True),
    (DbPriority.SNAPSHOT, DbOutcomeKind.COMPLETED, False),
    (DbPriority.BUSINESS, DbOutcomeKind.ROLLED_BACK, False),
    (DbPriority.BUSINESS, DbOutcomeKind.UNKNOWN, False),
])
async def test_confirmed_observer_only_receives_committed_business_write(owned, priority, kind, observed):
    observer = create_autospec(lambda owned, outcome, key: None)
    executor = DbExecutor(lambda: owned, confirmed_write_observer=observer)
    outcome = DbOutcome(kind, value=8)
    job = DbJob(OperationKey('b'*32), 'observer', DbJobKind.WRITE, priority, lambda owned: outcome)
    future = asyncio.get_running_loop().create_future()
    executor._results[job.key] = future
    executor._execute(job, owned)
    assert await future is outcome
    if observed:
        observer.assert_called_once_with(owned, outcome, job.key)
    else:
        observer.assert_not_called()


async def test_observer_failure_preserves_commit_and_business_handoff(owned):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import db_outcome_for
    error = RuntimeError('snapshot notice')
    observer = create_autospec(lambda owned, outcome, key: None, side_effect=error)
    executor = DbExecutor(lambda: owned, confirmed_write_observer=observer)
    def submit(job):
        future = asyncio.get_running_loop().create_future()
        executor._results[job.key] = future
        executor._execute(job, owned)
        return future
    port = create_autospec(DbExecutor, instance=True, spec_set=True)
    port.submit_write.side_effect = submit
    with pytest.raises(RuntimeError) as caught:
        await BusinessDatabase(port, owned.metadata).write(
            lambda owned: DbOutcome(DbOutcomeKind.COMPLETED, value=7), key=OperationKey('c'*32))
    assert caught.value is error
    actual = db_outcome_for(error)
    assert actual.kind is DbOutcomeKind.COMPLETED and actual.value == 7
    assert actual.observer_error is error
    owned.connection.close.assert_not_called()


async def test_unknown_closes_worker_connection_before_next_job_reopens(owned):
    replacement = OwnedConnection(create_autospec(sqlite3.Connection, instance=True), owned.metadata)
    factory = create_autospec(lambda: None, side_effect=[owned, replacement])
    executor = DbExecutor(factory)
    first = DbJob(OperationKey('d'*32), 'unknown', DbJobKind.WRITE,
        DbPriority.BUSINESS, lambda connection: DbOutcome(DbOutcomeKind.UNKNOWN))
    def read(connection):
        assert connection is replacement
        owned.connection.close.assert_called_once_with()
        return 'fresh'
    second = DbJob(OperationKey('e'*32), 'fresh read', DbJobKind.READ, DbPriority.BUSINESS, read)
    futures = []
    for job in (first, second):
        future = asyncio.get_running_loop().create_future()
        executor._results[job.key] = future
        futures.append(future)
        assert executor._core.try_admit(job)
    executor._stopping = True
    executor._run()
    first_result, second_result = await asyncio.gather(*futures)
    assert first_result.kind is DbOutcomeKind.UNKNOWN
    assert second_result.value == 'fresh' and factory.call_count == 2
    replacement.connection.close.assert_called_once_with()


async def test_worker_shutdown_close_error_keeps_delivered_read_and_is_recorded(owned):
    error = RuntimeError('close failed')
    owned.connection.close.side_effect = error
    executor = DbExecutor(lambda: owned)
    job = DbJob(OperationKey('f'*32), 'read before stop', DbJobKind.READ,
        DbPriority.BUSINESS, lambda owned: 'saved')
    future = asyncio.get_running_loop().create_future()
    executor._results[job.key] = future
    assert executor._core.try_admit(job)
    executor._stopping = True
    executor._run()
    result = await future
    assert isinstance(result, ReadReceipt) and result.value == 'saved'
    assert result.error is None and executor._worker_error is error


async def test_business_close_shares_actual_worker_shutdown_failure(owned, monkeypatch):
    from camctl.persistence.business import BusinessDatabase
    error = RuntimeError('worker close failed')
    executor = DbExecutor(lambda: owned)
    executor._worker_error = error
    executor._thread = create_autospec(threading.Thread, instance=True, spec_set=True)
    joined = asyncio.get_running_loop().create_future()
    join = create_autospec(executor._loop.run_in_executor, return_value=joined)
    monkeypatch.setattr(executor._loop, 'run_in_executor', join)
    database = BusinessDatabase(executor, owned.metadata)
    first = asyncio.create_task(database.close())
    second = asyncio.create_task(database.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    joined.set_result(None)
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == [error, error]
    with pytest.raises(RuntimeError) as caught:
        await database.close()
    assert caught.value is error
    join.assert_called_once_with(None, executor._thread.join)


@pytest.mark.parametrize('kind', [DbJobKind.READ, DbJobKind.WRITE])
async def test_factory_control_is_delivered_without_stranding_future(owned, kind):
    control = KeyboardInterrupt('factory control')
    factory = create_autospec(lambda: None, side_effect=control)
    executor = DbExecutor(factory)
    callback = create_autospec(lambda owned: None)
    job = DbJob(OperationKey('1'*32), 'factory failure', kind, DbPriority.BUSINESS, callback)
    future = asyncio.get_running_loop().create_future()
    executor._results[job.key] = future
    assert executor._core.try_admit(job)
    executor._stopping = True
    executor._run()
    outcome = await future
    if kind is DbJobKind.WRITE:
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK and outcome.control is control
    assert outcome.error is control
    callback.assert_not_called()


async def test_unknown_connection_close_failure_is_secondary_to_actual_outcome(owned):
    close_error = RuntimeError('unknown close')
    owned.connection.close.side_effect = close_error
    executor = DbExecutor(lambda: owned)
    actual_error = RuntimeError('commit unknown')
    job = DbJob(OperationKey('2'*32), 'unknown close', DbJobKind.WRITE,
        DbPriority.BUSINESS, lambda owned: DbOutcome(DbOutcomeKind.UNKNOWN, error=actual_error))
    future = asyncio.get_running_loop().create_future()
    executor._results[job.key] = future
    assert executor._execute(job, owned) is None
    actual = await future
    assert actual.kind is DbOutcomeKind.UNKNOWN and actual.error is actual_error
    assert actual.secondary_errors == (close_error,)
