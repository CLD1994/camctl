"""驱动端口使用本次期限，并原样交付受管调用的正式结果。"""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts, ManagedTransport, ResponseInterpreter
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.process import LocalExit, RawToolOutcome


_BINDING = DeviceBinding("cam-1", "driver-1")
_TICKET = AttemptTicket(1, "control", "12", "start/12", 1)
_RETURNED = EvidenceContract("operation_returned", 1, "control", frozenset())
_ASSUMPTION = EvidenceContract("adb_foreground_assumption", 1, "control", frozenset({"terminate_grace_s"}))
_DECLARATION = DriverDeclaration(True, True, True, True, False, False, True)


def _outcome():
    return CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue("transport_timeout", "transport", {"response_error": {"code": "rejected"}}),
        effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.ASSUMED,
            EvidenceValue("adb_foreground_assumption", 1, {"terminate_grace_s": Decimal("0.25")})),
        observations=(), call_info=CallInfo(local_exit_code=7))


def test_port_result_preserves_complete_managed_outcome():
    outcome = _outcome()

    result = DeviceCallResult.from_outcome(outcome)

    assert result.outcome is outcome
    assert result.observations == ()
    assert result.error == {
        "code": "transport_timeout", "stage": "transport",
        "details": {"response_error": {"code": "rejected"}},
    }


def test_port_result_rejects_two_different_observation_sources():
    outcome = _outcome()
    result = DeviceCallResult.from_outcome(outcome)

    with pytest.raises(ValueError):
        replace(result, error={"code": "other_error"})


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [Decimal("0.125"), Decimal("2.75")])
async def test_current_timeout_reaches_managed_transport_without_retry(timeout):
    from camctl.devices.managed_port import ManagedDeviceDriver

    interpreter = create_autospec(ResponseInterpreter, instance=True)
    interpreter.interpret.return_value = InterpretedFacts((), None, EffectState.UNKNOWN)
    command = DeviceCommand(
        "control", ("device-tool", "start"), _BINDING, Decimal("99"), Decimal("0.25"),
        EvidenceRegistry((_RETURNED, _ASSUMPTION)), _RETURNED, _ASSUMPTION,
        interpreter, "control")
    transport = create_autospec(ManagedTransport, instance=True)
    transport.run.return_value = RawToolOutcome(LocalExit(exit_code=7), b"partial", "timeout", Decimal("0.25"))
    driver = ManagedDeviceDriver(
        declaration=_DECLARATION, command_for=lambda request, batch: command, transport=transport)
    request = ControlRequest("start_recording", _BINDING, {}, ticket=_TICKET, timeout_s=timeout)

    result = await driver.control(request)

    assert transport.run.await_count == 1
    assert transport.run.call_args.args[0].timeout_s == timeout
    assert result.outcome.settlement.basis is SettlementBasis.ASSUMED
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": Decimal("0.25")}
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.call_info.local_exit_code == 7
    assert result.error["code"] == "transport_timeout"


@pytest.mark.asyncio
async def test_managed_port_rejects_another_saved_binding_before_transport():
    from camctl.devices.managed_port import ManagedDeviceDriver

    interpreter = create_autospec(ResponseInterpreter, instance=True)
    command = DeviceCommand(
        "control", ("device-tool", "start"), DeviceBinding("other", "driver-1"),
        Decimal("99"), Decimal("0.25"), EvidenceRegistry((_RETURNED, _ASSUMPTION)),
        _RETURNED, _ASSUMPTION, interpreter, "control")
    transport = create_autospec(ManagedTransport, instance=True)
    driver = ManagedDeviceDriver(
        declaration=_DECLARATION, command_for=lambda request, batch: command, transport=transport)

    with pytest.raises(ValueError):
        await driver.control(ControlRequest("start_recording", _BINDING, {},
            ticket=_TICKET, timeout_s=Decimal("1")))

    transport.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_port_requires_original_attempt_identity():
    from camctl.devices.managed_port import ManagedDeviceDriver

    transport = create_autospec(ManagedTransport, instance=True)
    factory = create_autospec(lambda request, batch: None)
    driver = ManagedDeviceDriver(declaration=_DECLARATION, command_for=factory, transport=transport)

    with pytest.raises(ValueError):
        await driver.control(ControlRequest("start_recording", _BINDING, {}, timeout_s=Decimal("1")))

    factory.assert_not_called()
    transport.run.assert_not_awaited()
