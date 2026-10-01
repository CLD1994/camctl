"""R3 固定 H 分批历史查询的组件集成测试。

同一短读事务中取得投影与绑定 C；逐批独立拥有；游标严格推进；
批间新提交不改变同 H 的结果；读取错误不返回空页。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.contracts.history_values import HistoryBoundary, ReadOrder, ReadScope
from camctl.contracts.pages import Page
from camctl.persistence.repositories.history import HistoryRepository

from ..persistence.test_runtime import _create_valid_database

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
    from camctl.persistence.repositories.acceptance import (
        AcceptanceRepository,
        register_acceptance_guards,
    )
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    register_acceptance_guards()
    catalog = StubCatalog()

    class _Reader:
        def read(self, path: str) -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

    owned = open_existing(repository.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        import json

        for index in range(count):
            body = {
                "request_id": str(100 + index),
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
            await accept_input(
                source,
                AcceptanceContext(
                    mode=CommandMode.SUBMIT,
                    catalog=catalog,
                    repository=AcceptanceRepository(),
                    clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
                ),
                new_operation_key(),
                owned,
            )
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
        after = repository.read_events(scope, boundary)
        assert [e.event_id for e in after.items] == [e.event_id for e in before.items]

    async def test_batch_limit_validated(self, repository: HistoryRepository) -> None:
        boundary = await _seed_plans(repository, 1)
        with pytest.raises(Exception):
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
