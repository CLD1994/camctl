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

from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.flows import cancel_flow, capture_flow, residual_flow
from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.results import FileKind as ResultFileKind
from camctl.devices.evidence import (
    DeviceObservation, EvidenceContract, EvidenceRegistry,
)
from camctl.devices.tasks import CaptureTask
from camctl.devices.ports import DeviceCallResult
from camctl.persistence.initialization import InitOutcome, initialize_state

from ..capture.test_capture_contract import ResultsDouble, _entry
from ..support.capture import capture_assembly
from ..support.session import submit_plan, session_config, cancel_session, await_database, query_database
from camctl.persistence.directory_switch import canonical_to_path
from camctl.bootstrap.capture_assembly import execution_wait_config
from camctl.session.service import run_session
from ..support.session import session_context
from .session_clock import SessionClock


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
        RESULT_FILES_CONTRACT,
        RESULT_PAGE_CONTRACT,
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
        task_factory = None
        if action_type == "camera_record":
            if parameter_type != "video":
                return None

            def task(params):
                return CaptureTask(
                    "camera_record",
                    target_duration_s=self.duration_s,
                    stop_supported=True,
                )
            task_factory = task
        elif action_type == "camera_take_photo":
            if parameter_type != "single_shot":
                return None
        else:
            return None
        return ParameterDefinition(
            schema=_RECORD_DEFINITION if action_type == "camera_record"
            else _PHOTO_DEFINITION,
            defaults={}, task_factory=task_factory)


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
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not paused:
            self.release.set()

    async def query_state(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        self.entered.set()
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
    return session_config(home)


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
    await submit_plan(tmp_path, cfg, catalog, body)


def _winddown_factory(driver, stopper, query, clock, files=None):
    """同会话的普通与残留流程共用一个正式生产工厂。"""
    factory = None

    async def build(database, device_id):
        nonlocal factory
        if factory is None:
            factory = capture_assembly(
                driver=driver, stopper=stopper, query=query,
                results=ResultsDouble(files or {}), evidence=_EVIDENCE,
                devices={"cam-1": {"kind": "camera", "driver": "camctl-adb"}},
                staging=canonical_to_path(database.metadata.staging_path),
                wall_us=(clock.utc_micros if isinstance(clock, SessionClock)
                         else lambda: int(time.time() * 1_000_000)),
                monotonic_ns=lambda: clock["ns"], wait_config=execution_wait_config)
        return await factory(database, device_id)

    return build


def _run_session(deps, cfg, factory, *, settled_round=None, clock=None):
    scheduling = capture_flow(factory)
    residual = residual_flow(factory, owns_device=scheduling.owns_device)
    flows = {
        "scheduling": scheduling,
        "residual": residual,
        "cancel": cancel_flow(staging=Path(cfg.paths.staging),
            ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing)),
    }
    if settled_round is not None:
        async def observe_round(_context):
            # 保留原 CaptureFlow 作为会话拥有者，观察流程只等其当前任务交付。
            await scheduling.settle()
            settled_round.set()
        flows["observed_round"] = observe_round
    if clock is not None:
        context = session_context(
            cfg.paths.state_db, catalog=_WinddownCatalog(), clock=clock,
            flows=flows, local_work=CombinedLocalWork((scheduling,)),
            poll_interval_s=0.1)
        return asyncio.create_task(run_session(context, None))
    return asyncio.create_task(execute_command(
        deps, None, flows=flows, poll_interval_s=0.1))


