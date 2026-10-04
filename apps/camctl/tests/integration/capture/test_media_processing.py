"""C8 录像内部处理决定与结果事务的组件集成测试。

真实 SQLite 与 P3 事务内核组合：RECORDING_DECIDED 固定检查与修
复决定及可靠原片关联，RECORDING_PROCESSED 保存检查、修复与取消
收场阶段；媒体观察保存公共结构并保持全精度，原键恢复首次响应，
非法前提与转换整组回滚。处理状态不携带采集成功结论。
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.processing import (
    CheckBasis,
    CheckDecisionChoice,
    CheckDecisionSave,
    CheckPhase,
    CheckReason,
    CheckResultSave,
    DiscardPhase,
    DiscardProgressSave,
    MediaObservation,
    ProcessingDisposition,
    ProcessingError,
    RepairBasis,
    RepairDecisionChoice,
    RepairDecisionSave,
    RepairOutcome,
    RepairReason,
    RepairResultSave,
    SourceFileSave,
    repair_basis_from_check,
)
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.changes import event_report_targets
from camctl.history.validators import (
    EventContext, EventValidationError, validate_event,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, register_capture_guards,
)
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionScope

from ..persistence.test_runtime import _create_valid_database

register_capture_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000

_DECIDED_EVENT = 18
_PROCESSED_EVENT = 19


@pytest.fixture
def environment(tmp_path: Path):
    """camera_record 动作与初始处理行；另备各类原片核对反例。"""
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    _seed_plan(connection, 1)
    _seed_plan(connection, 2)
    _seed_action(connection, 1, 1)
    _seed_action(connection, 2, 2)
    _seed_device_file(connection, 11, role=2, completion=3)
    _seed_device_file(connection, 12, role=3, completion=3)
    _seed_device_file(connection, 13, role=2, completion=2)
    _seed_device_file(connection, 14, role=2, completion=3, source=2)
    _seed_processing(connection, 1, 1)
    _seed_processing(connection, 2, 2)
    _seed_intermediate(
        connection, 700, action=1, purpose=4, path="derived/700.mp4",
        size=2048, sha256="a" * 64)
    _seed_intermediate(
        connection, 701, action=1, purpose=2, path="recording-inputs/701.mp4",
        size=1024, sha256="b" * 64)
    connection.commit()
    return owned


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, 4241 + plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 0, 'rec', 2, 'cam-1', ?, NULL, '{}',"
        " '{\"target_duration_s\": 60}', 'camctl-adb', 1000, '{}', 2, 1, 0, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, _NOW),
    )


def _seed_device_file(
    connection, file_id: int, *, role: int, completion: int, source: int = 1,
) -> None:
    # 未确认写完的文件不带可靠大小（表约束），核对反例保持该事实形态。
    size = 1024 if completion == 3 else None
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, '{}', '{}', 'video.mp4', 'video/mp4', ?, NULL, NULL,"
        " 2, ?, '{}', ?, 3, NULL, NULL, 1, 1, 1)",
        (file_id, source, f"file-{file_id:04d}", role, completion, size),
    )


def _seed_processing(connection, processing_id: int, action_id: int) -> None:
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (?, ?, NULL, 1, 1, NULL, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (processing_id, action_id),
    )


def _seed_intermediate(
    connection, file_id: int, *, action: int, purpose: int, path: str,
    size: int | None, sha256: str | None,
) -> None:
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id,"
        " purpose, relative_path, retention_state, cleanup_state, size_bytes,"
        " sha256, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, NULL, ?, ?, 1, 1, ?, ?, 1, 1, 1)",
        (file_id, action, purpose, path, size, sha256),
    )


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _total_events(owned) -> int:
    return _row(owned, "SELECT COUNT(*) FROM history_events")[0]


def _processing_column(owned, column: str, processing_id: int = 1):
    return _row(
        owned,
        f"SELECT {column} FROM recording_processing WHERE id=?",
        processing_id)[0]


def _json_column(owned, column: str, processing_id: int = 1):
    raw = _processing_column(owned, column, processing_id)
    return None if raw is None else parse_exact_json(raw)


def _repository() -> CaptureRepository:
    return CaptureRepository()


def _check_required(occurred_at: int = _NOW + 1) -> CheckDecisionSave:
    return CheckDecisionSave(
        1, CheckDecisionChoice.REQUIRED,
        CheckBasis(
            reason=CheckReason.INSUFFICIENT_TIMING,
            target_duration_ms=60_000),
        occurred_at)


def _check_not_needed(occurred_at: int = _NOW + 1) -> CheckDecisionSave:
    return CheckDecisionSave(
        1, CheckDecisionChoice.NOT_NEEDED,
        CheckBasis(
            reason=CheckReason.EXCESS_DURATION_CHECK,
            target_duration_ms=60_000,
            control_elapsed_ns=70_500_000_000),
        occurred_at)


def _repair_pending(occurred_at: int = _NOW + 5) -> RepairDecisionSave:
    return RepairDecisionSave(
        1, RepairDecisionChoice.PENDING,
        RepairBasis(
            reason=RepairReason.THRESHOLD_REACHED,
            target_duration_ms=60_000,
            threshold_s=Decimal("70"),
            actual_duration_s=Decimal("75.125")),
        occurred_at)


def _completed_observation(seconds: str = "75.125") -> MediaObservation:
    return MediaObservation(
        CheckPhase.COMPLETED, duration_s=Decimal(seconds))


def _error(code: str = "tool_failed") -> ProcessingError:
    return ProcessingError(code=code, stage="media", details={"exit": 1})


def _save(owned, command, *, key=None):
    repository = _repository()
    method = {
        CheckDecisionSave: repository.save_check_decision,
        RepairDecisionSave: repository.save_repair_decision,
        SourceFileSave: repository.save_source_file,
        CheckResultSave: repository.save_check_result,
        RepairResultSave: repository.save_repair_result,
        DiscardProgressSave: repository.save_discard_progress,
    }[type(command)]
    return method(command, key or new_operation_key(), owned)


def _assert_saved(outcome) -> None:
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ProcessingDisposition.SAVED


def _assert_rolled_back(outcome) -> None:
    """业务前提或状态转换不成立时整组回滚，保留实际错误。"""
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert outcome.error is not None


def _assert_consistency_rejected(outcome) -> None:
    """命令层业务前提失败按一致性错误拒绝。"""
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)


# ---- 正常链 ----


def test_full_processing_chain_saves_each_stage(environment) -> None:
    """检查与修复各阶段按序保存：事件、投影与全精度依据共同提交。"""
    owned = environment
    _assert_saved(_save(owned, SourceFileSave(1, 11, _NOW + 1)))
    _assert_saved(_save(owned, _check_required(_NOW + 2)))
    _assert_saved(_save(
        owned,
        CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 3)))
    _assert_saved(_save(owned, _repair_pending(_NOW + 4)))
    _assert_saved(_save(
        owned,
        RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 5)))
    _assert_saved(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 6,
            output_file_id=700)))

    row = _row(
        owned,
        "SELECT source_device_file_id, check_state, check_decision,"
        " repair_state, repair_output_file_id, discard_state"
        " FROM recording_processing WHERE id=1")
    assert row == (11, 3, 3, 5, 700, 1)
    assert _events(owned, _DECIDED_EVENT, 1) == 1
    assert _events(owned, _DECIDED_EVENT, 2) == 1
    assert _events(owned, _DECIDED_EVENT, 3) == 1
    assert _events(owned, _PROCESSED_EVENT, 1) == 1
    assert _events(owned, _PROCESSED_EVENT, 2) == 2
    assert _json_column(owned, "media_json") == {
        "check_status": "completed",
        "duration": {"status": "available", "seconds": Decimal("75.125")},
    }
    assert _json_column(owned, "repair_basis_json") == {
        "reason": 2,
        "target_duration_ms": 60_000,
        "threshold_s": Decimal("70"),
        "actual_duration_s": Decimal("75.125"),
    }
    assert _json_column(owned, "repair_error_json") is None


def test_not_needed_check_decision_saves_basis(environment) -> None:
    owned = environment
    _assert_saved(_save(owned, _check_not_needed()))
    assert _processing_column(owned, "check_decision") == 2
    assert _json_column(owned, "check_basis_json") == {
        "reason": 3,
        "target_duration_ms": 60_000,
        "control_elapsed_ns": 70_500_000_000,
    }


def test_check_running_then_completed(environment) -> None:
    """检查执行先保存进行中阶段，中断后可继续保存最终结果。"""
    owned = environment
    _assert_saved(_save(owned, _check_required()))
    _assert_saved(_save(
        owned,
        CheckResultSave(
            1, MediaObservation(CheckPhase.RUNNING), occurred_at=_NOW + 2)))
    assert _processing_column(owned, "check_state") == 2
    assert _json_column(owned, "media_json") == {
        "check_status": "running",
        "duration": {"status": "unknown"},
    }
    _assert_saved(_save(
        owned,
        CheckResultSave(1, _completed_observation("65"), occurred_at=_NOW + 3)))
    assert _processing_column(owned, "check_state") == 3


def test_check_failed_saves_error_and_stays_terminal(environment) -> None:
    owned = environment
    _assert_saved(_save(owned, _check_required()))
    _assert_saved(_save(
        owned,
        CheckResultSave(
            1, MediaObservation(CheckPhase.FAILED, error=_error()),
            occurred_at=_NOW + 2)))
    assert _json_column(owned, "media_json") == {
        "check_status": "failed",
        "duration": {"status": "unknown"},
        "error": {"code": "tool_failed", "stage": "media", "details": {"exit": 1}},
    }
    _assert_consistency_rejected(_save(
        owned,
        CheckResultSave(
            1, MediaObservation(CheckPhase.UNCONFIRMED, error=_error()),
            occurred_at=_NOW + 3)))


def test_check_decision_not_required_blocks_result(environment) -> None:
    """未固定需要检查的决定时，检查结果不能保存。"""
    owned = environment
    _assert_consistency_rejected(_save(
        owned,
        CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 1)))
    _assert_saved(_save(owned, _check_not_needed()))
    _assert_consistency_rejected(_save(
        owned,
        CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 2)))


def test_completed_check_is_terminal(environment) -> None:
    owned = environment
    _assert_saved(_save(owned, _check_required()))
    _assert_saved(_save(
        owned,
        CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 2)))
    _assert_consistency_rejected(_save(
        owned,
        CheckResultSave(
            1, MediaObservation(CheckPhase.RUNNING), occurred_at=_NOW + 3)))


def test_decisions_are_fixed_once(environment) -> None:
    """已固定的决定不因重送或配置变化重算。"""
    owned = environment
    _assert_saved(_save(owned, _check_required(_NOW + 1)))
    _assert_consistency_rejected(_save(owned, _check_not_needed(_NOW + 2)))
    _assert_saved(_save(owned, _repair_pending(_NOW + 3)))
    below = RepairDecisionSave(
        1, RepairDecisionChoice.NOT_NEEDED,
        RepairBasis(
            reason=RepairReason.BELOW_THRESHOLD,
            target_duration_ms=60_000, threshold_s=Decimal("70")),
        _NOW + 4)
    _assert_consistency_rejected(_save(owned, below))


def test_repair_result_requires_pending_or_running(environment) -> None:
    owned = environment
    _assert_consistency_rejected(_save(
        owned,
        RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 1)))
    _assert_saved(_save(owned, _repair_pending(_NOW + 2)))
    _assert_rolled_back(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 3,
            output_file_id=700)))
    _assert_saved(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.FAILED, occurred_at=_NOW + 4,
            error=_error("repair_failed"))))
    assert _json_column(owned, "repair_error_json") == {
        "code": "repair_failed", "stage": "media", "details": {"exit": 1}}


def test_repair_success_requires_repair_output_purpose(environment) -> None:
    owned = environment
    _assert_saved(_save(owned, _repair_pending()))
    _assert_saved(_save(
        owned,
        RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 2)))
    _assert_consistency_rejected(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 3,
            output_file_id=701)))
    _assert_consistency_rejected(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 4,
            output_file_id=999)))


def test_repair_success_output_must_be_complete(environment) -> None:
    """修复输出缺少完整字节事实时不能保存成功。"""
    owned = environment
    owned.connection.execute(
        "UPDATE intermediate_files SET sha256=NULL, size_bytes=NULL WHERE id=700")
    owned.connection.commit()
    _assert_saved(_save(owned, _repair_pending()))
    _assert_saved(_save(
        owned,
        RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 2)))
    _assert_consistency_rejected(_save(
        owned,
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 3,
            output_file_id=700)))


def test_repair_decision_not_needed_for_other_actions(environment) -> None:
    owned = environment
    command = RepairDecisionSave(
        2, RepairDecisionChoice.NOT_NEEDED,
        RepairBasis(
            reason=RepairReason.NO_USABLE_INPUT, target_duration_ms=60_000),
        _NOW + 1)
    _assert_saved(_save(owned, command))
    assert _processing_column(owned, "repair_state", 2) == 2


def test_source_file_requires_original_complete_own_file(environment) -> None:
    """可靠原片必须属于本次动作、角色为原片且已写完。"""
    owned = environment
    for file_id in (12, 13, 14):
        _assert_consistency_rejected(_save(
            owned, SourceFileSave(1, file_id, _NOW + 1)))
    _assert_saved(_save(owned, SourceFileSave(1, 11, _NOW + 2)))
    _assert_consistency_rejected(_save(owned, SourceFileSave(1, 11, _NOW + 3)))


def test_discard_progress_chain(environment) -> None:
    owned = environment
    _assert_saved(_save(
        owned, DiscardProgressSave(1, DiscardPhase.PENDING, occurred_at=_NOW + 1)))
    _assert_saved(_save(
        owned, DiscardProgressSave(1, DiscardPhase.RUNNING, occurred_at=_NOW + 2)))
    _assert_saved(_save(
        owned, DiscardProgressSave(1, DiscardPhase.COMPLETED, occurred_at=_NOW + 3)))
    assert _processing_column(owned, "discard_state") == 4
    assert _events(owned, _PROCESSED_EVENT, 3) == 3


def test_discard_invalid_transition_rejected(environment) -> None:
    owned = environment
    _assert_consistency_rejected(_save(
        owned, DiscardProgressSave(1, DiscardPhase.RUNNING, occurred_at=_NOW + 1)))
    _assert_saved(_save(
        owned, DiscardProgressSave(1, DiscardPhase.PENDING, occurred_at=_NOW + 2)))
    _assert_saved(_save(
        owned,
        DiscardProgressSave(
            1, DiscardPhase.UNKNOWN, occurred_at=_NOW + 3,
            error=_error("discard_unknown"))))
    assert _json_column(owned, "discard_error_json") == {
        "code": "discard_unknown", "stage": "media", "details": {"exit": 1}}


def test_unknown_processing_row_rejected(environment) -> None:
    owned = environment
    outcome = _repository().save_check_decision(
        CheckDecisionSave(
            99, CheckDecisionChoice.REQUIRED,
            CheckBasis(
                reason=CheckReason.INSUFFICIENT_TIMING,
                target_duration_ms=60_000),
            _NOW + 1),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


# ---- 原键恢复 ----


def test_original_key_replays_first_response(environment) -> None:
    """提交未知后的原键重送恢复首次响应，不产生第二次事件。"""
    owned = environment
    key = new_operation_key()
    first = _save(owned, _check_required(), key=key)
    _assert_saved(first)
    events_before = _total_events(owned)
    replay = _save(owned, _check_required(), key=key)
    assert replay.kind is DbOutcomeKind.COMPLETED
    assert replay.value.disposition is ProcessingDisposition.ALREADY
    assert _total_events(owned) == events_before


def test_original_key_with_other_facts_rejected(environment) -> None:
    """原键承载不同事实时按操作身份冲突拒绝。"""
    owned = environment
    key = new_operation_key()
    _assert_saved(_save(owned, _check_required(_NOW + 1), key=key))
    outcome = _save(owned, _check_required(_NOW + 9), key=key)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


def test_reuse_accepts_actual_progress(environment) -> None:
    """原键恢复不阻止已保存决定之外的事务继续推进。"""
    owned = environment
    key = new_operation_key()
    _assert_saved(_save(owned, _check_required(_NOW + 1), key=key))
    _assert_saved(_save(owned, _repair_pending(_NOW + 2)))
    replay = _save(owned, _check_required(_NOW + 1), key=key)
    assert replay.kind is DbOutcomeKind.COMPLETED
    assert replay.value.disposition is ProcessingDisposition.ALREADY


# ---- 正式守卫 ----


def _proposal(owned, command):
    owned.connection.execute("BEGIN")
    try:
        plan = command.plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    assert plan.events, "提案必须产生事件"
    return plan


def _validate(plan, events=None, state_rows=None):
    """模仿内核按顺序分配报告变化序号并逐事件校验。"""
    working = {
        table: {row_id: dict(rows) for row_id, rows in table_rows.items()}
        for table, table_rows in plan.state_rows.items()
    }
    chosen = events if events is not None else plan.events
    seq = 0
    for event in chosen:
        if event_report_targets(event, working):
            seq += 1
            event = replace(event, change_seq=seq)
        context = EventContext(
            TransactionRange(
                chosen[0].transaction_id, chosen[0].event_id, chosen[-1].event_id),
            dict(plan.owners),
            state_rows if state_rows is not None else plan.state_rows,
            read_coverage=plan.read_coverage)
        validate_event(event, context)
        for row in event.rows:
            if row.after.exists:
                working.setdefault(row.table, {}).setdefault(
                    row.row_id, {}).update(row.after.values)


def test_guard_accepts_real_processing_events(environment) -> None:
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    key = new_operation_key()
    for request, command_type in (
        (SourceFileSave(1, 11, _NOW + 1),
         capture_module._SourceFileCommand),
        (_check_required(_NOW + 2),
         capture_module._CheckDecisionCommand),
        (CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 3),
         capture_module._CheckResultCommand),
        (_repair_pending(_NOW + 4),
         capture_module._RepairDecisionCommand),
        (RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 5),
         capture_module._RepairResultCommand),
        (RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 6,
            output_file_id=700),
         capture_module._RepairResultCommand),
        (DiscardProgressSave(1, DiscardPhase.PENDING, occurred_at=_NOW + 7),
         capture_module._DiscardProgressCommand),
    ):
        plan = _proposal(owned, command_type(request, key))
        _validate(plan)
        _save(owned, request)


def test_guard_rejects_media_state_mismatch(environment) -> None:
    """守卫拒绝与保存阶段不一致的媒体结构。"""
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    _save(owned, _check_required(_NOW + 1))
    command = capture_module._CheckResultCommand(
        CheckResultSave(1, _completed_observation(), occurred_at=_NOW + 2),
        new_operation_key())
    plan = _proposal(owned, command)
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "media_json": {
                "check_status": "completed",
                "duration": {"status": "unknown"},
            }}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_failed_check_without_error(environment) -> None:
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    _save(owned, _check_required(_NOW + 1))
    command = capture_module._CheckResultCommand(
        CheckResultSave(
            1, MediaObservation(CheckPhase.FAILED, error=_error()),
            occurred_at=_NOW + 2),
        new_operation_key())
    plan = _proposal(owned, command)
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "media_json": {
                "check_status": "failed",
                "duration": {"status": "unknown"},
            }}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_check_basis_without_reason(environment) -> None:
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    command = capture_module._CheckDecisionCommand(
        _check_required(_NOW + 1), new_operation_key())
    plan = _proposal(owned, command)
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "check_basis_json": {"target_duration_ms": 60_000}}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_repair_basis_missing_threshold(environment) -> None:
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    command = capture_module._RepairDecisionCommand(
        _repair_pending(_NOW + 1), new_operation_key())
    plan = _proposal(owned, command)
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "repair_basis_json": {
                "reason": 2, "target_duration_ms": 60_000,
                "actual_duration_s": Decimal("75.125")}}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_success_without_registered_output(environment) -> None:
    from camctl.persistence.repositories import capture as capture_module

    owned = environment
    _save(owned, _repair_pending(_NOW + 1))
    _save(owned, RepairResultSave(1, RepairOutcome.RUNNING, occurred_at=_NOW + 2))
    command = capture_module._RepairResultCommand(
        RepairResultSave(
            1, RepairOutcome.SUCCEEDED, occurred_at=_NOW + 3,
            output_file_id=700),
        new_operation_key())
    plan = _proposal(owned, command)
    tampered = {
        table: {row_id: dict(rows) for row_id, rows in rows.items()}
        for table, rows in plan.state_rows.items()
    }
    del tampered["intermediate_files"][700]
    with pytest.raises(EventValidationError):
        _validate(plan, state_rows=tampered)
