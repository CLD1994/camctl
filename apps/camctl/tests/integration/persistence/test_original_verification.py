"""原操作核实使用真实数据库线程和独立只读连接。"""

import sqlite3
import threading
import uuid
from contextlib import closing

import pytest

from camctl.persistence.runtime import DatabaseInvalidError, DbConfig
from ..support.database import business_database, create_test_database

pytestmark = pytest.mark.asyncio


async def test_verification_is_readonly_closes_on_failure_and_keeps_writer(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    create_test_database(path)
    connections = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.creator = threading.get_ident()
            self.closed_on = None

        def close(self):
            self.closed_on = threading.get_ident()
            super().close()

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        if kwargs.get("uri"):
            connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    async with business_database(path, config=DbConfig(busy_timeout_ms=1234)) as database:
        def verify(owned):
            connection = owned.connection
            assert connection.execute('PRAGMA query_only').fetchone()[0] == 1
            assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 1234
            assert owned.metadata == database.metadata
            with pytest.raises(sqlite3.OperationalError) as error:
                connection.execute('CREATE TABLE must_not_exist (x INTEGER)')
            assert error.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_READONLY
            raise ValueError('核实失败')

        with pytest.raises(ValueError, match='核实失败'):
            await database.verify_original(verify)
        assert len(connections) == 2
        assert connections[1].closed_on == connections[1].creator
        assert connections[0].closed_on is None
        assert await database.read(lambda owned: owned.connection.execute('PRAGMA query_only').fetchone()[0]) == 0
    assert connections[0].closed_on == connections[0].creator


async def test_verification_rejects_changed_database_identity(tmp_path):
    path = tmp_path / 'state.db'
    create_test_database(path)
    async with business_database(path) as database:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('UPDATE database_metadata SET instance_id = ? WHERE id = 1', (uuid.uuid4().hex,))
            connection.commit()
        with pytest.raises(DatabaseInvalidError, match='身份或目录绑定已改变'):
            await database.verify_original(lambda owned: True)