async def _await_settled_round(event, task, *, timeout_s=20.0):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((waiter, task), timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            outcome = await task
            pytest.fail(f"原收场首轮交付前会话结束: {outcome!r}")
        assert waiter in done, "原收场首轮未在等待预算内交付"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _await_followup_result(db, attempt, *, task, timeout_s=20.0):
    await _await_query(db,
        "SELECT r.status,r.attempts_used,r.retry_wait_required,"
        " EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
        " AND t.attempt_no=r.attempts_used AND t.status=3 AND t.result_event_id IS NOT NULL)"
        " FROM operation_runs r WHERE r.responsibility_key='followup/2/1'",
        (2, attempt, 1, 1), timeout_s=timeout_s, task=task)


async def _cancel_task(task: asyncio.Task) -> None:
    await cancel_session(task)


def _scalar(db_path: Path, sql: str, *params):
    return query_database(db_path, sql, params)


async def _await_query(
        db_path: Path, sql: str, expected, timeout_s: float = 20.0, *, task=None) -> None:
    await await_database(db_path, sql, expected, timeout_s=timeout_s, task=task)


async def _exhaust_original_stop(db_path: Path, clock, *, task=None,
                                 after_second=None) -> None:
    """推进 A 的录像到三次停止失败耗尽：动作失败终态、活动占用保留。"""
    await _await_query(
        db_path,
        "SELECT started_at IS NOT NULL FROM device_activities"
        " WHERE id = 1", (1,), task=task)
    clock["ns"] += 6_000_000_001
    returned = ("EXISTS(SELECT 1 FROM operation_attempts t WHERE t.run_id=r.id"
        " AND t.attempt_no=r.attempts_used AND t.status=3 AND t.result_event_id IS NOT NULL)")
    await _await_query(db_path,
        f"SELECT r.status,r.attempts_used,{returned} FROM operation_runs r"
        " WHERE r.responsibility_key='stop/1'", (2, 1, 1), task=task)
    for expected in (2, 3):
        remaining_ns = 3_000_000_000
        if expected == 3 and after_second is not None:
            before = clock["ns"]
            await after_second()
            remaining_ns -= clock["ns"] - before
            assert remaining_ns >= 0
        clock["ns"] += remaining_ns
        await _await_query(db_path,
            f"SELECT r.attempts_used,{returned} FROM operation_runs r"
            " WHERE r.responsibility_key='stop/1'", (expected, 1), task=task)
    await _await_query(
        db_path,
        "SELECT status FROM operation_runs"
        " WHERE responsibility_key = 'stop/1'", (6,), task=task)
    await _await_query(db_path, "SELECT status FROM actions WHERE id = 1", (4,), task=task)
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
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (2,), task=task)
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            await _exhaust_original_stop(db, clock, task=task)
            query.release.set()
            # 触发动作到期先执行前检查：观察到残留活动仍在录制，建立
            # 独立收场流程并按预算停止；第 4 次停止调用（收场第 1 次）
            # 可靠确认后流程成功，活动按该停止事实收场并释放占用。
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 1), task=task)
            assert query.calls == ["query"]
            assert len(stopper.calls) == 4
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2), task=task)
            # 残留解除后触发动作放行：照片正常执行并登记产物。
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (3,), task=task)
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
            await _exhaust_original_stop(db, clock, task=task)
            # 触发动作到期执行前检查失败：不建立收场流程，动作保持
            # 待执行并按间隔重查。
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (2, 1, 1), task=task)
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
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (6,), task=task)
            await _await_query(
                db,
                "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (5,), task=task)
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
            await _exhaust_original_stop(db, clock, task=task)
            await _await_followup_result(db, 1, task=task)
        finally:
            await _cancel_task(task)
        close_runtime(deps)
        # 首停失败结果可靠保存后取消触发动作；下一会话沿原停止责任
        # 和剩余次数继续处理残留录像。
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(), _cancel_plan("2", 2))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        settled_round = asyncio.Event()
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock),
            settled_round=settled_round)
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (6,), task=task)
            deadline = time.monotonic() + 20.0
            await _await_settled_round(settled_round, task, timeout_s=20.0)
            clock["ns"] += 3_000_000_000
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 2), task=task,
                timeout_s=max(0.0, deadline - time.monotonic()))
            assert len(stopper.calls) == 5
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2), task=task)
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
        followup_due = _future_schedule(30)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", due),
                  _photo_action("followup", followup_due,
                                max_delay_ms=8000)))
        db = Path(cfg.paths.state_db)
        driver = _ControlDouble()
        stopper = _StopDouble(failures=99)
        query = _QueryDouble((1,), paused=True)
        clock = SessionClock()
        task = _run_session(
            None, cfg, _winddown_factory(driver, stopper, query, clock), clock=clock)
        try:
            clock.reach(due)
            await _exhaust_original_stop(db, clock, task=task)
            clock.reach(followup_due)
            await asyncio.wait_for(query.entered.wait(), timeout=20.0)
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            query.release.set()
            # 收场预算（3 次）与原停止预算分别计数，全部失败后流程按
            # recording_stop_failed 失败终态化，残留事实保留。
            await _await_followup_result(db, 1, task=task)
            for expected in (2, 3):
                clock["ns"] += 3_000_000_000
                if expected == 2:
                    await _await_followup_result(db, expected, task=task)
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (4, 3), task=task)
            assert _scalar(
                db, "SELECT error_json ->> '$.code' FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'"
            ) == ("recording_stop_failed",)
            assert len(stopper.calls) == 6
            # 收场失败后触发动作不再开始：启动窗口耗尽后按过期终态
            # 收尾，设备活动执行事实与占用原样保留。
            clock["ns"] += 3_000_000_000
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (5,), task=task)
            assert _scalar(
                db, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (2, 1)
        finally:
            await _cancel_task(task)

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
            await _exhaust_original_stop(db, clock, task=task)
            await _await_followup_result(db, 1, task=task)
        finally:
            await _cancel_task(task)
        close_runtime(deps)
        # 重启后沿原收场流程继续：累计次数不重置，同一流程用剩余两
        # 次完成停止（第 5 次失败、第 6 次确认），触发动作随后放行。
        files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
        settled_round = asyncio.Event()
        task = _run_session(
            deps, cfg, _winddown_factory(driver, stopper, query, clock, files),
            settled_round=settled_round)
        try:
            # 首轮公开推进完整交付后，原重试等待已观察到本次单调钟。
            deadline = time.monotonic() + 20.0
            await _await_settled_round(settled_round, task, timeout_s=20.0)
            clock["ns"] += 3_000_000_000
            # 观察与结果保存共用原20秒等待预算；意图计数不表示实际返回。
            await _await_followup_result(db, 2, task=task,
                timeout_s=max(0.0, deadline - time.monotonic()))
            clock["ns"] += 3_000_000_000
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 3), task=task)
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (1,)
            assert len(stopper.calls) == 6
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2), task=task)
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,), task=task)
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
            await _exhaust_original_stop(db, clock, task=task)
            # 执行前检查可靠确认设备空闲（设备已自行停止）：保存观察
            # 后触发动作直接开始，不建立收场流程，也不发送停止命令；
            # 残留活动的陈旧执行事实与占用原样保留。
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,), task=task)
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

    @pytest.mark.parametrize("window_ms,final_query", [(8000, 5), (12000, 6)],
                             ids=["window-expires", "budget-exhausted"])
    async def test_preflight_observing_other_activity_waits(
            self, tmp_path: Path, window_ms: int, final_query: int) -> None:
        home = tmp_path / "other-activity"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        followup_due = (datetime.strptime(due, "%Y-%m-%d %H:%M:%S")
                        + timedelta(seconds=11)).strftime("%Y-%m-%d %H:%M:%S")
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1",
                  _record_action("residue", due),
                  _photo_action("followup", followup_due, max_delay_ms=window_ms)))
        db = Path(cfg.paths.state_db)
        driver = _ControlDouble()
        stopper = _StopDouble(failures=3)
        # 观察到的活动身份无法对应残留候选：属于其他有效动作的占用
        # 或未知状态，等待而不授权停止。
        query = _QueryDouble((99,), paused=True)
        clock = SessionClock()
        task = _run_session(
            None, cfg, _winddown_factory(driver, stopper, query, clock), clock=clock)
        try:
            clock.reach(due)

            async def start_followup():
                # 原录像第二次停止失败后，后续动作在其窗口内进入运行。
                clock.reach(followup_due)
                await _await_query(
                    db, "SELECT status FROM actions WHERE id = 2", (2,), task=task)
                assert _scalar(db, "SELECT id FROM operation_runs"
                               " WHERE responsibility_key = 'start/2'") is None

            await _exhaust_original_stop(
                db, clock, task=task, after_second=start_followup)
            await asyncio.wait_for(query.entered.wait(), timeout=20.0)
            query.release.set()
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
                " WHERE responsibility_key = 'query/preflight/2'", (2, 1, 1), task=task)
            for expected in (2, 3):
                clock["ns"] += 3_000_000_000
                await _await_query(
                    db,
                    "SELECT r.attempts_used, EXISTS(SELECT 1 FROM operation_attempts t"
                    " WHERE t.run_id = r.id AND t.attempt_no = r.attempts_used"
                    " AND t.result_event_id IS NOT NULL) FROM operation_runs r"
                    " WHERE r.responsibility_key = 'query/preflight/2'",
                    (expected, 1), task=task)
            # 三次观察均指向其他活动，不授权停止或启动。
            assert len(query.calls) == 3
            assert _scalar(
                db, "SELECT COUNT(*) FROM operation_runs"
                " WHERE kind = 8") == (0,)
            assert _scalar(
                db, "SELECT status FROM actions WHERE id = 2") == (2,)
            assert _scalar(db, "SELECT id FROM operation_runs"
                           " WHERE responsibility_key = 'start/2'") is None
            assert len(stopper.calls) == 3
            # 再经过一个原重试间隔：8 秒窗口先过期；12 秒窗口仍有效，
            # 因需要新的观察但预算已耗尽而结束查询责任。
            clock.advance(3)
            if window_ms == 12000:
                await _await_query(
                    db, "SELECT status, attempts_used, retry_wait_required"
                    " FROM operation_runs WHERE responsibility_key = 'query/preflight/2'",
                    (6, 3, 0), task=task)
                assert _scalar(db, "SELECT status FROM actions WHERE id = 2") == (2,)
                clock.advance(3)
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (5,), task=task)
            await _await_query(
                db, "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'query/preflight/2'",
                (final_query, 3, 0), task=task)
        finally:
            await _cancel_task(task)

    async def test_expired_trigger_keeps_original_winddown_for_next_trigger(
            self, tmp_path: Path) -> None:
        home = tmp_path / "shared-winddown"
        home.mkdir()
        cfg = _config_for(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        due = _future_schedule(2)
        first_due = datetime.strptime(due, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=11)
        next_due = first_due + timedelta(seconds=3)
        await _submit_plan(
            tmp_path, cfg, _WinddownCatalog(),
            _plan("1", _record_action("residue", due),
                  _photo_action("first", first_due.strftime("%Y-%m-%d %H:%M:%S"),
                                max_delay_ms=2000),
                  _photo_action("next", next_due.strftime("%Y-%m-%d %H:%M:%S"))))
        db = Path(cfg.paths.state_db)
        driver = _ControlDouble(photo_activity_id=3)
        stopper = _StopDouble(failures=3, unconfirmed=1)
        query = _QueryDouble((1, 1, "idle"), paused=True)
        clock = SessionClock()
        files = {3: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
        task = _run_session(
            None, cfg, _winddown_factory(driver, stopper, query, clock, files),
            clock=clock)
        try:
            clock.reach(due)

            async def start_first():
                clock.reach(first_due.strftime("%Y-%m-%d %H:%M:%S"))
                await _await_query(
                    db, "SELECT status FROM actions WHERE id = 2", (2,), task=task)

            await _exhaust_original_stop(db, clock, task=task, after_second=start_first)
            await asyncio.wait_for(query.entered.wait(), timeout=20.0)
            query.release.set()
            await _await_query(
                db, "SELECT r.status, r.attempts_used, EXISTS(SELECT 1"
                " FROM operation_attempts t WHERE t.run_id=r.id AND t.attempt_no=1"
                " AND t.result_event_id IS NOT NULL) FROM operation_runs r"
                " WHERE r.responsibility_key = 'query/residual/2/1'", (2, 1, 1), task=task)
            clock.reach(next_due.strftime("%Y-%m-%d %H:%M:%S"))
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (5,), task=task)
            await _await_query(
                db, "SELECT first_window_observed_at IS NOT NULL"
                " FROM actions WHERE id = 3", (1,), task=task)
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
                " WHERE responsibility_key = 'followup/2/1'", (3, 1), task=task)
            await _await_query(
                db, "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'query/residual/2/1'", (3, 2), task=task)
            await _await_query(db, "SELECT status FROM actions WHERE id = 3", (3,), task=task)
            assert query.calls == ["query", "query", "query"]
            assert len(stopper.calls) == 4
            assert _scalar(db, "SELECT status, attempts_used FROM operation_runs"
                           " WHERE responsibility_key = 'stop/1'") == (6, 3)
        finally:
            await _cancel_task(task)

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
            await _exhaust_original_stop(db, clock, task=task)
            await _await_query(
                db,
                "SELECT status, attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'followup/2/1'", (3, 1), task=task)
            assert len(stopper.calls) == 4
            assert query.calls == ["query", "query"]
            assert _scalar(
                db, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'query/residual/2/1'") == (3,)
            await _await_query(
                db,
                "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1", (3, 2), task=task)
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 2", (3,), task=task)
        finally:
            await _cancel_task(task)
        close_runtime(deps)
