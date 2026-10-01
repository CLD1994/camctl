"""C3 录像完成终态与正式产物登记的组件集成测试。

真实 SQLite 与 P3 事务内核组合：动作成功终态、正式产物登记与父
计划状态同事务保存，任一写入失败整组回滚；停止预算跨普通、取消
及恢复入口共用原责任；已终态动作不被改写。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCapture,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    OperationKind,
)
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000


def _environment(tmp_path: Path):
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
    _seed_action(connection, 1, 1)
    _seed_device_file(connection, 11)
    _seed_device_file(connection, 12, role=3)
    _seed_stop_flow(connection)
    _seed_activity(connection)
    connection.commit()
    return owned


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, 4242, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, _NOW),
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


def _seed_device_file(connection, file_id: int, *, role: int = 2) -> None:
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, 1, ?, '{}', '{}', 'video.mp4', 'video/mp4', ?, NULL, NULL,"
        " 2, 3, '{}', 1024, 3, NULL, NULL, 1, 1, 1)",
        (file_id, f"file-{file_id:04d}", role),
    )


def _seed_activity(connection) -> None:
    connection.execute(
        "INSERT INTO device_activities (id, action_id, task_key, task_locator_json,"
        " state_query_supported, stop_supported, safe_repeat_stop,"
        " start_return_meaning, completion_mode, ownership_mode, output_scope_json,"
        " baseline_state, baseline_first_event_id, baseline_last_event_id,"
        " dispatch_state, activity_state, occupancy_state, sent_at, started_at,"
        " result_wait_margin_ms, extra_wait_ms_used, expected_check_at,"
        " wait_completed_event_id, capture_json, control_elapsed_ns,"
        " completion_basis, completion_evidence_json, result_set_state,"
        " last_error_json)"
        " VALUES (1, 1, ?, NULL, 1, 1, 1, 1, 1, 1, '{}', 1, NULL, NULL, 2, 2, 1,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL)",
        (f"{1:032x}",),
    )


def _seed_stop_flow(connection) -> None:
    """预建空停止流程（责任键 stop/1，未尝试），供预算共用验证。"""
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (30, 1, NULL, 2, NULL, 'stop/1', 1, NULL, NULL, NULL, 1, 0, 3,"
        " '10', '1', 0, NULL)"
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _original_draft(file_id: int, *, complete: bool = True) -> OutputDraft:
    return OutputDraft(
        kind=OutputKind.ORIGINAL,
        file=FileReference(device_file_id=file_id),
        file_complete=complete,
        sha256=None,
    )


def _finish(action_id: int = 1, drafts=None) -> FinishCapture:
    return FinishCapture(
        action_id=action_id,
        drafts=drafts if drafts is not None else (_original_draft(11),),
        catalog_facts=OutputCatalogFacts(action_id=action_id, ownership_confirmed=True),
        occurred_at=_NOW,
    )


pytestmark = pytest.mark.asyncio


async def test_finish_registers_outputs_action_and_plan_together(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    repository = CaptureRepository()
    try:
        outcome = repository.finish_capture(_finish(), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED
        result = outcome.value
        assert result.action_status == 3
        assert result.plan_status == 3

        output = _value(
            owned,
            "SELECT kind, device_file_id, availability, cleanup_status, source_action_id"
            " FROM outputs WHERE id = ?",
            result.output_ids[0],
        )
        assert output[0] == 1  # ORIGINAL
        assert output[1] == 11  # 文件身份保留
        assert output[2] == 1 and output[3] == 1
        assert output[4] == 1
        action = _value(owned, "SELECT status FROM actions WHERE id = 1")
        assert action[0] == 3
        plan = _value(owned, "SELECT status FROM plans WHERE id = 1")
        assert plan[0] == 3
    finally:
        owned.connection.close()


async def test_invalid_draft_rolls_back_whole_group(tmp_path: Path) -> None:
    """未完成文件混入草稿：产物与终态整组回滚。"""
    owned = _environment(tmp_path)
    repository = CaptureRepository()
    try:
        outcome = repository.finish_capture(
            _finish(drafts=(_original_draft(11), _original_draft(12, complete=False))),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM outputs")[0] == 0
        action = _value(owned, "SELECT status FROM actions WHERE id = 1")
        assert action[0] == 2
        plan = _value(owned, "SELECT status FROM plans WHERE id = 1")
        assert plan[0] == 1
    finally:
        owned.connection.close()


async def test_stop_uses_original_budget_across_cancel_and_restart(tmp_path: Path) -> None:
    """停止预算沿原流程累计：普通、取消与恢复入口共用同一责任。"""
    owned = _environment(tmp_path)
    operations = OperationRepository()
    try:
        used = 0
        for _entry in ("normal", "cancel", "restart"):
            outcome = operations.begin_attempt(_stop_intent(), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED
            ticket = outcome.value.ticket
            used += 1
            failed = _stop_failed(ticket)
            receipt = operations.finish_attempt(
                AttemptFinish(
                    ticket=ticket, outcome=failed, occurred_at=_NOW, retry_wait=True
                ),
                new_operation_key(),
                owned,
            )
            assert receipt.kind is DbOutcomeKind.COMPLETED
        row = _value(
            owned,
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key = 'stop/1'",
        )
        assert row[0] == 3
        assert used == 3
    finally:
        owned.connection.close()


async def test_terminal_action_is_not_rewritten(tmp_path: Path) -> None:
    """已成功的动作再次登记完成：整组拒绝，原终态保持。"""
    owned = _environment(tmp_path)
    repository = CaptureRepository()
    try:
        first = repository.finish_capture(_finish(), new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED
        again = repository.finish_capture(_finish(), new_operation_key(), owned)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        action = _value(owned, "SELECT status FROM actions WHERE id = 1")
        assert action[0] == 3
        outputs = _value(owned, "SELECT COUNT(*) FROM outputs")
        assert outputs[0] == 1
    finally:
        owned.connection.close()


def _stop_intent() -> AttemptIntent:
    return AttemptIntent(
        operation="stop",
        action_id=1,
        kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=1),
        query_purpose=None,
        config=AttemptConfig(
            max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("1")
        ),
        occurred_at=_NOW,
    )


def _stop_failed(ticket):
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.operations.validation import validate_outcome

    registry = EvidenceRegistry(
        (
            EvidenceContract(
                type="adb_foreground_assumption",
                version=1,
                operation="stop",
                fields=frozenset(),
            ),
        )
    )
    return validate_outcome(
        ticket,
        CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue(code="transport_timeout", stage="transport"),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                basis=SettlementBasis.ASSUMED,
                evidence=EvidenceValue(
                    type="adb_foreground_assumption", version=1, data={}
                ),
            ),
            observations=(),
        ),
        registry,
    )
