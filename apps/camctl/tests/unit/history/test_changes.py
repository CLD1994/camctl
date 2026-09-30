"""H2 历史归属、公开变化与计数派生的单元测试。

期望独立来自报告字段依赖登记（routes、entities.parent、进入公开投影的列）、
历史对象登记与事件转换登记；derive_changes 只读取调用方传入的事务前后事实，
不读取任何当前数据库关系。
"""

from __future__ import annotations

import pytest

from camctl.history.changes import (
    ChangeDerivationError,
    EntityEventLink,
    ProgressUpdate,
    ReportEntityChange,
    StateSlice,
    derive_changes,
    fill_in_parents,
)
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import ValidatedEvent

# 历史对象编号：action=1 delivery=2 output=3 plan=4 diagnostic=5 device_file=9。
ACTION, DELIVERY, OUTPUT, PLAN, DIAGNOSTIC, DEVICE_FILE, INTERMEDIATE_FILE = 1, 2, 3, 4, 5, 9, 10

_SHA = "a" * 64


def _envelope(
    event_id: int,
    event_type: int,
    reason: int,
    rows: tuple[RowChange, ...],
    change_seq: int | None = None,
    transaction_id: int = 7,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        transaction_id=transaction_id,
        event_type=event_type,
        event_version=1,
        occurred_at=1_700_000_000_000_000,
        clock_status=2,
        change_seq=change_seq,
        reason=reason,
        evidence={},
        rows=rows,
    )


def _validated(
    envelope: EventEnvelope,
    event_name: str,
    branch_name: str,
    owners: dict[tuple[str, int], tuple[int, int]],
) -> ValidatedEvent:
    """按 H1 语义构造已验证事件：引用为逐行归属按行序去重。"""
    references: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for row in envelope.rows:
        ref = owners[(row.table, row.row_id)]
        if ref not in seen:
            seen.add(ref)
            references.append(ref)
    return ValidatedEvent(
        envelope=envelope,
        event_name=event_name,
        branch_name=branch_name,
        references=tuple(references),
        row_owners=owners,
    )


def _file_checksum_event(event_id: int = 101, change_seq: int | None = None) -> EventEnvelope:
    return _envelope(
        event_id,
        17,
        4,
        (
            RowChange(
                table="device_files",
                row_id=3,
                before=RowImage(
                    exists=True,
                    values={"checksum_support": 1, "sha256": None, "last_error_json": None},
                ),
                after=RowImage(
                    exists=True,
                    values={"checksum_support": 2, "sha256": _SHA, "last_error_json": None},
                ),
            ),
        ),
        change_seq=change_seq,
    )


def _attempt_result_event(event_id: int = 102, change_seq: int | None = None) -> EventEnvelope:
    return _envelope(
        event_id,
        12,
        1,
        (
            RowChange(
                table="operation_attempts",
                row_id=44,
                before=RowImage(
                    exists=True,
                    values={
                        "status": 1,
                        "result_event_id": None,
                        "effect_state": None,
                        "result_json": None,
                        "error_json": None,
                    },
                ),
                after=RowImage(
                    exists=True,
                    values={
                        "status": 2,
                        "result_event_id": event_id,
                        "effect_state": 1,
                        "result_json": {"ok": True},
                        "error_json": None,
                    },
                ),
            ),
        ),
        change_seq=change_seq,
    )


