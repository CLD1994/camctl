"""RESULTS 验证沿公开受理和调度建立可回放的完整拍摄历史。"""

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet,
    FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction,
)
from camctl.capture.handlers import capture_handler
from camctl.capture.models import WaitCompletedSave
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.capture.processing import CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckReason
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlDriver, DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput, register_acceptance_guards
from camctl.persistence.repositories.cancellation import CancellationRepository, register_cancellation_guards
from camctl.persistence.repositories.scheduling import ObserveWindowRequest, SchedulingRepository, StartActionRequest, register_window_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_media_assembly import _config
from ..bootstrap.test_recording_stop import _RecordCatalog
from ..bootstrap.test_timelapse_finish import _TimelapseCatalog
from .test_capture_contract import _NOW, _runtime
from ..support.capture import capture_database, restart_capture_database

register_acceptance_guards()
register_cancellation_guards()
register_window_guard()

RESULT_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    *(EvidenceContract(name, 1, "control", frozenset({"activity_id"}), identity_field="activity_id")
      for name in ("photo_taken", "start_confirmed", "timelapse_sent")),
    EvidenceContract("stop_returned", 1, "stop", frozenset()),
    EvidenceContract("stop_confirmed", 1, "stop", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("results_returned", 1, "result", frozenset()),
    EvidenceContract("result_files_listed", 1, "result", frozenset({"activity_id", "entries"}), identity_field="activity_id"),
    EvidenceContract("adb_foreground_assumption", 1, "result", frozenset({"terminate_grace_s"})),
))

CASES = {
    "photo": ("camera_take_photo", "photo", "take_photo", "photo_taken"),
    "record": ("camera_record", "video", "start_recording", "start_confirmed"),
    "winddown": ("camera_record", "video", "start_recording", "start_confirmed"),
    "timelapse": ("camera_timelapse", "timelapse", "start_timelapse", "timelapse_sent"),
    "cancel": ("camera_timelapse", "timelapse", "start_timelapse", "timelapse_sent"),
}


class ResultCatalog(_RecordCatalog):
    """静态目录沿既有合法录像和延时任务定义，只补照片参数边界。"""

    duration_s = Decimal("60")

    def action_types(self):
        return frozenset({"camera_take_photo", "camera_record", "camera_timelapse", "cancel_task", "report_status"})

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type in {"camera_take_photo", "camera_record", "camera_timelapse"}

    def parameter_definition(self, device_id, action_type, parameter_type):
        if action_type == "camera_record":
            return super().parameter_definition(device_id, action_type, parameter_type)
        if action_type == "camera_timelapse":
            return _TimelapseCatalog(stop_supported=True, duration_s=Decimal("600")).parameter_definition(
                device_id, action_type, parameter_type)
        if self.device_exists(device_id) and action_type == "camera_take_photo" and parameter_type == "photo":
            return ParameterDefinition(schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object", "properties": {"type": {"const": "photo"}},
                "required": ["type"], "additionalProperties": False,
            }, defaults={})
        return None


def returned(operation, observation, activity_id):
    return CallOutcome(
        status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue(f"{operation}_returned", 1, {})),
        observations=(DeviceObservation(observation, 1, {"activity_id": str(activity_id)}),),
    )


def result_driver(case, outcome=None, *, side_effect=None):
    """在正式装配已有的结果端口提交下一次设备返回脚本。"""
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import ResultDriver

    driver = create_autospec(ResultDriver, instance=True)
    if outcome is not None:
        driver.list_results.return_value = DeviceCallResult.from_outcome(outcome)
    driver.list_results.side_effect = side_effect
    case.results.delegate = DriverResultListing(
        driver, DeviceBinding("cam-1", "camctl-adb"), case.evidence)
    return driver


async def complete_natural_wait(runtime, action_id):
    """按已由处理器安排的等待，公开保存当前可靠 UTC 到期事实。"""
    request = WaitCompletedSave(action_id, runtime.wall_us())
    key = new_operation_key()
    receipt = await runtime.database.write(
        lambda owned: TimelapseRepository().complete_wait(request, key, owned),
        key=key, description="保存测试等待完成事实")
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return receipt.value.event_id


class _SavedPreparation(Exception):
    """准备阶段在明确的已提交存储边界结束。"""


class _PreparationBoundary(CaptureRepository):
    """真实仓储完成保存后中断处理器，避免预先消费被测 RESULTS。"""

    def __init__(self, boundary):
        self.boundary = boundary
        self.saved = False

    def _after(self, boundary, receipt):
        if self.boundary == boundary and receipt.kind is DbOutcomeKind.COMPLETED:
            self.saved = True
            self.boundary = None
            return replace(receipt, handoff_error=_SavedPreparation(boundary))
        return receipt

    def finish_start_result(self, *args, **kwargs):
        return self._after("start", super().finish_start_result(*args, **kwargs))

    def finish_capture_stop(self, *args, **kwargs):
        return self._after("stop", super().finish_capture_stop(*args, **kwargs))

    def conclude_activity(self, *args, **kwargs):
        return self._after("conclude", super().conclude_activity(*args, **kwargs))

    def release_occupancy(self, *args, **kwargs):
        return self._after("release", super().release_occupancy(*args, **kwargs))


