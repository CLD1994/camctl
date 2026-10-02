"""原操作键读取的精确事实、完整事务和游标责任。"""

import sqlite3
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.persistence.transaction import saved_transaction_events
from unit.history.test_decoding import registration  # noqa: F401


_KEY = OperationKey("a" * 32)
_BODY = ('{"reason":1,"evidence":{"observation":{"value":0.10000000000000001}},'
         '"rows":[{"table":"plans","id":1,"before":{"exists":false},'
         '"after":{"exists":true,"values":{"request_id":42,"name":"plan",'
         '"created_at":0,"status":1}}}]}')


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    data = {"key_id": 3, "range": (3, 7, 8), "previous": (6,), "summary": (2, 7, 8),
            "rows": [(7, 3, 1, 1, 0, 1, 5, _BODY), (8, 3, 1, 1, 0, 1, 6, _BODY)],
            "cursors": []}

    def execute(statement, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        data["cursors"].append(cursor)
        if "operation_key = ?" in statement:
            cursor.fetchone.return_value = None if data["key_id"] is None else (data["key_id"],)
        elif "SELECT last_event_id" in statement:
            cursor.fetchone.return_value = data["previous"]
        elif "FROM history_transactions" in statement:
            cursor.fetchone.return_value = data["range"]
        elif "COUNT(*)" in statement:
            cursor.fetchone.return_value = data["summary"]
        elif "FROM history_events" in statement:
            cursor.__iter__.return_value = iter(data["rows"])
        else:
            raise AssertionError(f"未声明的 SQL 读取: {statement}")
        return cursor

    connection.execute.side_effect = execute
    return connection, data


def test_saved_events_preserve_exact_business_number(database, registration):
    connection, _ = database
    saved = saved_transaction_events(connection, _KEY)
    assert saved[0]["body"]["evidence"]["observation"]["value"] == Decimal("0.10000000000000001")


def test_saved_events_keep_identity_and_original_transaction_range(database, registration):
    connection, _ = database
    saved = saved_transaction_events(connection, _KEY)
    assert [event["event_id"] for event in saved] == [7, 8]
    assert all(event["transaction"] == TransactionRange(3, 7, 8) for event in saved)


def test_missing_operation_key_is_absent(database):
    connection, data = database
    data["key_id"] = None
    assert saved_transaction_events(connection, _KEY) is None


def test_saved_events_keep_original_fact_time(database, registration):
    connection, data = database
    data["rows"][0] = (7, 3, 1, 1, 123456789, 1, 5, _BODY)
    assert saved_transaction_events(connection, _KEY)[0]["occurred_at"] == 123456789


@pytest.mark.parametrize("field,value", [
    ("range", None), ("range", (3, 8, 7)), ("range", (True, 7, 8)),
    ("previous", None), ("previous", (5,)), ("summary", (1, 7, 7)),
])
def test_existing_key_with_invalid_group_is_not_absent(database, registration, field, value):
    connection, data = database
    data[field] = value
    with pytest.raises(ConsistencyError):
        saved_transaction_events(connection, _KEY)


@pytest.mark.parametrize("invalid", [
    '{"reason":1,"reason":2,"evidence":{},"rows":[]}',
    '{"reason":1,"evidence":{"value":NaN},"rows":[]}',
    '{"reason":1,"evidence":{},"rows":[]}',
])
def test_invalid_later_event_rejects_whole_saved_group(database, registration, invalid):
    connection, data = database
    data["rows"][1] = (8, 3, 1, 1, 0, 1, 6, invalid)
    with pytest.raises(ConsistencyError):
        saved_transaction_events(connection, _KEY)


@pytest.mark.parametrize("rows", [
    [], [(7, 3, 1, 1, 0, 1, 5, _BODY)],
    [(7, 3, 1, 1, 0, 1, 5, _BODY), (8, 4, 1, 1, 0, 1, 6, _BODY)],
    [(7, 3, 1, 1, 0, 1, 5, _BODY), (9, 3, 1, 1, 0, 1, 6, _BODY)],
    [(8, 3, 1, 1, 0, 1, 6, _BODY), (7, 3, 1, 1, 0, 1, 5, _BODY)],
    [(7, 3, 1, 1, 0, 1, 5, _BODY), (7, 3, 1, 1, 0, 1, 6, _BODY)],
])
def test_member_stream_must_match_verified_range(database, registration, rows):
    connection, data = database
    data["rows"] = rows
    with pytest.raises(ConsistencyError):
        saved_transaction_events(connection, _KEY)


@pytest.mark.parametrize("sql_prefix", [
    "SELECT id FROM history_transactions", "SELECT id, first_event_id",
    "SELECT last_event_id", "SELECT COUNT(*)", "SELECT id, transaction_id",
])
def test_sql_query_error_is_preserved_and_acquired_cursors_close(database, registration, sql_prefix):
    connection, data = database
    execute = connection.execute.side_effect
    failure = sqlite3.OperationalError("数据库读取失败")

    def fail_query(statement, parameters=()):
        if statement.startswith(sql_prefix):
            raise failure
        return execute(statement, parameters)

    connection.execute.side_effect = fail_query
    with pytest.raises(sqlite3.OperationalError) as caught:
        saved_transaction_events(connection, _KEY)
    assert caught.value is failure
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


def test_key_fetch_error_closes_acquired_cursor(database):
    connection, data = database
    execute = connection.execute.side_effect
    failure = sqlite3.OperationalError("操作键读取失败")

    def fail_fetch(statement, parameters=()):
        cursor = execute(statement, parameters)
        cursor.fetchone.side_effect = failure
        return cursor

    connection.execute.side_effect = fail_fetch
    with pytest.raises(sqlite3.OperationalError) as caught:
        saved_transaction_events(connection, _KEY)
    assert caught.value is failure
    data["cursors"][0].close.assert_called_once_with()


def test_event_iteration_error_does_not_return_prefix(database, registration):
    connection, data = database
    execute = connection.execute.side_effect
    failure = sqlite3.OperationalError("后续事件读取失败")

    def rows_then_error(rows):
        yield rows[0]
        raise failure

    def fail_iteration(statement, parameters=()):
        cursor = execute(statement, parameters)
        if "FROM history_events" in statement and "COUNT(*)" not in statement:
            cursor.__iter__.return_value = rows_then_error(data["rows"])
        return cursor

    connection.execute.side_effect = fail_iteration
    with pytest.raises(sqlite3.OperationalError) as caught:
        saved_transaction_events(connection, _KEY)
    assert caught.value is failure
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("state", ["valid", "missing", "invalid"])
def test_saved_group_releases_all_acquired_cursors(database, registration, state):
    connection, data = database
    if state == "missing":
        data["key_id"] = None
    elif state == "invalid":
        data["rows"][1] = (8, 3, 1, 1, 0, 1, 6, '{')
    if state == "invalid":
        with pytest.raises(ConsistencyError):
            saved_transaction_events(connection, _KEY)
    else:
        saved_transaction_events(connection, _KEY)
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()
