"""相机启动组合按一个原尝试执行，设置事实与拍摄事实分别解释。"""

from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.acceptance.schema import BodySchemaError
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError
from camctl.devices.drivers.adb_cameras import commands as camera_commands
from camctl.devices.drivers.adb_cameras import timelapse_parameters
from camctl.devices.drivers.adb_cameras.commands import CameraModel, settings_for, start_for
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState, ErrorValue, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome


RETURNED = EvidenceContract("operation_returned", 1, "control", frozenset())
ASSUMED = EvidenceContract("adb_foreground_recovery", 1, "control", frozenset({"terminate_grace_s"}))
SETTING = EvidenceContract("setting_applied", 1, "control", frozenset({"activity_id"}), identity_field="activity_id")
STARTED = EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}), identity_field="activity_id")
PREVENTED = EvidenceContract("dispatch_prevented", 1, "control", frozenset())
EVIDENCE = EvidenceRegistry((RETURNED, ASSUMED, SETTING, STARTED, PREVENTED))


ACTION6_AUTO_TIMELAPSE_SCRIPTS = (
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400035000080700000000000000000000",
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
)

ACTION6_MANUAL_D1_SCRIPTS = (
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400030a002c0100000000000000000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000",
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
)
ACTION6_AUTO_D1_SCRIPTS = (
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 0400020a002c0100000000000000000000",
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
)
OSMO_MANUAL_D1_SCRIPTS = (
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
    "dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 1e 0400",
    "dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 2A 06",
    "dji_mb_ctrl -r -S test -R diag -g 1 -t 0 -s 2 -c 28 013c8000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2c 0634000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04070014002c01000000000000000001",
    "simulate_device -s DeviceRecordRecSettingBitRate 2",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
)
OSMO_AUTO_D1_SCRIPTS = (
    "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x8e 01013f000101",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c e1 3b",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 6f03000000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000100",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1e 0000",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c 04000014002c01000000000000000000",
    "simulate_device -s DeviceRecordRecSettingBitRate 2",
    "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01",
)
TIMELAPSE_D1_CASES = (
    pytest.param(CameraModel.ACTION6, {"mode": "manual", "iso": 800}, "video_raw",
                 ACTION6_MANUAL_D1_SCRIPTS, id="action6-manual"),
    pytest.param(CameraModel.ACTION6, {"mode": "auto"}, "video_jpeg",
                 ACTION6_AUTO_D1_SCRIPTS, id="action6-auto"),
    pytest.param(CameraModel.OSMO360II, {"mode": "manual", "iso": 800}, "video",
                 OSMO_MANUAL_D1_SCRIPTS, id="osmo-manual"),
    pytest.param(CameraModel.OSMO360II, {"mode": "auto"}, "video",
                 OSMO_AUTO_D1_SCRIPTS, id="osmo-auto"),
)


class Parser:
    def __init__(self, kind, target):
        self.kind, self.target = kind, target

    def interpret(self, raw):
        if raw.output != b"OK":
            return InterpretedFacts((), ErrorValue("response_unconfirmed", "device", {"response": raw.output.decode()}), EffectState.UNKNOWN)
        observation = SETTING if self.kind is CameraCall.SETTING else STARTED
        return InterpretedFacts((DeviceObservation(observation.type, 1, {"activity_id": self.target}),), None, EffectState.CONFIRMED)


class Transport:
    def __init__(self, *, fail_at=None, after_call=None, fail_output=b"UNKNOWN"):
        self.calls = []
        self.fail_at, self.after_call = fail_at, after_call
        self.fail_output = fail_output

    async def run(self, spec, stop):
        self.calls.append(spec)
        if self.after_call is not None:
            self.after_call(len(self.calls))
        return RawToolOutcome(LocalExit(exit_code=0),
            self.fail_output if len(self.calls) == self.fail_at else b"OK", None, None, stderr=b"")


