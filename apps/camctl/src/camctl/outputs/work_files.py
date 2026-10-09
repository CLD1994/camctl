"""中间文件生命周期判定与有限清理。

对应[取回中间文件的保留与清理](../../architecture/obtaining-outputs.md#取回中间文件的保留与清理)
与[中间文件清理的运行预算](../../architecture/file-handoff.md#中间文件清理的运行预算)：
保留状态优先于清理判定；归属终态与操作停止分别核实后才允许删除，
删除失败保留责任由后续正常运行有限重试，不重开原动作。历史清理按
固定范围上界、每轮额度与可靠游标单轮推进，本次运行中新形成的清理
责任由所属业务流程经同一端口及时首次清理，不占历史额度。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import (
    MAX_OBJECT_ID, ObjectId, OperationKey, UtcMicros, new_operation_key,
)
from camctl.contracts.workflow_errors import (
    registered_error, validate_error_details,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

if TYPE_CHECKING:
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.runtime import OwnedConnection

_RETENTION = enum_for("intermediate_files.retention_state")
_CLEANUP = enum_for("intermediate_files.cleanup_state")
_PURPOSE = enum_for("intermediate_files.purpose")

#: 中间文件删除明确失败的公共错误码（workflow-codes.json 权威装载）。
WORK_FILE_DELETE_FAILED = "work_file_delete_failed"
#: 动作归属中间文件删除失败的公共错误码；详情携带文件身份。
ACTION_WORK_FILE_DELETE_FAILED = "action_work_file_delete_failed"


class WorkFileAction(Enum):
    """单个中间文件的清理判定分区；与保留清理决策表一一对应。"""

    KEEP_REQUIRED = "keep_required"
    NOT_MANAGED = "not_managed"
    ALREADY_CLEANED = "already_cleaned"
    KEEP_IN_USE = "keep_in_use"
    DELETE = "delete"


def _require_state(name: str, value: Any, enum_values: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数登记值: {value!r}")
    members = [int(member) for member in enum_values]
    if value not in members:
        raise ValueError(f"{name} 不在登记范围内: {value!r}")
    return value


@dataclass(frozen=True)
class WorkFileFacts:
    """清理判定所需的单个中间文件事实。

    owner_finished 与 operations_stopped 是加载方按数据库可靠记录
    核实后的事实：归属交付或动作已进入终态，相关读取、校验、移动
    和重试操作均已确认停止。
    """

    file_id: int
    purpose: int
    owner_action_id: int | None
    owner_delivery_id: int | None
    relative_path: str
    retention_state: int
    cleanup_state: int
    owner_finished: bool
    operations_stopped: bool

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        _require_state("用途", self.purpose, _PURPOSE)
        _require_state("保留状态", self.retention_state, _RETENTION)
        _require_state("清理状态", self.cleanup_state, _CLEANUP)
        has_action = self.owner_action_id is not None
        has_delivery = self.owner_delivery_id is not None
        if has_action == has_delivery:
            raise ValueError(
                f"中间文件必须恰好归属一个责任人: {self.owner_action_id!r}"
                f" / {self.owner_delivery_id!r}")
        if self.owner_action_id is not None:
            ObjectId(self.owner_action_id)
        if self.owner_delivery_id is not None:
            ObjectId(self.owner_delivery_id)
        if self.purpose == int(_PURPOSE.DELIVERY_COPY) and not has_delivery:
            raise ValueError("交付副本必须归属交付")
        if self.purpose != int(_PURPOSE.DELIVERY_COPY) and not has_action:
            raise ValueError("处理用途的中间文件必须归属动作")
        if not isinstance(self.relative_path, str) or not self.relative_path:
            raise ValueError(f"工作路径必须是非空文本: {self.relative_path!r}")
        for name in ("owner_finished", "operations_stopped"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(
                    f"{name} 必须是布尔值: {getattr(self, name)!r}")


@dataclass(frozen=True)
class WorkFileDecision:
    """一次清理判定；reason 保留诊断说明。"""

    action: WorkFileAction
    reason: str = ""


def classify_work_file(facts: WorkFileFacts) -> WorkFileDecision:
    """按保留状态、归属终态与操作停止情况决定中间文件的处理。

    已提升为正式产物或已交接的文件不归自动清理；归属交付或动作
    仍活跃时保留文件及其续传、发布责任；归属已终态但相关操作尚未
    确认停止时先收场不删除。归属终态且操作停止的 REQUIRED 副本由
    编排先释放保留状态再清理，RELEASABLE 候选直接清理；已完成的
    清理不重复。证据不足时保留文件，不猜测可删。
    """
    if not isinstance(facts, WorkFileFacts):
        raise TypeError(f"清理判定必须使用 WorkFileFacts: {facts!r}")
    if facts.retention_state in (
        int(_RETENTION.PROMOTED), int(_RETENTION.HANDED_OFF),
    ):
        return WorkFileDecision(
            WorkFileAction.NOT_MANAGED, "文件已提升或交接，不归自动清理")
    if facts.retention_state == int(_RETENTION.RELEASABLE) \
            and facts.cleanup_state == int(_CLEANUP.COMPLETED):
        return WorkFileDecision(
            WorkFileAction.ALREADY_CLEANED, "清理已完成")
    if not facts.owner_finished:
        return WorkFileDecision(
            WorkFileAction.KEEP_REQUIRED, "归属交付或动作尚未终态")
    if not facts.operations_stopped:
        return WorkFileDecision(
            WorkFileAction.KEEP_IN_USE, "相关操作尚未确认停止")
    return WorkFileDecision(
        WorkFileAction.DELETE, "归属终态且操作停止，可以清理")


def _require_limit(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是大于等于 1 的整数: {value!r}")
    return value


@dataclass(frozen=True)
class WorkFileLimits:
    """本地清理配置：每批读取上限与每次运行历史检查上限。"""

    batch_size: int
    limit_per_run: int

    def __post_init__(self) -> None:
        _require_limit("批量上限", self.batch_size)
        _require_limit("每轮上限", self.limit_per_run)


def effective_batch(batch_size: int, remaining: int) -> int:
    """本批实际读取数：批量配置不得越过本次剩余额度。"""
    return min(batch_size, max(remaining, 0))


@dataclass(frozen=True)
class CleanupScan:
    """一次正常运行的历史清理扫描状态。

    start_after 是本次运行的起始继续位置（上次可靠保存的游标或 0），
    ceiling 是本次固定的候选登记上界；本次最多检查范围一轮：先从
    继续位置读到上界，绕回候选范围起点后再读到本次起点即结束。
    """

    start_after: int
    ceiling: int
    remaining: int
    batch_size: int
    wrapped: bool = False
    after_id: int = 0

    def __post_init__(self) -> None:
        _require_limit("批量上限", self.batch_size)
        for name in ("start_after", "ceiling", "remaining", "after_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise ValueError(f"{name} 必须是非负整数: {value!r}")
        if not isinstance(self.wrapped, bool):
            raise ValueError(f"绕回标志必须是布尔值: {self.wrapped!r}")

    def next_query(self):
        """返回下一批读取区间与更新后的扫描状态；None 表示本轮结束。

        区间为 (after_id, limit]；到达固定上界时绕回候选范围起点并
        置 wrapped，绕回后到达本次起始继续位置即结束本轮。
        """
        if self.remaining <= 0 or self.ceiling <= 0:
            return None
        limit = effective_batch(self.batch_size, self.remaining)
        if not self.wrapped:
            if self.after_id >= self.ceiling:
                if min(self.start_after, self.ceiling) == 0:
                    return None
                return (0, limit), replace(self, wrapped=True, after_id=0)
            return (self.after_id, limit), self
        if self.after_id >= self.segment_ceiling():
            return None
        return (self.after_id, limit), self

    def segment_ceiling(self) -> int:
        """当前读取段的上界（含）：第一轮到固定上界，绕回后到本次起点。"""
        return self.ceiling if not self.wrapped else min(self.start_after, self.ceiling)

    def advanced(self, checked_id: int) -> "CleanupScan":
        """一条记录检查完成：消耗一条额度并推进到该记录位置。"""
        if not isinstance(checked_id, int) or isinstance(checked_id, bool) \
                or checked_id <= self.after_id or checked_id > self.segment_ceiling():
            raise ValueError(f"检查位置必须向后推进: {checked_id!r}")
        if self.remaining <= 0:
            raise ValueError("额度已耗尽，不能再检查记录")
        return replace(self, after_id=checked_id, remaining=self.remaining - 1)

    def exhausted_to_end(self) -> "CleanupScan":
        """当前区间无候选：位置推到段末，不消耗额度。"""
        return replace(self, after_id=self.segment_ceiling())


# ---- 清理命令请求与失败对象 ----


def _require_timestamp(occurred_at: int) -> None:
    timestamp = UtcMicros(occurred_at)
    if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
        raise ValueError(f"事实时刻超出 SQLite 整数范围: {occurred_at!r}")


class WorkFileOutcome(Enum):
    """一次清理尝试的持久化结果分区。"""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkFileFailure:
    """中间文件清理失败的公共错误诊断；按公共登记构造并校验。"""

    code: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise ValueError(f"错误名必须是文本: {self.code!r}")
        registered_error(self.code)
        if not isinstance(self.details, Mapping):
            raise ValueError(f"错误详情必须是对象: {self.details!r}")
        validate_error_details(self.code, self.details)

    @property
    def stage(self) -> str:
        return registered_error(self.code)["stage"]

    def as_json(self) -> dict[str, Any]:
        """持久化使用的错误对象；字段顺序稳定。"""
        return {
            "code": self.code, "stage": self.stage,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RetentionRelease:
    """释放保留状态的申请：归属已终态且实际操作停止后使用。"""

    file_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        _require_timestamp(self.occurred_at)


@dataclass(frozen=True)
class CleanupIntent:
    """自动清理意图的保存申请；意图先于物理删除提交。"""

    file_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        _require_timestamp(self.occurred_at)


@dataclass(frozen=True)
class CleanupResultSave:
    """自动清理结果的保存申请；可与游标推进同事务提交。"""

    file_id: int
    outcome: WorkFileOutcome
    error: WorkFileFailure | None
    occurred_at: int
    advance_cursor: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        if not isinstance(self.outcome, WorkFileOutcome):
            raise TypeError(f"清理结果必须使用 WorkFileOutcome: {self.outcome!r}")
        if self.outcome is WorkFileOutcome.COMPLETED and self.error is not None:
            raise ValueError("清理完成不得携带错误对象")
        if self.outcome is WorkFileOutcome.FAILED:
            if not isinstance(self.error, WorkFileFailure):
                raise TypeError("清理失败必须携带 WorkFileFailure")
            if (self.error.details.get("delivery_id") is None
                    and self.error.details.get("file_id") is None):
                raise ValueError("清理失败详情缺少归属身份（交付或文件）")
        if not isinstance(self.advance_cursor, bool):
            raise ValueError(f"游标推进必须是布尔值: {self.advance_cursor!r}")
        _require_timestamp(self.occurred_at)


@dataclass(frozen=True)
class CleanupChecked:
    """仅推进历史清理游标的申请；用于检查过但不能删除的记录。"""

    file_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.file_id)
        _require_timestamp(self.occurred_at)


class RetentionDisposition(Enum):
    """释放保留事务的分区。"""

    RELEASED = "released"
    ALREADY = "already"


@dataclass(frozen=True)
class RetentionReleaseOutcome:
    """释放保留事务的返回。"""

    disposition: RetentionDisposition


class CleanupIntentDisposition(Enum):
    """清理意图事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupIntentOutcome:
    """清理意图事务的返回。"""

    disposition: CleanupIntentDisposition


