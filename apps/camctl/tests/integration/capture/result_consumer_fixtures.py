"""RESULTS 验证沿公开受理和调度建立可回放的完整拍摄历史。"""

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet,
    FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction,
)
from camctl.capture.handlers import _advance_timelapse_natural_wait, _control_call, _stop_call, capture_handler
from camctl.capture.models import ActivityConcludeSave
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


def complete_natural_wait(runtime, action_id):
    """正常自然结果的手工消费者先可靠保存原实际等待，不制造设备结果。"""
    event_id = _advance_timelapse_natural_wait(runtime, runtime.action(action_id))
    assert event_id is not None
    return event_id


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
    assemble=None,
):
    """只执行本地公开事务和真实拍摄处理器，不补写历史或投影。"""
    cfg = _config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    handler, parameter_type, operation, observation = CASES[consumer]
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    actions = []
    if invalid_sibling:
        actions.append({"name":"无效设备任务","type":"camera_record","device_id":"missing-device",
            "scheduled_at":instant,"params":{"type":"video"},"policy":{"max_delay_ms":5000}})
    if independent_activity:
        actions.append({"name": "报告", "type": "report_status", "params": {"scope": "full"}})
    action_id = len(actions) + 1
    actions.append({"name": "拍摄", "type": handler, "device_id": "cam-1", "scheduled_at": instant,
                    "params": {"type": parameter_type}, "policy": {"max_delay_ms": 5000}})
    if pending_capture_group is not None:
        actions[-1]["group"] = pending_capture_group
        actions.append(
            {**actions[-1], "name": "待完成拍摄", "policy": {"max_delay_ms": 60000}}
        )
    if consumer == "cancel":
        actions.append({"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}})
    try:
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("results.json", {
            "request_id": "1", "created_at": instant, "name": "产物核实", "actions": actions,
        }), ResultCatalog() if catalog is None else catalog, CommandMode.RUN, _NOW), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(action_id, _NOW, _NOW)),
            (scheduling.start_action, StartActionRequest(action_id, _NOW, _NOW)),
        ):
            receipt = save(request, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        control = create_autospec(ControlDriver, instance=True)
        control.control.return_value = DeviceCallResult.from_outcome(returned("operation", observation, activity_id))
        runtime = (_runtime(owned, driver=control) if assemble is None
                   else await assemble(owned, driver=control))
        runtime.evidence = RESULT_EVIDENCE
        runtime.check_config = AttemptConfig(3, Decimal("1.25"), Decimal("2"))
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from .test_baseline_prepare import Directory

        runtime.baseline_directory = Directory(
            [lambda request: DirectoryRead(None, None, Page((), None))]
        )
        if not start:
            return owned, runtime, action_id, handler
        if handler == "camera_record":
            await capture_handler(handler)(action_id, runtime)
        else:
            facts = {"sent_at": _NOW}
            if handler == "camera_take_photo":
                facts.update(started_at=_NOW, activity_state=2)
            await _control_call(runtime, runtime.action(action_id), operation, observation, activity_facts=facts)
        if consumer in ("record", "winddown", "cancel"):
            if record_control_elapsed_ns is not None:
                from camctl.capture.models import HostTimerStopSave
                original_finish = runtime.finish
                def finish_with_control(ticket, actual, **kwargs):
                    if ticket.operation == 'stop':
                        kwargs['host_timer_stop'] = HostTimerStopSave(record_control_elapsed_ns)
                    return original_finish(ticket, actual, **kwargs)
                runtime.finish = finish_with_control
            runtime.wall_us = lambda: _NOW + 60_000_000
            runtime.monotonic_ns = lambda: 65_000_000_000
            stop = create_autospec(StopDriver, instance=True)
            stop.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
            runtime.stopper = stop
            stopped = await _stop_call(runtime, runtime.action(action_id),
                "stop_timelapse" if handler == "camera_timelapse" else "stop_recording")
            assert stopped.phase == "confirmed", stopped
            concluded = runtime.capture.conclude_activity(ActivityConcludeSave(action_id, runtime.wall_us()),
                new_operation_key(), owned)
            assert concluded.kind is DbOutcomeKind.COMPLETED, concluded.error
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
        if consumer == "timelapse":
            # 消费测试从已保存的前次 START 恢复，不带入旧运行的
            # 单调钟锚点；本次可靠 UTC 已越过原自主任务等待。
            runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={},
                              wall_us=lambda: _NOW + 700_000_000)
        return owned, runtime, action_id, handler
    except BaseException:
        owned.connection.close()
        raise
