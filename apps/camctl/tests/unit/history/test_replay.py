"""H3 正逆应用与独立恢复预期的单元测试。

独立预期直接构造目标镜像，不从被测恢复路径生成。
"""

from __future__ import annotations

import pytest

from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY, TransactionRange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.replay import EntityImage, ReplayError, RestoreSeed, apply_forward, apply_reverse, restore
from camctl.history.validators import EventContext, ValidatedEvent

TXN = TransactionRange(txn_id=7, first_event_id=101, last_event_id=103)


def _event(event_id: int, reason: int, rows: tuple[RowChange, ...], change_seq: int | None = None) -> ValidatedEvent:
    envelope = EventEnvelope(
        event_id=event_id,
        transaction_id=7,
        event_type=1,
        event_version=1,
        occurred_at=1,
        clock_status=1,
        change_seq=change_seq,
        reason=reason,
        evidence={},
        rows=rows,
    )
    owners = {(row.table, row.row_id): (4, 1) for row in rows}
    return ValidatedEvent(
        envelope=envelope,
        event_name="PLACEHOLDER",
        branch_name="PLACEHOLDER",
        references=((4, 1),) if rows else (),
        row_owners=owners,
    )


def _action_update_event(event_id: int, change_seq: int | None) -> ValidatedEvent:
    envelope = EventEnvelope(
        event_id=event_id,
        transaction_id=7,
        event_type=8,
        event_version=1,
        occurred_at=1,
        clock_status=1,
        change_seq=change_seq,
        reason=1,
        evidence={},
        rows=(
            RowChange(
                table="actions",
                row_id=8,
                before=RowImage(exists=True, values={"status": 2, "error_code": None}),
                after=RowImage(exists=True, values={"status": 3, "error_code": None}),
            ),
        ),
    )
    return ValidatedEvent(
        envelope=envelope,
        event_name="ACTION_FINISHED",
        branch_name="SUCCESS",
        references=((1, 8),),
        row_owners={("actions", 8): (1, 8)},
    )


def _plan_image(status: int, count: int, last: int) -> EntityImage:
    return EntityImage(
        entity_type=4,
        entity_id=1,
        exists=True,
        rows={("plans", 1): {"request_id": 42, "name": "p", "created_at": 5, "status": status}},
        last_event_id=last,
        change_count=count,
    )


def _create_plan_event(event_id: int, change_seq: int = 1) -> ValidatedEvent:
    return _event(
        event_id,
        1,
        (
            RowChange(
                table="plans",
                row_id=1,
                before=RowImage(exists=False, values={}),
                after=RowImage(
                    exists=True,
                    values={"request_id": 42, "name": "p", "created_at": 5, "status": 1},
                ),
            ),
        ),
        change_seq=change_seq,
    )


def _status_event(event_id: int, before: int, after: int, change_seq: int | None) -> ValidatedEvent:
    return _event(
        event_id,
        1,
        (
            RowChange(
                table="plans",
                row_id=1,
                before=RowImage(exists=True, values={"status": before}),
                after=RowImage(exists=True, values={"status": after}),
            ),
        ),
        change_seq,
    )


