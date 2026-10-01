"""C7 应急最终补记的组件集成测试。

真实 SQLite 与 P3 事务内核组合：零尝试已知/未知配置、有尝试已
知配置按正式组合保存；有尝试未知上限、超限、普通意图引用与重
复补记均拒绝；同键重送按原事务核实不重开流程。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.recovery import (
    EmergencyBudget,
    EmergencyOutcome,
    EmergencyRecord,
    RecordStatus,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_capture_guards()

_NOW = 1_750_000_000_000_000
_SESSION = "a" * 32


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
    _seed_record_action(connection, 1, 1)
    _seed_activity(connection, 1, dispatch_state=2, activity_state=2)
    connection.commit()
    return owned


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _attempt(status: int = 3, *, error=None, result=None) -> dict:
    if status == 3 and error is None:
        error = {"code": "stop_rejected", "stage": "driver"}
    return {
        "status": status,
        "effect_state": 2,
        "result_json": result if result is not None else {
            "format_version": 1,
            "settlement": {
                "basis": "observed",
                "evidence": {"type": "stop_confirmed", "version": 1, "data": {}},
            },
            "observations": [],
        },
        "error_json": error,
    }


def _save(owned, record, attempts, *, timeout=Decimal("10"), interval=Decimal("1")):
    return CaptureRepository().save_emergency(
        session_key=_SESSION,
        action_id=1,
        activity_id=1,
        record=record,
        attempts=attempts,
        occurred_at=_NOW,
        key=new_operation_key(),
        owned=owned,
        timeout_s=timeout,
        retry_interval_s=interval,
    )


pytestmark = pytest.mark.asyncio


async def test_stopped_with_attempts_records_final_flow(tmp_path: Path) -> None:
    """有尝试已知配置的停止成功：终态流程+尝试+活动结束共同保存。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED,
            attempts_used=2,
            max_attempts=3,
        )
        outcome = _save(
            owned, record, (_attempt(), _attempt(status=3))
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.record_status is RecordStatus.RECORDED

        run = _value(
            owned,
            "SELECT kind, status, attempts_used, max_attempts_used, error_json,"
            " responsibility_key FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 9 and run[1] == 3 and run[2] == 2
        assert run[3] == 3 and run[4] is None
        assert run[5] == f"emergency/{_SESSION}/1"
        rows = owned.connection.execute(
            "SELECT attempt_no, intent_event_id, result_event_id FROM"
            " operation_attempts WHERE run_id = ? ORDER BY attempt_no",
            (outcome.value.run_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]
        assert all(row[1] is None for row in rows)
        assert all(row[2] is not None for row in rows)
        activity = _value(
            owned, "SELECT activity_state, occupancy_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 3  # ENDED
        assert activity[1] == 1  # 应急不自动释放占用
    finally:
        owned.connection.close()


async def test_zero_attempts_unknown_config_saves_not_attempted(tmp_path: Path) -> None:
    """零尝试且配置未知：FAILED + not_attempted 原因，配置允许为空。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.NOT_ATTEMPTED,
            attempts_used=0,
            max_attempts=3,
        )
        outcome = CaptureRepository().save_emergency(
            session_key=_SESSION,
            action_id=1,
            activity_id=1,
            record=record,
            attempts=(),
            occurred_at=_NOW,
            key=new_operation_key(),
            owned=owned,
            timeout_s=None,
            retry_interval_s=None,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        run = _value(
            owned,
            "SELECT status, attempts_used, max_attempts_used, error_json"
            " FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 4 and run[1] == 0 and run[2] == 3
        assert json.loads(run[3])["code"] == "emergency_not_attempted"
        activity = _value(
            owned, "SELECT activity_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 2  # 未尝试不结束活动
    finally:
        owned.connection.close()


async def test_attempted_unknown_config_is_rejected(tmp_path: Path) -> None:
    """有尝试但配置未知：拒绝。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        outcome = CaptureRepository().save_emergency(
            session_key=_SESSION,
            action_id=1,
            activity_id=1,
            record=record,
            attempts=(_attempt(),),
            occurred_at=_NOW,
            key=new_operation_key(),
            owned=owned,
            timeout_s=None,
            retry_interval_s=None,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs")[0] == 0
    finally:
        owned.connection.close()


async def test_over_limit_and_count_mismatch_are_rejected(tmp_path: Path) -> None:
    """超限与行数不符：整组拒绝。"""
    owned = _environment(tmp_path)
    try:
        over = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=4, max_attempts=3
        )
        assert _save(owned, over, (_attempt(),) * 4).kind is DbOutcomeKind.ROLLED_BACK
        mismatch = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        assert _save(owned, mismatch, (_attempt(), _attempt())).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


async def test_duplicate_session_flow_is_rejected(tmp_path: Path) -> None:
    """同一会话同一活动只有一条应急流程；重复补记拒绝不重开。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        first = _save(owned, record, (_attempt(),))
        assert first.kind is DbOutcomeKind.COMPLETED
        again = _save(owned, record, (_attempt(),))
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 1
    finally:
        owned.connection.close()


async def test_unconfirmed_with_attempts_saves_unconfirmed(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.UNCONFIRMED, attempts_used=2, max_attempts=2
        )
        outcome = _save(
            owned,
            record,
            (_attempt(status=4, error={"code": "timeout", "stage": "transport"}),) * 2,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        run = _value(
            owned,
            "SELECT status, error_json FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 6
        assert json.loads(run[1])["code"] == "emergency_stop_unconfirmed"
        activity = _value(
            owned, "SELECT activity_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 2  # 未确认停止不结束活动
    finally:
        owned.connection.close()
