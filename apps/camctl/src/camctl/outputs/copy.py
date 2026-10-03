"""共用拷贝的续传准备、分段推进、可靠进度与完整性收尾。

取回、异常原片检查和修复的输入拷贝共用本模块：固定源身份与
长度后，按可靠进度和主机文件事实决定续传位置，再按本次配置
的段大小逐段传输并申请进度保存，全部字节可靠保存后按源端摘
要能力完成完整性判定与准备收尾。设备读取开始前的意图与次数
由操作仓储保存，本模块不重复预算判断。行为契约为[重启后选择
续传位置](../../architecture/file-copy.md#重启后选择续传位置)、
[进度保存的分段大小](../../architecture/file-copy.md#进度保存的分段大小)与
[读取正确性与文件校验](../../architecture/file-copy.md#读取正确性与文件校验)。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence

from camctl.contracts.enums import enum_for
from camctl.contracts.values import (
    ConsistencyError, MAX_OBJECT_ID, ObjectId, OperationKey, UtcMicros,
    new_operation_key,
)
from camctl.devices.read_session import ReadChunk, ReadEnd
from camctl.host_files.io import (
    LocalSourceReader as LocalCopySource, PositionedWriter, hash_target, prepare_target,
    sync_target,
)
from camctl.host_files.models import (
    BoundDirectories, FileObservation, FileObservationKind, FilePurpose, FileRef,
)
from camctl.host_files.paths import inspect_file, resolve_file
from camctl.host_files.segments import (
    DEFAULT_CHUNK_SIZE_BYTES, SegmentFailure, SegmentResult, SegmentSpec,
    transfer_segment,
)
from camctl.persistence.models import DbOutcomeKind

if TYPE_CHECKING:
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.runtime import OwnedConnection

_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_VERIFICATION = enum_for("file_copies.verification_state")
_HEX = frozenset("0123456789abcdef")


class SourceChecksumSupport(Enum):
    """源端摘要能力的可靠判定；未知不能折叠为不支持。"""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNDETERMINED = "undetermined"


def _require_digest(name: str, value) -> None:
    """摘要必须是 64 位小写十六进制，或明确未知（None）。"""
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise ValueError(f"{name} 必须是 64 位小写十六进制摘要: {value!r}")


class ResumeOutcome(Enum):
    """续传准备的分区；与重启后选择续传位置的决策表一一对应。"""

    CREATE = "create"
    CONTINUE = "continue"
    TRUNCATE = "truncate"
    VERIFY = "verify"
    RESET_TARGET = "reset_target"


@dataclass(frozen=True)
class TargetFileObservation:
    """主机目标文件的一次可靠观察；不可靠时只保留错误。

    observed 为 False 表示检查本身失败，present 与 length 必须
    为空；观察可靠时 present 表达文件是否存在，存在则携带同一
    次 stat 取得的长度。
    """

    observed: bool
    present: bool | None = None
    length: int | None = None
    error: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observed, bool):
            raise ValueError(f"观察可靠标志必须是布尔值: {self.observed!r}")
        if not self.observed:
            if self.present is not None or self.length is not None:
                raise ValueError("观察不可靠时不能携带文件事实")
            return
        if not isinstance(self.present, bool):
            raise ValueError("可靠观察必须表达文件是否存在")
        if self.present:
            if isinstance(self.length, bool) or not isinstance(self.length, int) or self.length < 0:
                raise ValueError(f"存在的文件必须携带非负整数长度: {self.length!r}")
        elif self.length is not None:
            raise ValueError("缺失的文件不能携带长度")


@dataclass(frozen=True)
class CopyFacts:
    """续传判定所需的固定事实：源长度 N、可靠进度 C 与文件观察。"""

    source_size: int
    committed_bytes: int
    reset_pending: bool = False
    target: TargetFileObservation | None = None

    def __post_init__(self) -> None:
        for name, value in (("source_size", self.source_size),
                            ("committed_bytes", self.committed_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if not isinstance(self.reset_pending, bool):
            raise ValueError(f"重置标志必须是布尔值: {self.reset_pending!r}")
        if not isinstance(self.target, TargetFileObservation):
            raise ValueError("续传判定必须携带目标文件观察")


@dataclass(frozen=True)
class CopyDecision:
    """续传决定：读取起点与需要截断到的长度；VERIFY 阶段两者为空。"""

    outcome: ResumeOutcome
    offset: int | None = None
    truncate_to: int | None = None


def decide_resume(facts: CopyFacts) -> CopyDecision:
    """按固定源长度、可靠进度与文件事实选择续传位置。

    进度与文件长度的关系矛盾按一致性错误处理，不跳过缺口、不
    把不可靠观察当作缺失或进度为零。
    """
    if not isinstance(facts, CopyFacts):
        raise TypeError(f"续传判定必须使用 CopyFacts: {facts!r}")
    size, committed = facts.source_size, facts.committed_bytes
    if committed > size:
        raise ConsistencyError(f"可靠进度 {committed} 超过固定源长度 {size}")
    if facts.reset_pending:
        if committed != 0:
            raise ConsistencyError("重置意图登记后可靠进度必须已经归零")
        return CopyDecision(ResumeOutcome.RESET_TARGET, offset=0, truncate_to=0)
    observation = facts.target
    assert observation is not None
    if not observation.observed:
        raise ConsistencyError(
            f"主机目标文件观察不可靠，不能当作缺失或进度为零: {observation.error!r}"
        )
    if not observation.present:
        if committed == 0:
            return CopyDecision(ResumeOutcome.CREATE, offset=0)
        raise ConsistencyError(f"主机目标文件缺失，已确认进度 {committed} 的数据不可用")
    length = observation.length
    assert length is not None
    if length < committed:
        raise ConsistencyError(f"主机文件长度 {length} 小于已确认进度 {committed}")
    if length > size:
        raise ConsistencyError(f"主机文件长度 {length} 超过固定源长度 {size}")
    if length == committed:
        if committed == size:
            return CopyDecision(ResumeOutcome.VERIFY)
        return CopyDecision(ResumeOutcome.CONTINUE, offset=committed)
    return CopyDecision(ResumeOutcome.TRUNCATE, offset=committed, truncate_to=committed)


class AttemptPlan(Enum):
    """读取尝试的计划：沿原尝试恢复，或经合法入口新增。"""

    RESUME_EXISTING = "resume_existing"
    NEW_REQUIRED = "new_required"


@dataclass(frozen=True)
class AttemptRecord:
    """已保存读取尝试的判定事实。"""

    attempt_id: int
    attempt_no: int
    status: int
    copy_round: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("attempt_id", self.attempt_id), ("attempt_no", self.attempt_no)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数: {value!r}")
        if isinstance(self.status, bool) or not isinstance(self.status, int) \
                or self.status not in set(int(member) for member in _ATTEMPT_STATUS):
            raise ValueError(f"尝试状态不属于登记的枚举: {self.status!r}")
        if self.copy_round is not None and (
            isinstance(self.copy_round, bool) or not isinstance(self.copy_round, int)
            or self.copy_round <= 0
        ):
            raise ValueError(f"拷贝轮次必须是正整数: {self.copy_round!r}")


@dataclass(frozen=True)
class AttemptDecision:
    """读取尝试计划；沿原尝试恢复时携带原尝试身份。"""

    plan: AttemptPlan
    attempt_id: int | None = None
    attempt_no: int | None = None


def decide_attempt(attempts: Sequence[AttemptRecord], current_round: int) -> AttemptDecision:
    """未保存读取失败的重启沿原尝试；已明确失败只能新增合法尝试。

    旧轮次的尝试属于历史，不参与恢复；当前轮次的在途尝试必须
    唯一。预算、重试等待与机会由意图入口判断，本函数不重复。
    """
    if isinstance(current_round, bool) or not isinstance(current_round, int) or current_round <= 0:
        raise ValueError(f"当前拷贝轮次必须是正整数: {current_round!r}")
    in_flight: list[AttemptRecord] = []
    for record in attempts:
        if not isinstance(record, AttemptRecord):
            raise TypeError(f"尝试事实必须使用 AttemptRecord: {record!r}")
        if record.copy_round is None:
            raise ConsistencyError(f"读取尝试 {record.attempt_id} 缺少拷贝轮次")
        if record.copy_round > current_round:
            raise ConsistencyError(
                f"读取尝试轮次 {record.copy_round} 超过当前轮次 {current_round}"
            )
        if record.copy_round == current_round \
                and record.status == int(_ATTEMPT_STATUS.RUNNING):
            in_flight.append(record)
    if len(in_flight) > 1:
        raise ConsistencyError("同一拷贝轮次存在多个未结束读取尝试")
    if in_flight:
        return AttemptDecision(
            AttemptPlan.RESUME_EXISTING,
            attempt_id=in_flight[0].attempt_id,
            attempt_no=in_flight[0].attempt_no,
        )
    return AttemptDecision(AttemptPlan.NEW_REQUIRED)


class TargetResetOutcome(Enum):
    """目标重置事务的分区。"""

    COMPLETED = "completed"
    ALREADY_READY = "already_ready"


@dataclass(frozen=True)
class TargetResetRequest:
    """目标重置完成的申请：目标拷贝与事实时刻。"""

    copy_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事实时刻超出 SQLite 整数范围: {self.occurred_at!r}")


@dataclass(frozen=True)
class TargetResetDecision:
    outcome: TargetResetOutcome


class CopyPreparationError(RuntimeError):
    """续传准备失败；保留实际阶段，不猜测写入效果。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class CopyTargetRef:
    """目标中间文件的定位事实；保存身份与规范相对路径。"""

    file_id: int
    purpose: FilePurpose
    relative_path: str

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        if not isinstance(self.purpose, FilePurpose):
            raise ValueError(f"目标用途必须是 FilePurpose: {self.purpose!r}")
        if not isinstance(self.relative_path, str) or not self.relative_path:
            raise ValueError("目标必须携带已保存的相对路径")