class TestApply:
    def test_forward_create_and_update(self) -> None:
        empty = EntityImage(entity_type=4, entity_id=1, exists=False, rows={}, last_event_id=0, change_count=0)
        created = apply_forward(empty, _create_plan_event(101))
        assert created.exists is True
        assert created.rows[("plans", 1)]["status"] == 1
        assert created.change_count == 1
        started = apply_forward(created, _status_event(102, 1, 2, 2))
        assert started.rows[("plans", 1)]["status"] == 2
        assert started.change_count == 2

    def test_forward_update_value_mismatch_rejected(self) -> None:
        image = _plan_image(status=3, count=1, last=101)
        with pytest.raises(ReplayError):
            apply_forward(image, _status_event(102, 1, 2, None))

    def test_reverse_restores_previous_values(self) -> None:
        image = _plan_image(status=2, count=2, last=102)
        reversed_image = apply_reverse(image, _status_event(102, 1, 2, 2))
        assert reversed_image.rows[("plans", 1)]["status"] == 1
        assert reversed_image.change_count == 1
        removed = apply_reverse(reversed_image, _create_plan_event(101))
        assert removed.exists is False
        assert removed.rows == {}
        assert removed.change_count == 0

    def test_reverse_update_value_mismatch_rejected(self) -> None:
        image = _plan_image(status=3, count=2, last=102)
        with pytest.raises(ReplayError):
            apply_reverse(image, _status_event(102, 1, 2, 2))

    def test_events_of_other_entities_do_not_touch_rows(self) -> None:
        image = _plan_image(status=1, count=1, last=101)
        untouched = apply_forward(image, _action_update_event(102, 1))
        assert untouched.rows[("plans", 1)]["status"] == 1
        assert untouched.change_count == 1
        assert untouched.last_event_id == 102


class TestRestore:
    def test_reverse_stops_at_complete_boundary(self) -> None:
        # 同一事务内两条成员变化，整笔事务边界 103 处的独立预期镜像。
        final = _plan_image(status=1, count=3, last=103)
        seed = RestoreSeed(image=final, boundary=HistoryBoundary(7, 103))
        events = (
            _create_plan_event(101, 1),
            _status_event(102, 1, 2, 2),
            _status_event(103, 2, 1, None),
        )
        restored = restore(seed, events, HistoryBoundary(7, 102))
        assert restored.rows[("plans", 1)]["status"] == 2
        assert restored.change_count == 2
        independent = _plan_image(status=2, count=2, last=102)
        assert restored.rows == independent.rows
        assert restored.change_count == independent.change_count

    def test_forward_from_initial(self) -> None:
        empty = EntityImage(entity_type=4, entity_id=1, exists=False, rows={}, last_event_id=0, change_count=0)
        seed = RestoreSeed(image=empty, boundary=INITIAL_BOUNDARY)
        events = (_create_plan_event(101, 1), _status_event(102, 1, 2, 2))
        restored = restore(seed, events, HistoryBoundary(7, 102))
        assert restored.rows[("plans", 1)]["status"] == 2
        assert restored.change_count == 2

    def test_reverse_to_initial_boundary(self) -> None:
        final = _plan_image(status=2, count=2, last=102)
        seed = RestoreSeed(image=final, boundary=HistoryBoundary(7, 102))
        events = (_create_plan_event(101, 1), _status_event(102, 1, 2, 2))
        restored = restore(seed, events, INITIAL_BOUNDARY)
        assert restored.exists is False
        assert restored.rows == {}
        assert restored.change_count == 0

    def test_missing_middle_event_rejected(self) -> None:
        final = _plan_image(status=2, count=3, last=103)
        seed = RestoreSeed(image=final, boundary=HistoryBoundary(7, 103))
        events = (
            _create_plan_event(101, 1),
            # 缺少 102 的状态变化。
            _status_event(103, 2, 1, None),
        )
        with pytest.raises(ReplayError):
            restore(seed, events, HistoryBoundary(7, 102))

    def test_exact_values_survive_round_trip(self) -> None:
        from decimal import Decimal

        row = RowChange(
            table="plans",
            row_id=1,
            before=RowImage(exists=True, values={"name": Decimal("1.0000000000000001")}),
            after=RowImage(exists=True, values={"name": Decimal("2.5")}),
        )
        event = _event(102, 1, (row,), None)
        image = EntityImage(
            entity_type=4,
            entity_id=1,
            exists=True,
            rows={("plans", 1): {"request_id": 42, "name": Decimal("1.0000000000000001"), "created_at": 5, "status": 1}},
            last_event_id=101,
            change_count=1,
        )
        forward = apply_forward(image, event)
        assert forward.rows[("plans", 1)]["name"] == Decimal("2.5")
        back = apply_reverse(forward, event)
        assert back.rows[("plans", 1)]["name"] == Decimal("1.0000000000000001")
