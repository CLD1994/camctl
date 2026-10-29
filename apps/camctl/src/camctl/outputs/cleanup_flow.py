"""源产物清理的目标固定输入、结果类型与会话推进。

精确清理按原请求逐项固定；范围清理等待全部来源固定并完成后按固
定来源枚举产物，来源可靠确认无产物时固定空集合并同事务按成功收
场。缺失目标按 output_not_found 直接终态，全部不可解析时动作的
失败终态由汇总事务（cleanup_items_failed）保存。

会话推进每轮依次开始到时的清理动作、固定执行中动作的目标集合、
经设备删除端口推进未终态成员，全部成员终态后保存汇总终态；删除
请求携带成员与目标文件身份供驱动定位。
"""

from __future__ import annotations

from camctl.persistence.models import db_failure

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from time import monotonic_ns as _default_monotonic_ns
from typing import Any, Awaitable, Callable, Mapping

from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.models import db_outcome_for, db_failure

from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.devices.bindings import BindingResult, DeviceBinding, binding_failure_details
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.models import FileRef
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, RetryWaitGate, RunOutcome, StaleRunFinish)
from camctl.operations.models import AttemptTicket, ErrorValue
from camctl.logging_runtime.diagnostics import DiagnosticState, record_call_outcome, record_error
from camctl.logging_runtime.events import measure_stage, record_event
from camctl.operations.recovery import RecoveryBoundary, RecoveryDiagnostic
from camctl.outputs.cleanup_round import (CleanupConsumption, record_consumption, handed_off_this_round, operation_runtime, operation_supported)
from camctl.operations.owned_calls import (
    owned_tool_call, check_call_interruption, current_call_scope)
from camctl.operations.validation import OutcomeValidationError
from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic
from camctl.outputs.cleanup_ownership import (
    CleanupSaveNotApplicable, PendingCleanupSave, save_cleanup_request,
    verify_cleanup_transaction)
from camctl.outputs.sources import ResolutionState, read_source_spec

__all__ = [
    "CancelCleanupItem",
    "CleanupActionDisposition",
    "CleanupActionFinished",
    "CleanupCancelChoice",
    "CleanupCancelFacts",
    "CleanupEntryChoice",
    "CleanupEntryFacts",
    "CleanupItemDisposition",
    "CleanupItemSaved",
    "CleanupOutcomeChoice",
    "CleanupStartDisposition",
    "CleanupStartResult",
    "CleanupTargetsDisposition",
    "CleanupTargetsSaved",
    "FailCleanupItem",
    "FinishCleanupAction",
    "FinishCleanupItem",
    "FixCleanupTargets",
    "ProgressCleanupItem",
    "RestrictCleanupItem",
    "StartCleanupAction",
    "advance_cleanup",
    "decide_cleanup_cancel",
    "decide_cleanup_entry",
    "delete_source_file",
]


@dataclass(frozen=True)
class StartCleanupAction:
    """一次清理动作开始执行的输入；时间资格由调用入口判断。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupStartDisposition(Enum):
    """清理动作开始事务的结果分区。"""

    SAVED = "saved"
    #: 动作已终态：只读恢复，不重复开始。
    ALREADY = "already"
    #: 未开始且不再普通执行（取消已生效），由取消链收场。
    REJECTED = "rejected"


@dataclass(frozen=True)
class CleanupStartResult:
    """清理动作开始事务的已保存事实。"""

    disposition: CleanupStartDisposition = CleanupStartDisposition.SAVED
    reason: str | None = None


@dataclass(frozen=True)
class FixCleanupTargets:
    """一次清理目标固定的申请输入。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupTargetsDisposition(Enum):
    """目标固定事务的结果分类。"""

    SAVED = "saved"
    #: 已固定集合或原键重送：只读复用首次结果。
    ALREADY = "already"
    #: 范围来源尚未终态或适用处理未完成：只读等待，不产生事件。
    WAITING = "waiting"


@dataclass(frozen=True)
class CleanupTargetsSaved:
    """目标固定事务的保存结果；item_ids 按创建顺序排列。"""

    disposition: CleanupTargetsDisposition
    item_ids: tuple[int, ...]
    failures: tuple[tuple[int, ErrorValue], ...] = ()


class CleanupItemDisposition(Enum):
    """清理项事务的结果分类。"""

    SAVED = "saved"
    #: 已处于该阶段或原键重送：只读复用。
    ALREADY = "already"
    #: 更早的取回或清理仍有资格，等待后重新检查。
    WAITING = "waiting"


@dataclass(frozen=True)
class CleanupItemSaved:
    """清理项事务的保存结果。"""

    disposition: CleanupItemDisposition
    item_id: int


def _item_timestamp(value: int) -> None:
    UtcMicros(value)


@dataclass(frozen=True)
class RestrictCleanupItem:
    """建立删除限制的申请输入（成员未解析 → 限制中）。"""

    item_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        _item_timestamp(self.occurred_at)


@dataclass(frozen=True)
class ProgressCleanupItem:
    """进入删除执行或保存不可撤销派发事实的申请输入。

    首次取得处理归属（限制中 → 删除中）保持可撤销限制；删除尝试
    取得可靠非未派发结果时以 irreversible=True 在同一保存事务中升
    级为不可撤销限制，可靠未派发结果不升级。
    """

    item_id: int
    occurred_at: int
    irreversible: bool = False

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if type(self.irreversible) is not bool:
            raise TypeError("不可撤销输入必须是布尔值")
        _item_timestamp(self.occurred_at)


class CleanupOutcomeChoice(Enum):
    """清理成功的结果依据选择（登记枚举成员）。"""

    DELETED = 1
    ALREADY_CLEANED = 2
    ABSENCE_CONFIRMED = 3


@dataclass(frozen=True)
class FinishCleanupItem:
    """保存清理成功终态的申请输入。"""

    item_id: int
    outcome: CleanupOutcomeChoice
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if not isinstance(self.outcome, CleanupOutcomeChoice):
            raise TypeError(f"清理结果依据必须使用 CleanupOutcomeChoice: {self.outcome!r}")
        _item_timestamp(self.occurred_at)


@dataclass(frozen=True)
class FailCleanupItem:
    """保存清理失败终态的申请输入；code 使用公共错误名称。"""

    item_id: int
    code: str
    details: dict
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if not isinstance(self.code, str) or not self.code:
            raise ValueError(f"清理失败必须使用公共错误名称: {self.code!r}")
        if not isinstance(self.details, dict):
            raise TypeError("清理失败详情必须是对象")
        _item_timestamp(self.occurred_at)


@dataclass(frozen=True)
class FinishCleanupAction:
    """清理动作汇总终态的申请输入：全部成员终态后保存动作结果。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class FinishCanceledCleanupAction:
    """取消的清理动作终态化申请：取消生效且成员全部终态后由取消
    结算保存动作取消终态；目标集合未固定的动作按零成员终态化。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupActionDisposition(Enum):
    """清理动作汇总事务的结果分类。"""

    SAVED = "saved"
    #: 原键重送或动作已终态：只读复用首次结果。
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupActionFinished:
    """清理动作汇总事务的保存结果。"""

    disposition: CleanupActionDisposition
    action_status: int
    plan_status: int
    succeeded: int
    failed: int


# ---- 取消分支与接手前置的决策表 ---------------------------------------


#: cleanup_items.status 的登记编号。
_ITEM_UNRESOLVED, _ITEM_PENDING_DELETE, _ITEM_DELETING = 1, 2, 3
_ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED = 4, 5, 6


class CleanupCancelChoice(Enum):
    """清理项在动作取消下的处理分支（按取消收场规则分类）。"""

    #: 已有最终结果：保持原结果，不重新开启。
    ALREADY = "already"
    #: 可靠确认从未发出删除：保存取消并解除可撤销限制。
    RELEASE = "release"
    #: 在途调用尚未取得收场依据：继续跟踪实际结束，不提前保存取消。
    TRACKING = "tracking"
    #: 收场后取得清理完成依据：本项保存成功。
    SUCCEED = "succeed"
    #: 删除已经或可能发生但结果未知：保存取消并保留不可撤销限制。
    UNCONFIRMED = "unconfirmed"
    #: 可靠确认删除失败且文件仍存在：保存取消并携带删除失败错误。
    DELETE_FAILED = "delete_failed"


@dataclass(frozen=True)
class CleanupCancelFacts:
    """取消分支判定的输入事实。

    delete_issued 要求本项此前任何可能删除文件的操作（已结束且收
    场依据不是可靠未派发的 DELETE 尝试），不只最后一次尝试，也不
    包括其他成员的删除；call_settled 表示已有调用取得适用收场依
    据。
    """

    status: int
    delete_issued: bool
    call_settled: bool
    file_absent: bool
    file_present: bool


def decide_cleanup_cancel(facts: CleanupCancelFacts) -> CleanupCancelChoice:
    """按取消收场规则选择清理项的取消分支。"""
    if facts.file_absent and facts.file_present:
        raise ValueError("文件缺席与在场不能同时成立")
    if facts.status in (_ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED):
        return CleanupCancelChoice.ALREADY
    if facts.status == _ITEM_DELETING:
        if not facts.call_settled:
            return CleanupCancelChoice.TRACKING
        if facts.file_absent:
            return CleanupCancelChoice.SUCCEED
        if not facts.delete_issued:
            # 删除中成员可靠从未派发自己的删除：无删除错误取消并解
            # 除本项可撤销限制，另一成员的删除事实不进入本项分类。
            return CleanupCancelChoice.RELEASE
        if facts.file_present:
            return CleanupCancelChoice.DELETE_FAILED
        return CleanupCancelChoice.UNCONFIRMED
    if facts.status in (_ITEM_UNRESOLVED, _ITEM_PENDING_DELETE):
        if facts.delete_issued:
            raise ValueError(
                f"未取得处理归属的成员不应有删除调用: status={facts.status}")
        return CleanupCancelChoice.RELEASE
    raise ValueError(f"未登记的清理成员状态: {facts.status!r}")


class CleanupEntryChoice(Enum):
    """清理项开始处理时的前置分支（按接手时的文件事实分类）。"""

    #: 已有可靠清理完成或文件缺席事实：直接保存成功，不发起调用。
    USE_COMPLETED = "use_completed"
    #: 本产物存在效果未确认的删除调用：先用自己的查询预算核实。
    VERIFY_FIRST = "verify_first"
    #: 不存在尚待核实的删除效果：直接进入删除。
    DIRECT_DELETE = "direct_delete"


@dataclass(frozen=True)
class CleanupEntryFacts:
    """接手前置判定的输入事实。

    unresolved_delete 覆盖同一产物任何成员的效果未确认删除调用；
    文件观察的有效性取决于观察之后的实际操作。
    """

    output_cleaned: bool
    unresolved_delete: bool
    file_absent: bool


def decide_cleanup_entry(facts: CleanupEntryFacts) -> CleanupEntryChoice:
    """按接手时的文件事实选择本项的首个处理步骤。"""
    if facts.output_cleaned or facts.file_absent:
        return CleanupEntryChoice.USE_COMPLETED
    if facts.unresolved_delete:
        return CleanupEntryChoice.VERIFY_FIRST
    return CleanupEntryChoice.DIRECT_DELETE


@dataclass(frozen=True)
class CancelCleanupItem:
    """保存清理取消终态的申请输入。

    code 为空表示未发出删除的解除限制取消；删除中成员的收场取消
    必须携带 delete_unconfirmed 或 file_delete_failed。
    """

    item_id: int
    occurred_at: int
    code: str | None = None
    details: dict | None = None

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        _item_timestamp(self.occurred_at)
        if self.code is None:
            if self.details is not None:
                raise TypeError("未发出删除的取消不携带错误详情")
        elif not isinstance(self.code, str) or not self.code:
            raise ValueError(f"清理取消必须使用公共错误名称: {self.code!r}")
        elif not isinstance(self.details, dict):
            raise TypeError("清理取消详情必须是对象")


# ---- 删除编排：限制 → 删除意图 → 设备调用 → 结果/核实 ----


class DeleteDriverPort:
    """设备删除与独立文件查询端口；契约替身与真实驱动同形。"""

    async def delete(self, request) -> object: ...

    async def query_file(self, request) -> object: ...


@dataclass(frozen=True)
class LocalArtifactRequest:
    """主机正式文件请求；保留原票据及保存文件的完整身份。"""

    database_id: str
    operation: str
    cleanup_item_id: int
    ticket: AttemptTicket
    file_ref: FileRef

    @property
    def path(self) -> str:
        """兼容只观察请求路径的端口替身；生产路径来自 FileRef。"""
        return self.file_ref.relative_path


class LocalArtifactTaskFailure(DiagnosticError, RuntimeError):
    """任务实际结果已取回，但执行器监督交接自身发生错误。"""

    def __init__(
            self, actual: DeviceCallResult,
            supervision_diagnostic: CleanupSupervisionDiagnostic | None,
            message: str | None = None):
        rendered = (message if supervision_diagnostic is None else
                    supervision_diagnostic.render())
        super().__init__(rendered or "主机文件等待取消，原 completion 已取回",
                         diagnostic_details={"supervision": None if supervision_diagnostic is None
                             else exception_diagnostic(supervision_diagnostic.exception),
                             "supervision_facts": [] if supervision_diagnostic is None else
                                 [asdict(fact) for fact in supervision_diagnostic.facts]})
        self.actual = actual
        self.supervision_diagnostic = supervision_diagnostic


def _raise_supervision_with_error(
        pending, prior: BaseException, context: str) -> None:
    """保存仍不可靠时同时交付原数据库问题和原监督错误。"""
    diagnostic = pending.supervision_diagnostic
    if diagnostic is None or pending.supervision_diagnostic_delivered:
        return
    supervision_error = diagnostic.exception or RuntimeError(diagnostic.render())
    if not isinstance(prior, Exception):
        note = "清理监督次级错误: " + json.dumps({
            "context": context, "error": exception_diagnostic(supervision_error),
            "facts": [asdict(fact) for fact in diagnostic.facts],
        }, ensure_ascii=False, separators=(",", ":"))
        if note not in tuple(getattr(prior, "__notes__", ())):
            prior.add_note(note)
        raise prior
    combined = BaseExceptionGroup(
        "清理结果保存与文件任务监督均有故障",
        [prior, supervision_error])
    raise ConsistencyError(
        f"{context}；{prior}; {diagnostic.render()}") from combined


