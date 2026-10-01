"""事务内完整行读取恢复精确 JSON 事实，隔离 SQLite 和资源读取。"""

from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence import transaction


@pytest.fixture
def connection(monkeypatch):
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.description = tuple((name, None, None, None, None, None, None)
                               for name in ("id", "payload_json", "note_json"))
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value = cursor
    monkeypatch.setattr(transaction, "json_columns", create_autospec(
        transaction.json_columns, return_value={"sample": frozenset({"payload_json"})}))
    return connection


def test_row_facts_recovers_exact_json_and_preserves_unregistered_text(connection):
    connection.execute.return_value.fetchone.return_value = (
        7, '{"v": 1.0000000000000001, "ready": false}', '{"v":1}')
    assert transaction.row_facts(connection, "sample", 7) == {
        "id": 7, "payload_json": {"v": Decimal("1.0000000000000001"), "ready": False},
        "note_json": '{"v":1}'}


def test_row_facts_preserves_sql_null(connection):
    connection.execute.return_value.fetchone.return_value = (7, None, "text")
    assert transaction.row_facts(connection, "sample", 7)["payload_json"] is None


def test_row_facts_preserves_missing_record(connection):
    connection.execute.return_value.fetchone.return_value = None
    assert transaction.row_facts(connection, "sample", 7) is None


@pytest.mark.parametrize("invalid", ['{"v":1,"v":2}', '{"v":NaN}', '{'])
def test_row_facts_rejects_invalid_json_in_existing_record(connection, invalid):
    connection.execute.return_value.fetchone.return_value = (7, invalid, "text")
    with pytest.raises(ConsistencyError):
        transaction.row_facts(connection, "sample", 7)


@pytest.mark.parametrize("state", ["valid", "missing", "invalid", "sql_error"])
def test_row_facts_releases_acquired_cursor_on_every_exit(connection, state):
    cursor = connection.execute.return_value
    if state == "missing":
        cursor.fetchone.return_value = None
    elif state == "sql_error":
        cursor.fetchone.side_effect = sqlite3.OperationalError("read failed")
    else:
        cursor.fetchone.return_value = (7, '{}' if state == "valid" else '{', "text")
    if state == "sql_error":
        with pytest.raises(sqlite3.OperationalError):
            transaction.row_facts(connection, "sample", 7)
    elif state == "invalid":
        with pytest.raises(ConsistencyError):
            transaction.row_facts(connection, "sample", 7)
    else:
        transaction.row_facts(connection, "sample", 7)
    cursor.close.assert_called_once_with()
