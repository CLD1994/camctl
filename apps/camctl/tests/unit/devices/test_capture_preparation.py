"""独立设置准备只授权匹配原活动的后续 START。"""

from unittest.mock import AsyncMock

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import shlex
from types import SimpleNamespace

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras import driver as driver_module
from camctl.devices.drivers.adb_cameras.commands import CameraModel, start_for
from camctl.devices.drivers.registry import CapabilityNotDeclaredError, DriverEntry, port_for
from camctl.devices.ports import DriverDeclaration
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError
from .test_adb_camera_driver import (
    Action6ContractTransport, Transport, _action6_contract_driver, _driver,
    _request, _timelapse_request,
)
from .test_osmo360ii_capture import _driver as _osmo_driver, _request as _osmo_request
from .test_osmo360ii_capture import Transport as OsmoTransport


def _preparation(request):
    return replace(request, ticket=AttemptTicket(2, "control", "7", "prepare/7/session/1", 10))


def _start(request):
    return replace(request, ticket=AttemptTicket(1, "control", "7", "start/7", 11),
                   prepared_by=request.ticket)


def _commands(transport):
    return [tuple(shlex.split(shlex.split(call.argv[5])[2])) for call in transport.calls]


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_capture_preparation_declaration_requires_boolean(value):
    with pytest.raises(ValueError):
        DriverDeclaration(True, False, False, False, False, False, False,
                          capture_preparation_supported=value)


def test_capture_preparation_port_and_default_are_independently_declared():
    driver = _action6_contract_driver(Action6ContractTransport())
    entry = DriverEntry(driver.contract.driver_id, driver, driver.declaration,
                        driver.contract.evidence, driver.contract.status)
    assert port_for(entry, "prepare_capture") is driver
    assert driver.default_prepare_lead_s == Decimal("10")
    unsupported = replace(entry, declaration=replace(driver.declaration,
                                                     capture_preparation_supported=False))
    with pytest.raises(CapabilityNotDeclaredError):
        port_for(unsupported, "prepare_capture")


@pytest.mark.asyncio
@pytest.mark.parametrize("model,kind", [("action6", "record"), ("action6", "timelapse"),
                                        ("osmo", "record"), ("osmo", "timelapse")])
async def test_preparation_returns_real_setting_and_matching_start_sends_only_start(model, kind):
    if model == "action6":
        transport = Action6ContractTransport()
        driver = _action6_contract_driver(transport)
        request = _request() if kind == "record" else _timelapse_request()
    else:
        transport = OsmoTransport()
        driver = _osmo_driver(transport)
        request = _osmo_request(kind)
    request = _preparation(request)
    result = await driver.prepare_capture(request)
    calls = transport.calls if model == "action6" else transport.specs
    before = len(calls)
    expected = start_for(driver.contract.driver_id, "camera_" + kind)
    commands = [tuple(shlex.split(shlex.split(call.argv[5])[2])) for call in calls]
    assert expected not in commands
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.outcome.call_info is not None
    assert [(item.type, item.version, item.data) for item in result.observations] == [
        ("capture_preparation_completed", 1, {"activity_id": "7"})]
    start = _start(request)
    assert driver.prepared_capture_matches(start)
    assert (await driver.control(start)).error is None
    assert len(calls) == before + 1
    assert tuple(shlex.split(shlex.split(calls[-1].argv[5])[2])) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exit_nonzero"])
async def test_failed_osmo_preparation_preserves_exact_command_and_call_budget(failure):
    expected = ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "1e", "0000")

    class FailedSettingTransport(OsmoTransport):
        async def run(self, spec, stop):
            command = tuple(shlex.split(shlex.split(spec.argv[5])[2]))
            if command != expected:
                return await super().run(spec, stop)
            self.commands.append(list(command))
            self.specs.append(spec)
            return RawToolOutcome(
                LocalExit(signal=15) if failure == "timeout" else LocalExit(exit_code=1),
                b"", "timeout" if failure == "timeout" else None, None,
                stderr=b"" if failure == "timeout" else b"device rejected setting")

    transport = FailedSettingTransport()
    driver = _osmo_driver(transport)
    result = await driver.prepare_capture(_preparation(_osmo_request("timelapse", "auto")))

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.error.details["command"] == list(expected)
    assert result.outcome.error.details["timeout_s"] == "10"
    details = (result.outcome.error.details["response_error"]["details"]
        if failure == "timeout" else result.outcome.error.details)
    assert details["local_signal"] == (15 if failure == "timeout" else None)
    assert details["stderr_hex"] == (b"" if failure == "timeout" else b"device rejected setting").hex()
    assert transport.commands[-1] == list(expected)
    assert not driver.prepared_capture_matches(_start(_preparation(_osmo_request("timelapse", "auto"))))


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["activity", "ticket", "operation", "params", "boolean",
                                     "binding", "serial", "session", "driver"])
