"""Action6 的会话设置先于两类拍摄，按设备复用可靠接受结果。"""

from dataclasses import replace
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome
from .test_adb_camera_driver import (
    Action6ContractTransport, _action6_contract_driver, _request, _timelapse_request,
)


SETTINGS = (
    ("simulate_device", "-n", "DualGuiCmd", "12"),
    ("simulate_device", "-s", "DevicePowerUserIdleControlAutoShutdownTime", "0"),
    ("simulate_device", "-s", "DeviceSystemLiveviewStream", "0", "0"),
    ("simulate_device", "-s", "DeviceSystemLiveviewStream", "1", "0"),
)


def commands(transport):
    return [tuple(shlex.split(shlex.split(call.argv[5])[2])) for call in transport.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [_request, _timelapse_request])
async def test_setup_precedes_record_or_native_and_is_reused(first):
    transport = Action6ContractTransport()
    driver = _action6_contract_driver(transport)
    for request in (first(), _request(), _timelapse_request()):
        assert (await driver.control(request)).error is None
    actual = commands(transport)
    assert tuple(actual[:4]) == SETTINGS
    assert all(actual.count(command) == 1 for command in SETTINGS)
    assert actual.count(("simulate_device", "-s", "bitrate", "2")) == 1 + (first is _request)


@pytest.mark.asyncio
async def test_setup_is_independent_per_device_and_repeated_in_new_session():
    transport = Action6ContractTransport()
    driver = _action6_contract_driver(transport)
    driver.devices = {**driver.devices, "other": {
        "driver": "dji-action6", "adb": {"serial": "serial-2"}}}
    for device in ("cam-1", "other", "cam-1", "other"):
        selected = replace(_request(), binding=DeviceBinding(device, "dji-action6"))
        assert (await driver.control(selected)).error is None
    assert all(commands(transport).count(command) == 2 for command in SETTINGS)
    driver.begin_session()
    assert (await driver.control(_timelapse_request())).error is None
    assert all(commands(transport).count(command) == 3 for command in SETTINGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [{"stderr": b"setting failed"}, {"error": "timeout"},
                                  {"output_failure": "truncated"},
                                  {"output": b"SimuDev: when send control set data, server not exist!\n"}])
async def test_unknown_setup_blocks_start_and_is_not_cached(fault):
    bad = replace(RawToolOutcome(LocalExit(exit_code=0), b"accepted\n", None, None, stderr=b""), **fault)
    transport = Action6ContractTransport(overrides={SETTINGS[1]: bad})
    driver = _action6_contract_driver(transport)
    assert (await driver.control(_timelapse_request())).error is not None
    assert tuple(commands(transport)) == SETTINGS[:2]
    transport.overrides.clear()
    assert (await driver.control(_timelapse_request())).error is None
    assert commands(transport).count(SETTINGS[0]) == 1
    assert commands(transport).count(SETTINGS[1]) == 2


@pytest.mark.asyncio
async def test_stop_does_not_run_session_setup():
    transport = Action6ContractTransport()
    driver = _action6_contract_driver(transport)
    stopped = replace(_request(), operation="stop_recording", params={},
                      ticket=AttemptTicket(1, "stop", "7", "stop/7", 9))
    assert (await driver.stop(stopped)).error is None
    assert len(commands(transport)) == 1
    assert commands(transport)[0][-2:] == ("02", "00")
