"""发现页的责任与继续位置不依赖候选详情缓存。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.pages import Page
from camctl.outputs.read_discovery import (
    ReadDiscoveryCursor, ReadDiscoveryOwner, ReadDiscoveryScope, check_cursor,
    next_preparation_at,
)
from camctl.outputs import read_discovery
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


@pytest.mark.asyncio
async def test_round_database_reads_finish_before_loop_frontier_changes():
    owner = ReadDiscoveryOwner(1)
    owner.observe(("db", 5), 123)
    scope = owner.scope("reads", "db", 123)
    owner.accept(scope, Page((), ReadDiscoveryCursor(scope, (8,))))
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    def execute(sql, parameters=()):
        assert owner.pending
        assert owner._stamp == ("db", 5)
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.fetchone.return_value = (6,) if "MAX(id)" in sql else (200,)
        cursor.fetchall.return_value = [(0, "main", "db")]
        return cursor
    owned.connection.execute.side_effect = execute
    assert await owner.begin_round(inline_business_database(owned), 124) == "db"
    assert not owner.pending
    assert owner._stamp == ("db", 6)
    assert owner._deadline == 200


def test_empty_page_with_cursor_continues():
    owner = ReadDiscoveryOwner(1)
    scope = owner.scope("reads", "db", 123)
    owner.accept(scope, Page((), ReadDiscoveryCursor(scope, (4,))))
    assert owner.pending
    assert owner.scope("reads", "db", 999) is scope
    owner.accept(scope, Page((), None))
    assert not owner.pending


@pytest.mark.parametrize("field,value", [
    ("database", "other"), ("kind", "local"), ("now_us", 124),
    ("generation", 1), ("device_id", "camera"),
])
def test_cursor_is_bound_to_scope_and_generation(field, value):
    scope = ReadDiscoveryScope("db", "reads", 123, 0)
    with pytest.raises(ValueError):
        check_cursor(replace(scope, **{field: value}), ReadDiscoveryCursor(scope, (1,)))


def test_eviction_keeps_discovery_responsibility():
    owner = ReadDiscoveryOwner(2)
    scope = owner.scope("local", "db", 123)
    page = Page(({"id": 1},), ReadDiscoveryCursor(scope, (2,)))
    owner.accept(scope, page)
    del page
    assert owner.pending and owner.cursor_count == 1
    assert not any(isinstance(value, dict) for value in owner._positions.values())


def test_new_eligibility_invalidates_old_frontier():
    owner = ReadDiscoveryOwner(1)
    owner.observe(("db", 5), 123)
    scope = owner.scope("reads", "db", 123)
    owner.accept(scope, Page((), ReadDiscoveryCursor(scope, (100,))))
    owner.observe(("db", 6), 124)
    assert not owner.pending
    with pytest.raises(ValueError):
        owner.accept(scope, Page((), None))


@pytest.mark.asyncio
async def test_future_capture_query_uses_shared_types_and_original_photo_start_rule(mocker):
    mocker.patch.object(read_discovery, "CAPTURE_ACTION_TYPES", (77,))
    mocker.patch("camctl.capture.preparation_discovery.photo_preparation_start_where",
        autospec=True, return_value="photo_start_allowed")
    mocker.patch.object(read_discovery, "next_preparation_at", autospec=True, return_value=None)
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchall.return_value = []
    owner = ReadDiscoveryOwner(10)
    owner._stamp = ("db", 1)

    owned = OwnedConnection(connection, DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    assert (await owner.captures(inline_business_database(owned), 1_000_000, {"photo": Decimal("2.5")})).items == ()

    query, parameters = connection.execute.call_args.args
    assert query.count("type IN (77)") == 2
    assert "scheduled_at<=? AND photo_start_allowed" in query
    assert parameters == (1_000_000, "photo", 3_500_000, 10)


def test_future_preparation_deadline_uses_shared_types_and_original_photo_start_rule(mocker):
    mocker.patch.object(read_discovery, "CAPTURE_ACTION_TYPES", (77,))
    mocker.patch("camctl.capture.preparation_discovery.photo_preparation_start_where",
        autospec=True, return_value="photo_start_allowed")
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = (10_000_500,)

    assert next_preparation_at(connection, 1_000_000, {"photo": Decimal("2.5")}) == 7_500_500

    query, parameters = connection.execute.call_args.args
    assert "type IN (77)" in query and query.endswith(" AND photo_start_allowed")
    assert parameters == ("photo", 3_500_000)
