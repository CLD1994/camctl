"""D3 单次设备调用适配的组件集成测试。

真实受管本地工具经 O3 执行：期限、退出及分类按受接口约束的响
应源解释；本地退出与原始文本不直接生成设备成功事实。
"""

from __future__ import annotations

import sys
from decimal import Decimal

import pytest

from camctl.devices.adb_transport import (
    DeviceCommand,
    InterpretedFacts,
    invoke,
)
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.operations.models import (
    AttemptTicket,
    EffectState,
    ErrorValue,
)
from camctl.operations.process import RawToolOutcome, StopSignal, ToolSpec, execute_tool

pytestmark = pytest.mark.asyncio

_BINDING = DeviceBinding(device_id="cam0", driver_id="camctl-adb")
_RETURNED = EvidenceContract(
    type="operation_returned", version=1, operation="digest", fields=frozenset({"terminate_grace_s"})
)
_ASSUMPTION = EvidenceContract(
    type="adb_foreground_assumption", version=1, operation="digest", fields=frozenset({"terminate_grace_s"})
)
_DIGEST_OK = EvidenceContract(
    type="file_digest_obtained",
    version=1,
    operation="digest",
    fields=frozenset({"file_id", "sha256"}),
    identity_field="file_id",
)
_REGISTRY = EvidenceRegistry([_RETURNED, _ASSUMPTION, _DIGEST_OK])
_TICKET = AttemptTicket(
    attempt_id=1, operation="digest", target_id="5", responsibility_key="digest/5"
)


class _Never:
    async def requested(self) -> None:
        import asyncio

        await asyncio.Future()


class O3Transport:
    """以 O3 受管子进程为真实传输。"""

    async def run(self, spec: ToolSpec, stop: StopSignal) -> RawToolOutcome:
        return await execute_tool(spec, stop=stop)


class FixedFormatInterpreter:
    """受固定响应格式约束的解释者：只认登记标记，不解析任意文本。"""

    def interpret(self, raw: RawToolOutcome) -> InterpretedFacts:
        output = raw.output or b""
        for line in output.splitlines():
            if line.startswith(b"OK "):
                digest = line[3:].decode("ascii", "strict")
                return InterpretedFacts(
                    observations=(
                        DeviceObservation(
                            type="file_digest_obtained",
                            version=1,
                            data={"file_id": "5", "sha256": digest},
                        ),
                    ),
                    error=None,
                    effect=EffectState.CONFIRMED,
                )
            if line == b"REJECT":
                return InterpretedFacts(
                    observations=(),
                    error=ErrorValue(code="device_start_failed", stage="execution", details={}),
                    effect=EffectState.NO_EFFECT,
                )
        return InterpretedFacts(observations=(), error=None, effect=EffectState.UNKNOWN)


def _command(script: str, *, timeout_s: Decimal = Decimal("5")) -> DeviceCommand:
    return DeviceCommand(
        operation="digest",
        argv=(sys.executable, "-c", script),
        binding=_BINDING,
        timeout_s=timeout_s,
        terminate_grace_s=Decimal("1"),
        evidence=_REGISTRY,
        returned_contract=_RETURNED,
        assumption_contract=_ASSUMPTION,
        interpreter=FixedFormatInterpreter(),
        capability="digest",
    )


async def test_real_tool_confirms_digest() -> None:
    script = "print('OK ' + 'a' * 64)"
    outcome = await invoke(_command(script), _TICKET, O3Transport())
    assert outcome.effect is EffectState.CONFIRMED
    assert outcome.observations[0].data["sha256"] == "a" * 64
    assert outcome.call_info is not None
    assert outcome.call_info.local_exit_code == 0


async def test_real_tool_exit_alone_stays_unknown() -> None:
    script = "print('no registered marker')"
    outcome = await invoke(_command(script), _TICKET, O3Transport())
    assert outcome.status.value == "succeeded"
    assert outcome.effect is EffectState.UNKNOWN
    assert outcome.observations == ()


async def test_real_tool_rejection_is_failure() -> None:
    script = "print('REJECT'); raise SystemExit(2)"
    outcome = await invoke(_command(script), _TICKET, O3Transport())
    assert outcome.status.value == "failed"
    assert outcome.effect is EffectState.NO_EFFECT
    assert outcome.error is not None


async def test_real_tool_timeout_settles_by_assumption() -> None:
    script = "import time; time.sleep(30)"
    outcome = await invoke(
        _command(script, timeout_s=Decimal("0.1")), _TICKET, O3Transport()
    )
    assert outcome.status.value == "failed"
    assert outcome.settlement is not None
    assert outcome.settlement.evidence.type == "adb_foreground_assumption"
    assert outcome.effect is EffectState.UNKNOWN
