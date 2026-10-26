"""同一准备尝试内，设备配置与普通控制分别消费自己的期限。"""

from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.capture_preparation import DEFAULT_SESSION_SETUP_TIMEOUT_S
from camctl.devices.drivers.adb_cameras.commands import (
    ACTION6_SESSION_SETUP, OSMO_RECORD_FORMAT, OSMO_SESSION_SETUP, CameraModel,
    settings_from_effective_params,
)
from camctl.operations.models import AttemptStatus
from camctl.operations.process import LocalExit, RawToolOutcome
from .test_adb_camera_driver import Action6ContractTransport, _action6_contract_driver, _request
from .test_capture_preparation import _preparation, _start
from .test_osmo360ii_capture import Transport as OsmoTransport
from .test_osmo360ii_capture import _driver as _osmo_driver, _request as _osmo_request


def _command(spec):
    return tuple(shlex.split(shlex.split(spec.argv[5])[2]))


def _world(*, setup_s="5", ordinary_s="0", after_command=None, overrides=None):
    clock = [0]

    def advance(command):
        duration = setup_s if command in ACTION6_SESSION_SETUP else ordinary_s
        clock[0] += int(Decimal(duration) * 1_000_000_000)
        if after_command is not None:
            after_command(command, clock)

    transport = Action6ContractTransport(on_command=advance, overrides=overrides)
    driver = _action6_contract_driver(transport, clock=lambda: clock[0])
    request = replace(_preparation(_request()), timeout_s=Decimal("40"),
        session_setup_timeout_s=DEFAULT_SESSION_SETUP_TIMEOUT_S)
    return driver, transport, request


@pytest.mark.asyncio
async def test_six_five_second_setup_calls_leave_the_full_ordinary_budget():
    driver, transport, request = _world()
    result = await driver.prepare_capture(request)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    setup = [call for call in transport.calls if _command(call) in ACTION6_SESSION_SETUP]
    ordinary = [call for call in transport.calls if _command(call) not in ACTION6_SESSION_SETUP]
    assert [call.timeout_s for call in setup] == list(map(Decimal, (30, 25, 20, 15, 10, 5)))
    assert ordinary and all(call.timeout_s == Decimal("10") for call in ordinary)
    assert driver.prepared_capture_matches(_start(request))
    assert driver.pending_session_setup_timeout_s(request.binding) == 0
    before = len(transport.calls)
    start = replace(_start(request), timeout_s=Decimal("10"), session_setup_timeout_s=Decimal("0"))
    assert (await driver.control(start)).error is None
    assert len(transport.calls) == before + 1
    assert transport.calls[-1].timeout_s == Decimal("10")


@pytest.mark.asyncio
async def test_setup_overrun_stops_before_ordinary_settings():
    def late_setup(command, clock):
        if command == ACTION6_SESSION_SETUP[-1]:
            clock[0] += 1_000_000_000

    driver, transport, request = _world(after_command=late_setup)
    result = await driver.prepare_capture(request)
    assert result.error["code"] == "start_preparation_timeout"
    assert result.error["details"]["phase"] == "session_setup"
    assert result.error["details"]["command"] == list(ACTION6_SESSION_SETUP[-1])
    assert result.error["details"]["timeout_s"] == "5"
    assert len(transport.calls) == len(ACTION6_SESSION_SETUP)
    last_call = result.outcome.settlement.evidence.data["calls"][-1]
    assert last_call["status"] == "succeeded" and last_call["error"] is None
    assert last_call["stdout_hex"] == b"link to server rlt 0\nregister to server successs\n".hex()
    assert not driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
async def test_ordinary_settings_cannot_borrow_unused_setup_time():
    driver, transport, request = _world(setup_s="1", ordinary_s="3")
    result = await driver.prepare_capture(request)
    ordinary = [call for call in transport.calls if _command(call) not in ACTION6_SESSION_SETUP]
    assert [call.timeout_s for call in ordinary] == list(map(Decimal, (10, 7, 4, 1)))
    assert result.error["code"] == "start_preparation_timeout"
    assert result.error["details"]["phase"] == "settings"
    assert not driver.prepared_capture_matches(_start(request))
    assert driver.pending_session_setup_timeout_s(request.binding) == 0


@pytest.mark.asyncio
async def test_last_ordinary_setting_is_checked_against_ten_seconds_not_total_forty():
    reference_driver, reference_transport, reference_request = _world(setup_s="0")
    await reference_driver.prepare_capture(reference_request)
    last = _command(reference_transport.calls[-1])

    def late_last(command, clock):
        if command == last:
            clock[0] += 11_000_000_000

    driver, transport, request = _world(setup_s="1", after_command=late_last)
    result = await driver.prepare_capture(request)
    assert len(transport.calls) == len(reference_transport.calls)
    assert result.error["code"] == "start_preparation_timeout"
    assert result.error["details"] == {"command": list(last), "timeout_s": "10", "phase": "settings"}
    assert not driver.prepared_capture_matches(_start(request))


@pytest.mark.asyncio
async def test_confirmed_session_setup_is_skipped_then_new_session_resets_it():
    driver, transport, request = _world()
    assert driver.pending_session_setup_timeout_s(request.binding) == Decimal("30")
    await driver.prepare_capture(request)
    assert driver.pending_session_setup_timeout_s(request.binding) == 0
    before = len(transport.calls)
    followup = replace(request, timeout_s=Decimal("10"), session_setup_timeout_s=Decimal("0"))
    assert (await driver.prepare_capture(followup)).error is None
    assert all(_command(call) not in ACTION6_SESSION_SETUP for call in transport.calls[before:])
    assert all(call.timeout_s == Decimal("10") for call in transport.calls[before:])
    driver.begin_session()
    assert driver.pending_session_setup_timeout_s(request.binding) == Decimal("30")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["binding", "serial"])
