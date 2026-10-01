"""统一原子输入用例。

run 与 submit 共用同一入口：文件读取与解析在事务外完成，请求复
用、正文校验、独立 ACK 及诊断登记在仓储的同一事务内决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.ports import StaticActionCatalog
from camctl.contracts.values import OperationKey
from camctl.persistence.models import DbOutcome, DbOutcomeKind

__all__ = [
    "AcceptanceContext",
    "AcceptanceResult",
    "AckDisposition",
    "CommandMode",
    "PlanDisposition",
    "ProcessInput",
    "accept_input",
]


class CommandMode(Enum):
    RUN = "run"
    SUBMIT = "submit"


class PlanDisposition(Enum):
    REGISTERED = "registered"
    REUSED = "reused"
    REJECTED = "rejected"


class AckDisposition(Enum):
    ABSORBED = "absorbed"
    VALID_NOT_ADVANCING = "valid_not_advancing"
    INVALID = "invalid"
    NOT_PROVIDED = "not_provided"
    NOT_PROCESSED = "not_processed"


@dataclass(frozen=True)
class AcceptanceResult:
    """一次输入处理的完整提交结果。

    plan_disposition 为 REJECTED 时 plan_id 为空；整份拒绝与解析
    失败都以诊断记录保存。ack_watermark 是本次提交后的累计确认
    水位（无 ACK 处理时保持原值）。
    """

    plan_disposition: PlanDisposition
    plan_id: int | None
    ack_disposition: AckDisposition
    ack_watermark: int
    diagnostic_id: int | None = None


@dataclass(frozen=True)
class ProcessInput:
    """仓储命令的完整输入：解析结果或诊断、静态目录、命令模式及事件时间。"""

    source: ParsedInput | InputDiagnostic
    catalog: StaticActionCatalog
    mode: CommandMode
    occurred_at: int


class AcceptanceRepository(Protocol):
    """原子处理输入的端口：唯一事务内完成复用、注册与 ACK。"""

    def process_input(
        self, command: ProcessInput, key: OperationKey, owned
    ) -> DbOutcome[AcceptanceResult]: ...


@dataclass(frozen=True)
class AcceptanceContext:
    mode: CommandMode
    catalog: StaticActionCatalog
    repository: AcceptanceRepository
    clock: Any = None


class AcceptanceStateError(RuntimeError):
    """输入处理因状态库错误未完成；不输出任何已完成业务结果。"""


async def accept_input(
    source: ParsedInput | InputDiagnostic,
    context: AcceptanceContext,
    key: OperationKey,
    owned,
) -> AcceptanceResult:
    """处理一次输入并返回完整提交结果。

    业务拒绝是已提交结果；回滚、未知或连接失效按状态库错误上
    交，不解释为业务结果。事件时间取自上下文的时钟端口。
    """
    occurred_at = context.clock.utc_micros() if context.clock is not None else 0
    command = ProcessInput(
        source=source,
        catalog=context.catalog,
        mode=context.mode,
        occurred_at=occurred_at,
    )
    outcome = context.repository.process_input(command, key, owned)
    if outcome.kind is DbOutcomeKind.COMPLETED:
        return outcome.value
    raise AcceptanceStateError(
        f"输入处理未完成（{outcome.kind.value}）: {outcome.error}"
    )
