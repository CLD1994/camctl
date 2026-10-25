"""公共行事实与历史事务范围在同一可靠读取阶段内复用。"""

from dataclasses import dataclass
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence import transaction
from camctl.persistence.read_reuse import clear_read_reuse, read_reuse_scope


@pytest.fixture
def database(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.in_transaction = True
    connection.total_changes = 0
    state = {"row": (1, '{"values":[1.0000000000000001]}'),
             "range": (2, 3, 4), "previous": (2,), "members": (2, 3, 4),
             "cursors": [], "failure": None, "failure_prefix": ""}
    monkeypatch.setattr(transaction, "json_columns", create_autospec(
        transaction.json_columns, return_value={"sample": frozenset({"payload_json"})}))

    def execute(statement, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        state["cursors"].append(cursor)
        if statement.startswith("SELECT *"):
            cursor.description = (("id", None, None, None, None, None, None),
                                  ("payload_json", None, None, None, None, None, None))
            value = state["row"]
        elif statement.startswith("SELECT id, first_event_id"):
            value = state["range"]
        elif statement.startswith("SELECT last_event_id"):
            value = state["previous"]
        elif statement.startswith("SELECT COUNT(*)"):
            value = state["members"]
        else:
            raise AssertionError(f"未声明的 SQL: {statement}")
        cursor.fetchone.return_value = value
        if state["failure"] is not None and statement.startswith(state["failure_prefix"]):
            cursor.fetchone.side_effect = state["failure"]
        return cursor

    connection.execute.side_effect = execute
    return connection, state


@pytest.mark.parametrize("missing", [False, True])
def test_row_facts_reuses_exact_success_or_absence(database, missing):
    connection, state = database
    if missing:
        state["row"] = None
    with read_reuse_scope(connection):
        first = transaction.row_facts(connection, "sample", 1)
        second = transaction.row_facts(connection, "sample", 1)
    assert first == second == (None if missing else {
        "id": 1, "payload_json": {"values": [Decimal("1.0000000000000001")]}})
    assert connection.execute.call_count == 1
    state["cursors"][0].close.assert_called_once_with()


def test_row_facts_returns_independent_nested_values(database):
    connection, _ = database
    with read_reuse_scope(connection):
        first = transaction.row_facts(connection, "sample", 1)
        first["payload_json"]["values"].append(2)
        second = transaction.row_facts(connection, "sample", 1)
        second["payload_json"]["values"].append(3)
        third = transaction.row_facts(connection, "sample", 1)
    assert third["payload_json"]["values"] == [Decimal("1.0000000000000001")]
    assert connection.execute.call_count == 1


class TableName(str):
    pass


@pytest.mark.parametrize("table,identity", [
    ("other", 1), (TableName("sample"), 1), ("sample", True), ("sample", 2),
])
def test_row_identity_includes_table_and_actual_input_types(database, table, identity):
    connection, _ = database
    with read_reuse_scope(connection):
        transaction.row_facts(connection, "sample", 1)
        transaction.row_facts(connection, table, identity)
    assert connection.execute.call_count == 2


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("读取失败"), KeyboardInterrupt()])
def test_row_read_failure_keeps_original_error_and_retries(database, failure):
    connection, state = database
    state["failure"] = failure
    with read_reuse_scope(connection):
        with pytest.raises(type(failure)) as caught:
            transaction.row_facts(connection, "sample", 1)
        assert caught.value is failure
        state["failure"] = None
        assert transaction.row_facts(connection, "sample", 1)["id"] == 1
        transaction.row_facts(connection, "sample", 1)
    assert connection.execute.call_count == 2
    for cursor in state["cursors"]:
        cursor.close.assert_called_once_with()


def test_row_decode_failure_is_not_cached(database):
    connection, state = database
    state["row"] = (1, '{"value":1,"value":2}')
    with read_reuse_scope(connection):
        with pytest.raises(ConsistencyError):
            transaction.row_facts(connection, "sample", 1)
        state["row"] = (1, '{"value":2}')
        assert transaction.row_facts(connection, "sample", 1)["payload_json"] == {"value": 2}
    assert connection.execute.call_count == 2


@pytest.mark.parametrize("provided,sql_count", [(False, 3), (True, 2)])
def test_transaction_range_reuses_only_complete_same_input(database, provided, sql_count):
    connection, _ = database
    validated = TransactionRange(2, 3, 4) if provided else None
    with read_reuse_scope(connection):
        first = transaction.read_transaction_range(connection, 2, validated)
        second = transaction.read_transaction_range(connection, 2, validated)
    assert first == second == TransactionRange(2, 3, 4)
    assert first is not second
    assert connection.execute.call_count == sql_count


@pytest.mark.parametrize("provided_first", [False, True])
def test_provided_range_and_full_member_check_have_different_identity(database, provided_first):
    connection, _ = database
    values = (TransactionRange(2, 3, 4), None) if provided_first else (None, TransactionRange(2, 3, 4))
    with read_reuse_scope(connection):
        for validated in values:
            assert transaction.read_transaction_range(connection, 2, validated) == TransactionRange(2, 3, 4)
    assert connection.execute.call_count == 5


