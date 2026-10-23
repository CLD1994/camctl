"""按已固定的实际分段成员读取、完整拷贝并保存主视频指标。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from camctl.capture.input_copy import InputContext, InputPhase, InputStep, obtain_recording_input
from camctl.capture.media_facts import probe_document, read_probe
from camctl.capture.video_coverage import CoverageMetric, CoverageTarget, VideoMetric
from camctl.capture.media_flow import (
    MediaFlow, _finish_internal_read, _resume_local_internal_end, _ensure_checksum_support,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.host_files.media import MediaProbe, select_main_video_stream
from camctl.host_files.models import FileRef
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.outputs.read_attempts import hold_read_end, retry_read_business, retry_read_ends, run_owned_read
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.segment_inspections import (
    InspectionCreate, InspectionResultSave, SegmentInspectionRef,
    SegmentInspectionRepository, read_inspection_for_member,
)

__all__ = [
    "SegmentInspectionPhase",
    "SegmentInspectionRequest",
    "SegmentInspectionStep",
    "inspect_segment",
]

_INSPECTION_STATE = enum_for("segment_inspections.state")
_RUN_STATUS = enum_for("operation_runs.status")


class SegmentInspectionPhase(Enum):
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class SegmentInspectionRequest:
    action_id: int
    member_id: int
    target: CoverageTarget
    budget: OperationConfig
    occurred_at: int

    def __post_init__(self) -> None:
        if type(self.action_id) is not int or self.action_id <= 0:
            raise ValueError("分段检查动作身份必须为正整数")
        if type(self.member_id) is not int or self.member_id <= 0:
            raise ValueError("分段检查成员身份必须为正整数")
        if not isinstance(self.target, CoverageTarget):
            raise TypeError("分段检查目标必须使用 CoverageTarget")
        if not isinstance(self.budget, OperationConfig):
            raise TypeError("分段检查预算必须使用 OperationConfig")
        if type(self.occurred_at) is not int:
            raise ValueError("分段检查时间必须是整数 UTC 微秒")


@dataclass(frozen=True, slots=True)
class SegmentInspectionStep:
    phase: SegmentInspectionPhase
    inspection_id: int | None = None
    copy_id: int | None = None
    metric: VideoMetric | None = None
    probe: MediaProbe | None = None
    input_file: FileRef | None = None
    reason: str | None = None
    error: BaseException | None = None


def _target_document(target: CoverageTarget) -> dict[str, Any]:
    return {"metric": target.metric.value, "value": target.value}


def _budget_document(config: OperationConfig) -> dict[str, Any]:
    return {"max_attempts": config.max_attempts,
            "timeout_s": config.timeout_s,
            "retry_interval_s": config.retry_interval_s}


def _step_from_ref(ref: SegmentInspectionRef, copy_id: int | None = None) -> SegmentInspectionStep:
    if ref.state == int(_INSPECTION_STATE.PENDING):
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                                     inspection_id=ref.inspection_id, copy_id=copy_id)
    document = ref.result_json
    if not isinstance(document, Mapping):
        raise ConsistencyError("终态分段检查缺少已保存结果")
    metric_doc = document.get("metric")
    if not isinstance(metric_doc, Mapping):
        raise ConsistencyError("分段检查缺少主视频度量事实")
    metric = VideoMetric(CoverageMetric(metric_doc["metric"]), metric_doc.get("value"),
                         metric_doc.get("reason"))
    probe_doc = document.get("probe")
    probe = read_probe(probe_doc) if isinstance(probe_doc, Mapping) else None
    phase = {
        int(_INSPECTION_STATE.COMPLETED): SegmentInspectionPhase.COMPLETED,
        int(_INSPECTION_STATE.FAILED): SegmentInspectionPhase.FAILED,
        int(_INSPECTION_STATE.UNCONFIRMED): SegmentInspectionPhase.UNCONFIRMED,
    }.get(ref.state)
    if phase is None:
        raise ConsistencyError(f"分段检查状态未登记: {ref.state!r}")
    failure = document.get("failure")
    reason = (failure.get("reason") if isinstance(failure, Mapping)
              else metric.reason)
    return SegmentInspectionStep(phase, ref.inspection_id, copy_id, metric, probe,
                                 reason=reason)


def _save_result(flow: MediaFlow, request: InspectionResultSave) -> bool:
    identity = request.inspection_id
    pending = flow.pending_segment_inspection_results.get(identity)
    if pending is None:
        pending = (request, new_operation_key())
        flow.pending_segment_inspection_results[identity] = pending
    elif pending[0] != request:
        raise ConsistencyError("同一分段检查已有不同的待保存结果")
    outcome = SegmentInspectionRepository().save_result(pending[0], pending[1], flow.owned)
    if outcome.kind is DbOutcomeKind.COMPLETED:
        del flow.pending_segment_inspection_results[identity]
        return True
    return False


def _member_source(owned, action_id: int, member_id: int) -> tuple[int, str | None]:
    rows = owned.connection.execute(
        "SELECT m.file_id,d.original_name FROM segment_members m"
        " JOIN segment_sets s ON s.id=m.set_id"
        " JOIN device_files d ON d.id=m.file_id"
        " WHERE m.id=? AND s.action_id=? LIMIT 2", (member_id, action_id)).fetchall()
    if len(rows) != 1:
        raise ConsistencyError("分段检查成员缺失或不唯一")
    return rows[0][0], rows[0][1]


def _copy_candidate(flow: MediaFlow, request: SegmentInspectionRequest,
                    inspection_id: int, source_file_id: int, original_name: str | None):
    suffix = Path(original_name).suffix[1:].lower() if original_name else None
    return FileCandidate(
        action_id=request.action_id, item_id=None, processing_id=None,
        output_id=None, source_device_file_id=source_file_id,
        target_extension=suffix, delivery_extension=None, delivery_display_name=None,
        config=request.budget, occurred_at=flow.occurred_at(), inspection_id=inspection_id)


def _run_state(flow: MediaFlow, copy_id: int):
    rows = flow.owned.connection.execute(
        "SELECT id,status,attempts_used,max_attempts_used,retry_wait_required"
        " FROM operation_runs WHERE copy_id=? AND kind=? LIMIT 2",
        (copy_id, int(enum_for("operation_runs.kind").READ_FILE))).fetchall()
    if len(rows) != 1:
        raise ConsistencyError("分段检查副本必须对应唯一 READ_FILE 流程")
    return rows[0]


async def _obtain_input(flow: MediaFlow, request: SegmentInspectionRequest,
                        inspection_id: int, source_file_id: int,
                        original_name: str | None) -> InputStep:
    config = request.budget
    read_flow = replace(flow, max_read_attempts=config.max_attempts,
                         read_idle_timeout_s=config.timeout_s,
                         retry_interval_s=config.retry_interval_s)
    _ensure_checksum_support(read_flow, source_file_id)
    copies = read_flow.copies()
    qualified = copies.qualify(_copy_candidate(read_flow, request, inspection_id,
                                              source_file_id, original_name))
    if qualified.disposition.value != "saved":
        from camctl.capture.media import SaveDisposition
        phase = InputPhase.QUALIFY_UNKNOWN if qualified.disposition is SaveDisposition.UNKNOWN else InputPhase.QUALIFY_REJECTED
        return InputStep(phase, error=qualified.error)
    qualification = qualified.value
    if qualification.outcome is not QualificationOutcome.GRANTED:
        return InputStep(InputPhase.REJECTED_FINAL if qualification.outcome is QualificationOutcome.REJECTED_FINAL
                         else InputPhase.WAITING, reason=qualification.reason)
    copy_id = qualification.copy_id
    state = copies.copy_state(copy_id)
    if state.verification_state not in (3, 5) or state.target_sha256 is None:
        run_id, run_status, attempts_used, max_attempts_used, retry_wait_required = _run_state(read_flow, copy_id)
        if run_status == int(_RUN_STATUS.FAILED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="run_ended")
        wait_key = f"read/{copy_id}"
        if read_flow.retry_gate.remaining(wait_key, attempts_used=attempts_used,
                retry_wait_required=retry_wait_required == 1,
                max_attempts_used=max_attempts_used,
                interval_s=config.retry_interval_s,
                now_ns=read_flow.monotonic_ns()) is not None:
            return InputStep(InputPhase.RETRY_WAITING, copy_id=copy_id)
        slot = copies.repository.grant_read_slot(SlotRequest(copy_id, read_flow.occurred_at()),
                                                  new_operation_key(), read_flow.owned)
        if slot.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"分段检查读取机会未可靠保存: {slot.error}")
        if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason=slot.value.reason)
        ticket, reason = __import__("camctl.capture.media_flow", fromlist=["acquire_read_attempt"]).acquire_read_attempt(
            read_flow, AttemptIntent("read", request.action_id, OperationKind.READ_FILE,
                AttemptTarget(copy_id=copy_id), None,
                AttemptConfig(config.max_attempts, config.timeout_s, config.retry_interval_s),
                read_flow.occurred_at(), copy_round=state.round))
        if ticket is None:
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason=reason)
    else:
        ticket = None

    from camctl.outputs.read_attempts import hold_read_end
    sessions = read_flow.sessions if ticket is None else read_flow.sessions.for_attempt(
        ticket, idle_timeout_s=config.timeout_s)
    context: InputContext
    context = InputContext(
        action_id=request.action_id, processing_id=None,
        source_device_file_id=source_file_id, target_extension=(Path(original_name).suffix[1:].lower()
            if original_name and Path(original_name).suffix else None),
        config=config, segment_size=read_flow.segment_size,
        staging=read_flow.roots.staging, copies=copies, sessions=sessions,
        occurred_at=read_flow.occurred_at(), digest=(read_flow.digest_for(source_file_id)
            if read_flow.digest_for is not None else read_flow.digest),
        read_end=(read_flow.pending_read_ends[ticket.target_id].end
            if ticket is not None and ticket.target_id in read_flow.pending_read_ends else None),
        on_read_end=(None if ticket is None else lambda end, complete:
            hold_read_end(read_flow, ticket, end, complete,
                resume=lambda current, held: _resume_local_internal_end(
                    current, held, read_flow, context), evidence=read_flow.evidence)),
        on_read_start=(None if ticket is None else
            lambda: read_flow.pending_read_ends.pop(copy_id, None)),
        inspection_id=inspection_id)

    async def read_and_save():
        step = await obtain_recording_input(context)
        if ticket is not None:
            _finish_internal_read(read_flow, ticket, step, max_attempts=config.max_attempts)
        return step

    result = await run_owned_read(read_flow, copy_id, read_and_save) if ticket is not None else await read_and_save()
    if result.phase is InputPhase.SEGMENT_FAILED and result.source_failed:
        read_flow.retry_gate.established(f"read/{copy_id}", read_flow.monotonic_ns())
    elif result.phase in (InputPhase.INPUT_READY, InputPhase.RECOPY_PENDING):
        read_flow.retry_gate.cleared(f"read/{copy_id}")
    return result


def _metric_from_probe(target: CoverageTarget, probe: MediaProbe) -> VideoMetric:
    error_code = (probe.error.split(":", 1)[0].strip()
                  if probe.error is not None else None)
    # 容器时长字段缺失不影响已成功解析的逐流事实；覆盖使用主视频
    # 流时长或显式请求的实际读取帧数，不使用容器时长。
    if probe.error is not None and error_code != "missing_duration":
        if error_code == "no_video_stream":
            return VideoMetric(target.metric, None, "no_video_stream")
        return VideoMetric(target.metric, None, "probe_error")
    stream = select_main_video_stream(probe)
    if stream is None:
        return VideoMetric(target.metric, None, "main_video_stream_unconfirmed")
    if target.metric is CoverageMetric.DURATION_S:
        value = stream.duration_s
        return VideoMetric(target.metric, value, None if value is not None else "stream_duration_unavailable")
    value = stream.frame_count
    return VideoMetric(target.metric, value, None if value is not None else "stream_frame_count_unavailable")


def _result_document(metric: VideoMetric, *, probe: MediaProbe | None,
                     ran: bool, tool_error: str | None,
                     failure: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "metric": {"metric": metric.metric.value, "value": metric.value,
                   "reason": metric.reason},
        "probe": probe_document(probe) if probe is not None else None,
        "tool": {"ran": ran, "error": tool_error},
        "failure": dict(failure) if failure is not None else None,
    }


async def inspect_segment(flow: MediaFlow, request: SegmentInspectionRequest) -> SegmentInspectionStep:
    """推进一个真实固定成员：复用原预算，完整拷贝后才读取媒体指标。"""
    if not isinstance(flow, MediaFlow) or not isinstance(request, SegmentInspectionRequest):
        raise TypeError("逐段检查需要 MediaFlow 与 SegmentInspectionRequest")
    repository = SegmentInspectionRepository()
    inspection_request = InspectionCreate(request.action_id, request.member_id,
        _target_document(request.target), _budget_document(request.budget), request.occurred_at)
    created = repository.create(inspection_request, new_operation_key(), flow.owned)
    if created.kind is not DbOutcomeKind.COMPLETED:
        if created.kind is DbOutcomeKind.ROLLED_BACK and isinstance(
            created.error, ConsistencyError
        ):
            raise created.error
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            reason="inspection_create_unknown", error=created.error)
    ref = created.value
    if (not json_equal(ref.target_json, _target_document(request.target))
            or not json_equal(ref.budget_json, _budget_document(request.budget))):
        raise ConsistencyError("分段检查成员不能更换原目标或读取预算")
    if ref.state != int(_INSPECTION_STATE.PENDING):
        facts = read_inspection_for_member(request.action_id, request.member_id, flow.owned)
        return _step_from_ref(ref, facts.copy.copy_id if facts.copy is not None else None)

    source_file_id, original_name = _member_source(flow.owned, request.action_id, request.member_id)
    copy_facts = read_inspection_for_member(request.action_id, request.member_id, flow.owned).copy
    copy_id = copy_facts.copy_id if copy_facts is not None else None
    if copy_id is not None:
        _run_id, run_status, _used, _maximum, _wait = _run_state(flow, copy_id)
        if run_status == int(_RUN_STATUS.FAILED):
            failure = {"code": "read_failed", "reason": "read_attempts_exhausted"}
            metric = VideoMetric(request.target.metric, None, "read_attempts_exhausted")
            saved = _save_result(flow, InspectionResultSave(ref.inspection_id,
                int(_INSPECTION_STATE.FAILED), _result_document(metric, probe=None,
                ran=False, tool_error=None, failure=failure), flow.occurred_at()))
            return (_step_from_ref(repository.read(ref.inspection_id, flow.owned), copy_id)
                    if saved else SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                        ref.inspection_id, copy_id, metric, reason="result_save_unknown"))

    # 同一个原检查结果保存若上次提交结果未知，优先用原请求和操作键续接。
    pending = flow.pending_segment_inspection_results.get(ref.inspection_id)
    if pending is not None:
        outcome = repository.save_result(pending[0], pending[1], flow.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                ref.inspection_id, copy_id, reason="result_save_unknown", error=outcome.error)
        del flow.pending_segment_inspection_results[ref.inspection_id]
        return _step_from_ref(repository.read(ref.inspection_id, flow.owned), copy_id)

    # 沿用原读取保存器的未知结果与挂起业务续接；不创建第二条读取责任。
    retry_read_business(flow)
    await retry_read_ends(flow)
    input_step = await _obtain_input(flow, request, ref.inspection_id, source_file_id, original_name)
    if input_step.phase is not InputPhase.INPUT_READY or input_step.input_file is None:
        if input_step.phase is InputPhase.QUALIFY_REJECTED and input_step.error is not None:
            return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
                input_step.copy_id, reason="qualification_waiting", error=input_step.error)
        if input_step.phase is InputPhase.WAITING and input_step.reason == "run_ended":
            failure = {"code": "read_failed", "reason": "read_attempts_exhausted"}
            metric = VideoMetric(request.target.metric, None, "read_attempts_exhausted")
            saved = _save_result(flow, InspectionResultSave(ref.inspection_id,
                int(_INSPECTION_STATE.FAILED), _result_document(metric, probe=None,
                ran=False, tool_error=None, failure=failure), flow.occurred_at()))
            return (_step_from_ref(repository.read(ref.inspection_id, flow.owned), input_step.copy_id)
                    if saved else SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                        ref.inspection_id, input_step.copy_id, metric, reason="result_save_unknown"))
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
            input_step.copy_id, reason=input_step.reason or input_step.phase.value,
            error=input_step.error)

    count_frames = request.target.metric is CoverageMetric.FRAMES
    probe_call = getattr(flow.tools, "probe_segment", None)
    if probe_call is None:
        raise ConsistencyError("受管媒体工具缺少显式逐段探测端口 probe_segment")
    try:
        result = await probe_call(input_step.input_file, count_frames=count_frames)
    except Exception as error:
        metric = VideoMetric(request.target.metric, None, "probe_failed")
        document = _result_document(metric, probe=None, ran=False,
                                    tool_error=str(error),
                                    failure={"code": "probe_failed", "reason": str(error)})
        state = _INSPECTION_STATE.FAILED
    else:
        if not result.ran or result.error is not None:
            metric = VideoMetric(request.target.metric, None,
                "probe_not_run" if not result.ran else "probe_failed")
            document = _result_document(metric, probe=None, ran=result.ran,
                tool_error=result.error,
                failure={"code": "probe_failed", "reason": result.error or "not_run"})
            state = _INSPECTION_STATE.FAILED
        elif not isinstance(result.value, MediaProbe):
            metric = VideoMetric(request.target.metric, None, "probe_result_unavailable")
            document = _result_document(metric, probe=None, ran=True,
                tool_error="probe returned no MediaProbe")
            state = _INSPECTION_STATE.UNCONFIRMED
        else:
            probe = result.value
            metric = _metric_from_probe(request.target, probe)
            document = _result_document(metric, probe=probe, ran=True, tool_error=None)
            state = (_INSPECTION_STATE.COMPLETED if metric.value is not None
                     else _INSPECTION_STATE.UNCONFIRMED)
    saved = _save_result(flow, InspectionResultSave(ref.inspection_id, int(state),
        document, flow.occurred_at()))
    if not saved:
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
            input_step.copy_id, metric=metric, input_file=input_step.input_file,
            reason="result_save_unknown")
    return _step_from_ref(repository.read(ref.inspection_id, flow.owned), input_step.copy_id)
