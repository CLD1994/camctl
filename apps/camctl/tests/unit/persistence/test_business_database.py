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
