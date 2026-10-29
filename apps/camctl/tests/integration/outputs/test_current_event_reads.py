"""限定只读端口使用原写事务及已验证前序事件，错误不表示空范围。"""

import sqlite3
from dataclasses import replace

import pytest

from camctl.history.validators import EventValidationError

from camctl.contracts.values import ConsistencyError
from camctl.history.reads import ProductReadScope
from camctl.history.validators import ValidatedEvent
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.repositories.outputs import OutputsRepository
from ..support.output_planning import capture_operation, EventSequence
from ..support.output_storage_cases import QueryProbe, grant_candidate
from camctl.persistence.transaction import (
    event_envelope,
    row_change,
    row_facts,
    update_change,
    commit_operation,
)
from camctl.contracts.values import new_operation_key

from .test_product_competition import competition_database  # noqa: F401
from .test_qualification import qualification_environment, _NOW  # noqa: F401


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
        fault = QueryProbe(
            connection,
            lambda columns, reads, parameters: columns == {"id"}
            and ("actions", "status") in reads,
            failure="execute",
        )
        with pytest.raises(sqlite3.OperationalError):
            tuple(SqlEventReads(fault, _scope()).actions())
    finally:
        connection.rollback()


@pytest.mark.parametrize(
    "field,value", [("action_id", 91), ("output_id", 702), ("occurred_at", _NOW + 1)]
)
def test_product_guard_rejects_wrong_read_scope(competition_database, field, value):
    owned = competition_database
    request = grant_candidate(owned, occurred_at=_NOW)
    proposal = capture_operation(
        owned,
        lambda actual: OutputsRepository().grant_file(
            request, new_operation_key(), actual
        ),
    )
    proposal = replace(proposal, product_read_scope=replace(_scope(), **{field: value}))
    before = tuple(owned.connection.iterdump())
    result = commit_operation(
        EventSequence(proposal.events, proposal), new_operation_key(), owned
    )
    assert result.kind == "rolled_back"
    assert isinstance(result.error, EventValidationError), result.error
    assert tuple(owned.connection.iterdump()) == before


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
