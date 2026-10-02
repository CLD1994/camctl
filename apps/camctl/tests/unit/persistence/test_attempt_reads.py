"""普通意图规划负责释放本地读取游标并保留实际 SQL 异常。"""

from decimal import Decimal
from enum import IntEnum
from importlib.util import find_spec, module_from_spec
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts import enums
from camctl.contracts.values import OperationKey
from camctl.history import events
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind, QueryPurpose,
)
from camctl.persistence import row_history, transaction
from camctl.persistence.transaction import TransactionError, TransactionScope


@pytest.fixture
def repository(monkeypatch):
    # 协作者先加载，独立源码实例仅隔离导入期的真实登记读取，不改变全局缓存。
    readers = (transaction.load_enum_registry, row_history.load_enum_registry)
    definitions = {
        "operation_runs.kind": {"START": 1, "STOP": 2, "READ_FILE": 3, "QUERY_ACTIVITY": 6,
                                "STOP_RESIDUAL": 8, "EMERGENCY_STOP": 9},
        "operation_runs.status": {"PENDING": 1, "ACTIVE": 2, "SUCCEEDED": 3},
        "operation_runs.query_purpose": {"BEFORE_EXECUTION": 1},
        "operation_attempts.status": {"RUNNING": 1},
        "operation_attempts.effect_state": {"UNKNOWN": 1},
    }
    monkeypatch.setattr(enums, "enum_for", create_autospec(enums.enum_for,
        side_effect=lambda column: IntEnum(column, definitions[column])))
    resources = []
    for module in (enums, events):
        reader = create_autospec(module.resource_bytes,
            side_effect=AssertionError("单元测试不能读取真实包资源"))
        resources.append(reader)
        monkeypatch.setattr(module, "resource_bytes", reader)
    spec = find_spec("camctl.persistence.repositories.operations")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_saved_transaction_events", create_autospec(
        module._saved_transaction_events, return_value=None))
    action = {"id": 1, "device_id": "cam-1"}
    run = {"id": 7, "action_id": 1, "delivery_id": None, "kind": 6,
           "query_purpose": 1, "responsibility_key": "query/preflight/1", "activity_id": None,
           "copy_id": None, "cleanup_item_id": None, "session_key": None, "status": 3,
           "attempts_used": 0, "max_attempts_used": 2, "timeout_s_json": Decimal("5"),
           "retry_interval_s_json": Decimal("1"), "retry_wait_required": 0, "error_json": None}
    monkeypatch.setattr(module, "row_facts", create_autospec(module.row_facts,
        side_effect=lambda connection, table, identity: action if table == "actions" else run))
    yield module, run
    for reader in resources:
        reader.assert_not_called()
    assert readers == (transaction.load_enum_registry, row_history.load_enum_registry)


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    data = {"found": (7,), "maximum": (None,), "cursors": [], "fault": None,
            "error": sqlite3.OperationalError("普通意图读取不可用")}

    def execute(sql, parameters=()):
        assert sql.lstrip().startswith("SELECT"), "规划不能结束事务或写入事实"
        if sql.startswith("SELECT id FROM operation_runs"):
            tag = "found"
        elif sql.startswith("SELECT MAX(attempt_no)"):
            tag = "maximum"
        elif sql.startswith("SELECT MAX(id)"):
            tag = "allocation"
        else:
            raise AssertionError(f"未声明的查询: {sql}")
        if data["fault"] == (tag, "execute"):
            raise data["error"]
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        data["cursors"].append(cursor)
        cursor.fetchone.return_value = (None,) if tag == "allocation" else data[tag]
        if data["fault"] == (tag, "fetch"):
            cursor.fetchone.side_effect = data["error"]
        return cursor

    connection.execute.side_effect = execute
    return connection, data


def _plan(module, connection, intent=None):
    if intent is None:
        intent = AttemptIntent("query", 1, OperationKind.QUERY_ACTIVITY, AttemptTarget(),
                               QueryPurpose.BEFORE_EXECUTION, AttemptConfig(2, "5", "1"), occurred_at=1)
    return module.BeginAttemptCommand(intent, OperationKey("a" * 32)).plan(TransactionScope(connection, 1, 1))


@pytest.mark.parametrize("found", [(7,), None])
def test_intent_plan_releases_responsibility_query_cursor(repository, database, found):
    module, _ = repository
    connection, data = database
    data["found"] = found
    result = _plan(module, connection).result
    if found is None:
        assert result.disposition is BeginDisposition.GRANTED
        assert result.ticket.responsibility_key == "query/preflight/1"
    else:
        assert result.disposition is BeginDisposition.REJECTED
        assert result.reason == "run_ended"
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("used,maximum", [(0, None), (1, 1)])
def test_intent_plan_releases_count_query_cursor(repository, database, used, maximum):
    module, run = repository
    connection, data = database
    run["attempts_used"] = used
    data["maximum"] = (maximum,)
    assert _plan(module, connection).result.reason == "run_ended"
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


def test_intent_plan_releases_count_query_cursor_on_inconsistent_count(repository, database):
    module, _ = repository
    connection, data = database
    data["maximum"] = (1,)
    with pytest.raises(TransactionError):
        _plan(module, connection)
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("query", ["found", "maximum"])
@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_intent_plan_preserves_sql_error_and_releases_acquired_cursors(repository, database, query, stage):
    module, _ = repository
    connection, data = database
    data["fault"] = (query, stage)
    with pytest.raises(sqlite3.OperationalError) as raised:
        _plan(module, connection)
    assert raised.value is data["error"]
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("intent", [{}, object()])
def test_intent_plan_checks_input_type_before_saved_key_lookup(repository, database, intent):
    module, _ = repository
    connection, _ = database
    with pytest.raises(TransactionError):
        _plan(module, connection, intent)
    module._saved_transaction_events.assert_not_called()
    connection.execute.assert_not_called()
