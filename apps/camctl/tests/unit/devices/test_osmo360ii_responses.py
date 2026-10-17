"""OSMO 响应只在原命令与操作范围内提供控制事实。"""

from dataclasses import replace

import pytest

from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.operations.models import EffectState
from camctl.operations.process import LocalExit, RawToolOutcome


def _raw(payload="00"):
    return RawToolOutcome(LocalExit(exit_code=0),
        f"Resp message, len = 1, data:\n {payload}\n".encode(), None, None, stderr=b"")


def _parser(kind=CameraCall.START, operation="start_timelapse", command=("dji_mb_ctrl", "-c", "01", "01")):
    from camctl.devices.drivers.adb_cameras.osmo360ii_responses import Osmo360IIDjiInterpreter
    return Osmo360IIDjiInterpreter(kind, operation, "7", command)


@pytest.mark.parametrize("kind,operation,command,payload,observation", [
    (CameraCall.SETTING, "start_timelapse", ("dji_mb_ctrl", "-c", "1e", "0000"), "ee", "setting_applied"),
    (CameraCall.STOP, "stop_timelapse", ("dji_mb_ctrl", "-c", "01", "00"), "df", "stop_confirmed"),
    (CameraCall.START, "start_timelapse", ("dji_mb_ctrl", "-c", "01", "01"), "00", "timelapse_sent"),
    (CameraCall.START, "start_recording", ("dji_mb_ctrl", "-c", "02", "01"), "00", "start_confirmed"),
    (CameraCall.STOP, "stop_recording", ("dji_mb_ctrl", "-c", "02", "00"), "00", "stop_confirmed"),
])
def test_command_scoped_response_records_actual_payload_and_control_fact(kind, operation, command, payload, observation):
    result = _parser(kind, operation, command).interpret(_raw(payload))
    assert result.error is None and result.effect is EffectState.CONFIRMED
    assert [(item.type, item.data["response_payload"]) for item in result.observations] == [(observation, payload)]
    assert result.observations[0].data["activity_id"] == "7"
    assert result.observations[0].data["confirmation_basis"].startswith("osmo360ii_")


@pytest.mark.parametrize("kind,operation,command,payload", [
    (CameraCall.SETTING, "start_recording", ("dji_mb_ctrl", "-c", "1e", "0000"), "ee"),
    (CameraCall.SETTING, "start_timelapse", ("dji_mb_ctrl", "-c", "1e", "0100"), "ee"),
    (CameraCall.START, "start_timelapse", ("dji_mb_ctrl", "-c", "01", "01"), "ee"),
    (CameraCall.STOP, "stop_recording", ("dji_mb_ctrl", "-c", "01", "00"), "df"),
    (CameraCall.STOP, "stop_timelapse", ("dji_mb_ctrl", "-c", "02", "00"), "df"),
])
def test_special_payload_cannot_cross_command_or_action_scope(kind, operation, command, payload):
    result = _parser(kind, operation, command).interpret(_raw(payload))
    assert result.error is not None and result.observations == ()
    assert result.effect is EffectState.UNKNOWN


@pytest.mark.parametrize("changes", [
    {"exit": None}, {"exit": LocalExit(exit_code=1)}, {"exit": LocalExit(signal=15)},
    {"stderr": None}, {"stderr": b"warning"}, {"output": None},
    {"error": "timeout"}, {"output_failure": "output_limit_exceeded"},
    {"output": b"Resp message, len = 2, data:\n 00\n"},
    {"output": b"Resp message, len = 1, data:\n 0\n"},
    {"output": b"Resp message, len = 1, data:\n 00\ntrailer"},
    {"output": _raw().output * 2}, {"output": b""},
])
def test_incomplete_or_failed_call_never_confirms_control(changes):
    result = _parser().interpret(replace(_raw(), **changes))
    assert result.error is not None and result.observations == ()
    assert result.effect is EffectState.UNKNOWN


def test_simulation_exit_accepts_command_without_claiming_parameter_readback():
    from camctl.devices.drivers.adb_cameras.osmo360ii_responses import Osmo360IISimulationInterpreter
    parser = Osmo360IISimulationInterpreter("7")
    result = parser.interpret(replace(_raw(), output=b"service connected\n"))
    assert result.error is None and result.effect is EffectState.CONFIRMED
    assert [item.type for item in result.observations] == ["setting_command_accepted"]


@pytest.mark.parametrize("changes", [{"exit": LocalExit(exit_code=1)}, {"stderr": b"error"},
    {"output_failure": "read_failed"}, {"error": "cancelled"}])
def test_failed_simulation_cannot_advance_settings(changes):
    from camctl.devices.drivers.adb_cameras.osmo360ii_responses import Osmo360IISimulationInterpreter
    result = Osmo360IISimulationInterpreter("7").interpret(replace(_raw(), **changes))
    assert result.error is not None and result.observations == ()


@pytest.mark.parametrize("kind,operation", [("start", "start_timelapse"),
    (CameraCall.RESULT, "result"), (CameraCall.START, "stop_timelapse"),
    (CameraCall.STOP, "start_recording")])
def test_interpreter_requires_typed_phase_and_matching_operation(kind, operation):
    with pytest.raises(ValueError):
        _parser(kind, operation)


@pytest.mark.parametrize("operation,basis", [("start_recording", "osmo360ii_record_ack/v1"),
    ("start_timelapse", "osmo360ii_timelapse_response/v1")])
def test_response_basis_preserves_original_action_scope(operation, basis):
    result = _parser(operation=operation).interpret(_raw())
    assert result.observations[0].data["confirmation_basis"] == basis