@dataclass(frozen=True)
class CopyStateFacts:
    """仓储加载的拷贝固定事实；源身份与长度已经核对一致。"""

    copy_id: int
    source_size: int
    committed_bytes: int
    round: int
    reset_pending: bool
    target: CopyTargetRef
    attempts: tuple[AttemptRecord, ...]
    source_sha256: str | None = None
    target_sha256: str | None = None
    verification_state: int = int(_VERIFICATION.NOT_PERFORMED)
    recopies_used: int = 0
    max_recopies_used: int = 0
    source_support: SourceChecksumSupport = SourceChecksumSupport.UNDETERMINED


@dataclass(frozen=True)
class CopyStep:
    """一次续传准备的结果：续传决定、读取尝试计划与重置事实。"""

    decision: CopyDecision
    attempt: AttemptDecision
    reset: TargetResetOutcome | None = None


class SegmentOutcome(Enum):
    """段计划的分区：仍有内容拷贝，或已全部可靠保存。"""

    COPY = "copy"
    ALL_COMMITTED = "all_committed"


@dataclass(frozen=True)
class SegmentFacts:
    """段计划所需的固定事实：源长度 N、可靠进度 C 与本次段大小 S。"""

    source_size: int
    committed_bytes: int
    segment_size: int

    def __post_init__(self) -> None:
        for name, value in (("source_size", self.source_size),
                            ("committed_bytes", self.committed_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if isinstance(self.segment_size, bool) or not isinstance(self.segment_size, int) \
                or self.segment_size <= 0:
            raise ValueError(f"segment_size 必须是正整数: {self.segment_size!r}")


@dataclass(frozen=True)
class SegmentPlan:
    """一段的计划范围 [start, end)；无剩余内容时不携带范围。

    final 表示本段到达固定源长度，段提交后进入所属流程的完整性
   确认，不再安排下一段。
    """

    outcome: SegmentOutcome
    start: int | None = None
    end: int | None = None
    final: bool = False


def plan_segment(facts: SegmentFacts) -> SegmentPlan:
    """按 E = C + min(S, N - C) 计划下一段；已无内容不产生空段。

    旧进度不是新段大小倍数时从原位置继续，不向上或向下对齐；
    进度越界按一致性错误处理。
    """
    if not isinstance(facts, SegmentFacts):
        raise TypeError(f"段计划必须使用 SegmentFacts: {facts!r}")
    size, committed, segment = facts.source_size, facts.committed_bytes, facts.segment_size
    if committed > size:
        raise ConsistencyError(f"可靠进度 {committed} 超过固定源长度 {size}")
    if committed == size:
        return SegmentPlan(SegmentOutcome.ALL_COMMITTED)
    end = committed + min(segment, size - committed)
    return SegmentPlan(
        SegmentOutcome.COPY, start=committed, end=end, final=end == size,
    )


@dataclass(frozen=True)
class ReliableSegment:
    """一段已写入并同步成功的目标范围；未同步字节不能构造保存申请。

    committed_before 是保存前数据库中的可靠进度；保存事务核对
    该旧值与当前拷贝一致后，才把进度推进到 segment_end。
    """

    copy_id: int
    copy_round: int
    committed_before: int
    segment_end: int
    synced: bool
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        for name, value in (("copy_round", self.copy_round),):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数: {value!r}")
        for name, value in (("committed_before", self.committed_before),
                            ("segment_end", self.segment_end)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if self.segment_end <= self.committed_before:
            raise ValueError("段保存申请必须推进可靠进度")
        if not isinstance(self.synced, bool) or not self.synced:
            raise ValueError("段尾同步未确认的字节不能保存为可靠进度")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事实时刻超出 SQLite 整数范围: {self.occurred_at!r}")


@dataclass(frozen=True)
class CopyContext:
    """续传准备的协作者：仓储、连接、目录绑定与事实时刻。"""

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    roots: BoundDirectories
    occurred_at: int
    reset_key: OperationKey | None = None


class SegmentSaveDisposition(Enum):
    """段保存事务的可靠结果分区。"""

    SAVED = "saved"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SegmentSaveOutcome:
    """段保存事务的返回：新进度，或不保存的可靠原因。

    SKIPPED 表示保存前核对到发起责任已取消或不在执行，未提交任
    何进度事件；已开始的进度事务按数据库执行规则确认实际结果。
    """

    disposition: SegmentSaveDisposition
    committed_bytes: int
    reason: str | None = None


class CopySegmentError(RuntimeError):
    """一段拷贝失败；保留实际阶段，不猜测未确认尾部的效果。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class SegmentSource(Protocol):
    """段传输读取端的端口：设备读取会话与主机源读取器共同满足。"""

    def position(self) -> int: ...

    def read_chunk(self, limit: int) -> ReadChunk: ...

    def poll_stopped(self) -> ReadEnd | None: ...


@dataclass(frozen=True)
class SegmentContext:
    """一段拷贝的协作者、本次段大小与停止通知。"""

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    roots: BoundDirectories
    occurred_at: int
    segment_size: int
    session: SegmentSource
    chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES
    stop: threading.Event | None = None
    key: OperationKey | None = None


@dataclass(frozen=True)
class SegmentStep:
    """一次分段推进的结果；无剩余内容时不携带保存与传输事实。"""

    plan: SegmentPlan
    saved: SegmentSaveOutcome | None = None
    transfer: SegmentResult | None = None


def _transfer_segment(
    ref: FileRef, roots: BoundDirectories, plan: SegmentPlan, spec: SegmentSpec,
    session: SegmentSource,
) -> SegmentResult:
    """在工作线程内定位目标并传输本段；句柄随段打开和关闭。"""
    host = resolve_file(ref, roots)
    with PositionedWriter(host.path, plan.start) as writer:
        return transfer_segment(spec, session, writer)


async def copy_next_segment(copy_id: int, context: SegmentContext) -> SegmentStep:
    """推进一段已建档拷贝：传输、同步并保存可靠进度。

    一文件一次一段；段大小取自本次配置。实际写入及同步可靠、发
    起责任仍有执行资格才保存进度，保存确认提交后由调用方推进下
    一段。传输或保存失败保留阶段诊断，不推进可靠进度；提交结果
    未知停止推进，恢复先确认数据库实际提交的进度。
    """
    if isinstance(context.segment_size, bool) or not isinstance(context.segment_size, int) \
            or context.segment_size <= 0:
        raise ValueError(f"本次段大小必须是正整数: {context.segment_size!r}")
    facts = context.repository.load_copy_state(copy_id, context.owned)
    plan = plan_segment(SegmentFacts(
        source_size=facts.source_size,
        committed_bytes=facts.committed_bytes,
        segment_size=context.segment_size,
    ))
    if plan.outcome is SegmentOutcome.ALL_COMMITTED:
        return SegmentStep(plan=plan)
    assert plan.start is not None and plan.end is not None
    spec = SegmentSpec(
        attempt=f"copy/{copy_id}", round_index=facts.round,
        target_name=facts.target.relative_path,
        range_start=plan.start, range_end=plan.end,
        chunk_size=context.chunk_size,
        stop=context.stop if context.stop is not None else threading.Event(),
    )
    ref = FileRef(
        file_id=facts.target.file_id, purpose=facts.target.purpose,
        relative_path=facts.target.relative_path, root=context.roots.staging,
    )
    result = await asyncio.to_thread(_transfer_segment, ref, context.roots, plan, spec,
                                     context.session)
    if result.error is SegmentFailure.SYNC_FAILED:
        raise CopySegmentError("segment_sync_failed", str(result.failure))
    if result.error is not None:
        raise CopySegmentError("segment_transfer_failed", str(result.error))
    if not result.synced:
        raise CopySegmentError("segment_sync_failed", "段尾同步未确认")
    key = context.key if context.key is not None else new_operation_key()
    outcome = context.repository.save_segment(
        ReliableSegment(
            copy_id=copy_id, copy_round=facts.round,
            committed_before=plan.start, segment_end=plan.end,
            synced=True, occurred_at=context.occurred_at,
        ),
        key, context.owned,
    )
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise CopySegmentError(
            "segment_save_unknown" if outcome.kind is DbOutcomeKind.UNKNOWN
            else "segment_save_failed",
            str(outcome.error),
        )
    return SegmentStep(plan=plan, saved=outcome.value, transfer=result)


def _observation_of(observation: FileObservation) -> TargetFileObservation:
    if observation.kind is FileObservationKind.VALID_OBJECT:
        return TargetFileObservation(observed=True, present=True, length=observation.size_bytes)
    if observation.kind is FileObservationKind.MISSING:
        return TargetFileObservation(observed=True, present=False)
    if observation.kind is FileObservationKind.TYPE_MISMATCH:
        raise ConsistencyError(f"目标路径不是普通文件: {observation.path}")
    return TargetFileObservation(observed=False, error=observation.error)


def _prepare_target(ref: FileRef, roots: BoundDirectories, decision: CopyDecision) -> None:
    """按决定创建或截断目标并同步；失败保留阶段并阻断续传。"""
    desired = decision.truncate_to if decision.truncate_to is not None else 0
    mutation = prepare_target(ref, roots, desired)
    if not mutation.can_continue:
        raise CopyPreparationError("target_prepare_failed", mutation.error or "")
    sync = sync_target(ref, roots)
    if sync.error is not None:
        raise CopyPreparationError("target_sync_failed", sync.error)


async def prepare_copy(copy_id: int, context: CopyContext) -> CopyStep:
    """为一份已建档拷贝准备续传：核对事实、准备目标并计划尝试。

    截断与重置完成事实分别可靠保存；准备失败时保留诊断，主机
    文件的实际状态由下一次准备重新观察。不重新分配交付或重拷
    轮次，不保存读取意图。
    """
    facts = context.repository.load_copy_state(copy_id, context.owned)
    ref = FileRef(
        file_id=facts.target.file_id, purpose=facts.target.purpose,
        relative_path=facts.target.relative_path, root=context.roots.staging,
    )
    observation = _observation_of(await inspect_file(ref, context.roots))
    decision = decide_resume(CopyFacts(
        source_size=facts.source_size,
        committed_bytes=facts.committed_bytes,
        reset_pending=facts.reset_pending,
        target=observation,
    ))
    if decision.outcome in (ResumeOutcome.CREATE, ResumeOutcome.TRUNCATE,
                            ResumeOutcome.RESET_TARGET):
        await asyncio.to_thread(_prepare_target, ref, context.roots, decision)
    reset: TargetResetOutcome | None = None
    if decision.outcome is ResumeOutcome.RESET_TARGET:
        key = context.reset_key if context.reset_key is not None else new_operation_key()
        outcome = context.repository.reset_copy_target(
            TargetResetRequest(copy_id=copy_id, occurred_at=context.occurred_at),
            key, context.owned,
        )
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise CopyPreparationError(
                "reset_save_failed", str(outcome.error),
            )
        reset = outcome.value.outcome
    attempt = decide_attempt(facts.attempts, facts.round)
    return CopyStep(decision=decision, attempt=attempt, reset=reset)


class IntegrityOutcome(Enum):
    """完整性判定的分区；与源端校验决策表一一对应。"""

    MATCHED = "matched"
    MISMATCHED = "mismatched"
    SOURCE_UNAVAILABLE = "source_unavailable"
    VERIFICATION_FAILED = "verification_failed"
    CAPABILITY_UNDETERMINED = "capability_undetermined"


class RecopyPlan(Enum):
    """摘要不一致后的重拷判定；读取尝试次数不参与本判定。"""

    REGISTER = "register"
    EXHAUSTED = "exhausted"
    OWNER_NOT_ELIGIBLE = "owner_not_eligible"


@dataclass(frozen=True)
class IntegrityFacts:
    """完整性判定所需的固定事实。

    source_sha256 为空表达本次未可靠取得源摘要；支持源端校验时
    必须同时给出获取结果或获取失败之一。owner_running 表达发起
    责任是否仍允许执行；读取尝试次数不属于本判定输入。
    """

    source_size: int
    committed_bytes: int
    source_support: SourceChecksumSupport
    source_sha256: str | None = None
    source_error: object | None = None
    target_sha256: str | None = None
    target_error: object | None = None
    owner_running: bool = True
    recopies_used: int = 0
    max_recopies: int = 0

    def __post_init__(self) -> None:
        for name, value in (("source_size", self.source_size),
                            ("committed_bytes", self.committed_bytes),
                            ("recopies_used", self.recopies_used),
                            ("max_recopies", self.max_recopies)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if not isinstance(self.source_support, SourceChecksumSupport):
            raise ValueError(
                f"源端摘要能力必须使用 SourceChecksumSupport: {self.source_support!r}")
        if not isinstance(self.owner_running, bool):
            raise ValueError(f"发起责任资格必须是布尔值: {self.owner_running!r}")
        _require_digest("source_sha256", self.source_sha256)
        _require_digest("target_sha256", self.target_sha256)


@dataclass(frozen=True)
class IntegrityDecision:
    """完整性决定；摘要不一致时附带重拷判定。"""

    outcome: IntegrityOutcome
    recopy: RecopyPlan | None = None
    error: object | None = None


def decide_integrity(facts: IntegrityFacts) -> IntegrityDecision:
    """按源端摘要能力与比较结果决定完整性收尾分区。

    主机摘要必须已经可靠计算；能力未知先确认能力，不折叠为不
    支持分支。摘要不一致时按重拷次数与本次上限独立判定，读取
    尝试预算不影响本决定。
    """
    if not isinstance(facts, IntegrityFacts):
        raise TypeError(f"完整性判定必须使用 IntegrityFacts: {facts!r}")
    if facts.committed_bytes != facts.source_size:
        raise ConsistencyError(
            f"可靠进度 {facts.committed_bytes} 尚未到达固定源长度 {facts.source_size}"
        )
    if facts.target_sha256 is None:
        if facts.target_error is None:
            raise ConsistencyError("主机摘要计算失败必须携带失败诊断")
        return IntegrityDecision(
            outcome=IntegrityOutcome.VERIFICATION_FAILED, error=facts.target_error)
    if facts.source_support is SourceChecksumSupport.UNDETERMINED:
        return IntegrityDecision(outcome=IntegrityOutcome.CAPABILITY_UNDETERMINED)
    if facts.source_support is SourceChecksumSupport.UNSUPPORTED:
        if facts.source_sha256 is not None:
            raise ConsistencyError("明确不支持的源不能携带源端摘要")
        return IntegrityDecision(outcome=IntegrityOutcome.SOURCE_UNAVAILABLE)
    if facts.source_sha256 is None:
        if facts.source_error is None:
            raise ConsistencyError("支持源端校验时必须给出获取结果或获取失败")
        return IntegrityDecision(
            outcome=IntegrityOutcome.VERIFICATION_FAILED, error=facts.source_error)
    if facts.source_sha256 == facts.target_sha256:
        return IntegrityDecision(outcome=IntegrityOutcome.MATCHED)
    if not facts.owner_running:
        plan = RecopyPlan.OWNER_NOT_ELIGIBLE
    elif facts.recopies_used >= facts.max_recopies:
        plan = RecopyPlan.EXHAUSTED
    else:
        plan = RecopyPlan.REGISTER
    return IntegrityDecision(outcome=IntegrityOutcome.MISMATCHED, recopy=plan)


#: 校验事务允许保存的终局状态；进行中状态由编排过程表达，不落库。
_TERMINAL_VERIFICATION = frozenset(
    int(_VERIFICATION[member])
    for member in ("MATCHED", "MISMATCHED", "SOURCE_CHECKSUM_UNAVAILABLE", "FAILED")
)


@dataclass(frozen=True)
class VerificationSave:
    """一次校验结果的保存申请。

    state 是目标校验状态；MATCHED/MISMATCHED 必须携带源摘要，
    FAILED 必须携带错误诊断对象且不能携带源摘要，明确不支持分
    支两者都不携带。error_json 是结构化诊断对象，由持久化内核
    按 JSON 列约定序列化。
    """

    copy_id: int
    state: int
    source_sha256: str | None
    target_sha256: str
    error_json: Mapping[str, Any] | None
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        if isinstance(self.state, bool) or not isinstance(self.state, int) \
                or self.state not in _TERMINAL_VERIFICATION:
            raise ValueError(f"校验状态必须是登记的终局状态: {self.state!r}")
        _require_digest("source_sha256", self.source_sha256)
        _require_digest("target_sha256", self.target_sha256)
        if self.error_json is not None and not isinstance(self.error_json, Mapping):
            raise ValueError(f"校验诊断必须是对象: {self.error_json!r}")
        if self.state == int(_VERIFICATION.FAILED):
            if self.source_sha256 is not None or self.error_json is None:
                raise ValueError("校验失败必须携带错误诊断且不能携带源摘要")
        elif self.state == int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE):
            if self.source_sha256 is not None or self.error_json is not None:
                raise ValueError("明确不支持分支不携带源摘要或错误诊断")
        else:
            if self.source_sha256 is None or self.error_json is not None:
                raise ValueError("摘要比较结果必须携带源摘要且不携带错误诊断")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事实时刻超出 SQLite 整数范围: {self.occurred_at!r}")


class VerificationDisposition(Enum):
    """校验保存事务的可靠结果分区。"""

    SAVED = "saved"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class VerificationResult:
    """校验保存事务的返回；SKIPPED 表示核对到取消或不在执行。"""

    disposition: VerificationDisposition
    state: int
    reason: str | None = None


@dataclass(frozen=True)
class RecopyRegistration:
    """一次整片重拷的登记申请。

    source_sha256 与 target_sha256 是判定不一致所依据的两个摘要；
    max_recopies 是本次判定实际采用的本地上限。
    """

    copy_id: int
    source_sha256: str
    target_sha256: str
    max_recopies: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        if self.source_sha256 is None or self.target_sha256 is None:
            raise ValueError("重拷登记必须携带判定不一致所依据的两个摘要")
        _require_digest("source_sha256", self.source_sha256)
        _require_digest("target_sha256", self.target_sha256)
        if isinstance(self.max_recopies, bool) or not isinstance(self.max_recopies, int) \
                or self.max_recopies < 0:
            raise ValueError(f"本次重拷上限必须是非负整数: {self.max_recopies!r}")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事实时刻超出 SQLite 整数范围: {self.occurred_at!r}")


class RecopyDisposition(Enum):
    """重拷登记事务的分区。"""

    REGISTERED = "registered"
    EXHAUSTED = "exhausted"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RecopyResult:
    """重拷登记事务的返回；登记成功携带新轮次与已用次数。"""

    disposition: RecopyDisposition
    round: int | None = None
    recopies_used: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PreparedRequest:
    """副本准备完成的保存申请：完整目标事实与事实时刻。"""

    copy_id: int
    target_sha256: str
    size_bytes: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        _require_digest("target_sha256", self.target_sha256)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) \
                or self.size_bytes < 0:
            raise ValueError(f"完整目标长度必须是非负整数: {self.size_bytes!r}")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事实时刻超出 SQLite 整数范围: {self.occurred_at!r}")


@dataclass(frozen=True)
class PreparedCopy:
    """已可靠准备的副本事实；released 表达本次事务解除取回源依赖。

    录像内部输入副本的源保留由所属处理状态表达，不解除取回项
    源依赖；准备完成事务尚未确认提交时，调用方不能视为已解除。
    """

    copy_id: int
    target_file_id: int
    size_bytes: int
    sha256: str
    source_dependency_released: bool


class CopyCompletionError(RuntimeError):
    """完整性收尾失败；保留实际阶段，不猜测未确认事实。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class SourceDigest:
    """源端摘要获取的一次结果；digest 为空表达获取失败。"""

    digest: str | None = None
    error: object | None = None

    def __post_init__(self) -> None:
        _require_digest("digest", self.digest)
        if self.digest is None and self.error is None:
            raise ValueError("源端摘要获取失败必须携带失败诊断")


class SourceDigestReader(Protocol):
    """源端摘要获取端口；设备摘要驱动与测试替身共同满足。"""

    async def read_digest(self) -> SourceDigest: ...


@dataclass(frozen=True)
class CompletionContext:
    """完整性收尾的协作者、源摘要端口与本次事实时刻。"""

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    roots: BoundDirectories
    occurred_at: int
    digest: SourceDigestReader | None = None
    max_recopies: int = 1
    key: OperationKey | None = None


class CompletionPhase(Enum):
    """完整性收尾的出口：已准备完成，或已登记新一轮重拷。"""

    PREPARED = "prepared"
    RECOPY_REGISTERED = "recopy_registered"


@dataclass(frozen=True)
class CompletionStep:
    """一次完整性收尾的结果。"""

    phase: CompletionPhase
    prepared: PreparedCopy | None = None
    recopy: RecopyResult | None = None


class PreparedDisposition(Enum):
    """准备完成事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class PreparedSaveOutcome:
    """准备完成事务的返回。

    SKIPPED 表示核对到取消或不在执行；ALREADY 表示本次事实先前
    已完整提交（断电恢复重放），不重复保存事件。
    """

    disposition: PreparedDisposition
    prepared: PreparedCopy | None = None
    reason: str | None = None


_OWNER_ERROR_STAGES = {"cancel_requested": "owner_canceled",
                       "owner_not_running": "owner_not_running"}


def _owner_stage(reason: str | None) -> str:
    return _OWNER_ERROR_STAGES.get(reason or "", "owner_not_running")


def _require_completed(outcome, stage_failed: str, stage_unknown: str) -> None:
    """仓储结果不是完成时按分区保留阶段诊断。"""
    if outcome.kind is DbOutcomeKind.COMPLETED:
        return
    raise CopyCompletionError(
        stage_unknown if outcome.kind is DbOutcomeKind.UNKNOWN else stage_failed,
        str(outcome.error),
    )


async def complete_copy(copy_id: int, context: CompletionContext) -> CompletionStep:
    """收尾一份全部字节可靠保存的拷贝：校验、有限重拷或准备完成。

    主机摘要必须计算；源端支持时必须比较，获取失败不能降级为不
    支持。摘要不一致在同一事务保存不一致事实并登记新一轮从零
    拷贝，随后由调用方重置目标并重新分段。准备完成与解除取回源
    依赖共同保存；提交结果未知时不冒充已解除。
    """
    facts = context.repository.load_copy_state(copy_id, context.owned)
    if facts.committed_bytes != facts.source_size:
        raise CopyCompletionError(
            "copy_incomplete",
            f"可靠进度 {facts.committed_bytes} 尚未到达固定源长度 {facts.source_size}")
    state = facts.verification_state
    if state == int(_VERIFICATION.FAILED):
        raise CopyCompletionError("verification_failed", "校验已终局失败")
    if state == int(_VERIFICATION.MISMATCHED):
        if facts.target_sha256 is None or facts.source_sha256 is None:
            raise ConsistencyError("已保存的不一致结果缺少判定所依据的摘要")
        return await _register_recopy(copy_id, context, facts, facts.target_sha256)
    if state in (int(_VERIFICATION.MATCHED),
                 int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE)):
        digest = facts.target_sha256
        if digest is None:
            raise ConsistencyError("已完成的校验缺少主机摘要")
        return await _save_prepared(copy_id, context, digest)
    return await _verify_and_finish(copy_id, context, facts)


async def _verify_and_finish(copy_id, context, facts) -> CompletionStep:
    """计算主机摘要、取得源摘要并按完整性判定收尾。"""
    ref = FileRef(
        file_id=facts.target.file_id, purpose=facts.target.purpose,
        relative_path=facts.target.relative_path, root=context.roots.staging,
    )
    hashed = await asyncio.to_thread(hash_target, ref, context.roots)
    if hashed.error is not None or hashed.digest is None:
        raise CopyCompletionError("host_digest_failed", hashed.error or "未知失败")
    source_sha256, source_error = facts.source_sha256, None
    if source_sha256 is None \
            and facts.source_support is SourceChecksumSupport.SUPPORTED:
        if context.digest is None:
            raise CopyCompletionError(
                "digest_reader_missing", "支持源端校验但未提供源摘要端口")
        fetched = await context.digest.read_digest()
        if fetched.digest is None:
            source_error = fetched.error
        else:
            source_sha256 = fetched.digest
    decision = decide_integrity(IntegrityFacts(
        source_size=facts.source_size,
        committed_bytes=facts.committed_bytes,
        source_support=facts.source_support,
        source_sha256=source_sha256,
        source_error=source_error,
        target_sha256=hashed.digest,
        owner_running=True,
        recopies_used=facts.recopies_used,
        max_recopies=context.max_recopies,
    ))
    if decision.outcome is IntegrityOutcome.CAPABILITY_UNDETERMINED:
        raise CopyCompletionError(
            "checksum_support_undetermined", "先确认设备源端摘要能力")
    if decision.outcome is IntegrityOutcome.MISMATCHED:
        assert source_sha256 is not None
        return await _register_recopy(copy_id, context, facts, hashed.digest,
                                      source_sha256)
    if decision.outcome is IntegrityOutcome.VERIFICATION_FAILED:
        save_state = int(_VERIFICATION.FAILED)
        source_value = None
        error_json = {"reason": str(decision.error)}
    elif decision.outcome is IntegrityOutcome.SOURCE_UNAVAILABLE:
        save_state = int(_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE)
        source_value, error_json = None, None
    else:
        save_state = int(_VERIFICATION.MATCHED)
        source_value, error_json = source_sha256, None
    key = context.key if context.key is not None else new_operation_key()
    outcome = context.repository.save_verification(
        VerificationSave(
            copy_id=copy_id, state=save_state, source_sha256=source_value,
            target_sha256=hashed.digest, error_json=error_json,
            occurred_at=context.occurred_at,
        ),
        key, context.owned,
    )
    if outcome.kind is DbOutcomeKind.COMPLETED \
            and outcome.value.disposition is VerificationDisposition.SKIPPED:
        raise CopyCompletionError(
            _owner_stage(outcome.value.reason), "发起责任不再执行")
    _require_completed(outcome, "verification_save_failed", "verification_save_unknown")
    if save_state == int(_VERIFICATION.FAILED):
        raise CopyCompletionError("verification_failed", str(decision.error))
    return await _save_prepared(copy_id, context, hashed.digest)


async def _register_recopy(copy_id, context, facts, target_sha256: str,
                           source_sha256: str | None = None) -> CompletionStep:
    """保存不一致事实（如尚未保存）并按剩余额度登记新一轮重拷。"""
    if source_sha256 is None:
        source_sha256 = facts.source_sha256
    if source_sha256 is None or not target_sha256:
        raise ConsistencyError("重拷登记缺少判定所依据的摘要")
    key = context.key if context.key is not None else new_operation_key()
    outcome = context.repository.register_recopy(
        RecopyRegistration(
            copy_id=copy_id, source_sha256=source_sha256,
            target_sha256=target_sha256, max_recopies=context.max_recopies,
            occurred_at=context.occurred_at,
        ),
        key, context.owned,
    )
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        _require_completed(outcome, "recopy_register_failed", "recopy_register_unknown")
    result = outcome.value
    if result.disposition is RecopyDisposition.SKIPPED:
        raise CopyCompletionError(_owner_stage(result.reason), "发起责任不再执行")
    if result.disposition is RecopyDisposition.EXHAUSTED:
        raise CopyCompletionError(
            "recopy_budget_exhausted",
            f"已用 {facts.recopies_used} 次重拷，本次上限 {context.max_recopies}")
    return CompletionStep(phase=CompletionPhase.RECOPY_REGISTERED, recopy=result)


async def _save_prepared(copy_id, context, target_sha256: str) -> CompletionStep:
    """保存准备完成事实；与解除取回源依赖共同提交。"""
    facts = context.repository.load_copy_state(copy_id, context.owned)
    key = context.key if context.key is not None else new_operation_key()
    outcome = context.repository.save_prepared(
        PreparedRequest(
            copy_id=copy_id, target_sha256=target_sha256,
            size_bytes=facts.source_size, occurred_at=context.occurred_at,
        ),
        key, context.owned,
    )
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        _require_completed(outcome, "prepared_save_failed", "prepared_save_unknown")
    result = outcome.value
    if result.disposition is PreparedDisposition.SKIPPED:
        raise CopyCompletionError(_owner_stage(result.reason), "发起责任不再执行")
    assert result.prepared is not None
    return CompletionStep(phase=CompletionPhase.PREPARED, prepared=result.prepared)