def _params(model):
    if model is CameraModel.ACTION6:
        return {"type": "action6_record", "duration_s": 10, "resolution": "4k30", "fov": "wide",
                "stabilization": "off", "aperture": "f2.8", "bitrate": "high",
                "exposure": {"mode": "auto", "compensation_ev": 0}}
    return {"type": "osmo360ii_record", "duration_s": 10,
            "bitrate": "high", "exposure": {"mode": "auto"}}


def _request(model=CameraModel.ACTION6, *, check=lambda: None, device_id="cam-1"):
    return ControlRequest("start_recording", DeviceBinding(device_id, model), _params(model),
        AttemptTicket(1, "control", "7", "start/3", 9), Decimal("20"), dispatch_check=check)


def _timelapse_request(*, check=lambda: None):
    return replace(_request(check=check), operation="start_timelapse", params={
        "type": "action6_timelapse", "interval_s": 8, "duration_s": 1800,
        "outputs": "video_raw", "exposure": {"mode": "auto"},
    })


def _candidate_timelapse_request(model, exposure, outputs, *, interval_s=None, duration_s=300, check=lambda: None):
    parameter_type = "action6_timelapse" if model is CameraModel.ACTION6 else "osmo360ii_timelapse"
    if interval_s is None:
        interval_s = 1 if model is CameraModel.ACTION6 else 2
    selected = {
        "type": parameter_type, "interval_s": interval_s, "duration_s": duration_s,
        "outputs": outputs, "exposure": exposure,
    }
    if model is CameraModel.OSMO360II:
        del selected["outputs"]
        selected.update(bitrate="high", exposure={"mode": exposure["mode"]})
    return replace(_request(model, check=check), operation="start_timelapse", params=selected)


def _narrow_time_domain(monkeypatch, field, excluded, model):
    if field == "interval_s":
        original = timelapse_parameters.timelapse_intervals_s
        narrowed = tuple(value for value in original(model.value) if value != excluded)
        assert len(narrowed) == len(original(model.value)) - 1

        def current_intervals(requested_model):
            return narrowed if requested_model == model.value else original(requested_model)

        monkeypatch.setattr(timelapse_parameters, "timelapse_intervals_s", current_intervals)
        return
    narrowed = tuple(value for value in timelapse_parameters.TIMELAPSE_DURATIONS_S if value != excluded)
    assert len(narrowed) == len(timelapse_parameters.TIMELAPSE_DURATIONS_S) - 1
    monkeypatch.setattr(timelapse_parameters, "TIMELAPSE_DURATIONS_S", narrowed)


TIMELAPSE_STABLE_FORMAT_CASES = (
    (CameraModel.ACTION6, 1, 300, "0400000a002c0100000000000000000000"),
    (CameraModel.ACTION6, 2, 600, "0400001400580200000000000000000000"),
    (CameraModel.OSMO360II, 2, 600, "04000014005802000000000000000000"),
)