def _attempt_started_event(event_id: int = 101, change_seq: int | None = None) -> EventEnvelope:
    return _envelope(
        event_id,
        11,
        1,
        (
            RowChange(
                table="operation_attempts",
                row_id=44,
                before=RowImage(exists=False, values={}),
                after=RowImage(
                    exists=True,
                    values={"run_id": 31, "attempt_no": 1, "copy_round": 1, "intent_event_id": event_id},
                ),
            ),
            RowChange(
                table="operation_runs",
                row_id=31,
                before=RowImage(
                    exists=True,
                    values={"attempts_used": 0, "max_attempts_used": 3, "retry_wait_required": 0, "status": 2},
                ),
                after=RowImage(
                    exists=True,
                    values={"attempts_used": 1, "max_attempts_used": 3, "retry_wait_required": 0, "status": 1},
                ),
            ),
            RowChange(
                table="device_activities",
                row_id=6,
                before=RowImage(exists=True, values={"dispatch_state": 1}),
                after=RowImage(exists=True, values={"dispatch_state": 2}),
            ),
        ),
        change_seq=change_seq,
    )


def _delivery_prepare_event(event_id: int = 101, change_seq: int | None = 6) -> EventEnvelope:
    return _envelope(
        event_id,
        23,
        2,
        (
            RowChange(
                table="deliveries",
                row_id=5,
                before=RowImage(exists=True, values={"status": 1}),
                after=RowImage(exists=True, values={"status": 2}),
            ),
        ),
        change_seq=change_seq,
    )


def _copy_segment_event(event_id: int = 103, change_seq: int | None = None) -> EventEnvelope:
    return _envelope(
        event_id,
        22,
        2,
        (
            RowChange(
                table="file_copies",
                row_id=12,
                before=RowImage(exists=True, values={"committed_bytes": 100}),
                after=RowImage(exists=True, values={"committed_bytes": 200}),
            ),
        ),
        change_seq=change_seq,
    )


def _intermediate_cleanup_event(event_id: int = 104, change_seq: int | None = None) -> EventEnvelope:
    return _envelope(
        event_id,
        26,
        4,
        (
            RowChange(
                table="intermediate_files",
                row_id=7,
                before=RowImage(exists=True, values={"cleanup_state": 3, "last_error_json": None}),
                after=RowImage(exists=True, values={"cleanup_state": 4, "last_error_json": None}),
            ),
        ),
        change_seq=change_seq,
    )


def _apply_rows(
    rows_by_table: dict[str, dict[int, dict]], events: tuple[ValidatedEvent, ...]
) -> dict[str, dict[int, dict]]:
    working = {table: dict(rows) for table, rows in rows_by_table.items()}
    for validated in events:
        for row in validated.envelope.rows:
            merged = dict(working.get(row.table, {}).get(row.row_id, {}))
            merged.update(row.after.values)
            working.setdefault(row.table, {})[row.row_id] = merged
    return working


class TestRebuildUsesHistoricalRelationship:
    def test_rebuild_uses_historical_relationship(self) -> None:
        # 事件发生时产物 7 和 9 都引用设备文件 3；两者公开摘要都应更新。
        event = _file_checksum_event(change_seq=11)
        validated = _validated(
            event, "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        historical_rows = {
            "device_files": {3: {"checksum_support": 1, "sha256": None}},
            "outputs": {
                7: {"source_action_id": 1, "device_file_id": 3},
                9: {"source_action_id": 2, "device_file_id": 3},
            },
        }
        before = StateSlice(rows=historical_rows, change_counts={})
        after = StateSlice(
            rows=_apply_rows(historical_rows, (validated,)), change_counts={(DEVICE_FILE, 3): 1}
        )
        changes = derive_changes((validated,), before, after)
        changed_entities = [(c.entity_type, c.entity_id) for c in changes.report_changes]
        expected_entities = [(OUTPUT, 7), (OUTPUT, 9)]
        assert changed_entities == expected_entities
        # 产物自身记录未变：不保存恢复关联，也不增加计数。
        assert changes.links == (EntityEventLink(DEVICE_FILE, 3, 101, 1),)
        assert changes.change_counts == {(DEVICE_FILE, 3): 1}
        # 后来产物 7 改指文件 4：当前关系不再覆盖文件 3，但重建本事件
        # 仍使用事件当时的关联，两份产物都保持为应报告对象。
        assert (OUTPUT, 7) in {(c.entity_type, c.entity_id) for c in changes.report_changes}


