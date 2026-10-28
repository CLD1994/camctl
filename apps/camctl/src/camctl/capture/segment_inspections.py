"""按已固定的实际分段成员读取、完整拷贝并保存主视频指标。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from camctl.capture.input_copy import InputContext, InputPhase, InputStep, obtain_recording_input
from camctl.capture.media_facts import probe_document, read_probe
from camctl.capture.media_flow import (
    MediaFlow, _finish_internal_read, _resume_local_internal_end, _ensure_checksum_support,
    _exhaust_unknown_read_before_slot, _read_dispatched,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget, VideoMetric
from camctl.host_files.media import MediaProbe, select_main_video_stream
from camctl.host_files.models import FileRef
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.outputs.read_attempts import hold_read_end, retry_read_business, retry_read_ends, run_owned_read
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.business import settle_required
from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_errors
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.segment_inspections import (
    InspectionCreate, InspectionResultSave, SegmentInspectionRef,
    SegmentInspectionRepository, read_inspection_for_member,
)
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE, positive_limit
from camctl.logging_runtime.diagnostics import record_error

__all__ = [
    "SegmentInspectionPhase",
    "SegmentInspectionRequest",
    "SegmentInspectionStep",
    "inspect_segment",
]

_INSPECTION_STATE = enum_for("segment_inspections.state")
_RUN_STATUS = enum_for("operation_runs.status")


def _inspection_batch_size(flow) -> int:
    configured = getattr(flow, "read_discovery_batch_size", None)
    return positive_limit(DEFAULT_BATCH_SIZE if configured is None else configured)


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


async def _save_result(flow: MediaFlow, request: InspectionResultSave) -> bool:
    identity = request.inspection_id
    pending = flow.pending_segment_inspection_results.get(identity)
    if pending is None:
        pending = (request, new_operation_key())
        flow.pending_segment_inspection_results[identity] = pending
    elif pending[0] != request:
        raise ConsistencyError("同一分段检查已有不同的待保存结果")
    outcome = await flow.database.write(lambda owned: SegmentInspectionRepository().save_result(pending[0], pending[1], owned), key=pending[1])
    if outcome.kind is DbOutcomeKind.COMPLETED:
        del flow.pending_segment_inspection_results[identity]
        return outcome.value.state != int(_INSPECTION_STATE.PENDING)
    if any(not isinstance(error, asyncio.CancelledError) for error in db_outcome_errors(outcome)):
        record_error(db_failure(ConsistencyError, "原分段检查结果保存未可靠完成", outcome),
            stage="capture.segment_save", context={"inspection_id": identity})
    return False


async def settle_inspection_read_failure(flow: MediaFlow, copy_id: int,
                                   occurred_at: int) -> bool:
    """把已可靠终结的 inspection READ 错误写成原检查结果。"""
    row = (await flow.database.read(lambda owned, inputs=("SELECT c.inspection_id,c.committed_bytes,c.source_size,c.source_sha256,"
        "c.target_sha256,c.verification_state,c.verification_error_json,"
        "r.id,r.status,r.error_json,r.retry_wait_required,r.attempts_used"
        " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " WHERE c.id=? AND r.kind=? LIMIT 2", (copy_id, int(enum_for("operation_runs.kind").READ_FILE))): owned.connection.execute(*inputs).fetchall(), description="读取拍摄直接事实"))
    if len(row) != 1 or row[0][0] is None:
        raise ConsistencyError("分段检查读取失败缺少唯一的检查副本归属")
    (inspection_id, committed_bytes, source_size, source_sha256, target_sha256,
     verification_state, verification_error_raw, run_id, run_status, run_error_raw,
     retry_wait_required, attempts_used) = row[0]
    if run_status != int(_RUN_STATUS.FAILED):
        return False
    if retry_wait_required != 0:
        raise ConsistencyError("已失败的分段检查读取仍要求重试等待")
    attempt_running = int(enum_for("operation_attempts.status").RUNNING)
    unfinished = (await flow.database.read(lambda owned, inputs=("SELECT 1 FROM operation_attempts WHERE run_id=("
        " SELECT id FROM operation_runs WHERE copy_id=? AND kind=?)"
        " AND (status=? OR result_json IS NULL) LIMIT 1", (copy_id, int(enum_for("operation_runs.kind").READ_FILE), attempt_running)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
    if unfinished is not None:
        return False

    repository = SegmentInspectionRepository()
    inspection = (await flow.database.read(lambda owned: repository.read(inspection_id, owned)))
    if inspection.state != int(_INSPECTION_STATE.PENDING):
        return inspection_id not in flow.pending_segment_inspection_results
    if run_error_raw is None:
        raise ConsistencyError("失败的分段检查读取流程缺少原错误")
    read_error = parse_exact_json(run_error_raw)
    if not isinstance(read_error, Mapping) or not isinstance(read_error.get("code"), str):
        raise ConsistencyError("失败的分段检查读取错误结构不可解释")
    verification_error = (None if verification_error_raw is None
                          else parse_exact_json(verification_error_raw))
    reason = read_error["code"]
    if reason == "source_file_unconfirmed":
        failed_state = int(enum_for("file_copies.verification_state").FAILED)
        if (committed_bytes != source_size or source_sha256 is not None
                or target_sha256 is None or verification_state != failed_state
                or verification_error is None):
            raise ConsistencyError("源摘要失败的分段检查缺少完整读取或校验诊断")
        attempt = (await flow.database.read(lambda owned, inputs=("SELECT attempt_no,status,result_json FROM operation_attempts"
            " WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1", (run_id,)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
        if attempt is None or attempt[0] != attempts_used:
            raise ConsistencyError("源摘要失败的分段检查缺少最后原读取尝试")
        if (attempt[1] != int(enum_for("operation_attempts.status").SUCCEEDED)
                or attempt[2] is None):
            raise ConsistencyError("源摘要失败不能以未确认或失败的 READ 结果收场")
        attempt_result = parse_exact_json(attempt[2])
        settlement = (attempt_result.get("settlement")
                      if isinstance(attempt_result, Mapping) else None)
        evidence = settlement.get("evidence") if isinstance(settlement, Mapping) else None
        if (not isinstance(settlement, Mapping) or settlement.get("basis") != "observed"
                or not isinstance(evidence, Mapping)
                or evidence.get("type") != "read_returned"):
            raise ConsistencyError("源摘要失败的 READ 未保存原实际 clean ReadEnd 观察")
    metric = VideoMetric(
        CoverageMetric(inspection.target_json["metric"]), None, reason,
    )
    failure = {"code": "read_failed", "reason": reason, "read_error": read_error}
    if verification_error is not None:
        failure["verification_error"] = verification_error
    request = InspectionResultSave(
        inspection_id=inspection_id,
        state=int(_INSPECTION_STATE.FAILED),
        result_json=_result_document(metric, probe=None, ran=False,
                                     tool_error=None, failure=failure),
        occurred_at=occurred_at,
    )
    return (await _save_result(flow, request))


async def settle_terminal_inspection_read_slot(flow: MediaFlow, copy_id: int,
                                         occurred_at: int) -> bool:
    """检查结果可靠终态后，按原 release_read_slot 申请补齐机会释放。"""
    from camctl.outputs.read_attempts import save_read_business

    query = ("SELECT c.inspection_id,c.slot_device_id,r.status,r.retry_wait_required,s.state,a.cancel_requested,c.read_end_event_id"
        " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " JOIN segment_inspections s ON s.id=c.inspection_id"
        " JOIN actions a ON a.id=s.action_id"
        " WHERE c.id=? AND r.kind=? AND r.action_id=s.action_id LIMIT 2",
        (copy_id, int(enum_for("operation_runs.kind").READ_FILE)))
    row = await flow.database.read(lambda owned: owned.connection.execute(*query).fetchall(), description="读取拍摄直接事实")
    if len(row) != 1 or row[0][0] is None:
        raise ConsistencyError("终态分段检查缺少唯一的检查读取副本")
    inspection_id, slot_device_id, run_status, retry_wait_required, inspection_state, canceled, end_id = row[0]
    if inspection_state == int(_INSPECTION_STATE.PENDING):
        return False
    if type(canceled) is not int or canceled not in (0, 1):
        raise ConsistencyError("终态分段检查的所属动作取消状态无效")
    pending_intent = flow.pending_read_business.get((copy_id, "read_intent"))
    if pending_intent is not None:
        if canceled != 1 or not pending_intent.shared.failed():
            return False
        if not pending_intent.inspection_cancel_recovery:
            pending_intent.inspection_cancel_recovery.append(True)
        await save_read_business(flow, copy_id, "read_intent", pending_intent.request,
                                 pending_intent.save, retry=True)
        row = await flow.database.read(lambda owned: owned.connection.execute(*query).fetchall(), description="读取拍摄直接事实")
        if len(row) != 1:
            raise ConsistencyError("分段检查原读取意图核实后缺少唯一责任")
        inspection_id, slot_device_id, run_status, retry_wait_required, inspection_state, canceled, end_id = row[0]
        if inspection_state == int(_INSPECTION_STATE.PENDING) or canceled != 1:
            raise ConsistencyError("分段检查原读取意图核实后的取消资格不一致")
    continuation = flow.continuing_read_tickets.get(copy_id)
    if canceled == 1 and continuation is not None and continuation.inspection_cancel_recovery:
        ticket = continuation.ticket
        identity = (ticket.run_id, ticket.attempt_id)
        pending = flow.pending_read_results.get(identity)
        retry = pending is not None
        if pending is None and continuation.never_dispatched:
            from camctl.outputs.read_attempts import ReadDispatchPrevented, prevented_read_result
            pending = prevented_read_result(flow, ticket,
                ReadDispatchPrevented("canceled_before_dispatch", canceled=True), flow.evidence)
        if pending is not None:
            from camctl.capture.media_flow import _save_internal_read_and_settle
            await _save_internal_read_and_settle(flow, pending, retry=retry)
            row = await flow.database.read(lambda owned: owned.connection.execute(*query).fetchall(), description="读取拍摄直接事实")
            if len(row) != 1:
                raise ConsistencyError("分段检查原读取结果保存后缺少唯一责任")
            inspection_id, slot_device_id, run_status, retry_wait_required, inspection_state, canceled, end_id = row[0]
            if inspection_state == int(_INSPECTION_STATE.PENDING) or canceled != 1:
                raise ConsistencyError("分段检查原读取结果保存后的取消资格不一致")
    if canceled == 1 and run_status == int(_RUN_STATUS["ACTIVE"]) and end_id is not None:
        await _settle_canceled_inspection_end(flow, copy_id)
        row = await flow.database.read(lambda owned: owned.connection.execute(*query).fetchall(), description="读取拍摄直接事实")
        if len(row) != 1:
            raise ConsistencyError("分段检查读取结束收场后缺少唯一责任")
        inspection_id, slot_device_id, run_status, retry_wait_required, inspection_state, canceled, end_id = row[0]
    pending_release = (copy_id, "release_read_slot") in flow.pending_read_business
    if slot_device_id is None and not pending_release:
        return True
    if run_status not in tuple(int(_RUN_STATUS[name])
                               for name in ("PENDING", "SUCCEEDED", "FAILED", "CANCELED")):
        return False
    if retry_wait_required != 0:
        return False
    attempt_running = int(enum_for("operation_attempts.status").RUNNING)
    unfinished = (await flow.database.read(lambda owned, inputs=("SELECT 1 FROM operation_attempts WHERE run_id=("
        " SELECT id FROM operation_runs WHERE copy_id=? AND kind=?)"
        " AND (status=? OR result_json IS NULL) LIMIT 1", (copy_id, int(enum_for("operation_runs.kind").READ_FILE), attempt_running)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
    if unfinished is not None:
        return False
    outcome = (await save_read_business(
        flow, copy_id, "release_read_slot",
        SlotRequest(copy_id, occurred_at), OutputsRepository().release_read_slot,
    ))
    return outcome.value.outcome in (SlotOutcome.RELEASED, SlotOutcome.ALREADY_RELEASED)


async def _settle_canceled_inspection_end(flow, copy_id):
    from camctl.outputs.read_attempts import (HeldReadEnd, canceled_complete_read_result,
                                              running_read_ticket, run_owned_read)
    from camctl.persistence.repositories.read_end import load_read_end
    from camctl.capture.media_flow import _save_internal_read_and_settle

    async def settle(_control):
        def current(owned):
            original = running_read_ticket(owned, copy_id)
            return None if original is None else (original[0], load_read_end(copy_id, owned))
        result = await flow.database.read(current)
        if result is None or result[1] is None:
            return
        ticket, saved = result
        if ticket != saved[0]:
            raise ConsistencyError("取消检查的读取结束与原票据不一致")
        held = flow.pending_read_ends.get(copy_id)
        if held is not None and (held.ticket != ticket or held.end != saved[1]):
            raise ConsistencyError("取消检查的读取结束与原拥有者不一致")
        if held is None:
            flow.pending_read_ends[copy_id] = HeldReadEnd(ticket, saved[1], saved[2], evidence=flow.evidence)
        pending = await canceled_complete_read_result(flow, ticket, flow.evidence)
        if pending is not None:
            await _save_internal_read_and_settle(flow, pending)

    await run_owned_read(flow, copy_id, settle)


async def settle_terminal_inspection_slots_for_action(flow: MediaFlow, action_id: int,
                                                occurred_at: int) -> bool:
    """仅补齐该动作已终态检查副本上欠缺的读取机会释放。"""
    complete = True
    # UNKNOWN 的释放必须按原 copy id 与原 request/key 续接。先核对该 copy
    # 当前仍属于本动作的终态 inspection，不能把其他读取责任并入本次收场。
    pending_copy_ids = tuple(
        copy_id for copy_id, operation in flow.pending_read_business
        if operation == "release_read_slot"
    )
    for copy_id in pending_copy_ids:
        rows = (await flow.database.read(lambda owned: owned.connection.execute(
            "SELECT s.id FROM file_copies c"
            " JOIN segment_inspections s ON s.id=c.inspection_id"
            " WHERE c.id=? AND s.action_id=? AND s.state!=? LIMIT 2",
            (copy_id, action_id, int(_INSPECTION_STATE.PENDING)),
        ).fetchall()))
        if len(rows) > 1:
            raise ConsistencyError("同一读取机会关联了重复的当前动作检查责任")
        if rows:
            complete = (await settle_terminal_inspection_read_slot(
                flow, copy_id, occurred_at)) and complete

    batch_size = _inspection_batch_size(flow)
    after_inspection_id = 0
    after_copy_id = 0
    while True:
        rows = (await flow.database.read(lambda owned: owned.connection.execute(
            "SELECT s.id,c.id FROM segment_inspections s"
            " JOIN file_copies c ON c.inspection_id=s.id"
            " JOIN actions a ON a.id=s.action_id"
            " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
            " WHERE s.action_id=? AND s.state!=? AND (c.slot_device_id IS NOT NULL"
            " OR (c.read_end_event_id IS NOT NULL AND r.status=2 AND a.cancel_requested=1))"
            " AND (s.id>? OR (s.id=? AND c.id>?))"
            " ORDER BY s.id,c.id LIMIT ?",
            (action_id, int(_INSPECTION_STATE.PENDING), after_inspection_id,
             after_inspection_id, after_copy_id, batch_size),
        ).fetchall()))
        if not rows:
            break
        for inspection_id, copy_id in rows:
            complete = (await settle_terminal_inspection_read_slot(
                flow, copy_id, occurred_at)) and complete
        after_inspection_id, after_copy_id = rows[-1]
    return complete


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


async def _run_state(flow: MediaFlow, copy_id: int):
    rows = (await flow.database.read(lambda owned, inputs=("SELECT id,status,attempts_used,max_attempts_used,retry_wait_required"
        " FROM operation_runs WHERE copy_id=? AND kind=? LIMIT 2", (copy_id, int(enum_for("operation_runs.kind").READ_FILE))): owned.connection.execute(*inputs).fetchall(), description="读取拍摄直接事实"))
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
    (await _ensure_checksum_support(read_flow, source_file_id))
    copies = read_flow.copies()
    qualified = (await copies.qualify(_copy_candidate(read_flow, request, inspection_id,
                                              source_file_id, original_name)))
    if qualified.disposition.value != "saved":
        from camctl.capture.media import SaveDisposition
        phase = InputPhase.QUALIFY_UNKNOWN if qualified.disposition is SaveDisposition.UNKNOWN else InputPhase.QUALIFY_REJECTED
        return InputStep(phase, error=qualified.error)
    qualification = qualified.value
    if qualification.outcome is not QualificationOutcome.GRANTED:
        return InputStep(InputPhase.REJECTED_FINAL if qualification.outcome is QualificationOutcome.REJECTED_FINAL
                         else InputPhase.WAITING, reason=qualification.reason)
    copy_id = qualification.copy_id
    state = (await copies.copy_state(copy_id))
    ticket = None
    from camctl.outputs.read_attempts import HeldReadEnd, unfinished_read_end
    unfinished = await read_flow.database.read(lambda owned: unfinished_read_end(owned, copy_id))
    if unfinished is not None:
        _original, saved_end = unfinished
        if saved_end is not None:
            ticket, end, occurred_at, _ = saved_end
            held = read_flow.pending_read_ends.get(copy_id)
            if held is None:
                read_flow.pending_read_ends[copy_id] = HeldReadEnd(ticket, end, occurred_at, evidence=read_flow.evidence)
            elif held.ticket != ticket or held.end != end:
                raise ConsistencyError("内存中的读取结束与当前轮可靠依据不一致")
        elif state.verification_state in (3, 5) and state.target_sha256 is not None:
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="read_end_unconfirmed")
    if (state.verification_state not in (3, 5) or state.target_sha256 is None) and ticket is None:
        run_id, run_status, attempts_used, max_attempts_used, retry_wait_required = (await _run_state(read_flow, copy_id))
        if run_status == int(_RUN_STATUS.FAILED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="run_ended")
        attempt_config = AttemptConfig(config.max_attempts, config.timeout_s, config.retry_interval_s)
        exhausted = await _exhaust_unknown_read_before_slot(
            read_flow, action_id=request.action_id, copy_id=copy_id,
            copy_round=state.round, config=attempt_config)
        if exhausted is not None:
            if exhausted.reason == "budget_exhausted":
                return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="run_ended")
            return InputStep(InputPhase.WAITING, copy_id=copy_id,
                             reason=exhausted.reason or "exhaustion_not_eligible")
        wait_key = f"read/{copy_id}"
        if read_flow.retry_gate.remaining(wait_key, attempts_used=attempts_used,
                retry_wait_required=retry_wait_required == 1,
                max_attempts_used=max_attempts_used,
                interval_s=config.retry_interval_s,
                now_ns=read_flow.monotonic_ns()) is not None:
            return InputStep(InputPhase.RETRY_WAITING, copy_id=copy_id)
        current = read_flow.occurred_at()
        selection = None if read_flow.capture_selection is None else read_flow.capture_selection(current)
        request_slot, slot_key = SlotRequest(copy_id, current, capture_selection=selection), new_operation_key()
        slot = await read_flow.database.write(lambda owned: copies.repository.grant_read_slot(request_slot, slot_key, owned), key=slot_key)
        if slot.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"分段检查读取机会未可靠保存: {slot.error}", slot)
        if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason=slot.value.reason)
        current = read_flow.occurred_at()
        selection = None if read_flow.capture_selection is None else read_flow.capture_selection(current)
        ticket, reason = (await __import__("camctl.capture.media_flow", fromlist=["acquire_read_attempt"]).acquire_read_attempt(
            read_flow, AttemptIntent("read", request.action_id, OperationKind.READ_FILE,
                AttemptTarget(copy_id=copy_id), None,
                attempt_config,
                current, copy_round=state.round, capture_selection=selection)))
        if ticket is None:
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason=reason)
    from camctl.outputs.read_attempts import persist_read_end
    sessions = read_flow.sessions if ticket is None else read_flow.sessions.for_attempt(
        ticket, idle_timeout_s=config.timeout_s, capture_selection=read_flow.capture_selection,
        continuations=read_flow.continuing_read_tickets, occurred_at=read_flow.occurred_at)
    context: InputContext
    context = InputContext(
        action_id=request.action_id, processing_id=None,
        source_device_file_id=source_file_id, target_extension=(Path(original_name).suffix[1:].lower()
            if original_name and Path(original_name).suffix else None),
        config=config, segment_size=read_flow.segment_size,
        staging=read_flow.roots.staging, copies=copies, sessions=sessions,
        occurred_at=read_flow.occurred_at(), digest=(await read_flow.digest_for(source_file_id)
            if read_flow.digest_for is not None else read_flow.digest),
        read_end=(read_flow.pending_read_ends[copy_id].end
            if ticket is not None and copy_id in read_flow.pending_read_ends else None),
        on_read_end=(None if ticket is None else lambda end, complete:
            persist_read_end(read_flow, ticket, end, complete,
                state=state, source_device_file_id=source_file_id, source_intermediate_file_id=None,
                resume=lambda current, held: _resume_local_internal_end(
                    current, held, read_flow, context), evidence=read_flow.evidence)),
        on_read_start=(None if ticket is None else
            lambda: _read_dispatched(read_flow, copy_id)),
        inspection_id=inspection_id)
    if ticket is not None and copy_id in read_flow.pending_read_ends:
        held = read_flow.pending_read_ends[copy_id]
        if held.resume is None:
            read_flow.pending_read_ends[copy_id] = replace(held, resume=lambda current, value:
                _resume_local_internal_end(current, value, read_flow, context))

    async def read_and_save(control):
        if ticket is not None and state.verification_state in (3, 5) and state.target_sha256 is not None:
            from camctl.outputs.read_attempts import current_local_read_end
            if not await current_local_read_end(read_flow, copy_id, read_flow.pending_read_ends[copy_id]):
                return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="read_end_changed")
        current = replace(context, sessions=(sessions if ticket is None else
            sessions.for_attempt(ticket, idle_timeout_s=config.timeout_s, control=control)))
        step = await obtain_recording_input(current)
        if ticket is not None:
            (await _finish_internal_read(read_flow, ticket, step, max_attempts=config.max_attempts,
                diagnostic_context={"action_id": request.action_id, "inspection_id": inspection_id,
                    "source_device_file_id": source_file_id}))
        return step

    result = await run_owned_read(read_flow, copy_id, read_and_save) if ticket is not None else await read_and_save(None)
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
    key = new_operation_key()
    created = await flow.database.write(lambda owned: repository.create(inspection_request, key, owned), key=key)
    if created.kind is not DbOutcomeKind.COMPLETED:
        if created.kind is DbOutcomeKind.ROLLED_BACK and isinstance(
            created.error, ConsistencyError
        ):
            raise created.error
        if any(not isinstance(error, asyncio.CancelledError) for error in db_outcome_errors(created)):
            record_error(db_failure(ConsistencyError, "分段检查登记未可靠完成", created),
                stage="capture.segment_create",
                context={"action_id": request.action_id, "member_id": request.member_id})
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            reason="inspection_create_unknown", error=created.error)
    ref = created.value
    if (not json_equal(ref.target_json, _target_document(request.target))
            or not json_equal(ref.budget_json, _budget_document(request.budget))):
        raise ConsistencyError("分段检查成员不能更换原目标或读取预算")
    copy_facts = (await flow.database.read(lambda owned, inputs=(request.action_id, request.member_id), options=dict(): read_inspection_for_member(inputs[0], inputs[1], owned, **options), description="读取拍摄直接事实")).copy
    copy_id = copy_facts.copy_id if copy_facts is not None else None

    # 已确定的 AttemptFinish 是第一责任：先沿原 ticket/key 保存，不能
    # 让后续的 inspection 合成结果遮住原读取诊断。
    pending_read_identities = []
    if copy_id is not None:
        from camctl.capture.media_flow import _save_internal_read_and_settle

        for pending_read in tuple(flow.pending_read_results.values()):
            ticket = getattr(getattr(pending_read, "finish", None), "ticket", None)
            if ticket is not None and int(ticket.target_id) == copy_id:
                identity = (ticket.run_id, ticket.attempt_id)
                pending_read_identities.append(identity)
                (await _save_internal_read_and_settle(flow, pending_read))

    # 原 inspection request/key 的未知回执先重送；它可能已在数据库
    # 可靠提交，只是本进程尚未收到回执。
    pending = flow.pending_segment_inspection_results.get(ref.inspection_id)
    if pending is not None and not (await _save_result(flow, pending[0])):
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            ref.inspection_id, copy_id, reason="result_save_unknown")

    # 若首次收场因检查结果 UNKNOWN 而保留了原 AttemptFinish，检查结果
    # 已由上面的同键重送核实后，再沿原结束键完成所属 READ 收尾。
    if pending_read_identities:
        from camctl.capture.media_flow import _save_internal_read_and_settle

        for identity in pending_read_identities:
            pending_read = flow.pending_read_results.get(identity)
            if pending_read is None:
                continue
            (await _save_internal_read_and_settle(flow, pending_read))
            if identity in flow.pending_read_results:
                return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                    ref.inspection_id, copy_id, reason="result_save_unknown")

    ref = (await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned)))
    if ref.state != int(_INSPECTION_STATE.PENDING):
        if copy_id is not None:
            (await settle_terminal_inspection_read_slot(flow, copy_id, flow.occurred_at()))
        return _step_from_ref(ref, copy_id)

    # 原完整 ReadEnd 的本地续接必须先于任何 run-ended 合成。
    (await retry_read_business(flow))
    await retry_read_ends(flow)

    pending = flow.pending_segment_inspection_results.get(ref.inspection_id)
    if pending is not None and not (await _save_result(flow, pending[0])):
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            ref.inspection_id, copy_id, reason="result_save_unknown")

    ref = (await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned)))
    if ref.state != int(_INSPECTION_STATE.PENDING):
        if copy_id is not None:
            (await settle_terminal_inspection_read_slot(flow, copy_id, flow.occurred_at()))
        return _step_from_ref(ref, copy_id)

    owner = (await flow.database.read(lambda owned, inputs=("SELECT status,cancel_requested FROM actions WHERE id=?", (request.action_id,)): owned.connection.execute(*inputs).fetchone(), description="读取拍摄直接事实"))
    if owner is None:
        raise ConsistencyError("分段检查的来源动作不存在")
    action_status = enum_for("actions.status")
    if owner[1] or owner[0] in tuple(
            int(action_status[name]) for name in ("SUCCEEDED", "FAILED", "EXPIRED", "CANCELED")):
        # Action6 的既有收场入口负责取消或动作终态下的检查；此处分
        # 类后不得重新建读取责任，也不得把它改写为读取预算耗尽。
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            ref.inspection_id, copy_id,
            reason="inspection_canceled" if owner[1] else "source_terminal")

    if copy_id is not None:
        _run_id, run_status, _used, _maximum, _wait = (await _run_state(flow, copy_id))
        if run_status == int(_RUN_STATUS.FAILED):
            saved = (await settle_inspection_read_failure(flow, copy_id, request.occurred_at))
            if not saved:
                return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                    ref.inspection_id, copy_id, reason="result_save_unknown")
            (await settle_terminal_inspection_read_slot(flow, copy_id, request.occurred_at))
            return _step_from_ref((await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned))), copy_id)

    source_file_id, original_name = (await flow.database.read(lambda owned, inputs=(request.action_id, request.member_id), options=dict(): _member_source(owned, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))
    input_step = await _obtain_input(flow, request, ref.inspection_id, source_file_id, original_name)
    # 摘要校验失败可能在 _finish_internal_read 中完成了读取与检查收场。
    # 未知回执时必须等待原 key 核实，不能因数据库已经可见终态而释放 slot。
    if ref.inspection_id in flow.pending_segment_inspection_results:
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
            ref.inspection_id, input_step.copy_id, reason="result_save_unknown")
    ref = (await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned)))
    if ref.state != int(_INSPECTION_STATE.PENDING):
        if input_step.copy_id is not None:
            (await settle_terminal_inspection_read_slot(flow, input_step.copy_id, flow.occurred_at()))
        return _step_from_ref(ref, input_step.copy_id)
    if input_step.phase is not InputPhase.INPUT_READY or input_step.input_file is None:
        if input_step.phase is InputPhase.QUALIFY_REJECTED and input_step.error is not None:
            return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
                input_step.copy_id, reason="qualification_waiting", error=input_step.error)
        if input_step.phase is InputPhase.WAITING and input_step.reason == "run_ended":
            if input_step.copy_id is None:
                raise ConsistencyError("已结束分段检查读取缺少拷贝身份")
            saved = (await settle_inspection_read_failure(
                flow, input_step.copy_id, request.occurred_at))
            if saved:
                (await settle_terminal_inspection_read_slot(
                    flow, input_step.copy_id, request.occurred_at))
                return _step_from_ref((await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned))),
                                      input_step.copy_id)
            return SegmentInspectionStep(SegmentInspectionPhase.WAITING,
                ref.inspection_id, input_step.copy_id, reason="result_save_unknown")
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
            input_step.copy_id, reason=input_step.reason or input_step.phase.value,
            error=input_step.error)

    from camctl.persistence.repositories.segment_inspections import inspection_input_read_succeeded
    input_read_finished = await flow.database.read(lambda owned: inspection_input_read_succeeded(
        owned.connection, {"id": ref.inspection_id, "action_id": ref.action_id,
                           "source_device_file_id": ref.source_device_file_id}))
    if not input_read_finished:
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
            input_step.copy_id, reason="read_result_unconfirmed")

    count_frames = request.target.metric is CoverageMetric.FRAMES
    probe_call = getattr(flow.tools, "probe_segment", None)
    if probe_call is None:
        raise ConsistencyError("受管媒体工具缺少显式逐段探测端口 probe_segment")
    try:
        result = await probe_call(input_step.input_file, count_frames=count_frames)
    except Exception as error:
        record_error(error, stage="capture.segment_probe",
            context={"action_id": request.action_id, "inspection_id": ref.inspection_id,
                     "member_id": request.member_id, "copy_id": input_step.copy_id})
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
        if result.ran and (result.error is not None or not isinstance(result.value, MediaProbe)
                           or metric.value is None or result.value.error is not None) and not (
                isinstance(result.value, MediaProbe) and result.value.tool_diagnostics is not None
                and result.value.tool_diagnostics.control_only):
            record_error(result.error or {"reason": metric.reason, "probe": result.value},
                stage="capture.segment_probe",
                context={"action_id": request.action_id, "inspection_id": ref.inspection_id,
                         "member_id": request.member_id, "copy_id": input_step.copy_id})
    saved = (await _save_result(flow, InspectionResultSave(ref.inspection_id, int(state),
        document, flow.occurred_at())))
    if not saved:
        return SegmentInspectionStep(SegmentInspectionPhase.WAITING, ref.inspection_id,
            input_step.copy_id, metric=metric, input_file=input_step.input_file,
            reason="result_save_unknown")
    return _step_from_ref((await flow.database.read(lambda owned: repository.read(ref.inspection_id, owned))), input_step.copy_id)