def test_provided_range_does_not_authorize_missing_members(database):
    connection, state = database
    state["members"] = (1, 3, 3)
    with read_reuse_scope(connection):
        assert transaction.read_transaction_range(connection, 2, TransactionRange(2, 3, 4)) == TransactionRange(2, 3, 4)
        with pytest.raises(ConsistencyError, match="成员"):
            transaction.read_transaction_range(connection, 2)
    assert connection.execute.call_count == 5


@dataclass(frozen=True)
class OtherRange:
    txn_id: int
    first_event_id: int
    last_event_id: int


@pytest.mark.parametrize("validated", [
    TransactionRange(3, 3, 4), TransactionRange(2, 2, 4), TransactionRange(2, 3, 5),
    OtherRange(2, 3, 4), True,
])
def test_different_provided_range_cannot_use_previous_success(database, validated):
    connection, _ = database
    with read_reuse_scope(connection):
        transaction.read_transaction_range(connection, 2, TransactionRange(2, 3, 4))
        with pytest.raises(ConsistencyError, match="范围发生变化"):
            transaction.read_transaction_range(connection, 2, validated)
    assert connection.execute.call_count == 4


def test_transaction_identity_preserves_bool_and_integer_difference(database):
    connection, state = database
    state["range"], state["members"] = (1, 1, 2), (2, 1, 2)
    with read_reuse_scope(connection):
        transaction.read_transaction_range(connection, 1)
        transaction.read_transaction_range(connection, True)
    assert connection.execute.call_count == 4


@pytest.mark.parametrize("member", ["txn_id", "first_event_id", "last_event_id"])
def test_provided_range_key_preserves_every_member_type(database, member):
    connection, state = database
    first = TransactionRange(1, 1, 1)
    second = TransactionRange(1, 1, 1)
    # 模拟已持有对象中的错误字段；输入在 Python 相等语义下仍等于 first。
    object.__setattr__(second, member, True)
    state["range"] = (1, 1, 1)
    with read_reuse_scope(connection):
        transaction.read_transaction_range(connection, 1, first)
        transaction.read_transaction_range(connection, 1, second)
    assert connection.execute.call_count == 2


class CompatibleRange(TransactionRange):
    def __eq__(self, other):
        return (self.txn_id, self.first_event_id, self.last_event_id) == (
            other.txn_id, other.first_event_id, other.last_event_id)


def test_provided_range_key_preserves_actual_dataclass_type(database):
    connection, _ = database
    with read_reuse_scope(connection):
        transaction.read_transaction_range(connection, 2, TransactionRange(2, 3, 4))
        transaction.read_transaction_range(connection, 2, CompatibleRange(2, 3, 4))
    assert connection.execute.call_count == 4


@pytest.mark.parametrize("field,value", [
    ("range", None), ("range", (True, 3, 4)), ("range", (2, 4, 3)),
    ("range", (3, 3, 4)), ("previous", None), ("previous", (1,)),
    ("members", (1, 3, 3)),
])
def test_invalid_range_failure_is_retried_after_facts_recover(database, field, value):
    connection, state = database
    original = state[field]
    state[field] = value
    with read_reuse_scope(connection):
        with pytest.raises(ConsistencyError):
            transaction.read_transaction_range(connection, 2)
        state[field] = original
        assert transaction.read_transaction_range(connection, 2) == TransactionRange(2, 3, 4)
    for cursor in state["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("prefix,failed_queries", [
    ("SELECT id, first_event_id", 1), ("SELECT last_event_id", 2), ("SELECT COUNT(*)", 3),
])
def test_range_query_error_is_preserved_and_not_cached(database, prefix, failed_queries):
    connection, state = database
    error = sqlite3.OperationalError("范围读取失败")
    state["failure"] = error
    state["failure_prefix"] = prefix
    with read_reuse_scope(connection):
        with pytest.raises(sqlite3.OperationalError) as caught:
            transaction.read_transaction_range(connection, 2)
        assert caught.value is error
        state["failure"] = None
        assert transaction.read_transaction_range(connection, 2) == TransactionRange(2, 3, 4)
    assert connection.execute.call_count == failed_queries + 3
    for cursor in state["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("reset", ["write", "clear", "scope"])
def test_both_public_reads_reload_after_phase_or_generation_change(database, reset):
    connection, state = database

    def read():
        return (transaction.row_facts(connection, "sample", 1),
                transaction.read_transaction_range(connection, 2))

    with read_reuse_scope(connection):
        read()
        if reset == "write":
            connection.total_changes += 1
        elif reset == "clear":
            clear_read_reuse(connection)
        if reset != "scope":
            state["row"], state["range"], state["members"] = (1, '{}'), (2, 3, 5), (3, 3, 5)
            assert read() == ({"id": 1, "payload_json": {}}, TransactionRange(2, 3, 5))
    if reset == "scope":
        with read_reuse_scope(connection):
            read()
    assert connection.execute.call_count == 8


@pytest.mark.parametrize("provided,sql_count", [(False, 6), (True, 4)])
def test_range_outside_scope_keeps_original_queries(database, provided, sql_count):
    connection, _ = database
    validated = TransactionRange(2, 3, 4) if provided else None
    transaction.read_transaction_range(connection, 2, validated)
    transaction.read_transaction_range(connection, 2, validated)
    assert connection.execute.call_count == sql_count


def test_row_facts_outside_scope_keeps_original_query(database):
    connection, _ = database
    transaction.row_facts(connection, "sample", 1)
    transaction.row_facts(connection, "sample", 1)
    assert connection.execute.call_count == 2
