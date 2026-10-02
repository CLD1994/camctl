"""原写事务内的限定行历史读取：范围、目录、分页与资源所有权。"""

from dataclasses import replace
from importlib import import_module
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.history.events import EventEnvelope, RowChange, RowImage, business_columns
from camctl.history.decoding import decode_event_row
from camctl.contracts.enums import load_registry
from camctl.persistence.transaction import read_transaction_range


def _event(event_id, row_id, before, after):
    return EventEnvelope(event_id, event_id, 10, 1, 1, 2, None, 2, {}, (
        RowChange("operation_runs", row_id, RowImage(True, before), RowImage(True, after)),
    ))


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    data = {"primary": (4, 4), "floor": (1, 1), "head": (4, 4), "cursors": [], "pages": [],
            "fault": None, "events": [
                (4, _event(4, 7, {"max_attempts_used": 2}, {"max_attempts_used": 3})),
                (3, _event(3, 8, {"max_attempts_used": 1}, {"max_attempts_used": 2})),
                (2, _event(2, 7, {"max_attempts_used": 1}, {"max_attempts_used": 2})),
            ]}

    def execute(sql, parameters=()):
        assert sql.lstrip().startswith("SELECT"), "读取不能结束调用方事务或写入事实"
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        data["cursors"].append(cursor)
        if sql.startswith("SELECT last_event_id, change_count"):
            tag = "primary"
            cursor.fetchone.return_value = data["primary"]
        elif sql.startswith("SELECT event_id, change_count"):
            tag = "floor" if "event_id <= ?" in sql else "head"
            cursor.fetchone.return_value = data[tag]
        elif sql.startswith("SELECT l.change_count"):
            tag = "page"
            entity_type, entity_id, lower, upper, previous, limit = parameters
            assert (entity_type, entity_id) == (1, 1)
            selected = [(count, event) for count, event in data["events"]
                        if lower < event.event_id <= upper and event.event_id < previous][:limit]
            rows = [(count, event.event_id, event.transaction_id, event.event_type,
                     event.event_version, event.occurred_at, event.clock_status, event.change_seq, "{}")
                    for count, event in selected]
            data["pages"].append((len(rows), limit))
            cursor.fetchall.return_value = rows
        else:
            raise AssertionError(f"未声明的读取: {sql}")
        if data["fault"] == tag:
            cursor.fetchone.side_effect = sqlite3.OperationalError(tag)
            cursor.fetchall.side_effect = sqlite3.OperationalError(tag)
        return cursor

    connection.execute.side_effect = execute
    return connection, data


def _reader(monkeypatch, data):
    reader = import_module("camctl.persistence.row_history")
    monkeypatch.setattr(reader, "load_enum_registry", create_autospec(load_registry, return_value={
        "history_objects": {"action": {"id": 1, "table": "actions"}},
    }))
    monkeypatch.setattr(reader, "business_columns", create_autospec(business_columns,
        return_value=frozenset({"status", "error_json", "max_attempts_used"})))
    monkeypatch.setattr(reader, "read_transaction_range", create_autospec(read_transaction_range,
        side_effect=lambda connection, identity, validated=None: TransactionRange(identity, identity, identity)))
    monkeypatch.setattr(reader, "decode_event_row", create_autospec(decode_event_row,
        side_effect=lambda row: next(event for _, event in data["events"] if event.event_id == row[0])))
    return reader


def _read(reader, connection, **changes):
    request = dict(owner=("action", 1), table="operation_runs", row_id=7,
                   columns=frozenset({"max_attempts_used"}), current_values={"max_attempts_used": 3},
                   boundary=HistoryBoundary(1, 1), current_boundary=HistoryBoundary(4, 4))
    request.update(changes)
    return reader.read_row_values_at_boundary(connection, **request)


def test_read_restores_only_requested_row_and_columns(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    assert _read(reader, connection) == {"max_attempts_used": 1}


def test_read_at_current_boundary_does_not_rewind(database, monkeypatch):
    connection, data = database
    data["floor"] = data["head"]
    reader = _reader(monkeypatch, data)
    assert _read(reader, connection, boundary=HistoryBoundary(4, 4)) == {"max_attempts_used": 3}


@pytest.mark.parametrize("column", ["id", "last_event_id", "unknown"])
def test_read_rejects_nonbusiness_columns(database, monkeypatch, column):
    connection, data = database
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection, columns=frozenset({column}), current_values={column: 3})


def test_read_rejects_missing_requested_current_value(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection, current_values={})


@pytest.mark.parametrize("field,value", [
    ("primary", None), ("primary", (3, 4)), ("primary", (4, 5)),
    ("head", None), ("head", (5, 4)), ("floor", (1, 5)),
])
def test_read_rejects_inconsistent_catalog_anchors(database, monkeypatch, field, value):
    connection, data = database
    data[field] = value
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_rejects_missing_middle_catalog_entry(database, monkeypatch):
    connection, data = database
    del data["events"][1]
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_rejects_missing_last_catalog_entry(database, monkeypatch):
    connection, data = database
    del data["events"][0]
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_rejects_wrong_original_boundary(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    reader.read_transaction_range.side_effect = lambda connection, identity, validated=None: (
        TransactionRange(1, 1, 2) if identity == 1 else TransactionRange(identity, identity, identity))
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_rejects_future_boundary(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection, boundary=HistoryBoundary(5, 5))


def test_read_rejects_event_outside_its_transaction_range(database, monkeypatch):
    connection, data = database
    data["events"][0] = (4, replace(data["events"][0][1], transaction_id=2))
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_rejects_inconsistent_current_field_value(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    with pytest.raises(ConsistencyError):
        _read(reader, connection, current_values={"max_attempts_used": True})


def test_read_rejects_uninterpretable_stored_event(database, monkeypatch):
    connection, data = database
    reader = _reader(monkeypatch, data)
    reader.decode_event_row.side_effect = ConsistencyError("存储正文无法解释")
    with pytest.raises(ConsistencyError):
        _read(reader, connection)


def test_read_pages_catalog_with_bounded_buffers(database, monkeypatch):
    connection, data = database
    data["primary"] = data["head"] = (132, 132)
    data["events"] = [(index, _event(index, 7, {"max_attempts_used": index - 1}, {"max_attempts_used": index}))
                      for index in range(132, 1, -1)]
    reader = _reader(monkeypatch, data)
    assert _read(reader, connection, columns=frozenset({"max_attempts_used"}),
                 current_values={"max_attempts_used": 132}, current_boundary=HistoryBoundary(132, 132)) == {"max_attempts_used": 1}
    assert len(data["pages"]) >= 2
    assert all(size <= limit <= 128 for size, limit in data["pages"])


@pytest.mark.parametrize("fault", [None, "primary", "head", "floor", "page"])
def test_read_releases_every_acquired_cursor_on_return_or_error(database, monkeypatch, fault):
    connection, data = database
    data["fault"] = fault
    reader = _reader(monkeypatch, data)
    if fault is None:
        _read(reader, connection)
    else:
        with pytest.raises(sqlite3.OperationalError):
            _read(reader, connection)
    assert data["cursors"]
    assert all(cursor.close.call_count == 1 for cursor in data["cursors"])
