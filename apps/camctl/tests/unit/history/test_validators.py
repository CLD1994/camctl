"""P3 事件校验器的状态转换豁免与归属解析单元测试。

权威规则来自 docs/camctl/database/event-transitions.md：状态字段没
有改变时无需状态转换，但同一记录必须有其他真实变化；归属规格按
cases、inherit 与 via 链解析到唯一历史对象。
"""

from __future__ import annotations

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import (
    EventContext,
    EventValidationError,
    _check_transitions,
    _ownership_guard,
)


def _update(table: str, row_id: int, before: dict, after: dict) -> RowChange:
    return RowChange(
        table=table,
        row_id=row_id,
        before=RowImage(exists=True, values=before),
        after=RowImage(exists=True, values=after),
    )


def _context(owners=None, state_rows=None) -> EventContext:
    return EventContext(
        transaction=TransactionRange(txn_id=1, first_event_id=1, last_event_id=1),
        owners=owners or {},
        state_rows=state_rows or {},
    )


class TestUnchangedStateNeedsNoTransition:
    """状态列保持原值时不要求登记转换边。"""

    def test_unchanged_status_is_exempt(self) -> None:
        spec = {
            "columns": ["status", "attempts_used"],
            "before": {"status": [1, 2]},
            "after": {"status": [2]},
            "transitions": {"status": ["transition_1"]},
        }
        row = _update(
            "operation_runs",
            5,
            {"status": 2, "attempts_used": 1},
            {"status": 2, "attempts_used": 2},
        )
        _check_transitions(row, spec, "ATTEMPT_STARTED", "NEW")

    def test_changed_status_still_requires_edge(self) -> None:
        spec = {
            "columns": ["status"],
            "transitions": {"status": ["transition_1"]},
        }
        row = _update("operation_runs", 5, {"status": 2}, {"status": 2 - 1})
        with pytest.raises(EventValidationError):
            _check_transitions(row, spec, "ATTEMPT_STARTED", "NEW")


class TestOwnershipResolution:
    """归属规格按登记解析到唯一对象；解析事实缺失时明确拒绝。"""

    @staticmethod
    def _envelope(row: RowChange) -> EventEnvelope:
        return EventEnvelope(
            event_id=1,
            transaction_id=1,
            event_type=11,
            event_version=1,
            occurred_at=1,
            clock_status=2,
            change_seq=None,
            reason=1,
            evidence={},
            rows=(row,),
        )

    def test_simple_spec_verifies_entity_and_id(self) -> None:
        row = _update(
            "device_activities",
            3,
            {"dispatch_state": 1},
            {"dispatch_state": 2},
        )
        context = _context(
            owners={("device_activities", 3): ("action", 7)},
            state_rows={"device_activities": {3: {"id": 3, "action_id": 7}}},
        )
        _ownership_guard(self._envelope(row), context)

    def test_wrong_entity_is_rejected(self) -> None:
        row = _update(
            "device_activities",
            3,
            {"dispatch_state": 1},
            {"dispatch_state": 2},
        )
        context = _context(
            owners={("device_activities", 3): ("plan", 7)},
            state_rows={"device_activities": {3: {"id": 3, "action_id": 7}}},
        )
        with pytest.raises(EventValidationError):
            _ownership_guard(self._envelope(row), context)

    def test_case_spec_selects_branch_by_facts(self) -> None:
        # kind=8 走 via activity_id 分支：归属是活动所属动作，不是触发动作。
        row = RowChange(
            table="operation_runs",
            row_id=11,
            before=RowImage(exists=False, values={}),
            after=RowImage(
                exists=True,
                values={"id": 11, "kind": 8, "action_id": 7, "activity_id": 3},
            ),
        )
        context = _context(
            owners={("operation_runs", 11): ("action", 42)},
            state_rows={"device_activities": {3: {"id": 3, "action_id": 42}}},
        )
        _ownership_guard(self._envelope(row), context)

    def test_inherit_spec_follows_reference(self) -> None:
        # 尝试行继承所属流程的归属；kind=3 的流程再继承拷贝归属。
        row = RowChange(
            table="operation_attempts",
            row_id=21,
            before=RowImage(exists=False, values={}),
            after=RowImage(exists=True, values={"id": 21, "run_id": 12}),
        )
        context = _context(
            owners={
                ("operation_attempts", 21): ("delivery", 2),
                ("operation_runs", 12): ("delivery", 2),
                ("file_copies", 9): ("delivery", 2),
            },
            state_rows={
                "operation_runs": {
                    12: {"id": 12, "kind": 3, "copy_id": 9},
                },
                "file_copies": {
                    9: {"id": 9, "delivery_id": 2, "processing_id": None},
                },
            },
        )
        _ownership_guard(self._envelope(row), context)

    def test_missing_reference_facts_are_rejected(self) -> None:
        row = RowChange(
            table="operation_attempts",
            row_id=21,
            before=RowImage(exists=False, values={}),
            after=RowImage(exists=True, values={"id": 21, "run_id": 12}),
        )
        context = _context(
            owners={("operation_attempts", 21): ("action", 7)},
            state_rows={
                # 缺少 operation_runs#12 的事实，继承链无法解析。
            },
        )
        with pytest.raises(EventValidationError):
            _ownership_guard(self._envelope(row), context)
