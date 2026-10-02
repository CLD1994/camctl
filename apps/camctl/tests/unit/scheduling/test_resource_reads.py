"""持有者读取负责释放游标，保留查询与读取的实际错误。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.scheduling.resources import ConsistencyError, StartHolder, current_start_holder


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    return connection, cursor


@pytest.mark.parametrize("rows,expected", [([], None), ([(4, 8, 3)], StartHolder(4, 8, 3))])
def test_holder_read_releases_cursor_before_returning(database, rows, expected):
    connection, cursor = database
    cursor.fetchall.return_value = rows
    assert current_start_holder(connection, "cam-1") == expected
    cursor.close.assert_called_once_with()


def test_holder_read_releases_cursor_on_ambiguous_result(database):
    connection, cursor = database
    cursor.fetchall.return_value = [(4, 8, 3), (5, 9, 1)]
    with pytest.raises(ConsistencyError):
        current_start_holder(connection, "cam-1")
    cursor.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_holder_read_preserves_sql_error_and_releases_only_acquired_cursor(database, stage):
    connection, cursor = database
    error = sqlite3.OperationalError("启动持有者不可读")
    (connection.execute if stage == "execute" else cursor.fetchall).side_effect = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        current_start_holder(connection, "cam-1")
    assert raised.value is error
    if stage == "execute":
        cursor.close.assert_not_called()
    else:
        cursor.close.assert_called_once_with()
