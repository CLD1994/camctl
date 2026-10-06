"""录像内部输入副本的取得编排。

复用 X4—X6 的读取资格与拷贝流程：先经资格申请建档（或复用既有
准备记录），再按续传决定定位读取会话，分段推进可靠进度，最后做
完整性收尾。已完成副本重入直接就绪，不重开设备会话；资格等待、
发起责任取消、段失败与摘要不一致各有分区，不在本次执行内就地重
试。读取尝试预算与相机读取机会由意图入口管理，本编排不代替。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from camctl.capture.media import SaveDisposition, SaveReceipt
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.devices.read_session import ReadChunk, ReadEnd
from camctl.host_files.models import FileRef
from camctl.outputs.copy import (
    CompletionPhase,
    CompletionStep,
    CopyCompletionError,
    CopyPreparationError,
    CopySegmentError,
    CopyStateFacts,
    CopyStep,
    ResumeOutcome,
    SegmentOutcome,
    SegmentSaveDisposition,
    SegmentStep,
    SourceDigestReader,
)
from camctl.outputs.qualification import (
    FileCandidate,
    OperationConfig,
    QualificationOutcome,
)

__all__ = [
    "InputContext",
    "InputPhase",
    "InputStep",
    "ReadSessionOpener",
    "RecordingCopies",
    "RecordingSource",
    "obtain_recording_input",
]

_VERIFICATION = enum_for("file_copies.verification_state")

#: 已完成副本的固定事实组合：校验通过且目标字节事实已保存。
_READY_VERIFICATIONS = (
    int(_VERIFICATION.MATCHED), int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
)


class RecordingSource(Protocol):
    """内部输入读取会话端口；设备读取会话与替身共同满足。"""

    def position(self) -> int: ...

    def read_chunk(self, limit: int) -> ReadChunk: ...

    def poll_stopped(self) -> ReadEnd | None: ...

    def request_stop(self) -> None: ...

    async def wait_stopped(self) -> ReadEnd: ...


class ReadSessionOpener(Protocol):
    """按源文件与偏移打开读取会话的端口；适配层持有设备绑定。"""

    async def open_session(
        self, source_device_file_id: int, offset: int,
    ) -> RecordingSource: ...


class RecordingCopies(Protocol):
    """内部输入拷贝端口；适配层组合资格、分段与完整性事务。"""

    def qualify(self, candidate: FileCandidate) -> SaveReceipt: ...

    def copy_state(self, copy_id: int) -> CopyStateFacts: ...

    async def prepare(self, copy_id: int) -> CopyStep: ...

    async def transfer(self, copy_id: int, session: RecordingSource,
                       segment_size: int) -> SegmentStep: ...

    async def complete(self, copy_id: int,
                       digest: SourceDigestReader | None) -> CompletionStep: ...


class InputPhase(Enum):
    """输入取得编排的结果分区。"""

    WAITING = "waiting"
    REJECTED_FINAL = "rejected_final"
    INPUT_READY = "input_ready"
    QUALIFY_REJECTED = "qualify_rejected"
    QUALIFY_UNKNOWN = "qualify_unknown"
    PREPARE_FAILED = "prepare_failed"
    SEGMENT_FAILED = "segment_failed"
    OWNER_SKIPPED = "owner_skipped"
    COMPLETION_FAILED = "completion_failed"
    RECOPY_PENDING = "recopy_pending"
    RETRY_WAITING = "retry_waiting"


@dataclass(frozen=True)
class InputStep:
    """一次输入取得的结果：阶段、就绪引用与诊断。"""

    phase: InputPhase
    copy_id: int | None = None
    input_file: FileRef | None = None
    reason: str | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class InputContext:
    """一次输入取得的输入：处理事实、源文件、配置与端口。

    源设备文件须已停止并写完；扩展名沿用原片容器类型。digest 为
    None 表示不提供源端摘要读取，源端支持与否由实际读取表达。
    """

    action_id: int
    processing_id: int
    source_device_file_id: int
    target_extension: str | None
    config: OperationConfig
    segment_size: int
    staging: Path
    copies: RecordingCopies
    sessions: ReadSessionOpener
    occurred_at: int
    digest: SourceDigestReader | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        ObjectId(self.processing_id)
        ObjectId(self.source_device_file_id)


async def obtain_recording_input(context: InputContext) -> InputStep:
    """取得录像内部输入副本：资格、续传、分段与完整性收尾。

    资格等待或最终失败时不开始拷贝；已完成副本重入直接就绪。会话
    在段循环结束后无论成败都请求停止并等待实际结束；段保存跳过表
    示发起责任不再执行，本次停止推进。摘要不一致登记新一轮重拷后
    不就地重试，由下一次执行从零重读。
    """
    receipt = context.copies.qualify(FileCandidate(
        action_id=context.action_id,
        item_id=None,
        processing_id=context.processing_id,
        output_id=None,
        source_device_file_id=context.source_device_file_id,
        target_extension=context.target_extension,
        delivery_extension=None,
        delivery_display_name=None,
        config=context.config,
        occurred_at=context.occurred_at,
    ))
    if receipt.disposition is SaveDisposition.REJECTED:
        return InputStep(InputPhase.QUALIFY_REJECTED, error=receipt.error)
    if receipt.disposition is SaveDisposition.UNKNOWN:
        return InputStep(InputPhase.QUALIFY_UNKNOWN, error=receipt.error)
    qualification = receipt.value
    if qualification.outcome is QualificationOutcome.REJECTED_FINAL:
        return InputStep(InputPhase.REJECTED_FINAL, reason=qualification.reason)
    if qualification.outcome is not QualificationOutcome.GRANTED:
        return InputStep(InputPhase.WAITING, reason=qualification.reason)
    copy_id = qualification.copy_id

    facts = context.copies.copy_state(copy_id)
    if (facts.verification_state in _READY_VERIFICATIONS
            and facts.target_sha256 is not None):
        return InputStep(
            InputPhase.INPUT_READY, copy_id=copy_id,
            input_file=_input_ref(facts, context.staging))

    try:
        prepared = await context.copies.prepare(copy_id)
    except (CopyPreparationError, ConsistencyError) as error:
        return InputStep(InputPhase.PREPARE_FAILED, copy_id=copy_id,
                         error=error)

    if prepared.decision.outcome is ResumeOutcome.VERIFY:
        return await _complete_input(context, copy_id, facts)

    session = await context.sessions.open_session(
        context.source_device_file_id, prepared.decision.offset)
    try:
        while True:
            step = await context.copies.transfer(
                copy_id, session, context.segment_size)
            if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                break
            if step.saved is None:
                raise ConsistencyError(
                    "段推进未携带保存事实，拷贝端口契约不一致")
            if step.saved.disposition is SegmentSaveDisposition.SKIPPED:
                return InputStep(
                    InputPhase.OWNER_SKIPPED, copy_id=copy_id,
                    reason=step.saved.reason)
    except (CopySegmentError, ConsistencyError) as error:
        return InputStep(InputPhase.SEGMENT_FAILED, copy_id=copy_id,
                         error=error)
    finally:
        session.request_stop()
        await session.wait_stopped()
    return await _complete_input(context, copy_id, facts)


async def _complete_input(context: InputContext, copy_id: int,
                          facts: CopyStateFacts) -> InputStep:
    """完整性收尾：准备完成即就绪；登记重拷表示本次不重试。"""
    try:
        completion = await context.copies.complete(copy_id, context.digest)
    except (CopyCompletionError, ConsistencyError) as error:
        return InputStep(InputPhase.COMPLETION_FAILED, copy_id=copy_id,
                         error=error)
    if completion.phase is CompletionPhase.PREPARED:
        return InputStep(
            InputPhase.INPUT_READY, copy_id=copy_id,
            input_file=_input_ref(facts, context.staging))
    return InputStep(InputPhase.RECOPY_PENDING, copy_id=copy_id)


def _input_ref(facts: CopyStateFacts, staging: Path) -> FileRef:
    return FileRef(
        file_id=facts.target.file_id, purpose=facts.target.purpose,
        relative_path=facts.target.relative_path, root=staging,
    )
