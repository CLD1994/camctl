from decimal import Decimal

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.operations.process import LocalExit, RawToolOutcome


class Transport:
    def __init__(self, clock, increments):
        self.clock = clock
        self.increments = increments

    async def run(self, spec, stop):
        self.clock[0] += next(self.increments)
        output = {
            "list": b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n",
            "info": b"serial : CAMERA-1\n",
            "doctor": b"doctor: PASS\n",
        }[spec.argv[3]]
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("increments,available", [
    ((1, 1, 1), True),
    ((1, 1, 3), False),
])
async def test_availability_preserves_shared_deadline_facts(increments, available):
    clock = [10_000_000_000]
    transport = Transport(clock, iter(value * 1_000_000_000 for value in increments))
    driver = HasselbladDriver({"camera-1": {"kind": "camera", "driver": DRIVER_ID,
        "hasselblad": {"executable": "/opt/hb_cli", "serial": "CAMERA-1"}}},
        None, monotonic_ns=lambda: clock[0], transport=transport)
    result = await driver.check_availability(DeviceBinding("camera-1", DRIVER_ID),
        expected_serial="CAMERA-1", expected_product_id=10, timeout_s=Decimal(5))
    facts = result.to_device_result("31").outcome.observations[0].data
    assert result.available is available
    assert facts["started_monotonic_ns"] == 10_000_000_000
    assert facts["finished_monotonic_ns"] == clock[0]
    assert facts["steps"][-1]["returned_monotonic_ns"] == clock[0]
    if not available:
        assert result.reason == "deadline_elapsed"
