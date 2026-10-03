"""共用拷贝的续传位置决策、目标准备与读取尝试计划。

取回、异常原片检查和修复的输入拷贝共用本模块：固定源身份与
长度后，按可靠进度和主机文件事实决定续传位置；设备读取开始
前的意图与次数由操作仓储保存，本模块不重复预算判断。行为契
约为[重启后选择续传位置](../../architecture/file-copy.md#重启后选择续传位置)。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from camctl.contracts.enums import enum_for
from camctl.contracts.values import (
    ConsistencyError, MAX_OBJECT_ID, ObjectId, OperationKey, UtcMicros,
    new_operation_key,
)
from camctl.host_files.io import prepare_target, sync_target
from camctl.host_files.models import (
    BoundDirectories, FileObservation, FileObservationKind, FilePurpose, FileRef,
)
from camctl.host_files.paths import inspect_file
from camctl.persistence.models import DbOutcomeKind

if TYPE_CHECKING:
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.runtime import OwnedConnection

_ATTEMPT_STATUS = enum_for("operation_attempts.status")


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


@dataclass(frozen=True)
class CopyStep:
    """一次续传准备的结果：续传决定、读取尝试计划与重置事实。"""

    decision: CopyDecision
    attempt: AttemptDecision
    reset: TargetResetOutcome | None = None


@dataclass(frozen=True)
class CopyContext:
    """续传准备的协作者：仓储、连接、目录绑定与事实时刻。"""

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    roots: BoundDirectories
    occurred_at: int
    reset_key: OperationKey | None = None


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
