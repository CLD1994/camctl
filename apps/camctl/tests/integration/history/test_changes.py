"""H2 目录与计数在真实事务中的集成测试。

真实 SQLite 组合：事件、投影、两类目录、维护进度与派生历史列同
组提交；业务水位只由实际公开变化分配（J 系列引用与计数规则）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import register_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.persistence.transaction import CommandPlan, commit_operation

from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import PlanCreateCommand, PlanStartCommand, plan_guards

_CREATED_AT = 1_700_000_000_000_000


@pytest.fixture()
def clock_guard():
    from camctl.history import validators

    saved = validators.NAMED_GUARDS.get("clock")
    register_guard("clock", lambda event, context: None)
    yield
    if saved is None:
        validators.NAMED_GUARDS.pop("clock", None)
    else:
        validators.NAMED_GUARDS["clock"] = saved


class ClockAcceptCommand:
    """首次保存可信时间下界（内部事件：无业务水位）。"""

    def plan(self, scope):
        allocation = scope.allocate(1)
        event = EventEnvelope(
            event_id=allocation.first_event_id,
            transaction_id=allocation.txn_id,
            event_type=31,
            event_version=1,
            occurred_at=_CREATED_AT + 5,
            clock_status=1,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="runtime_state",
                    row_id=1,
                    before=RowImage(
                        exists=True,
                        values={"trusted_time_lower_bound": None, "trusted_time_event_id": None},
                    ),
                    after=RowImage(
                        exists=True,
                        values={
                            "trusted_time_lower_bound": _CREATED_AT + 5,
                            # 引用本事件：正文编码前最终范围已知。
                            "trusted_time_event_id": allocation.first_event_id,
                        },
                    ),
                ),
            ),
        )
        return CommandPlan(
            events=(event,),
            owners={("runtime_state", 1): ("runtime_state", 1)},
            state_rows={"runtime_state": {}},
        )


def _open(tmp_path: Path) -> OwnedConnection:
    return open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())


def _rows(connection: sqlite3.Connection, sql: str, parameters=()) -> list[tuple]:
    return connection.execute(sql, parameters).fetchall()


class TestCatalogsTogether:
    def test_catalogs_progress_and_derived_advance_together(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            assert commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned).kind == "completed"
            assert commit_operation(PlanStartCommand(1, before_status=1), new_operation_key(), owned).kind == "completed"
            assert commit_operation(
                PlanStartCommand(1, before_status=2, after_status=3), new_operation_key(), owned
            ).kind == "completed"

            # J-04：同一对象连续计数，逐事件一次。
            links = _rows(
                connection,
                "SELECT event_id, change_count FROM entity_event_links"
                " WHERE entity_type = 4 ORDER BY change_count",
            )
            assert links == [(1, 1), (2, 2), (3, 3)]
            # 业务水位只分配给实际公开变化，事件与序号成对。
            changes = _rows(
                connection,
                "SELECT r.event_id, r.change_seq, e.change_seq FROM report_entity_changes r"
                " JOIN history_events e ON e.id = r.event_id AND e.change_seq = r.change_seq"
                " ORDER BY r.change_seq",
            )
            assert changes == [(1, 1, 1), (2, 2, 2), (3, 3, 3)]
            progress = _rows(
                connection,
                "SELECT current_change_count, snapshot_change_count FROM entity_snapshot_progress",
            )
            assert progress == [(3, 0)]
            plan = _rows(
                connection,
                "SELECT status, created_event_id, last_event_id, change_count FROM plans",
            )
            assert plan == [(3, 1, 3, 3)]
        finally:
            connection.close()

    def test_internal_event_has_no_watermark_and_consumes_none(
        self, tmp_path, plan_guards, clock_guard
    ) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            assert commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned).kind == "completed"
            internal = commit_operation(ClockAcceptCommand(), new_operation_key(), owned)
            assert internal.kind == "completed"
            assert commit_operation(PlanStartCommand(1, before_status=1), new_operation_key(), owned).kind == "completed"

            # 内部事件：change_seq 为空，不占用业务水位。
            internal_event = _rows(
                connection, "SELECT id, change_seq FROM history_events WHERE id = 2"
            )
            assert internal_event == [(2, None)]
            # 后续公开变化沿用连续水位，没有被内部事件消耗。
            public = _rows(
                connection,
                "SELECT r.event_id, r.change_seq, e.id FROM report_entity_changes r"
                " JOIN history_events e ON e.id = r.event_id"
                " ORDER BY r.change_seq",
            )
            assert public == [(1, 1, 1), (3, 2, 3)]
            # runtime_state 保存恢复关联与首次计数，但不参加快照维护。
            runtime_link = _rows(
                connection,
                "SELECT entity_type, entity_id, event_id, change_count FROM entity_event_links"
                " WHERE entity_type = 8",
            )
            assert runtime_link == [(8, 1, 2, 1)]
            assert _rows(
                connection, "SELECT COUNT(*) FROM entity_snapshot_progress WHERE entity_type = 8"
            ) == [(0,)]
            # 可信时间事件引用自身（引用 E 在编码前已知最终范围）。
            assert _rows(
                connection, "SELECT trusted_time_lower_bound, trusted_time_event_id FROM runtime_state"
            ) == [(_CREATED_AT + 5, 2)]
        finally:
            connection.close()