def _deliver_supervision(pending, operation: str) -> None:
    """原 actual 已可靠保存后，先固定一次交付，再阻止进入 consumer。"""
    diagnostic = pending.supervision_diagnostic
    if diagnostic is None or pending.supervision_diagnostic_delivered:
        return
    pending.supervision_diagnostic_delivered = True
    error = ConsistencyError(
        f"{operation}实际结果已可靠保存，文件任务监督交接失败: "
        f"{diagnostic.render()}")
    error.__cause__ = diagnostic.exception or RuntimeError(diagnostic.render())
    record_error(error, stage=f"cleanup_{pending.ticket.operation}_supervision",
                 state=pending.diagnostic_state,
                 context={"database_id": pending.database_id, "cleanup_item_id": pending.item_id})
    raise error


class LocalArtifactTargetError(ConsistencyError):
    """原票据可核对，但已保存的主机文件身份或纯路径校验不成立。"""


class HostArtifactPort:
    """主机派生成品的本地删除与文件查询端口；观察与设备调用同形。"""

    async def delete(self, request: LocalArtifactRequest) -> object: ...

    async def query_file(self, request: LocalArtifactRequest) -> object: ...


@dataclass(frozen=True)
class CleanupStep:
    """一次删除推进的结果分区。"""

    phase: str
    detail: str | None = None
    diagnostic: str | None = None
    exception: BaseException | None = field(default=None, compare=False, repr=False)


class CleanupQueryConsumer(Enum):
    """实际查询的原消费用途；保存重送不重新选择用途。"""

    BEFORE_DELETE = "before_delete"
    AFTER_DELETE = "after_delete"
    CANCEL = "cancel"


class CleanupQueryPhase(Enum):
    """原查询从实际返回到可靠保存的责任阶段。"""

    RETURNED = "returned"
    PREPARED = "prepared"
    SAVED = "saved"


@dataclass
class PendingCleanupMember:
    """成员结果的唯一申请；提交未知时继续核实原 key。"""

    request: FinishCleanupItem | CancelCleanupItem | FailCleanupItem
    key: OperationKey
    saved: bool = False
    diagnostic_state: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


@dataclass(frozen=True)
class PendingCleanupCompanion:
    """已终态成员仍拥有的伴随流程结束申请。"""

    database_id: str
    request: StaleRunFinish
    key: OperationKey


@dataclass(frozen=True)
class CleanupValidationDiagnostic:
    """原返回的固定诊断数据；不持有异常及其旧调用栈。"""

    error_type: str
    message: str


@dataclass(frozen=True)
class CleanupSupervisionFault:
    """一次文件任务监督故障的无 traceback 记录。"""

    source: str
    identity: str | None
    stage: str | None
    business: str | None
    resources: tuple[str, ...]
    error_type: str
    error_message: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupSupervisionDiagnostic:
    """本次文件结果交接的故障事实和原异常图，不持有任务。"""

    facts: tuple[CleanupSupervisionFault, ...]
    exception: BaseException | None = field(default=None, compare=False, repr=False)

    def render(self) -> str:
        rendered = []
        for fact in self.facts:
            record = ", ".join(value for value in (
                f"identity={fact.identity}" if fact.identity is not None else None,
                f"stage={fact.stage}" if fact.stage is not None else None,
                f"business={fact.business}" if fact.business is not None else None,
                f"resources={fact.resources}" if fact.resources else None,
            ) if value is not None)
            notes = "" if not fact.notes else f" notes={fact.notes}"
            rendered.append(
                f"{fact.source} [{record}] {fact.error_type}: "
                f"{fact.error_message}{notes}")
        return "; ".join(rendered)


@dataclass
class PendingCleanupQuery:
    """会话拥有的原返回与保存申请，不持有连接或设备回调。

    SAVED 仍须交给成员结果和伴随流程消费者；查询事务本身不能
    释放这些后续责任。验证失败保留原返回和原诊断。
    """

    database_id: str
    item_id: int
    consumer: CleanupQueryConsumer
    ticket: AttemptTicket
    actual: DeviceCallResult
    evidence: EvidenceRegistry | None
    occurred_at: int | None
    returned_ns: int | None
    key: OperationKey
    phase: CleanupQueryPhase = CleanupQueryPhase.RETURNED
    finish: AttemptFinish | None = None
    validation_diagnostic: CleanupValidationDiagnostic | None = None
    member: PendingCleanupMember | None = None
    saved_actual: DeviceCallResult | None = None
    saved_key: OperationKey | None = None
    saved_occurred_at: int | None = None
    diagnostic: str | None = None
    binding_failure: BindingResult | None = None
    recovery_max_event_id: int | None = None
    query_unsupported: bool = False
    supervision_diagnostic: CleanupSupervisionDiagnostic | None = None
    supervision_diagnostic_delivered: bool = False
    diagnostic_state: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    diagnostic_exception: BaseException | None = field(default=None, compare=False, repr=False)
    validation_state: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


@dataclass
class PendingCleanupIntent:
    """原意图与本会话派发资格；持久化意图不等于可重放票据。"""

    database_id: str
    item_id: int
    request: Any
    key: OperationKey
    consumer: CleanupQueryConsumer | None
    ticket: AttemptTicket | None = None
    dispatched: bool = False
    last_kind: Any = None


@dataclass
class PendingCleanupDelete:
    """DELETE 实际返回先进入会话，再验证和保存。"""

    database_id: str
    item_id: int
    ticket: AttemptTicket
    actual: DeviceCallResult
    evidence: EvidenceRegistry
    occurred_at: int | None
    returned_ns: int | None
    key: OperationKey
    finish: AttemptFinish | None = None
    validation_diagnostic: CleanupValidationDiagnostic | None = None
    saved: bool = False
    saved_actual: DeviceCallResult | None = None
    saved_key: OperationKey | None = None
    saved_occurred_at: int | None = None
    diagnostic: str | None = None
    binding_failure: BindingResult | None = None
    recovery_max_event_id: int | None = None
    delete_unsupported: bool = False
    supervision_diagnostic: CleanupSupervisionDiagnostic | None = None
    supervision_diagnostic_delivered: bool = False
    diagnostic_state: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    diagnostic_exception: BaseException | None = field(default=None, compare=False, repr=False)
    validation_state: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


@dataclass
class CleanupRuntime:
    """删除编排的端口集合；配置与设备绑定由装配层提供。

    monotonic_ns 与 retry_gate 支撑设备删除和查询的重试间隔时间门槛：
    保存重试等待后按各自配置的间隔到时才允许下一次尝试；装配层会话
    共享同一门槛。主机文件尝试仅使用次数预算，不设置应用层时限或
    定时重试间隔。
    """

    database: BusinessDatabase
    outputs: Any
    operations: Any
    driver: DeleteDriverPort
    evidence: Any
    binding_of: Callable[[int], Awaitable[DeviceBinding | None]]
    occurred_at: Callable[[], int]
    delete_config: Any
    query_config: Any
    #: 主机派生成品的本地删除协作者；未装配时此类成员保持等待。
    local_files: HostArtifactPort | None = None
    #: 与 HostArtifacts 绑定的 staging 身份；仅主机文件任务使用。
    local_file_root: Path | None = None
    monotonic_ns: Callable[[], int] = _default_monotonic_ns
    retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)
    binding_check: Callable[[DeviceBinding], BindingResult] | None = None
    delete_supported: bool | None = None
    query_supported: bool | None = None
    for_operation: Callable[[int, str], Awaitable[CleanupRuntime]] | None = None
    round_token: object | None = None
    for_item: Callable[[int], Awaitable[CleanupRuntime]] | None = None
    pending_intents: dict[int, PendingCleanupIntent] = field(default_factory=dict)
    pending_deletes: dict[int, PendingCleanupDelete] = field(default_factory=dict)
    pending_saves: dict[tuple[str, int], PendingCleanupSave] = field(default_factory=dict)
    pending_queries: dict[tuple[int, int], PendingCleanupQuery] = field(default_factory=dict)
    pending_companions: dict[int, PendingCleanupCompanion] = field(default_factory=dict)
    #: 每个活动成员只保留最近一次普通交接身份，避免逐轮重置等待。
    consumed_queries: dict[int, CleanupConsumption] = field(default_factory=dict)
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    recovery_max_event_id: int | None = None
    recovery_evidence_for: Callable[[Any, str], Any] | None = None
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None
    last_recovery_diagnostic: RecoveryDiagnostic | None = None
    discovery_batch_size: int = 100
    allow_new_calls: bool = True
    #: 仅由实际 await 的 try/finally 持有；不存 Task、回调或连接。
    active_calls: set[tuple[str, int, int]] = field(default_factory=set)


async def _read_cleanup(runtime, callback):
    """完整查询在所属数据库执行器运行，只交付物化事实。"""
    return await runtime.database.read(lambda owned: callback(owned.connection), description="清理事实读取")


def _presence_observation(result):
    """合法存在性观察的布尔事实；没有该观察时为 None。"""
    presence = None
    found = False
    for observation in result.observations:
        if not isinstance(observation, DeviceObservation):
            raise OutcomeValidationError("存在性查询的观察不是合法 DeviceObservation")
        if observation.type == "file_presence":
            if found:
                raise OutcomeValidationError("存在性查询包含多份在场观察")
            if observation.version != 1:
                raise OutcomeValidationError("存在性观察版本不支持")
            presence = observation.data.get("present")
            if type(presence) is not bool:
                raise OutcomeValidationError("存在性观察 present 必须是布尔值")
            found = True
    return presence


def _cancel_requested(connection, action_id: int) -> bool:
    row = connection.execute(
        "SELECT cancel_requested FROM actions WHERE id = ?",
        (action_id,)).fetchone()
    return row is not None and bool(row[0])


def _output_delete_facts(connection, output_id: int) -> dict:
    """本产物删除调用的可靠事实：缺席、未决删除与晚于删除的在场确认。

    尝试编号按保存顺序单调递增，观察的有效性取决于其后的实际操
    作：晚于全部未决删除的可靠查询使其失去直接删除资格。
    """
    file_row = connection.execute(
        "SELECT device_file_id, intermediate_file_id FROM outputs"
        " WHERE id = ?", (output_id,)).fetchone()
    file_absent = False
    if file_row is not None:
        device_id, intermediate_id = file_row
        if device_id is not None:
            presence = connection.execute(
                "SELECT presence_state FROM device_files WHERE id = ?",
                (device_id,)).fetchone()
            file_absent = presence is not None and presence[0] == 3
        elif intermediate_id is not None:
            cleanup = connection.execute(
                "SELECT cleanup_state FROM intermediate_files WHERE id = ?",
                (intermediate_id,)).fetchone()
            file_absent = cleanup is not None and cleanup[0] == 4
    unresolved = _latest_possible_delete(connection, output_id, unresolved_only=True)
    unresolved_delete = 0 if unresolved is None else unresolved[0]
    confirmed_check = 0
    delete = _latest_possible_delete(connection, output_id)
    if delete is not None and delete[1] != 1:
        from camctl.contracts.json_values import parse_exact_json
        from camctl.operations.result_format import read_result_document
        try:
            actual = read_result_document(delete[1], delete[2], parse_exact_json(delete[3]),
                None if delete[4] is None else parse_exact_json(delete[4]))
            present = _delete_presence(actual, delete[5])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("产物的原删除结果不可解释") from error
        if present is True:
            confirmed_check = delete[0]
        elif present is False:
            file_absent = True
    from camctl.outputs.cleanup_group import cleanup_group
    from camctl.persistence.repositories.outputs import _ProductReads
    group = cleanup_group(_ProductReads(connection, {}), output_id)
    query = connection.execute(
        "SELECT a.id,a.status,a.effect_state,a.result_json,a.error_json,c.id"
        " FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id"
        " JOIN cleanup_items c ON r.cleanup_item_id=c.id"
        " WHERE c.output_id IN (%s) AND r.kind=5 ORDER BY a.id DESC LIMIT 1"
        % ",".join("?" * len(group)), group).fetchone()
    if query is not None and query[1] != 1:
        from camctl.contracts.json_values import parse_exact_json
        from camctl.operations.result_format import read_result_document
        from camctl.outputs.cleanup_results import saved_query_presence

        try:
            actual = read_result_document(
                query[1], query[2], parse_exact_json(query[3]),
                None if query[4] is None else parse_exact_json(query[4]))
            present = saved_query_presence(actual, query[5])
        except (KeyError, TypeError, ValueError) as error:
            raise ConsistencyError("产物的原查询结果不可解释") from error
        if present is True:
            confirmed_check = max(confirmed_check, query[0])
        elif present is False and query[0] > unresolved_delete:
            file_absent = True
    return {
        "file_absent": file_absent,
        "unresolved_delete": unresolved_delete > confirmed_check,
        "confirmed_present_after": confirmed_check > 0 and confirmed_check >= unresolved_delete,
    }



def _latest_possible_delete(connection, output_id, *, unresolved_only=False):
    """普通推进、原结果选择和重试阶段共用完整的可能删除事实。"""
    value = _group_latest_possible_delete(connection, output_id, unresolved_only=unresolved_only)
    return None if value is None else value[:6]


def _group_latest_possible_delete(connection, output_id, *, unresolved_only=False):
    from camctl.outputs.cleanup_results import latest_possible_cleanup_delete
    from camctl.outputs.cleanup_group import cleanup_group
    from camctl.persistence.repositories.outputs import _ProductReads
    group = cleanup_group(_ProductReads(connection, {}), output_id)
    values = [latest_possible_cleanup_delete(connection, member, unresolved_only=unresolved_only)
              for member in group]
    return max((value for value in values if value is not None), default=None,
               key=lambda value: value[0])

def _attempts_used(connection, responsibility_key: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a"
        " JOIN operation_runs r ON a.run_id = r.id"
        " WHERE r.responsibility_key = ?", (responsibility_key,)).fetchone()
    return int(row[0]) if row is not None else 0


