"""绑定收场的真实业务线程、有限保存观察及 SQLite 故障。"""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import threading

import pytest_asyncio

from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .database import database_path


@dataclass(frozen=True)
class SaveLocation:
    metadata: object
    path: Path
    thread_id: int
    fault_injected: bool


class BindingSaveSpy(CaptureRepository):
    """保存真实完整申请与有限归属信息，不将 worker 连接交给观察线程。"""

    def __init__(self):
        self.calls = []
        self.receipts = []

    def finish_binding_failure(self, request, key, owned):
        location = SaveLocation(owned.metadata, database_path(owned),
            threading.get_ident(), isinstance(owned.connection, _FaultConnection))
        self.calls.append((request, key, location))
        receipt = super().finish_binding_failure(request, key, owned)
        self.receipts.append(receipt)
        return receipt


def assert_business_save(location, observer):
    assert location.metadata == observer.metadata
    assert location.path == database_path(observer)
    assert location.thread_id != threading.get_ident()


class _FaultConnection:
    def __init__(self, connection, fault):
        self.connection, self.fault = connection, fault

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        connection, fault = self.connection, self.fault
        operations = set()
        denied = False

        def authorize(operation, table, column, _database, _source):
            nonlocal denied
            operations.add((operation, table, column))
            if fault.deny(operation, table, column, tuple(parameters), connection):
                denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            try:
                result = connection.execute(statement, parameters)
            except sqlite3.DatabaseError as error:
                if denied:
                    raise fault.error from error
                raise
        finally:
            connection.set_authorizer(None)
        fault.after(operations, connection)
        return result


class BindingTransactionFault:
    """按 SQLite 操作种类识别真实产物投影及原 COMMIT，不匹配 SQL 文本。"""

    def __init__(self, mode):
        self.mode = mode
        self.armed = True
        self.fault_transaction = False
        self.hits = self.commit_calls = self.rollback_count = 0
        self.partial_outputs = self.outputs_after_rollback = None
        self.error = sqlite3.OperationalError("绑定终态事务保存故障")

    def wrap(self, connection):
        return _FaultConnection(connection, self)

    def deny(self, operation, table, column, parameters, connection):
        if (self.armed and self.fault_transaction and operation == sqlite3.SQLITE_TRANSACTION
                and table == 'COMMIT' and self.mode == 'commit_before'):
            self.armed = False
            self.hits += 1
            self.commit_calls += 1
            return True
        return False

    def after(self, operations, connection):
        if any(operation == sqlite3.SQLITE_INSERT and table == 'outputs'
               for operation, table, _ in operations):
            self.fault_transaction = True
            self.partial_outputs = connection.execute('SELECT COUNT(*) FROM outputs').fetchone()[0]
            if self.armed and self.mode == 'projection':
                self.armed = False
                self.hits += 1
                raise self.error
        if (sqlite3.SQLITE_TRANSACTION, 'COMMIT', None) in operations:
            if self.armed and self.fault_transaction and self.mode == 'commit_after':
                self.armed = False
                self.hits += 1
                self.commit_calls += 1
                raise self.error
        if (sqlite3.SQLITE_TRANSACTION, 'ROLLBACK', None) in operations and self.fault_transaction:
            self.rollback_count += 1
            self.outputs_after_rollback = connection.execute('SELECT COUNT(*) FROM outputs').fetchone()[0]
            self.fault_transaction = False


class FaultingBindingSaveSpy(BindingSaveSpy):
    def __init__(self, fault):
        super().__init__()
        self.fault = fault

    def finish_binding_failure(self, request, key, owned):
        return super().finish_binding_failure(
            request, key, replace(owned, connection=self.fault.wrap(owned.connection)))


class ClosedInputFault:
    """只拒绝原 RESULTS 输入的实际 result_json 读取，其他操作正常执行。"""

    def __init__(self, activity_id):
        self.activity_id = activity_id
        self.armed = True
        self.hits = 0
        self.error = sqlite3.OperationalError('原 RESULTS 输入读取失败')

    def wrap(self, connection):
        return _FaultConnection(connection, self)

    def deny(self, operation, table, column, parameters, connection):
        if (self.armed and operation == sqlite3.SQLITE_READ
                and (table, column) == ('operation_attempts', 'result_json')
                and parameters == (f'results/{self.activity_id}',)):
            self.armed = False
            self.hits += 1
            return True
        return False

    def after(self, operations, connection):
        pass


class RecoveryReadFault(_FaultConnection):
    """按原键或父计划目录字段拒绝读取，保留原事务回滚观察。"""

    def __init__(self, connection, field, parameters):
        super().__init__(connection, self)
        self.field, self.parameters = field, parameters
        self.armed = True
        self.hits = self.rollback_count = 0
        self.fault_transaction = False
        self.in_transaction_at_failure = None
        self.error = sqlite3.OperationalError('绑定原申请恢复读取失败')

    def deny(self, operation, table, column, parameters, connection):
        if (self.armed and operation == sqlite3.SQLITE_READ
                and (table, column) == self.field and parameters == self.parameters):
            self.armed = False
            self.hits += 1
            self.fault_transaction = True
            self.in_transaction_at_failure = connection.in_transaction
            return True
        return False

    def after(self, operations, connection):
        if ((sqlite3.SQLITE_TRANSACTION, 'ROLLBACK', None) in operations
                and self.fault_transaction):
            self.rollback_count += 1
            self.fault_transaction = False


_active = ContextVar('binding_case', default=None)


class BindingCase:
    def __init__(self, resources):
        self.resources = resources
        self.databases = []

    async def runtime(self, observer, world, binding, *, fault=None):
        from ..capture.test_closed_result_local_consumption import fresh_closed_runtime
        path = database_path(observer)

        def factory():
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            return owned if fault is None else replace(owned, connection=fault.wrap(owned.connection))

        database = await BusinessDatabase.open(factory)
        self.databases.append(database)
        return await fresh_closed_runtime(database, world, binding)

    async def close(self):
        errors = []
        try:
            await self.resources.close()
        except BaseException as error:
            errors.append(error)
        for database in self.databases:
            try:
                await database.close()
            except BaseException as error:
                errors.append(error)
        self.databases.clear()
        if errors:
            raise BaseExceptionGroup('绑定测试实际资源收场失败', errors)


@pytest_asyncio.fixture(autouse=True)
async def binding_case(capture_resources):
    case = BindingCase(capture_resources)
    token = _active.set(case)
    try:
        try:
            yield case
        finally:
            await case.close()
    finally:
        _active.reset(token)


async def fresh_binding_runtime(observer, world, binding, *, fault=None):
    return await _active.get().runtime(observer, world, binding, fault=fault)


async def close_binding_databases():
    await _active.get().close()
