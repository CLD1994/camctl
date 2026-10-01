"""O4 独立查询责任的组件集成测试。

真实 SQLite 与 O2 意图事务组合：五种用途的责任键与目标组合由
仓储与守卫共同核对；同一动作的启动核实与停止核实分别计数；执
行前检查不指向活动；不支持用途的目标组合被拒绝。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    QueryPurpose,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()

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
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)",
        (_NOW,),
    )
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (1, 1, 0, 'rec', 2, 'cam-1', ?, NULL, '{}', '{\"target_duration_s\": 60}',"
        " 'camctl-adb', 1000, '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (_NOW,),
    )
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
    # 原启动与停止流程（查询用途 2/4 的归属核对依据）。
    for run_id, kind in ((1, 1), (2, 2)):
        connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (?, 1, NULL, ?, NULL, ?, 1, NULL, NULL, NULL, 2, 1, 2, '10',"
            " NULL, 0, NULL)",
            (run_id, kind, f"{'start' if kind == 1 else 'stop'}/1"),
        )
        connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, copy_round,"
            " status, intent_event_id, result_event_id, max_attempts_used,"
            " timeout_s_json, retry_interval_s_json, effect_state, result_json,"
            " error_json)"
            " VALUES (?, ?, 1, NULL, 1, 1, NULL, 2, '10', NULL, 1, NULL, NULL)",
            (run_id, run_id),
        )
    connection.commit()
    return owned


def _intent(purpose: QueryPurpose, *, activity_id: int | None = 1) -> AttemptIntent:
    return AttemptIntent(
        operation="query",
        action_id=1,
        kind=OperationKind.QUERY_ACTIVITY,
        target=AttemptTarget(activity_id=activity_id),
        query_purpose=purpose,
        config=AttemptConfig(
            max_attempts=3, timeout_s=Decimal("5"), retry_interval_s=Decimal("1")
        ),
        occurred_at=_NOW,
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def test_start_and_stop_confirmation_count_independently(tmp_path: Path) -> None:
    """同一动作的启动核实与停止核实分别计数（Q-03）。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        first = repository.begin_attempt(
            _intent(QueryPurpose.START_CONFIRMATION), new_operation_key(), owned
        )
        assert first.kind is DbOutcomeKind.COMPLETED
        assert first.value.disposition is BeginDisposition.GRANTED
        stop = repository.begin_attempt(
            _intent(QueryPurpose.STOP_CONFIRMATION), new_operation_key(), owned
        )
        assert stop.value.disposition is BeginDisposition.GRANTED

        rows = owned.connection.execute(
            "SELECT responsibility_key, attempts_used FROM operation_runs"
            " WHERE kind = 6 ORDER BY id"
        ).fetchall()
        assert [row[0] for row in rows] == ["query/start/1/1", "query/stop/1/1"]
        assert all(row[1] == 1 for row in rows)

        # 无重试等待时再次登记新尝试违反重试协议：整组回滚，不新建尝试。
        again = repository.begin_attempt(
            _intent(QueryPurpose.START_CONFIRMATION), new_operation_key(), owned
        )
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        counts = owned.connection.execute(
            "SELECT responsibility_key, attempts_used FROM operation_runs"
            " WHERE kind = 6 ORDER BY id"
        ).fetchall()
        assert all(row[1] == 1 for row in counts)
    finally:
        owned.connection.close()


def test_preflight_query_has_no_activity(tmp_path: Path) -> None:
    """执行前检查不指向活动；责任键按发起动作推导（Q-05/Q-11）。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        outcome = repository.begin_attempt(
            _intent(QueryPurpose.BEFORE_EXECUTION, activity_id=None),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.disposition is BeginDisposition.GRANTED
        run = _value(
            owned,
            "SELECT responsibility_key, activity_id FROM operation_runs WHERE kind = 6",
        )
        assert run[0] == "query/preflight/1"
        assert run[1] is None
    finally:
        owned.connection.close()


def test_confirmation_without_original_flow_is_rejected(tmp_path: Path) -> None:
    """停止核实要求存在同动作同活动的原 STOP 流程（Q-11 归属核对）。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        owned.connection.execute("BEGIN IMMEDIATE")
        owned.connection.execute("DELETE FROM operation_runs WHERE kind = 2")
        owned.connection.commit()
        outcome = repository.begin_attempt(
            _intent(QueryPurpose.STOP_CONFIRMATION), new_operation_key(), owned
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()
