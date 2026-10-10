"""取回与清理装配分别保持设备配置前提错误的分类。"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverRegistry


@pytest.fixture
def owned():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = None
    connection.execute.return_value = cursor
    return SimpleNamespace(connection=connection)


def _factory(kind, devices):
    shared = {"devices": devices, "drivers": DriverRegistry(()), "staging": Path("/staging")}
    if kind == "obtain":
        return session_obtain_assembly(**shared, ready=Path("/ready"), processing=Path("/processing"))
    return session_cleanup_assembly(**shared, max_delete_attempts=3, max_query_attempts=3)


@pytest.mark.parametrize("kind", ["obtain", "cleanup"])
@pytest.mark.parametrize("declaration", [None, "unreadable", {}, {"driver": "unregistered"}])
def test_output_assembly_rejects_unreliable_or_unregistered_device_configuration(kind, declaration, owned):
    factory = _factory(kind, {"cam-a": declaration})

    with pytest.raises(DeviceConfigurationError):
        factory(owned)


@pytest.mark.parametrize("kind", ["obtain", "cleanup"])
def test_empty_device_directory_still_assembles_host_file_runtime(kind, owned):
    runtime = _factory(kind, {})(owned)

    assert runtime is not None
