"""生产设备端口与真实受管进程组合，保留期限和先取得的事实。"""

import sys
from decimal import Decimal

import pytest

from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.managed_port import ManagedDeviceDriver
from camctl.devices.ports import ControlRequest, DriverDeclaration
from camctl.operations.models import AttemptTicket, EffectState, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome

from .test_transport import O3Transport, _BINDING


class Interpreter:
    def interpret(self, raw):
        confirmed = b"STARTED\n" in (raw.output or b"")
        return InterpretedFacts(
            (DeviceObservation("start_confirmed", 1, {"activity_id": "5"}),) if confirmed else (),
            None, EffectState.CONFIRMED if confirmed else EffectState.UNKNOWN)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.skipif(sys.platform == "win32",
                    reason="超时终止时已打印输出的保留依赖部署平台的进程组"
                           "软终止时序；Windows 树杀不保证该窗口，按部署边界在 Linux 验证")
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


@pytest.mark.asyncio
async def test_managed_file_query_preserves_query_ticket_and_target():
    presence = EvidenceContract(
        "file_presence", 1, "query", frozenset({"cleanup_item_id", "present"}),
        identity_field="cleanup_item_id")
    returned = EvidenceContract("query_returned", 1, "query", frozenset())
    assumption = EvidenceContract(
        "query_foreground_recovery", 1, "query", frozenset({"terminate_grace_s"}))
    evidence = EvidenceRegistry((presence, returned, assumption))
    observation = DeviceObservation(
        "file_presence", 1, {"cleanup_item_id": "3", "present": True})

    class QueryInterpreter:
        def interpret(self, raw):
            return InterpretedFacts((observation,), None, EffectState.CONFIRMED)

    command = DeviceCommand("query", ("true",), _BINDING, Decimal("9"), Decimal("1"),
        evidence, returned, assumption, QueryInterpreter(), "file_query")
    received = []

    class Transport:
        async def run(self, spec, stop):
            return RawToolOutcome(LocalExit(exit_code=0), b"", None, None, stderr=b"")

    driver = ManagedDeviceDriver(
        declaration=DriverDeclaration(False, False, False, False, False, False, False,
                                      file_query_supported=True),
        command_for=lambda request, batch: received.append(request) or command,
        transport=Transport())
    ticket = AttemptTicket(1, "query", "3", "exists/3", 12)
    request = ControlRequest(
        operation="query", binding=_BINDING, params={"cleanup_item_id": "3"},
        ticket=ticket, timeout_s=Decimal("5"))

    result = await driver.query_file(request)

    assert received == [request]
    assert received[0].ticket is ticket
    assert received[0].ticket.target_id == "3"
    assert received[0].ticket.responsibility_key == "exists/3"
    assert result.observations == (observation,)
