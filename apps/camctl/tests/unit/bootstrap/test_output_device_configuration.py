"""取回与清理装配分别保持设备配置前提错误的分类。"""

import sqlite3
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverRegistry
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = None
    connection.execute.return_value = cursor
    owned = OwnedConnection(connection, DatabaseMetadata(
        application_id="camctl", instance_id="1234567890abcdef1234567890abcdef",
        format_version=1, staging_path="/staging", ready_path="/ready",
        processing_path="/processing"))
    return inline_business_database(owned)


def _factory(kind, devices):
    shared = {"devices": devices, "drivers": DriverRegistry(()), "staging": Path("/staging")}
    if kind == "obtain":
        return session_obtain_assembly(**shared, ready=Path("/ready"), processing=Path("/processing"))
    return session_cleanup_assembly(**shared, max_delete_attempts=3, max_query_attempts=3)


@pytest.mark.parametrize("kind", ["obtain", "cleanup"])
@pytest.mark.parametrize("declaration", [None, "unreadable", {}, {"driver": "unregistered"}])
@pytest.mark.asyncio
async def test_output_assembly_rejects_unreliable_or_unregistered_device_configuration(kind, declaration, database):
    factory = _factory(kind, {"cam-a": declaration})

    with pytest.raises(DeviceConfigurationError):
        if kind == "cleanup":
            await factory(database)
        else:
            factory(database)


@pytest.mark.parametrize("kind", ["obtain", "cleanup"])
@pytest.mark.asyncio
async def test_empty_device_directory_still_assembles_host_file_runtime(kind, database):
    factory = _factory(kind, {})
    runtime = await factory(database) if kind == "cleanup" else factory(database)

    assert runtime is not None
