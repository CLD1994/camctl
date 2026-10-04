"""异常录像检查与内部修复的决策与执行编排。

修复门槛 = duration_s + 固定余量秒数 T；可信计时严格超过门槛才
触发修复，恰好相等不触发。仅下界证据时下界超门槛即确认多录，
未超不能证明无需修复；计时不足按未知进入原片检查。修复决定只
表达处理分支，不替代采集结果；源文件停止与写完确认前不读取。

执行编排经端口驱动保存与受管工具：意图先保存再执行，提交未知
不推进，任务未取得观察不保存终态结论；probe 工具失败按检查失败
终态，工具正常但未取得可靠时长按未确认终态。修复输出文件先登
记路径和责任再写入，完整字节与成功终态同事务固定。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from camctl.capture.processing import (
    CheckResultSave,
    CheckPhase,
    MediaObservation,
    ProcessingDisposition,
    ProcessingError,
    RepairDecisionSave,
    RepairDecisionChoice,
    RepairOutcome,
    RepairOutputFile,
    RepairResultSave,
    RepairReason,
    RepairStart,
    RepairSuccess,
    repair_basis_from_check,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId
from camctl.host_files.media import MediaArtifact, MediaProbe
from camctl.host_files.models import FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskResult

__all__ = [
    "CheckContext",
    "CheckExecutionPhase",
    "CheckStep",
    "MediaDecision",
    "MediaKind",
    "MediaPolicy",
    "MediaTools",
    "ProcessingSaves",
    "ProcessingStatus",
    "RecordingEvidence",
    "RepairContext",
    "RepairExecutionPhase",
    "RepairStep",
    "SaveDisposition",
    "SaveReceipt",
    "check_observation_from_probe",
    "decide_media_processing",
    "execute_check",
    "execute_repair",
    "repair_decision_from_check",
    "repair_error_from_artifact",
]


class MediaKind(Enum):
    """媒体文件的用途身份；原片、修复输入、临时输出与成品分开。"""

    ORIGINAL = "original"
    RECORDING_INPUT = "recording_input"
    TEMP_OUTPUT = "temp_output"
    REPAIRED = "repaired"


@dataclass(frozen=True)
class MediaPolicy:
    """修复余量：固定秒数，不随目标时长按比例变化。"""

    repair_margin_s: Decimal

    def __post_init__(self) -> None:
        value = self.repair_margin_s
        if isinstance(value, float):
            try:
                value = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError(f"余量必须是非负有限秒数: {self.repair_margin_s!r}") from error
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError(f"余量必须是非负有限秒数: {self.repair_margin_s!r}")
        object.__setattr__(self, "repair_margin_s", value)


@dataclass(frozen=True)
class RecordingEvidence:
    """修复判定的已保存证据。

    confirmed_recorded_s 是同段连续录像的已确认录制时长；
    recorded_lower_bound_s 仅为下界证据。两者互斥提供。
    """

    recording_completed_normally: bool
    recording_abnormal: bool
    duration_s: Decimal
    confirmed_recorded_s: Decimal | None
    recorded_lower_bound_s: Decimal | None
    timing_insufficient: bool
    stop_confirmed: bool
    source_file_complete: bool
    facts_readable: bool


class MediaDecision(Enum):
    """媒体处理分支；不携带采集成功结论。"""

    NORMAL_COMPLETION = "normal_completion"
    REPAIR_REQUIRED = "repair_required"
    NO_REPAIR_WITHIN_THRESHOLD = "no_repair_within_threshold"
    TIMING_UNKNOWN_CHECK_ORIGINAL = "timing_unknown_check_original"
    WAIT_STOP_AND_FILE = "wait_stop_and_file"
    CONFIG_ERROR = "config_error"


def decide_media_processing(
    evidence: RecordingEvidence, policy: MediaPolicy
) -> MediaDecision:
    """按多录门槛与计时证据决定处理分支。"""
    if not evidence.facts_readable:
        return MediaDecision.CONFIG_ERROR
    if evidence.recording_completed_normally and not evidence.recording_abnormal:
        return MediaDecision.NORMAL_COMPLETION
    threshold = evidence.duration_s + policy.repair_margin_s
    if evidence.confirmed_recorded_s is not None:
        if evidence.confirmed_recorded_s > threshold:
            return _gate_repair_start(evidence)
        return MediaDecision.NO_REPAIR_WITHIN_THRESHOLD
    if evidence.recorded_lower_bound_s is not None:
        if evidence.recorded_lower_bound_s > threshold:
            return _gate_repair_start(evidence)
        return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL
    if evidence.timing_insufficient:
        return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL
    return MediaDecision.TIMING_UNKNOWN_CHECK_ORIGINAL


def _gate_repair_start(evidence: RecordingEvidence) -> MediaDecision:
    """已确认多录：停止及源文件写完确认前不读取仍在写入的文件。"""
    if evidence.stop_confirmed and evidence.source_file_complete:
        return MediaDecision.REPAIR_REQUIRED
    return MediaDecision.WAIT_STOP_AND_FILE


# ---- 执行编排：观察与决定的换算 ----

_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")

#: 工具正常结束但未取得可靠时长的分类：核验未确认，不是工具失败。
_UNCONFIRMED_PROBE_CODES = frozenset({"missing_duration", "invalid_structure"})


def _error_from_message(stage: str, message: str) -> ProcessingError:
    """从工具诊断文本提取分类码；完整消息保留在 details 中。"""
    code = message.split(":", 1)[0].strip()
    if not code:
        code = f"{stage}_failed"
    return ProcessingError(code=code, stage=stage, details={"message": message})


def check_observation_from_probe(probe: MediaProbe) -> MediaObservation:
    """把 probe 实际结果换算为公共媒体观察。

    工具失败按检查失败终态保存；工具正常结束但未取得可靠时长按
    未确认终态保存，时长判定保持未知，不猜测为零或足够。
    """
    if probe.duration_s is not None:
        return MediaObservation(CheckPhase.COMPLETED, duration_s=probe.duration_s)
    if probe.error is None:
        raise ValueError("probe 结果缺少时长与错误，不能构成媒体观察")
    error = _error_from_message("probe", probe.error)
    phase = (CheckPhase.UNCONFIRMED
             if error.code in _UNCONFIRMED_PROBE_CODES else CheckPhase.FAILED)
    return MediaObservation(phase, error=error)


def repair_decision_from_check(
    observation: MediaObservation, policy: MediaPolicy,
    target_duration_ms: int, processing_id: int, occurred_at: int,
) -> RepairDecisionSave | None:
    """检查完成才派生修复决定；门槛判定与检查分类共用同一规则。"""
    if observation.phase is not CheckPhase.COMPLETED or observation.duration_s is None:
        return None
    basis = repair_basis_from_check(
        duration_s=Decimal(target_duration_ms) / 1000,
        repair_margin_s=policy.repair_margin_s,
        observed_s=observation.duration_s,
        target_duration_ms=target_duration_ms,
    )
    decision = (RepairDecisionChoice.PENDING
                if basis.reason is RepairReason.THRESHOLD_REACHED
                else RepairDecisionChoice.NOT_NEEDED)
    return RepairDecisionSave(processing_id, decision, basis, occurred_at)


def repair_error_from_artifact(artifact: MediaArtifact) -> ProcessingError:
    """把不完整成品换算为修复失败错误，保留各阶段的实际事实。"""
    if artifact.tool_error is not None:
        return _error_from_message("repair", artifact.tool_error)
    return ProcessingError(
        code="artifact_incomplete", stage="repair",
        details={"message": artifact.error or "artifact_incomplete"})


# ---- 执行编排：端口与事实 ----


class SaveDisposition(Enum):
    """编排保存端口调用的实际结果分类。"""

    SAVED = "saved"
    ALREADY = "already"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SaveReceipt:
    """一次保存端口调用的结果；value 携带保存返回的业务结果。"""

    disposition: SaveDisposition
    value: Any = None
    error: BaseException | None = None


def _state_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数: {value!r}")
    return value


@dataclass(frozen=True)
class ProcessingStatus:
    """编排所需的当前处理事实；由调用方从当前投影装载。

    check_duration_s 只在检查完成时有值，来自已保存 media_json；
    repair_output_file_id 与 repair_output_path 须同时提供或同为空。
    """

    processing_id: int
    check_decision: int
    check_state: int
    check_duration_s: Decimal | None
    target_duration_ms: int
    repair_state: int
    repair_output_file_id: int | None
    repair_output_path: str | None

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        for name in ("check_decision", "check_state", "repair_state"):
            _state_int(name, getattr(self, name))
        _state_int("target_duration_ms", self.target_duration_ms)
        if self.target_duration_ms <= 0:
            raise ValueError(f"目标时长毫秒必须是正整数: {self.target_duration_ms!r}")
        if self.check_duration_s is not None:
            value = self.check_duration_s
            if isinstance(value, bool) or not isinstance(value, Decimal):
                raise TypeError(f"已保存时长必须是精确十进制数: {value!r}")
            if not value.is_finite() or value < 0:
                raise ValueError(f"已保存时长必须是非负有限秒数: {value!r}")
        if (self.repair_output_file_id is None) != (self.repair_output_path is None):
            raise ValueError("修复输出身份与登记路径必须同时提供或同为空")
        if self.repair_output_file_id is not None:
            ObjectId(self.repair_output_file_id)
        if self.repair_output_path is not None and (
                not isinstance(self.repair_output_path, str)
                or not self.repair_output_path):
            raise ValueError(
                f"修复输出登记路径必须是非空文本: {self.repair_output_path!r}")


class MediaTools(Protocol):
    """受管媒体工具端口；适配层持有执行器、任务身份与工具参数。"""

    async def probe(self, input: FileRef) -> FileTaskResult: ...

    async def repair(self, input: FileRef, output: FileRef) -> FileTaskResult: ...


class ProcessingSaves(Protocol):
    """录像处理事务端口；每次调用独立提交并返回实际结果。"""

    def save_check_result(self, command: CheckResultSave) -> SaveReceipt: ...

    def save_repair_decision(self, command: RepairDecisionSave) -> SaveReceipt: ...

    def save_repair_result(self, command: RepairResultSave) -> SaveReceipt: ...

    def start_repair_output(self, command: RepairStart) -> SaveReceipt: ...

    def complete_repair_output(self, command: RepairSuccess) -> SaveReceipt: ...


# ---- 检查执行编排 ----


class CheckExecutionPhase(Enum):
    """检查编排的结果分区。"""

    NOT_REQUIRED = "not_required"
    ALREADY_FINISHED = "already_finished"
    FINISHED_DECISION_SAVED = "finished_decision_saved"
    START_REJECTED = "start_rejected"
    START_UNKNOWN = "start_unknown"
    PROBE_NOT_COMPLETED = "probe_not_completed"
    RESULT_REJECTED = "result_rejected"
    RESULT_UNKNOWN = "result_unknown"
    DECISION_REJECTED = "decision_rejected"
    DECISION_UNKNOWN = "decision_unknown"
    CHECK_TERMINAL = "check_terminal"
    CHECK_COMPLETED = "check_completed"


@dataclass(frozen=True)
class CheckStep:
    """一次检查编排的结果：阶段、取得的观察与修复决定输入。"""

    phase: CheckExecutionPhase
    media: MediaObservation | None = None
    repair_decision: RepairDecisionSave | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class CheckContext:
    """一次检查执行的输入：当前事实、输入副本、端口与策略。"""

    processing: ProcessingStatus
    input_file: FileRef
    policy: MediaPolicy
    tools: MediaTools
    saves: ProcessingSaves
    occurred_at: int


async def execute_check(context: CheckContext) -> CheckStep:
    """执行原片检查：先保存运行阶段，再运行工具并保存观察。

    检查决定不是需要检查、或检查已终态时按恢复入口处理；运行阶
    段保存被拒或未知时不启动工具；任务未取得观察不保存终态结论。
    """
    status = context.processing
    if status.check_decision != int(_CHECK_DECISION.REQUIRED):
        return CheckStep(CheckExecutionPhase.NOT_REQUIRED)
    if status.check_state != int(_CHECK_STATE.NOT_PERFORMED):
        if status.check_state != int(_CHECK_STATE.RUNNING):
            return _resume_finished_check(context)
    else:
        started = context.saves.save_check_result(CheckResultSave(
            status.processing_id, MediaObservation(CheckPhase.RUNNING),
            context.occurred_at))
        if started.disposition is SaveDisposition.REJECTED:
            return CheckStep(CheckExecutionPhase.START_REJECTED,
                             error=started.error)
        if started.disposition is SaveDisposition.UNKNOWN:
            return CheckStep(CheckExecutionPhase.START_UNKNOWN,
                             error=started.error)

    result = await context.tools.probe(context.input_file)
    if not result.ran or result.value is None:
        detail = result.error or "probe task did not run"
        return CheckStep(CheckExecutionPhase.PROBE_NOT_COMPLETED,
                         error=RuntimeError(detail))
    observation = check_observation_from_probe(result.value)
    saved = context.saves.save_check_result(CheckResultSave(
        status.processing_id, observation, context.occurred_at))
    if saved.disposition is SaveDisposition.REJECTED:
        return CheckStep(CheckExecutionPhase.RESULT_REJECTED, media=observation,
                         error=saved.error)
    if saved.disposition is SaveDisposition.UNKNOWN:
        return CheckStep(CheckExecutionPhase.RESULT_UNKNOWN, media=observation,
                         error=saved.error)
    return _fix_repair_decision(context, observation)


def _resume_finished_check(context: CheckContext) -> CheckStep:
    """检查已终态的恢复入口：只有完成且决定未固定时补固定决定。"""
    status = context.processing
    if (status.check_state == int(_CHECK_STATE.COMPLETED)
            and status.check_duration_s is not None
            and status.repair_state == int(_REPAIR_STATE.UNDETERMINED)):
        observation = MediaObservation(
            CheckPhase.COMPLETED, duration_s=status.check_duration_s)
        step = _fix_repair_decision(context, observation)
        if step.phase is CheckExecutionPhase.CHECK_COMPLETED:
            return replace(step, phase=CheckExecutionPhase.FINISHED_DECISION_SAVED)
        return step
    return CheckStep(CheckExecutionPhase.ALREADY_FINISHED)


def _fix_repair_decision(context: CheckContext,
                         observation: MediaObservation) -> CheckStep:
    """检查完成后固定修复决定；决定保存失败保留已取得的观察。"""
    status = context.processing
    decision = repair_decision_from_check(
        observation, context.policy, status.target_duration_ms,
        status.processing_id, context.occurred_at)
    if decision is None:
        return CheckStep(CheckExecutionPhase.CHECK_TERMINAL, media=observation)
    saved = context.saves.save_repair_decision(decision)
    if saved.disposition is SaveDisposition.REJECTED:
        return CheckStep(CheckExecutionPhase.DECISION_REJECTED, media=observation,
                         repair_decision=decision, error=saved.error)
    if saved.disposition is SaveDisposition.UNKNOWN:
        return CheckStep(CheckExecutionPhase.DECISION_UNKNOWN, media=observation,
                         repair_decision=decision, error=saved.error)
    return CheckStep(CheckExecutionPhase.CHECK_COMPLETED, media=observation,
                     repair_decision=decision)


# ---- 修复执行编排 ----


class RepairExecutionPhase(Enum):
    """修复编排的结果分区。"""

    NOT_PENDING = "not_pending"
    ALREADY_FINISHED = "already_finished"
    START_REJECTED = "start_rejected"
    START_UNKNOWN = "start_unknown"
    TOOL_NOT_COMPLETED = "tool_not_completed"
    COMPLETE_REJECTED = "complete_rejected"
    COMPLETE_UNKNOWN = "complete_unknown"
    FAILED_SAVED = "failed_saved"
    FAILED_REJECTED = "failed_rejected"
    FAILED_UNKNOWN = "failed_unknown"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class RepairStep:
    """一次修复编排的结果：阶段与已登记的修复输出文件。"""

    phase: RepairExecutionPhase
    output_file: RepairOutputFile | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class RepairContext:
    """一次修复执行的输入：当前事实、输入副本与输出扩展名。"""

    processing: ProcessingStatus
    input_file: FileRef
    extension: str | None
    tools: MediaTools
    saves: ProcessingSaves
    occurred_at: int


async def execute_repair(context: RepairContext) -> RepairStep:
    """执行多录修复：登记输出与运行同事务，工具完成后固定结果。

    恢复入口沿用已登记输出续执行；输出登记保存被拒或未知时不启
    动工具；任务未取得成品不保存终态结论。完整成品按字节事实与
    成功同事务固定，不完整成品保存失败终态与错误结构。
    """
    status = context.processing
    if status.repair_state == int(_REPAIR_STATE.PENDING):
        started = context.saves.start_repair_output(RepairStart(
            status.processing_id, context.extension, context.occurred_at))
        if started.disposition is SaveDisposition.REJECTED:
            return RepairStep(RepairExecutionPhase.START_REJECTED,
                              error=started.error)
        if started.disposition is SaveDisposition.UNKNOWN:
            return RepairStep(RepairExecutionPhase.START_UNKNOWN,
                              error=started.error)
        output = started.value
        if not isinstance(output, RepairOutputFile):
            raise TypeError(f"修复输出登记缺少文件结果: {output!r}")
    elif status.repair_state == int(_REPAIR_STATE.RUNNING):
        if status.repair_output_file_id is None or status.repair_output_path is None:
            raise ValueError(
                "修复处于执行中，但缺少已登记的修复输出文件，恢复事实不一致")
        output = RepairOutputFile(
            ProcessingDisposition.SAVED,
            status.repair_output_file_id, status.repair_output_path)
    elif status.repair_state in (
            int(_REPAIR_STATE.SUCCEEDED), int(_REPAIR_STATE.FAILED),
            int(_REPAIR_STATE.CANCELED)):
        return RepairStep(RepairExecutionPhase.ALREADY_FINISHED)
    else:
        return RepairStep(RepairExecutionPhase.NOT_PENDING)

    output_ref = FileRef(output.file_id, FilePurpose.REPAIR_OUTPUT,
                         output.relative_path, context.input_file.root)
    result = await context.tools.repair(context.input_file, output_ref)
    if not result.ran or result.value is None:
        detail = result.error or "repair task did not run"
        return RepairStep(RepairExecutionPhase.TOOL_NOT_COMPLETED,
                          output_file=output, error=RuntimeError(detail))
    artifact = result.value
    if artifact.complete:
        return _complete_repair(context, output, output_ref, artifact)
    return _save_repair_failure(context, output, artifact)


def _complete_repair(context: RepairContext, output: RepairOutputFile,
                     output_ref: FileRef, artifact: MediaArtifact) -> RepairStep:
    completed = context.saves.complete_repair_output(RepairSuccess(
        context.processing.processing_id, output_ref.file_id,
        artifact.size_bytes, artifact.digest, context.occurred_at))
    if completed.disposition is SaveDisposition.REJECTED:
        return RepairStep(RepairExecutionPhase.COMPLETE_REJECTED,
                          output_file=output, error=completed.error)
    if completed.disposition is SaveDisposition.UNKNOWN:
        return RepairStep(RepairExecutionPhase.COMPLETE_UNKNOWN,
                          output_file=output, error=completed.error)
    return RepairStep(RepairExecutionPhase.SUCCEEDED, output_file=output)


def _save_repair_failure(context: RepairContext, output: RepairOutputFile,
                         artifact: MediaArtifact) -> RepairStep:
    saved = context.saves.save_repair_result(RepairResultSave(
        context.processing.processing_id, RepairOutcome.FAILED,
        context.occurred_at, error=repair_error_from_artifact(artifact)))
    if saved.disposition is SaveDisposition.REJECTED:
        return RepairStep(RepairExecutionPhase.FAILED_REJECTED,
                          output_file=output, error=saved.error)
    if saved.disposition is SaveDisposition.UNKNOWN:
        return RepairStep(RepairExecutionPhase.FAILED_UNKNOWN,
                          output_file=output, error=saved.error)
    return RepairStep(RepairExecutionPhase.FAILED_SAVED, output_file=output)