class TestReportTargetRules:
    def test_file_without_referring_output_has_no_report_target(self) -> None:
        event = _file_checksum_event()
        validated = _validated(
            event, "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        before = StateSlice(
            rows={"device_files": {3: {"checksum_support": 1, "sha256": None}}, "outputs": {}},
            change_counts={},
        )
        after = StateSlice(
            rows=_apply_rows(before.rows, (validated,)), change_counts={(DEVICE_FILE, 3): 1}
        )
        changes = derive_changes((validated,), before, after)
        assert changes.report_changes == ()

    def test_internal_attempt_change_allocates_no_watermark(self) -> None:
        event = _attempt_result_event()
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"run_id": 31, "attempt_no": 1, "status": 1}}},
            change_counts={(ACTION, 8): 4},
        )
        after = StateSlice(
            rows=_apply_rows(before.rows, (validated,)), change_counts={(ACTION, 8): 5}
        )
        changes = derive_changes((validated,), before, after)
        assert changes.report_changes == ()
        assert changes.links == (EntityEventLink(ACTION, 8, 102, 5),)
        assert changes.change_counts == {(ACTION, 8): 5}
        assert changes.progress_updates == (ProgressUpdate(ACTION, 8, 5),)

    def test_public_delivery_change_registers_delivery_and_fills_parents(self) -> None:
        event = _delivery_prepare_event(change_seq=6)
        validated = _validated(
            event, "DELIVERY_CHANGED", "PREPARE", {("deliveries", 5): (DELIVERY, 5)}
        )
        before_rows = {
            "deliveries": {5: {"action_id": 8, "output_id": 3, "status": 1}},
            "actions": {8: {"plan_id": 2, "status": 1}},
            "plans": {2: {"status": 1}},
        }
        before = StateSlice(rows=before_rows, change_counts={(DELIVERY, 5): 1})
        after = StateSlice(
            rows=_apply_rows(before_rows, (validated,)), change_counts={(DELIVERY, 5): 2}
        )
        changes = derive_changes((validated,), before, after)
        changed_entities = [(c.entity_type, c.entity_id) for c in changes.report_changes]
        assert changed_entities == [(DELIVERY, 5)]
        assert changes.report_changes == (ReportEntityChange(DELIVERY, 5, 101, 6),)
        assert changes.links == (EntityEventLink(DELIVERY, 5, 101, 2),)
        # 报告补齐的取回动作与计划不进入目录，也不增加计数。
        parents = fill_in_parents(((DELIVERY, 5),), after.rows)
        assert parents == ((ACTION, 8), (PLAN, 2))
        assert {(DELIVERY, 5), *parents} == {(DELIVERY, 5), (ACTION, 8), (PLAN, 2)}
        assert (ACTION, 8) not in changes.change_counts
        assert changes.progress_updates == (ProgressUpdate(DELIVERY, 5, 2),)

    def test_byte_progress_change_is_not_public_change(self) -> None:
        event = _copy_segment_event()
        validated = _validated(event, "COPY_CHANGED", "SEGMENT", {("file_copies", 12): (DELIVERY, 5)})
        before_rows = {"file_copies": {12: {"target_file_id": 7, "delivery_id": 5, "committed_bytes": 100}}}
        before = StateSlice(rows=before_rows, change_counts={(DELIVERY, 5): 3})
        after = StateSlice(rows=_apply_rows(before_rows, (validated,)), change_counts={(DELIVERY, 5): 4})
        changes = derive_changes((validated,), before, after)
        assert changes.report_changes == ()
        assert changes.links == (EntityEventLink(DELIVERY, 5, 103, 4),)

    def test_internal_file_maintenance_keeps_only_file_history(self) -> None:
        event = _intermediate_cleanup_event()
        validated = _validated(
            event, "INTERMEDIATE_FILE_CHANGED", "CLEANUP_RESULT", {("intermediate_files", 7): (INTERMEDIATE_FILE, 7)}
        )
        before_rows = {
            "intermediate_files": {7: {"cleanup_state": 3}},
            "file_copies": {12: {"target_file_id": 7, "delivery_id": 5}},
        }
        before = StateSlice(rows=before_rows, change_counts={(INTERMEDIATE_FILE, 7): 2, (DELIVERY, 5): 3})
        after = StateSlice(
            rows=_apply_rows(before_rows, (validated,)),
            change_counts={(INTERMEDIATE_FILE, 7): 3, (DELIVERY, 5): 3},
        )
        changes = derive_changes((validated,), before, after)
        assert changes.report_changes == ()
        assert changes.links == (EntityEventLink(INTERMEDIATE_FILE, 7, 104, 3),)
        assert (DELIVERY, 5) not in changes.change_counts


