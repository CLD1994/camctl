"""录像停止链的会话级组件集成测试。

真实 run 会话装配（execute_command 与拍摄、取消流程）同契约驱动与
停止端口替身组合：录像启动确认后按会话锚点加目标时长到时停止，停
止确认即收场设备活动，控制完成加完整结果登记成功终态与正式产物；
停止调用失败沿原预算重试，不刷新额度；录制中取消生效后不经计时立
即停止，按取消终态收场且不登记正式产物。结果列举由契约替身提供
（驱动适配归 D5），异常录像的检查与修复决策随媒体链轮次接入。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime, SessionRecordingState
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.tasks import CaptureTask
from camctl.devices.ports import DeviceCallResult
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_capture_contract import (
    ResultsDouble,
    _entry,
)
from .test_run_dispatch import _ActivityDriver, _parsed

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

pytestmark = pytest.mark.asyncio

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="start_confirmed", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
    )
)

_RECORD_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "video"}},
    "required": ["type"],
    "additionalProperties": False,
}


class _RecordCatalog:
    """受理目录替身：支持 cam-1 的录像与取消动作。"""

    duration_s = Decimal("6")

    def action_types(self):
        return frozenset({"camera_record", "cancel_task"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_record"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if not self.device_supports(device_id, action_type):
            return None
        if parameter_type != "video":
            return None

        def task(params):
            return CaptureTask(
                "camera_record",
                target_duration_s=self.duration_s,
                stop_supported=True,
            )

        return ParameterDefinition(
            schema=_RECORD_DEFINITION, defaults={}, task_factory=task)


class _StopDouble:
    """停止端口替身：前 failures 次返回调用错误，之后确认停止。"""

    def __init__(self, activity_target: str = "1", failures: int = 0) -> None:
        self._target = activity_target
        self._failures = failures
        self.calls: list[str] = []

    async def stop(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        if len(self.calls) <= self._failures:
            return DeviceCallResult(
                observations=(), error={"code": "device_error"})
        from camctl.devices.evidence import DeviceObservation

        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": self._target}),
            ),
            error=None,
        )


def _config_for(home: Path):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            }
        },
        ConfigDefaults(),
    )


def _future_schedule(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _record_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "record",
                "type": "camera_record",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def _cancel_plan(request_id: str, target_id: int, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "cancel",
                "type": "cancel_task",
                "scheduled_at": scheduled_at,
                "params": {"target": {"action_instance_id": str(target_id)}},
            }
        ],
    }


async def _submit_plan(tmp_path: Path, cfg, catalog, body: dict) -> None:
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=catalog)
    try:
        outcome = await execute_command(deps, await _parsed(tmp_path, body))
        assert outcome.succeeded is True, outcome.details
    finally:
        close_runtime(deps)


def _record_factory(driver, stopper, results, clock):
    """会话共享锚点表：推进循环每轮重建运行时，锚点跨轮保留。"""
    anchors: dict[int, tuple[int, int]] = {}

    def build(owned, device_id: str) -> CaptureRuntime:
        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=results,
            evidence=_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=lambda: clock["ns"],
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0),
            stopper=stopper,
        )
        runtime.recording_state = SessionRecordingState(runtime, anchors)
        return runtime

    return build


def _run_session(deps, cfg, driver, stopper, clock, results):
    return asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(
                _record_factory(driver, stopper, results, clock)),
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _scalar(db_path: Path, sql: str, *params):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, params).fetchone()


async def _await_query(
        db_path: Path, sql: str, expected, timeout_s: float = 15.0) -> None:
    """异步轮询查询结果；等待期间让出控制权供会话任务推进。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(sql).fetchone()
        if row == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待 {sql} = {expected} 超时，当前: "
        f"{_scalar(db_path, sql if '?' not in sql else sql)}")