def _driver(model, transport, *, clock=lambda: 0, devices=None):
    def factory(kind):
        def build(request, serial, argv, batch):
            assert batch is None
            return DeviceCommand(request.ticket.operation, argv, request.binding, request.timeout_s,
                Decimal("1"), EVIDENCE, RETURNED, ASSUMED,
                Parser(kind, request.ticket.target_id), kind.value)
        return build
    contract = CameraContract(model, commands={kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START)},
        file_tools=ShellFileTools(), evidence=EVIDENCE)
    return AdbCameraDriver(contract, devices or {"cam-1": {"driver": model, "adb": {"serial": "serial-1"}}},
        transport, terminate_grace_s=Decimal("1"), monotonic_ns=clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", list(CameraModel))
async def test_control_applies_settings_before_one_actual_start(model):
    transport = Transport()
    request = _request(model)
    result = await _driver(model, transport).control(request)
    expected = (*settings_for(model, "camera_record", request.params), start_for(model, "camera_record"))
    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == [shlex.join(command) for command in expected]
    assert all(call.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T") for call in transport.calls)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure, call_count", [
    ({"mode": "manual", "iso": 800}, 12),
    ({"mode": "auto", "compensation_ev": 0}, 11),
])
async def test_action6_record_omitted_aperture_reaches_one_start_without_adjusting_aperture(exposure, call_count):
    request = _request()
    params = {**request.params, "exposure": exposure}
    del params["aperture"]
    request = replace(request, params=params)
    transport = Transport()

    result = await _driver(CameraModel.ACTION6, transport).control(request)

    scripts = [shlex.split(shlex.split(call.argv[5])[2]) for call in transport.calls]
    start = ["dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "02", "01"]
    assert len(scripts) == call_count
    assert scripts[-1] == start
    assert scripts.count(start) == 1
    assert not any(command[-2] == "0x26" for command in scripts)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure, aperture_call_number", [
    ({"mode": "manual", "iso": 800}, 11),
    ({"mode": "auto", "compensation_ev": 0}, 10),
])
@pytest.mark.parametrize("aperture, payload", [("f2.8", "1801"), ("f4.0", "9001")])
@pytest.mark.parametrize("response", [b"UNKNOWN", b"e3"])
async def test_action6_record_unconfirmed_explicit_aperture_blocks_start(
        exposure, aperture_call_number, aperture, payload, response):
    request = _request()
    request = replace(request, params={**request.params, "exposure": exposure, "aperture": aperture})
    transport = Transport(fail_at=aperture_call_number, fail_output=response)

    result = await _driver(CameraModel.ACTION6, transport).control(request)

    scripts = [shlex.split(shlex.split(call.argv[5])[2]) for call in transport.calls]
    assert len(scripts) == aperture_call_number
    assert scripts[-1] == ["dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "0x26", payload]
    assert not any(command[-2:] == ["02", "01"] for command in scripts)
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": "response_unconfirmed", "stage": "device", "details": {"response": response.decode()}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert not result.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure, call_count", [
    ({"mode": "manual", "iso": 800}, 11),
    ({"mode": "auto", "compensation_ev": 0}, 10),
])
async def test_action6_record_omitted_aperture_and_bitrate_reaches_one_start(exposure, call_count):
    request = _request()
    params = {**request.params, "exposure": exposure}
    del params["aperture"]
    del params["bitrate"]
    transport = Transport()

    result = await _driver(CameraModel.ACTION6, transport).control(replace(request, params=params))

    scripts = [shlex.split(shlex.split(call.argv[5])[2]) for call in transport.calls]
    start = ["dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "02", "01"]
    assert len(scripts) == call_count
    assert scripts[-1] == start
    assert scripts.count(start) == 1
    assert not any(command[-2] == "0x26" or command[0] == "simulate_device" for command in scripts)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure, bitrate_call_number", [
    ({"mode": "manual", "iso": 800}, 11),
    ({"mode": "auto", "compensation_ev": 0}, 10),
])
@pytest.mark.parametrize("bitrate, value", [("standard", "1"), ("high", "2")])
async def test_action6_record_unconfirmed_explicit_bitrate_blocks_start(exposure, bitrate_call_number, bitrate, value):
    request = _request()
    params = {**request.params, "exposure": exposure, "bitrate": bitrate}
    del params["aperture"]
    response = b"name [DeviceRecordRecSettingBitRate]\nlink to server rlt 0\nregister to server successs\n"
    transport = Transport(fail_at=bitrate_call_number, fail_output=response)

    result = await _driver(CameraModel.ACTION6, transport).control(replace(request, params=params))

    scripts = [shlex.split(shlex.split(call.argv[5])[2]) for call in transport.calls]
    assert len(scripts) == bitrate_call_number
    assert scripts[-1] == ["simulate_device", "-s", "bitrate", value]
    assert not any(command[-2:] == ["02", "01"] for command in scripts)
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": "response_unconfirmed", "stage": "device", "details": {"response": response.decode()}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert not result.observations


