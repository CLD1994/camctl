"""取消目标、寻址结果与固定集合的公共类型。

四种有约束寻址（request_id、计划实例、动作实例、计划实例加组）由
`CancelTarget` 表达；可靠存在、可靠不存在与查询错误分别表达，不折
叠为同一空结果。自动预览联动候选由已保存关联提供，固定集合记录每
个目标的直接或联动依据及初始取消效果。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from camctl.contracts.values import ObjectId, UtcMicros

__all__ = [
    "ApplyCancelTarget",
    "CancelApplyMode",
    "CancelItemProgress",
    "CancelOutcomeChoice",
    "CancelProgress",
    "CancelStartDisposition",
    "CancelStartResult",
    "CancelTarget",
    "CancelTargetError",
    "CancelTargetsDisposition",
    "CancelTargetsSaved",
    "CancelActionDisposition",
    "CancelActionFinished",
    "CancelOriginFacts",
    "CancellationResult",
    "CancellationStatus",
    "OriginCancelDecision",
    "FailCancelTargets",
    "FinishCancelAction",
    "FixCancelTargets",
    "RecordCancelResult",
    "StartCancelAction",
    "StopWaitCancelItems",
    "CancellationEffect",
    "FixedCancelSet",
    "FixedTarget",
    "ResolvedTargets",
    "SelectionBasis",
    "TargetFacts",
    "TargetResolution",
]


@dataclass(frozen=True)
class CancelTarget:
    """一次取消的寻址输入：恰好采用四种有约束组合之一。

    request_id 取消该请求首次受理的整个计划；plan_instance_id 取消
    指定计划；action_instance_id 取消指定动作；plan_instance_id 与
    group 取消指定计划中该组的动作集合。
    """

    request_id: str | None = None
    plan_instance_id: int | None = None
    action_instance_id: int | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        combinations = {
            (self.request_id is not None),
            (self.plan_instance_id is not None),
            (self.action_instance_id is not None),
        }
        if self.group is not None and self.plan_instance_id is None:
            raise ValueError("组目标必须同时携带计划实例")
        if self.action_instance_id is not None and (
                self.plan_instance_id is not None or self.group is not None):
            raise ValueError("动作目标不与计划或组组合")
        if self.request_id is not None and (
                self.plan_instance_id is not None
                or self.action_instance_id is not None
                or self.group is not None):
            raise ValueError("请求目标不与其他寻址字段组合")
        if sum(combinations) == 0:
            raise ValueError("取消目标必须恰好采用一种寻址组合")
        for identity in (self.plan_instance_id, self.action_instance_id):
            if identity is not None:
                ObjectId(identity)
        if self.request_id is not None and (
                not isinstance(self.request_id, str) or not self.request_id):
            raise ValueError("请求目标必须是非空请求标识")
        if self.group is not None and (
                not isinstance(self.group, str) or not self.group):
            raise ValueError("组目标必须是非空组名")

    def as_fields(self) -> dict:
        """按原请求字段重建目标对象，用于不存在错误详情。"""
        fields: dict = {}
        if self.request_id is not None:
            fields["request_id"] = self.request_id
        if self.plan_instance_id is not None:
            fields["plan_instance_id"] = str(self.plan_instance_id)
        if self.action_instance_id is not None:
            fields["action_instance_id"] = str(self.action_instance_id)
        if self.group is not None:
            fields["group"] = self.group
        return fields


class CancelTargetError(Exception):
    """目标解析的终局错误；details 遵守公共错误登记。"""

    def __init__(self, code: str, details: Mapping) -> None:
        super().__init__(f"{code}: {dict(details)}")
        self.code = code
        self.details = dict(details)


@dataclass(frozen=True)
class TargetResolution:
    """一次寻址的可靠结果。

    missing 表示可靠确认目标不存在（登记 cancel_target_not_found）；
    lookup_failed 携带查询错误说明，不解释为不存在。二者与 action_
    ids 互斥：可靠存在时给出完整动作集合。
    """

    action_ids: tuple[int, ...] = ()
    missing: bool = False
    lookup_failed: str | None = None


@dataclass(frozen=True)
class TargetFacts:
    """一个直接目标在固定集合时需要的目标事实。

    terminal 表示目标动作已终态（联动与初始取消效果据此判断）；
    may_cancel 表示本次允许对该目标施加取消（由取消资格规则提供）。
    """

    action_id: int
    terminal: bool
    may_cancel: bool


@dataclass(frozen=True)
class ResolvedTargets:
    """自包含检查的输入：完整直接目标与自动关联候选。

    auto_candidates 每项为 (来源拍摄动作, 自动预览取回动作)；候选
    与直接目标可能重叠，固定集合时去重并标记两种依据同时成立。
    """

    direct: tuple[TargetFacts, ...]
    auto_candidates: tuple[tuple[int, int], ...] = ()


class SelectionBasis(Enum):
    """固定目标进入集合的依据（登记整数一致）。"""

    DIRECT = 1
    AUTO_PREVIEW = 2
    BOTH = 3


class CancellationEffect(Enum):
    """固定目标的初始取消效果（登记整数一致）。"""

    NOT_APPLIED = 1
    APPLIED = 2
    NOT_REQUIRED = 3


@dataclass(frozen=True)
class FixedTarget:
    """固定集合中的一个目标：依据与初始取消效果。"""

    action_id: int
    basis: SelectionBasis
    cancellation_effect: CancellationEffect


@dataclass(frozen=True)
class FixedCancelSet:
    """自包含检查通过后固定的实际处理集合。"""

    targets: tuple[FixedTarget, ...] = ()


@dataclass(frozen=True)
class FixCancelTargets:
    """一次取消目标固定的申请输入（TARGETS_FIXED.CANCEL）。"""

    action_id: int
    targets: FixedCancelSet
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        if not isinstance(self.targets, FixedCancelSet):
            raise TypeError("固定取消目标必须使用 FixedCancelSet")
        UtcMicros(self.occurred_at)


class CancelStartDisposition(Enum):
    """取消动作开始事务的结果分类。"""

    SAVED = "saved"
    #: 动作已终态或原键重送：只读恢复首次结果。
    ALREADY = "already"
    #: 取消请求已生效：不开始新的取消执行。
    REJECTED = "rejected"


@dataclass(frozen=True)
class CancelStartResult:
    """取消动作开始事务的保存结果。"""

    disposition: CancelStartDisposition
    reason: str | None = None


@dataclass(frozen=True)
class StartCancelAction:
    """一次取消动作开始执行的申请输入（ACTION_STARTED.START）。

    有限收场入口（时钟异常受限会话）对未排期取消动作先保存开始事
    实，再解析并固定目标集合；时间资格由调用入口判断。
    """

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CancelTargetsDisposition(Enum):
    """取消目标固定事务的结果分类。"""

    SAVED = "saved"
    #: 已固定集合或原键重送：只读复用首次结果。
    ALREADY = "already"


@dataclass(frozen=True)
class CancelTargetsSaved:
    """取消目标固定事务的保存结果；item_ids 按目标动作升序。"""

    disposition: CancelTargetsDisposition
    item_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class FailCancelTargets:
    """目标集合解析失败的申请输入（TARGETS_FIXED.FAIL）。

    仅接受 cancel_self_target 与 cancel_target_not_found；取消动作以
    登记错误结束，不创建任何取消成员。
    """

    action_id: int
    error: CancelTargetError
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        if not isinstance(self.error, CancelTargetError):
            raise TypeError("目标解析失败必须使用 CancelTargetError")
        if self.error.code not in ("cancel_self_target",
                                   "cancel_target_not_found"):
            raise ValueError(
                f"目标解析失败代码不在允许集合: {self.error.code!r}")
        UtcMicros(self.occurred_at)


class CancelApplyMode(Enum):
    """一次取消生效事务的目标处理方式（由取消资格决定）。"""

    #: 可靠未启动：PENDING 直接取消，RUNNING 标记后等待本地收场。
    PRE_START = "pre_start"
    #: 已启动或可能启动且支持停止：保存取消标记，停止收场另行推进。
    WITH_STOP = "with_stop"
    #: 目标已终态：不改写目标，本项按既有终态成功。
    TERMINAL = "terminal"
    #: 目标取消已生效：只保存本项效果，复用原取消责任收场。
    ALREADY = "already"


@dataclass(frozen=True)
class ApplyCancelTarget:
    """保存一个目标的取消生效（CANCEL_CHANGED.APPLY）。"""

    item_id: int
    mode: CancelApplyMode
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if not isinstance(self.mode, CancelApplyMode):
            raise TypeError(
                f"取消生效方式必须使用 CancelApplyMode: {self.mode!r}")
        UtcMicros(self.occurred_at)


class CancelOutcomeChoice(Enum):
    """取消项成功的完成依据（登记整数一致）。"""

    CANCELED = 1
    ALREADY_TERMINAL = 2


@dataclass(frozen=True)
class RecordCancelResult:
    """保存一个取消项的最终结果（CANCEL_CHANGED.RESULT）。

    outcome 与 code 恰好一个：成功携带完成依据，失败携带公共错误
    名称及详情。
    """

    item_id: int
    occurred_at: int
    outcome: CancelOutcomeChoice | None = None
    code: str | None = None
    details: dict | None = None

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        UtcMicros(self.occurred_at)
        if (self.outcome is None) == (self.code is None):
            raise ValueError("取消项结果必须携带完成依据或错误之一")
        if self.outcome is not None:
            if not isinstance(self.outcome, CancelOutcomeChoice):
                raise TypeError("完成依据必须使用 CancelOutcomeChoice")
            if self.details is not None:
                raise TypeError("取消项成功不携带错误详情")
        else:
            if not isinstance(self.code, str) or not self.code:
                raise ValueError("取消项失败必须使用公共错误名称")
            if not isinstance(self.details, dict):
                raise TypeError("取消项失败详情必须是对象")


@dataclass(frozen=True)
class FinishCancelAction:
    """取消动作汇总终态的申请输入：全部成员终态后保存动作结果。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CancelItemProgress:
    """一个取消项的当前进度快照；供汇总判定，不含目标自身状态。"""

    __slots__ = ("item_id", "target_action_id", "status", "outcome",
                 "error_code", "error_details_json")

    def __init__(self, *, item_id: int, target_action_id: int, status: int,
                 outcome: int | None = None, error_code: int | None = None,
                 error_details_json=None) -> None:
        self.item_id = item_id
        self.target_action_id = target_action_id
        self.status = status
        self.outcome = outcome
        self.error_code = error_code
        self.error_details_json = error_details_json