class TestNormalStopAtTarget:
    async def test_stops_at_anchor_target_and_finishes(
            self, tmp_path: Path) -> None:
        home = tmp_path / "normal"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _RecordCatalog(),
            _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
        driver = _ActivityDriver()
        stopper = _StopDouble()
        clock = {"ns": time.monotonic_ns()}
        results = ResultsDouble({})
        task = _run_session(deps, cfg, driver, stopper, clock, results)
        try:
            # 到期开始：启动确认即登记本会话计时锚点与活动启动事实。
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL, activity_state"
                " FROM device_activities WHERE id = 1", (1, 2))
            assert driver.calls == ["start_recording"]
            # 会话单调钟越过锚点加目标时长：下一轮到时停止并可靠确认。
            clock["ns"] += 6_000_000_001
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'stop/1'", (3, 1))
            assert stopper.calls == ["stop_recording"]
            # 停止确认即收场活动：结束观察与占用释放同链完成。
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 2)
            # 结果尚未列举：动作保持执行中等待产物核实。
            assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
            # 迟到的完整结果：控制完成即成功依据，终态与产物登记。
            results.files_by_action[1] = (_entry("clip-1"),)
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            output = _scalar(
                db, "SELECT kind, device_file_id FROM outputs"
                " WHERE source_action_id = 1")
            assert output == (1, 1)
            assert _scalar(
                db, "SELECT status FROM plans WHERE id = 1") == (3,)
        finally:
            await _cancel(task)
        close_runtime(deps)


class TestStopRetryWithinBudget:
    async def test_failed_stop_retries_and_finishes(
            self, tmp_path: Path) -> None:
        home = tmp_path / "retry"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _RecordCatalog(),
            _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
        driver = _ActivityDriver()
        stopper = _StopDouble(failures=1)
        clock = {"ns": time.monotonic_ns()}
        results = ResultsDouble({1: (_entry("clip-1"),)})
        task = _run_session(deps, cfg, driver, stopper, clock, results)
        try:
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE id = 1", (1,))
            clock["ns"] += 6_000_000_001
            # 第一次停止调用失败保存调用错误并建立重试等待，预算沿原
            # 流程累计；第二次停止调用成功后结束流程并收场活动。
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'stop/1'", (3, 2))
            assert stopper.calls == ["stop_recording", "stop_recording"]
            assert _scalar(
                db, "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'stop/1'"
                " AND a.attempt_no = 1") == (3,)
            assert _scalar(
                db, "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'stop/1'"
                " AND a.attempt_no = 2") == (2,)
            # 重试成功后同样到达终态：完整结果随控制完成登记。
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 2)
            assert _scalar(db, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            await _cancel(task)
        close_runtime(deps)


class TestCancelDuringRecording:
    async def test_cancel_stops_immediately_and_ends_without_outputs(
            self, tmp_path: Path) -> None:
        home = tmp_path / "cancel"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _RecordCatalog()
        catalog.duration_s = Decimal("600")
        await _submit_plan(
            tmp_path, cfg, catalog, _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        record_id = _scalar(
            db, "SELECT id FROM actions WHERE name = 'record'")[0]
        # 取消排期在录像开始之后、目标时长之前：停止由取消触发。
        await _submit_plan(
            tmp_path, cfg, catalog,
            _cancel_plan("2", record_id, _future_schedule(5)))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        driver = _ActivityDriver()
        stopper = _StopDouble()
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, driver, stopper, clock, ResultsDouble({}))
        try:
            # 录制中取消生效：不经目标时长立即停止并按取消终态收场。
            await _await_query(
                db,
                f"SELECT status, cancel_requested FROM actions"
                f" WHERE id = {record_id}", (6, 1))
            assert stopper.calls == ["stop_recording"]
            # 取消放弃内容：不登记正式产物，计划随动作收场完成。
            assert _scalar(db, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _scalar(
                db, "SELECT status FROM plans WHERE id = 1") == (3,)
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 2)
            # 取消动作在目标终态后收场：成员与动作同为成功。
            await _await_query(
                db, "SELECT status FROM actions WHERE name = 'cancel'", (3,))
            assert _scalar(
                db, "SELECT status FROM cancel_items") == (3,)
        finally:
            await _cancel(task)
        close_runtime(deps)