class CleanupResultDisposition(Enum):
    """清理结果事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupResultSaveOutcome:
    """清理结果事务的返回。"""

    disposition: CleanupResultDisposition


class CleanupCheckedDisposition(Enum):
    """游标推进事务的分区。"""

    SAVED = "saved"
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupCheckedOutcome:
    """游标推进事务的返回。"""

    disposition: CleanupCheckedDisposition


# ---- 清理编排 ----


class WorkFileCleanupError(RuntimeError):
    """中间文件清理的数据库类失败；保留实际阶段诊断。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass
class WorkFileHistory:
    """本次运行固定的历史范围与已经消耗的检查额度。"""

    scan: CleanupScan | None = None
    finished: bool = False


@dataclass(frozen=True)
class PendingWorkFileResult:
    """实际文件结果已经取得，原保存请求尚待可靠核实。"""

    request: CleanupResultSave
    key: OperationKey
    outcome: WorkFileSingleOutcome
    detail: str | None
    scan: CleanupScan | None = None


@dataclass(frozen=True)
class WorkFileContext:
    """清理编排的协作者、工作目录、事实时刻、预算与会话登记。

    processed 登记本次运行已实际处理过的文件及结果；同一文件被
    首次入口和历史扫描先后发现时复用该结果，不发起第二次尝试。
    """

    repository: "OutputsRepository"
    owned: "OwnedConnection"
    staging: Path
    occurred_at: int
    limits: WorkFileLimits
    processed: dict[int, WorkFileSingleOutcome] = field(default_factory=dict)
    #: 正式装配注入本会话共用执行器，核实媒体、拷贝及清理实际占用。
    executor: "FileTaskExecutor | None" = None
    history: WorkFileHistory = field(default_factory=WorkFileHistory)
    pending_results: dict[int, PendingWorkFileResult] = field(default_factory=dict)
    stop_requested: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.limits, WorkFileLimits):
            raise TypeError(f"清理配置必须使用 WorkFileLimits: {self.limits!r}")
        if not isinstance(self.staging, Path):
            raise TypeError(f"staging 必须是目录路径: {self.staging!r}")
        if not isinstance(self.processed, dict):
            raise TypeError(f"会话登记必须是字典: {self.processed!r}")
        if self.executor is None:
            object.__setattr__(self, "executor", FileTaskExecutor(Supervisor()))
        _require_timestamp(self.occurred_at)


