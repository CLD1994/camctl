"""相机启动组合按一个原尝试执行，设置事实与拍摄事实分别解释。"""

from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError
from camctl.devices.drivers.adb_cameras.commands import CameraModel, settings_for, start_for
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
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


class Parser:
    def __init__(self, kind, target):
        self.kind, self.target = kind, target

    def interpret(self, raw):
        if raw.output != b"OK":
            return InterpretedFacts((), ErrorValue("response_unconfirmed", "device", {"response": raw.output.decode()}), EffectState.UNKNOWN)
        observation = SETTING if self.kind is CameraCall.SETTING else STARTED
        return InterpretedFacts((DeviceObservation(observation.type, 1, {"activity_id": self.target}),), None, EffectState.CONFIRMED)


class Transport:
    def __init__(self, *, fail_at=None, after_call=None):
        self.calls = []
        self.fail_at, self.after_call = fail_at, after_call

    async def run(self, spec, stop):
        self.calls.append(spec)
        if self.after_call is not None:
            self.after_call(len(self.calls))
        return RawToolOutcome(LocalExit(exit_code=0),
            b"UNKNOWN" if len(self.calls) == self.fail_at else b"OK", None, None, stderr=b"")


def _params(model):
    if model is CameraModel.ACTION6:
        return {"type": "action6_record", "duration_s": 10, "resolution": "4k30", "fov": "wide",
                "stabilization": "off", "aperture": "f2.8", "bitrate": "high",
                "exposure": {"mode": "auto", "compensation_ev": 0}}
    return {"type": "osmo360ii_record", "duration_s": 10,
            "exposure": {"mode": "auto", "compensation_ev": 0}}


def _request(model=CameraModel.ACTION6, *, check=lambda: None, device_id="cam-1"):
    return ControlRequest("start_recording", DeviceBinding(device_id, model), _params(model),
        AttemptTicket(1, "control", "7", "start/3", 9), Decimal("20"), dispatch_check=check)


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
