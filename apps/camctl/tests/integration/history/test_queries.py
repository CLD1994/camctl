"""固定 H 全局事件分页和报告候选查询的组件集成测试。

事件页逐批独立拥有，游标严格推进；批间新提交不改变固定事件范围；
读取错误不返回空页。对象投影与 C 的组合恢复由后续验收覆盖。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import url2pathname
import os
import json
import sqlite3

import pytest

from camctl.contracts.history_values import BoundaryError, HistoryBoundary, INITIAL_BOUNDARY, ReadOrder, ReadScope
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.history import HistoryRepository

from ..persistence.test_runtime import _create_valid_database
from ..support.database import business_database
from ..support.history_rows import observe_readonly_rows

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def repository(tmp_path: Path) -> HistoryRepository:
    _create_valid_database(tmp_path / "state.db")
    return HistoryRepository(tmp_path / "state.db")


async def _seed_plans(repository: HistoryRepository, count: int) -> HistoryBoundary:
    """经受理仓储建立 count 个计划并返回完整 H。"""
    from camctl.acceptance.input import parse_input, read_input
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from unit.acceptance.helpers import StubCatalog
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    catalog = StubCatalog()

    class _Reader:
        def read(self, path: str) -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

    owned = open_existing(repository.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        import json

        previous = owned.connection.execute("SELECT MAX(request_id) FROM plans").fetchone()[0]
        first_request = 100 if previous is None else previous + 1
        async with business_database(repository.path) as database:
            for index in range(count):
                body = {
                    "request_id": str(first_request + index),
                    "created_at": "2026-01-15 08:00:00",
                    "name": f"plan-{index}",
                    "actions": [
                        {
                            "name": "shoot",
                            "type": "camera_take_photo",
                            "device_id": "cam-1",
                            "scheduled_at": "2026-01-15 09:00:00",
                            "params": {"type": "single_shot"},
                            "policy": {"max_delay_ms": 1000},
                        }
                    ],
                }
                source = parse_input(
                    await read_input(f"/tmp/plan-{index}.json", _StubSource(body), )
                )
                accepted = await accept_input(
                    source,
                    AcceptanceContext(
                        mode=CommandMode.RUN,
                        catalog=catalog,
                        repository=AcceptanceRepository(),
                        clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
                    ),
                    new_operation_key(),
                    database,
                )
                assert accepted.plan_id is not None
        boundary = repository.current_boundary()
        return boundary
    finally:
        owned.connection.close()


class _StubSource:
    def __init__(self, body: dict) -> None:
        self._body = body

    def read(self, path: str) -> bytes:
        import json

        return json.dumps(self._body).encode("utf-8")


class TestReadEvents:
    async def test_descending_events_follow_scope_order(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 3)
        page = repository.read_events(
            ReadScope(
                order=ReadOrder.DESCENDING, previous_position=None,
                lower_position=1, upper_position=boundary.last_event_id,
                batch_limit=2, cursor_position=lambda cursor: cursor,
            ), boundary,
        )
        assert [event.event_id for event in page.items] == [6, 5]
        collected = list(page.items)
        while not page.exhausted:
            page = repository.read_events(
                ReadScope(
                    order=ReadOrder.DESCENDING, previous_position=page.next_cursor,
                    lower_position=1, upper_position=boundary.last_event_id,
                    batch_limit=2, cursor_position=lambda cursor: cursor,
                ), boundary,
            )
            collected.extend(page.items)
        assert [event.event_id for event in collected] == [6, 5, 4, 3, 2, 1]

    async def test_first_page_obeys_lower_bound(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 2)
        page = repository.read_events(
            ReadScope(
                order=ReadOrder.ASCENDING, previous_position=None,
                lower_position=3, upper_position=boundary.last_event_id,
                batch_limit=10, cursor_position=lambda cursor: cursor,
            ), boundary,
        )
        assert [event.event_id for event in page.items] == [3, 4]
        assert page.exhausted

    async def test_unbounded_scope_uses_frozen_history_end(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 2)
        page = repository.read_events(
            ReadScope(
                order=ReadOrder.ASCENDING, previous_position=None,
                lower_position=1, upper_position=None,
                batch_limit=10, cursor_position=lambda cursor: cursor,
            ), boundary,
        )
        assert [event.event_id for event in page.items] == [1, 2, 3, 4]
        assert page.exhausted

    async def test_paged_events_bind_boundary(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 3)
        scope = ReadScope(
            order=ReadOrder.ASCENDING,
            previous_position=None,
            lower_position=0,
            upper_position=boundary.last_event_id,
            batch_limit=2,
            cursor_position=lambda cursor: int(cursor),
        )
        first = repository.read_events(scope, boundary)
        assert isinstance(first, Page)
        assert len(first.items) == 2
        assert first.next_cursor == 2
        second_scope = ReadScope(
            order=ReadOrder.ASCENDING,
            previous_position=first.next_cursor,
            lower_position=0,
            upper_position=boundary.last_event_id,
            batch_limit=2,
            cursor_position=lambda cursor: int(cursor),
        )
        collected = list(first.items)
        cursor = first.next_cursor
        while cursor is not None:
            scope_next = ReadScope(
                order=ReadOrder.ASCENDING,
                previous_position=cursor,
                lower_position=0,
                upper_position=boundary.last_event_id,
                batch_limit=2,
                cursor_position=lambda value: int(value),
            )
            page = repository.read_events(scope_next, boundary)
            if page.items:
                assert page.items[0].event_id > collected[-1].event_id
            collected.extend(page.items)
            cursor = page.next_cursor
        # 3 个计划 × 2 条事件：全部读取后结束。
        assert len(collected) == 6
        assert cursor is None

    async def test_new_commits_do_not_enter_frozen_h(
        self, repository: HistoryRepository
    ) -> None:
        boundary = await _seed_plans(repository, 2)
        scope = ReadScope(
            order=ReadOrder.ASCENDING,
            previous_position=None,
            lower_position=0,
            upper_position=boundary.last_event_id,
            batch_limit=10,
            cursor_position=lambda cursor: int(cursor),
        )
        before = repository.read_events(scope, boundary)
        # 冻结后新提交：同 H 读取结果不变。
        await _seed_plans(repository, 1)
        assert repository.current_boundary().last_event_id > boundary.last_event_id
        after = repository.read_events(scope, boundary)
        assert [e.event_id for e in after.items] == [e.event_id for e in before.items]

    async def test_batch_limit_validated(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 1)
        with pytest.raises(BoundaryError):
            repository.read_events(
                ReadScope(
                    order=ReadOrder.ASCENDING,
                    previous_position=None,
                    lower_position=0,
                    upper_position=boundary.last_event_id,
                    batch_limit=0,
                    cursor_position=lambda cursor: int(cursor),
                ),
                boundary,
            )


class TestReadEntities:
    async def test_entity_ids_selected_by_watermark_window(
        self, repository: HistoryRepository
    ) -> None:
        await _seed_plans(repository, 3)
        ids = repository.select_report_entities(
            entity_type=4, from_wm=0, to_wm=10_000, after_id=0, limit=10
        )
        assert ids == [1, 2, 3]
        # 续读严格越过最后一个 ID。
        more = repository.select_report_entities(
            entity_type=4, from_wm=0, to_wm=10_000, after_id=3, limit=10
        )
        assert more == []

    async def test_window_excludes_higher_watermarks(
        self, repository: HistoryRepository
    ) -> None:
        await _seed_plans(repository, 2)
        first_wm = repository.current_boundary()
        ids = repository.select_report_entities(
            entity_type=4, from_wm=0, to_wm=0, after_id=0, limit=10
        )
        assert ids == []


def _scope(boundary, **overrides):
    values = {"order": ReadOrder.ASCENDING, "previous_position": None, "lower_position": 1,
              "upper_position": boundary.last_event_id, "batch_limit": 10,
              "cursor_position": lambda cursor: cursor}
    values.update(overrides)
    return ReadScope(**values)


@pytest.mark.parametrize("target", [HistoryBoundary(999, 4), HistoryBoundary(1, 1),
                                    HistoryBoundary(1, 4), HistoryBoundary(999, 999)])
async def test_false_history_boundary_cannot_return_a_successful_page(repository, target):
    await _seed_plans(repository, 2)
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(target), target)


@pytest.mark.parametrize("field,bad", [("lower_position", True), ("lower_position", "1"),
                                      ("upper_position", 1.5), ("previous_position", "1"),
                                      ("previous_position", False), ("previous_position", -1),
                                      ("previous_position", 99)])
async def test_invalid_range_or_cursor_is_not_coerced_by_sqlite(repository, field, bad):
    boundary = await _seed_plans(repository, 1)
    with pytest.raises(BoundaryError):
        repository.read_events(_scope(boundary, **{field: bad}), boundary)


async def test_range_above_frozen_boundary_is_rejected_instead_of_clipped(repository):
    boundary = await _seed_plans(repository, 1)
    with pytest.raises(BoundaryError):
        repository.read_events(_scope(boundary, upper_position=99), boundary)


async def test_missing_event_is_not_an_empty_or_partial_success(repository):
    boundary = await _seed_plans(repository, 2)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("DELETE FROM history_events WHERE id = 2")
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary), boundary)


async def test_group_membership_mismatch_is_rejected(repository):
    boundary = await _seed_plans(repository, 2)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE history_events SET transaction_id = 2 WHERE id = 1")
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary), boundary)


async def test_missing_evidence_in_stored_event_is_not_an_empty_default(repository):
    boundary = await _seed_plans(repository, 1)
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute("SELECT body_json FROM history_events WHERE id = 1").fetchone()
        body = json.loads(row[0])
        del body["evidence"]
        connection.execute("UPDATE history_events SET body_json = ? WHERE id = 1", (json.dumps(body),))
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary), boundary)


async def test_successful_page_is_returned_after_explicit_read_transaction_ends(repository, monkeypatch):
    boundary = await _seed_plans(repository, 1)
    returned = False
    ended = []

    def end_view(connection):
        assert not returned and not connection.in_transaction
        ended.append(connection)

    opened = observe_readonly_rows(monkeypatch, repository.path, lambda *_: None, end_view)
    page = repository.read_events(_scope(boundary), boundary)
    returned = True
    assert [event.event_id for event in page.items] == [1, 2]
    assert page.exhausted and ended and opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


async def test_initial_boundary_excludes_later_committed_events(repository):
    await _seed_plans(repository, 1)
    page = repository.read_events(_scope(INITIAL_BOUNDARY, lower_position=0), INITIAL_BOUNDARY)
    assert page.items == ()
    assert page.exhausted


async def test_descending_zero_lower_bound_ends_at_first_event(repository):
    boundary = await _seed_plans(repository, 1)
    page = repository.read_events(_scope(boundary, order=ReadOrder.DESCENDING,
                                        lower_position=0, batch_limit=2), boundary)
    assert [event.event_id for event in page.items] == [2, 1]
    assert page.exhausted


async def test_continuation_does_not_rescan_the_same_immutable_transaction(repository, monkeypatch):
    boundary = await _seed_plans(repository, 1)
    aggregates = _observe_group_aggregates(repository.path, monkeypatch)
    first = repository.read_events(_scope(boundary, batch_limit=1), boundary)
    second = repository.read_events(_scope(boundary, batch_limit=1,
                                          previous_position=first.next_cursor), boundary)
    assert [event.event_id for event in first.items + second.items] == [1, 2]
    assert second.exhausted
    # 记录实际执行的完整成员聚合，不推断其他查询的扫描量。
    assert len(aggregates) == 1


async def test_repository_rejects_a_different_database_instance_between_pages(repository):
    boundary = await _seed_plans(repository, 1)
    first = repository.read_events(_scope(boundary, batch_limit=1), boundary)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE database_metadata SET instance_id = ?", ("f" * 32,))
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary, batch_limit=1,
                                      previous_position=first.next_cursor), boundary)


@pytest.mark.parametrize("target", [None, HistoryBoundary(999, 2)])
async def test_empty_range_only_ends_after_validating_history_boundary(repository, target):
    actual = await _seed_plans(repository, 1)
    boundary = actual if target is None else target
    if target is None:
        page = repository.read_events(_scope(boundary, lower_position=3), boundary)
        assert page.items == ()
        assert page.exhausted
    else:
        with pytest.raises(ConsistencyError):
            repository.read_events(_scope(boundary, lower_position=3), boundary)


@pytest.mark.parametrize("order", [ReadOrder.ASCENDING, ReadOrder.DESCENDING])
async def test_cross_transaction_continuations_validate_each_group_once(repository, monkeypatch, order):
    boundary = await _seed_plans(repository, 3)
    aggregates = _observe_group_aggregates(repository.path, monkeypatch)
    position = None
    collected = []
    while True:
        page = repository.read_events(_scope(boundary, order=order, batch_limit=1,
                                            previous_position=position), boundary)
        collected.extend(event.event_id for event in page.items)
        if page.exhausted:
            break
        position = page.next_cursor
    assert collected == ([1, 2, 3, 4, 5, 6] if order is ReadOrder.ASCENDING else [6, 5, 4, 3, 2, 1])
    assert len(aggregates) == 3


async def test_cached_transaction_metadata_is_checked_on_continuation(repository):
    boundary = await _seed_plans(repository, 1)
    first = repository.read_events(_scope(boundary, batch_limit=1), boundary)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE history_transactions SET last_event_id = 3 WHERE id = 1")
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary, batch_limit=1,
                                      previous_position=first.next_cursor), boundary)


def _observe_target_connections(monkeypatch, path, factory):
    real_connect = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        if kwargs.get("uri") and str(database).startswith("file:"):
            parts = urlsplit(str(database))
            file_path = url2pathname(("//" + parts.netloc if parts.netloc else "") + parts.path)
            if (parse_qs(parts.query).get("mode") == ["ro"] and os.path.exists(file_path)
                    and os.path.samefile(file_path, path)):
                assert "factory" not in kwargs
                kwargs["factory"] = factory
                connection = real_connect(database, *args, **kwargs)
                opened.append(connection)
                return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def _observe_group_aggregates(path, monkeypatch):
    aggregates = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            functions = set()
            columns = set()

            def authorize(action, first, second, _database, _source):
                if action == sqlite3.SQLITE_FUNCTION and second:
                    functions.add(second.lower())
                if action == sqlite3.SQLITE_READ and first == "history_events":
                    columns.add(second)
                return sqlite3.SQLITE_OK

            self.set_authorizer(authorize)
            try:
                cursor = super().execute(*args, **kwargs)
                if {"count", "min", "max"} <= functions and {"id", "transaction_id"} <= columns:
                    aggregates.append(True)
                return cursor
            finally:
                self.set_authorizer(None)

    _observe_target_connections(monkeypatch, path, ObservedConnection)
    return aggregates


class _TrackedCursor(sqlite3.Cursor):
    def close(self):
        super().close()
        self.was_closed = True


class _TrackedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        denied = False

        def authorize(action, first, second, _database, _source):
            nonlocal denied
            if action == sqlite3.SQLITE_TRANSACTION and first == "BEGIN":
                self.tracking = True
            if ((action == sqlite3.SQLITE_TRANSACTION and first == self.failure)
                    or (self.failure == "EVENT_PAGE" and action == sqlite3.SQLITE_READ
                        and first == "history_events" and second == "body_json")):
                denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        cursor = self.cursor(factory=_TrackedCursor)
        cursor.was_closed = False
        self.set_authorizer(authorize)
        try:
            try:
                cursor.execute(sql, parameters)
            except sqlite3.DatabaseError:
                cursor.close()
                if not denied:
                    raise
        finally:
            self.set_authorizer(None)
            if self.tracking:
                self.cursors.append(cursor)
        if denied:
            raise self.injected_error
        return cursor

    def close(self):
        super().close()
        self.was_closed = True


def _track_read_connection(repository, monkeypatch, failure=None):
    class TrackedConnection(_TrackedConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cursors = []
            self.tracking = False
            self.was_closed = False
            self.failure = failure
            self.injected_error = injected_error

    injected_error = sqlite3.OperationalError("read boundary failure")
    return _observe_target_connections(monkeypatch, repository.path, TrackedConnection), injected_error


def _assert_resources_released(connection):
    assert connection.was_closed
    assert all(cursor.was_closed for cursor in connection.cursors)
    with pytest.raises(sqlite3.ProgrammingError):
        sqlite3.Connection.execute(connection, "SELECT 1")


async def test_successful_page_owns_data_after_all_read_resources_close(repository, monkeypatch):
    boundary = await _seed_plans(repository, 1)
    opened, _ = _track_read_connection(repository, monkeypatch)
    page = repository.read_events(_scope(boundary), boundary)
    assert opened
    for connection in opened:
        _assert_resources_released(connection)
    plan = next(row for event in page.items for row in event.rows if row.table == "plans")
    assert plan.row_id == 1
    assert plan.after.values["name"] == "plan-0"


@pytest.mark.parametrize("failure", ["BEGIN", "EVENT_PAGE", "COMMIT"])
async def test_sql_failure_preserves_error_and_releases_read_resources(repository, monkeypatch, failure):
    boundary = await _seed_plans(repository, 1)
    opened, injected_error = _track_read_connection(repository, monkeypatch, failure)
    with pytest.raises(sqlite3.OperationalError) as caught:
        repository.read_events(_scope(boundary), boundary)
    assert caught.value is injected_error
    assert opened
    for connection in opened:
        _assert_resources_released(connection)


async def test_decoding_failure_releases_read_resources(repository, monkeypatch):
    boundary = await _seed_plans(repository, 1)
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE history_events SET body_json = '{}' WHERE id = 1")
    opened, _ = _track_read_connection(repository, monkeypatch)
    with pytest.raises(ConsistencyError):
        repository.read_events(_scope(boundary), boundary)
    assert opened
    for connection in opened:
        _assert_resources_released(connection)
