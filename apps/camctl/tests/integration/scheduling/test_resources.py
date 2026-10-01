"""Q4 原子授予、启动保留与派发再检查的组件集成测试。

真实 SQLite 与 P3 事务内核组合：首次启动机会与尝试意图、次数、
参数及设备活动身份在同一事务共同保存；授予提交后派发前再次核对
窗口与取消，不派发不退次数；同设备候选竞争按统一排序只有一个
获准。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptTarget,
    BeginDisposition,
    FinishDisposition,
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
from camctl.operations.validation import validate_outcome
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import (
    GrantOutcome,
    GrantRequest,
    SchedulingRepository,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.scheduling.resources import (
    ConsistencyError,
    current_start_holder,
    recheck_dispatch,
)
from camctl.scheduling.rules import LaunchWindow

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()

_NOW = 1_750_000_000_000_000

_CONTRACTS = EvidenceRegistry(
    (
        EvidenceContract(
            type="start_sent",
            version=1,
            operation="control",
            fields=frozenset({"activity_id"}),
            identity_field="activity_id",
        ),
        EvidenceContract(
            type="dispatch_prevented",
            version=1,
            operation="control",
            fields=frozenset(),
        ),
    )
)

_WINDOW = LaunchWindow(scheduled_at=_NOW - 1_000_000, window_end=_NOW + 1_000_000)


class DispatchSpy:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, ticket) -> None:
        self.calls.append(ticket)


def _seed_environment(tmp_path: Path):
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
    connection.commit()
    return owned


def _seed_plan(connection: sqlite3.Connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, 1, 1, 1, 1)",
        (plan_id, 1000 + plan_id, f"plan-{plan_id}", _NOW),
    )


def _seed_record_action(
    connection: sqlite3.Connection,
    action_id: int,
    plan_id: int,
    *,
    input_index: int = 0,
    status: int = 2,
    scheduled_at: int | None = _NOW,
    cancel_requested: int = 0,
) -> None:
    started = 0 if status == 1 else 1
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, 2, 'cam-1', ?, NULL, '{}', '{\"duration_s\": 60}',"
        " 'camctl-adb', 1000, '{}', ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (action_id, plan_id, input_index, f"rec-{action_id}", scheduled_at, status,
         started, cancel_requested),
    )


def _seed_activity(
    connection: sqlite3.Connection,
    action_id: int,
    *,
    dispatch_state: int = 1,
) -> None:
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
        " VALUES (?, ?, ?, NULL, 1, 1, 1, 1, 1, 1, '{}', 1, NULL, NULL, ?, 1, 1,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL)",
        (action_id, action_id, f"{action_id:032x}", dispatch_state),
    )


def _grant_request(action_id: int, **overrides) -> GrantRequest:
    values = dict(
        device_id="cam-1",
        action_id=action_id,
        window=_WINDOW,
        trusted_wall_now=_NOW,
        config=AttemptConfig(max_attempts=2, timeout_s=Decimal("10")),
        occurred_at=_NOW,
    )
    values.update(overrides)
    return GrantRequest(**values)


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def test_grant_does_not_skip_dispatch_recheck(tmp_path: Path) -> None:
    """授予提交后窗口耗尽或取消生效：不派发，次数不退还。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    operations = OperationRepository()
    dispatches = DispatchSpy()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1)
        _seed_activity(connection, 1)
        connection.commit()

        granted = repository.grant_start(
            _grant_request(1), new_operation_key(), owned
        )
        assert granted.kind is DbOutcomeKind.COMPLETED
        assert granted.value.outcome is GrantOutcome.GRANTED
        ticket = granted.value.ticket
        assert ticket is not None
        assert granted.value.activity_id == 1

        # 授予提交后、派发前：窗口已经耗尽，再检查拒绝派发。
        late = recheck_dispatch(
            window=_WINDOW, trusted_wall_now=_WINDOW.window_end + 1, canceled=False
        )
        assert late.allowed is False
        assert late.reason == "window_ended"
        assert dispatches.calls == []

        # 可靠未派发结果：FAILED + NO_EFFECT + dispatch_prevented。
        prevented = validate_outcome(
            ticket,
            CallOutcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="window_ended", stage="dispatch"),
                effect=EffectState.NO_EFFECT,
                settlement=Settlement(
                    basis=SettlementBasis.NOT_DISPATCHED,
                    evidence=EvidenceValue(type="dispatch_prevented", version=1, data={}),
                ),
                observations=(),
            ),
            _CONTRACTS,
        )
        finish = operations.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=prevented, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert finish.kind is DbOutcomeKind.COMPLETED
        assert finish.value.disposition is FinishDisposition.SAVED

        # 已提交次数保留，不因未派发退还。
        used = _value(
            owned,
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key = ?",
            "start/1",
        )
        assert used[0] == 1

        # 取消生效同样拒绝派发。
        canceled = recheck_dispatch(
            window=_WINDOW, trusted_wall_now=_NOW, canceled=True
        )
        assert canceled.allowed is False
        assert canceled.reason == "canceled"

        # 窗口内未取消的再检查允许派发。
        ok = recheck_dispatch(window=_WINDOW, trusted_wall_now=_NOW, canceled=False)
        assert ok.allowed is True and ok.reason is None
    finally:
        owned.connection.close()


