"""执行会话固定恢复边界前重置全部内置相机的准备凭据。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.bootstrap.lifecycle import _initialize_recovery
from camctl.devices.drivers.adb_cameras import registration as adb
from camctl.devices.drivers.hasselblad import registration as hasselblad
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION, DatabaseMetadata, OwnedConnection,
)
from support.business_database import inline_business_database


@pytest.mark.asyncio
async def test_execution_session_resets_adb_and_hasselblad_preparation(monkeypatch):
    begin_adb, begin_hasselblad = Mock(), Mock()
    monkeypatch.setattr(adb, "begin_builtin_camera_session", begin_adb)
    monkeypatch.setattr(hasselblad, "begin_builtin_hasselblad_session", begin_hasselblad)
    recovery_cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    recovery_cursor.fetchall.return_value = ()
    horizon_cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    horizon_cursor.fetchone.return_value = (42,)
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.side_effect = (recovery_cursor, horizon_cursor)
    owned = OwnedConnection(connection, DatabaseMetadata(
        "camctl", "a" * 32, CURRENT_FORMAT_VERSION,
        "/staging", "/ready", "/processing"))
    database = inline_business_database(owned)
    initialize = AsyncMock()
    deps = SimpleNamespace(
        work_files=SimpleNamespace(initialize=initialize),
        recovery_max_event_id=None)

    await _initialize_recovery(deps, database)

    begin_adb.assert_called_once_with()
    begin_hasselblad.assert_called_once_with()
    assert deps.recovery_max_event_id == 42
    initialize.assert_awaited_once_with(database)
    connection.close.assert_not_called()
