"""固定设置在会话内按设备复用，动作码率始终重新设置。"""

from dataclasses import replace

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.operations.models import AttemptStatus, AttemptTicket
from .test_osmo360ii_capture import Transport, _driver, _request


INITIAL_SETTINGS = [
    ["simulate_device", "-n", "DualGuiCmd", "12"],
    ["simulate_device", "-s", "0xa13cdabe", "3"],
]
FORMAT = ["simulate_device", "-s", "BaseFormat", "6", "69", "0"]


def request(kind, bitrate="high", mode="auto", device="camera"):
    original = _request(kind, mode)
    return replace(original, binding=DeviceBinding(device, "dji-osmo360-ii"),
                   params={**original.params, "bitrate": bitrate})


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["record", "timelapse"])
async def test_fixed_settings_once_but_bitrate_for_each_capture(first):
    transport = Transport()
    driver = _driver(transport)
    for kind, bitrate, value in [(first, "standard", "1"), ("record", "high", "2"),
                                  ("timelapse", "high", "2"), ("record", "standard", "1")]:
        before = len(transport.commands)
        result = await driver.control(request(kind, bitrate))
        assert result.error is None
        actual = transport.commands[before:]
        assert actual[-2] == ["simulate_device", "-s", "DeviceRecordRecSettingBitRate", value]
        assert actual[-1][-2:] == (["02", "01"] if kind == "record" else ["01", "01"])
    assert transport.commands[:2] == INITIAL_SETTINGS
    assert all(transport.commands.count(command) == 1 for command in (*INITIAL_SETTINGS, FORMAT))


@pytest.mark.asyncio
async def test_each_device_has_its_own_fixed_settings():
    transport = Transport()
    driver = _driver(transport)
    driver.devices = {**driver.devices, "other": {"driver": "dji-osmo360-ii", "adb": {"serial": "serial-2"}}}
    for device in ("camera", "other", "camera", "other"):
        assert (await driver.control(request("record", device=device))).error is None
    for serial in ("serial-1", "serial-2"):
        commands = [command for command, spec in zip(transport.commands, transport.specs)
                    if spec.argv[2] == serial]
        assert commands[:2] == INITIAL_SETTINGS
        assert all(commands.count(command) == 1 for command in (*INITIAL_SETTINGS, FORMAT))


@pytest.mark.asyncio
async def test_new_session_repeats_fixed_settings():
    transport = Transport()
    driver = _driver(transport)
    await driver.control(request("record"))
    driver.begin_session()
    before = len(transport.commands)
    await driver.control(request("record"))
    assert transport.commands[before:before + 2] == INITIAL_SETTINGS
    assert all(transport.commands.count(command) == 2 for command in (*INITIAL_SETTINGS, FORMAT))


@pytest.mark.asyncio
async def test_partial_setup_keeps_only_reliably_accepted_settings():
    transport = Transport(fail_at=2)
    driver = _driver(transport)
    result = await driver.control(request("record"))
    assert result.outcome.status is AttemptStatus.FAILED
    assert transport.commands == INITIAL_SETTINGS
    transport.fail_at = None
    assert (await driver.control(request("record"))).error is None
    assert transport.commands.count(INITIAL_SETTINGS[0]) == 1
    assert transport.commands.count(INITIAL_SETTINGS[1]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [{"stderr": b"setting failed"}, {"output_failure": "truncated"},
                                  {"error": "timeout"}])
async def test_unknown_initial_setting_is_not_skipped_on_next_authorized_start(fault):
    class FaultTransport(Transport):
        async def run(self, spec, stop):
            outcome = await super().run(spec, stop)
            return replace(outcome, **fault) if len(self.commands) == 1 else outcome

    transport = FaultTransport()
    driver = _driver(transport)
    assert (await driver.control(request("record"))).error is not None
    assert transport.commands == INITIAL_SETTINGS[:1]
    assert (await driver.control(request("record"))).error is None
    assert transport.commands.count(INITIAL_SETTINGS[0]) == 2


@pytest.mark.asyncio
async def test_cancellation_after_fixed_setup_prevents_start_and_preserves_accepted_settings():
    transport = Transport()
    driver = _driver(transport)
    selected = replace(request("record"), dispatch_check=lambda: "canceled" if len(transport.commands) >= 2 else None)
    assert (await driver.control(selected)).error is not None
    assert transport.commands == INITIAL_SETTINGS
    assert (await driver.control(request("record"))).error is None
    assert all(transport.commands.count(command) == 1 for command in INITIAL_SETTINGS)


@pytest.mark.asyncio
async def test_session_setup_consumes_the_original_start_deadline():
    transport = Transport()
    driver = _driver(transport)
    clock = iter((0, 0, 11_000_000_000))
    driver.monotonic_ns = lambda: next(clock)
    result = await driver.control(request("record"))
    assert result.error["code"] == "start_preparation_timeout"
    assert transport.commands == INITIAL_SETTINGS[:1]


@pytest.mark.asyncio
async def test_stop_does_not_initialize_or_mark_the_camera_as_initialized():
    transport = Transport()
    driver = _driver(transport)
    selected = replace(request("record"), operation="stop_recording",
                       ticket=AttemptTicket(1, "stop", "7", "stop/7", 9), params={})
    assert (await driver.stop(selected)).error is None
    assert len(transport.commands) == 1 and transport.commands[0][-2:] == ["02", "00"]
    await driver.control(request("record"))
    assert transport.commands[1:3] == INITIAL_SETTINGS