async def test_prepared_start_rejects_each_mismatched_proof_without_device_call(mismatch):
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    start = _start(preparation)
    if mismatch == "activity":
        start = replace(start, ticket=replace(start.ticket, target_id="8"))
    elif mismatch == "ticket":
        start = replace(start, prepared_by=replace(preparation.ticket, run_id=100))
    elif mismatch == "operation":
        start = replace(start, operation="start_timelapse")
    elif mismatch == "params":
        start = replace(start, params={**start.params, "duration_s": 11})
    elif mismatch == "boolean":
        start = replace(start, params={**start.params, "exposure": {
            **start.params["exposure"], "compensation_ev": False}})
    elif mismatch == "binding":
        driver.devices["other"] = {"driver": "dji-action6", "adb": {"serial": "serial-1"}}
        start = replace(start, binding=DeviceBinding("other", "dji-action6"))
    elif mismatch == "serial":
        driver.devices["cam-1"]["adb"]["serial"] = "serial-2"
    elif mismatch == "session":
        driver.begin_session()
    else:
        driver = _driver(CameraModel.ACTION6, transport)
    before = len(transport.calls)
    assert not driver.prepared_capture_matches(start)
    with pytest.raises(ValueError):
        await driver.control(start)
    assert len(transport.calls) == before


@pytest.mark.asyncio
async def test_unseen_preparation_ticket_does_not_skip_or_repeat_settings():
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    start = _start(_preparation(_request()))
    assert not driver.prepared_capture_matches(start)
    with pytest.raises(ValueError):
        await driver.control(start)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_preparation_snapshots_nested_parameters_before_dispatch():
    params = deepcopy(_request().params)
    transport = Transport(after_call=lambda _: params["exposure"].update(compensation_ev=1))
    driver = _driver(CameraModel.ACTION6, transport)
    request = _preparation(replace(_request(), params=params))
    await driver.prepare_capture(request)
    assert not driver.prepared_capture_matches(_start(request))
    assert driver.prepared_capture_matches(_start(replace(request, params=_request().params)))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["setting_failed", "cancelled", "timeout", "window"])
async def test_incomplete_preparation_retains_last_result_and_cannot_authorize_start(reason, monkeypatch):
    now = [0]
    scope = SimpleNamespace(interrupted=None)

    def after_call(_):
        if reason == "cancelled":
            scope.interrupted = object()
        elif reason == "timeout":
            now[0] = 20_000_000_000

    transport = Transport(fail_at=1 if reason == "setting_failed" else None, after_call=after_call)
    driver = _driver(CameraModel.ACTION6, transport, clock=lambda: now[0])
    monkeypatch.setattr(driver_module, "current_call_scope", lambda: scope)
    request = _preparation(replace(_request(), dispatch_check=AsyncMock(side_effect=lambda:
        "window_closed" if reason == "window" and transport.calls else None)))
    result = await driver.prepare_capture(request)
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.call_info.local_exit_code == 0
    assert len(transport.calls) == 1
    assert not driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
async def test_interruption_during_dispatch_query_prevents_the_next_device_call(monkeypatch):
    scope = SimpleNamespace(interrupted=None)
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    monkeypatch.setattr(driver_module, "current_call_scope", lambda: scope)

    async def check():
        scope.interrupted = object()
        return None

    request = _preparation(replace(_request(), dispatch_check=check))
    result = await driver.prepare_capture(request)

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.error.code == "transport_cancelled"
    assert transport.calls == []
    assert not driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
async def test_prepared_start_checks_dispatch_and_uses_its_own_deadline():
    now = [0]
    transport = Transport(after_call=lambda _: now.__setitem__(0, now[0] + 1_000_000_000))
    driver = _driver(CameraModel.ACTION6, transport, clock=lambda: now[0])
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    start = _start(preparation)
    before = len(transport.calls)
    blocked = await driver.control(replace(start, dispatch_check=AsyncMock(side_effect=lambda: "window_closed")))
    assert blocked.error["code"] == "window_closed"
    assert len(transport.calls) == before
    now[0] += 100_000_000_000
    assert (await driver.control(start)).error is None
    assert len(transport.calls) == before + 1
    assert transport.calls[-1].timeout_s == start.timeout_s


@pytest.mark.asyncio
async def test_new_preparation_replaces_previous_device_proof_even_when_it_fails():
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    first = _preparation(_request())
    await driver.prepare_capture(first)
    transport.fail_at = len(transport.calls) + 1
    second = replace(first, ticket=replace(first.ticket, run_id=20), params={**first.params, "duration_s": 11})
    await driver.prepare_capture(second)
    assert not driver.prepared_capture_matches(_start(first))
    assert not driver.prepared_capture_matches(_start(second))


@pytest.mark.asyncio
async def test_legacy_control_settings_invalidate_preparation():
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    await driver.control(_request())
    assert not driver.prepared_capture_matches(_start(preparation))


