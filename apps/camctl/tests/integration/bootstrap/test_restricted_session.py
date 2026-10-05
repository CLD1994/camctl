"""取消执行与受限会话的生产装配集成测试。

真实 execute_command 装配：正常会话按排期或立即执行取消动作并
完成目标取消；墙钟不可信的受限会话不进入普通调度，仍执行本次新
受理且未排期的取消动作并完成其目标取消与必要收场，已排期的取消
动作与普通拍摄动作保留待执行，不用不可信墙钟判断过期或到时。受
限会话处理一次报告责任后按时钟异常退出，不更新可信时间下界；重
复受限运行不重复施加取消，也不为无新变化的责任生成新报告。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.contracts.workflow_errors import registered_error
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.operations import (
    register_operation_guards,
)
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards

from . import test_run_dispatch
from .test_run_dispatch import _parsed

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

pytestmark = pytest.mark.asyncio


class _Catalog(test_run_dispatch._Catalog):
    """受理目录替身：在单张拍摄之外支持取消动作。"""

    def action_types(self):
        return super().action_types() | {"cancel_task"}


def _config_for(home: Path, *, restricted: bool) -> object:
    clock = ({"min_plausible_date": "2100-01-01", "recheck_delay_s": "0.01"}
             if restricted else {})
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "clock": clock,
        },
        ConfigDefaults(),
    )


def _future_schedule(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _plan_with_photo(request_id: str, scheduled_at: str, *,
                     cancel_scheduled_at: str | None,
                     cancel_target: dict | None = None) -> dict:
    """一份拍摄计划：远期拍摄动作，可带一个已排期的取消动作。"""
    actions = [
        {
            "name": "shoot",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": scheduled_at,
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 5000},
        }
    ]
    if cancel_scheduled_at is not None:
        actions.append({
            "name": "later",
            "type": "cancel_task",
            "scheduled_at": cancel_scheduled_at,
            # 默认指向不存在的计划：受限对照里保持不执行即可。
            "params": {"target": cancel_target or {"plan_instance_id": "900"}},
        })
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": actions,
    }


def _cancel_plan(request_id: str, target: dict, *,
                 scheduled_at: str | None = None) -> dict:
    action = {
        "name": "cancel",
        "type": "cancel_task",
        "params": {"target": target},
    }
    if scheduled_at is not None:
        action["scheduled_at"] = scheduled_at
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [action],
    }


def _scalar(db_path: Path, sql: str, *params):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, params).fetchone()


def _all(db_path: Path, sql: str, *params):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, params).fetchall()


async def _submit_plan(tmp_path: Path, cfg, body: dict) -> None:
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=_Catalog())
    try:
        outcome = await execute_command(deps, await _parsed(tmp_path, body))
        assert outcome.succeeded is True, outcome.details
    finally:
        close_runtime(deps)


class TestNormalCancelExecution:
    """正常会话的取消执行：时钟可信时取消动作按排期推进。"""

    async def test_normal_run_executes_cancel_and_exits(
            self, tmp_path: Path) -> None:
        home = tmp_path / "normal"
        home.mkdir()
        cfg = _config_for(home, restricted=False)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 远期拍摄计划与独立的已排期取消计划：取消到期后取消整个
        # 拍摄计划，会话没有待执行工作，正常退出。
        await _submit_plan(
            tmp_path, cfg,
            _plan_with_photo("1", _future_schedule(7200),
                             cancel_scheduled_at=None))
        await _submit_plan(
            tmp_path, cfg,
            _cancel_plan("2", {"plan_instance_id": "1"},
                         scheduled_at=_future_schedule(1)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        try:
            outcome = await asyncio.wait_for(
                execute_command(deps, None, poll_interval_s=0.1), 120)
        finally:
            close_runtime(deps)

        assert outcome.succeeded is True, outcome.details
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'cancel'") == (3,)
        assert _scalar(
            db, "SELECT status, cancel_requested FROM actions"
            " WHERE name = 'shoot'") == (6, 1)
        assert _all(db, "SELECT status FROM cancel_items") == [(3,)]
        assert _scalar(db, "SELECT COUNT(*) FROM reports WHERE status = 4")[0] >= 1
        assert len(list((home / "ready").glob("status-report-*.json"))) >= 1

    async def test_normal_run_executes_unscheduled_cancel_from_input(
            self, tmp_path: Path) -> None:
        home = tmp_path / "normal-unscheduled"
        home.mkdir()
        cfg = _config_for(home, restricted=False)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg,
            _plan_with_photo("1", _future_schedule(7200),
                             cancel_scheduled_at=None))
        db = Path(cfg.paths.state_db)
        shoot_id = _scalar(
            db, "SELECT id FROM actions WHERE name = 'shoot'")[0]
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        try:
            outcome = await asyncio.wait_for(
                execute_command(
                    deps,
                    await _parsed(tmp_path, _cancel_plan(
                        "2", {"action_instance_id": str(shoot_id)})),
                    poll_interval_s=0.1),
                120)
        finally:
            close_runtime(deps)

        assert outcome.succeeded is True, outcome.details
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'cancel'") == (3,)
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'shoot'") == (6,)


class TestRestrictedSession:
    async def test_restricted_executes_unscheduled_cancel_and_reports_once(
            self, tmp_path: Path) -> None:
        home = tmp_path / "restricted"
        home.mkdir()
        cfg = _config_for(home, restricted=False)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 先提交远期拍摄计划（含一个已排期的取消动作作为对照）。
        await _submit_plan(
            tmp_path, cfg,
            _plan_with_photo("1", _future_schedule(7200),
                             cancel_scheduled_at=_future_schedule(3600)))
        db = Path(cfg.paths.state_db)
        shoot_id = _scalar(
            db, "SELECT id FROM actions WHERE name = 'shoot'")[0]

        # 本次 run 受理未排期的取消请求后墙钟检查失败：进入受限会话。
        run_cfg = _config_for(home, restricted=True)
        deps = build_runtime(CommandMode.RUN, run_cfg, catalog=_Catalog())
        try:
            outcome = await execute_command(
                deps,
                await _parsed(
                    tmp_path, _cancel_plan("2", {"action_instance_id": str(shoot_id)})),
                poll_interval_s=0.1)
        finally:
            close_runtime(deps)

        assert outcome.succeeded is False
        assert outcome.reason == "clock_invalid"
        # 未排期取消动作已执行并成功结束；目标拍摄动作已生效取消。
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'cancel'") == (3,)
        assert _scalar(
            db, "SELECT status, cancel_requested FROM actions WHERE id = ?",
            shoot_id) == (6, 1)
        assert _all(db, "SELECT status FROM cancel_items") == [(3,)]
        # 对照：已排期取消与远期拍摄动作保持待执行，不用不可信墙钟
        # 判断到时或过期。
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'later'") == (1,)
        # 一次报告机会：取消结果进入本次报告并完成本地发布。
        reports = _all(
            db, "SELECT id, status, frozen_event_id FROM reports")
        assert reports and all(row[1] == 4 for row in reports)
        cancel_last = _scalar(
            db, "SELECT last_event_id FROM actions WHERE name = 'cancel'")[0]
        assert all(row[2] >= cancel_last for row in reports)
        ready = list((home / "ready").glob("status-report-*.json"))
        assert len(ready) == 1
        # 不可信读数不推进可信时间下界。
        assert _scalar(
            db, "SELECT trusted_time_lower_bound FROM runtime_state"
            " WHERE id = 1") == (None,)

        # 重复受限运行：取消不重复施加，无新变化不再生成新报告。
        deps = build_runtime(CommandMode.RUN, run_cfg, catalog=_Catalog())
        try:
            again = await execute_command(deps, None, poll_interval_s=0.1)
        finally:
            close_runtime(deps)
        assert again.reason == "clock_invalid"
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'cancel'") == (3,)
        assert _all(db, "SELECT status FROM cancel_items") == [(3,)]
        assert _all(db, "SELECT id FROM reports") == [
            (row[0],) for row in reports]
        assert len(list((home / "ready").glob("status-report-*.json"))) == 1

    async def test_restricted_cancel_target_not_found_fails_action(
            self, tmp_path: Path) -> None:
        home = tmp_path / "missing"
        home.mkdir()
        cfg = _config_for(home, restricted=False)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _plan_with_photo("1", _future_schedule(7200),
                                            cancel_scheduled_at=None))
        db = Path(cfg.paths.state_db)

        run_cfg = _config_for(home, restricted=True)
        deps = build_runtime(CommandMode.RUN, run_cfg, catalog=_Catalog())
        try:
            outcome = await execute_command(
                deps,
                await _parsed(
                    tmp_path,
                    _cancel_plan("2", {"action_instance_id": "4242"})),
                poll_interval_s=0.1)
        finally:
            close_runtime(deps)

        assert outcome.reason == "clock_invalid"
        # 可靠确认目标不存在：取消动作按登记错误结束，不创建成员。
        not_found = registered_error("cancel_target_not_found")[
            "action_error_id"]
        assert _scalar(
            db, "SELECT status, error_code FROM actions"
            " WHERE name = 'cancel'") == (4, not_found)
        assert _all(db, "SELECT id FROM cancel_items") == []
        # 目标计划不受影响，报告责任仍完成一次。
        assert _scalar(
            db, "SELECT status FROM actions WHERE name = 'shoot'") == (1,)
        assert _scalar(db, "SELECT COUNT(*) FROM reports WHERE status = 4")[0] >= 1