class WorkFileSingleOutcome(Enum):
    """单个中间文件一次清理编排的出口。"""

    DELETED = "deleted"
    ALREADY_CLEANED = "already_cleaned"
    KEPT = "kept"
    NOT_MANAGED = "not_managed"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkFileSingleResult:
    """单个中间文件的清理结果；decision 保留判定依据。"""

    file_id: int
    decision: WorkFileDecision
    outcome: WorkFileSingleOutcome
    detail: str | None = None


@dataclass(frozen=True)
class WorkFileCleanupResult:
    """一次历史清理扫描的汇总；额度内检查的记录各归其类。"""

    checked: int
    cleaned: int
    kept: int
    failed: int


def _observe_work_file(path: Path) -> bool | OSError:
    """观察工作位置是否仍有副本；观察失败保留实际错误。"""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as failure:
        return failure
    return True


def _remove_work_file(path: Path) -> None:
    """删除工作副本；删除失败保留实际异常。"""
    path.unlink()


def _require_completed(outcome, stage: str) -> None:
    """仓储结果不是完成时按数据库错误保留阶段诊断。"""
    if outcome.kind is DbOutcomeKind.COMPLETED:
        return
    raise WorkFileCleanupError(stage, str(outcome.error))


def _delete_failure(facts: WorkFileFacts) -> WorkFileFailure:
    """删除失败按归属构造公共错误详情；交付与动作归属分别登记。"""
    if facts.owner_delivery_id is not None:
        return WorkFileFailure(
            code=WORK_FILE_DELETE_FAILED,
            details={"delivery_id": str(facts.owner_delivery_id)},
        )
    return WorkFileFailure(
        code=ACTION_WORK_FILE_DELETE_FAILED,
        details={"file_id": str(facts.file_id)},
    )


