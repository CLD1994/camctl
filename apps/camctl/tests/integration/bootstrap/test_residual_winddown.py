"""后续动作触发残留收场的会话级组件集成测试。

真实 run 会话装配（capture_flow 残留门、residual_flow 孤儿推进、
execute_command）同契约驱动、查询与停止端口替身组合：录像停止预
算耗尽后动作失败终态化而活动占用保留，后续到期动作先按需执行前
检查，确认已知残留即建立独立收场流程按预算停止，可靠确认后收场
活动、释放占用并放行触发动作；触发动作取消后已发出的收场继续使
用剩余次数；收场预算耗尽保留残留事实，触发动作按窗口过期收尾。
三类计数（执行前检查、残留收场停止、确认查询）相互独立，重复检
查不新建预算，重启后沿原次数继续。
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

from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow, residual_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime, SessionRecordingState
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.evidence import (
    DeviceObservation, EvidenceContract, EvidenceRegistry,
)
from camctl.devices.tasks import CaptureTask
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import RetryWaitGate
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.capture import (
    CaptureRepository, register_capture_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository, register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository, register_timelapse_guards,
)
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_capture_contract import ResultsDouble, _entry
from .test_run_dispatch import _parsed

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
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="photo_taken", version=1, operation="control",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="query_returned", version=1, operation="query",
                         fields=frozenset()),
        EvidenceContract(type="activity_status", version=1, operation="query",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
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

_PHOTO_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}


class _WinddownCatalog:
    """受理目录替身：支持 cam-1 的录像、照片与取消动作。"""

    duration_s = Decimal("6")

    def action_types(self):
        return frozenset({"camera_record", "camera_take_photo", "cancel_task"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type in (
            "camera_record", "camera_take_photo")

    def parameter_definition(self, device_id, action_type, parameter_type):
        if action_type == "camera_record":
            if parameter_type != "video":
                return None

            def task(params):
                return CaptureTask(
                    "camera_record",
                    target_duration_s=self.duration_s,
                    stop_supported=True,
                )
        elif action_type == "camera_take_photo":
            if parameter_type != "single_shot":
                return None

            def task(params):
                return CaptureTask("camera_take_photo")
        else:
            return None
        return ParameterDefinition(
            schema=_RECORD_DEFINITION if action_type == "camera_record"
            else _PHOTO_DEFINITION,
            defaults={}, task_factory=task)


class _ControlDouble:
    """控制端口替身：录像确认活动 1，照片返回场景指定的活动身份。"""

    def __init__(self, *, photo_activity_id: int = 2) -> None:
        self.calls: list[str] = []
        self._photo_activity_id = photo_activity_id

    async def control(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        if request.operation == "start_recording":
            observation, identity = "start_confirmed", "1"
        else:
            observation, identity = "photo_taken", str(self._photo_activity_id)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation, version=1, data={"activity_id": identity}),
            ),
            error=None,
        )


class _QueryDouble:
    """查询端口替身：按调用序返回编排的观察（活动或空闲）或错误。"""

    def __init__(self, responses: tuple, *, paused: bool = False) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.release = asyncio.Event()
        if not paused:
            self.release.set()

    async def query_state(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        await self.release.wait()
        response = self._responses.pop(0) if len(self._responses) > 1 \
            else self._responses[0]
        if response == "idle":
            return DeviceCallResult(observations=(), error=None)
        if response == "error":
            return DeviceCallResult(observations=(), error={"code": "device_error"})
        identity = str(response)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="activity_status", version=1,
                    data={"activity_id": identity}),
            ),
            error=None,
        )


class _StopDouble:
    """停止端口替身：按序返回错误、发出未确认或可靠确认停止。

    前 failures 次返回调用错误；随后 unconfirmed 次返回发出但未确
    认（无观察无错误）；之后返回可靠确认停止（观察活动 1）。
    """

    def __init__(self, failures: int = 0, unconfirmed: int = 0) -> None:
        self._failures = failures
        self._unconfirmed = failures + unconfirmed
        self.calls: list[str] = []

    async def stop(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        if len(self.calls) <= self._failures:
            return DeviceCallResult(
                observations=(), error={"code": "device_error"})
        if len(self.calls) <= self._unconfirmed:
            return DeviceCallResult(observations=(), error=None)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1, data={"activity_id": "1"}),
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


def _plan(request_id: str, *actions: dict) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": list(actions),
    }


def _record_action(name: str, scheduled_at: str, *, max_delay_ms: int = 5000):
    return {
        "name": name,
        "type": "camera_record",
        "device_id": "cam-1",
        "scheduled_at": scheduled_at,
        "params": {"type": "video"},
        "policy": {"max_delay_ms": max_delay_ms},
    }


def _photo_action(name: str, scheduled_at: str, *, max_delay_ms: int = 20000):
    return {
        "name": name,
        "type": "camera_take_photo",
        "device_id": "cam-1",
        "scheduled_at": scheduled_at,
        "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": max_delay_ms},
    }


def _cancel_plan(request_id: str, target_id: int) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "cancel",
                "type": "cancel_task",
                "scheduled_at": _future_schedule(0),
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


def _winddown_factory(driver, stopper, query, clock, files=None):
    """会话共享锚点表与重试等待门槛：推进循环每轮重建运行时。"""
    anchors: dict[int, tuple[int, int]] = {}
    retry_gate = RetryWaitGate()

    def build(owned, device_id: str) -> CaptureRuntime:
        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=ResultsDouble(files or {}),
            evidence=_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=lambda: clock["ns"],
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0),
            stopper=stopper,
            retry_gate=retry_gate,
            state_query=query,
        )
        runtime.recording_state = SessionRecordingState(runtime, anchors)
        return runtime

    return build


def _tracewrap(name, flow):
    async def wrapped(context):
        try:
            return await flow(context)
        except BaseException:
            import traceback
            print(f"FLOW {name} TRACEBACK:", flush=True)
            traceback.print_exc()
            raise
    return wrapped


def _run_session(deps, cfg, factory, *, trace=False):
    scheduling = capture_flow(factory)
    residual = residual_flow(factory)
    if trace:
        scheduling = _tracewrap("scheduling", scheduling)
        residual = _tracewrap("residual", residual)
    return asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": scheduling,
            "residual": residual,
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _scalar(db_path: Path, sql: str, *params):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, params).fetchone()


async def _await_query(
        db_path: Path, sql: str, expected, timeout_s: float = 20.0) -> None:
    """异步轮询查询结果；等待期间让出控制权供会话任务推进。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(sql).fetchone()
        if row == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待 {sql} = {expected} 超时，当前: {_scalar(db_path, sql)}")