@pytest.mark.asyncio
async def test_software_reboot_invalidates_preparation_before_actual_call(mocker):
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    raw = RawToolOutcome(LocalExit(exit_code=0), b"", None, None, stderr=b"")
    mocker.patch.object(driver, "_recovery_raw", autospec=True, return_value=raw)
    await driver.recovery_reboot(preparation.binding, timeout_s=Decimal("3"))
    assert not driver.prepared_capture_matches(_start(preparation))


@pytest.mark.asyncio
async def test_software_recovery_preparation_clears_proof_even_when_first_setting_fails(mocker):
    driver = _action6_contract_driver(Action6ContractTransport())
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    raw = RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=b"failed")
    mocker.patch.object(driver, "_recovery_raw", autospec=True, return_value=raw)
    result = await driver.recovery_prepare(preparation.binding, preparation.params,
        activity_id=7, directories=("/DCIM",), timeout_s=Decimal("3"))
    assert result["ready"] is False
    assert not driver.prepared_capture_matches(_start(preparation))


@pytest.mark.asyncio
async def test_preparation_records_one_current_proof_per_device_and_preserves_other_device():
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    driver.devices["other"] = {"driver": "dji-action6", "adb": {"serial": "serial-2"}}
    first = _preparation(_request())
    other = replace(first, binding=DeviceBinding("other", "dji-action6"))
    await driver.prepare_capture(first)
    await driver.prepare_capture(other)
    for index in range(5):
        newest = replace(first, ticket=replace(first.ticket, run_id=20 + index))
        await driver.prepare_capture(newest)
    assert len(driver._prepared_captures) == 2
    assert not driver.prepared_capture_matches(_start(first))
    assert driver.prepared_capture_matches(_start(newest))
    assert driver.prepared_capture_matches(_start(other))


@pytest.mark.asyncio
async def test_prepared_start_unknown_result_does_not_repeat_start_or_settings():
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport)
    preparation = _preparation(_request())
    await driver.prepare_capture(preparation)
    before = len(transport.calls)
    transport.fail_at = before + 1
    result = await driver.control(_start(preparation))
    assert result.error["code"] == "response_unconfirmed"
    assert result.outcome.effect is EffectState.UNKNOWN
    assert len(transport.calls) == before + 1
    assert _commands(transport)[-1] == start_for(CameraModel.ACTION6, "camera_record")


@pytest.mark.asyncio
async def test_action6_preparation_keeps_aperture_failure_history_and_allows_start():
    transport = Action6ContractTransport(aperture_payload="e3")
    driver = _action6_contract_driver(transport)
    request = _preparation(_request())
    result = await driver.prepare_capture(request)
    assert result.error is None
    assert result.outcome.settlement.evidence.version == 2
    history = result.outcome.settlement.evidence.data["calls"]
    assert len(history) == len(transport.calls)
    assert any(call["error"] is not None for call in history)
    assert not any(call["command"] == list(start_for(CameraModel.ACTION6, "camera_record"))
                   for call in history)
    assert driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "tool_start"])
async def test_last_non_blocking_aperture_failure_is_preserved_beneath_preparation_completion(failure):
    aperture = ("dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2",
                "-c", "0x26", "1801")
    transport = Action6ContractTransport(aperture_payload="e3", exceptions={
        aperture: ToolStartError("spawn denied")} if failure == "tool_start" else None)
    driver = _action6_contract_driver(transport)
    params = dict(_request().params)
    del params["bitrate"]
    request = _preparation(replace(_request(), params=params))
    result = await driver.prepare_capture(request)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.error is None
    assert [(item.type, item.data) for item in result.observations] == [
        ("capture_preparation_completed", {"activity_id": "7"})]
    assert result.outcome.settlement.basis.value == "observed"
    calls = result.outcome.settlement.evidence.data["calls"]
    assert calls[-1]["command"] == list(aperture)
    assert calls[-1]["status"] == "failed"
    assert calls[-1]["error"] is not None
    assert calls[-1]["result_document"]["settlement"]["basis"] == (
        "not_dispatched" if failure == "tool_start" else "observed")
    assert driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["window", "timeout"])
async def test_loss_of_qualification_after_last_setting_does_not_complete_preparation(gate):
    now = [0]
    transport = Action6ContractTransport(on_command=lambda command:
        now.__setitem__(0, 20_000_000_000) if command[:3] == ("simulate_device", "-s", "bitrate") else None)
    driver = _action6_contract_driver(transport, clock=lambda: now[0] if gate == "timeout" else 0)
    request = _preparation(replace(_request(), dispatch_check=AsyncMock(side_effect=lambda:
        "window_closed" if gate == "window" and now[0] else None)))
    result = await driver.prepare_capture(request)
    assert result.outcome.status is AttemptStatus.FAILED
    assert all(item.type != "capture_preparation_completed" for item in result.observations)
    assert not driver.prepared_capture_matches(_start(request))