class TestPerEventCounting:
    def test_rows_of_one_event_count_once_per_entity(self) -> None:
        # ATTEMPT_STARTED.NEW 的尝试、流程与活动行都归属动作 8：一次事件只计一次。
        started = _attempt_started_event(101, change_seq=9)
        result = _attempt_result_event(102)
        owners = {
            ("operation_attempts", 44): (ACTION, 8),
            ("operation_runs", 31): (ACTION, 8),
            ("device_activities", 6): (ACTION, 8),
        }
        validated_started = _validated(started, "ATTEMPT_STARTED", "NEW", owners)
        validated_result = _validated(result, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)})
        before_rows = {
            "operation_runs": {31: {"action_id": 8, "attempts_used": 0}},
            "device_activities": {6: {"action_id": 8, "dispatch_state": 1}},
        }
        before = StateSlice(rows=before_rows, change_counts={(ACTION, 8): 4})
        after = StateSlice(
            rows=_apply_rows(before_rows, (validated_started, validated_result)),
            change_counts={(ACTION, 8): 6},
        )
        changes = derive_changes((validated_started, validated_result), before, after)
        assert changes.links == (
            EntityEventLink(ACTION, 8, 101, 5),
            EntityEventLink(ACTION, 8, 102, 6),
        )
        assert changes.change_counts == {(ACTION, 8): 6}
        # 派发状态进入公开投影：首条事件登记动作；结果事件无公开变化。
        assert changes.report_changes == (ReportEntityChange(ACTION, 8, 101, 9),)


class TestChangeSeqConsistency:
    def test_target_without_change_seq_rejected(self) -> None:
        event = _file_checksum_event(change_seq=None)
        validated = _validated(
            event, "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        before = StateSlice(
            rows={
                "device_files": {3: {"checksum_support": 1, "sha256": None}},
                "outputs": {7: {"device_file_id": 3}},
            },
            change_counts={},
        )
        after = StateSlice(rows=_apply_rows(before.rows, (validated,)), change_counts={(DEVICE_FILE, 3): 1})
        with pytest.raises(ChangeDerivationError, match="change_seq"):
            derive_changes((validated,), before, after)

    def test_change_seq_without_target_rejected(self) -> None:
        event = _attempt_result_event(change_seq=7)
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"status": 1}}}, change_counts={(ACTION, 8): 4}
        )
        after = StateSlice(
            rows=_apply_rows(before.rows, (validated,)), change_counts={(ACTION, 8): 5}
        )
        with pytest.raises(ChangeDerivationError, match="change_seq"):
            derive_changes((validated,), before, after)