async def _retry_wait_step(runtime: CleanupRuntime, responsibility: str,
                     config: AttemptConfig, phase: str) -> CleanupStep | None:
    """开始新尝试前的间隔门槛；允许开始时返回 None。

    以责任链保存的重试等待标志与累计次数为权威：首次尝试不预先
    等待，预算耗尽即时交还意图事务按耗尽收场；处于重试等待时按
    本次配置的间隔到时才放行。
    """
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT attempts_used, retry_wait_required FROM operation_runs WHERE responsibility_key = ?', (responsibility,)).fetchone())
    if row is None:
        return None
    remaining = runtime.retry_gate.remaining(
        responsibility,
        attempts_used=int(row[0]),
        retry_wait_required=int(row[1]) == 1,
        max_attempts_used=config.max_attempts,
        interval_s=config.retry_interval_s,
        now_ns=runtime.monotonic_ns())
    if remaining is None:
        return None
    return CleanupStep(phase, f"{remaining}s")


async def _query_retry_wait_step(
        runtime: CleanupRuntime, item_id: int) -> CleanupStep | None:
    """新删除后的首次核实即时开始，失败查询的重试才等待间隔。

    同一产物可以由不同清理成员删除。以该产物最新删除意图与本成员
    最新查询意图的保存顺序判定阶段，不能把上一轮的查询等待沿用到
    新删除后的首次核实；查询预算仍归原成员的查询流程。
    """
    responsibility = f"exists/{item_id}"
    output_id = (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()))[0]
    latest = await _read_cleanup(runtime, lambda connection: _group_latest_possible_delete(connection, output_id))
    delete_intent = None if latest is None else latest[6]
    query_intent = (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT MAX(a.intent_event_id) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?', (responsibility,)).fetchone()))[0]
    if (query_intent is None
            or (delete_intent is not None and delete_intent > query_intent)):
        runtime.retry_gate.cleared(responsibility)
        return None
    return await _retry_wait_step(runtime, responsibility, runtime.query_config, 'query_retry_wait')


def _exhaustion_details(
        connection, item_id: int, output_id, operation: str, config) -> dict:
    return {"output_id": str(output_id), "max_attempts": config.max_attempts,
            "attempts_used": _attempts_used(connection, f"{operation}/{item_id}")}


async def _companion_member_result(runtime: CleanupRuntime, item_id: int):
    """已有伴随责任先核对原成员终态，不能靠普通推进补造前提。"""
    # 回执 ALREADY 时，以真实可靠成员终态收场；原拟写入值不覆盖它。
    member = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status,error_code,error_details_json FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
    if member is None or member[0] not in _MEMBER_TERMINAL:
        raise ConsistencyError("待存伴随流程没有可靠成员终态")
    if await _read_cleanup(runtime, lambda connection: connection.execute('SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind IN (4,5) AND (a.status=1 OR a.result_event_id IS NULL) LIMIT 1', (item_id,)).fetchone()) is not None:
        raise ConsistencyError("终态清理成员仍有未收场物理尝试")
    status, error = _terminal_companion(member[0], member[1])
    if error is not None and member[2] is not None:
        from camctl.contracts.json_values import parse_exact_json
        from dataclasses import replace
        error = replace(error, details=parse_exact_json(member[2]))
    pending = runtime.pending_companions.get(item_id)
    if pending is not None and (
            pending.database_id != runtime.database.metadata.instance_id
            or pending.request.status is not status or pending.request.error != error):
        raise ConsistencyError("原伴随流程收场与本次数据库或成员结果不对应")
    return status, error


async def _settle_companion_runs(
        runtime: CleanupRuntime, item_id: int) -> str | None:
    """成员终态后统一收场删除与存在性查询两条伴随流程。

    两条流程的后续尝试都由本成员的推进驱动，成员终态即不再有后
    续尝试；仍开放的流程行按成员的最终结果结束并清除重试等待，
    否则会话的流程收尾计数无法归零。已结束的行不受影响。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import StaleRunFinish
    from camctl.persistence.models import DbOutcomeKind

    status, error = await _companion_member_result(runtime, item_id)
    pending = runtime.pending_companions.get(item_id)
    if pending is None:
        pending = PendingCleanupCompanion(
            runtime.database.metadata.instance_id,
            StaleRunFinish(
                responsibility_keys=(f"delete/{item_id}", f"exists/{item_id}"),
                status=status, error=error, occurred_at=runtime.occurred_at()),
            new_operation_key())
        runtime.pending_companions[item_id] = pending
    def apply_completed(settled):
        from camctl.operations.attempts import StaleRunFinishResult
        if not isinstance(settled.value, StaleRunFinishResult):
            raise ConsistencyError("原伴随流程结束没有可靠回执")
        runtime.retry_gate.cleared(f"delete/{item_id}")
        runtime.retry_gate.cleared(f"exists/{item_id}")
        runtime.consumed_queries.pop(item_id, None)
        del runtime.pending_companions[item_id]

    try:
        settled = await runtime.database.write(lambda owned: runtime.operations.finish_stale_runs(pending.request, pending.key, owned), key=pending.key)
    except BaseException as error:
        actual = db_outcome_for(error)
        if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
            try:
                apply_completed(actual)
            except BaseException as application_error:
                raise error from application_error
        raise
    from camctl.persistence.models import DbOutcome
    if not isinstance(settled, DbOutcome):
        raise ConsistencyError("伴随流程保存没有合法数据库回执")
    if settled.kind is not DbOutcomeKind.COMPLETED:
        return str(settled.error)
    apply_completed(settled)
    return None


def _terminal_companion(status: int, error_code) -> tuple:
    """终态成员的伴随流程收场映射：成功、失败、取消或结果未知。

    成员失败与删除中的收场取消都保存了公共错误编号；按编号还原
    流程错误。成员行是收场结果的权威来源，此处不重复推导删除或
    查询的具体次数事实。
    """
    from camctl.contracts.workflow_errors import registered_error_spec
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue

    if status == 4:
        return RunOutcome.SUCCEEDED, None
    if status == 6 and error_code is None:
        return RunOutcome.CANCELED, None
    if error_code is None:
        raise ConsistencyError(
            f"终态清理成员缺少错误编号: status={status}")
    name, _ = registered_error_spec(
        "item_error_ids.cleanup_items", int(error_code))
    if status == 5:
        stage = ("query" if name == "file_query_attempts_exhausted" else
                 "execution" if name == "device_binding_unavailable" else "delete")
        return RunOutcome.FAILED, ErrorValue(code=name, stage=stage)
    if name == "file_delete_failed":
        return RunOutcome.FAILED, ErrorValue(code=name, stage="delete")
    return RunOutcome.UNCONFIRMED, ErrorValue(code=name, stage="delete")


async def delete_source_file(runtime: CleanupRuntime, item_id: int) -> CleanupStep:
    """推进一个清理成员的删除：意图先行，结果或核实后终态。

    删除与存在性查询使用各自流程的独立预算；效果未知只能用查询额
    核实，查询确认仍在时等待预算内重试，查询预算耗尽按公共错误终
    态失败。接手前先按本产物已有删除事实决定复用完成、先核实或直
    接删除。动作取消生效后停止新增普通调用：未发出删除的成员解除
    限制，删除中的成员跟踪已有调用并按实际结论收场。
    """
    from camctl.persistence.models import DbOutcomeKind

    member_state = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
    if member_state is not None and member_state[0] in _MEMBER_TERMINAL:
        unsettled = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind IN (4,5) AND (a.status=1 OR a.result_event_id IS NULL) LIMIT 1', (item_id,)).fetchone())
        if unsettled is not None:
            raise ConsistencyError("终态清理成员仍有未收场物理尝试")
        from camctl.outputs.cleanup_absence import handoff_terminal_absence
        rejected = await handoff_terminal_absence(runtime, item_id)
        if rejected is not None:
            return rejected
    if item_id in runtime.pending_companions:
        await _companion_member_result(runtime, item_id)
    for (method, target_id), saved_request in tuple(runtime.pending_saves.items()):
        if target_id != item_id or not hasattr(saved_request.request, "item_id"):
            continue
        saved = await save_cleanup_request(runtime, method, saved_request.request)
        if saved.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(saved.error))
    original = runtime.pending_intents.get(item_id)
    if item_id in runtime.pending_deletes or (original is not None and original.request.operation == "delete"):
        row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id,output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        if row is None:
            raise ConsistencyError("原删除申请缺少成员")
        return await _delete_once(runtime, item_id, *row)
    if original is not None:
        result = await settle_required(_begin_cleanup_intent(runtime, original.request, original.consumer))
        if isinstance(result, CleanupStep):
            return result
        if result.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("query_rejected", str(result.error))
        if result.value.ticket is None:
            if result.value.reason == "cleanup_waiting":
                return CleanupStep("waiting")
            if result.value.reason == "binding_unavailable":
                return await _finish_binding_failure(runtime, item_id, canceled=await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, original.request.action_id)))
            output_id = (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()))[0]
            if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, original.request.action_id)) or result.value.reason == "canceled":
                return await _cancel_member(runtime, item_id, 3, output_id)
            if result.value.reason in ("delete_unsupported", "query_unsupported"):
                return await _fail_unsupported(runtime, item_id, output_id)
            if result.value.reason == "budget_exhausted":
                return await _fail_exhausted(runtime, item_id, output_id, 'exists', runtime.query_config, 'file_query_attempts_exhausted')
            return CleanupStep("query_budget_rejected", result.value.reason)
        if result.value.ticket is not None:
            if not runtime.allow_new_calls and _pending_query_for_item(runtime,item_id) is None:
                return CleanupStep("local_handoff")
            await _run_query(runtime, result.value.ticket, item_id, original.consumer)
            return (await settle_required(_consume_saved_query(runtime, _pending_query_for_item(runtime, item_id)))
                    or CleanupStep("still_present"))

    from camctl.outputs.cleanup_recovery import recover_cleanup_call
    await recover_cleanup_call(runtime, item_id)
    if item_id in runtime.pending_deletes:
        return await _delete_once(runtime, item_id, *await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id,output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()))

    # 新进程没有原内存返回，先恢复精确 DELETE 的完整持久结果。
    from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_query
    from camctl.operations.models import SettlementBasis
    saved_delete = await _read_cleanup(runtime, lambda connection: saved_cleanup_delete(connection, item_id))
    consumed = runtime.consumed_queries.get(item_id)
    restore_delete = False
    if saved_delete is not None:
        if _delete_presence(saved_delete.actual, item_id) is False:
            restore_delete = True
        elif (saved_delete.actual.outcome.settlement.basis is not SettlementBasis.NOT_DISPATCHED
                and _pending_query_for_item(runtime, item_id) is None
                and await _read_cleanup(runtime, lambda connection: saved_cleanup_query(connection, item_id)) is None):
            identity = (runtime.database.metadata.instance_id, item_id,
                        saved_delete.ticket, saved_delete.key)
            restore_delete = consumed is None or consumed.delete_identity != identity
    if restore_delete:
        pending_delete = PendingCleanupDelete(runtime.database.metadata.instance_id,
            item_id, saved_delete.ticket, saved_delete.actual, None,
            saved_delete.occurred_at, runtime.monotonic_ns(), saved_delete.key, saved=True)
        runtime.pending_deletes[item_id] = pending_delete
        return await _consume_delete_return(runtime, pending_delete)

    pending = _pending_query_for_item(runtime, item_id)
    restored = pending is None
    if pending is None:
        pending = await _restore_saved_query(runtime, item_id)
    if pending is not None:
        await _run_query(runtime, pending.ticket, item_id, pending.consumer)
        step = await settle_required(_consume_saved_query(runtime, pending))
        if handed_off_this_round(runtime, item_id):
            return step or CleanupStep("local_handoff")
        if step is not None and (not restored or step.phase not in ("query_unknown", "still_present")):
            return step

    occurred = runtime.occurred_at()
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id, status, output_id, error_code FROM cleanup_items WHERE id = ?', (item_id,)).fetchone())
    if row is None:
        return CleanupStep("missing_item")
    action_id, status, output_id, error_code = row
    if status in (4, 5, 6):
        runtime.consumed_queries.pop(item_id, None)
        # 成员已终态但伴随流程仍未收场（此前事务之间中断）：按已
        # 保存终态补齐收场，否则会话的流程收尾计数无法归零。
        outcome, error = _terminal_companion(int(status), error_code)
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("already_terminal")
    if not runtime.allow_new_calls:
        return CleanupStep("local_handoff")
    # 取消已生效时不再建立新的普通处理责任：未发出删除直接取消，
    # 删除中的成员按已有调用的实际结论收场。
    if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
        return await _cancel_member(runtime, item_id, status, output_id)
    restrict = await save_cleanup_request(runtime, 'restrict_cleanup_item', RestrictCleanupItem(item_id, occurred))
    if restrict.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("restrict_rejected", str(restrict.error))
    if restrict.value.disposition is CleanupItemDisposition.WAITING:
        return CleanupStep("waiting")
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id, status, output_id FROM cleanup_items WHERE id = ?', (item_id,)).fetchone())
    action_id, status, output_id = row

    entry = decide_cleanup_entry(await _read_cleanup(runtime, lambda connection: _entry_facts(connection, output_id)))
    if entry is CleanupEntryChoice.USE_COMPLETED:
        from camctl.outputs.cleanup_group import cleanup_group
        from camctl.persistence.repositories.outputs import _ProductReads
        group, own_file = await _read_cleanup(runtime, lambda connection: (
            cleanup_group(_ProductReads(connection, {}), output_id), connection.execute(
                "SELECT f.presence_state FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.id=?",
                (output_id,)).fetchone()))
        if len(group) > 1 and own_file is not None and own_file[0] != 3:
            return CleanupStep("waiting")
        cleaned = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT availability, cleanup_status FROM outputs WHERE id = ?', (output_id,)).fetchone())
        choice = (CleanupOutcomeChoice.ALREADY_CLEANED
                  if cleaned is not None and cleaned[0] == 3
                  else CleanupOutcomeChoice.ABSENCE_CONFIRMED)
        done = await save_cleanup_request(runtime, 'finish_cleanup_item', FinishCleanupItem(item_id, choice, runtime.occurred_at()))
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return (CleanupStep("already_terminal") if done.value.disposition is CleanupItemDisposition.ALREADY
                else CleanupStep("succeeded", choice.name))
    from camctl.persistence.repositories.outputs import cleanup_group_call_owner
    group_owner = await _read_cleanup(runtime, lambda connection: cleanup_group_call_owner(connection, item_id))
    if group_owner is not None and group_owner["id"] != item_id:
        if group_owner["status"] != _ITEM_FAILED:
            return CleanupStep("waiting")
        from camctl.contracts.workflow_errors import registered_error_spec
        code, _ = registered_error_spec("item_error_ids.cleanup_items", group_owner["error_code"])
        details = dict(group_owner["error_details_json"])
        if "output_id" in details:
            details["output_id"] = str(output_id)
        saved = await save_cleanup_request(runtime, 'fail_cleanup_item',
            FailCleanupItem(item_id, code, details, runtime.occurred_at()))
        if saved.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("fail_rejected", str(saved.error))
        if saved.value.disposition is CleanupItemDisposition.WAITING:
            return CleanupStep("waiting")
        return CleanupStep("failed", code)
    # 设备调用目标：主机派生成品经本地文件链路删除，未装配设备或
    # 本地协作者时成员保持等待，不解释为失败。
    failed_binding = await _finish_binding_failure(runtime, item_id)
    if failed_binding is not None:
        return failed_binding
    needed_operation = "query" if entry is CleanupEntryChoice.VERIFY_FIRST else "delete"
    runtime = await operation_runtime(runtime, item_id, needed_operation)
    if not await operation_supported(runtime, item_id, needed_operation):
        return await _fail_unsupported(runtime, item_id, output_id)
    if await runtime.binding_of(item_id) is None:
        if runtime.local_files is None:
            return CleanupStep("target_unbound")
        # 主机文件的数据库身份、用途、路径和绑定根在首次授予票据前
        # 完成纯校验；stat 与 unlink 留给共享文件任务线程。
        await _local_file_ref(runtime, item_id)
    if entry is CleanupEntryChoice.VERIFY_FIRST:
        verified = await _verify_before_delete(
            runtime, item_id, action_id, output_id)
        if verified is not None:
            return verified
    return await _delete_once(runtime, item_id, action_id, output_id)


async def _fail_unsupported(runtime, item_id, output_id):
    from camctl.persistence.models import DbOutcomeKind
    saved = await save_cleanup_request(runtime, 'fail_cleanup_item', FailCleanupItem(item_id, 'file_delete_failed', {'output_id': str(output_id)}, runtime.occurred_at()))
    if saved.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("fail_rejected", str(saved.error))
    rejected = await settle_required(_settle_companion_runs(runtime, item_id))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    return (CleanupStep("already_terminal") if saved.value.disposition is CleanupItemDisposition.ALREADY
            else CleanupStep("failed", "file_delete_failed"))


async def _current_binding_failure(runtime, item_id):
    binding = await runtime.binding_of(item_id)
    if binding is None or runtime.binding_check is None:
        return None
    result = runtime.binding_check(binding)
    return result if binding_failure_details(result) is not None else None


async def _finish_binding_failure(runtime, item_id, *, canceled=False, binding_result=None):
    result = binding_result or await _current_binding_failure(runtime, item_id)
    if result is None:
        return None
    from camctl.persistence.repositories.outputs import FailCleanupBinding
    from camctl.persistence.models import DbOutcomeKind

    source_id = (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT o.device_file_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id WHERE c.id=?', (item_id,)).fetchone()))[0]
    keys = tuple((row[0] for row in await _read_cleanup(runtime, lambda connection: connection.execute('SELECT responsibility_key FROM operation_runs WHERE cleanup_item_id=? AND status IN (1,2) ORDER BY id', (item_id,)).fetchall())))
    if canceled and not keys:
        return await _cancel_with_error(runtime, item_id, (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()))[0], 'delete_unconfirmed')
    saved = await save_cleanup_request(runtime, 'fail_cleanup_binding', FailCleanupBinding(item_id, source_id, result, keys, runtime.occurred_at(), canceled))
    if isinstance(saved.value, CleanupSaveNotApplicable):
        return CleanupStep("cancel_pending")
    if saved.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"清理绑定失败事务未完成（{saved.kind.value}）: {saved.error}", saved)
    runtime.retry_gate.cleared(f"delete/{item_id}")
    runtime.retry_gate.cleared(f"exists/{item_id}")
    return CleanupStep("canceled" if canceled else "failed", "device_binding_unavailable")


def _entry_facts(connection, output_id: int) -> CleanupEntryFacts:
    output = connection.execute(
        "SELECT availability, cleanup_status FROM outputs WHERE id = ?",
        (output_id,)).fetchone()
    facts = _output_delete_facts(connection, output_id)
    return CleanupEntryFacts(
        output_cleaned=(
            output is not None and output[0] == 3 and output[1] == 4),
        unresolved_delete=facts["unresolved_delete"],
        file_absent=facts["file_absent"])


async def _verify_before_delete(
        runtime: CleanupRuntime, item_id: int, action_id: int,
        output_id: int) -> CleanupStep | None:
    """先用自己的查询预算核实本产物未决的删除效果。

    确认缺席直接成功；确认仍在时返回 None 落入删除路径；查询额度
    耗尽按公共错误终态失败，不以删除代替核实。
    """
    from camctl.persistence.models import DbOutcomeKind

    waiting = await _query_retry_wait_step(runtime, item_id)
    if waiting is not None:
        return waiting
    ticket = await _begin_query_attempt(runtime, item_id, action_id)
    if isinstance(ticket, CleanupStep):
        return ticket
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(ticket.error))
    if ticket.value.ticket is None:
        if ticket.value.reason == "cleanup_waiting":
            return CleanupStep("waiting")
        if ticket.value.reason == "query_unsupported":
            return await _fail_unsupported(runtime, item_id, output_id)
        if ticket.value.reason == "budget_exhausted":
            return await _fail_exhausted(runtime, item_id, output_id, 'exists', runtime.query_config, 'file_query_attempts_exhausted')
        return CleanupStep("query_budget_rejected", ticket.value.reason)
    present = await _run_query(
        runtime, ticket.value.ticket, item_id, CleanupQueryConsumer.BEFORE_DELETE)
    return await settle_required(_consume_saved_query(runtime, runtime.pending_queries[ticket.value.ticket.run_id, ticket.value.ticket.attempt_id]))


async def _begin_cleanup_intent(runtime, request, consumer):
    """在第一次写入前持有原申请，可靠票据只属于本会话拥有者。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import BeginAttemptResult, BeginDisposition
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    item_id = request.target.cleanup_item_id
    database_id = runtime.database.metadata.instance_id
    pending = runtime.pending_intents.get(item_id)
    if pending is None:
        pending = PendingCleanupIntent(database_id, item_id, request, new_operation_key(), consumer)
        runtime.pending_intents[item_id] = pending
    elif (pending.database_id != database_id
          or pending.request.operation != request.operation or pending.consumer is not consumer):
        raise ConsistencyError("原清理意图与数据库、操作或消费用途不对应")
    if pending.ticket is not None:
        return DbOutcome(DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, pending.ticket))
    saved = await runtime.database.read(lambda owned: verify_cleanup_transaction(owned, pending.key))
    if saved is None:
        if not runtime.allow_new_calls:
            return CleanupStep("local_handoff")
        member = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        canceled = await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, pending.request.action_id))
        canceled_without_delete = canceled and request.operation == "delete"
        if canceled and request.operation == "query":
            from camctl.outputs.cleanup_results import cleanup_member_delete_state, cleanup_cancel_query_available
            own_delete, _unsettled = await _read_cleanup(runtime, lambda connection: cleanup_member_delete_state(connection, item_id))
            canceled_without_delete = not own_delete or not await _read_cleanup(runtime, lambda connection: cleanup_cancel_query_available(connection, item_id))
        reason = None
        if member is None or member[0] in _MEMBER_TERMINAL or canceled_without_delete:
            reason = "canceled"
        elif await _current_binding_failure(runtime, item_id) is not None:
            reason = "binding_unavailable"
        else:
            current = await operation_runtime(runtime, item_id, request.operation)
            if not await operation_supported(current, item_id, request.operation):
                reason = f"{request.operation}_unsupported"
            else:
                config = current.delete_config if request.operation == "delete" else current.query_config
                prefix = "delete" if request.operation == "delete" else "exists"
                if await _read_cleanup(runtime, lambda connection: _attempts_used(connection, f'{prefix}/{item_id}')) >= min(config.max_attempts, pending.request.config.max_attempts):
                    reason = "budget_exhausted"
                else:
                    waiting = None
                    if not canceled:
                        waiting = (await _retry_wait_step(current, f'delete/{item_id}', config, 'delete_retry_wait')
                            if request.operation == "delete" else await _query_retry_wait_step(current, item_id))
                    if waiting is not None:
                        return waiting
        if reason is not None:
            del runtime.pending_intents[item_id]
            return DbOutcome(DbOutcomeKind.COMPLETED,
                BeginAttemptResult(BeginDisposition.REJECTED, reason=reason))
    def apply_completed(result):
        if not isinstance(result.value, BeginAttemptResult):
            raise ConsistencyError("清理意图回执类型不对应")
        ticket = result.value.ticket
        if ticket is None:
            if result.value.disposition is not BeginDisposition.REJECTED:
                raise ConsistencyError("清理意图授予回执没有票据")
            del runtime.pending_intents[item_id]
        else:
            prefix = "delete" if pending.request.operation == "delete" else "exists"
            if (result.value.disposition is not BeginDisposition.GRANTED
                    or ticket.operation != pending.request.operation or ticket.target_id != str(item_id)
                    or ticket.responsibility_key != f"{prefix}/{item_id}"):
                raise ConsistencyError("清理意图票据与原申请不对应")
            pending.ticket = ticket
        pending.last_kind = result.kind

    try:
        result = await runtime.database.write(lambda owned: runtime.outputs.begin_cleanup_attempt(pending.request, pending.key, owned), key=pending.key)
    except BaseException as error:
        actual = db_outcome_for(error)
        if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
            try:
                apply_completed(actual)
            except BaseException as application_error:
                raise error from application_error
        raise
    if not isinstance(result, DbOutcome):
        raise ConsistencyError("清理意图保存没有可靠数据库回执")
    pending.last_kind = result.kind
    if result.kind is DbOutcomeKind.COMPLETED:
        apply_completed(result)
    return result


