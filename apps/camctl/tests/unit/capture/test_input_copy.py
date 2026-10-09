"""C8 内部输入取得编排的单元测试。

复用 X4—X6 的读取资格与拷贝流程：资格等待与最终失败、已完成副本
重入不重拷、续传位置由准备决定（重置归零、VERIFY 按原实际结束事实续行）、段
保存跳过与各阶段失败分区、摘要不一致登记重拷后不就地重试。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from camctl.capture.input_copy import (
    InputContext,
    InputPhase,
    obtain_recording_input,
)
from camctl.capture.media import SaveDisposition, SaveReceipt
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.read_session import ReadChunk, ReadEnd
from camctl.host_files.models import FilePurpose, FileRef
from camctl.outputs.copy import (
    AttemptDecision,
    AttemptPlan,
    CompletionPhase,
    CompletionStep,
    CopyCompletionError,
    CopyDecision,
    CopyPreparationError,
    CopySegmentError,
    CopyStep,
    CopyStateFacts,
    CopyTargetRef,
    PreparedCopy,
    RecopyDisposition,
    RecopyResult,
    ResumeOutcome,
    SegmentOutcome,
    SegmentPlan,
    SegmentSaveDisposition,
    SegmentSaveOutcome,
    SegmentStep,
)
from camctl.outputs.qualification import (
    FileCandidate,
    FileQualification,
    OperationConfig,
    QualificationOutcome,
)

_NOW = 1_750_000_000_000_000
_STAGING = Path("staging")
_CONFIG = OperationConfig(max_attempts=3, timeout_s=Decimal("10"),
                          retry_interval_s=Decimal("0"))
_SOURCE_SIZE = 10
_VERIFICATION = enum_for("file_copies.verification_state")
_TARGET = CopyTargetRef(701, FilePurpose.RECORDING_INPUT,
                        "recording-inputs/701.mp4")
_INPUT_REF = FileRef(701, FilePurpose.RECORDING_INPUT,
                     "recording-inputs/701.mp4", _STAGING)


def _granted() -> FileQualification:
    return FileQualification(
        outcome=QualificationOutcome.GRANTED, copy_id=9, run_id=4,
        delivery_id=None, target_file_id=701, reason=None)


def _state(**overrides: Any) -> CopyStateFacts:
    values: dict[str, Any] = {
        "copy_id": 9,
        "source_size": _SOURCE_SIZE,
        "committed_bytes": 0,
        "round": 1,
        "reset_pending": False,
        "target": _TARGET,
        "attempts": (),
    }
    values.update(overrides)
    return CopyStateFacts(**values)


def _copy_step(outcome: ResumeOutcome = ResumeOutcome.CONTINUE,
               offset: int | None = 0) -> CopyStep:
    return CopyStep(
        decision=CopyDecision(outcome, offset=offset),
        attempt=AttemptDecision(AttemptPlan.NEW_REQUIRED),
    )


def _segment(end: int) -> SegmentStep:
    return SegmentStep(
        plan=SegmentPlan(SegmentOutcome.COPY, start=0, end=end, final=False),
        saved=SegmentSaveOutcome(SegmentSaveDisposition.SAVED, end),
    )


def _committed_step() -> SegmentStep:
    return SegmentStep(plan=SegmentPlan(SegmentOutcome.ALL_COMMITTED))


def _prepared_step() -> CompletionStep:
    return CompletionStep(
        phase=CompletionPhase.PREPARED,
        prepared=PreparedCopy(9, 701, _SOURCE_SIZE, "a" * 64,
                              source_dependency_released=False))


class _Session:
    """读取会话替身：记录停止请求与等待。"""

    def __init__(self) -> None:
        self.stop_requested = False
        self.awaited = False
        self.offset = 0

    def position(self) -> int:
        return self.offset

    def read_chunk(self, limit: int) -> ReadChunk:
        raise AssertionError("编排不直接读取会话")

    def poll_stopped(self) -> ReadEnd | None:
        return None

    def request_stop(self) -> None:
        self.stop_requested = True

    async def wait_stopped(self) -> ReadEnd:
        self.awaited = True
        return ReadEnd(stopped=True, bytes_read=0, error=None)


class _Sessions:
    """会话工厂替身：记录打开请求。"""

    def __init__(self, session: _Session | None = None) -> None:
        self.session = session or _Session()
        self.opens: list[tuple[int, int]] = []

    async def open_session(self, source_device_file_id: int,
                           offset: int) -> _Session:
        self.opens.append((source_device_file_id, offset))
        self.session.offset = offset
        return self.session


class _Copies:
    """拷贝端口替身：按脚本返回并记录调用。"""

    def __init__(self, *, qualification: FileQualification | None = None,
                 state: CopyStateFacts | None = None,
                 prepare: CopyStep | Exception | None = None,
                 segments: list[Any] | None = None,
                 completion: Any = None,
                 qualify_receipt: SaveReceipt | None = None) -> None:
        self.qualification = qualification or _granted()
        self.qualify_receipt = qualify_receipt
        self.state = state or _state()
        self.prepare_result = prepare if prepare is not None else _copy_step()
        self.segments = list(segments or [_segment(10), _committed_step()])
        self.completion_result = (completion if completion is not None
                                  else _prepared_step())
        self.candidates: list[FileCandidate] = []
        self.calls: list[str] = []

    def qualify(self, candidate: FileCandidate) -> SaveReceipt:
        self.candidates.append(candidate)
        self.calls.append("qualify")
        if self.qualify_receipt is not None:
            return self.qualify_receipt
        return SaveReceipt(SaveDisposition.SAVED, value=self.qualification)

    def copy_state(self, copy_id: int) -> CopyStateFacts:
        self.calls.append(f"state:{copy_id}")
        return self.state

    async def prepare(self, copy_id: int) -> CopyStep:
        self.calls.append("prepare")
        if isinstance(self.prepare_result, Exception):
            raise self.prepare_result
        return self.prepare_result

    async def transfer(self, copy_id: int, session,
                       segment_size: int) -> SegmentStep:
        self.calls.append("transfer")
        step = self.segments.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def complete(self, copy_id: int, digest) -> CompletionStep:
        self.calls.append("complete")
        if isinstance(self.completion_result, Exception):
            raise self.completion_result
        return self.completion_result


def _context(copies: _Copies, sessions: _Sessions,
             *, digest=None, read_end=None) -> InputContext:
    return InputContext(
        action_id=1, processing_id=5, source_device_file_id=11,
        target_extension="mp4", config=_CONFIG, segment_size=4,
        staging=_STAGING, copies=copies, sessions=sessions,
        digest=digest, occurred_at=_NOW, read_end=read_end)


# ---- 正常与重入 ----


@pytest.mark.asyncio
async def test_full_copy_drives_segments_and_completes() -> None:
    copies = _Copies()
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.INPUT_READY
    assert step.input_file == _INPUT_REF
    assert step.copy_id == 9
    assert copies.calls == ["qualify", "state:9", "prepare", "transfer",
                            "transfer", "complete"]
    candidate = copies.candidates[0]
    assert (candidate.action_id, candidate.processing_id,
            candidate.source_device_file_id) == (1, 5, 11)
    assert candidate.item_id is None and candidate.output_id is None
    assert candidate.config is _CONFIG
    assert sessions.opens == [(11, 0)]
    assert sessions.session.stop_requested and sessions.session.awaited


@pytest.mark.asyncio
async def test_ready_copy_reentry_does_not_recopy() -> None:
    """已完成副本：资格复用后直接就绪，不开会话不推进段。"""
    copies = _Copies(state=_state(
        committed_bytes=_SOURCE_SIZE,
        verification_state=int(_VERIFICATION.MATCHED),
        target_sha256="a" * 64))
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.INPUT_READY
    assert step.input_file == _INPUT_REF
    assert copies.calls == ["qualify", "state:9"]
    assert sessions.opens == []


@pytest.mark.asyncio
async def test_verify_without_end_confirms_session_at_saved_full_offset() -> None:
    """全部字节已保存而无结束事实：从原文件末尾打开并确认实际结束。"""
    copies = _Copies(state=_state(committed_bytes=_SOURCE_SIZE),
        prepare=_copy_step(ResumeOutcome.VERIFY, None), segments=[_committed_step()])
    class EndSession(_Session):
        def read_chunk(self, limit):
            assert self.position() == _SOURCE_SIZE
            return ReadChunk(data=None, eof=True)

    sessions = _Sessions(EndSession())

    step = await obtain_recording_input(_context(copies, sessions))

    assert step.phase is InputPhase.INPUT_READY
    assert step.input_file == _INPUT_REF
    assert step.read_end == ReadEnd(True, 0, None)
    assert sessions.opens == [(11, _SOURCE_SIZE)]
    assert sessions.session.stop_requested and sessions.session.awaited
    assert copies.state.committed_bytes == _SOURCE_SIZE


@pytest.mark.asyncio
async def test_verify_with_original_end_completes_without_reopening_source() -> None:
    copies = _Copies(state=_state(committed_bytes=_SOURCE_SIZE),
        prepare=_copy_step(ResumeOutcome.VERIFY, None), segments=[AssertionError("不能再传输已存字节")])
    sessions = _Sessions()
    end = ReadEnd(True, _SOURCE_SIZE, None)

    step = await obtain_recording_input(_context(copies, sessions, read_end=end))

    assert step.phase is InputPhase.INPUT_READY
    assert step.input_file == _INPUT_REF
    assert step.read_end is end
    assert sessions.opens == []
    assert copies.state.committed_bytes == _SOURCE_SIZE


@pytest.mark.asyncio
async def test_reset_decision_opens_session_from_zero() -> None:
    """重拷轮次重置后：从零重新读取，不从旧进度续传。"""
    copies = _Copies(
        state=_state(committed_bytes=0, reset_pending=True),
        prepare=_copy_step(ResumeOutcome.RESET_TARGET, 0))
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.INPUT_READY
    assert sessions.opens == [(11, 0)]


@pytest.mark.asyncio
async def test_resume_opens_session_at_committed_position() -> None:
    copies = _Copies(
        state=_state(committed_bytes=6),
        prepare=_copy_step(ResumeOutcome.CONTINUE, 6))
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.INPUT_READY
    assert sessions.opens == [(11, 6)]


# ---- 资格分区 ----


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, phase, reason", [
    (QualificationOutcome.REJECTED, InputPhase.WAITING, "not_due"),
    (QualificationOutcome.REJECTED, InputPhase.WAITING, "input_undecided"),
    (QualificationOutcome.REJECTED_FINAL, InputPhase.REJECTED_FINAL,
     "input_need_ended"),
])
async def test_qualification_wait_and_final_reject_stop_before_copy(
        outcome, phase, reason) -> None:
    copies = _Copies(qualification=FileQualification(
        outcome=outcome, copy_id=None, run_id=None, delivery_id=None,
        target_file_id=None, reason=reason))
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is phase
    assert step.reason == reason
    assert copies.calls == ["qualify"]
    assert sessions.opens == []


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition, phase", [
    (SaveDisposition.REJECTED, InputPhase.QUALIFY_REJECTED),
    (SaveDisposition.UNKNOWN, InputPhase.QUALIFY_UNKNOWN),
])
async def test_qualification_save_failure_stops(disposition, phase) -> None:
    copies = _Copies(qualify_receipt=SaveReceipt(
        disposition, error=RuntimeError("db")))
    step = await obtain_recording_input(_context(copies, _Sessions()))
    assert step.phase is phase
    assert step.error is not None
    assert copies.calls == ["qualify"]


# ---- 失败与跳过分区 ----


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CopyPreparationError("prepare_failed", "io"),
    ConsistencyError("续传事实矛盾"),
])
async def test_prepare_failure_stops_before_session(error) -> None:
    copies = _Copies(prepare=error)
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.PREPARE_FAILED
    assert step.error is error
    assert copies.calls == ["qualify", "state:9", "prepare"]
    assert sessions.opens == []


@pytest.mark.asyncio
async def test_segment_failure_stops_and_closes_session() -> None:
    copies = _Copies(segments=[CopySegmentError("transfer_failed", "io")])
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.SEGMENT_FAILED
    assert isinstance(step.error, CopySegmentError)
    assert copies.calls == ["qualify", "state:9", "prepare", "transfer"]
    assert sessions.session.stop_requested and sessions.session.awaited


@pytest.mark.asyncio
async def test_owner_skip_stops_progress() -> None:
    """发起责任取消或不在执行：段保存跳过，不再推进下一段。"""
    copies = _Copies(segments=[
        SegmentStep(
            plan=SegmentPlan(SegmentOutcome.COPY, 0, 4, False),
            saved=SegmentSaveOutcome(
                SegmentSaveDisposition.SKIPPED, 0, reason="cancel_requested"),
        ),
        _committed_step(),
    ])
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.OWNER_SKIPPED
    assert step.reason == "cancel_requested"
    assert copies.calls == ["qualify", "state:9", "prepare", "transfer"]
    assert sessions.session.stop_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CopyCompletionError("verify_failed", "digest"),
    ConsistencyError("完整性事实矛盾"),
])
async def test_completion_failure_keeps_session_closed(error) -> None:
    copies = _Copies(completion=error)
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.COMPLETION_FAILED
    assert step.error is error
    assert copies.calls == ["qualify", "state:9", "prepare", "transfer",
                            "transfer", "complete"]
    assert sessions.session.awaited


@pytest.mark.asyncio
async def test_mismatch_registers_recopy_without_retry() -> None:
    """摘要不一致：登记新一轮重拷后本次不就地重试。"""
    copies = _Copies(completion=CompletionStep(
        phase=CompletionPhase.RECOPY_REGISTERED,
        recopy=RecopyResult(RecopyDisposition.REGISTERED, 9, 2),
    ))
    sessions = _Sessions()
    step = await obtain_recording_input(_context(copies, sessions))
    assert step.phase is InputPhase.RECOPY_PENDING
    assert step.input_file is None
    assert copies.calls[-1] == "complete"
