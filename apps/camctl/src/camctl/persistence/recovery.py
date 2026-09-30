"""提交结果未知后的按操作身份核实。

核实只依据可靠连接上查到的持久化事实：内存中分配的事件编号不
作为提交证明，等待者取消不更换操作身份。决策不自动重做任何操
作，重做资格由调用方按原业务规则另行判断。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Generic, Mapping, Protocol, TypeVar

from camctl.contracts.values import OperationKey

T = TypeVar("T")


class RecoveryError(ValueError):
    """核实依据与操作身份矛盾。"""


class CommitFindingKind(enum.Enum):
    """一次核实查询在可靠连接上得到的事实分区。"""

    PRESENT = "present"
    ABSENT = "absent"
    IN_FLIGHT = "in_flight"
    LOOKUP_FAILED = "lookup_failed"


@dataclass(frozen=True)
class OperationIdentity:
    """一次具体保存责任的核对依据：输入、阶段和目标。"""

    inputs: Mapping[str, Any]
    stage: str
    target: str


@dataclass(frozen=True)
class CommitFinding(Generic[T]):
    """按操作身份查询到的提交事实。

    PRESENT 携带已提交结果与保存时的身份；ABSENT 表示可靠不存
    在且原事务已不可能迟到提交（失效连接已停用）；IN_FLIGHT 表
    示暂时不存在但原事务仍可能提交；LOOKUP_FAILED 保留查询错误。
    """

    kind: CommitFindingKind
    value: T | None = None
    identity: OperationIdentity | None = None
    error: BaseException | None = None


class CommitLookup(Protocol[T]):
    """在新可靠连接上按操作身份核实提交结果的端口。"""

    def lookup(self, key: OperationKey) -> CommitFinding[T]: ...


class RecoveryKind(enum.Enum):
    """核实决策的四个唯一分区。"""

    REUSE = "reuse"
    RETRY = "retry"
    WAIT = "wait"
    ERROR = "error"


@dataclass(frozen=True)
class RecoveryDecision(Generic[T]):
    """一次提交未知的核实决策。

    REUSE 复用原已提交结果；RETRY 表示可靠确认未保存且原事务不
    可能迟到，允许按原资格考虑重做；WAIT 保留未知（可能在途或查
    询失败），不能重做；ERROR 表示已保存事实与操作身份矛盾。
    """

    kind: RecoveryKind
    value: T | None = None
    error: BaseException | None = None

    @property
    def can_retry(self) -> bool:
        return self.kind is RecoveryKind.RETRY


def resolve_commit(
    key: OperationKey,
    expected: OperationIdentity,
    lookup: CommitLookup[T],
) -> RecoveryDecision[T]:
    """按稳定操作身份核实一次结果未知的提交。

    各核实分区有唯一结果：错误和未知不解释为空，也不折叠为可
    重做；本函数不产生任何副作用，不自动再次调用原操作。
    """
    finding = lookup.lookup(key)
    if finding.kind is CommitFindingKind.LOOKUP_FAILED:
        return RecoveryDecision(kind=RecoveryKind.WAIT, error=finding.error)
    if finding.kind is CommitFindingKind.IN_FLIGHT:
        return RecoveryDecision(kind=RecoveryKind.WAIT)
    if finding.kind is CommitFindingKind.ABSENT:
        return RecoveryDecision(kind=RecoveryKind.RETRY)
    # PRESENT：核对输入、阶段和目标与本次操作身份一致。
    saved = finding.identity
    if saved is None:
        raise RecoveryError(f"操作 {key} 的核实结果缺少已保存身份")
    if (
        saved.inputs != expected.inputs
        or saved.stage != expected.stage
        or saved.target != expected.target
    ):
        return RecoveryDecision(
            kind=RecoveryKind.ERROR,
            error=RecoveryError(
                f"操作 {key} 已保存的身份与本次核实的身份不一致"
            ),
        )
    return RecoveryDecision(kind=RecoveryKind.REUSE, value=finding.value)
