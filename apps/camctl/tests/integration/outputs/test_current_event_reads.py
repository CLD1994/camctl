"""限定只读端口使用原写事务及已验证前序事件，错误不表示空范围。"""

import sqlite3
from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.history.reads import ProductReadScope
from camctl.history.validators import ValidatedEvent
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.repositories.outputs import _BoundedProductReads
from camctl.outputs.competition import has_product_predecessor
from camctl.persistence.transaction import event_envelope, row_change, row_facts, update_change

from .test_product_competition import competition_database  # noqa: F401
from .test_qualification import qualification_environment, _NOW  # noqa: F401
from ..operations.test_result_reuse import _FaultConnection


def _scope():
    return ProductReadScope(31, 701, _NOW, 1)


@pytest.mark.parametrize("table,column,identity", [
    ("history_events", "id", 1), ("actions", "name", 1),
    ("actions; DROP TABLE actions", "id", 1), ("actions", "status", True),
])
def test_current_fact_port_rejects_unregistered_ranges(competition_database, table, column, identity):
    connection = competition_database.connection
    connection.execute("BEGIN")
    try:
        with pytest.raises((ConsistencyError, ValueError)):
            tuple(SqlEventReads(connection, _scope()).iter_members(table, column, identity))
    finally:
        connection.rollback()


def test_current_fact_port_requires_original_transaction(competition_database):
    with pytest.raises(ConsistencyError):
        SqlEventReads(competition_database.connection, _scope()).required("actions", 31)


def test_current_fact_port_query_error_is_not_empty(competition_database):
    connection = competition_database.connection
    connection.execute("BEGIN")
    try:
        fault = _FaultConnection(connection, "SELECT id FROM actions")
        with pytest.raises(sqlite3.OperationalError):
            tuple(SqlEventReads(fault, _scope()).actions())
    finally:
        connection.rollback()


@pytest.mark.parametrize("field,value", [("action_id", 91), ("output_id", 702), ("occurred_at", _NOW + 1)])
def test_product_guard_rejects_wrong_read_scope(competition_database, field, value):
    connection = competition_database.connection
    connection.execute("BEGIN")
    try:
        reads = _BoundedProductReads(SqlEventReads(connection, replace(_scope(), **{field: value})))
        with pytest.raises(ConsistencyError):
            has_product_predecessor(reads, 31, 701, _NOW)
    finally:
        connection.rollback()


def test_current_fact_port_folds_only_validated_prefix_in_bounded_pages(competition_database):
    connection = competition_database.connection
    connection.execute("BEGIN")
    try:
        inserted = row_facts(connection, "actions", 91)
        inserted["name"] = "前序新增动作"
        inserted["status"] = 1
        rows = (update_change("actions", 91, {"status": 2}, {"status": 3}),
                row_change("actions", 92, {key: value for key, value in inserted.items() if key != "id"}))
        accepted = ValidatedEvent(event_envelope(2, 2, 8, 1, rows, _NOW), "ACTION_FINISHED", "SUCCEEDED", (), {})
        initial = SqlEventReads(connection, _scope())
        current = SqlEventReads(connection, _scope(), (accepted,))
        assert initial.optional("actions", 92) is None
        assert initial.required("actions", 91)["status"] == 2
        pages = tuple(current.iter_members("actions", "status", 1, columns=current.ACTION_COLUMNS))
        assert all(len(page) <= 1 for page in pages)
        assert [identity for page in pages for identity, _ in page] == [92]
        assert current.required("actions", 91)["status"] == 3
        assert connection.execute("SELECT status FROM actions WHERE id=91").fetchone() == (2,)
        with pytest.raises(ConsistencyError):
            tuple(current.iter_members("actions", "status", 1, limit=2))
    finally:
        connection.rollback()