def _not_dispatched(operation, error=None):
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
        Settlement, SettlementBasis)
    return DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED,
        error=error or ErrorValue("canceled_before_dispatch", operation),
        effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                              EvidenceValue("dispatch_prevented", 1, {})), observations=()))


def _not_dispatched_evidence(original, ticket):
    """框架阻止原票据派发时固定操作视图，不接受设备自报的替代合同。"""
    from camctl.devices.evidence import EvidenceContract
    return original.with_contract(EvidenceContract(
        "dispatch_prevented", 1, ticket.operation, frozenset()))


def _delete_presence(result, item_id):
    """DELETE 唯一观察与完整返回一起验证，不借查询获取相同事实。"""
    presence = None
    found = False
    for observation in result.observations:
        if not isinstance(observation, DeviceObservation):
            raise OutcomeValidationError("删除返回的观察不是合法 DeviceObservation")
        if observation.type not in ("file_absent", "file_delete_presence"):
            continue
        if found or type(observation.version) is not int or observation.version != 1 or observation.data.get("cleanup_item_id") != str(item_id):
            raise OutcomeValidationError("删除文件观察的版本、数量或成员身份不对应")
        expected = {"cleanup_item_id"} if observation.type == "file_absent" else {"cleanup_item_id", "present"}
        if set(observation.data) != expected:
            raise OutcomeValidationError("删除文件观察字段不完整")
        presence = False if observation.type == "file_absent" else observation.data["present"]
        if type(presence) is not bool:
            raise OutcomeValidationError("删除文件观察 present 必须是布尔值")
        found = True
    return presence


def _legacy_error(actual, operation):
    """兼容返回也保留完整错误字段，不替换为通用 device_error。"""
    from camctl.operations.models import ErrorValue
    if actual.error is None:
        return None
    if isinstance(actual.error, ErrorValue):
        return actual.error
    return ErrorValue(actual.error["code"], actual.error.get("stage", operation), actual.error.get("details", {}))


def _record_cleanup_actual(pending) -> None:
    """由原结果拥有者准备诊断；取消未派发是正常控制分支。"""
    actual = pending.actual
    exception = pending.diagnostic_exception or actual.diagnostic_error
    error = actual.outcome.error if actual.outcome is not None else actual.error
    code = (error.code if isinstance(error, ErrorValue) else
            error.get("code") if isinstance(error, Mapping) else None)
    if (error is None or code == "canceled_before_dispatch") and exception is None:
        return
    context = {"database_id": pending.database_id, "cleanup_item_id": pending.item_id}
    if actual.outcome is not None:
        record_call_outcome(actual.outcome, ticket=pending.ticket,
                            run_error=exception,
                            context=context, state=pending.diagnostic_state)
    else:
        stage = (error.stage if isinstance(error, ErrorValue) else
                 error.get("stage", pending.ticket.operation) if isinstance(error, Mapping)
                 else pending.ticket.operation)
        record_error(error or exception, stage=stage, context=context,
                     state=pending.diagnostic_state,
                     details={"ticket": pending.ticket, "observations": actual.observations,
                              "exception": exception})


def _raise_cleanup_validation(pending, original: BaseException | None = None) -> None:
    diagnostic = pending.validation_diagnostic
    error = OutcomeValidationError(diagnostic.message)
    stage = f"cleanup_{pending.ticket.operation}_validation"
    state = record_error(original or error, stage=stage, state=pending.validation_state,
                         context={"database_id": pending.database_id,
                                  "cleanup_item_id": pending.item_id},
                         details={"ticket": pending.ticket, "actual": pending.actual})
    # 原错误已由同一诊断持有，外层继续传播原有校验异常类型。
    record_error(error, stage=stage, state=state)
    raise error from None