def _save_result(
    facts: WorkFileFacts, outcome: WorkFileOutcome,
    failure: WorkFileFailure | None, context: WorkFileContext,
    *, advance_cursor: bool, detail: str | None = None,
) -> None:
    """保存清理结果；与游标推进同一事务提交。"""
    request = CleanupResultSave(
        file_id=facts.file_id, outcome=outcome, error=failure,
        occurred_at=context.occurred_at, advance_cursor=advance_cursor,
    )
    pending = PendingWorkFileResult(
        request=request, key=new_operation_key(),
        outcome=(WorkFileSingleOutcome.FAILED if outcome is WorkFileOutcome.FAILED
                 else WorkFileSingleOutcome.DELETED), detail=detail,
        scan=context.history.scan if advance_cursor else None)
    context.pending_results[facts.file_id] = pending
    _save_pending_result(pending, context)


def _save_pending_result(
    pending: PendingWorkFileResult, context: WorkFileContext,
) -> None:
    """同一拥有者只核实完整原请求，不重新观察或删除文件。"""
    result = context.repository.save_cleanup_result(
        pending.request, pending.key, context.owned)
    _require_completed(result, "cleanup_result_failed")
    context.processed[pending.request.file_id] = pending.outcome
    context.pending_results.pop(pending.request.file_id, None)


