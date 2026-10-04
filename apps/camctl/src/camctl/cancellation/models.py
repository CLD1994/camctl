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
    "CancelTarget",
    "CancelTargetError",
    "CancelTargetsDisposition",
    "CancelTargetsSaved",
    "FailCancelTargets",
    "FixCancelTargets",
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
