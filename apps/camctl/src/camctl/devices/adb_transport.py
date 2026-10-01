"""一次受管设备调用与类型化事实适配。

调用一次驱动已确认的明确操作并交出 CallOutcome：业务预算、重试
及动作终态留给流程层。共享 ADB 服务端启动等待包含在本次调用期
限内；不在恢复服务端后暗自重发业务命令，也无任何隐藏重试。本
地退出和原始文本不直接生成设备成功事实——效果与观察一律来自
受端口约束的响应解释者。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallInfo,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.process import (
    LocalExit,
    RawToolOutcome,
    StopSignal,
    ToolSpec,
)
from camctl.operations.validation import validate_outcome

__all__ = [
    "DeviceCommand",
    "InterpretedFacts",
    "ManagedTransport",
    "ResponseInterpreter",
    "invoke",
]


@dataclass(frozen=True)
class InterpretedFacts:
    """响应解释者从原始调用结果提取的类型化事实。"""

    observations: tuple[DeviceObservation, ...]
    error: ErrorValue | None
    effect: EffectState


class ResponseInterpreter(Protocol):
    """受接口约束的响应源：原始输出→已登记的类型化事实。"""

    def interpret(self, raw: RawToolOutcome) -> InterpretedFacts: ...


class ManagedTransport(Protocol):
    """O3 受管工具调用的传输端口：每次恰好执行一次。"""

    async def run(self, spec: ToolSpec, stop: StopSignal) -> RawToolOutcome: ...


@dataclass(frozen=True)
class DeviceCommand:
    """具体驱动已确认的单次设备操作。

    interpreter 按该驱动的已核验响应契约把原始结果解释为登记事
    实；returned/assumption 契约给出本次调用责任的结束依据类型。
    """

    operation: str
    argv: tuple[str, ...]
    binding: DeviceBinding
    timeout_s: Decimal
    terminate_grace_s: Decimal
    evidence: EvidenceRegistry
    returned_contract: EvidenceContract
    assumption_contract: EvidenceContract
    interpreter: ResponseInterpreter
    capability: str


class _NeverStop:
    async def requested(self) -> None:
        import asyncio

        await asyncio.Future()


async def invoke(
    command: DeviceCommand,
    call: AttemptTicket,
    transport: ManagedTransport,
    stop: StopSignal | None = None,
) -> CallOutcome:
    """执行一次设备调用并组装正式结束结果。

    单次执行、无隐藏重试；超时或取消经声明假设收场，其余经实际
    返回收场。组装结果先通过统一结果校验再交出，保证各分区不
    变量成立。
    """
    spec = ToolSpec(
        argv=command.argv,
        timeout_s=command.timeout_s,
        terminate_grace_s=command.terminate_grace_s,
    )
    raw = await transport.run(spec, stop=stop or _NeverStop())
    facts = command.interpreter.interpret(raw)

    transport_error = (
        ErrorValue(code=f"transport_{raw.error}", stage="transport", details={})
        if raw.error is not None
        else None
    )
    call_error = facts.error or transport_error
    status = AttemptStatus.FAILED if call_error is not None else AttemptStatus.SUCCEEDED
    assumed = raw.error is not None
    contract = command.assumption_contract if assumed else command.returned_contract
    data: dict[str, Decimal] = (
        {"terminate_grace_s": raw.used_grace_s}
        if raw.used_grace_s is not None
        else {}
    )
    outcome = CallOutcome(
        status=status,
        error=call_error,
        effect=facts.effect,
        settlement=Settlement(
            basis=SettlementBasis.ASSUMED
            if assumed
            else SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type=contract.type, version=contract.version, data=data
            ),
        ),
        observations=facts.observations,
        call_info=_call_info(raw),
    )
    validate_outcome(call, outcome, command.evidence)
    return outcome


def _call_info(raw: RawToolOutcome) -> CallInfo | None:
    if raw.exit is None:
        return None
    if raw.exit.exit_code is not None:
        return CallInfo(local_exit_code=raw.exit.exit_code)
    return CallInfo(local_signal=raw.exit.signal)
