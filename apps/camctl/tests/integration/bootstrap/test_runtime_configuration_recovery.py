"""运行配置变化的两次会话组合验收。

同一真实状态库先 submit 受理两台设备的计划，第一运行保存照片核
实重试等待与延时等待安排后可靠停止；修改按设备的核实预算和额外
等待后第二运行采用新配置完成全部责任。报告维护以真实冻结、生成
与重建收尾：公共 Schema 校验、两动作投影与字节一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
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
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportingRepository,
    register_report_guards,
    register_sync_guard,
)
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_capture_contract import ResultsDouble, _entry
from .test_recording_stop import (
    _await_query,
    _cancel,
    _cancel_plan,
    _future_schedule,
    _scalar,
    _StopDouble,
    _submit_plan,
)
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.validators import ValidatedEvent
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.transaction import row_facts
from .test_timelapse_finish import _TimelapseCatalog, _timelapse_plan

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()

pytestmark = pytest.mark.asyncio

_EVIDENCE = EvidenceRegistry((
    EvidenceContract(type="operation_returned", version=1, operation="control",
                     fields=frozenset()),
    EvidenceContract(type="photo_taken", version=1, operation="control",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="timelapse_sent", version=1, operation="control",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="results_returned", version=1, operation="result",
                     fields=frozenset()),
    EvidenceContract(type="stop_returned", version=1, operation="stop",
                     fields=frozenset()),
    EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    RESULT_FILES_CONTRACT,
    RESULT_PAGE_CONTRACT,
))


class _TwoDeviceCatalog(_TimelapseCatalog):
    """受理目录替身：两台设备都提供照片与发送后等待的延时任务。"""

    def action_types(self):
        return frozenset({"camera_take_photo", "camera_timelapse"})

    def device_exists(self, device_id):
        return device_id in ("cam-1", "cam-2")

    def driver_id(self, device_id):
        return "camctl-adb" if self.device_exists(device_id) else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type in {
            "camera_take_photo", "camera_timelapse"}

    def parameter_definition(self, device_id, action_type, parameter_type):
        if action_type == "camera_timelapse":
            return super().parameter_definition(device_id, action_type, parameter_type)
        if (self.device_exists(device_id) and action_type == "camera_take_photo"
                and parameter_type == "photo"):
            return ParameterDefinition(
                schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"type": {"const": "photo"}},
                    "required": ["type"], "additionalProperties": False,
                }, defaults={})
        return None


class _TwoDeviceDriver:
    """契约替身：按设备身份返回各自活动的观察，记录实际调用。"""

    _OBSERVATIONS = {
        "take_photo": "photo_taken",
        "start_timelapse": "timelapse_sent",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def control(self, request):
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        self.calls.append((request.binding.device_id, request.operation))
        identity = "1" if request.binding.device_id == "cam-1" else "2"
        return DeviceCallResult(
            observations=(DeviceObservation(
                type=self._OBSERVATIONS[request.operation], version=1,
                data={"activity_id": identity}),),
            error=None,
        )


def _paths_and_devices(home: Path):
    """同一部署路径下的两台相机设备声明；两次运行共享此路径。"""
    devices = {
        device: {"kind": "camera", "driver": "camctl-adb"}
        for device in ("cam-1", "cam-2")
    }
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "devices": devices,
        },
        ConfigDefaults(),
    )


def _two_device_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "photo",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "photo"},
                "policy": {"max_delay_ms": 30000},
            },
            {
                "name": "timelapse",
                "type": "camera_timelapse",
                "device_id": "cam-2",
                "scheduled_at": scheduled_at,
                "params": {"type": "timelapse"},
                "policy": {"max_delay_ms": 30000},
            },
        ],
    }


def _factory(driver, results, *, photo_check, extra_ms, wait_ms=2000):
    """每设备运行时工厂：照片核实预算限 cam-1，额外等待限 cam-2。"""
    retry_gate = RetryWaitGate()

    def build(owned, device_id: str) -> CaptureRuntime:
        overrides = {}
        if device_id == "cam-1":
            overrides["check_config"] = photo_check
        with closing(owned.connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM history_events")) as cursor:
            horizon = int(cursor.fetchone()[0])
        overrides["recovery_boundary"] = RecoveryBoundary.HOST_LOCAL_SETTLED
        overrides["recovery_max_event_id"] = horizon
        overrides["recovery_evidence_for"] = (
            lambda binding, operation: EvidenceRegistry((
                EvidenceContract(
                    "adb_foreground_recovery", 1, operation, frozenset()),)))
        return CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=results,
            evidence=_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=time.monotonic_ns,
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=wait_ms, driver_margin_ms=0,
                extra_wait_ms=extra_ms if device_id == "cam-2" else 0),
            retry_gate=retry_gate,
            **overrides,
        )

    return build


def _run_session(deps, driver, results, *, photo_check, extra_ms):
    return asyncio.create_task(execute_command(
        deps, None,
        flows={"scheduling": capture_flow(
            _factory(driver, results, photo_check=photo_check, extra_ms=extra_ms))},
        poll_interval_s=0.1,
    ))


class TestTwoSessionConfigurationRecovery:
    async def test_second_run_uses_changed_budget_and_extra_wait(
            self, tmp_path: Path) -> None:
        home = tmp_path / "recovery"
        home.mkdir()
        cfg = _paths_and_devices(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TwoDeviceCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _two_device_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)

        photo_file = replace(_entry("img-1", kind=FileKind.PHOTO),
                             original_name="img-1.jpg", media_type="image/jpeg")
        # 集合未 finalized：第一运行两台设备都只保存未完成责任。
        results = ResultsDouble(
            {1: (photo_file,), 2: (_entry("sequence-1"),)}, set_finalized=False)
        driver = _TwoDeviceDriver()
        # 第一运行：照片预算两项、间隔拉长保证观察窗口内只有一轮；
        # cam-2 额外等待为零。
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(2, Decimal("10"), Decimal("30")),
            extra_ms=0)
        try:
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'",
                (2, 1, 1))
            await _await_query(
                db,
                "SELECT extra_wait_ms_used, expected_check_at IS NOT NULL"
                " FROM device_activities WHERE action_id = 2", (0, 1))
            assert driver.calls == [("cam-1", "take_photo"),
                                    ("cam-2", "start_timelapse")]
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 修改按设备配置：照片核实预算上调且间隔缩短，cam-2 额外等待
        # 增至三秒。第二运行采用新配置完成全部责任。
        results.set_finalized = True
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(5, Decimal("10"), Decimal("0.2")),
            extra_ms=3000)
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (3,))
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 照片按新上限补足剩余名额：恰好第二轮成功，原尝试保持。
        assert _scalar(
            db,
            "SELECT status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'results/1'") == (3, 2)
        # 延时等待按新额外等待从原发送锚点重算：采用值与预计检查精确。
        sent_at, extra_used, check_at = _scalar(
            db,
            "SELECT sent_at, extra_wait_ms_used, expected_check_at"
            " FROM device_activities WHERE action_id = 2")
        assert extra_used == 3000
        assert check_at == sent_at + (2000 + 3000) * 1000
        assert _scalar(
            db,
            "SELECT status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'results/2'") == (3, 1)
        # 两台设备各登记一份产物，占用都已释放。
        assert _scalar(
            db, "SELECT COUNT(*), COUNT(DISTINCT source_action_id)"
            " FROM outputs") == (2, 2)
        assert _scalar(
            db, "SELECT COUNT(*) FROM device_activities"
            " WHERE occupancy_state = 2") == (2,)
        assert _scalar(db, "SELECT status FROM plans WHERE id = 1") == (3,)

        # 报告维护：冻结后生成公共 Schema 合法的报告，两动作投影为成
        # 功；分批重建字节一致。
        owned = open_existing(db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            frozen = ReportingRepository().freeze_report(
                new_operation_key(), owned, occurred_at=1)
            assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
            report = frozen.value.report
        finally:
            owned.connection.close()
        spec = GenerationSpec(
            db_path=db, report_id=report.report_id,
            from_wm=report.from_wm, to_wm=report.to_wm,
            frozen_event_id=report.boundary.last_event_id,
            staging_path=home / "first.json")
        original = generate_report_file(spec).path.read_bytes()
        document = json.loads(original)
        validate_document("protocol/status-report.schema.json", document)
        statuses = {
            action["action_instance_id"]: action["status"]
            for plan in document["plans"] for action in plan["actions"]}
        assert statuses == {"1": "succeeded", "2": "succeeded"}
        rebuilt = generate_report_file(GenerationSpec(
            db_path=db, report_id=report.report_id,
            from_wm=report.from_wm, to_wm=report.to_wm,
            frozen_event_id=report.boundary.last_event_id,
            staging_path=home / "second.json",
            entity_batch_size=1, event_batch_size=1))
        assert rebuilt.path.read_bytes() == original
        assert hashlib.sha256(original).hexdigest() == generate_report_file(
            GenerationSpec(
                db_path=db, report_id=report.report_id,
                from_wm=report.from_wm, to_wm=report.to_wm,
                frozen_event_id=report.boundary.last_event_id,
                staging_path=home / "third.json")
        ).sha256


class TestTwoSessionCancelUnderChangedStopBudget:
    async def test_second_run_cancels_with_current_stop_configuration(
            self, tmp_path: Path) -> None:
        home = tmp_path / "cancel-recovery"
        home.mkdir()
        cfg = _paths_and_devices(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TimelapseCatalog(stop_supported=True, duration_s=Decimal("600"))
        await _submit_plan(
            tmp_path, cfg, catalog, _timelapse_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        # 已拍完文件在设备上：取消收尾的核实把它登记为正式产物。
        results = ResultsDouble({1: (_entry("sequence-1"),)})
        driver = _TwoDeviceDriver()
        class _FailingThenConfirmingStop(_StopDouble):
            """第一次返回带阶段的调用错误，之后确认停止。"""

            async def stop(self, request):
                if len(self.calls) == 0:
                    self.calls.append(request.operation)
                    from camctl.devices.ports import DeviceCallResult
                    return DeviceCallResult(
                        observations=(),
                        error={"code": "stop_timeout", "stage": "transport"})
                return await super().stop(request)

        stopper = _FailingThenConfirmingStop()

        def build_factory(stop_config):
            retry_gate = RetryWaitGate()

            def build(owned, device_id: str) -> CaptureRuntime:
                with closing(owned.connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM history_events")) as cursor:
                    horizon = int(cursor.fetchone()[0])
                return CaptureRuntime(
                    owned=owned,
                    scheduling=SchedulingRepository(),
                    operations=OperationRepository(),
                    capture=CaptureRepository(),
                    timelapse=TimelapseRepository(),
                    driver=driver,
                    results=results,
                    evidence=_EVIDENCE,
                    stopper=stopper,
                    stop_config=stop_config,
                    wall_us=lambda: int(time.time() * 1_000_000),
                    monotonic_ns=time.monotonic_ns,
                    window_of=lambda action: LaunchWindow(
                        scheduled_at=action["scheduled_at"],
                        window_end=action["scheduled_at"]
                        + action["max_delay_ms"] * 1000),
                    wait_config=lambda params: CaptureWaitConfig(
                        target_duration_ms=600_000, driver_margin_ms=0),
                    retry_gate=retry_gate,
                    recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
                    recovery_max_event_id=horizon,
                    recovery_evidence_for=lambda binding, operation: EvidenceRegistry((
                        EvidenceContract(
                            "adb_foreground_recovery", 1, operation, frozenset()),)),
                )

            return build

        # 第一运行：单次停止预算与长间隔；只推进到启动与等待安排。
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = asyncio.create_task(execute_command(
            deps, None,
            flows={"scheduling": capture_flow(build_factory(
                AttemptConfig(1, Decimal("10"), Decimal("30"))))},
            poll_interval_s=0.1))
        try:
            await _await_query(
                db,
                "SELECT extra_wait_ms_used, expected_check_at IS NOT NULL"
                " FROM device_activities WHERE action_id = 1", (0, 1))
            assert driver.calls == [("cam-1", "start_timelapse")]
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 取消排期在发送之后、预计检查时间之前；停止预算改为三项
        # 且间隔缩短，第一次停止错误的等待不再阻塞第二次确认。
        await _submit_plan(
            tmp_path, cfg, catalog, _cancel_plan("2", 1, _future_schedule(1)))
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = asyncio.create_task(execute_command(
            deps, None,
            flows={
                "scheduling": capture_flow(build_factory(
                    AttemptConfig(3, Decimal("10"), Decimal("0.2")))),
                "cancel": cancel_flow(
                    ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing)),
            },
            poll_interval_s=0.1))
        try:
            await _await_query(
                db, "SELECT status, cancel_requested FROM actions WHERE id = 1",
                (6, 1))
            await _await_query(
                db, "SELECT status FROM actions WHERE name = 'cancel'", (3,))
        finally:
            await _cancel(task)
        close_runtime(deps)
        # 本次停止预算实际生效：第一次调用错误后重试，第二次确认。
        assert stopper.calls == ["stop_timelapse", "stop_timelapse"]
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (1,)
        assert _scalar(
            db,
            "SELECT activity_state, occupancy_state, result_set_state"
            " FROM device_activities WHERE id = 1") == (3, 2, 1)
        with sqlite3.connect(db) as connection:
            plan_statuses = tuple(
                row[0] for row in connection.execute(
                    "SELECT status FROM plans ORDER BY id"))
        assert plan_statuses == (3, 3)


class TestInterruptedSecondCallRecovery:
    async def test_interrupted_result_call_resumes_without_duplicate_side_effects(
            self, tmp_path: Path) -> None:
        home = tmp_path / "interrupted"
        home.mkdir()
        cfg = _paths_and_devices(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TwoDeviceCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog,
            {
                "request_id": "1", "created_at": "2026-01-15 08:00:00",
                "name": "plan-1",
                "actions": [{
                    "name": "photo", "type": "camera_take_photo",
                    "device_id": "cam-1",
                    "scheduled_at": _future_schedule(2),
                    "params": {"type": "photo"},
                    "policy": {"max_delay_ms": 30000},
                }],
            })
        db = Path(cfg.paths.state_db)
        photo_file = replace(_entry("img-1", kind=FileKind.PHOTO),
                             original_name="img-1.jpg", media_type="image/jpeg")

        # 第二次列举调用阻塞在设备端口：同步点在开始调用后、结果保
        # 存前。中断由会话取消注入，进程内持有的返回被丢弃。
        blocked = asyncio.Event()

        class _BlockingResults(ResultsDouble):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self._blocked_once = False

            async def list_files(self, action_id: int) -> tuple:
                if len(self.calls) == 1 and not self._blocked_once:
                    self._blocked_once = True
                    blocked.set()
                    await asyncio.Event().wait()
                return await super().list_files(action_id)

        results = _BlockingResults({1: (photo_file,)}, set_finalized=False)
        driver = _TwoDeviceDriver()

        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(2, Decimal("10"), Decimal("30")),
            extra_ms=0)
        try:
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'",
                (2, 1, 1))
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 第二运行采用新配置，在第二次列举调用处中断。
        results.set_finalized = True
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(5, Decimal("10"), Decimal("0.2")),
            extra_ms=0)
        try:
            await asyncio.wait_for(blocked.wait(), timeout=15)
            await _await_query(
                db,
                "SELECT attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'results/1'", (2,))
        finally:
            await _cancel(task)
        close_runtime(deps)
        # 第二次调用阻塞在设备端口，尚未登记：中断点恰在开始调用后。
        assert results.calls == [1]
        run_id = _scalar(
            db, "SELECT id FROM operation_runs"
            " WHERE responsibility_key = 'results/1'")[0]

        # 第三运行恢复：在途尝试按原责任收场，不重复既有名额；随后
        # 以恰好一次新调用完成核实与业务收场。
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(5, Decimal("10"), Decimal("0.2")),
            extra_ms=0)
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
        finally:
            await _cancel(task)
        close_runtime(deps)
        assert _scalar(
            db,
            "SELECT status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'results/1'") == (3, 3)
        assert results.calls == [1, 1]
        assert _scalar(
            db, "SELECT COUNT(*), COUNT(DISTINCT attempt_no)"
            " FROM operation_attempts WHERE run_id = ?", run_id) == (3, 3)
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (1,)
        assert _scalar(
            db, "SELECT occupancy_state FROM device_activities WHERE id = 1"
            ) == (2,)


def _current_rows(owned) -> dict:
    """读取四张业务表的当前投影行，作为恢复对照的独立预期。"""
    return {
        table: {int(row["id"]): dict(row) for row in map(
            lambda raw: row_facts(owned.connection, table, int(raw[0])),
            owned.connection.execute(f"SELECT id FROM {table}"))}
        for table in ("actions", "device_activities",
                      "operation_runs", "operation_attempts")}


class TestConfigurationChangeHistoryRecovery:
    async def test_freezes_before_and_after_change_recover_identically(
            self, tmp_path: Path) -> None:
        home = tmp_path / "history-recovery"
        home.mkdir()
        cfg = _paths_and_devices(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        catalog = _TwoDeviceCatalog()
        await _submit_plan(
            tmp_path, cfg, catalog, _two_device_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        photo_file = replace(_entry("img-1", kind=FileKind.PHOTO),
                             original_name="img-1.jpg", media_type="image/jpeg")
        results = ResultsDouble(
            {1: (photo_file,), 2: (_entry("sequence-1"),)}, set_finalized=False)
        driver = _TwoDeviceDriver()

        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(2, Decimal("10"), Decimal("30")),
            extra_ms=0)
        try:
            await _await_query(
                db,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'",
                (2, 1, 1))
            await _await_query(
                db,
                "SELECT extra_wait_ms_used FROM device_activities"
                " WHERE action_id = 2", (0,))
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 配置更新前冻结 H1；独立预期直接读取当时的当前投影业务列，
        # 并把动作对象状态固定为快照供配置更新后正向恢复使用。
        owned = open_existing(db, DbOpenMode.EXISTING_RW, DbConfig())
        executor = DbExecutor(
            lambda: open_existing(db, DbOpenMode.EXISTING_RW, DbConfig()),
            capacity=2, enqueue_timeout_seconds=9)
        store = SqliteSnapshotStore(db, executor)
        try:
            history = HistoryRepository(db)
            first_boundary = history.current_boundary()

            def _table_rows() -> dict:
                return {
                    table: {int(row["id"]): dict(row) for row in map(
                        lambda raw: row_facts(owned.connection, table, int(raw[0])),
                        owned.connection.execute(f"SELECT id FROM {table}"))}
                    for table in ("actions", "device_activities",
                                  "operation_runs", "operation_attempts")}

            expected_h1 = _current_rows(owned)
            h1_images = dict(zip(
                (1, 2), await store.load_entity_images(
                    (SnapshotRef(1, 1), SnapshotRef(1, 2)))))
            await store.save_snapshots(tuple(
                prepare_snapshot(image) for image in h1_images.values()))
            frozen1 = ReportingRepository().freeze_report(
                new_operation_key(), owned, occurred_at=1)
            assert frozen1.kind is DbOutcomeKind.COMPLETED, frozen1.error
            report1 = frozen1.value.report
        finally:
            owned.connection.close()
        spec1 = GenerationSpec(
            db_path=db, report_id=report1.report_id,
            from_wm=report1.from_wm, to_wm=report1.to_wm,
            frozen_event_id=report1.boundary.last_event_id,
            staging_path=home / "before.json")
        before_bytes = generate_report_file(spec1).path.read_bytes()

        # 配置更新后：照片预算上调，延时额外等待增至三秒且结果集合
        # 明确缺必需类别（已知失败）。
        results.set_finalized = True
        results.files_by_action[2] = ()
        deps = build_runtime(CommandMode.RUN, cfg, catalog=catalog)
        task = _run_session(
            deps, driver, results,
            photo_check=AttemptConfig(5, Decimal("10"), Decimal("0.2")),
            extra_ms=3000)
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
            await _await_query(db, "SELECT status FROM actions WHERE id = 2", (4,))
        finally:
            await _cancel(task)
        close_runtime(deps)

        owned = open_existing(db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            history = HistoryRepository(db)
            end_boundary = history.current_boundary()
            expected_h2 = _current_rows(owned)
            frozen2 = ReportingRepository().freeze_report(
                new_operation_key(), owned, occurred_at=2)
            assert frozen2.kind is DbOutcomeKind.COMPLETED, frozen2.error
            report2 = frozen2.value.report

            _META = {"id", "created_event_id", "last_event_id", "change_count"}

            def _business(values: dict) -> dict:
                return {name: value for name, value in values.items()
                        if name not in _META}

            def _events(entity_id: int):
                rows = owned.connection.execute(
                    "SELECT h.id, h.transaction_id, h.event_type, h.event_version,"
                    " h.occurred_at, h.clock_status, h.change_seq, h.body_json"
                    " FROM history_events h"
                    " JOIN entity_event_links l ON l.event_id=h.id"
                    " WHERE l.entity_type=1 AND l.entity_id=? ORDER BY h.id",
                    (entity_id,)).fetchall()
                reference = (1, entity_id)
                events = []
                for raw in rows:
                    envelope = decode_event_row(raw)
                    events.append(ValidatedEvent(
                        envelope=envelope,
                        event_name=event_type_name(envelope.event_type),
                        branch_name=branch_of(envelope.event_type,
                                              envelope.reason)[0],
                        references=(reference,),
                        row_owners={(row.table, row.row_id): reference
                                    for row in envelope.rows},
                    ))
                return events

            _TABLES = ("actions", "device_activities", "operation_runs",
                       "operation_attempts")
            action_events = {aid: _events(aid) for aid in (1, 2)}

            def _projected(seed, entity_id: int, boundary) -> dict:
                image = restore(seed, action_events[entity_id], boundary)
                return {key: _business(values) for key, values in image.rows.items()
                        if key[0] in _TABLES}

            def _expected_rows(expected: dict, keys) -> dict:
                return {(table, row_id): _business(expected[table][row_id])
                        for table, row_id in keys}

            for aid in (1, 2):
                empty = RestoreSeed(
                    EntityImage(1, aid, False, {}, 0, 0), INITIAL_BOUNDARY)
                # 配置更新前后两个边界上的初始回放都对照独立预期。
                for boundary, expected in (
                        (first_boundary, expected_h1), (end_boundary, expected_h2)):
                    projected = _projected(empty, aid, boundary)
                    assert projected == _expected_rows(
                        expected, projected.keys()), (aid, boundary.last_event_id)
                # 快照正向：H1 快照种子回放到 H2 与 H2 初始回放一致。
                snapshot_seed = RestoreSeed(
                    EntityImage(1, aid, True,
                                {(row.table, row.row_id): dict(row.values)
                                 for row in h1_images[aid].rows},
                                first_boundary.last_event_id,
                                h1_images[aid].change_count),
                    first_boundary)
                assert (_projected(snapshot_seed, aid, end_boundary)
                        == _projected(empty, aid, end_boundary))
                # 当前投影逆向：H2 业务列种子回退到 H1 与 H1 初始回放一致。
                counted = int(owned.connection.execute(
                    "SELECT MAX(change_count) FROM entity_event_links"
                    " WHERE entity_type=1 AND entity_id=?",
                    (aid,)).fetchone()[0])
                h2_projection = _projected(empty, aid, end_boundary)
                reverse_seed = RestoreSeed(
                    EntityImage(1, aid, True,
                                {key: _business(expected_h2[key[0]][key[1]])
                                 for key in h2_projection},
                                end_boundary.last_event_id, counted),
                    end_boundary)
                assert (_projected(reverse_seed, aid, first_boundary)
                        == _projected(empty, aid, first_boundary))

            # 旧报告在新配置与终态事实之后按原冻结依据重建，字节一致。
            rebuilt = generate_report_file(GenerationSpec(
                db_path=db, report_id=report1.report_id,
                from_wm=report1.from_wm, to_wm=report1.to_wm,
                frozen_event_id=report1.boundary.last_event_id,
                staging_path=home / "before-rebuilt.json",
                entity_batch_size=1, event_batch_size=1))
            assert rebuilt.path.read_bytes() == before_bytes
        finally:
            await executor.close()
            owned.connection.close()

        # 当前报告表达实际逐项结果与原绑定事实，不新增内部过程字段。
        spec2 = GenerationSpec(
            db_path=db, report_id=report2.report_id,
            from_wm=report2.from_wm, to_wm=report2.to_wm,
            frozen_event_id=report2.boundary.last_event_id,
            staging_path=home / "after.json")
        after_bytes = generate_report_file(spec2).path.read_bytes()
        document = json.loads(after_bytes)
        validate_document("protocol/status-report.schema.json", document)
        actions = {action["action_instance_id"]: action
                   for plan in document["plans"] for action in plan["actions"]}
        assert set(actions) == {"1", "2"}
        photo, timelapse = actions["1"], actions["2"]
        assert (photo["status"], photo["device_id"]) == ("succeeded", "cam-1")
        assert (timelapse["status"], timelapse["device_id"]) == ("failed", "cam-2")
        assert timelapse["input_params"] == {"type": "timelapse"}
        assert timelapse["error"]["code"]
        # 公共投影不含内部过程与配置字段。
        _INTERNAL = {"extra_wait_ms_used", "max_attempts", "retry_interval_s",
                     "attempt_no", "check_config", "wait_config"}
        for action in actions.values():
            assert not (_INTERNAL & set(action))
        assert "device_execution" not in photo
