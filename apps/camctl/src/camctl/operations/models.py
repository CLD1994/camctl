"""统一尝试意图及结束结果模型。

结果外层 format_version=1：调用状态、错误、效果分类、收场依据、
观察及已知调用信息按正式结构表达。本地退出不推出远端退出或业务
成功；可靠效果可与调用错误并存。AttemptTicket 的完整持久化字段
由 O2 落地，本模块固定调用方核对所需的身份与操作上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from camctl.devices.evidence import DeviceObservation, EvidenceContract

__all__ = [
    "RESULT_FORMAT_VERSION",
    "AttemptStatus",
    "AttemptTicket",
    "CallInfo",
    "CallOutcome",
    "EffectState",
    "ErrorValue",
    "EvidenceValue",
    "Settlement",
    "SettlementBasis",
    "ValidatedOutcome",
]

#: 尝试结果外层的解释规则版本；不代替历史事件版本。
RESULT_FORMAT_VERSION = 1


class AttemptStatus(Enum):
    """一次尝试的结束状态；运行中不携带结束结果。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EffectState(Enum):
    """本次尝试的业务效果分类。"""

    CONFIRMED = "confirmed"
    NO_EFFECT = "no_effect"
    UNKNOWN = "unknown"


class SettlementBasis(Enum):
    """本次调用满足结束条件的三种依据。"""

    OBSERVED = "observed"
    ASSUMED = "assumed"
    NOT_DISPATCHED = "not_dispatched"


@dataclass(frozen=True)
class ErrorValue:
    """公共错误对象：登记编码、阶段及开放详情。"""

    code: str
    stage: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceValue:
    """类型化结束依据：类型、版本及符合该版本结构的正文。"""

    type: str
    version: int
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, contract: EvidenceContract) -> "EvidenceValue":
        """按登记契约构造空正文依据（未使用宽限时正文为空）。"""
        return cls(type=contract.type, version=contract.version, data={})


@dataclass(frozen=True)
class Settlement:
    """收场依据：basis 与类型化证据共同说明为何结束成立。"""

    basis: SettlementBasis
    evidence: EvidenceValue


@dataclass(frozen=True)
class CallInfo:
    """实际取得的调用信息：本地退出与远端退出分别表达。"""

    local_exit_code: int | None = None
    local_signal: int | None = None
    remote_exit_code: int | None = None

    def __post_init__(self) -> None:
        provided = [
            value
            for value in (self.local_exit_code, self.local_signal, self.remote_exit_code)
            if value is not None
        ]
        if not provided:
            raise ValueError("call_info 至少携带一种已知调用信息")
        # 本地正常退出码与本地终止信号互斥；远端退出独立表达。
        if (
            self.local_exit_code is not None
            and self.local_signal is not None
        ):
            raise ValueError("本地退出码与终止信号只能提供其一")
        if self.remote_exit_code is not None and not (
            0 <= self.remote_exit_code <= 255
        ):
            raise ValueError(f"远端退出码超出协议范围 0~255: {self.remote_exit_code}")


@dataclass(frozen=True)
class CallOutcome:
    """一次尝试的完整结束结果（正式结构）。"""

    format_version: int = RESULT_FORMAT_VERSION
    status: AttemptStatus = AttemptStatus.SUCCEEDED
    error: ErrorValue | None = None
    effect: EffectState = EffectState.UNKNOWN
    settlement: Settlement | None = None
    observations: tuple[DeviceObservation, ...] = ()
    call_info: CallInfo | None = None

    def __post_init__(self) -> None:
        if self.format_version != RESULT_FORMAT_VERSION:
            raise ValueError(f"结果外层版本不支持: {self.format_version!r}")
        if not isinstance(self.observations, tuple):
            raise ValueError("观察必须是元组；空观察与 NULL 分别处理")


@dataclass(frozen=True)
class ValidatedOutcome:
    """通过证据登记与结果分区校验的结束结果。"""

    outcome: CallOutcome
    settlement_contract: EvidenceContract
    observation_contracts: tuple[EvidenceContract, ...]


@dataclass(frozen=True)
class AttemptTicket:
    """一次已提交尝试的身份与操作上下文。

    attempt_id 为本次流程内连续编号；operation 是 D2 登记的操作类
    别；target_id 是观察身份核对的规范十进制目标；responsibility_
    key 沿原流程责任键。原流程、活动或文件引用及实际配置由 O2
    持久化时补齐，本字段集供结果核对使用。
    """

    attempt_id: int
    operation: str
    target_id: str | None
    responsibility_key: str