@pytest.mark.asyncio
async def test_action6_timelapse_applies_full_auto_sequence_before_one_start():
    transport = Transport()
    result = await _driver(CameraModel.ACTION6, transport).control(_timelapse_request())

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(ACTION6_AUTO_TIMELAPSE_SCRIPTS)
    assert all(call.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T") for call in transport.calls)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.error is None
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,exposure,outputs,expected", TIMELAPSE_D1_CASES)
async def test_timelapse_dispatch_uses_each_models_full_sequence_before_one_start(
        model, exposure, outputs, expected):
    checks = []
    transport = Transport()

    def check():
        checks.append(len(transport.calls))

    request = _candidate_timelapse_request(model, exposure, outputs, check=check)
    result = await _driver(model, transport).control(request)

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(expected)
    assert scripts.count("dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 01 01") == 1
    assert checks == list(range(len(expected)))
    assert [call.timeout_s for call in transport.calls] == [Decimal("20")] * len(expected)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,interval,duration,payload", TIMELAPSE_STABLE_FORMAT_CASES)
@pytest.mark.parametrize("narrow_field", ["interval_s", "duration_s"])
async def test_driver_dispatches_original_candidate_after_only_current_domain_narrows(
        monkeypatch, model, interval, duration, payload, narrow_field):
    outputs, exposure = "video", {"mode": "auto"}
    request = _candidate_timelapse_request(
        model, exposure, outputs, interval_s=interval, duration_s=duration)
    frozen_settings = settings_for(model, "camera_timelapse", request.params)
    frozen_payload = next(command[-1] for command in frozen_settings if command[-2] == "6c")
    assert frozen_payload == payload

    _narrow_time_domain(monkeypatch, narrow_field,
                        interval if narrow_field == "interval_s" else duration, model)
    with pytest.raises(BodySchemaError):
        settings_for(model, "camera_timelapse", request.params)

    transport = Transport()
    result = await _driver(model, transport).control(request)

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    expected = [shlex.join(command) for command in (*frozen_settings, start_for(model, "camera_timelapse"))]
    assert scripts == expected
    assert [command[-1] for command in (shlex.split(script) for script in scripts)
            if command[-2] == "6c"] == [payload]
    assert scripts.count(shlex.join(start_for(model, "camera_timelapse"))) == 1
    assert all(call.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T") for call in transport.calls)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation("start_confirmed", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting_number", range(1, 5))
async def test_action6_timelapse_unconfirmed_setting_preserves_error_and_blocks_later_commands(setting_number):
    transport = Transport(fail_at=setting_number)
    result = await _driver(CameraModel.ACTION6, transport).control(_timelapse_request())

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(ACTION6_AUTO_TIMELAPSE_SCRIPTS[:setting_number])
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": "response_unconfirmed", "stage": "device", "details": {"response": "UNKNOWN"}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert not result.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("model,exposure,outputs,expected", TIMELAPSE_D1_CASES)
async def test_timelapse_unconfirmed_setting_blocks_every_later_command_for_each_sequence(
        model, exposure, outputs, expected):
    for setting_number in range(1, len(expected)):
        transport = Transport(fail_at=setting_number)
        request = _candidate_timelapse_request(model, exposure, outputs)

        result = await _driver(model, transport).control(request)

        scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
        assert scripts == list(expected[:setting_number])
        assert result.outcome.status is AttemptStatus.FAILED
        assert result.outcome.effect is EffectState.NO_EFFECT
        assert result.error == {"code": "response_unconfirmed", "stage": "device",
                                "details": {"response": "UNKNOWN"}}
        assert not result.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("model,exposure,outputs,expected", TIMELAPSE_D1_CASES)
async def test_timelapse_cancel_at_each_dispatch_boundary_blocks_later_commands(
        model, exposure, outputs, expected):
    settings_count = len(expected) - 1
    for completed_settings in range(settings_count + 1):
        transport = Transport()
        checks = []

        def check():
            checks.append(len(transport.calls))
            return "canceled" if len(transport.calls) == completed_settings else None

        request = _candidate_timelapse_request(model, exposure, outputs, check=check)
        result = await _driver(model, transport).control(request)

        scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
        assert scripts == list(expected[:completed_settings])
        assert checks == list(range(completed_settings + 1))
        assert result.outcome.status is AttemptStatus.FAILED
        assert result.error["code"] == "canceled"
        assert result.error["stage"] == "dispatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,exposure,outputs,expected", TIMELAPSE_D1_CASES)
