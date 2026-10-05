"""真实 run 会话驱动拍摄计划的组件集成测试。

真实装配（execute_command + 会话推进循环）与注入的拍摄流程和
契约驱动替身组合：run 受理未来计划后等待计划时间，到期同一轮
开始动作并派发设备调用，正常路径保存成功终态与正式产物，设备
失败路径保留文件并保存失败终态；本地报告责任使会话继续等待，
取消会话后接纳释放且受理幂等。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
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
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.scheduling.rules import LaunchWindow
from camctl.session.locks import probe_admission

from ..capture.test_capture_contract import (
    DriverDouble,
    ResultsDouble,
    _EVIDENCE,
    _entry,
)
from ..persistence.test_runtime import _create_valid_database  # noqa: F401

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

pytestmark = pytest.mark.asyncio

_CAMERA_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}


class _Catalog:
    """受理目录替身：支持 cam-1 的单张拍摄。"""

    def action_types(self):
        return frozenset({"camera_take_photo"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_take_photo"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if not self.device_supports(device_id, action_type):
            return None
        return ParameterDefinition(
            schema=_CAMERA_DEFINITION, defaults={})


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
    """计划时间按 UTC 表达；部署时钟与受理解析都使用 UTC。"""
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _plan_body(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


async def _parsed(tmp_path: Path, body: dict):
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body), encoding="utf-8")

    class Reader:
        def read(self, path: str) -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

    return parse_input(await read_input(str(target), Reader()))


class _ActivityDriver(DriverDouble):
    """契约替身：观察身份使用真实活动身份，而非种子的固定值。"""

    def __init__(self, activity_target: str = "1", error=None) -> None:
        super().__init__(error=error)
        self._target = activity_target

    async def control(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        observation_type, _ = self._OBSERVATIONS[request.operation]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation_type, version=1,
                    data={"activity_id": self._target}),
            ),
            error=self.error,
        )


class _SequentialActivityDriver(DriverDouble):
    """契约替身：按派发顺序核对各动作自身的活动身份。"""

    def __init__(self, targets: tuple[str, ...]) -> None:
        super().__init__()
        self._targets = list(targets)

    async def control(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        observation_type, _ = self._OBSERVATIONS[request.operation]
        target = self._targets[len(self.calls) - 1]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation_type, version=1,
                    data={"activity_id": target}),
            ),
            error=self.error,
        )


def _capture_factory(driver: DriverDouble, files: dict):
    def build(owned) -> CaptureRuntime:
        return CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=ResultsDouble(files),
            evidence=_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=time.monotonic_ns,
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0),
        )

    return build


def _run(deps, source, driver, files):
    return asyncio.create_task(execute_command(
        deps, source,
        flows={"scheduling": capture_flow(_capture_factory(driver, files))},
        poll_interval_s=0.1,
    ))


def _scalar(db_path: Path, sql: str):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql).fetchone()


def _all(db_path: Path, sql: str):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql).fetchall()


async def _await_status(db_path: Path, status: int, timeout_s: float = 12.0) -> None:
    """异步轮询动作状态；等待期间让出控制权供会话任务推进。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _scalar(db_path, "SELECT status FROM actions WHERE id = 1")
        if row is not None and row[0] == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待动作终态 {status} 超时，当前: "
        f"{_scalar(db_path, 'SELECT status FROM actions WHERE id = 1')}")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestRunDispatch:
    async def test_run_dispatches_due_photo_and_keeps_report_wait(
        self, tmp_path: Path
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        source = await _parsed(tmp_path, _plan_body("1", _future_schedule(2)))
        driver = _ActivityDriver()
        files = {1: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}
        task = _run(deps, source, driver, files)
        try:
            # 到期前不派发；到期后同一轮开始动作、调用设备并保存终态。
            await _await_status(Path(cfg.paths.state_db), 3)
            assert driver.calls == ["take_photo"]
            row = _scalar(
                Path(cfg.paths.state_db),
                "SELECT COUNT(*) FROM outputs o JOIN device_files f"
                " ON o.device_file_id = f.id WHERE f.completion_state = 3")
            assert row == (1,)
            # 本地报告责任未完成（报告流程未装配）：会话继续等待并
            # 持有接纳。
            await asyncio.sleep(0.3)
            assert probe_admission(deps.admission_lock).is_free is False
        finally:
            await _cancel(task)
        close_runtime(deps)
        assert probe_admission(deps.admission_lock).is_free is True

    async def test_run_dispatch_failure_path_fails_action(
        self, tmp_path: Path
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        source = await _parsed(tmp_path, _plan_body("2", _future_schedule(2)))
        driver = _ActivityDriver(error={"code": "device_error"})
        files = {1: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}
        task = _run(deps, source, driver, files)
        try:
            await _await_status(Path(cfg.paths.state_db), 4)
            row = _scalar(
                Path(cfg.paths.state_db),
                "SELECT error_code FROM actions WHERE id = 1")
            assert row[0] is not None
            # 失败仍登记完整且归属明确的文件。
            assert _scalar(
                Path(cfg.paths.state_db),
                "SELECT COUNT(*) FROM outputs") == (1,)
            assert probe_admission(deps.admission_lock).is_free is False
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_cancelled_run_rerun_is_idempotent(self, tmp_path: Path) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        source = await _parsed(tmp_path, _plan_body("3", _future_schedule(2)))
        driver = _ActivityDriver()
        files = {1: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}

        # 第一次 run 在等待计划时间时被取消：计划与动作已持久化。
        first = _run(deps, source, driver, files)
        await asyncio.sleep(0.5)
        await _cancel(first)
        assert probe_admission(deps.admission_lock).is_free is True
        assert _scalar(
            Path(cfg.paths.state_db), "SELECT COUNT(*) FROM plans") == (1,)
        assert _scalar(
            Path(cfg.paths.state_db), "SELECT COUNT(*) FROM actions") == (1,)

        # 同一请求重送：受理幂等，动作仍只有一个并被派发到终态。
        second = _run(deps, source, driver, files)
        try:
            await _await_status(Path(cfg.paths.state_db), 3)
            assert _scalar(
                Path(cfg.paths.state_db), "SELECT COUNT(*) FROM actions") == (1,)
            assert driver.calls == ["take_photo"]
        finally:
            await _cancel(second)
        close_runtime(deps)


class TestRunWindowExpiration:
    """窗口外未派发动作的过期退出：报告流程与拍摄流程共同装配。"""

    @staticmethod
    def _flows(deps, driver, files):
        from camctl.bootstrap.lifecycle import _report_assembly

        report_flows, supervisor = _report_assembly(deps)
        flows = {
            "scheduling": capture_flow(_capture_factory(driver, files)),
            **report_flows,
        }
        return flows, supervisor

    async def test_run_expires_missed_window_and_exits(
        self, tmp_path: Path,
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        # 计划时间与窗口都远在过去：会话首轮检查时窗口已结束且没有
        # 持久化观察，动作按错过窗口过期。
        source = await _parsed(tmp_path, _plan_body("9", "2026-01-15 09:00:00"))
        driver = _ActivityDriver()
        flows, supervisor = self._flows(deps, driver, {})
        try:
            outcome = await asyncio.wait_for(
                execute_command(deps, source, flows=flows, poll_interval_s=0.1),
                120)
        finally:
            await supervisor.stop()
            close_runtime(deps)

        assert outcome.succeeded is True, outcome.details
        db = Path(cfg.paths.state_db)
        assert _scalar(
            db, "SELECT status, expiration_reason, first_window_observed_at"
            " FROM actions WHERE id = 1") == (5, 1, None)
        assert _scalar(db, "SELECT status FROM plans") == (3,)
        # 过期动作从未派发设备调用。
        assert driver.calls == []
        # 过期变化已全部发布：报告责任清空后会话退出。
        reports = _all(db, "SELECT id, status FROM reports")
        assert reports and all(status == 4 for _, status in reports)
        ready_files = list((tmp_path / "ready").glob("status-report-*.json"))
        assert ready_files
        assert not any((tmp_path / "staging" / "reports").iterdir())

    async def test_run_expires_exhausted_window_after_observation(
        self, tmp_path: Path,
    ) -> None:
        from camctl.acceptance.service import AcceptanceContext, accept_input
        from camctl.persistence.repositories.acceptance import (
            AcceptanceRepository,
        )
        from camctl.persistence.repositories.scheduling import (
            ObserveOutcome,
            ObserveWindowRequest,
        )
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        # 先直接受理一个窗口临近的计划：窗口内保存首次观察后等待
        # 窗口结束，模拟观察已提交但启动未发生的恢复场景。
        scheduled_second = int(time.time()) + 2
        body = _plan_body(
            "10", datetime.fromtimestamp(scheduled_second, timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"))
        body["actions"][0]["policy"]["max_delay_ms"] = 1500
        source = await _parsed(tmp_path, body)

        owned = open_existing(
            Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            accepted = await accept_input(
                source,
                AcceptanceContext(
                    mode=CommandMode.RUN,
                    catalog=_Catalog(),
                    repository=AcceptanceRepository(),
                    clock=type("C", (), {
                        "utc_micros": staticmethod(
                            lambda: int(time.time() * 1_000_000))})(),
                ),
                new_operation_key(), owned,
            )
            assert accepted.plan_id is not None
            while time.time() < scheduled_second + 0.1:
                await asyncio.sleep(0.05)
            now_us = int(time.time() * 1_000_000)
            observation = SchedulingRepository().observe_window(
                ObserveWindowRequest(
                    action_id=1, trusted_wall_now=now_us, occurred_at=now_us),
                new_operation_key(), owned)
            assert observation.value.outcome is ObserveOutcome.OBSERVED
            observed_at = now_us
        finally:
            owned.connection.close()

        # 等待窗口结束后进入会话：按已持久化观察区分为窗口耗尽。
        await asyncio.sleep(max(scheduled_second + 1.6 - time.time(), 0.0))
        driver = _ActivityDriver()
        flows, supervisor = self._flows(deps, driver, {})
        try:
            outcome = await asyncio.wait_for(
                execute_command(deps, source, flows=flows, poll_interval_s=0.1),
                120)
        finally:
            await supervisor.stop()
            close_runtime(deps)

        assert outcome.succeeded is True, outcome.details
        db = Path(cfg.paths.state_db)
        assert _scalar(
            db, "SELECT status, expiration_reason, first_window_observed_at"
            " FROM actions WHERE id = 1") == (5, 2, observed_at)
        assert _scalar(db, "SELECT status FROM plans") == (3,)
        assert driver.calls == []
        reports = _all(db, "SELECT id, status FROM reports")
        assert reports and all(status == 4 for _, status in reports)


class TestSameDeviceProgress:
    """Q6 会话级：占用释放后同设备下一动作继续推进。"""

    async def test_second_action_proceeds_after_first_releases(
        self, tmp_path: Path,
    ) -> None:
        cfg = _config_for(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        # 同设备两个拍摄动作：第一份完成后其占用释放，第二份在
        # 同一会话内继续派发并完成。
        body = {
            "request_id": "21",
            "created_at": "2026-01-15 08:00:00",
            "name": "plan",
            "actions": [
                {
                    "name": f"shoot-{index}",
                    "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": _future_schedule(1),
                    "params": {"type": "single_shot"},
                    "policy": {"max_delay_ms": 30_000},
                }
                for index in range(2)
            ],
        }
        source = await _parsed(tmp_path, body)
        driver = _SequentialActivityDriver(("1", "2"))
        files = {
            1: (_entry("shot-1", kind=ResultFileKind.PHOTO),),
            2: (_entry("shot-2", kind=ResultFileKind.PHOTO),),
        }
        flows, supervisor = TestRunWindowExpiration._flows(deps, driver, files)
        try:
            outcome = await asyncio.wait_for(
                execute_command(deps, source, flows=flows, poll_interval_s=0.1),
                120)
        finally:
            await supervisor.stop()
            close_runtime(deps)

        assert outcome.succeeded is True, outcome.details
        db = Path(cfg.paths.state_db)
        statuses = _all(db, "SELECT id, status FROM actions ORDER BY id")
        assert statuses == [(1, 3), (2, 3)]
        assert driver.calls == ["take_photo", "take_photo"]
        # 两个活动都已收场并释放：同设备占用不再保持。
        occupancies = _all(
            db, "SELECT activity_state, occupancy_state FROM device_activities"
            " ORDER BY id")
        assert occupancies == [(3, 2), (3, 2)]
        reports = _all(db, "SELECT id, status FROM reports")
        assert reports and all(status == 4 for _, status in reports)
