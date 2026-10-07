"""C8 检查与修复执行编排的单元测试。

probe 结果换算公共媒体观察，检查完成派生修复决定；编排经端口替
身验证保存次序、失败分区与恢复入口：意图先保存、提交未知不推
进、任务未取得观察不保存终态结论、修复输出登记先于工具执行。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from camctl.capture.media import (
    CheckContext,
    CheckExecutionPhase,
    MediaPolicy,
    ProcessingStatus,
    RepairContext,
    RepairExecutionPhase,
    SaveDisposition,
    SaveReceipt,
    check_observation_from_probe,
    execute_check,
    execute_repair,
    repair_decision_from_check,
    repair_error_from_artifact,
)
from camctl.capture.processing import (
    CheckDecisionChoice,
    CheckPhase,
    CheckReason,
    MediaObservation,
    ProcessingDisposition,
    ProcessingError,
    RepairDecisionChoice,
    RepairOutcome,
    RepairReason,
    RepairStart,
    RepairSuccess,
    RepairOutputFile,
    saved_check_duration,
    saved_target_duration_ms,
)
from camctl.host_files.io import DirectorySyncStage, HashResult, SyncResult
from camctl.host_files.media import MediaArtifact, MediaProbe
from camctl.host_files.models import (
    FileObservation,
    FileObservationKind,
    FilePurpose,
    FileRef,
)
from camctl.host_files.tasks import FileTaskId, FileTaskResult


_NOW = 1_750_000_000_000_000
_STAGING = Path("staging")
_POLICY = MediaPolicy(repair_margin_s=Decimal("10"))
_TARGET_MS = 60_000


# ---- 检查观察换算 ----


def test_probe_duration_becomes_completed_observation() -> None:
    observation = check_observation_from_probe(
        MediaProbe(duration_s=Decimal("75.125"), error=None))
    assert observation.phase is CheckPhase.COMPLETED
    assert observation.duration_s == Decimal("75.125")
    assert observation.error is None
    assert observation.as_json() == {
        "check_status": "completed",
        "duration": {"status": "available", "seconds": Decimal("75.125")},
    }


@pytest.mark.parametrize("error", [
    "tool_failed: exit=1",
    "tool_unavailable: 启动失败",
    "tool_cancelled: 工具尚未启动，停止请求已生效",
    "output_failed: 管道读取失败",
])
def test_probe_tool_error_is_failed_check(error: str) -> None:
    observation = check_observation_from_probe(
        MediaProbe(duration_s=None, error=error))
    assert observation.phase is CheckPhase.FAILED
    assert observation.duration_s is None
    assert observation.error is not None
    assert observation.error.code == error.split(":", 1)[0]
    assert observation.error.stage == "probe"
    assert observation.error.details["message"] == error


@pytest.mark.parametrize("error", [
    "missing_duration: 缺少时长字段",
    "invalid_structure: 时长不是数字",
    "invalid_structure: 输出缺少流信息段",
    "no_video_stream: 容器没有视频流",
])
def test_unreliable_duration_is_unconfirmed_check(error: str) -> None:
    """工具正常结束但未取得可靠时长：核验未确认，时长判定未知。

    无视频流的容器不提供视频时长事实（容器时长语义只接受有视频
    流的容器时长），与缺少时长同样按未确认分类不补造。
    """
    observation = check_observation_from_probe(
        MediaProbe(duration_s=None, error=error))
    assert observation.phase is CheckPhase.UNCONFIRMED
    assert observation.duration_s is None
    assert observation.error is not None
    assert observation.error.code == error.split(":", 1)[0]


# ---- 修复决定派生 ----


def _completed(observed: str) -> MediaObservation:
    return MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal(observed))


def _terminal_error() -> ProcessingError:
    return ProcessingError(code="tool_failed", stage="probe", details={})


def test_over_threshold_check_fixes_pending_repair() -> None:
    decision = repair_decision_from_check(
        _completed("75.125"), _POLICY, _TARGET_MS, 5, _NOW)
    assert decision is not None
    assert decision.decision is RepairDecisionChoice.PENDING
    assert decision.basis.reason is RepairReason.THRESHOLD_REACHED
    assert decision.basis.threshold_s == Decimal("70")
    assert decision.basis.actual_duration_s == Decimal("75.125")
    assert decision.basis.target_duration_ms == _TARGET_MS


@pytest.mark.parametrize("observed", ["70", "65", "60", "30"])
def test_within_or_short_check_needs_no_repair(observed: str) -> None:
    """恰好达到门槛不触发修复；区间内与原片不足同样不修复。"""
    decision = repair_decision_from_check(
        _completed(observed), _POLICY, _TARGET_MS, 5, _NOW)
    assert decision is not None
    assert decision.decision is RepairDecisionChoice.NOT_NEEDED
    assert decision.basis.reason is RepairReason.BELOW_THRESHOLD


@pytest.mark.parametrize("observation", [
    MediaObservation(CheckPhase.FAILED, error=_terminal_error()),
    MediaObservation(CheckPhase.UNCONFIRMED, error=_terminal_error()),
    MediaObservation(CheckPhase.RUNNING),
])
def test_uncompleted_check_derives_no_repair_decision(observation) -> None:
    assert repair_decision_from_check(
        observation, _POLICY, _TARGET_MS, 5, _NOW) is None


# ---- 修复成品错误换算 ----


def _artifact(*, tool_error: str | None, digest: str | None = "c" * 64) -> MediaArtifact:
    observation = FileObservation(
        FileObservationKind.VALID_OBJECT, Path("staging/derived/21.mp4"),
        is_file=True, size_bytes=2048)
    return MediaArtifact(
        tool_error=tool_error,
        observation=observation,
        checksum=None if digest is None else HashResult(
            digest=digest, size_bytes=2048, error=None),
        synchronization=SyncResult(True, DirectorySyncStage.SYNCED, None),
    )


def test_tool_failure_error_keeps_category() -> None:
    error = repair_error_from_artifact(
        _artifact(tool_error="tool_failed: exit=1"))
    assert error.code == "tool_failed"
    assert error.stage == "repair"
    assert "tool_failed" in error.details["message"]


def test_incomplete_artifact_error_reports_artifact() -> None:
    """工具成功但成品不完整：错误按成品不完整分类并保留各阶段事实。"""
    error = repair_error_from_artifact(_artifact(tool_error=None, digest=None))
    assert error.code == "artifact_incomplete"
    assert error.stage == "repair"
    assert error.details["message"]


# ---- 已保存检查事实的读取 ----


def test_saved_check_duration_reads_available_value() -> None:
    assert saved_check_duration({
        "check_status": "completed",
        "duration": {"status": "available", "seconds": Decimal("75.125")},
    }) == Decimal("75.125")


@pytest.mark.parametrize("document", [
    {"check_status": "completed", "duration": {"status": "unknown"}},
    {"check_status": "failed", "duration": {"status": "unknown"},
     "error": {"code": "tool_failed", "stage": "probe", "details": {}}},
    {"check_status": "not_performed", "duration": {"status": "unknown"}},
])
def test_saved_check_duration_is_none_without_reliable_value(document) -> None:
    assert saved_check_duration(document) is None


@pytest.mark.parametrize("document", [
    {}, {"check_status": "completed"}, {"check_status": "completed", "duration": {}},
])
def test_saved_check_duration_rejects_malformed_document(document) -> None:
    with pytest.raises(ValueError):
        saved_check_duration(document)


def test_saved_target_duration_reads_basis() -> None:
    basis = {
        "reason": CheckReason.INSUFFICIENT_TIMING.value,
        "target_duration_ms": 60_000,
    }
    assert saved_target_duration_ms(basis) == 60_000


@pytest.mark.parametrize("basis", [{}, {"reason": 2}, {"target_duration_ms": "x"}])
def test_saved_target_duration_rejects_invalid_basis(basis) -> None:
    with pytest.raises(ValueError):
        saved_target_duration_ms(basis)


# ---- 编排替身 ----


def _input_ref() -> FileRef:
    return FileRef(11, FilePurpose.RECORDING_INPUT,
                   "recording-inputs/11.mp4", _STAGING)


def _status(**overrides: Any) -> ProcessingStatus:
    values: dict[str, Any] = {
        "processing_id": 1,
        "check_decision": 3,
        "check_state": 1,
        "check_duration_s": None,
        "target_duration_ms": _TARGET_MS,
        "repair_state": 1,
        "repair_output_file_id": None,
        "repair_output_path": None,
    }
    values.update(overrides)
    return ProcessingStatus(**values)


def _saved(value: Any = None) -> SaveReceipt:
    return SaveReceipt(SaveDisposition.SAVED, value=value)


def _receipt(disposition: SaveDisposition) -> SaveReceipt:
    return SaveReceipt(disposition, error=RuntimeError("db"))


class _Saves:
    """按脚本回执的保存替身；记录调用供次序断言。"""

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, Any]] = []

    def _dispatch(self, name: str, command: Any) -> SaveReceipt:
        self.calls.append((name, command))
        receipt = self.script.get(name, _saved())
        return receipt(command) if callable(receipt) else receipt

    def save_check_result(self, command):
        return self._dispatch("save_check_result", command)

    def save_repair_decision(self, command):
        return self._dispatch("save_repair_decision", command)

    def save_repair_result(self, command):
        return self._dispatch("save_repair_result", command)

    def start_repair_output(self, command):
        return self._dispatch("start_repair_output", command)

    def complete_repair_output(self, command):
        return self._dispatch("complete_repair_output", command)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _Tools:
    """媒体工具替身：记录调用并返回脚本结果。"""

    def __init__(self, probe_result, repair_result) -> None:
        self.probe_result = probe_result
        self.repair_result = repair_result
        self.probe_calls: list[FileRef] = []
        self.repair_calls: list[tuple[FileRef, FileRef, Decimal]] = []

    async def probe(self, input: FileRef) -> FileTaskResult:
        self.probe_calls.append(input)
        return self.probe_result

    async def repair(self, input: FileRef, output: FileRef,
                     *, trim_s: Decimal) -> FileTaskResult:
        self.repair_calls.append((input, output, trim_s))
        return self.repair_result


def _probe_result(probe: MediaProbe) -> FileTaskResult:
    return FileTaskResult(FileTaskId("probe-1"), ran=True, value=probe)


_REGISTERED_OUTPUT = RepairOutputFile(
    disposition=ProcessingDisposition.SAVED,
    file_id=21,
    relative_path="derived/21.mp4",
)

_OUTPUT_REF = FileRef(21, FilePurpose.REPAIR_OUTPUT, "derived/21.mp4", _STAGING)


def _check_context(status: ProcessingStatus, saves: _Saves, tools: _Tools) -> CheckContext:
    return CheckContext(
        processing=status, input_file=_input_ref(), policy=_POLICY,
        tools=tools, saves=saves, occurred_at=_NOW)


def _repair_context(status: ProcessingStatus, saves: _Saves, tools: _Tools,
                    extension: str | None = "mp4") -> RepairContext:
    return RepairContext(
        processing=status, input_file=_input_ref(), extension=extension,
        tools=tools, saves=saves, occurred_at=_NOW)


# ---- execute_check ----


@pytest.mark.asyncio
async def test_check_saves_running_then_result_and_decision() -> None:
    saves = _Saves()
    tools = _Tools(_probe_result(MediaProbe(Decimal("75.125"), None)), None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is CheckExecutionPhase.CHECK_COMPLETED
    assert saves.names() == [
        "save_check_result", "save_check_result", "save_repair_decision"]
    running, result, decision = saves.calls
    assert running[1].phase is CheckPhase.RUNNING
    assert result[1].media.phase is CheckPhase.COMPLETED
    assert decision[1].decision is RepairDecisionChoice.PENDING
    assert step.media is not None and step.media.phase is CheckPhase.COMPLETED
    assert step.repair_decision is decision[1]
    assert tools.probe_calls == [_input_ref()]


@pytest.mark.asyncio
async def test_check_resumes_from_running_without_restart() -> None:
    saves = _Saves()
    tools = _Tools(_probe_result(MediaProbe(Decimal("65"), None)), None)
    step = await execute_check(_check_context(_status(check_state=2), saves, tools))
    assert step.phase is CheckExecutionPhase.CHECK_COMPLETED
    assert saves.names() == ["save_check_result", "save_repair_decision"]
    assert saves.calls[0][1].media.phase is CheckPhase.COMPLETED


@pytest.mark.asyncio
async def test_check_recovery_fixes_decision_from_saved_duration() -> None:
    """检查已保存完成而修复决定未固定：仅从已保存时长补固定决定。"""
    saves = _Saves()
    tools = _Tools(None, None)
    status = _status(check_state=3, check_duration_s=Decimal("75.125"))
    step = await execute_check(_check_context(status, saves, tools))
    assert step.phase is CheckExecutionPhase.FINISHED_DECISION_SAVED
    assert saves.names() == ["save_repair_decision"]
    assert saves.calls[0][1].decision is RepairDecisionChoice.PENDING
    assert tools.probe_calls == []


@pytest.mark.parametrize("status", [
    _status(check_state=3, repair_state=3),
    _status(check_state=4),
    _status(check_state=5),
])
@pytest.mark.asyncio
async def test_terminal_check_without_pending_work_is_finished(status) -> None:
    """检查已终态：失败与未确认没有可派生的决定，重复进入不保存。"""
    saves = _Saves()
    step = await execute_check(_check_context(status, saves, _Tools(None, None)))
    assert step.phase is CheckExecutionPhase.ALREADY_FINISHED
    assert saves.names() == []


@pytest.mark.asyncio
async def test_check_not_required_is_rejected() -> None:
    saves = _Saves()
    step = await execute_check(_check_context(
        _status(check_decision=2), saves, _Tools(None, None)))
    assert step.phase is CheckExecutionPhase.NOT_REQUIRED
    assert saves.names() == []
    assert CheckDecisionChoice.NOT_NEEDED.value == 2


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, CheckExecutionPhase.START_REJECTED),
    (SaveDisposition.UNKNOWN, CheckExecutionPhase.START_UNKNOWN),
])
@pytest.mark.asyncio
async def test_check_does_not_probe_before_saved_running(disposition, phase) -> None:
    saves = _Saves({"save_check_result": _receipt(disposition)})
    tools = _Tools(_probe_result(MediaProbe(Decimal("75.125"), None)), None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is phase
    assert step.error is not None
    assert saves.names() == ["save_check_result"]
    assert tools.probe_calls == []


@pytest.mark.parametrize("result", [
    FileTaskResult(FileTaskId("probe-1"), ran=False),
    FileTaskResult(FileTaskId("probe-1"), ran=True, error="file_conflict: 占用冲突"),
])
@pytest.mark.asyncio
async def test_probe_without_observation_saves_no_verdict(result) -> None:
    """检查任务未取得观察：不保存终态结论，责任留给下一次执行。"""
    saves = _Saves()
    tools = _Tools(result, None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is CheckExecutionPhase.PROBE_NOT_COMPLETED
    assert saves.names() == ["save_check_result"]
    assert saves.calls[0][1].phase is CheckPhase.RUNNING


@pytest.mark.asyncio
async def test_failed_probe_saves_terminal_check_without_decision() -> None:
    saves = _Saves()
    tools = _Tools(_probe_result(MediaProbe(None, "tool_failed: exit=1")), None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is CheckExecutionPhase.CHECK_TERMINAL
    assert saves.names() == ["save_check_result", "save_check_result"]
    assert saves.calls[1][1].media.phase is CheckPhase.FAILED
    assert step.media is not None and step.media.phase is CheckPhase.FAILED
    assert step.repair_decision is None


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, CheckExecutionPhase.RESULT_REJECTED),
    (SaveDisposition.UNKNOWN, CheckExecutionPhase.RESULT_UNKNOWN),
])
@pytest.mark.asyncio
async def test_result_save_failure_stops_decision(disposition, phase) -> None:
    receipts = [_saved(), _receipt(disposition)]

    def check_receipt(command):
        return receipts.pop(0)

    saves = _Saves({"save_check_result": check_receipt})
    tools = _Tools(_probe_result(MediaProbe(Decimal("75.125"), None)), None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is phase
    assert step.media is not None
    assert step.media.phase is CheckPhase.COMPLETED
    assert saves.names() == ["save_check_result", "save_check_result"]


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, CheckExecutionPhase.DECISION_REJECTED),
    (SaveDisposition.UNKNOWN, CheckExecutionPhase.DECISION_UNKNOWN),
])
@pytest.mark.asyncio
async def test_decision_save_failure_keeps_result(disposition, phase) -> None:
    saves = _Saves({"save_repair_decision": _receipt(disposition)})
    tools = _Tools(_probe_result(MediaProbe(Decimal("75.125"), None)), None)
    step = await execute_check(_check_context(_status(), saves, tools))
    assert step.phase is phase
    assert saves.names() == [
        "save_check_result", "save_check_result", "save_repair_decision"]
    assert step.repair_decision is not None


# ---- execute_repair ----


def _repair_result(artifact: MediaArtifact) -> FileTaskResult:
    return FileTaskResult(FileTaskId("repair-1"), ran=True, value=artifact)


def _complete_result() -> FileTaskResult:
    return _repair_result(_artifact(tool_error=None))


@pytest.mark.asyncio
async def test_repair_registers_output_runs_tool_and_succeeds() -> None:
    saves = _Saves({"start_repair_output": _saved(_REGISTERED_OUTPUT)})
    tools = _Tools(None, _complete_result())
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is RepairExecutionPhase.SUCCEEDED
    assert saves.names() == ["start_repair_output", "complete_repair_output"]
    assert saves.calls[0][1] == RepairStart(1, "mp4", _NOW)
    assert tools.repair_calls == [(_input_ref(), _OUTPUT_REF, Decimal(60))]
    assert saves.calls[1][1] == RepairSuccess(1, 21, 2048, "c" * 64, _NOW)
    assert step.output_file == _REGISTERED_OUTPUT


@pytest.mark.asyncio
async def test_repair_resumes_with_registered_output() -> None:
    """已登记输出并处于执行中的恢复入口：不再重复登记，直接续执行。"""
    saves = _Saves()
    tools = _Tools(None, _complete_result())
    status = _status(repair_state=4, repair_output_file_id=21,
                     repair_output_path="derived/21.mp4")
    step = await execute_repair(_repair_context(status, saves, tools))
    assert step.phase is RepairExecutionPhase.SUCCEEDED
    assert saves.names() == ["complete_repair_output"]
    assert tools.repair_calls == [(_input_ref(), _OUTPUT_REF, Decimal(60))]


@pytest.mark.asyncio
async def test_repair_trim_follows_saved_target_duration() -> None:
    """裁剪时长按已保存修复决定的目标时长推导，保持十进制精度。"""
    saves = _Saves({"start_repair_output": _saved(_REGISTERED_OUTPUT)})
    tools = _Tools(None, _complete_result())
    status = _status(repair_state=3, target_duration_ms=3_500)
    step = await execute_repair(_repair_context(status, saves, tools))
    assert step.phase is RepairExecutionPhase.SUCCEEDED
    assert tools.repair_calls == [(_input_ref(), _OUTPUT_REF, Decimal("3.5"))]


@pytest.mark.asyncio
async def test_repair_running_without_registered_output_is_invalid() -> None:
    saves = _Saves()
    with pytest.raises(ValueError):
        await execute_repair(_repair_context(
            _status(repair_state=4), saves, _Tools(None, None)))


@pytest.mark.parametrize("repair_state, phase", [
    (1, RepairExecutionPhase.NOT_PENDING),
    (2, RepairExecutionPhase.NOT_PENDING),
    (5, RepairExecutionPhase.ALREADY_FINISHED),
    (6, RepairExecutionPhase.ALREADY_FINISHED),
    (7, RepairExecutionPhase.ALREADY_FINISHED),
])
@pytest.mark.asyncio
async def test_repair_without_open_execution_does_not_start(repair_state, phase) -> None:
    saves = _Saves()
    step = await execute_repair(_repair_context(
        _status(repair_state=repair_state), saves, _Tools(None, None)))
    assert step.phase is phase
    assert saves.names() == []


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, RepairExecutionPhase.START_REJECTED),
    (SaveDisposition.UNKNOWN, RepairExecutionPhase.START_UNKNOWN),
])
@pytest.mark.asyncio
async def test_repair_does_not_run_tool_before_saved_start(disposition, phase) -> None:
    saves = _Saves({"start_repair_output": _receipt(disposition)})
    tools = _Tools(None, _complete_result())
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is phase
    assert saves.names() == ["start_repair_output"]
    assert tools.repair_calls == []


@pytest.mark.asyncio
async def test_repair_task_without_artifact_saves_no_verdict() -> None:
    saves = _Saves({"start_repair_output": _saved(_REGISTERED_OUTPUT)})
    tools = _Tools(None, FileTaskResult(FileTaskId("repair-1"), ran=False))
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is RepairExecutionPhase.TOOL_NOT_COMPLETED
    assert saves.names() == ["start_repair_output"]


@pytest.mark.asyncio
async def test_incomplete_artifact_saves_repair_failure() -> None:
    saves = _Saves({"start_repair_output": _saved(_REGISTERED_OUTPUT)})
    tools = _Tools(
        None, _repair_result(_artifact(tool_error="tool_failed: exit=1")))
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED
    assert saves.names() == ["start_repair_output", "save_repair_result"]
    failure = saves.calls[1][1]
    assert failure.phase is RepairOutcome.FAILED
    assert failure.error.code == "tool_failed"
    assert failure.error.stage == "repair"


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, RepairExecutionPhase.COMPLETE_REJECTED),
    (SaveDisposition.UNKNOWN, RepairExecutionPhase.COMPLETE_UNKNOWN),
])
@pytest.mark.asyncio
async def test_repair_success_save_failure_keeps_running(disposition, phase) -> None:
    saves = _Saves({
        "start_repair_output": _saved(_REGISTERED_OUTPUT),
        "complete_repair_output": _receipt(disposition),
    })
    tools = _Tools(None, _complete_result())
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is phase
    assert saves.names() == ["start_repair_output", "complete_repair_output"]


@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, RepairExecutionPhase.FAILED_REJECTED),
    (SaveDisposition.UNKNOWN, RepairExecutionPhase.FAILED_UNKNOWN),
])
@pytest.mark.asyncio
async def test_repair_failure_save_outcome_is_reported(disposition, phase) -> None:
    saves = _Saves({
        "start_repair_output": _saved(_REGISTERED_OUTPUT),
        "save_repair_result": _receipt(disposition),
    })
    tools = _Tools(
        None, _repair_result(_artifact(tool_error="tool_failed: exit=1")))
    step = await execute_repair(_repair_context(_status(repair_state=3), saves, tools))
    assert step.phase is phase
    assert saves.names() == ["start_repair_output", "save_repair_result"]