def test_grant_saves_activity_intent_and_params_together(tmp_path: Path) -> None:
    """首次机会记录同时含活动身份、尝试意图与参数；缺活动整组拒绝。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1)
        _seed_activity(connection, 1)
        connection.commit()

        outcome = repository.grant_start(
            _grant_request(1), new_operation_key(), owned
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        ticket = outcome.value.ticket
        assert ticket is not None

        event = _value(
            owned,
            "SELECT event_type, body_json FROM history_events WHERE id = ?",
            _value(
                owned,
                "SELECT intent_event_id FROM operation_attempts WHERE run_id = ?",
                ticket.run_id,
            )[0],
        )
        assert event[0] == 11
        tables = {
            row["table"] for row in json.loads(event[1])["rows"]
        }
        assert tables == {"operation_attempts", "operation_runs", "device_activities"}
        activity = _value(
            owned, "SELECT dispatch_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 2  # MAY_HAVE_DISPATCHED
        run = _value(
            owned,
            "SELECT kind, activity_id, timeout_s_json FROM operation_runs WHERE id = ?",
            ticket.run_id,
        )
        assert run[0] == 1  # START
        assert run[1] == 1
        assert Decimal(run[2]) == Decimal("10")
    finally:
        owned.connection.close()


def test_grant_without_activity_is_rejected_whole(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1)
        # 未建立设备活动行：授予整组拒绝，不产生流程或尝试。
        connection.commit()

        outcome = repository.grant_start(
            _grant_request(1), new_operation_key(), owned
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs")[0] == 0
        assert _value(owned, "SELECT COUNT(*) FROM operation_attempts")[0] == 0
    finally:
        owned.connection.close()


def test_two_candidates_same_device_only_one_granted(tmp_path: Path) -> None:
    """同设备两个候选竞争只能一个获准；同时间排序始终一致。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1, input_index=0, status=2)
        _seed_record_action(connection, 2, 1, input_index=1, status=2)
        _seed_activity(connection, 1)
        _seed_activity(connection, 2)
        connection.commit()

        first = repository.grant_start(_grant_request(1), new_operation_key(), owned)
        second = repository.grant_start(_grant_request(2), new_operation_key(), owned)
        assert first.value.outcome is GrantOutcome.GRANTED
        assert second.value.outcome is GrantOutcome.REJECTED
        assert second.value.reason == "device_busy"

        # 相同排序重复请求第一候选：已持有者沿原责任，另一候选仍被拒。
        again = repository.grant_start(_grant_request(2), new_operation_key(), owned)
        assert again.value.outcome is GrantOutcome.REJECTED
        holder = current_start_holder(connection, "cam-1")
        assert holder is not None and holder.action_id == 1
    finally:
        owned.connection.close()


def test_earlier_candidate_wins_consistently(tmp_path: Path) -> None:
    """排序更早的候选优先；较晚候选请求授予被拒并指向更早候选。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_plan(connection, 2)
        # 计划 2 的动作计划时间更早。
        _seed_record_action(connection, 2, 2, input_index=0, scheduled_at=_NOW - 5_000_000)
        _seed_record_action(connection, 1, 1, input_index=0, scheduled_at=_NOW)
        _seed_activity(connection, 1)
        _seed_activity(connection, 2)
        connection.commit()

        late = repository.grant_start(_grant_request(1), new_operation_key(), owned)
        assert late.value.outcome is GrantOutcome.REJECTED
        assert late.value.reason == "not_first_candidate"
        early = repository.grant_start(
            _grant_request(2), new_operation_key(), owned
        )
        assert early.value.outcome is GrantOutcome.GRANTED
    finally:
        owned.connection.close()


def test_grant_rejects_window_and_terminal_states(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1)
        _seed_activity(connection, 1)
        connection.commit()

        early = repository.grant_start(
            _grant_request(1, trusted_wall_now=_WINDOW.scheduled_at - 1),
            new_operation_key(),
            owned,
        )
        assert early.value.outcome is GrantOutcome.REJECTED
        assert early.value.reason == "too_early"

        late = repository.grant_start(
            _grant_request(1, trusted_wall_now=_WINDOW.window_end + 1),
            new_operation_key(),
            owned,
        )
        assert late.value.outcome is GrantOutcome.REJECTED
        assert late.value.reason == "window_ended"

        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE actions SET cancel_requested = 1 WHERE id = 1")
        connection.commit()
        canceled = repository.grant_start(
            _grant_request(1), new_operation_key(), owned
        )
        assert canceled.value.outcome is GrantOutcome.REJECTED
        assert canceled.value.reason == "canceled"
    finally:
        owned.connection.close()


def test_holder_derivation_detects_inconsistency(tmp_path: Path) -> None:
    """同设备出现两个满足持有者条件的动作：按一致性错误处理。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    repository = SchedulingRepository()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_record_action(connection, 1, 1, status=2)
        _seed_record_action(connection, 2, 1, input_index=1, status=2)
        _seed_activity(connection, 1)
        _seed_activity(connection, 2)
        connection.commit()
        first = repository.grant_start(_grant_request(1), new_operation_key(), owned)
        assert first.value.outcome is GrantOutcome.GRANTED

        # 直接构造第二个非法持有者（测试准备，绕过授予入口）。
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (50, 2, NULL, 1, NULL, 'start/2', 2, NULL, NULL, NULL, 2, 1,"
            " 2, '10', NULL, 0, NULL)"
        )
        connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, copy_round,"
            " status, intent_event_id, result_event_id, max_attempts_used,"
            " timeout_s_json, retry_interval_s_json, effect_state, result_json,"
            " error_json)"
            " VALUES (50, 50, 1, NULL, 1, 1, NULL, 2, '10', NULL, 1, NULL, NULL)"
        )
        connection.commit()

        with pytest.raises(ConsistencyError):
            current_start_holder(connection, "cam-1")
    finally:
        owned.connection.close()
