"""受管文件查询保留完整请求票据的纯局部契约。"""
from decimal import Decimal

import pytest

from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.managed_port import ManagedDeviceDriver
from camctl.devices.ports import ControlRequest, DriverDeclaration
from camctl.operations.models import AttemptTicket, EffectState
from camctl.operations.process import LocalExit, RawToolOutcome

_BINDING = DeviceBinding("cam-1", "contract-double")


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