async def test_timelapse_remaining_deadline_at_each_dispatch_boundary_blocks_later_commands(
        model, exposure, outputs, expected):
    settings_count = len(expected) - 1
    for completed_settings in range(1, settings_count + 1):
        now = [0]

        def advance_clock(count):
            now[0] = (100_000_000_000 if count == completed_settings
                      else now[0] + 5_000_000_000)

        transport = Transport(after_call=advance_clock)
        request = replace(_candidate_timelapse_request(model, exposure, outputs),
                          timeout_s=Decimal("100"))
        result = await _driver(model, transport, clock=lambda: now[0]).control(request)

        scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
        assert scripts == list(expected[:completed_settings])
        assert [call.timeout_s for call in transport.calls] == [
            Decimal(100 - 5 * index) for index in range(completed_settings)
        ]
        assert result.outcome.status is AttemptStatus.FAILED
        assert result.error["code"] == "start_preparation_timeout"
        assert result.error["stage"] == "dispatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["canceled", "window_ended"])
@pytest.mark.parametrize("completed_settings", [2, 3])
async def test_action6_timelapse_eligibility_lost_before_or_after_preset_blocks_later_commands(reason, completed_settings):
    transport = Transport()
    request = _timelapse_request(check=lambda: reason if len(transport.calls) == completed_settings else None)
    result = await _driver(CameraModel.ACTION6, transport).control(request)

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(ACTION6_AUTO_TIMELAPSE_SCRIPTS[:completed_settings])
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": reason, "stage": "dispatch", "details": {}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert result.observations == (DeviceObservation("setting_applied", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_settings, expected_timeouts", [
    (2, [Decimal("20"), Decimal("15")]),
    (3, [Decimal("20"), Decimal("15"), Decimal("10")]),
])
async def test_action6_timelapse_deadline_exhausted_before_or_after_preset_blocks_later_commands(completed_settings, expected_timeouts):
    now = [0]
    def advance_clock(count):
        now[0] = 20_000_000_000 if count == completed_settings else now[0] + 5_000_000_000
    transport = Transport(after_call=advance_clock)
    result = await _driver(CameraModel.ACTION6, transport, clock=lambda: now[0]).control(_timelapse_request())

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(ACTION6_AUTO_TIMELAPSE_SCRIPTS[:completed_settings])
    assert [call.timeout_s for call in transport.calls] == expected_timeouts
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": "start_preparation_timeout", "stage": "dispatch", "details": {}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert result.observations == (DeviceObservation("setting_applied", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
async def test_action6_timelapse_unknown_start_is_not_repeated_or_followed_by_stop():
    transport = Transport(fail_at=5)
    result = await _driver(CameraModel.ACTION6, transport).control(_timelapse_request())

    scripts = [shlex.split(call.argv[5])[2] for call in transport.calls]
    assert scripts == list(ACTION6_AUTO_TIMELAPSE_SCRIPTS)
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.error == {"code": "response_unconfirmed", "stage": "device", "details": {"response": "UNKNOWN"}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert not result.observations


AUTO_TEMPLATE_DRIVER_CASES = (
    (CameraModel.ACTION6, "video"),
    (CameraModel.ACTION6, "video_raw"),
    (CameraModel.ACTION6, "video_jpeg"),
    (CameraModel.OSMO360II, "video"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,outputs", AUTO_TEMPLATE_DRIVER_CASES)
@pytest.mark.parametrize("damage", ["missing", "malformed"])
async def test_auto_candidate_template_failure_prevents_all_driver_calls(
        monkeypatch, model, outputs, damage):
    presets = camera_commands.TIMELAPSE_PRESETS[model]
    fixed = next(preset for preset in presets
                 if (preset.interval_s, preset.duration_s, preset.outputs.value, preset.exposure_mode)
                 == (8, 1800, outputs, "auto"))
    if damage == "missing":
        changed = tuple(preset for preset in presets if preset is not fixed)
        error = CapabilityNotDeclaredError
    else:
        malformed = replace(fixed, command=(*fixed.command[:-1], "not-hex"))
        changed = tuple(malformed if preset is fixed else preset for preset in presets)
        error = ValueError
    monkeypatch.setitem(camera_commands.TIMELAPSE_PRESETS, model, changed)
    request = _candidate_timelapse_request(model, {"mode": "auto"}, outputs)
    transport = Transport()

    with pytest.raises(error):
        await _driver(model, transport).control(request)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_malformed_timelapse_template_is_rejected_before_any_device_call(monkeypatch):
    model = CameraModel.ACTION6
    presets = camera_commands.TIMELAPSE_PRESETS[model]
    selected = next(preset for preset in presets
                    if (preset.interval_s, preset.duration_s, preset.outputs, preset.exposure_mode)
                    == (8, 1800, camera_commands.OutputSelection.VIDEO_RAW, "auto"))
    malformed = replace(selected, command=(*selected.command[:-1], "040003"))
    monkeypatch.setitem(camera_commands.TIMELAPSE_PRESETS, model, tuple(
        malformed if preset is selected else preset for preset in presets
    ))
    request = _timelapse_request()
    transport = Transport()

    with pytest.raises(ValueError):
        await _driver(model, transport).control(request)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_timelapse_rejects_interval_beyond_replacement_window_before_any_device_call():
    request = _candidate_timelapse_request(
        CameraModel.ACTION6, {"mode": "auto"}, "video", interval_s=6554, duration_s=10800)
    transport = Transport()

    with pytest.raises(ValueError):
        await _driver(CameraModel.ACTION6, transport).control(request)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_setting_error_prevents_start_and_keeps_actual_return():
    transport = Transport(fail_at=2)
    result = await _driver(CameraModel.ACTION6, transport).control(_request())
    assert len(transport.calls) == 2
    assert result.outcome.status is AttemptStatus.FAILED and result.outcome.effect is EffectState.NO_EFFECT
    assert result.error == {"code": "response_unconfirmed", "stage": "device", "details": {"response": "UNKNOWN"}}
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert not result.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["canceled", "window_ended"])
async def test_settings_completed_then_dispatch_check_prevents_actual_start(reason):
    request = _request(check=lambda: reason if transport.calls else None)
    transport = Transport()
    result = await _driver(CameraModel.ACTION6, transport).control(request)
    assert len(transport.calls) == 1
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error["code"] == reason and result.error["stage"] == "dispatch"
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert result.observations == (DeviceObservation("setting_applied", 1, {"activity_id": "7"}),)


@pytest.mark.asyncio
async def test_prevented_before_any_setting_has_no_actual_call():
    transport = Transport()
    result = await _driver(CameraModel.ACTION6, transport).control(_request(check=lambda: "canceled"))
    assert not transport.calls
    assert result.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.call_info is None and not result.observations


@pytest.mark.asyncio
async def test_unknown_actual_start_is_not_sent_again():
    request = _request()
    count = len(settings_for(CameraModel.ACTION6, "camera_record", request.params))
    transport = Transport(fail_at=count + 1)
    result = await _driver(CameraModel.ACTION6, transport).control(request)
    assert len(transport.calls) == count + 1
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.status is AttemptStatus.FAILED


@pytest.mark.asyncio
async def test_settings_consume_original_complete_deadline():
    now = [0]
    transport = Transport(after_call=lambda _count: now.__setitem__(0, now[0] + 12_000_000_000))
    result = await _driver(CameraModel.ACTION6, transport, clock=lambda: now[0]).control(_request())
    assert [call.timeout_s for call in transport.calls] == [Decimal("20"), Decimal("8")]
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error["code"] == "start_preparation_timeout"
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED


@pytest.mark.asyncio
async def test_same_driver_resolves_each_original_device_serial():
    model, transport = CameraModel.ACTION6, Transport()
    driver = _driver(model, transport, devices={
        "cam-1": {"driver": model, "adb": {"serial": "serial-1"}},
        "cam-2": {"driver": model, "adb": {"serial": "serial-2"}}})
    await driver.control(_request(device_id="cam-2"))
    assert {call.argv[2] for call in transport.calls} == {"serial-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", [None, {"driver": "different", "adb": {"serial": "serial-2"}},
    {"driver": CameraModel.ACTION6}, {"driver": CameraModel.ACTION6, "adb": {"serial": ""}}])
async def test_unavailable_original_binding_causes_no_device_call(declaration):
    transport = Transport()
    driver = _driver(CameraModel.ACTION6, transport, devices={"cam-1": declaration})
    with pytest.raises(DeviceConfigurationError):
        await driver.control(_request())
    assert not transport.calls


@pytest.mark.asyncio
async def test_digest_uses_its_full_file_deadline_and_original_file_identity():
    from camctl.devices.evidence import EvidenceContract
    digest_contract = EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id")
    expected = "a" * 64
    class DigestTransport(Transport):
        async def run(self, spec, stop):
            self.calls.append(spec)
            return RawToolOutcome(LocalExit(exit_code=0), (expected + "  -\n").encode(), None, None, stderr=b"")
    transport = DigestTransport()
    driver = _driver(CameraModel.ACTION6, transport)
    sizes = []
    contract = replace(driver.contract, evidence=EVIDENCE.with_contract(digest_contract),
        digest_timeout_s=lambda size: sizes.append(size) or Decimal("240"))
    driver = AdbCameraDriver(contract, {"cam-1": {"driver": CameraModel.ACTION6,
        "adb": {"serial": "serial-1"}, "result_check": {"call_timeout_s": Decimal("0.01")}}},
        transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    binding = DeviceBinding("cam-1", CameraModel.ACTION6)
    result = await driver.digest(ControlRequest("digest", binding, {
        "file_id": "9", "identity_key": '["cam-1","dji-action6","/DCIM/source.mp4"]',
        "size_bytes": 100_000_000, "locator": {"path": "/DCIM/source.mp4"}}))
    assert sizes == [100_000_000]
    assert transport.calls[0].timeout_s == Decimal("240")
    assert result.error is None and result.observations == (
        DeviceObservation("file_digest", 1, {"file_id": "9", "sha256": expected}),)


@pytest.mark.parametrize("timeout", [None, Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), 10])
@pytest.mark.asyncio
async def test_invalid_digest_deadline_does_not_call_device(timeout):
    transport = Transport()
    original = _driver(CameraModel.ACTION6, transport)
    contract = replace(original.contract, digest_timeout_s=lambda _size: timeout,
        evidence=EVIDENCE.with_contract(EvidenceContract("file_digest", 1, "digest",
            frozenset({"file_id", "sha256"}), identity_field="file_id")))
    driver = AdbCameraDriver(contract, original.devices, transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    binding = DeviceBinding("cam-1", CameraModel.ACTION6)
    with pytest.raises(ValueError):
        await driver.digest(ControlRequest("digest", binding, {"file_id": "9", "size_bytes": 5,
            "identity_key": '["cam-1","dji-action6","/DCIM/source.mp4"]', "locator": {"path": "/DCIM/source.mp4"}}))
    assert not transport.calls