async def _perform_deletion(
    facts: WorkFileFacts, context: WorkFileContext, *, advance_cursor: bool,
) -> tuple[WorkFileSingleOutcome, str | None, bool]:
    """已判定可清理的文件：意图、观察、删除与结果的有限一次尝试。

    观察失败或删除失败按归属保存公共错误（交付或动作归属的错误
    详情均已登记）；删除成功与失败的结果都可靠保存，未决责任留
    给后续正常运行重新核实，不中断其余记录。
    """
    if facts.cleanup_state != int(_CLEANUP.RUNNING):
        intent = context.repository.save_cleanup_intent(
            CleanupIntent(
                file_id=facts.file_id, occurred_at=context.occurred_at),
            new_operation_key(), context.owned)
        _require_completed(intent, "cleanup_intent_failed")
    path = context.staging / facts.relative_path
    observation = await asyncio.to_thread(_observe_work_file, path)
    if isinstance(observation, OSError):
        _save_result(
            facts, WorkFileOutcome.FAILED, _delete_failure(facts), context,
            advance_cursor=advance_cursor, detail=str(observation))
        return WorkFileSingleOutcome.FAILED, str(observation), True
    if observation is False:
        _save_result(
            facts, WorkFileOutcome.COMPLETED, None, context,
            advance_cursor=advance_cursor)
        return WorkFileSingleOutcome.DELETED, None, True
    try:
        await asyncio.to_thread(_remove_work_file, path)
    except OSError as failure:
        _save_result(
            facts, WorkFileOutcome.FAILED, _delete_failure(facts), context,
            advance_cursor=advance_cursor, detail=str(failure))
        return WorkFileSingleOutcome.FAILED, str(failure), True
    _save_result(
        facts, WorkFileOutcome.COMPLETED, None, context,
        advance_cursor=advance_cursor)
    return WorkFileSingleOutcome.DELETED, None, True


_SINGLE_OUTCOMES = {
    WorkFileAction.ALREADY_CLEANED: WorkFileSingleOutcome.ALREADY_CLEANED,
    WorkFileAction.KEEP_REQUIRED: WorkFileSingleOutcome.KEPT,
    WorkFileAction.KEEP_IN_USE: WorkFileSingleOutcome.KEPT,
    WorkFileAction.NOT_MANAGED: WorkFileSingleOutcome.NOT_MANAGED,
}


