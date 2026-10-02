"""目标关联读取拥有所取得游标，正常与失败出口均释放它。"""

import sqlite3
from enum import IntEnum
from importlib.util import find_spec, module_from_spec
from unittest.mock import create_autospec

import pytest

from camctl.operations.attempts import AttemptTarget, OperationKind, QueryPurpose
from camctl.contracts import enums
from camctl.persistence.transaction import TransactionError


@pytest.fixture
def operations(monkeypatch):
    # 只提供本次辅助读取及模块装配所需的登记成员，不复制完整清单。
    partial = {
        "operation_runs.kind": {"START": 1, "STOP": 2, "STOP_RESIDUAL": 8},
        "operation_runs.status": {"ACTIVE": 2},
        "operation_runs.query_purpose": {"BEFORE_EXECUTION": 1},
        "operation_attempts.status": {"RUNNING": 1},
        "operation_attempts.effect_state": {"UNKNOWN": 1},
    }
    registered = {column: IntEnum(column, members) for column, members in partial.items()}
    monkeypatch.setattr(enums, "enum_for", create_autospec(enums.enum_for,
        side_effect=lambda column: registered[column]))
    monkeypatch.setattr(enums, "resource_bytes", create_autospec(enums.resource_bytes,
        side_effect=AssertionError("单元测试不能读取真实包资源")))
    # 解释器读取被测源码；独立模块实例不占用或替换生产模块缓存。
    spec = find_spec("camctl.persistence.repositories.operations")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", [OperationKind.DELETE_FILE, OperationKind.CHECK_FILE_EXISTS])
@pytest.mark.parametrize("branch", ["found", "missing", "read_error", "execute_error"])
def test_cleanup_context_releases_cursor(operations, kind, branch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    cursor.fetchone.return_value = None if branch == "missing" else (1,)
    error = sqlite3.OperationalError("清理项查询不可用")
    if branch == "execute_error":
        connection.execute.side_effect = error
    elif branch == "read_error":
        cursor.fetchone.side_effect = error
    expected = TransactionError if branch == "missing" else sqlite3.OperationalError
    if branch == "found":
        operations._load_flow_context(connection, kind, 1, AttemptTarget(cleanup_item_id=1), None, {})
    else:
        with pytest.raises(expected) as raised:
            operations._load_flow_context(connection, kind, 1, AttemptTarget(cleanup_item_id=1), None, {})
        if branch != "missing":
            assert raised.value is error
    if branch == "execute_error":
        cursor.close.assert_not_called()
    else:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("branch", ["found", "empty", "read_error", "execute_error"])
def test_query_confirmation_context_releases_cursor(operations, monkeypatch, branch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    cursor.fetchall.return_value = [(2,)] if branch == "found" else []
    activity, run = {"id": 1}, {"id": 2}
    monkeypatch.setattr(operations, "_load_row", create_autospec(
        operations._load_row, side_effect=lambda connection, table, row_id: activity if table == "device_activities" else run,
    ))
    error = sqlite3.OperationalError("查询确认的原流程不可用")
    if branch == "execute_error":
        connection.execute.side_effect = error
    elif branch == "read_error":
        cursor.fetchall.side_effect = error
    state = {}
    if branch in ("found", "empty"):
        operations._load_flow_context(connection, OperationKind.QUERY_ACTIVITY, 1,
                                      AttemptTarget(activity_id=1), QueryPurpose.START_CONFIRMATION, state)
        assert state["device_activities"] == {1: activity}
        assert state.get("operation_runs", {}) == ({2: run} if branch == "found" else {})
    else:
        with pytest.raises(sqlite3.OperationalError) as raised:
            operations._load_flow_context(connection, OperationKind.QUERY_ACTIVITY, 1,
                                          AttemptTarget(activity_id=1), QueryPurpose.START_CONFIRMATION, state)
        assert raised.value is error
    if branch == "execute_error":
        cursor.close.assert_not_called()
    else:
        cursor.close.assert_called_once_with()
