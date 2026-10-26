"""两种 ADB 相机在首次拍摄准备时共同关闭省电并选择仅充电。"""

from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.operations.models import AttemptStatus, EffectState
from camctl.operations.process import LocalExit, RawToolOutcome
from .test_adb_camera_driver import (
    Action6ContractTransport, _action6_contract_driver, _request, _timelapse_request,
)
from .test_capture_preparation import _preparation, _start
from .test_osmo360ii_capture import Transport as OsmoTransport
from .test_osmo360ii_capture import _driver as _osmo_driver, _request as _osmo_request


POWER_SETTINGS = (
    ("simulate_device", "-s", "DevicePowerUserIdleControlLapseLockTime", "0"),
    ("simulate_device", "-s", "0xa13cdabe", "3"),
)


def _camera(model, kind="timelapse"):
    if model == "action6":
        transport = Action6ContractTransport()
        driver = _action6_contract_driver(transport)
        request = _request() if kind == "record" else _timelapse_request()
        calls = transport.calls
    else:
        transport = OsmoTransport()
        driver = _osmo_driver(transport)
        request = _osmo_request(kind)
        calls = transport.specs
    return driver, transport, _preparation(request), calls


def _command(argv):
    return tuple(shlex.split(shlex.split(argv[5])[2]))


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["action6", "osmo"])
@pytest.mark.parametrize("kind", ["record", "timelapse"])
async def test_power_settings_run_in_first_preparation_and_repeat_only_in_new_session(model, kind):
    driver, _, preparation, calls = _camera(model, kind)
    assert calls == []

    assert (await driver.prepare_capture(preparation)).error is None
    commands = [_command(call.argv) for call in calls]
    assert tuple(commands[2:4]) == POWER_SETTINGS
    assert all(commands.count(command) == 1 for command in POWER_SETTINGS)
    before_start = len(calls)
    assert (await driver.control(_start(preparation))).error is None
    assert len(calls) == before_start + 1

    later = replace(preparation, ticket=replace(preparation.ticket, run_id=20))
    assert (await driver.prepare_capture(later)).error is None
    commands = [_command(call.argv) for call in calls]
    assert all(commands.count(command) == 1 for command in POWER_SETTINGS)

    driver.begin_session()
    assert (await driver.prepare_capture(preparation)).error is None
    commands = [_command(call.argv) for call in calls]
    assert all(commands.count(command) == 2 for command in POWER_SETTINGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["action6", "osmo"])
@pytest.mark.parametrize("failed_command", POWER_SETTINGS)
async def test_failed_power_setting_blocks_preparation_and_is_not_cached(model, failed_command, monkeypatch):
    driver, transport, preparation, calls = _camera(model)
    original_run = transport.run
    blocked = [True]

    async def run(spec, stop):
        raw = await original_run(spec, stop)
        if blocked[0] and _command(spec.argv) == failed_command:
            return replace(raw, exit=LocalExit(exit_code=1), stderr=b"setting failed")
        return raw

    monkeypatch.setattr(transport, "run", run)
    result = await driver.prepare_capture(preparation)
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error is not None
    commands = [_command(call.argv) for call in calls]
    assert commands[-1] == failed_command
    assert not driver.prepared_capture_matches(_start(preparation))

    blocked[0] = False
    later = replace(preparation, ticket=replace(preparation.ticket, run_id=20))
    assert (await driver.prepare_capture(later)).error is None
    commands = [_command(call.argv) for call in calls]
    assert commands.count(failed_command) == 2
    other = next(command for command in POWER_SETTINGS if command != failed_command)
    assert commands.count(other) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["action6", "osmo"])
async def test_recovery_preparation_reapplies_power_settings_and_restores_cache(model, mocker):
    driver, _, preparation, calls = _camera(model)
    assert (await driver.prepare_capture(preparation)).error is None

    def returned(binding, argv, timeout_s):
        output = (b"link to server rlt 0\nregister to server successs\n"
                  if _command(argv)[0] == "simulate_device" else
                  b"Filesystem 1K-blocks Used Available Use% Mounted on\n"
                  b"/dev/media 100 10 90 10% /DCIM\n")
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    recovery = mocker.patch.object(driver, "_recovery_raw", autospec=True, side_effect=returned)
    result = await driver.recovery_prepare(preparation.binding, preparation.params,
        activity_id=7, directories=("/DCIM",), timeout_s=Decimal("3"))
    assert result["ready"] is True
    settings = [_command(call.args[1]) for call in recovery.await_args_list[:-1]]
    assert tuple(settings[2:4]) == POWER_SETTINGS
    assert all(settings.count(command) == 1 for command in POWER_SETTINGS)

    before = len(calls)
    later = replace(preparation, ticket=replace(preparation.ticket, run_id=20))
    assert (await driver.prepare_capture(later)).error is None
    assert all(_command(call.argv) not in POWER_SETTINGS for call in calls[before:])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["action6", "osmo"])
@pytest.mark.parametrize("failed_command", POWER_SETTINGS)
async def test_recovery_power_setting_failure_stops_before_later_commands(model, failed_command, mocker):
    driver, _, preparation, _ = _camera(model)
    assert (await driver.prepare_capture(preparation)).error is None

    def returned(binding, argv, timeout_s):
        failed = _command(argv) == failed_command
        return RawToolOutcome(LocalExit(exit_code=1 if failed else 0),
            b"link to server rlt 0\nregister to server successs\n", None, None,
            stderr=b"setting failed" if failed else b"")

    recovery = mocker.patch.object(driver, "_recovery_raw", autospec=True, side_effect=returned)
    result = await driver.recovery_prepare(preparation.binding, preparation.params,
        activity_id=7, directories=("/DCIM",), timeout_s=Decimal("3"))

    assert result["ready"] is False
    commands = [_command(call.args[1]) for call in recovery.await_args_list]
    assert commands[-1] == failed_command
    assert len(commands) == 3 + POWER_SETTINGS.index(failed_command)
    assert not driver.prepared_capture_matches(_start(preparation))