async def _prepare_through_handler(runtime, action_id, handler, boundary):
    repository = _PreparationBoundary(boundary)
    runtime.capture = repository
    try:
        try:
            await capture_handler(handler)(action_id, runtime)
        except _SavedPreparation:
            assert repository.saved
        else:
            assert boundary is None, "处理器必须实际保存指定准备事实"
    finally:
        runtime.capture = CaptureRepository()


async def consumer_world(
    tmp_path,
    consumer,
    *,
    independent_activity=False,
    catalog=None,
    start=True,
    record_control_elapsed_ns=None,
    invalid_sibling=False,
    pending_capture_group=None,
    runtime_factory=None,
    runtime_options=None,
    max_delay_ms=5000,
    now_us=_NOW,
):
    """只执行本地公开事务和真实拍摄处理器，不补写历史或投影。"""
    cfg = _config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    handler, parameter_type, operation, observation = CASES[consumer]
    instant = datetime.fromtimestamp(now_us // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    actions = []
    if invalid_sibling:
        actions.append({"name":"无效设备任务","type":"camera_record","device_id":"missing-device",
            "scheduled_at":instant,"params":{"type":"video"},"policy":{"max_delay_ms":5000}})
    if independent_activity:
        actions.append({"name": "报告", "type": "report_status", "params": {"scope": "full"}})
    action_id = len(actions) + 1
    actions.append({"name": "拍摄", "type": handler, "device_id": "cam-1", "scheduled_at": instant,
                    "params": {"type": parameter_type}, "policy": {"max_delay_ms": max_delay_ms}})
    if pending_capture_group is not None:
        actions[-1]["group"] = pending_capture_group
        actions.append(
            {**actions[-1], "name": "待完成拍摄", "policy": {"max_delay_ms": 60000}}
        )
    if consumer == "cancel":
        actions.append({"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}})
    try:
        database = await capture_database(owned)
        accepted = await accept_input(ParsedInput("results.json", {
            "request_id": "1", "created_at": instant, "name": "产物核实", "actions": actions,
        }),
            AcceptanceContext(CommandMode.RUN, ResultCatalog() if catalog is None else catalog,
                AcceptanceRepository(), clock=SimpleNamespace(utc_micros=lambda: now_us)),
            new_operation_key(), database)
        assert accepted.plan_id is not None, accepted
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(action_id, now_us, now_us)),
            (scheduling.start_action, StartActionRequest(action_id, now_us, now_us)),
        ):
            receipt = save(request, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        control = create_autospec(ControlDriver, instance=True)
        control.control.return_value = DeviceCallResult.from_outcome(returned("operation", observation, activity_id))
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from .test_baseline_prepare import Directory

        options = {"evidence": RESULT_EVIDENCE,
            "check_config": AttemptConfig(3, Decimal("1.25"), Decimal("2")),
            "directory": Directory([lambda request: DirectoryRead(None, None, Page((), None))])}
        options.update({} if runtime_options is None else runtime_options)
        runtime = (await _runtime(owned, driver=control, **options)
            if runtime_factory is None else await runtime_factory(owned, driver=control))
        if not start:
            return owned, runtime, action_id, handler
        if handler == "camera_take_photo":
            await _prepare_through_handler(runtime, action_id, handler, "start")
        else:
            await capture_handler(handler)(action_id, runtime)
        if consumer in ("record", "winddown"):
            runtime.wall_us = lambda: _NOW + 60_000_000
            runtime.monotonic_ns = lambda: 5_000_000_000 + (60_000_000_000 if record_control_elapsed_ns is None else record_control_elapsed_ns)
            stop = create_autospec(StopDriver, instance=True)
            stop.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
            runtime.stopper = stop
            await _prepare_through_handler(runtime, action_id, handler, "conclude")
        if handler == "camera_record":
            processing_id, = owned.connection.execute(
                "SELECT id FROM recording_processing WHERE action_id=?", (action_id,)).fetchone()
            receipt = runtime.capture.save_check_decision(CheckDecisionSave(processing_id,
                CheckDecisionChoice.NOT_NEEDED, CheckBasis(CheckReason.CONTINUOUS_CONTROL_COMPLETE,
                    60000, control_elapsed_ns=60_000_000_000), runtime.wall_us()), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        if consumer == "cancel":
            repository = CancellationRepository()
            cancel_id = action_id + 1
            started = repository.start_cancel_action(StartCancelAction(cancel_id, runtime.wall_us()), new_operation_key(), owned)
            assert started.kind is DbOutcomeKind.COMPLETED, started.error
            fixed = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
                action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), runtime.wall_us()), new_operation_key(), owned)
            assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
            item_id, = fixed.value.item_ids
            applied = repository.apply_cancel_target(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP,
                runtime.wall_us()), new_operation_key(), owned)
            assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
            runtime.wall_us = lambda: _NOW + 60_000_000
            runtime.monotonic_ns = lambda: 65_000_000_000
            stop = create_autospec(StopDriver, instance=True)
            stop.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
            runtime.stopper = stop
            await _prepare_through_handler(runtime, action_id, handler, "stop")
        if consumer == "timelapse":
            # 前次 START 会话的文件责任和 worker 实际结束；新工厂只消费
            # 已保存事实，本次可靠 UTC 已越过原自主任务等待。
            database = await restart_capture_database(owned)
            runtime = (await _runtime(database, driver=control,
                **{**options, "wall_us": lambda: _NOW + 700_000_000})
                if runtime_factory is None else await runtime_factory(owned, driver=control))
        return owned, runtime, action_id, handler
    except BaseException:
        owned.connection.close()
        raise
