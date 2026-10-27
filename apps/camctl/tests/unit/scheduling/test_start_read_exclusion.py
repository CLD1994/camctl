"""在途设备调用查询只使用受控的枚举和数据库替身。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from camctl.contracts import enums

with patch.object(enums, "resource_bytes", side_effect=AssertionError("单元测试不能读取包资源")):
    from camctl.scheduling import resources


@pytest.fixture
def fake_enums(monkeypatch):
    definitions = {
        "operation_runs.kind": SimpleNamespace(START=1, READ_FILE=3),
        "operation_attempts.status": SimpleNamespace(RUNNING=1),
    }
    monkeypatch.setattr(resources, "enum_for", lambda column: definitions[column])
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))


@pytest.mark.parametrize("result,expected", [(None, False), ((1,), True)])
@pytest.mark.parametrize("query,kind,device_column", [
    (resources.device_read_in_flight, 3, "c.slot_device_id"),
    (resources.device_start_in_flight, 1, "a.device_id"),
])
def test_device_query_limits_in_flight_attempts_to_its_device(
        fake_enums, query, kind, device_column, result, expected):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = result
    connection.execute.return_value = cursor

    assert query(connection, "camera") is expected
    sql, parameters = connection.execute.call_args.args
    assert f"{device_column}=?" in sql
    assert "t.status=1" in sql
    assert parameters == (kind, "camera")
    cursor.close.assert_called_once()