class TestTransactionInvariants:
    def test_empty_events_rejected(self) -> None:
        before = StateSlice(rows={}, change_counts={})
        with pytest.raises(ChangeDerivationError):
            derive_changes((), before, before)

    def test_mixed_transaction_rejected(self) -> None:
        first = _validated(
            _file_checksum_event(101), "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        second = _validated(
            _file_checksum_event(102, change_seq=None), "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        object.__setattr__(second.envelope, "transaction_id", 8)
        before = StateSlice(rows={"device_files": {3: {}}, "outputs": {}}, change_counts={})
        with pytest.raises(ChangeDerivationError, match="事务"):
            derive_changes((first, second), before, before)

    def test_non_increasing_event_ids_rejected(self) -> None:
        first = _validated(
            _file_checksum_event(102), "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        second = _validated(
            _file_checksum_event(102), "DEVICE_FILE_OBSERVED", "CHECKSUM", {("device_files", 3): (DEVICE_FILE, 3)}
        )
        before = StateSlice(rows={"device_files": {3: {}}, "outputs": {}}, change_counts={})
        with pytest.raises(ChangeDerivationError, match="事件"):
            derive_changes((first, second), before, before)


class TestAfterSliceVerification:
    def test_missing_changed_row_in_after_rejected(self) -> None:
        event = _attempt_result_event()
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"status": 1}}}, change_counts={(ACTION, 8): 4}
        )
        after = StateSlice(rows={}, change_counts={(ACTION, 8): 5})
        with pytest.raises(ChangeDerivationError, match="after"):
            derive_changes((validated,), before, after)

    def test_conflicting_after_value_rejected(self) -> None:
        event = _attempt_result_event()
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"status": 1}}}, change_counts={(ACTION, 8): 4}
        )
        after = StateSlice(
            rows={"operation_attempts": {44: {"status": 3}}}, change_counts={(ACTION, 8): 5}
        )
        with pytest.raises(ChangeDerivationError, match="after"):
            derive_changes((validated,), before, after)

    def test_after_count_drift_rejected(self) -> None:
        event = _attempt_result_event()
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"status": 1}}}, change_counts={(ACTION, 8): 4}
        )
        after = StateSlice(
            rows=_apply_rows(before.rows, (validated,)), change_counts={(ACTION, 8): 9}
        )
        with pytest.raises(ChangeDerivationError, match="计数"):
            derive_changes((validated,), before, after)

    def test_untouched_entity_count_drift_rejected(self) -> None:
        event = _attempt_result_event()
        validated = _validated(
            event, "ATTEMPT_RESULT", "SUCCEED", {("operation_attempts", 44): (ACTION, 8)}
        )
        before = StateSlice(
            rows={"operation_attempts": {44: {"status": 1}}}, change_counts={(ACTION, 8): 4}
        )
        after = StateSlice(
            rows=_apply_rows(before.rows, (validated,)),
            change_counts={(ACTION, 8): 5, (PLAN, 2): 3},
        )
        with pytest.raises(ChangeDerivationError, match="计数"):
            derive_changes((validated,), before, after)


class TestProgressScope:
    def test_progress_only_for_snapshot_eligible_entities(self) -> None:
        # 诊断对象保存可逆历史但不参加快照维护。
        event = _envelope(
            105,
            27,
            1,
            (
                RowChange(
                    table="plan_file_diagnostics",
                    row_id=9,
                    before=RowImage(exists=False, values={}),
                    after=RowImage(
                        exists=True,
                        values={"input_path": "in.bin", "request_id": 42, "plan_id": 2, "errors_json": []},
                    ),
                ),
            ),
            change_seq=3,
        )
        validated = _validated(
            event, "INPUT_DIAGNOSTIC", "CREATE", {("plan_file_diagnostics", 9): (5, 9)}
        )
        before = StateSlice(rows={"plan_file_diagnostics": {}}, change_counts={})
        after = StateSlice(rows=_apply_rows({}, (validated,)), change_counts={(5, 9): 1})
        changes = derive_changes((validated,), before, after)
        assert changes.links == (EntityEventLink(5, 9, 105, 1),)
        assert changes.report_changes == (ReportEntityChange(5, 9, 105, 3),)
        assert changes.progress_updates == ()
