"""物理 ID 分配读取负责释放游标，不改写实际 SQL 错误。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.persistence.transaction import next_row_id


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    return connection, cursor


@pytest.mark.parametrize("maximum,expected", [(None, 1), (42, 43)])
def test_next_row_id_releases_cursor_before_returning(database, maximum, expected):
    connection, cursor = database
    cursor.fetchone.return_value = (maximum,)
    assert next_row_id(connection, "operation_attempts") == expected
    cursor.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_next_row_id_preserves_sql_error_and_releases_only_acquired_cursor(database, stage):
    connection, cursor = database
    error = sqlite3.OperationalError("最大物理 ID 不可读")
    (connection.execute if stage == "execute" else cursor.fetchone).side_effect = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        next_row_id(connection, "operation_attempts")
    assert raised.value is error
    if stage == "execute":
        cursor.close.assert_not_called()
    else:
        cursor.close.assert_called_once_with()