async def _exhaust_original_stop(db_path: Path, clock: dict) -> None:
    """推进 A 的录像到三次停止失败耗尽：动作失败终态、活动占用保留。"""
    await _await_query(
        db_path,
        "SELECT started_at IS NOT NULL FROM device_activities"
        " WHERE id = 1", (1,))
    clock["ns"] += 6_000_000_001
    await _await_query(
        db_path,
        "SELECT status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = 'stop/1'", (2, 1))
    for expected in (2, 3):
        clock["ns"] += 3_000_000_000
        await _await_query(
            db_path,
            "SELECT attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'stop/1'", (expected,))
    await _await_query(
        db_path,
        "SELECT status FROM operation_runs"
        " WHERE responsibility_key = 'stop/1'", (6,))
    await _await_query(db_path, "SELECT status FROM actions WHERE id = 1", (4,))
    assert _scalar(
        db_path, "SELECT activity_state, occupancy_state"
        " FROM device_activities WHERE id = 1") == (2, 1)


class TestResidualWinddown:
    async def test_winddown_stops_recording_releases_and_trigger_proceeds(
            self, tmp_path: Path) -> None:
        home = tmp_path / "winddown"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", due),
                  _photo_action("followup", due)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=3)
        query = _QueryDouble((1,), paused=True)
        clock = {"ns": time.monotonic_ns()}
        files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock, files))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (2,))
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            await _exhaust_original_stop(db, clock)
            query.release.set()
            # 触发动作到期先执行前检查：观察到残留活动仍在录制，建立
            # 独立收场流程并按预算停止；第 4 次停止调用（收场第 1 次）
            # 可靠确认后流程成功，活动按该停止事实收场并释放占用。
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 1))
            assert query.calls == ["query"]
            assert len(stopper.calls) == 4
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2))
            # 残留解除后触发动作放行：照片正常执行并登记产物。
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (3,))
            assert _scalar(
                db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 2"
            ) == (1,)
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_trigger_cancelled_before_winddown_keeps_residual(
            self, tmp_path: Path) -> None:
        home = tmp_path / "cancel-before"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", _future_schedule(2)),
                  _photo_action("followup", _future_schedule(3))))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=3)
        query = _QueryDouble(("error",))
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _exhaust_original_stop(db, clock)
            # 触发动作到期执行前检查失败：不建立收场流程，动作保持
            # 待执行并按间隔重查。
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (2, 1))
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (0,)
        finally:
            await _cancel_task(task)
        close_runtime(deps)
        # 触发动作在收场流程发出停止前取消：不再开始收场，其执行前
        # 检查责任随取消结束，残留事实与占用原样保留。
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(), _cancel_plan("2", 2))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (6,))
            await _await_query(
                db,
                "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (5,))
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (0,)
            assert len(stopper.calls) == 3
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (2, 1)
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_cancelled_trigger_after_first_stop_finishes_winddown(
            self, tmp_path: Path) -> None:
        home = tmp_path / "cancel-after"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", _future_schedule(2)),
                  _photo_action("followup", _future_schedule(3))))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        # 第 4 次（收场首停）仍失败，触发动作取消后第 5 次成功。
        stopper = _StopDouble(failures=4)
        query = _QueryDouble((1,))
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _exhaust_original_stop(db, clock)
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (2, 1))
        finally:
            await _cancel_task(task)
        close_runtime(deps)
        # 触发动作在首停发出后取消：收场按已保存意图继续使用剩余次
        # 数处理残留录像。
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(), _cancel_plan("2", 2))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock),
            trace=True)
        try:
            await asyncio.sleep(2.0)
            print("DIAG done:", task.done(), flush=True)
            if task.done() and not task.cancelled():
                print("DIAG exc:", task.exception(), flush=True)
                import traceback
                print("DIAG result:", task.result(), flush=True)
                # 回放第二阶段会话并打印流程异常栈：复刻门推进路径。
                try:
                    from camctl.capture.residual import pass_residual_gate
                    runtime2 = _winddown_factory(
                        driver, stopper, query, clock, None)(owned2, "cam-1")                         if False else None
                except Exception:
                    traceback.print_exc()
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (6,))
            clock["ns"] += 3_000_000_000
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 2))
            assert len(stopper.calls) == 5
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2))
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_winddown_budget_exhausted_keeps_residual_and_trigger_expires(
            self, tmp_path: Path) -> None:
        home = tmp_path / "exhausted"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", due),
                  _photo_action("followup", due,
                                max_delay_ms=8000)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=99)
        query = _QueryDouble((1,), paused=True)
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (2,))
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            await _exhaust_original_stop(db, clock)
            query.release.set()
            # 收场预算（3 次）与原停止预算分别计数，全部失败后流程按
            # recording_stop_failed 失败终态化，残留事实保留。
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (2, 1))
            for expected in (2, 3):
                clock["ns"] += 3_000_000_000
                await _await_query(
                    db,
                    "SELECT attempts_used FROM operation_runs"
                    " WHERE responsibility_key = 'followup/2/1'",
                    (expected,))
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (4, 3))
            assert _scalar(
                db, "SELECT error_json ->> '$.code' FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'"
            ) == ("recording_stop_failed",)
            assert len(stopper.calls) == 6
            # 收场失败后触发动作不再开始：启动窗口耗尽后按过期终态
            # 收尾，设备活动执行事实与占用原样保留。
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (5,))
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (2, 1)
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_restart_resumes_winddown_with_cumulative_budget(
            self, tmp_path: Path) -> None:
        home = tmp_path / "restart"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 窗口放大：重启与收场推进期间触发动作保持有效，过期收尾由
        # 专门用例验证。
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", _future_schedule(2)),
                  _photo_action("followup", _future_schedule(3),
                                max_delay_ms=90000)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=5)
        query = _QueryDouble((1,))
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _exhaust_original_stop(db, clock)
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (2, 1))
        finally:
            await _cancel_task(task)
        close_runtime(deps)
        # 重启后沿原收场流程继续：累计次数不重置，同一流程用剩余两
        # 次完成停止（第 5 次失败、第 6 次确认），触发动作随后放行。
        files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        task = _run_session(
            deps, cfg,
            _winddown_factory(driver, stopper, query, clock, files))
        try:
            # 重启后的重试等待按本次间隔从本会话首次观察重新计时：先
            # 让会话推进一轮登记锚点，再推进单调钟触发剩余次数。
            await asyncio.sleep(0.5)
            clock["ns"] += 3_000_000_000
            await _await_query(
                db,
                "SELECT attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (2,))
            clock["ns"] += 3_000_000_000
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 3))
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (1,)
            assert len(stopper.calls) == 6
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2))
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,))
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_preflight_idle_confirms_and_trigger_proceeds_without_stop(
            self, tmp_path: Path) -> None:
        home = tmp_path / "idle"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", _future_schedule(2)),
                  _photo_action("followup", _future_schedule(3))))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=3)
        query = _QueryDouble(("idle",))
        clock = {"ns": time.monotonic_ns()}
        files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock, files))
        try:
            await _exhaust_original_stop(db, clock)
            # 执行前检查可靠确认设备空闲（设备已自行停止）：保存观察
            # 后触发动作直接开始，不建立收场流程，也不发送停止命令；
            # 残留活动的陈旧执行事实与占用原样保留。
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,))
            assert query.calls == ["query"]
            assert len(stopper.calls) == 3
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (0,)
            assert _scalar(
                db, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'") == (3,)
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (2, 1)
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_preflight_observing_other_activity_waits(
            self, tmp_path: Path) -> None:
        home = tmp_path / "other-activity"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", due),
                  _photo_action("followup", due, max_delay_ms=8000)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        stopper = _StopDouble(failures=3)
        # 观察到的活动身份无法对应残留候选：属于其他有效动作的占用
        # 或未知状态，等待而不授权停止。
        query = _QueryDouble((99,), paused=True)
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (2,))
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            await _exhaust_original_stop(db, clock)
            query.release.set()
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (2, 1, 1))
            for expected in (2, 3):
                clock["ns"] += 3_000_000_000
                await _await_query(
                    db,
                    "SELECT attempts_used FROM operation_runs"
                    " WHERE responsibility_key = 'query/preflight/2'",
                    (expected,))
            # 检查预算（3 次）耗尽：不推测空闲也不停止，触发动作保持
            # RUNNING 且没有启动尝试，不建立收场流程。
            assert len(query.calls) == 3
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (0,)
            assert _scalar(
                db, "SELECT status FROM actions WHERE id = 2") == (2,)
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            assert len(stopper.calls) == 3
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (5,))
            await _await_query(
                db, "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'query/preflight/2'",
                (6, 3, 0))
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_expired_trigger_keeps_original_winddown_for_next_trigger(
            self, tmp_path: Path) -> None:
        home = tmp_path / "shared-winddown"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1", _record_action("residue", due),
                  _photo_action("first", due, max_delay_ms=2000),
                  _photo_action("next", _future_schedule(6))))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble(photo_activity_id=3)
        stopper = _StopDouble(failures=3, unconfirmed=1)
        query = _QueryDouble((1, 1, "idle"), paused=True)
        clock = {"ns": time.monotonic_ns()}
        files = {3: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock, files))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (2,))
            await _exhaust_original_stop(db, clock)
            query.release.set()
            await _await_query(
                db, "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'query/residual/2/1'", (2, 1))
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (5,))
            await _await_query(
                db, "SELECT first_window_observed_at IS NOT NULL"
                " FROM actions WHERE id = 3", (1,))
            # 后续候选不取得第二套收场或查询预算。原停止已经发出，
            # 触发者过期不结束尚需核实的原确认查询。
            assert _scalar(db, "SELECT COUNT(*) FROM operation_runs WHERE kind = 8") == (1,)
            assert _scalar(db, "SELECT status, attempts_used FROM operation_runs"
                           " WHERE responsibility_key = 'query/residual/2/1'") == (2, 1)
            assert _scalar(db, "SELECT COUNT(*) FROM operation_runs"
                           " WHERE responsibility_key = 'query/residual/3/1'") == (0,)
            assert len(stopper.calls) == 4
            clock["ns"] += 3_000_000_000
            await _await_query(
                db, "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 1))
            await _await_query(
                db, "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'query/residual/2/1'", (3, 2))
            await _await_query(db, "SELECT status FROM actions WHERE id = 3", (3,))
            assert query.calls == ["query", "query", "query"]
            assert len(stopper.calls) == 4
            assert _scalar(db, "SELECT status, attempts_used FROM operation_runs"
                           " WHERE responsibility_key = 'stop/1'") == (6, 3)
        finally:
            await _cancel_task(task)
        close_runtime(deps)

    async def test_unconfirmed_stop_confirmed_by_query_releases(
            self, tmp_path: Path) -> None:
        home = tmp_path / "confirm-by-query"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", _future_schedule(2)),
                  _photo_action("followup", _future_schedule(3))))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        driver = _ControlDouble()
        # 收场首停发出但未确认：先由确认查询核实设备已空闲，按停止
        # 事实收场，不再重复停止。
        stopper = _StopDouble(failures=3, unconfirmed=1)
        query = _QueryDouble((1, "idle"))
        clock = {"ns": time.monotonic_ns()}
        files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock, files))
        try:
            await _exhaust_original_stop(db, clock)
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 1))
            assert len(stopper.calls) == 4
            assert query.calls == ["query", "query"]
            assert _scalar(
                db, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'query/residual/2/1'") == (3,)
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2))
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,))
        finally:
            await _cancel_task(task)
        close_runtime(deps)
