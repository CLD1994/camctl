"""SQL 旧值核对的精确 JSON 契约；连接由受接口约束的替身隔离。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.persistence import transaction


def _write(monkeypatch, raw):
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (raw,)
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value = cursor
    monkeypatch.setattr(transaction, "json_columns", create_autospec(
        transaction.json_columns, return_value={"sample": frozenset({"payload_json"})}))
    event = EventEnvelope(1, 1, 1, 1, 0, 1, None, 1, {}, (
        RowChange("sample", 7,
                  RowImage(True, {"payload_json": {"count": 1, "ready": True}}),
                  RowImage(True, {"payload_json": {"count": 2, "ready": True}})),))
    transaction._write_projections(connection, (event,), {})


def test_projection_old_json_accepts_equivalent_structure(monkeypatch):
    _write(monkeypatch, '{ "ready":true, "count":1e0 }')


@pytest.mark.parametrize("raw", ['{"ready":1,"count":1}', '{"ready":true,"count":1.0000000000000001}'])
def test_projection_old_json_rejects_different_exact_value(monkeypatch, raw):
    with pytest.raises(transaction.TransactionError):
        _write(monkeypatch, raw)