async def _delete_with_release(
    facts: WorkFileFacts, context: WorkFileContext, *, advance_cursor: bool,
) -> tuple[WorkFileSingleOutcome, str | None, bool]:
    """已判定可清理的文件先释放保留状态，再按意图、删除、结果推进。"""
    if facts.retention_state == int(_RETENTION.REQUIRED):
        release = context.repository.save_retention_release(
            RetentionRelease(
                file_id=facts.file_id, occurred_at=context.occurred_at),
            new_operation_key(), context.owned)
        _require_completed(release, "retention_release_failed")
    return await _perform_deletion(facts, context, advance_cursor=advance_cursor)


async def _owned_deletion(
    file_id: int, context: WorkFileContext, *, facts: WorkFileFacts | None = None,
    advance_cursor: bool = False,
) -> tuple[WorkFileSingleOutcome, str | None, bool]:
    """实际文件处理及原结果保存共用文件资格，取消后完成接手。"""
    async def perform(_control):
        pending = context.pending_results.get(file_id)
        if pending is not None:
            _save_pending_result(pending, context)
            return pending.outcome, pending.detail, True
        assert facts is not None
        result = await _delete_with_release(
            facts, context, advance_cursor=advance_cursor)
        context.processed[file_id] = result[0]
        return result

    assert context.executor is not None
    return await context.executor.run_owned_async_file_task(AsyncFileTask(
        task_id=FileTaskId(f"work-file/{file_id}/{new_operation_key()}"),
        file_ids=(file_id,), stage="work_file_cleanup",
        business="中间文件查询、删除及结果保存", resources=("state_db",),
        body=perform))


async def clean_one_work_file(
    file_id: int, context: WorkFileContext,
) -> WorkFileSingleResult:
    """清理单个中间文件；本次运行中新形成的责任经此入口首次清理。

    判定不可清理时保留文件并返回原因；可清理时先保存释放（归属
    终态且操作停止才允许），再按意图、删除、结果的顺序推进。首次
    清理不推进历史游标，也不占用历史扫描额度；本次已处理过的文件
    直接复用会话结果。
    """
    ObjectId(file_id)
    cached = context.processed.get(file_id)
    if cached is not None:
        return WorkFileSingleResult(
            file_id=file_id,
            decision=WorkFileDecision(
                WorkFileAction.DELETE, "复用本次运行已处理的实际结果"),
            outcome=cached)
    if file_id in context.pending_results:
        outcome, detail, _ = await _owned_deletion(file_id, context)
        return WorkFileSingleResult(
            file_id, WorkFileDecision(WorkFileAction.DELETE, "核实原实际结果"),
            outcome, detail)
    facts = context.repository.load_work_file_state(file_id, context.owned)
    assert context.executor is not None
    if file_id in context.executor.unfinished_files():
        facts = replace(facts, operations_stopped=False)
    decision = classify_work_file(facts)
    if decision.action is not WorkFileAction.DELETE:
        outcome = _SINGLE_OUTCOMES[decision.action]
        context.processed[file_id] = outcome
        return WorkFileSingleResult(
            file_id=file_id, decision=decision, outcome=outcome)
    outcome, detail, _persisted = await _owned_deletion(
        file_id, context, facts=facts, advance_cursor=False)
    context.processed[file_id] = outcome
    return WorkFileSingleResult(
        file_id=file_id, decision=decision, outcome=outcome, detail=detail)


