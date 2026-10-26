"""Action6 的会话设置先于两类拍摄，按设备复用可靠接受结果。"""

from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError
from .test_adb_camera_driver import (
    Action6ContractTransport, _action6_contract_driver, _request, _timelapse_request,
)


SETTINGS = (
    ("simulate_device", "-n", "DualGuiCmd", "12"),
    ("simulate_device", "-s", "DevicePowerUserIdleControlAutoShutdownTime", "0"),
    ("simulate_device", "-s", "DevicePowerUserIdleControlLapseLockTime", "0"),
    ("simulate_device", "-s", "0xa13cdabe", "3"),
    ("simulate_device", "-s", "DeviceSystemLiveviewStream", "0", "0"),
    ("simulate_device", "-s", "DeviceSystemLiveviewStream", "1", "0"),
)
OUTPUT_UNCHECKED = (SETTINGS[0], SETTINGS[1], SETTINGS[3])
UNVERIFIED_OUTPUT = b"SimuDev: when send control notify data, server not exist!\n"


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
    assert tuple(actual[:len(SETTINGS)]) == SETTINGS
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
    transport = Action6ContractTransport(overrides={SETTINGS[2]: bad})
    driver = _action6_contract_driver(transport)
    assert (await driver.control(_timelapse_request())).error is not None
    assert tuple(commands(transport)) == SETTINGS[:3]
    transport.overrides.clear()
    assert (await driver.control(_timelapse_request())).error is None
    assert commands(transport).count(SETTINGS[0]) == 1
    assert commands(transport).count(SETTINGS[1]) == 1
    assert commands(transport).count(SETTINGS[2]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [_request, _timelapse_request])
@pytest.mark.parametrize("prepare_only", [False, True])
@pytest.mark.parametrize("stderr", [b"", b"unverified diagnostic\xff\n"])
async def test_three_temporary_settings_ignore_output_and_reuse_completed_calls(
        first, prepare_only, stderr):
    raw = RawToolOutcome(LocalExit(exit_code=0), UNVERIFIED_OUTPUT, None, None, stderr=stderr)
    transport = Action6ContractTransport(overrides=dict.fromkeys(OUTPUT_UNCHECKED, raw))
    driver = _action6_contract_driver(transport)
    request = first()
    if prepare_only:
        request = replace(request, ticket=AttemptTicket(2, "control", "7", "prepare/7/session/1", 10))
        result = await driver.prepare_capture(request)
        assert result.error is None
        before_start = len(transport.calls)
        start = replace(request, ticket=AttemptTicket(1, "control", "7", "start/7", 11),
                        prepared_by=request.ticket)
        assert driver.prepared_capture_matches(start)
        assert (await driver.control(start)).error is None
        assert len(transport.calls) == before_start + 1
    else:
        result = await driver.control(request)
        assert result.error is None
    assert tuple(commands(transport)[:len(SETTINGS)]) == SETTINGS
    if first is _request:
        attached = result.outcome.settlement.evidence.data["calls"]
        for command in OUTPUT_UNCHECKED:
            call = next(item for item in attached if item["command"] == list(command))
            assert call["stdout_hex"] == UNVERIFIED_OUTPUT.hex()
            assert call["stderr_hex"] == stderr.hex()
            assert call["error"] is None
            assert call["result_document"]["observations"][0]["data"]["confirmation_basis"] == (
                "action6_simulate_device_output_unchecked/v1")
    assert (await driver.control(first())).error is None
    assert all(commands(transport).count(command) == 1 for command in SETTINGS)
    driver.begin_session()
    assert (await driver.control(first())).error is None
    assert all(commands(transport).count(command) == 2 for command in SETTINGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", OUTPUT_UNCHECKED)
@pytest.mark.parametrize("fault", [
    {"exit": LocalExit(exit_code=1)}, {"exit": LocalExit(signal=15)}, {"exit": None},
    {"error": "timeout"}, {"error": "cancelled"}, {"output_failure": "truncated"},
    {"output": None}, {"stderr": None},
])
async def test_temporary_settings_still_block_on_call_failure_and_retry(command, fault):
    raw = replace(RawToolOutcome(LocalExit(exit_code=0), UNVERIFIED_OUTPUT, None, None,
                                stderr=b"unverified diagnostic"), **fault)
    transport = Action6ContractTransport(overrides={command: raw})
    driver = _action6_contract_driver(transport)
    assert (await driver.control(_request())).error is not None
    assert commands(transport)[-1] == command
    transport.overrides.clear()
    assert (await driver.control(_request())).error is None
    assert commands(transport).count(command) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("command", OUTPUT_UNCHECKED)
async def test_temporary_setting_tool_start_failure_blocks_capture(command):
    transport = Action6ContractTransport(exceptions={command: ToolStartError("adb unavailable")})
    result = await _action6_contract_driver(transport).control(_request())
    assert result.error["code"] == "tool_start_failed"
    assert commands(transport)[-1] == command


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [SETTINGS[4], SETTINGS[5], ("simulate_device", "-s", "bitrate", "2")])
async def test_other_action6_settings_still_require_confirmed_output(command):
    raw = RawToolOutcome(LocalExit(exit_code=0), UNVERIFIED_OUTPUT, None, None, stderr=b"")
    transport = Action6ContractTransport(overrides={command: raw})
    result = await _action6_contract_driver(transport).control(_request())
    assert result.error["details"]["reason"] == "simulation_service_unavailable"
    assert commands(transport)[-1] == command


@pytest.mark.asyncio
async def test_software_recovery_uses_temporary_policy_and_retains_original_output(mocker):
    transport = Action6ContractTransport()
    driver = _action6_contract_driver(transport)
    request = _request()
    assert (await driver.control(request)).error is None

    def returned(binding, argv, timeout_s):
        command = tuple(shlex.split(shlex.split(argv[5])[2]))
        unchecked = command in OUTPUT_UNCHECKED
        output = (UNVERIFIED_OUTPUT if unchecked else
                  b"Filesystem 1K-blocks Used Available Use% Mounted on\n"
                  b"/dev/media 100 10 90 10% /DCIM\n")
        return RawToolOutcome(LocalExit(exit_code=0), output,
                              None, None, stderr=b"unverified\xff" if unchecked else b"")

    recovery = mocker.patch.object(driver, "_recovery_raw", autospec=True, side_effect=returned)
    result = await driver.recovery_prepare(request.binding, request.params,
        activity_id=7, directories=("/DCIM",), timeout_s=Decimal("3"))
    assert result["ready"] is True
    actual = [tuple(shlex.split(shlex.split(call.args[1][5])[2]))
              for call in recovery.await_args_list[:-1]]
    assert tuple(actual) == SETTINGS
    for command in OUTPUT_UNCHECKED:
        raw = result["calls"][actual.index(command)]["result"]
        assert raw["stdout_hex"] == UNVERIFIED_OUTPUT.hex()
        assert raw["stderr_hex"] == b"unverified\xff".hex()
    before = len(transport.calls)
    assert (await driver.control(request)).error is None
    assert not any(command in SETTINGS for command in commands(transport)[before:])


@pytest.mark.asyncio
async def test_stop_does_not_run_session_setup():
    transport = Action6ContractTransport()
    driver = _action6_contract_driver(transport)
    stopped = replace(_request(), operation="stop_recording", params={},
                      ticket=AttemptTicket(1, "stop", "7", "stop/7", 9))
    assert (await driver.stop(stopped)).error is None
    assert len(commands(transport)) == 1
    assert commands(transport)[0][-2:] == ("02", "00")