async def _save_delete_return(runtime, pending):
    from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunStatus
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, EvidenceValue,
        Settlement, SettlementBasis)
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    if (pending.database_id != runtime.database.metadata.instance_id
            or pending.ticket.operation != "delete"
            or pending.ticket.target_id != str(pending.item_id)
            or pending.ticket.responsibility_key != f"delete/{pending.item_id}"):
        raise ConsistencyError("原删除返回不属于本次数据库或原成员票据")
    _record_cleanup_actual(pending)
    original = _matching_cleanup_intent(runtime, pending.item_id, pending.ticket,
        pending.consumer if isinstance(pending, PendingCleanupQuery) else None)
    if pending.occurred_at is None:
        pending.occurred_at = runtime.occurred_at()
    if pending.returned_ns is None:
        pending.returned_ns = runtime.monotonic_ns()
    if pending.validation_diagnostic is not None:
        _raise_cleanup_validation(pending)
    if pending.finish is None:
        retry_config = await _original_cleanup_config(runtime, pending.item_id, pending.ticket, original)
        try:
            present = _delete_presence(pending.actual, pending.item_id)
            error = _legacy_error(pending.actual, "delete")
            outcome = pending.actual.outcome or CallOutcome(
                status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
                error=error, effect=EffectState.CONFIRMED if present is not None else EffectState.UNKNOWN,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})),
                observations=pending.actual.observations)
            pending.finish = AttemptFinish(pending.ticket,
                validate_outcome(pending.ticket, outcome, pending.evidence),
                pending.occurred_at,
                retry_wait=(present is not False and retry_config.retry_interval_s is not None),
                recovery_max_event_id=pending.recovery_max_event_id)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            original_error = error
            if not isinstance(error, OutcomeValidationError):
                error = OutcomeValidationError(f"原调用返回无法按契约解释: {error}")
            pending.validation_diagnostic = CleanupValidationDiagnostic(type(error).__name__, str(error))
            _raise_cleanup_validation(pending, original_error)
    if not pending.saved:
        async def apply_completed(saved):
            value = saved.value
            if not isinstance(value, FinishAttemptResult) or not isinstance(value.run_status, RunStatus):
                raise ConsistencyError("原删除结束回执与原返回不对应")
            if value.disposition is FinishDisposition.ALREADY_ENDED:
                await _recover_already_ended(runtime, pending, value)
            elif (value.disposition is not FinishDisposition.SAVED
                  or value.attempt_status is not pending.finish.outcome.outcome.status):
                raise ConsistencyError("原删除结束回执与原返回不对应")
            pending.saved = True

        try:
            saved = await runtime.database.write(lambda owned: runtime.outputs.finish_cleanup_attempt(pending.finish, pending.key, owned), key=pending.key)
        except BaseException as error:
            actual = db_outcome_for(error)
            if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
                try:
                    await apply_completed(actual)
                except BaseException as application_error:
                    raise error from application_error
            raise
        if not isinstance(saved, DbOutcome):
            raise ConsistencyError("删除结束未返回合法数据库回执")
        if saved.kind is not DbOutcomeKind.COMPLETED:
            error = db_failure(ConsistencyError,
                f"删除原实际结果未可靠保存（{saved.kind.value}），清理成员={pending.item_id}", saved)
            if not isinstance(error, Exception):
                raise error
            return CleanupStep("finish_rejected", str(saved.error), exception=error)
        await apply_completed(saved)
    return None


async def _recover_already_ended(runtime, pending, value):
    """枚举回执不交付观察；从原票据的完整历史取得实际已保存结果。"""
    from camctl.outputs.cleanup_results import saved_cleanup_attempt
    saved = await runtime.database.read(lambda owned: saved_cleanup_attempt(owned, pending.ticket, database_id=pending.database_id, item_id=pending.item_id))
    if saved.actual.outcome.status is not value.attempt_status:
        raise ConsistencyError("已结束回执与原可靠尝试结果不同")
    pending.saved_actual = saved.actual
    pending.saved_key = saved.key
    pending.saved_occurred_at = saved.occurred_at
    if saved.actual.outcome != pending.finish.outcome.outcome:
        pending.diagnostic = "迟到返回与原已保存结果不同，保留原终态并交接已保存事实"


async def _consume_delete_return(runtime, pending):
    from camctl.operations.models import SettlementBasis
    from camctl.persistence.models import DbOutcomeKind
    item_id = pending.item_id
    if not pending.saved or pending.database_id != runtime.database.metadata.instance_id:
        raise ConsistencyError("原删除返回尚未可靠保存")
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id,status,output_id,error_code FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
    if row is None:
        raise ConsistencyError("原删除返回所属成员不存在")
    action_id, status, output_id, error_code = row
    if status in _MEMBER_TERMINAL:
        from camctl.outputs.cleanup_absence import handoff_terminal_absence
        rejected = await handoff_terminal_absence(runtime, item_id)
        if rejected is not None:
            return rejected
        outcome, error = _terminal_companion(status, error_code)
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        runtime.pending_deletes.pop(item_id)
        runtime.pending_intents.pop(item_id, None)
        return CleanupStep("already_terminal", diagnostic=pending.diagnostic)
    if ((pending.binding_failure is not None or pending.delete_unsupported) and (pending.saved_actual is None
            or _delete_presence(pending.saved_actual, item_id) is not False)):
        if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
            from camctl.persistence.repositories.outputs import _member_delete_dispatch_state
            dispatched, _unsettled = await _read_cleanup(runtime, lambda connection: _member_delete_dispatch_state(connection, item_id))
            if not dispatched:
                step = await _cancel_member(runtime, item_id, status, output_id)
                if step.phase == "canceled":
                    runtime.pending_deletes.pop(item_id)
                    runtime.pending_intents.pop(item_id, None)
                return step
        if pending.delete_unsupported and pending.binding_failure is None:
            step = await _fail_unsupported(runtime, item_id, output_id)
        else:
            step = await _finish_binding_failure(runtime, item_id, canceled=await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)), binding_result=pending.binding_failure)
        if step is not None and step.phase in ("failed", "canceled"):
            runtime.pending_deletes.pop(item_id)
            runtime.pending_intents.pop(item_id, None)
        return step
    actual_result = pending.saved_actual or pending.actual
    present = _delete_presence(actual_result, item_id)
    if present is False:
        choice = (CleanupOutcomeChoice.DELETED if actual_result.error is None
                  else CleanupOutcomeChoice.ABSENCE_CONFIRMED)
        done = await save_cleanup_request(runtime, 'finish_cleanup_item', FinishCleanupItem(item_id, choice, runtime.occurred_at()))
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        actual = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status,error_code FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        if actual is None or actual[0] not in _MEMBER_TERMINAL:
            raise ConsistencyError("原删除的成员回执缺少可靠终态")
        outcome, error = _terminal_companion(*actual)
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        runtime.pending_deletes.pop(item_id)
        runtime.pending_intents.pop(item_id, None)
        return (CleanupStep("succeeded", choice.name, pending.diagnostic) if actual[0] == 4
                else CleanupStep("already_terminal", diagnostic=pending.diagnostic))
    runtime.pending_deletes.pop(item_id)
    runtime.pending_intents.pop(item_id, None)
    not_dispatched = (
        (actual_result.outcome or pending.finish.outcome.outcome).settlement.basis
        is SettlementBasis.NOT_DISPATCHED)
    record_consumption(runtime, pending)
    if not_dispatched:
        if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
            return await _cancel_member(runtime, item_id, status, output_id)
        # 等待取消只结束原 DELETE ticket。成员仍属 DELETING；下一轮
        # 按保存事实和剩余预算授予新 ticket，当前轮不得立即重投。
        check_call_interruption()
        return CleanupStep("not_dispatched")
    if present is True:
        runtime.retry_gate.established(f"delete/{item_id}", pending.returned_ns)
        if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
            return await _cancel_with_error(runtime, item_id, output_id, 'file_delete_failed')
        return CleanupStep("still_present", diagnostic=pending.diagnostic)
    runtime.retry_gate.cleared(f"delete/{item_id}")
    check_call_interruption()
    if await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
        return await _settle_canceling_member(runtime, item_id, output_id)
    if not runtime.allow_new_calls:
        return CleanupStep("local_handoff")
    runtime = await operation_runtime(runtime, item_id, 'query')
    if not await operation_supported(runtime, item_id, 'query'):
        return await _fail_unsupported(runtime, item_id, output_id)
    return await _verify_after_delete(runtime, item_id, action_id, output_id)