async def test_setup_cache_is_bound_to_the_logical_binding_and_current_serial(changed):
    driver, transport, request = _world(setup_s="0")
    await driver.prepare_capture(request)
    before = len(transport.calls)
    if changed == "binding":
        driver.devices["other"] = {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}
        binding = DeviceBinding("other", CameraModel.ACTION6)
    else:
        driver.devices["cam-1"]["adb"]["serial"] = "serial-2"
        binding = request.binding
    assert driver.pending_session_setup_timeout_s(binding) == Decimal("30")
    assert len(transport.calls) == before


@pytest.mark.asyncio
async def test_budget_query_ignores_local_recovery_isolation_but_dispatch_still_checks_it():
    driver, transport, request = _world()
    driver.hold_recovery(request.binding.device_id)
    assert driver.pending_session_setup_timeout_s(request.binding) == Decimal("30")
    assert not transport.calls
    with pytest.raises(RuntimeError):
        await driver.prepare_capture(request)
    assert not transport.calls


@pytest.mark.asyncio
async def test_cancel_after_confirmed_setup_prefix_stops_and_reuses_that_prefix():
    driver, transport, request = _world(setup_s="1")
    request = replace(request, dispatch_check=lambda: _check_cancel(len(transport.calls)))
    result = await driver.prepare_capture(request)
    assert result.error["code"] == "canceled"
    assert len(transport.calls) == 2
    assert driver.pending_session_setup_timeout_s(request.binding) == Decimal("30")
    before = len(transport.calls)
    fresh = replace(request, dispatch_check=lambda: _check_cancel(0))
    assert (await driver.prepare_capture(fresh)).error is None
    assert [_command(call) for call in transport.calls[before:]
        if _command(call) in ACTION6_SESSION_SETUP] == list(ACTION6_SESSION_SETUP[2:])


async def _check_cancel(completed):
    return "canceled" if completed == 2 else None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["session_setup", "settings"])
async def test_real_timeout_keeps_its_error_command_and_remaining_budget(phase):
    command = (ACTION6_SESSION_SETUP[2] if phase == "session_setup" else
        settings_from_effective_params(CameraModel.ACTION6, "camera_record", _request().params)[0])
    raw = RawToolOutcome(LocalExit(signal=15), b"original bytes", "timeout", Decimal("1"), stderr=b"")
    driver, transport, request = _world(setup_s="1", overrides={command: raw})
    result = await driver.prepare_capture(request)
    assert result.error["code"] == "transport_timeout"
    assert result.error["details"]["command"] == list(command)
    assert result.error["details"]["timeout_s"] == ("28" if phase == "session_setup" else "10")
    assert result.error["details"]["phase"] == phase
    assert len(transport.calls) == (3 if phase == "session_setup" else len(ACTION6_SESSION_SETUP) + 1)
    assert driver.pending_session_setup_timeout_s(request.binding) == (Decimal("30") if phase == "session_setup" else 0)


@pytest.mark.asyncio
async def test_lapse_lock_service_error_text_allows_preparation_with_split_budget():
    command = ("simulate_device", "-s", "DevicePowerUserIdleControlLapseLockTime", "0")
    raw = RawToolOutcome(LocalExit(exit_code=0),
        b"SimuDev: when send control set data, server not exist!", None, None, stderr=b"")
    driver, transport, request = _world(setup_s="0", overrides={command: raw})
    result = await driver.prepare_capture(request)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.error is None
    assert driver.prepared_capture_matches(_start(request))
    assert driver.pending_session_setup_timeout_s(request.binding) == 0
    call = next(item for item in result.outcome.settlement.evidence.data["calls"]
                if item["command"] == list(command))
    assert call["stdout_hex"] == raw.output.hex()
    assert call["result_document"]["observations"][0]["data"]["confirmation_basis"] == (
        "action6_simulate_device_output_unchecked/v1")


@pytest.mark.asyncio
async def test_zero_setup_budget_preserves_the_original_single_budget():
    driver, transport, request = _world(setup_s="2")
    request = replace(request, timeout_s=Decimal("10"), session_setup_timeout_s=Decimal("0"))
    result = await driver.prepare_capture(request)
    assert [call.timeout_s for call in transport.calls] == list(map(Decimal, (10, 8, 6, 4, 2)))
    assert result.error == {"code": "start_preparation_timeout", "stage": "dispatch", "details": {}}


@pytest.mark.asyncio
async def test_osmo_record_format_uses_ordinary_budget_even_though_it_is_session_once():
    clock = [0]

    class TimedOsmoTransport(OsmoTransport):
        async def run(self, spec, stop):
            raw = await super().run(spec, stop)
            if _command(spec) in OSMO_SESSION_SETUP:
                clock[0] += 5_000_000_000
            return raw

    transport = TimedOsmoTransport()
    driver = _osmo_driver(transport)
    driver.monotonic_ns = lambda: clock[0]
    request = replace(_preparation(_osmo_request()), timeout_s=Decimal("40"),
        session_setup_timeout_s=Decimal("30"))
    assert (await driver.prepare_capture(request)).error is None
    calls = { _command(call): call for call in transport.specs }
    assert calls[OSMO_RECORD_FORMAT].timeout_s == Decimal("10")
    assert driver.pending_session_setup_timeout_s(request.binding) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", [Decimal("-1"), Decimal("40"), Decimal("NaN"), 30, False])
async def test_invalid_phase_budget_does_not_dispatch(setup):
    driver, transport, request = _world()
    with pytest.raises(ValueError):
        await driver.prepare_capture(replace(request, session_setup_timeout_s=setup))
    assert not transport.calls
