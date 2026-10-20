"""Action6 正式文件端口执行精确 shell 删除与独立核实。"""
from decimal import Decimal
import json
import os
import time

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket
from camctl.operations.validation import validate_outcome
from .test_adb_camera_transport import LocalAdbChannel

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.name != "posix", reason="真实 POSIX shell 文件边界")]
BINDING = DeviceBinding("cam-1", "dji-action6")


@pytest.mark.parametrize("selected", ["clip.LRF", "clip.MP4", "frame.JPG", "frame.DNG"])
async def test_action6_original_binding_deletes_exact_file_and_queries_absence(tmp_path, selected):
    names = ("clip.LRF", "clip.MP4", "frame.JPG", "frame.DNG")
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    contract = action6_record_contract()
    channel = LocalAdbChannel()
    driver = AdbCameraDriver(contract, {"cam-1": {"driver": BINDING.driver_id,
        "adb": {"serial": "serial-1"}}}, channel, terminate_grace_s=Decimal("1"), monotonic_ns=time.monotonic_ns)
    target = str(tmp_path / selected)
    params = {"cleanup_item_id": "7", "identity_key": json.dumps([BINDING.device_id, BINDING.driver_id, target]),
        "locator": {"path": target}}
    for operation, port, present in (("query", driver.query_file, True),
                                    ("delete", driver.delete, False),
                                    ("query", driver.query_file, False)):
        ticket = AttemptTicket(1, operation, "7", "cleanup/7", 2)
        result = await port(ControlRequest(operation, BINDING, params, ticket, Decimal("3")))
        validate_outcome(ticket, result.outcome, contract.evidence)
        assert result.error is None
        assert result.observations[0].data == {"cleanup_item_id": "7", "present": present}
    assert len(channel.calls) == 3
    assert not (tmp_path / selected).exists()
    assert {name for name in names if (tmp_path / name).exists()} == set(names) - {selected}
    for name in set(names) - {selected}:
        assert (tmp_path / name).read_bytes() == name.encode()


async def test_action6_same_name_directory_is_unknown_and_keeps_contents(tmp_path):
    target = tmp_path / "clip.MP4"
    target.mkdir()
    (target / "kept").write_bytes(b"kept")
    contract = action6_record_contract()
    driver = AdbCameraDriver(contract, {"cam-1": {"driver": BINDING.driver_id,
        "adb": {"serial": "serial-1"}}}, LocalAdbChannel(), terminate_grace_s=Decimal("1"), monotonic_ns=time.monotonic_ns)
    ticket = AttemptTicket(1, "delete", "7", "cleanup/7", 2)
    result = await driver.delete(ControlRequest("delete", BINDING, {
        "cleanup_item_id": "7", "identity_key": json.dumps([BINDING.device_id, BINDING.driver_id, str(target)]),
        "locator": {"path": str(target)}}, ticket, Decimal("3")))
    validate_outcome(ticket, result.outcome, contract.evidence)
    assert result.error is not None and result.observations == ()
    assert (target / "kept").read_bytes() == b"kept"
