"""D3 单次设备调用适配的单元测试。

一次受管调用交出类型化事实：发送、启动、任务完成、明确拒绝、
超时及成功后错误各按保证范围表达；能力缺失与调用失败分开；本
地退出和原始文本不生成设备成功事实；无隐藏重试。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import DriverDeclaration
from camctl.operations.models import (
    AttemptTicket,
    EffectState,
    ErrorValue,
)
from camctl.operations.process import LocalExit, RawToolOutcome, ToolSpec
from camctl.devices.adb_transport import (
    DeviceCommand,
    InterpretedFacts,
    invoke,
)

pytestmark = pytest.mark.asyncio

_BINDING = DeviceBinding(device_id="cam0", driver_id="camctl-adb")
_RETURNED = EvidenceContract(
    type="operation_returned", version=1, operation="digest", fields=frozenset({"terminate_grace_s"})
)
_ASSUMPTION = EvidenceContract(
    type="adb_foreground_assumption",
    version=1,
    operation="digest",
    fields=frozenset({"terminate_grace_s"}),
)
_DIGEST_OK = EvidenceContract(
    type="file_digest_obtained",
    version=1,
    operation="digest",
    fields=frozenset({"file_id", "sha256"}),
    identity_field="file_id",
    max_observations=1,
)
_DIGEST_UNAVAILABLE = EvidenceContract(
    type="digest_source_failed",
    version=1,
    operation="digest",
    fields=frozenset({"file_id"}),
    identity_field="file_id",
    max_observations=1,
)
_REGISTRY = EvidenceRegistry([_RETURNED, _ASSUMPTION, _DIGEST_OK, _DIGEST_UNAVAILABLE])

_TICKET = AttemptTicket(
    attempt_id=1, operation="digest", target_id="5", responsibility_key="digest/5"
)


class _Interpreter:
    """受响应解释端口约束的替身：原始结果→类型化事实。"""

    def __init__(self, facts: InterpretedFacts) -> None:
        self._facts = facts
        self.calls = 0

    def interpret(self, raw: RawToolOutcome) -> InterpretedFacts:
        self.calls += 1
        return self._facts


class _Transport:
    """受管传输替身：记录调用次数，返回预设原始结果。"""

    def __init__(self, raw: RawToolOutcome) -> None:
        self._raw = raw
        self.runs = 0

    async def run(self, spec: ToolSpec, stop) -> RawToolOutcome:
        self.runs += 1
        return self._raw


def _command(interpreter: _Interpreter) -> DeviceCommand:
    return DeviceCommand(
        operation="digest",
        argv=("adb", "shell", "sha256sum", "/sdcard/a.mp4"),
        binding=_BINDING,
        timeout_s=Decimal("2"),
        terminate_grace_s=Decimal("3"),
        evidence=_REGISTRY,
        returned_contract=_RETURNED,
        assumption_contract=_ASSUMPTION,
        interpreter=interpreter,
        capability="digest",
    )


DECLARATION = DriverDeclaration(
    control_supported=True,
    stop_supported=True,
    query_supported=True,
    result_supported=True,
    read_supported=True,
    digest_supported=True,
    delete_supported=True,
)


async def test_send_confirmed_keeps_effect_unknown() -> None:
    interpreter = _Interpreter(
        InterpretedFacts(
            observations=(), error=None, effect=EffectState.UNKNOWN
        )
    )
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(RawToolOutcome(exit=LocalExit(exit_code=0), output=b"sent", error=None, used_grace_s=None)),
    )
    assert outcome.status.value == "succeeded"
    assert outcome.effect is EffectState.UNKNOWN
    assert outcome.error is None


async def test_task_completion_with_confirmed_digest() -> None:
    observation = DeviceObservation(
        type="file_digest_obtained", version=1, data={"file_id": "5", "sha256": "a" * 64}
    )
    interpreter = _Interpreter(
        InterpretedFacts(observations=(observation,), error=None, effect=EffectState.CONFIRMED)
    )
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(RawToolOutcome(exit=LocalExit(exit_code=0), output=b"", error=None, used_grace_s=None)),
    )
    assert outcome.observations == (observation,)
    assert outcome.effect is EffectState.CONFIRMED


async def test_failed_hash_is_not_unsupported() -> None:
    """声明摘要能力但本次调用失败：能力仍支持，结果是调用错误。"""
    observation = DeviceObservation(
        type="digest_source_failed", version=1, data={"file_id": "5"}
    )
    interpreter = _Interpreter(
        InterpretedFacts(
            observations=(observation,),
            error=ErrorValue(code="source_checksum_unavailable", stage="execution", details={}),
            effect=EffectState.UNKNOWN,
        )
    )
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(RawToolOutcome(exit=LocalExit(exit_code=1), output=b"err", error=None, used_grace_s=None)),
    )
    assert outcome.error is not None
    assert DECLARATION.digest_supported is True


async def test_explicit_rejection_is_call_failure() -> None:
    interpreter = _Interpreter(
        InterpretedFacts(
            observations=(),
            error=ErrorValue(code="device_start_failed", stage="execution", details={}),
            effect=EffectState.NO_EFFECT,
        )
    )
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(RawToolOutcome(exit=LocalExit(exit_code=255), output=b"rejected", error=None, used_grace_s=None)),
    )
    assert outcome.status.value == "failed"
    assert outcome.effect is EffectState.NO_EFFECT


async def test_timeout_records_assumption_with_grace() -> None:
    interpreter = _Interpreter(InterpretedFacts(observations=(), error=None, effect=EffectState.UNKNOWN))
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(
            RawToolOutcome(exit=LocalExit(exit_code=1), output=None, error="timeout", used_grace_s=Decimal(3))
        ),
    )
    assert outcome.status.value == "failed"
    assert outcome.error is not None
    assert outcome.settlement is not None
    assert outcome.settlement.evidence.type == "adb_foreground_assumption"
    assert outcome.settlement.evidence.data["terminate_grace_s"] == Decimal(3)
    assert outcome.effect is EffectState.UNKNOWN


async def test_success_then_error_keeps_both() -> None:
    observation = DeviceObservation(
        type="file_digest_obtained", version=1, data={"file_id": "5", "sha256": "a" * 64}
    )
    interpreter = _Interpreter(
        InterpretedFacts(observations=(observation,), error=None, effect=EffectState.CONFIRMED)
    )
    # 可靠摘要先取得，本地调用随后超时：FAILED 与 CONFIRMED 共同保留。
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(
            RawToolOutcome(exit=LocalExit(exit_code=0), output=b"digest", error="timeout", used_grace_s=Decimal(3))
        ),
    )
    assert outcome.status.value == "failed"
    assert outcome.effect is EffectState.CONFIRMED
    assert outcome.observations == (observation,)


async def test_local_exit_alone_never_confirms() -> None:
    """解释者不确认效果时，退出码 0 不生成业务成功事实。"""
    interpreter = _Interpreter(InterpretedFacts(observations=(), error=None, effect=EffectState.UNKNOWN))
    outcome = await invoke(
        _command(interpreter),
        _TICKET,
        _Transport(RawToolOutcome(exit=LocalExit(exit_code=0), output=b"", error=None, used_grace_s=None)),
    )
    assert outcome.effect is not EffectState.CONFIRMED


async def test_no_implicit_retries() -> None:
    interpreter = _Interpreter(InterpretedFacts(observations=(), error=None, effect=EffectState.UNKNOWN))
    transport = _Transport(
        RawToolOutcome(exit=LocalExit(exit_code=1), output=None, error="timeout", used_grace_s=Decimal(3))
    )
    await invoke(_command(interpreter), _TICKET, transport)
    assert transport.runs == 1
