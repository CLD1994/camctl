"""H1 事件契约在真实写事务中的集成测试。

P3 事务内核执行 H1 校验：登记外分支、错误归属或缺项守卫的事务
整组回滚，即使行内 SQL 本身可以接受也不能提交。
"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.values import ConsistencyError
from camctl.history.decoding import decode_event_row
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import register_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.persistence.transaction import CommandPlan, commit_operation

from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import PlanCreateCommand, plan_guards

_CREATED_AT = 1_700_000_000_000_000


def test_registered_observation_rejects_later_illegal_state_transition():
    body = {"reason": 2, "evidence": {"observation": {}}, "rows": [{
        "table": "device_activities", "id": 1,
        "before": {"exists": True, "values": {"dispatch_state": 1, "activity_state": 3}},
        "after": {"exists": True, "values": {"dispatch_state": 2, "activity_state": 2}},
    }]}
    with pytest.raises(ConsistencyError):
        decode_event_row((1, 1, 13, 1, 0, 2, None, json.dumps(body)))


def _open(tmp_path: Path) -> OwnedConnection:
    return open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())


class WrongOwnershipCommand:
    """创建计划但把行归属登记到错误历史对象。"""

    def plan(self, scope):
        allocation = scope.allocate(1)
        event = EventEnvelope(
            event_id=allocation.first_event_id,
            transaction_id=allocation.txn_id,
            event_type=1,
            event_version=1,
            occurred_at=_CREATED_AT,
            clock_status=2,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(
                RowChange(
                    table="plans",
                    row_id=1,
                    before=RowImage(exists=False, values={}),
                    after=RowImage(
                        exists=True,
                        values={
                            "request_id": 101,
                            "name": "plan-101",
                            "created_at": _CREATED_AT,
                            "status": 1,
                        },
                    ),
                ),
            ),
        )
        return CommandPlan(
            events=(event,),
            owners={("plans", 1): ("action", 1)},
            state_rows={"plans": {}},
        )


class MissingOwnerCommand(WrongOwnershipCommand):
    def plan(self, scope):
        plan = super().plan(scope)
        return CommandPlan(events=plan.events, owners={}, state_rows={"plans": {}})


class UnknownEventCommand(WrongOwnershipCommand):
    def plan(self, scope):
        plan = super().plan(scope)
        original = plan.events[0]
        event = EventEnvelope(
            event_id=original.event_id,
            transaction_id=original.transaction_id,
            event_type=99,
            event_version=1,
            occurred_at=_CREATED_AT,
            clock_status=2,
            change_seq=None,
            reason=1,
            evidence={},
            rows=original.rows,
        )
        return CommandPlan(events=(event,), owners={("plans", 1): ("plan", 1)}, state_rows={"plans": {}})


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class TestProductionValidationRejects:
    @pytest.mark.parametrize(
        ("command_type", "message"),
        [
            (WrongOwnershipCommand, "归属"),
            (MissingOwnerCommand, "归属未提供"),
            (UnknownEventCommand, "未知事件类型"),
        ],
    )
    def test_rejected_transaction_leaves_no_trace(self, tmp_path, plan_guards, command_type, message) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            receipt = commit_operation(command_type(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert message in str(receipt.error)
            for table in ("history_transactions", "history_events", "plans", "entity_event_links"):
                assert _count(connection, table) == 0, table
        finally:
            connection.close()

    def test_valid_transaction_commits_after_rejections(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            rejected = commit_operation(WrongOwnershipCommand(), new_operation_key(), owned)
            assert rejected.kind == "rolled_back"
            accepted = commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned)
            assert accepted.kind == "completed"
            # 回滚编号可被后续实际提交使用：事务与事件都从 1 开始。
            assert _count(connection, "history_transactions") == 1
            assert _count(connection, "history_events") == 1
        finally:
            connection.close()
