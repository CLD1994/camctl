"""执行器在本进程内驱动单项worker分支，不启动线程。"""
import asyncio
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbJob, DbJobKind, DbOutcomeKind, DbPriority
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