@owned_tool_call
async def _delete_once(runtime, item_id, action_id, output_id):
    """可靠原意图只派发一次，返回后的任何保存故障沿原申请续接。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
    from camctl.operations.models import SettlementBasis
    from camctl.persistence.models import DbOutcomeKind
    supervision_diagnostic = None
    diagnostic_exception = None
    pending = runtime.pending_deletes.get(item_id)
    if pending is not None:
        supervision_diagnostic = pending.supervision_diagnostic
    if pending is None:
        original = runtime.pending_intents.get(item_id)
        if original is None:
            if not runtime.allow_new_calls:
                return CleanupStep("local_handoff")
            failed_binding = await _finish_binding_failure(runtime, item_id)
            if failed_binding is not None:
                return failed_binding
            runtime = await operation_runtime(runtime, item_id, 'delete')
            if not await operation_supported(runtime, item_id, 'delete'):
                return await _fail_unsupported(runtime, item_id, output_id)
            waiting = await _retry_wait_step(runtime, f'delete/{item_id}', runtime.delete_config, 'delete_retry_wait')
            if waiting is not None:
                return waiting
        request = original.request if original else AttemptIntent(
            operation="delete", action_id=action_id, kind=OperationKind.DELETE_FILE,
            target=AttemptTarget(cleanup_item_id=item_id), query_purpose=None,
            config=runtime.delete_config, occurred_at=runtime.occurred_at())
        saved = await settle_required(_begin_cleanup_intent(runtime, request, None))
        if isinstance(saved, CleanupStep):
            return saved
        if saved.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("delete_budget_rejected", str(saved.error))
        if saved.value.ticket is None:
            if saved.value.reason == "cleanup_waiting":
                return CleanupStep("waiting")
            if saved.value.reason == "budget_exhausted":
                return await _fail_exhausted(runtime, item_id, output_id, 'delete', runtime.delete_config, 'delete_attempts_exhausted')
            if saved.value.reason == "delete_unsupported":
                return await _fail_unsupported(runtime, item_id, output_id)
            if saved.value.reason == "binding_unavailable":
                return await _finish_binding_failure(runtime, item_id)
            if saved.value.reason == "canceled":
                return await _cancel_member(runtime, item_id, 3, output_id)
            return CleanupStep("delete_budget_rejected", saved.value.reason)
        intent = runtime.pending_intents[item_id]
        if not runtime.allow_new_calls:
            return CleanupStep("local_handoff")
        if intent.dispatched:
            raise ConsistencyError("原删除已派发但尚无可交接实际结果")
        member_status = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        canceled = await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id))
        binding_failure = await _current_binding_failure(runtime, item_id)
        if (not canceled and binding_failure is None and member_status is not None
                and member_status[0] not in _MEMBER_TERMINAL):
            runtime = await operation_runtime(runtime, item_id, 'delete')
        delete_unsupported = not await operation_supported(runtime, item_id, 'delete') if not canceled and binding_failure is None else False
        if canceled or binding_failure is not None or delete_unsupported or member_status is None or member_status[0] in _MEMBER_TERMINAL:
            from camctl.operations.models import ErrorValue
            error = (ErrorValue("device_binding_unavailable", "execution", binding_failure_details(binding_failure))
                     if binding_failure is not None else
                     ErrorValue("file_delete_failed", "delete", {"output_id": str(output_id)})
                     if delete_unsupported and not canceled else None)
            actual = _not_dispatched("delete", error)
            evidence = _not_dispatched_evidence(runtime.evidence, intent.ticket)
        else:
            local_target_error = None
            try:
                port, call_request, local_call = await _prepare_cleanup_call(
                    runtime, item_id, intent.ticket, 'delete')
            except LocalArtifactTargetError as error:
                diagnostic_exception = local_target_error = error
            can_dispatch = (local_target_error is None and await _read_cleanup(
                runtime, lambda connection: _cleanup_dispatch_allowed(
                    connection, item_id, intent.ticket)))
            if local_target_error is not None or not can_dispatch or not runtime.allow_new_calls:
                from camctl.operations.models import ErrorValue
                error = (ErrorValue(
                    "file_delete_failed", "delete", {"output_id": str(output_id),
                                                        "reason": str(local_target_error)})
                         if local_target_error is not None else None)
                actual = _not_dispatched("delete", error)
                evidence = _not_dispatched_evidence(runtime.evidence, intent.ticket)
            else:
                check_call_interruption()
                intent.dispatched = True
                identity = (runtime.database.metadata.instance_id,intent.ticket.run_id,intent.ticket.attempt_id)
                runtime.active_calls.add(identity)
                evidence = runtime.evidence
                try:
                    try:
                        with measure_stage("cleanup.delete", database_id=runtime.database.metadata.instance_id,
                                action_id=action_id, output_id=output_id, cleanup_item_id=item_id,
                                run_id=intent.ticket.run_id, attempt_id=intent.ticket.attempt_id):
                            actual = await port.delete(call_request)
                    except LocalArtifactTaskFailure as error:
                        actual = error.actual
                        supervision_diagnostic = error.supervision_diagnostic
                        if supervision_diagnostic is not None:
                            diagnostic_exception = error
                    except LocalArtifactTargetError as error:
                        diagnostic_exception = error
                        from camctl.operations.models import ErrorValue
                        actual = _not_dispatched("delete", ErrorValue(
                            "file_delete_failed", "delete", {
                                "output_id": str(output_id), "reason": str(error)}))
                        evidence = _not_dispatched_evidence(runtime.evidence, intent.ticket)
                    if (local_call and actual.outcome is not None
                            and actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED):
                        evidence = _not_dispatched_evidence(runtime.evidence, intent.ticket)
                finally:
                    runtime.active_calls.discard(identity)
        pending = PendingCleanupDelete(runtime.database.metadata.instance_id, item_id, intent.ticket,
            actual, evidence, None, None, new_operation_key(),
            supervision_diagnostic=supervision_diagnostic,
            diagnostic_exception=diagnostic_exception)
        runtime.pending_deletes[item_id] = pending
        pending.binding_failure = binding_failure
        pending.delete_unsupported = delete_unsupported
        pending.occurred_at = runtime.occurred_at()
        pending.returned_ns = runtime.monotonic_ns()
    try:
        rejected = await settle_required(_save_delete_return(runtime, pending))
    except BaseException as error:
        _raise_supervision_with_error(
            pending, error, "删除原实际结果尚未可靠保存")
        raise
    if rejected is not None:
        _raise_supervision_with_error(
            pending, rejected.exception or ConsistencyError(
                f"删除原实际结果未可靠保存: {rejected.phase} {rejected.detail}"),
            "删除原实际结果尚未可靠保存")
        return rejected
    _deliver_supervision(pending, "删除")
    return await _consume_delete_return(runtime, pending)




async def _verify_after_delete(
        runtime: CleanupRuntime, item_id: int, action_id: int,
        output_id: int) -> CleanupStep:
    """删除效果未知时用查询预算核实；确认仍在时等待预算内重试。"""
    from camctl.persistence.models import DbOutcomeKind

    waiting = await _query_retry_wait_step(runtime, item_id)
    if waiting is not None:
        return waiting
    ticket = await _begin_query_attempt(runtime, item_id, action_id, CleanupQueryConsumer.AFTER_DELETE)
    if isinstance(ticket, CleanupStep):
        return ticket
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(ticket.error))
    if ticket.value.ticket is None:
        if ticket.value.reason == "query_unsupported":
            return await _fail_unsupported(runtime, item_id, output_id)
        if ticket.value.reason == "budget_exhausted":
            return await _fail_exhausted(runtime, item_id, output_id, 'exists', runtime.query_config, 'file_query_attempts_exhausted')
        return CleanupStep("query_budget_rejected", ticket.value.reason)
    present = await _run_query(
        runtime, ticket.value.ticket, item_id, CleanupQueryConsumer.AFTER_DELETE)
    return await settle_required(_consume_saved_query(runtime, runtime.pending_queries[ticket.value.ticket.run_id, ticket.value.ticket.attempt_id]))


async def _cancel_member(
        runtime: CleanupRuntime, item_id: int, status: int,
        output_id) -> CleanupStep:
    """取消已生效的成员处理：未发出删除解除限制，删除中按结论收场。"""
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import (
        _member_delete_dispatch_state)

    pending = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND (a.status=1 OR a.result_json IS NULL) LIMIT 1', (item_id,)).fetchone())
    if pending is not None:
        # 原删除或查询未可靠收场；取消保持原成员、限制和调用责任。
        return CleanupStep("call_tracking")

    if status in (1, 2):
        canceled = await save_cleanup_request(runtime, 'cancel_cleanup_item', CancelCleanupItem(item_id, runtime.occurred_at()))
        if canceled.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("cancel_rejected", str(canceled.error))
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return (CleanupStep("already_terminal") if canceled.value.disposition is CleanupItemDisposition.ALREADY
                else CleanupStep("canceled"))
    dispatched, _unsettled = await _read_cleanup(runtime, lambda connection: _member_delete_dispatch_state(connection, item_id))
    if not dispatched:
        # 删除中成员可靠从未派发自己的删除（只有查询或可靠未派发的
        # 删除尝试）：无删除错误取消并解除本项可撤销限制。
        canceled = await save_cleanup_request(runtime, 'cancel_cleanup_item', CancelCleanupItem(item_id, runtime.occurred_at()))
        if canceled.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("cancel_rejected", str(canceled.error))
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return (CleanupStep("already_terminal") if canceled.value.disposition is CleanupItemDisposition.ALREADY
                else CleanupStep("canceled"))
    return await _settle_canceling_member(runtime, item_id, output_id)


async def _settle_canceling_member(
        runtime: CleanupRuntime, item_id: int, output_id,
) -> CleanupStep:
    """删除中成员的取消收场：按可靠文件事实保存成功或取消错误。

    已有可靠缺席事实保存成功；晚于未决删除的在场确认按删除失败收
    场；无可靠结论时用自己的查询预算核实，额度耗尽或仍未知按结果
    未知保存 delete_unconfirmed，不补造查询次数耗尽或文件仍在结论。
    """
    from camctl.persistence.models import DbOutcomeKind

    facts = await _read_cleanup(runtime, lambda connection: _output_delete_facts(connection, output_id))
    if facts["file_absent"]:
        done = await save_cleanup_request(runtime, 'finish_cleanup_item', FinishCleanupItem(item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED, runtime.occurred_at()))
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = await settle_required(_settle_companion_runs(runtime, item_id))
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
    if facts["confirmed_present_after"]:
        return await _cancel_with_error(runtime, item_id, output_id, 'file_delete_failed')
    from camctl.outputs.cleanup_results import cleanup_cancel_query_available
    if not await _read_cleanup(runtime, lambda connection: cleanup_cancel_query_available(connection, item_id)):
        return await _cancel_with_error(runtime, item_id, output_id, 'delete_unconfirmed')
    if not runtime.allow_new_calls:
        return CleanupStep("local_handoff")
    binding_failure = await _finish_binding_failure(runtime, item_id, canceled=True)
    if binding_failure is not None:
        return binding_failure
    runtime = await operation_runtime(runtime, item_id, 'query')
    if not await operation_supported(runtime, item_id, 'query'):
        return await _cancel_with_error(runtime, item_id, output_id, 'delete_unconfirmed')
    ticket = await _begin_query_attempt(runtime, item_id, await _read_cleanup(runtime, lambda connection: _item_action(connection, item_id)), CleanupQueryConsumer.CANCEL)
    if isinstance(ticket, CleanupStep):
        return ticket
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(ticket.error))
    if ticket.value.ticket is not None:
        present = await _run_query(
            runtime, ticket.value.ticket, item_id, CleanupQueryConsumer.CANCEL)
        return await settle_required(_consume_saved_query(runtime, runtime.pending_queries[ticket.value.ticket.run_id, ticket.value.ticket.attempt_id]))
    return await _cancel_with_error(runtime, item_id, output_id, 'delete_unconfirmed')


def _item_action(connection, item_id: int) -> int:
    row = connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id = ?",
        (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"清理成员不存在: {item_id}")
    return int(row[0])


async def _cancel_with_error(
        runtime: CleanupRuntime, item_id: int, output_id, code: str,
) -> CleanupStep:
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    canceled = await save_cleanup_request(runtime, 'cancel_cleanup_item', CancelCleanupItem(item_id, runtime.occurred_at(), code, {'output_id': str(output_id)}))
    if canceled.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("cancel_rejected", str(canceled.error))
    outcome = (RunOutcome.FAILED if code == "file_delete_failed"
               else RunOutcome.UNCONFIRMED)
    rejected = await settle_required(_settle_companion_runs(runtime, item_id))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    return (CleanupStep("already_terminal") if canceled.value.disposition is CleanupItemDisposition.ALREADY
            else CleanupStep("canceled", code))


async def _fail_exhausted(
        runtime: CleanupRuntime, item_id: int, output_id, operation: str,
        config, code: str) -> CleanupStep:
    from camctl.persistence.models import DbOutcomeKind

    failed = await save_cleanup_request(runtime, 'fail_cleanup_item', FailCleanupItem(item_id, code, await _read_cleanup(runtime, lambda connection: _exhaustion_details(connection, item_id, output_id, operation, config)), runtime.occurred_at()))
    if failed.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("fail_rejected", str(failed.error))
    stage = "delete" if operation == "delete" else "query"
    rejected = await settle_required(_settle_companion_runs(runtime, item_id))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    return (CleanupStep("already_terminal") if failed.value.disposition is CleanupItemDisposition.ALREADY
            else CleanupStep("failed", code))


async def _begin_query_attempt(runtime: CleanupRuntime, item_id: int, action_id: int,
                         consumer=CleanupQueryConsumer.BEFORE_DELETE):
    """登记一次存在性查询尝试；预算独立于删除流程。

    首次查询取得处理归属、查询流程与本次尝试在同一事务建档；已在
    处理阶段的成员只登记查询尝试。
    """
    from camctl.operations.attempts import (
        AttemptIntent, AttemptTarget, OperationKind)

    original = runtime.pending_intents.get(item_id)
    if original is not None:
        return await settle_required(_begin_cleanup_intent(runtime, original.request, consumer))
    if not runtime.allow_new_calls:
        return CleanupStep("local_handoff")
    runtime = await operation_runtime(runtime, item_id, 'query')
    return await settle_required(_begin_cleanup_intent(runtime, AttemptIntent(operation='query', action_id=action_id, kind=OperationKind.CHECK_FILE_EXISTS, target=AttemptTarget(cleanup_item_id=item_id), query_purpose=None, config=runtime.query_config, occurred_at=runtime.occurred_at()), consumer))



@owned_tool_call
async def _run_query(
        runtime: CleanupRuntime, ticket: AttemptTicket, item_id: int,
        consumer: CleanupQueryConsumer | None = None,
) -> bool | None:
    """持有原实际返回；只有完整原申请可靠保存后才交付观察。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.models import SettlementBasis

    if (ticket.operation != "query" or ticket.target_id != str(item_id)
            or ticket.responsibility_key != f"exists/{item_id}"):
        raise ConsistencyError("查询票据与清理成员不对应")
    identity = (ticket.run_id, ticket.attempt_id)
    pending = runtime.pending_queries.get(identity)
    consumer = consumer or (pending.consumer if pending is not None else CleanupQueryConsumer.BEFORE_DELETE)
    database_id = runtime.database.metadata.instance_id
    supervision_diagnostic = (None if pending is None
                              else pending.supervision_diagnostic)
    if pending is not None:
        if (pending.database_id != database_id or pending.ticket != ticket
                or pending.item_id != item_id or pending.consumer is not consumer):
            raise ConsistencyError("原查询持有责任与本次数据库或票据不对应")
    else:
        diagnostic_exception = None
        intent = runtime.pending_intents.get(item_id)
        if intent is not None:
            if (intent.database_id != database_id or intent.ticket != ticket
                    or intent.consumer is not consumer or intent.dispatched):
                raise ConsistencyError("原查询派发资格与票据、用途或数据库不对应")
        canceled = await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, _item_action(connection, item_id))) if intent else False
        if canceled:
            from camctl.persistence.repositories.outputs import _member_delete_dispatch_state
            dispatched, unsettled = await _read_cleanup(runtime, lambda connection: _member_delete_dispatch_state(connection, item_id))
        else:
            dispatched, unsettled = True, False
        member_status = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone()) if intent else None
        binding_failure = await _current_binding_failure(runtime, item_id) if intent else None
        from camctl.outputs.cleanup_results import cleanup_cancel_query_available
        cancel_available = (not canceled or await _read_cleanup(runtime, lambda connection: cleanup_cancel_query_available(connection, item_id, ticket=ticket)))
        blocked = binding_failure is not None or (canceled and (not dispatched or unsettled or not cancel_available)) or (intent and (member_status is None or member_status[0] in _MEMBER_TERMINAL))
        query_unsupported = False
        if not blocked:
            runtime = await operation_runtime(runtime, item_id, 'query')
            query_unsupported = not await operation_supported(runtime, item_id, 'query')
        if blocked or query_unsupported:
            from camctl.operations.models import ErrorValue
            error = (ErrorValue("device_binding_unavailable", "execution", binding_failure_details(binding_failure))
                     if binding_failure is not None else None)
            if error is None and query_unsupported:
                output_id = (await _read_cleanup(runtime, lambda connection: connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()))[0]
                error = ErrorValue("file_delete_failed", "query", {"output_id": str(output_id)})
            actual = _not_dispatched("query", error)
            evidence = _not_dispatched_evidence(runtime.evidence, ticket)
        else:
            local_target_error = None
            try:
                port, call_request, local_call = await _prepare_cleanup_call(
                    runtime, item_id, ticket, 'query')
            except LocalArtifactTargetError as error:
                diagnostic_exception = local_target_error = error
            can_dispatch = (local_target_error is None and await _read_cleanup(
                runtime, lambda connection: _cleanup_dispatch_allowed(
                    connection, item_id, ticket)))
            if local_target_error is not None or not can_dispatch or not runtime.allow_new_calls:
                from camctl.operations.models import ErrorValue
                error = (ErrorValue("file_delete_failed", "query",
                                    {"reason": str(local_target_error)})
                         if local_target_error is not None else None)
                actual = _not_dispatched("query", error)
                evidence = _not_dispatched_evidence(runtime.evidence, ticket)
            else:
                check_call_interruption()
                if intent is not None:
                    intent.dispatched = True
                active = (database_id,ticket.run_id,ticket.attempt_id)
                runtime.active_calls.add(active)
                evidence = runtime.evidence
                try:
                    try:
                        with measure_stage("cleanup.query", database_id=database_id,
                                cleanup_item_id=item_id,
                                run_id=ticket.run_id, attempt_id=ticket.attempt_id):
                            actual = await port.query_file(call_request)
                    except LocalArtifactTaskFailure as error:
                        actual = error.actual
                        supervision_diagnostic = error.supervision_diagnostic
                        if supervision_diagnostic is not None:
                            diagnostic_exception = error
                    except LocalArtifactTargetError as error:
                        diagnostic_exception = error
                        from camctl.operations.models import ErrorValue
                        actual = _not_dispatched("query", ErrorValue(
                            "file_delete_failed", "query", {"reason": str(error)}))
                        evidence = _not_dispatched_evidence(runtime.evidence, ticket)
                    if (local_call and actual.outcome is not None
                            and actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED):
                        evidence = _not_dispatched_evidence(runtime.evidence, ticket)
                finally:
                    runtime.active_calls.discard(active)
        pending = PendingCleanupQuery(
            database_id=database_id, item_id=item_id, consumer=consumer,
            ticket=ticket, actual=actual, evidence=evidence,
            occurred_at=None, returned_ns=None,
            key=new_operation_key(),
            supervision_diagnostic=supervision_diagnostic,
            diagnostic_exception=diagnostic_exception)
        # 原返回先于验证和首次结果写入进入会话责任。
        runtime.pending_queries[identity] = pending
        pending.binding_failure = binding_failure
        pending.query_unsupported = query_unsupported
        pending.occurred_at = runtime.occurred_at()
        pending.returned_ns = runtime.monotonic_ns()

    return await settle_required(_save_query_return(runtime, pending))


