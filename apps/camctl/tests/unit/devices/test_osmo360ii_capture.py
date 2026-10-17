"""同一 OSMO 合同提供录像与有限原生延时，不缩减公共时间参数。"""

from decimal import Decimal
import shlex

import pytest

from camctl.acceptance.schema import validate_precise
from camctl.devices.tasks import CompletionMode, EndControl, StartReturn
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome


def _contract():
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
    return osmo360ii_contract()


def test_two_capabilities_have_preview_and_real_device_ports():
    contract = _contract()
    assert {cap.action_type for cap in contract.capabilities() if callable(cap.task_factory)} == {
        "camera_record", "camera_timelapse"}
    assert all(cap.preview_supported for cap in contract.capabilities())
    ports = contract.declaration
    assert ports.control_supported and ports.stop_supported and ports.result_supported
    assert ports.directory_supported and ports.read_supported and ports.digest_supported
    assert not ports.query_supported and not ports.delete_supported


def test_record_task_requires_one_nonzero_osv_and_independent_stop_wait():
    task = _contract().task_factories["osmo360ii_record"]({"duration_s": 12})
    assert task.target_duration_s == 12 and task.stop_supported is True
    assert task.file_completion_wait_s == Decimal("30")
    assert task.output_scope == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
    assert task.ownership_mode == 2
    assert task.product_rules == ({"kind": "video", "format_id": "osv", "min_count": 1,
        "exact_count": 1, "require_pairing": False, "min_size_bytes": 1},)


def test_timelapse_task_freezes_natural_wait_and_finite_cancel_policy():
    task = _contract().task_factories["osmo360ii_timelapse"]({"duration_s": 300})
    assert task.target_duration_s == 300 and task.stop_supported is True
    assert task.end_control is EndControl.DEVICE and task.start_return_meaning is StartReturn.SENT
    assert task.completion_mode is CompletionMode.TIME_AND_OUTPUTS
    assert task.duration_based is True and task.wait_after_send is True
    assert task.file_completion_wait_s == task.result_wait_margin_s == Decimal("30")
    assert task.cancel_completion_policy == "finite_device_end"
    assert task.product_rules == ({"kind": "video", "format_id": "osv", "min_count": 1,
        "exact_count": None, "require_pairing": False, "min_size_bytes": 1},)


def test_formal_timelapse_keeps_all_134_valid_time_combinations():
    cap = next(cap for cap in _contract().capabilities() if cap.action_type == "camera_timelapse")
    count = 0
    for duration in (300, 600, 1200, 1800, 3600, 7200, 10800, 18000):
        for interval in (1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 60, 120, 300, 1800, 3600):
            if interval < duration:
                params = {"type": "osmo360ii_timelapse", "duration_s": duration,
                    "interval_s": interval, "outputs": "video", "exposure": {"mode": "auto"}}
                validate_precise(cap.schema, params)
                assert cap.task_factory(params).target_duration_s == duration
                count += 1
    assert count == 134


class Transport:
    def __init__(self, fail_at=None, stop_payload="00"):
        self.commands, self.specs = [], []
        self.fail_at, self.stop_payload = fail_at, stop_payload

    async def run(self, spec, stop):
        command = shlex.split(shlex.split(spec.argv[5])[2])
        self.commands.append(command)
        self.specs.append(spec)
        payload = ("ee" if command[-3:] == ["-c", "1e", "0000"] else self.stop_payload
            if command[-3:] in (["-c", "01", "00"], ["-c", "02", "00"]) else "00")
        output = (b"service connected\n" if command[0] == "simulate_device" else
            f"Resp message, len = 1, data:\n {payload}\n".encode())
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None,
            stderr=b"setting failed" if len(self.commands) == self.fail_at else b"")


def _driver(transport):
    return AdbCameraDriver(_contract(), {"camera": {"driver": "dji-osmo360-ii",
        "adb": {"serial": "serial-1"}}}, transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)


def _request(action="record", mode="manual", dispatch_check=lambda: None):
    params = {"type": "osmo360ii_" + action, "duration_s": 300,
        "exposure": {"mode": mode, **({"iso": 800} if mode == "manual" else
            {"compensation_ev": 0} if action == "record" else {})}}
    if action == "timelapse":
        params.update(interval_s=1, outputs="video")
    return ControlRequest("start_recording" if action == "record" else "start_timelapse",
        DeviceBinding("camera", "dji-osmo360-ii"), params,
        AttemptTicket(1, "control", "7", "start/7", 9), Decimal("10"), dispatch_check)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,mode,count,observation,start", [
    ("record", "manual", 11, "start_confirmed", ["02", "01"]),
    ("record", "auto", 10, "start_confirmed", ["02", "01"]),
    ("timelapse", "manual", 10, "timelapse_sent", ["01", "01"]),
    ("timelapse", "auto", 7, "timelapse_sent", ["01", "01"]),
])
async def test_real_driver_contract_dispatches_settings_and_one_scoped_start(action, mode, count, observation, start):
    transport = Transport()
    result = await _driver(transport).control(_request(action, mode))
    assert result.error is None and result.outcome.status is AttemptStatus.SUCCEEDED
    assert [item.type for item in result.observations] == [observation]
    assert len(transport.commands) == count
    assert sum(command[-2:] == start for command in transport.commands) == 1
    assert transport.commands[-1][-2:] == start
    assert all(spec.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
        and Decimal("0") < spec.timeout_s <= Decimal("10") for spec in transport.specs)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,fail_at", [("record", index) for index in range(1, 11)]
    + [("timelapse", index) for index in range(1, 7)])
async def test_each_failed_setting_prevents_every_later_setting_and_start(action, fail_at):
    transport = Transport(fail_at)
    result = await _driver(transport).control(_request(action, "manual" if action == "record" else "auto"))
    assert result.error is not None and result.outcome.status is AttemptStatus.FAILED
    assert len(transport.commands) == fail_at
    assert not any(command[-3:] in (["-c", "02", "01"], ["-c", "01", "01"])
        for command in transport.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,cancel_after", [("record", 0), ("record", 1), ("record", 10),
    ("timelapse", 0), ("timelapse", 1), ("timelapse", 6)])
async def test_dispatch_cancellation_prevents_subsequent_start(action, cancel_after):
    transport = Transport()
    check = lambda: "canceled" if len(transport.commands) >= cancel_after else None
    result = await _driver(transport).control(_request(action,
        "manual" if action == "record" else "auto", check))
    assert result.error is not None and len(transport.commands) == cancel_after
    assert not any(command[-3:] in (["-c", "02", "01"], ["-c", "01", "01"])
        for command in transport.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,payload,accepted", [("stop_timelapse", "00", True),
    ("stop_timelapse", "df", True), ("stop_recording", "00", True), ("stop_recording", "df", False)])
async def test_stop_factory_uses_actual_scoped_command(operation, payload, accepted):
    transport = Transport(stop_payload=payload)
    request = ControlRequest(operation, DeviceBinding("camera", "dji-osmo360-ii"), {},
        AttemptTicket(1, "stop", "7", "stop/7", 9), Decimal("10"))
    result = await _driver(transport).stop(request)
    assert (result.error is None) is accepted
    assert [item.type for item in result.observations] == (["stop_confirmed"] if accepted else [])