class CancelProgress:
    """一次取消的逐项进度快照。"""

    __slots__ = ("items",)

    def __init__(self, *, items) -> None:
        self.items = tuple(items)
        if not self.items:
            raise ValueError("取消进度必须包含至少一个目标项")
        for item in self.items:
            if not isinstance(item, CancelItemProgress):
                raise TypeError("取消进度项必须使用 CancelItemProgress")


class CancellationStatus(Enum):
    """取消动作的结果分类。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CancellationResult:
    """取消汇总判定结果；失败携带登记的机器错误对象。"""

    __slots__ = ("status", "succeeded", "failed", "error")

    def __init__(self, *, status: CancellationStatus, succeeded: int,
                 failed: int, error: dict | None = None) -> None:
        self.status = status
        self.succeeded = succeeded
        self.failed = failed
        self.error = error


class CancelActionDisposition(Enum):
    """取消动作汇总事务的结果分类。"""

    SAVED = "saved"
    #: 原键重送或动作已终态：只读复用首次结果。
    ALREADY = "already"


class CancelActionFinished:
    """取消动作汇总事务的保存结果。"""

    __slots__ = ("disposition", "action_status", "plan_status",
                 "succeeded", "failed")

    def __init__(self, *, disposition: CancelActionDisposition,
                 action_status: int, plan_status: int, succeeded: int,
                 failed: int) -> None:
        self.disposition = disposition
        self.action_status = action_status
        self.plan_status = plan_status
        self.succeeded = succeeded
        self.failed = failed


@dataclass(frozen=True)
class CancelOriginFacts:
    """取消发起者进度判定的输入事实。

    pending_transactions 表示目标取消事务、自身最终结果或自身取消
    的保存结果尚未确认；不可靠事实先核实，不猜测分支。
    """

    origin_terminal: bool
    origin_cancel_applied: bool
    pending_transactions: bool = False
    facts_reliable: bool = True


class OriginCancelDecision(Enum):
    """取消发起者的处理分支。"""

    #: 自身取消未生效且未终态：继续正常取消流程。
    CONTINUE = "continue"
    #: 已可靠保存终态：保留原终态，不重新取消或重开处理。
    KEEP_TERMINAL = "keep_terminal"
    #: 自身取消已生效：停止新增目标影响并结束等待，未结束项转入
    #: 取消收场，发起者以 canceled 结束；已生效目标独立继续。
    SETTLE_CANCELED = "settle_canceled"
    #: 相关事务尚未确认或事实不可靠：先核实实际结果。
    VERIFY_FIRST = "verify_first"


@dataclass(frozen=True)
class StopWaitCancelItems:
    """取消发起者结束等待的申请输入（CANCEL_CHANGED.STOP_WAIT）。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