async def _save_query_return(runtime, pending):
    """原查询返回可靠保存及事件循环交接为同一个必要阶段。"""
    from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunStatus
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    item_id = pending.item_id
    if pending.phase is not CleanupQueryPhase.SAVED:
        _record_cleanup_actual(pending)
    original = _matching_cleanup_intent(runtime, pending.item_id, pending.ticket,
        pending.consumer if isinstance(pending, PendingCleanupQuery) else None)
    if pending.occurred_at is None:
        pending.occurred_at = runtime.occurred_at()
    if pending.returned_ns is None:
        pending.returned_ns = runtime.monotonic_ns()
    try:
        if pending.validation_diagnostic is not None:
            _raise_cleanup_validation(pending)
        if pending.finish is None and pending.phase is not CleanupQueryPhase.SAVED:
            retry_config = await _original_cleanup_config(runtime, pending.item_id, pending.ticket, original)
            try:
                pending.finish = _prepare_query_finish(
                    pending, retry_wait=retry_config.retry_interval_s is not None)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                original_error = error
                if not isinstance(error, OutcomeValidationError):
                    error = OutcomeValidationError(f"原调用返回无法按契约解释: {error}")
                pending.validation_diagnostic = CleanupValidationDiagnostic(
                    type(error).__name__, str(error))
                _raise_cleanup_validation(pending, original_error)
            pending.phase = CleanupQueryPhase.PREPARED
        if pending.phase is not CleanupQueryPhase.SAVED:
            async def apply_completed(saved):
                value = saved.value
                if not isinstance(value, FinishAttemptResult) or not isinstance(value.run_status, RunStatus):
                    raise ConsistencyError("查询结果未返回可靠数据库回执")
                if value.disposition is FinishDisposition.ALREADY_ENDED:
                    await _recover_already_ended(runtime, pending, value)
                elif (value.disposition is not FinishDisposition.SAVED
                      or value.attempt_status is not pending.finish.outcome.outcome.status):
                    raise ConsistencyError("原查询结束回执与原返回不对应")
                pending.phase = CleanupQueryPhase.SAVED
                runtime.pending_intents.pop(item_id, None)

            try:
                saved = await runtime.database.write(lambda owned: runtime.operations.finish_attempt(pending.finish, pending.key, owned), key=pending.key)
            except BaseException as error:
                actual = db_outcome_for(error)
                if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
                    try:
                        await apply_completed(actual)
                    except BaseException as application_error:
                        raise error from application_error
                raise
            if not isinstance(saved, DbOutcome):
                raise ConsistencyError("查询结果保存未返回合法数据库结果")
            if saved.kind is not DbOutcomeKind.COMPLETED:
                error = ConsistencyError(
                    f"原查询结果未可靠保存（{saved.kind.value}）: {saved.error}")
                if isinstance(saved.error, BaseException):
                    raise error from saved.error
                raise error
            await apply_completed(saved)
    except BaseException as error:
        _raise_supervision_with_error(
            pending, error, "查询原实际结果尚未可靠保存")
        raise
    _deliver_supervision(pending, "查询")
    present = _presence_observation(pending.saved_actual or pending.actual)
    runtime.pending_intents.pop(item_id, None)
    return present


def _pending_query_for_item(runtime: CleanupRuntime, item_id: int) -> PendingCleanupQuery | None:
    queries = [pending for pending in runtime.pending_queries.values() if pending.item_id == item_id]
    if len(queries) > 1:
        raise ConsistencyError(f"清理成员存在多个尚未交接的原查询: {item_id}")
    return queries[0] if queries else None


async def _restore_saved_query(runtime: CleanupRuntime, item_id: int) -> PendingCleanupQuery | None:
    """新会话从持久化结果恢复本地交接，不重新验证当前驱动。"""
    from camctl.outputs.cleanup_results import saved_cleanup_query

    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
    if row is None or row[0] in _MEMBER_TERMINAL:
        runtime.consumed_queries.pop(item_id, None)
        return None
    saved = await _read_cleanup(runtime, lambda connection: saved_cleanup_query(connection, item_id))
    if saved is None:
        return None
    identity = (saved.ticket.run_id, saved.ticket.attempt_id)
    database_id = runtime.database.metadata.instance_id
    consumed = runtime.consumed_queries.get(item_id)
    if consumed is not None and consumed.query_identity == (
            database_id, item_id, saved.ticket, saved.key):
        return None
    pending = PendingCleanupQuery(
        database_id, item_id,
        CleanupQueryConsumer.AFTER_DELETE if saved.after_delete else CleanupQueryConsumer.BEFORE_DELETE,
        saved.ticket, saved.actual, None, saved.occurred_at, runtime.monotonic_ns(),
        saved.key, phase=CleanupQueryPhase.SAVED)
    runtime.pending_queries[identity] = pending
    return pending


