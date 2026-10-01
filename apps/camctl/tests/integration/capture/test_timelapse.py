"""C5 延时摄影等待安排的组件集成测试。

真实 SQLite 与 P3 事务内核组合：发送成功事实与等待安排按
CAPTURE_WAIT_CHANGED 事件保存；重启后采用本次额外等待重算预计
检查；已保存的等待完成事实不被新配置重写。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.capture.timelapse import (
    CaptureWaitConfig,
    ClockReading,
    EndControl,
    StartReturn,
    TimelapseState,
    WaitKind,
    WaitPlan,
    plan_capture_wait,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.timelapse import (
    ScheduleWait,
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_timelapse_guards()

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
    _seed_record_action(connection, 1, 1)
    _seed_activity(connection, 1)
    # 发送成功事实先行保存（sent_at），等待安排才允许建立预计检查。
    connection.execute("UPDATE device_activities SET sent_at = ? WHERE id = 1", (_NOW,))
    connection.commit()
    return owned


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def test_unchanged_wait_configuration_returns_saved_result_without_new_history(tmp_path):
    owned = _environment(tmp_path)
    repository = TimelapseRepository()
    try:
        config = CaptureWaitConfig(target_duration_ms=600_000, driver_margin_ms=0)
        plan = plan_capture_wait(
            TimelapseState(clock_trusted=True, start_return=StartReturn.SENT,
                           end_control=EndControl.DEVICE, sent_at_utc=_NOW,
                           anchor_monotonic_ns=5_000_000_000), config,
            ClockReading(utc_us=_NOW, monotonic_ns=5_000_000_000))
        command = ScheduleWait(action_id=1, plan=plan, driver_margin_ms=0,
                               extra_wait_ms=0, occurred_at=_NOW)
        first = repository.schedule_wait(command, new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = owned.connection.execute("SELECT id, body_json FROM history_events ORDER BY id").fetchall()
        second = repository.reconfigure_wait(command, new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        assert second.value.expected_check_at == plan.check_at_utc
        assert owned.connection.execute("SELECT id, body_json FROM history_events ORDER BY id").fetchall() == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("field,value", [("extra_wait_ms", 0.0), ("extra_wait_ms", False),
                                         ("check_at_utc", float(_NOW + 600_000_000))])
def test_same_wait_values_do_not_hide_invalid_input_types(tmp_path, field, value):
    owned = _environment(tmp_path)
    repository = TimelapseRepository()
    try:
        command = ScheduleWait(1, WaitPlan(WaitKind.WAIT_THEN_CHECK, _NOW + 600_000_000), 0, 0, _NOW)
        first = repository.schedule_wait(command, new_operation_key(), owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        if field == "check_at_utc":
            command = replace(command, plan=replace(command.plan, check_at_utc=value))
        else:
            command = replace(command, **{field: value})
        before = owned.connection.execute("SELECT id, body_json FROM history_events ORDER BY id").fetchall()
        outcome = repository.reconfigure_wait(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT id, body_json FROM history_events ORDER BY id").fetchall() == before
    finally:
        owned.connection.close()


def test_schedule_saves_wait_plan_from_send_anchor(tmp_path: Path) -> None:
    """等待安排保存发送锚点推导的预计检查；DB 延迟不参与计算。"""
    owned = _environment(tmp_path)
    repository = TimelapseRepository()
    try:
        config = CaptureWaitConfig(
            target_duration_ms=20 * 60 * 1000, driver_margin_ms=60_000
        )
        plan = plan_capture_wait(
            TimelapseState(
                clock_trusted=True,
                start_return=StartReturn.SENT,
                end_control=EndControl.DEVICE,
                sent_at_utc=_NOW,
                anchor_monotonic_ns=5_000_000_000,
            ),
            config,
            ClockReading(utc_us=_NOW + 3600 * 1_000_000, monotonic_ns=9_000_000_000),
        )
        assert plan.kind is WaitKind.WAIT_THEN_CHECK
        outcome = repository.schedule_wait(
            ScheduleWait(
                action_id=1, plan=plan, driver_margin_ms=config.driver_margin_ms,
                extra_wait_ms=config.extra_wait_ms, occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        row = _value(
            owned,
            "SELECT result_wait_margin_ms, extra_wait_ms_used, expected_check_at"
            " FROM device_activities WHERE id = 1",
        )
        assert row[0] == 60_000
        assert row[1] == 0
        assert row[2] == plan.check_at_utc
    finally:
        owned.connection.close()


def test_restart_reconfigure_uses_current_extra_wait(tmp_path: Path) -> None:
    """重启后本次额外等待变化：重算预计检查，等待完成事实不变。"""
    owned = _environment(tmp_path)
    repository = TimelapseRepository()
    try:
        first = plan_capture_wait(
            TimelapseState(
                clock_trusted=True, start_return=StartReturn.SENT,
                end_control=EndControl.DEVICE, sent_at_utc=_NOW,
                anchor_monotonic_ns=5_000_000_000,
            ),
            CaptureWaitConfig(target_duration_ms=600_000, driver_margin_ms=0),
            ClockReading(utc_us=_NOW, monotonic_ns=5_000_000_000),
        )
        repository.schedule_wait(
            ScheduleWait(
                action_id=1, plan=first, driver_margin_ms=0,
                extra_wait_ms=0, occurred_at=_NOW
            ),
            new_operation_key(),
            owned,
        )
        original_check = first.check_at_utc

        resume_at = _NOW + 60 * 1_000_000
        resumed = plan_capture_wait(
            TimelapseState(
                clock_trusted=True, start_return=StartReturn.SENT,
                end_control=EndControl.DEVICE, sent_at_utc=_NOW,
                anchor_monotonic_ns=None, restart=True,
            ),
            CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0, extra_wait_ms=30_000
            ),
            ClockReading(utc_us=resume_at, monotonic_ns=42_000_000_000),
        )
        assert resumed.kind is WaitKind.RESUME_WAIT
        outcome = repository.reconfigure_wait(
            ScheduleWait(
                action_id=1, plan=resumed, driver_margin_ms=0,
                extra_wait_ms=30_000, occurred_at=resume_at,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        row = _value(
            owned,
            "SELECT extra_wait_ms_used, expected_check_at, wait_completed_event_id"
            " FROM device_activities WHERE id = 1",
        )
        assert row[0] == 30_000
        assert row[1] == original_check + 30_000 * 1000
        assert row[2] is None  # 等待完成事实仍未建立
    finally:
        owned.connection.close()