async def clean_work_files(context: WorkFileContext) -> WorkFileCleanupResult:
    """按固定上界、剩余额度与可靠游标推进一次历史清理扫描。

    本次开始时固定候选登记上界，从上次可靠保存的游标之后按稳定
    顺序分批检查；到达上界后绕回候选范围起点，本轮最多检查一轮
    或达到额度即止。检查但不能删除的记录同样占用额度并推进游标；
    每条记录的检查结果可靠保存后才越过对应记录。
    """
    repository = context.repository
    history = context.history
    if history.finished:
        return WorkFileCleanupResult(checked=0, cleaned=0, kept=0, failed=0)
    if history.scan is None:
        ceiling = repository.max_cleanup_candidate_id(context.owned)
        cursor = repository.load_cleanup_cursor(context.owned)
        start_after = cursor if cursor is not None else 0
        history.scan = CleanupScan(
            start_after=start_after, ceiling=ceiling,
            remaining=context.limits.limit_per_run,
            batch_size=context.limits.batch_size, after_id=start_after)
    scan = history.scan
    cleaned = kept = failed = 0
    # COMMIT 已完成时该记录可能不再出现在候选 SQL 中。原实际
    # 输入仍可靠持有，必须先核实原键，再消费原检查额度及位置。
    for pending in tuple(context.pending_results.values()):
        if not pending.request.advance_cursor:
            continue
        if pending.scan is None:
            raise WorkFileCleanupError("cleanup_result_failed", "原历史检查范围缺失")
        outcome, _detail, _ = await _owned_deletion(pending.request.file_id, context)
        scan = pending.scan.advanced(pending.request.file_id)
        history.scan = scan
        cleaned += int(outcome is WorkFileSingleOutcome.DELETED)
        failed += int(outcome is WorkFileSingleOutcome.FAILED)
    query = scan.next_query()
    while query is not None:
        if context.stop_requested is not None and context.stop_requested():
            history.scan = scan
            return WorkFileCleanupResult(cleaned + kept + failed, cleaned, kept, failed)
        (after, limit), scan = query
        batch = repository.next_cleanup_candidates(
            after, scan.segment_ceiling(), limit, context.owned)
        if not batch:
            scan = scan.exhausted_to_end()
            query = scan.next_query()
            continue
        for file_id in batch:
            if context.stop_requested is not None and context.stop_requested():
                history.scan = scan
                return WorkFileCleanupResult(cleaned + kept + failed, cleaned, kept, failed)
            cached = context.processed.get(file_id)
            if cached is None:
                facts = repository.load_work_file_state(
                    file_id, context.owned)
                assert context.executor is not None
                if file_id in context.executor.unfinished_files():
                    facts = replace(facts, operations_stopped=False)
                decision = classify_work_file(facts)
                if decision.action is WorkFileAction.DELETE:
                    history.scan = scan
                    outcome, _detail, persisted = await _owned_deletion(
                        file_id, context, facts=facts, advance_cursor=True)
                    context.processed[file_id] = outcome
                    if not persisted:
                        # 结果尚无登记的错误对象可保存（动作归属），仍以
                        # 游标事务可靠保存本项检查位置。
                        checked = repository.save_cleanup_checked(
                            CleanupChecked(
                                file_id=file_id,
                                occurred_at=context.occurred_at),
                            new_operation_key(), context.owned)
                        _require_completed(checked, "cleanup_checked_failed")
                else:
                    outcome = _SINGLE_OUTCOMES[decision.action]
                    context.processed[file_id] = outcome
                    checked = repository.save_cleanup_checked(
                        CleanupChecked(
                            file_id=file_id,
                            occurred_at=context.occurred_at),
                        new_operation_key(), context.owned)
                    _require_completed(checked, "cleanup_checked_failed")
            else:
                # 复用本次实际结果，只推进游标，不发起第二次尝试。
                outcome = cached
                checked = repository.save_cleanup_checked(
                    CleanupChecked(
                        file_id=file_id,
                        occurred_at=context.occurred_at),
                    new_operation_key(), context.owned)
                _require_completed(checked, "cleanup_checked_failed")
            if outcome is WorkFileSingleOutcome.DELETED:
                cleaned += 1
            elif outcome is WorkFileSingleOutcome.FAILED:
                failed += 1
            else:
                kept += 1
            scan = scan.advanced(file_id)
            history.scan = scan
        query = scan.next_query()
    history.scan = scan
    history.finished = True
    return WorkFileCleanupResult(
        checked=cleaned + kept + failed, cleaned=cleaned,
        kept=kept, failed=failed)
