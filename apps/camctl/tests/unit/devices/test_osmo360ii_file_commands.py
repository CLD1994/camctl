"""真实 OSMO 合同的文件端口、原身份与受管调用接线。"""

from dataclasses import replace
from decimal import Decimal
import json
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome


BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")
PATH = "/mnt/media_rw/emulated/DCIM/empty.OSV"


def _request(operation, path=PATH):
    return ControlRequest(operation, BINDING, {
        "cleanup_item_id": "7", "identity_key": json.dumps([BINDING.device_id, BINDING.driver_id, path]),
        "locator": {"path": path}}, AttemptTicket(1, operation, "7", "cleanup/7", 2), Decimal("3"))


def _driver(contract, channel):
    return AdbCameraDriver(contract,
        {"cam-1": {"driver": BINDING.driver_id, "adb": {"serial": "serial-1"}}}, channel,
        terminate_grace_s=Decimal("2"), monotonic_ns=lambda: 0)


class Channel:
    def __init__(self, operation="query", present=True, path=PATH):
        self.operation, self.present, self.path = operation, present, path
        self.calls = []

    async def run(self, spec, stop):
        self.calls.append(spec)
        return RawToolOutcome(LocalExit(exit_code=0), b"\0".join((
            b"CAMCTL-FILE/1", self.operation.encode(), self.path.encode(),
            b"1" if self.present else b"0", b"0" if self.operation == "delete" else b"-",
            b"0", b"END", b"")), None, None)


def test_formal_osmo_contract_supplies_file_factories():
    contract = osmo360ii_contract()
    assert callable(contract.commands.get(CameraCall.FILE_QUERY))
    assert callable(contract.commands.get(CameraCall.DELETE))


@pytest.mark.asyncio
async def test_formal_osmo_query_file_returns_original_presence():
    result = await _driver(osmo360ii_contract(), Channel()).query_file(_request("query"))
    assert result.error is None
    assert result.observations[0].data == {"cleanup_item_id": "7", "present": True}


@pytest.mark.asyncio
async def test_formal_delete_dispatches_one_complete_command_with_original_evidence():
    contract = osmo360ii_contract()
    request = _request("delete")
    channel = Channel("delete", False)
    driver = _driver(contract, channel)
    result = await driver.delete(request)
    assert len(channel.calls) == 1
    assert channel.calls[0].argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
    script = shlex.split(channel.calls[0].argv[5])[2]
    assert script == contract.file_tools.file_presence_script(PATH, delete=True)
    assert channel.calls[0].timeout_s == Decimal("3")
    assert result.error is None
    assert result.observations[0].type == "file_delete_presence"
    assert result.observations[0].data == {"cleanup_item_id": "7", "present": False}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    for kind, operation in ((CameraCall.FILE_QUERY, "query"), (CameraCall.DELETE, "delete")):
        command = contract.commands[kind](_request(operation), "serial-1", None, None)
        assert command.evidence is contract.evidence
        assert command.returned_contract is contract.evidence.contract(operation + "_returned", 1)
        assert command.assumption_contract is contract.evidence.contract(operation + "_foreground_recovery", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "delete"])
@pytest.mark.parametrize("invalid", ["binding", "ticket", "path"])
async def test_formal_file_call_rejects_changed_original_target_before_io(operation, invalid):
    channel = Channel(operation)
    request = _request(operation)
    if invalid == "binding":
        request = replace(request, params={**request.params,
            "identity_key": json.dumps(["different", BINDING.driver_id, PATH])})
    elif invalid == "ticket":
        request = replace(request, ticket=replace(request.ticket, target_id="8"))
    else:
        request = _request(operation, "/mnt/media_rw/emulated/DCIM/../other.OSV")
    with pytest.raises(ValueError):
        await getattr(_driver(osmo360ii_contract(), channel), "query_file" if operation == "query" else "delete")(request)
    assert channel.calls == []