async def _consume_saved_query(runtime: CleanupRuntime, pending: PendingCleanupQuery) -> CleanupStep | None:
    """原观察只交接一次；成员与伴随申请可靠完成后释放原查询。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.models import ErrorValue
    from camctl.persistence.models import DbOutcomeKind

    if (pending.phase is not CleanupQueryPhase.SAVED
            or pending.database_id != runtime.database.metadata.instance_id):
        raise ConsistencyError("原查询尚未可靠保存或不属于本次数据库")
    item_id = pending.item_id
    identity = (pending.ticket.run_id, pending.ticket.attempt_id)
    present = _presence_observation(pending.saved_actual or pending.actual)
    if pending.member is None:
        row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT action_id,status,output_id,error_code FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        if row is None:
            raise ConsistencyError(f"原查询所属清理成员不存在: {item_id}")
        action_id, status, output_id, error_code = row
        if status in (4, 5, 6):
            from camctl.outputs.cleanup_absence import handoff_terminal_absence
            rejected = await handoff_terminal_absence(runtime, item_id)
            if rejected is not None:
                return rejected
            outcome, error = _terminal_companion(status, error_code)
            rejected = await settle_required(_settle_companion_runs(runtime, item_id))
            if rejected is not None:
                return CleanupStep("companion_rejected", rejected)
            del runtime.pending_queries[identity]
            runtime.consumed_queries.pop(item_id, None)
            return CleanupStep("already_terminal", diagnostic=pending.diagnostic)
        binding_blocks = pending.binding_failure is not None
        if binding_blocks and await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)):
            from camctl.persistence.repositories.outputs import _member_delete_dispatch_state
            dispatched, _unsettled = await _read_cleanup(runtime, lambda connection: _member_delete_dispatch_state(connection, item_id))
            binding_blocks = dispatched
        if (binding_blocks and (pending.saved_actual is None
                or _presence_observation(pending.saved_actual) is not False)):
            step = await _finish_binding_failure(runtime, item_id, canceled=await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)), binding_result=pending.binding_failure)
            if step is not None and step.phase in ("failed", "canceled"):
                del runtime.pending_queries[identity]
                runtime.consumed_queries.pop(item_id, None)
            return step
        canceled = (pending.consumer is CleanupQueryConsumer.CANCEL
                    or await _read_cleanup(runtime, lambda connection: _cancel_requested(connection, action_id)))
        if pending.query_unsupported and pending.saved_actual is None and not canceled:
            step = await _fail_unsupported(runtime, item_id, output_id)
            if step.phase in ("failed", "already_terminal"):
                del runtime.pending_queries[identity]
                runtime.consumed_queries.pop(item_id, None)
            return step
        if present is False:
            request = FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED, runtime.occurred_at())
        elif canceled:
            from camctl.persistence.repositories.outputs import (
                _member_delete_dispatch_state)
            dispatched, _unsettled = await _read_cleanup(runtime, lambda connection: _member_delete_dispatch_state(connection, item_id))
            if not dispatched:
                # 查询完成不证明本成员删除；本项可靠从未派发自己的删
                # 除时无删除错误取消并解除本项限制，不借用其他成员
                # 的删除生成收场错误。
                request = CancelCleanupItem(item_id, runtime.occurred_at())
            else:
                code = "file_delete_failed" if present else "delete_unconfirmed"
                request = CancelCleanupItem(
                    item_id, runtime.occurred_at(), code, {"output_id": str(output_id)})
        else:
            await _establish_query_retry_gate(runtime, item_id, present)
            record_consumption(runtime, pending, query=True)
            del runtime.pending_queries[identity]
            if present is None:
                return CleanupStep("query_unknown", diagnostic=pending.diagnostic)
            return (None if pending.consumer is CleanupQueryConsumer.BEFORE_DELETE and pending.diagnostic is None
                    else CleanupStep("still_present", diagnostic=pending.diagnostic))
        pending.member = PendingCleanupMember(request, new_operation_key())

    member = pending.member
    if isinstance(member.request, FinishCleanupItem):
        outcome, error = RunOutcome.SUCCEEDED, None
        phase, detail = "succeeded", "ABSENCE_CONFIRMED"
        save = runtime.outputs.finish_cleanup_item
        rejected_phase = "result_rejected"
    elif member.request.code is None:
        # 可靠未派发删除的解除取消：无删除错误，伴随流程按取消收场。
        outcome, error = RunOutcome.CANCELED, None
        phase, detail = "canceled", None
        save = runtime.outputs.cancel_cleanup_item
        rejected_phase = "cancel_rejected"
    else:
        code = member.request.code
        outcome = RunOutcome.FAILED if code == "file_delete_failed" else RunOutcome.UNCONFIRMED
        error = ErrorValue(code=code, stage="delete")
        phase, detail = "canceled", code
        save = runtime.outputs.cancel_cleanup_item
        rejected_phase = "cancel_rejected"
    if error is not None:
        record_error(replace(error, details=member.request.details or {}),
                     stage=error.stage, state=member.diagnostic_state,
                     context={"database_id": pending.database_id, "cleanup_item_id": item_id})
    if not member.saved:
        def apply_completed(saved):
            if (not isinstance(saved.value, CleanupItemSaved)
                    or saved.value.item_id != item_id):
                raise ConsistencyError("原查询的成员结果保存回执不对应")
            member.saved = True

        try:
            saved = await runtime.database.write(lambda owned: save(member.request, member.key, owned), key=member.key)
        except BaseException as error:
            actual = db_outcome_for(error)
            if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
                try:
                    apply_completed(actual)
                except BaseException as application_error:
                    raise error from application_error
            raise
        if saved.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep(rejected_phase, str(saved.error))
        apply_completed(saved)
    # ALREADY 也可能表示另一合法事务已经终态化成员。原预想结果
    # 不覆盖该事实；伴随流程始终沿实际已提交成员结果收场。
    actual_member = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status,error_code FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
    if actual_member is None or actual_member[0] not in _MEMBER_TERMINAL:
        raise ConsistencyError("成员保存回执缺少可靠终态")
    actual_outcome, actual_error = _terminal_companion(*actual_member)
    if actual_outcome is not outcome or actual_error != error:
        phase, detail = "already_terminal", None
    outcome, error = actual_outcome, actual_error
    rejected = await settle_required(_settle_companion_runs(runtime, item_id))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    del runtime.pending_queries[identity]
    runtime.consumed_queries.pop(item_id, None)
    return CleanupStep(phase, detail, pending.diagnostic)


async def _establish_query_retry_gate(runtime: CleanupRuntime, item_id: int, present: bool | None) -> None:
    """可靠观察已经交接且仍有普通重试资格时，开始本次间隔。"""
    key = f"exists/{item_id}" if present is None else f"delete/{item_id}"
    config = runtime.query_config if present is None else runtime.delete_config
    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT attempts_used,status FROM operation_runs WHERE responsibility_key=?', (key,)).fetchone())
    if row is not None and (row[1] not in (1, 2) or row[0] >= config.max_attempts):
        return
    if present is not None:
        runtime.retry_gate.cleared(f"exists/{item_id}")
    runtime.retry_gate.established(key, runtime.monotonic_ns())


def _prepare_query_finish(
        pending: PendingCleanupQuery, *, retry_wait: bool = True) -> AttemptFinish:
    """使用原证据登记解释完整返回，形成唯一的结束申请。"""
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
        Settlement, SettlementBasis)
    from camctl.operations.validation import validate_outcome

    actual = pending.actual
    present = _presence_observation(actual)
    outcome = actual.outcome or CallOutcome(
        status=AttemptStatus.SUCCEEDED if actual.error is None else AttemptStatus.FAILED,
        error=_legacy_error(actual, "query"),
        effect=EffectState.CONFIRMED if present is not None else EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type="file_presence", version=1, data={})),
        observations=actual.observations)
    return AttemptFinish(
        ticket=pending.ticket,
        outcome=validate_outcome(pending.ticket, outcome, pending.evidence),
        occurred_at=pending.occurred_at,
        retry_wait=(present is not False and retry_wait),
        recovery_max_event_id=pending.recovery_max_event_id)


def _matching_cleanup_intent(runtime, item_id, ticket, consumer):
    original = runtime.pending_intents.get(item_id)
    if original is not None and (
            original.database_id != runtime.database.metadata.instance_id or original.item_id != item_id
            or original.ticket != ticket or original.consumer is not consumer
            or original.request.operation != ticket.operation
            or original.request.target.cleanup_item_id != item_id):
        raise ConsistencyError("原调用返回与本会话意图票据或消费用途不对应")
    return original


async def _original_cleanup_config(runtime, item_id, ticket, original):
    if original is not None:
        return original.request.config
    from camctl.outputs.cleanup_results import cleanup_attempt_config
    return await runtime.database.read(lambda owned: cleanup_attempt_config(owned, ticket, database_id=runtime.database.metadata.instance_id, item_id=item_id))


async def _control_request(runtime: CleanupRuntime, item_id: int, operation: str, ticket):
    from camctl.devices.ports import ControlRequest

    if ticket.operation != operation:
        raise ConsistencyError("原文件调用与票据操作不对应")
    intent = runtime.pending_intents.get(item_id)
    original = _matching_cleanup_intent(runtime, item_id, ticket,
        intent.consumer if intent is not None and operation == "query" else None)
    config = await _original_cleanup_config(runtime, item_id, ticket, original)
    return ControlRequest(operation=operation, binding=await runtime.binding_of(item_id), params=await _read_cleanup(runtime, lambda connection: _target_params(connection, item_id)), ticket=ticket, timeout_s=config.timeout_s)


def _cleanup_dispatch_allowed(connection, item_id, ticket):
    """全部请求读取完成后，取得派发所需的最新持久事实。"""
    row = connection.execute(
        "SELECT action_id,status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    if row is None or row[1] in _MEMBER_TERMINAL:
        return False
    if not _cancel_requested(connection, row[0]):
        if ticket.operation == "delete":
            from camctl.persistence.repositories.outputs import _ProductReads, _cleanup_entry_ready
            reads = _ProductReads(connection, {})
            item = reads.required("cleanup_items", item_id)
            return _cleanup_entry_ready(reads, item)
        return True
    if ticket.operation == "delete":
        return False
    from camctl.persistence.repositories.outputs import _member_delete_dispatch_state
    from camctl.outputs.cleanup_results import cleanup_cancel_query_available

    dispatched, unsettled = _member_delete_dispatch_state(connection, item_id)
    return (dispatched and not unsettled
            and cleanup_cancel_query_available(connection, item_id, ticket=ticket))


async def _prepare_cleanup_call(runtime: CleanupRuntime, item_id: int, ticket, operation):
    """数据库读取先完成；派发标记后只等待原设备或文件调用。"""
    binding = await runtime.binding_of(item_id)
    if binding is None:
        if runtime.local_files is None:
            raise ConsistencyError(f"清理成员缺少可调用目标: {item_id}")
        request = await _local_artifact_request(runtime, item_id, ticket, operation)
        return runtime.local_files, request, True
    request = await _control_request(runtime, item_id, operation, ticket)
    return runtime.driver, request, False


async def _local_file_ref(runtime, item_id: int) -> FileRef:
    """从成员与保存的中间文件登记构造并纯校验 FileRef。"""
    from camctl.contracts.enums import enum_for
    from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
    from camctl.host_files.paths import resolve_file

    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT o.device_file_id,o.intermediate_file_id,f.id,f.purpose,f.relative_path FROM cleanup_items c JOIN outputs o ON o.id=c.output_id LEFT JOIN intermediate_files f ON f.id=o.intermediate_file_id WHERE c.id=?', (item_id,)).fetchone())
    if (row is None or row[0] is not None or row[1] is None
            or row[2] != row[1] or row[3] is None or row[4] is None):
        raise LocalArtifactTargetError(
            f"主机清理成员缺少对应的中间文件身份: {item_id}")
    purpose_values = enum_for("intermediate_files.purpose")
    try:
        purpose_name = purpose_values(int(row[3])).name
        purpose = FilePurpose[purpose_name]
    except (TypeError, ValueError, KeyError) as error:
        raise LocalArtifactTargetError(
            f"主机中间文件用途未登记: {row[3]!r}") from error
    root = runtime.local_file_root
    if root is None:
        root = getattr(runtime.local_files, "staging", None)
    # 注入的纯端口替身没有实际目录绑定；生产 HostArtifacts 始终由装配层绑定 root。
    root = Path(".").resolve() if root is None else Path(root)
    file_ref = FileRef(int(row[2]), purpose, str(row[4]), root)
    try:
        resolve_file(file_ref, BoundDirectories(root))
    except (TypeError, ValueError) as error:
        raise LocalArtifactTargetError(
            f"主机清理 FileRef 身份或路径不合法: {error}") from error
    return file_ref


async def _local_artifact_request(
        runtime, item_id: int, ticket: AttemptTicket,
        operation: str) -> LocalArtifactRequest:
    """从原成员和文件登记构造请求，并核对原票据与用途。"""
    if operation not in ("delete", "query"):
        raise ConsistencyError(f"主机文件操作未登记: {operation}")
    responsibility = f"delete/{item_id}" if operation == "delete" else f"exists/{item_id}"
    if (ticket.operation != operation or ticket.target_id != str(item_id)
            or ticket.responsibility_key != responsibility):
        raise ConsistencyError("主机文件请求与原清理票据不对应")
    file_ref = await _local_file_ref(runtime, item_id)
    return LocalArtifactRequest(
        database_id=runtime.database.metadata.instance_id,
        operation=operation, cleanup_item_id=item_id, ticket=ticket,
        file_ref=file_ref)


def _target_params(connection, item_id: int) -> dict:
    """删除/查询请求的目标身份与设备文件定位。

    驱动按成员身份回填观察、按设备文件身份定位目标；目标不是设
    备文件属于装配不一致，明确拒绝。
    """
    row = connection.execute(
        "SELECT f.id, f.identity_key, f.locator_json"
        " FROM cleanup_items c"
        " JOIN outputs o ON o.id = c.output_id"
        " JOIN device_files f ON f.id = o.device_file_id"
        " WHERE c.id = ?", (item_id,)).fetchone()
    if row is None:
        raise ConsistencyError(f"清理成员缺少设备文件目标: {item_id}")
    device_file_id, identity_key, locator_json = row
    locator = json.loads(locator_json) \
        if isinstance(locator_json, str) else {}
    return {
        "cleanup_item_id": str(item_id),
        "file_id": str(device_file_id),
        "identity_key": identity_key,
        "locator": locator,
    }


# ---- 会话推进：开始、目标固定、成员删除与汇总 ----


#: 清理成员的终态集合（成功、失败、取消）。
_MEMBER_TERMINAL = (4, 5, 6)

#: 推进结果中指示事务未完成或事实不一致的分区。
_MEMBER_ERROR_PHASES = (
    "missing_item", "restrict_rejected", "query_rejected",
    "progress_rejected", "result_rejected", "delete_budget_rejected",
    "query_budget_rejected", "finish_rejected", "fail_rejected",
    "cancel_rejected", "companion_rejected")



def _active_cleanup_call(runtime,item_id):
    original = runtime.pending_intents.get(item_id)
    if original is None or original.ticket is None:
        return False
    return (original.database_id == runtime.database.metadata.instance_id
        and original.dispatched and item_id not in runtime.pending_deletes
        and _pending_query_for_item(runtime,item_id) is None
        and (original.database_id,original.ticket.run_id,original.ticket.attempt_id) in runtime.active_calls)

async def advance_cleanup(runtime: CleanupRuntime) -> bool:
    """推进一个轮次的清理执行链；各步按已保存事实幂等。

    到时的待执行动作先开始，执行中的动作按目标集合状态推进：集
    合未固定先固定（范围来源未就绪保持等待），集合已固定逐成员
    推进删除，全部成员终态后保存汇总终态。
    """
    current_round = replace(runtime, round_token=object())
    try:
        return await _advance_cleanup_round(current_round)
    finally:
        runtime.last_recovery_diagnostic = current_round.last_recovery_diagnostic


async def _advance_cleanup_round(runtime: CleanupRuntime) -> bool:
    wakeup_required = False
    for item_id in tuple(runtime.consumed_queries):
        row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone())
        if row is None or row[0] in _MEMBER_TERMINAL:
            runtime.consumed_queries.pop(item_id,None)
    # 伴随责任的终态前提必须在本轮任何保存前成立。
    for item_id in runtime.pending_companions:
        await _companion_member_result(runtime, item_id)
    # 本地保存集合先于动作状态和当前设备目录；新工厂只接手数据。
    for (method, _target_id), pending in tuple(runtime.pending_saves.items()):
        if hasattr(pending.request, "action_id"):
            saved = await save_cleanup_request(runtime, method, pending.request, action=True)
            from camctl.persistence.models import DbOutcomeKind
            if saved.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"原清理动作申请续接未完成: {method} {saved.error}", saved)
            wakeup_required = True
    recovered = {pending.item_id for pending in runtime.pending_queries.values()}
    recovered.update(runtime.pending_companions)
    recovered.update(runtime.pending_intents)
    recovered.update(runtime.pending_deletes)
    recovered.update(pending.request.item_id for pending in runtime.pending_saves.values()
                     if hasattr(pending.request, "item_id"))
    local = replace(runtime, allow_new_calls=False)
    try:
        for item_id in sorted(recovered):
            if _active_cleanup_call(runtime,item_id):
                continue
            step = await delete_source_file(local, item_id)
            wakeup_required = True
            if step is not None and step.phase in _MEMBER_ERROR_PHASES:
                raise step.exception or ConsistencyError(
                    f"原清理结果续接未完成: {item_id} {step.phase} {step.detail}")
        from camctl.outputs.cleanup_discovery import cleanup_item_batches
        async for batch in cleanup_item_batches(runtime.database, batch_size=runtime.discovery_batch_size):
            for item_id in batch:
                if item_id in recovered or _active_cleanup_call(runtime,item_id):
                    continue
                step = await delete_source_file(local, item_id)
                wakeup_required = True
                if step is not None and step.phase in _MEMBER_ERROR_PHASES:
                    raise step.exception or ConsistencyError(
                        f"原清理结果续接未完成: {item_id} {step.phase} {step.detail}")
    finally:
        runtime.last_recovery_diagnostic = local.last_recovery_diagnostic
    now = runtime.occurred_at()
    wakeup_required |= await _start_due(runtime, now)
    async for action_id in _running_cleanup_actions(runtime):
        await _advance_cleanup_action(runtime, action_id, now)
        wakeup_required = True
    from camctl.outputs.cleanup_discovery import cleanup_item_batches
    async for batch in cleanup_item_batches(runtime.database, batch_size=runtime.discovery_batch_size):
        for item_id in batch:
            if handed_off_this_round(runtime, item_id) or _active_cleanup_call(runtime,item_id):
                continue
            step = await delete_source_file(runtime, item_id)
            wakeup_required = True
            if step is not None and step.phase in _MEMBER_ERROR_PHASES:
                raise step.exception or ConsistencyError(
                    f"原清理结果续接未完成: {item_id} {step.phase} {step.detail}")
    async for action_id in _running_cleanup_actions(runtime):
        await _advance_cleanup_action(runtime, action_id, now)
        wakeup_required = True
    return wakeup_required


async def _start_due(runtime: CleanupRuntime, now: int) -> bool:
    """开始到时的待执行清理动作；未到时与已取消动作不进入。"""
    from camctl.persistence.models import DbOutcomeKind

    after = (0, -1, 0)
    wakeup_required = False
    while True:
        rows = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT plan_id,input_index,id FROM actions WHERE status=1 AND cancel_requested=0 AND type=5 AND scheduled_at<=? AND (plan_id,input_index,id)>(?,?,?) ORDER BY plan_id,input_index,id LIMIT ?', (now, *after, runtime.discovery_batch_size)).fetchall())
        if not rows:
            return wakeup_required
        after = rows[-1]
        for _plan, _index, action_id in rows:
            outcome = await save_cleanup_request(runtime, 'start_cleanup_action', StartCleanupAction(action_id=action_id, occurred_at=now), action=True)
            if outcome.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(
                    f"清理开始事务未完成（{outcome.kind.value}）: {outcome.error}")
            wakeup_required |= outcome.value.disposition is CleanupStartDisposition.SAVED
            if outcome.value.disposition is CleanupStartDisposition.SAVED:
                record_event("cleanup.action", event="started", action_id=action_id,
                             database_id=runtime.database.metadata.instance_id)


async def _running_cleanup_actions(runtime: CleanupRuntime):
    """按原 plan/input 顺序分批发现动作，不缓存全部历史动作。"""
    after = (0, -1, 0)
    while True:
        rows = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT plan_id,input_index,id FROM actions WHERE status=2 AND type=5 AND (plan_id,input_index,id)>(?,?,?) ORDER BY plan_id,input_index,id LIMIT ?', (*after, runtime.discovery_batch_size)).fetchall())
        if not rows:
            return
        after = rows[-1]
        for _plan, _index, action_id in rows:
            yield action_id


async def _advance_cleanup_action(
        runtime: CleanupRuntime, action_id: int, now: int) -> None:
    """按已保存事实推进一个执行中的清理动作的下一个阶段。"""
    from camctl.persistence.models import DbOutcomeKind

    row = await _read_cleanup(runtime, lambda connection: connection.execute('SELECT target_selection_state, cancel_requested, source_resolution_state, input_fields_json FROM actions WHERE id = ?', (action_id,)).fetchone())
    if row is None:
        raise ConsistencyError(f"清理动作不存在: {action_id}")
    canceled = int(row[1]) == 1
    if row[0] == 1:
        # 目标集合待固定；已请求取消时不再建立新的普通执行责任，
        # 零成员目标由取消结算直接终态化。
        if canceled:
            return
        if row[2] == int(ResolutionState.PENDING):
            if not await _resolve_cleanup_sources(runtime, action_id, row[3], now):
                return
        # 范围来源未就绪或零产物收场时本动作结束本轮推进。
        if not await _fix_targets(runtime, action_id, now):
            return
    elif row[0] != 2:
        return
    unfinished = int((await _read_cleanup(runtime, lambda connection: connection.execute('SELECT COUNT(*) FROM cleanup_items WHERE action_id = ? AND status NOT IN (4, 5, 6)', (action_id,)).fetchone()))[0])
    if unfinished:
        return
    canceled_now = int((await _read_cleanup(runtime, lambda connection: connection.execute('SELECT cancel_requested FROM actions WHERE id = ?', (action_id,)).fetchone()))[0]) == 1
    if canceled or canceled_now:
        # 取消已请求（含本轮成员推进期间生效）：不保存普通汇总终
        # 态，动作终态化由取消结算保存，完成依据按取消语义判定。
        return
    outcome = await save_cleanup_request(runtime, 'finish_cleanup_action', FinishCleanupAction(action_id=action_id, occurred_at=now), action=True)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"清理汇总事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    if outcome.value.disposition is CleanupActionDisposition.SAVED:
        record_event("cleanup.action", action_id=action_id,
                     database_id=runtime.database.metadata.instance_id,
                     action_status=outcome.value.action_status)


async def _resolve_cleanup_sources(runtime: CleanupRuntime, action_id: int,
                             input_fields_json: str, now: int) -> bool:
    """先保存完整范围来源；取消及保存不确定时沿原申请处理。"""
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import ResolveSources

    outcome = await save_cleanup_request(runtime, 'resolve_sources', ResolveSources(action_id=action_id, spec=read_source_spec(input_fields_json), occurred_at=now), action=True)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"清理来源解析事务未完成（{outcome.kind.value}）: {outcome.error}")
    return outcome.value.fixed


async def _fix_targets(runtime: CleanupRuntime, action_id: int, now: int) -> bool:
    """固定目标集合；来源未就绪或零产物已收场时返回假。"""
    from camctl.persistence.models import DbOutcomeKind

    outcome = await save_cleanup_request(runtime, 'fix_cleanup_targets', FixCleanupTargets(action_id=action_id, occurred_at=now), action=True)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"清理目标固定事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    if outcome.value.disposition is CleanupTargetsDisposition.WAITING:
        return False
    if not outcome.value.item_ids:
        # 范围来源可靠确认无产物：空集合已同事务按成功收场。
        return False
    return True
