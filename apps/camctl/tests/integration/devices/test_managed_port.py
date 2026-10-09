"""生产设备端口与真实受管进程组合，保留期限和先取得的事实。"""

import sys
from decimal import Decimal

import pytest

from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.managed_port import ManagedDeviceDriver
from camctl.devices.ports import ControlRequest, DriverDeclaration
from camctl.operations.models import AttemptTicket, EffectState, SettlementBasis

from .test_transport import O3Transport, _BINDING


class Interpreter:
    def interpret(self, raw):
        confirmed = b"STARTED\n" in (raw.output or b"")
        return InterpretedFacts(
            (DeviceObservation("start_confirmed", 1, {"activity_id": "5"}),) if confirmed else (),
            None, EffectState.CONFIRMED if confirmed else EffectState.UNKNOWN)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_real_managed_timeout_keeps_original_result_and_earlier_confirmation(confirmed):
    returned = EvidenceContract("operation_returned", 1, "control", frozenset({"terminate_grace_s"}))
    assumption = EvidenceContract("adb_foreground_assumption", 1, "control", frozenset({"terminate_grace_s"}))
    evidence = EvidenceRegistry((returned, assumption,
        EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}), "activity_id")))
    script = ("print('STARTED', flush=True); " if confirmed else "") + "import time; time.sleep(30)"
    command = DeviceCommand("control", (sys.executable, "-c", script), _BINDING,
        Decimal("30"), Decimal("0.1"), evidence, returned, assumption, Interpreter(), "control")
    driver = ManagedDeviceDriver(declaration=DriverDeclaration(True, False, False, False, False, False, False),
        command_for=lambda request, batch: command, transport=O3Transport())
    ticket = AttemptTicket(1, "control", "5", "start/5", 1)

    result = await driver.control(ControlRequest("start_recording", _BINDING, {},
        ticket=ticket, timeout_s=Decimal("0.25")))

    assert result.error["code"] == "transport_timeout"
    assert result.outcome.effect is (EffectState.CONFIRMED if confirmed else EffectState.UNKNOWN)
    assert bool(result.observations) is confirmed
    assert result.outcome.settlement.basis is SettlementBasis.ASSUMED
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": Decimal("0.1")}
    assert result.outcome.call_info is not None
