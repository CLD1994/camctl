"""Action6 录像响应分类；确认语义来自限定录像的运行假设。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import EffectState
from camctl.operations.process import LocalExit, RawToolOutcome


ACK = b"[dji_mb_ctrl] read global seq: 17\nResp message, len = 1, data:\n  00\n\n"
SIMULATION_LOG = (
    b"name [DeviceRecordRecSettingBitRate]\n"
    b"link to server rlt 0\nregister to server successs\n"
)


def _interpreter(kind=CameraCall.START, operation="start_recording", activity_id="7"):
    from camctl.devices.drivers.adb_cameras.responses import Action6DjiInterpreter

    return Action6DjiInterpreter(kind, operation, activity_id)


def _raw(output=ACK):
    return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")


@pytest.mark.parametrize("kind, operation, observation", [
    (CameraCall.SETTING, "start_recording", "setting_applied"),
    (CameraCall.START, "start_recording", "start_confirmed"),
    (CameraCall.STOP, "stop_recording", "stop_confirmed"),
])
@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_complete_single_zero_ack_confirms_only_its_recording_phase(kind, operation, observation, line_ending):
    raw = _raw(ACK.replace(b"\n", line_ending))

    result = _interpreter(kind, operation).interpret(raw)

    assert result.error is None
    assert result.effect is EffectState.CONFIRMED
    assert result.observations == (DeviceObservation(observation, 1, {
        "activity_id": "7", "response_payload": "00",
        "confirmation_basis": "action6_record_ack/v1",
    }),)


@pytest.mark.parametrize("output, reason, details", [
    (b"", "response_missing", {"response_count": 0}),
    (b"Send message, data: 00\n", "response_missing", {"response_count": 0}),
    (ACK + ACK, "response_multiple", {"response_count": 2}),
    (ACK + b"Resp message, len = 1, data:\n e3\n", "response_multiple", {"response_count": 2}),
    (b"Resp message, len = 1, data:\n 0", "response_malformed", {"response_count": 1}),
    (b"Resp message, len = 1, data:\n", "response_length_mismatch", {
        "response_count": 1, "declared_length": 1, "response_payload": "",
    }),
    (b"Resp message, len = 2, data:\n 00\n", "response_length_mismatch", {
        "response_count": 1, "declared_length": 2, "response_payload": "00",
    }),
    (b"Resp message, len = 1, data:\n 00 e3\n", "response_length_mismatch", {
        "response_count": 1, "declared_length": 1, "response_payload": "00e3",
    }),
    (b"Resp message, len = 1, data:\n zz\n", "response_malformed", {"response_count": 1}),
    (ACK + b"unexpected trailer\n", "response_malformed", {"response_count": 1}),
    (b"Resp message, len = 1, data:\n e3\n", "response_payload_unconfirmed", {
        "response_count": 1, "declared_length": 1, "response_payload": "e3",
    }),
    (b"Resp message, len = 1, data:\n 01\n", "response_payload_unconfirmed", {
        "response_count": 1, "declared_length": 1, "response_payload": "01",
    }),
    (b"Resp message, len = 2, data:\n 00 e3\n", "response_payload_unconfirmed", {
        "response_count": 1, "declared_length": 2, "response_payload": "00e3",
    }),
    (b"Resp message, len = 0, data:\n", "response_payload_unconfirmed", {
        "response_count": 1, "declared_length": 0, "response_payload": "",
    }),
])
def test_unconfirmed_response_keeps_actual_partition_without_device_facts(output, reason, details):
    result = _interpreter().interpret(_raw(output))

    assert result.effect is EffectState.UNKNOWN
    assert result.observations == ()
    assert result.error.code == "action6_response_unconfirmed"
    assert result.error.stage == "device_response"
    assert result.error.details["reason"] == reason
    for name, expected in details.items():
        assert result.error.details[name] == expected


@pytest.mark.parametrize("changes, reason, details", [
    ({"exit": None}, "exit_missing", {}),
    ({"exit": LocalExit(exit_code=7)}, "exit_nonzero", {"local_exit_code": 7}),
    ({"exit": LocalExit(signal=15)}, "process_signalled", {"local_signal": 15}),
    ({"stderr": None}, "stderr_unavailable", {"stderr_hex": None}),
    ({"stderr": b"warning\n"}, "stderr_present", {"stderr_hex": "7761726e696e670a"}),
    ({"stderr": b"\xff"}, "stderr_present", {"stderr_hex": "ff"}),
    ({"output": None}, "stdout_unavailable", {}),
    ({"output_failure": "output_limit_exceeded"}, "output_incomplete", {
        "output_failure": "output_limit_exceeded",
    }),
    ({"error": "timeout", "used_grace_s": Decimal("1")}, "transport_error", {"transport_error": "timeout"}),
    ({"error": "cancelled", "used_grace_s": Decimal("1")}, "transport_error", {"transport_error": "cancelled"}),
    ({"error": "output_failed", "used_grace_s": Decimal("1"), "output_failure": "read_failed"}, "transport_error", {
        "transport_error": "output_failed", "output_failure": "read_failed",
    }),
])
def test_call_failure_with_visible_zero_ack_does_not_confirm_recording(changes, reason, details):
    result = _interpreter().interpret(replace(_raw(), **changes))

    assert result.effect is EffectState.UNKNOWN
    assert result.observations == ()
    assert result.error.code == "action6_response_unconfirmed"
    assert result.error.stage == "device_response"
    assert result.error.details["reason"] == reason
    for name, expected in details.items():
        assert result.error.details[name] == expected


def test_simultaneous_call_and_response_errors_keep_each_actual_input():
    raw = RawToolOutcome(LocalExit(exit_code=7), b"Resp message, len = 1, data:\n e3\n",
        "timeout", Decimal("1"), output_failure="read_failed", stderr=b"failure\n")

    result = _interpreter().interpret(raw)

    assert result.effect is EffectState.UNKNOWN
    assert result.observations == ()
    assert result.error.details["reason"] == "transport_error"
    assert result.error.details["transport_error"] == "timeout"
    assert result.error.details["output_failure"] == "read_failed"
    assert result.error.details["local_exit_code"] == 7
    assert result.error.details["stderr_hex"] == "6661696c7572650a"
    assert result.error.details["response_count"] == 1
    assert result.error.details["declared_length"] == 1
    assert result.error.details["response_payload"] == "e3"


@pytest.mark.parametrize("kind, operation", [
    (CameraCall.RESULT, "result"), (CameraCall.QUERY, "query"), (CameraCall.DELETE, "delete"),
    (CameraCall.START, "start_timelapse"), (CameraCall.STOP, "stop_timelapse"),
    (CameraCall.SETTING, "start_timelapse"), (CameraCall.SETTING, "stop_recording"),
    (CameraCall.START, "stop_recording"), (CameraCall.STOP, "start_recording"),
])
def test_interpreter_rejects_unsupported_operation_or_phase(kind, operation):
    with pytest.raises(ValueError):
        _interpreter(kind, operation)


@pytest.mark.parametrize("activity_id", [None, "", "0", "07", "unknown", "9223372036854775808"])
def test_interpreter_rejects_invalid_activity_identity(activity_id):
    with pytest.raises(ValueError):
        _interpreter(activity_id=activity_id)


@pytest.mark.parametrize("output, exit_code", [(SIMULATION_LOG, 0), (SIMULATION_LOG, 1), (b"", 0)])
def test_simulation_property_and_service_logs_do_not_confirm_bitrate(output, exit_code):
    from camctl.devices.drivers.adb_cameras.responses import Action6UnconfirmedSettingInterpreter

    result = Action6UnconfirmedSettingInterpreter().interpret(replace(_raw(output), exit=LocalExit(exit_code=exit_code)))

    assert result.effect is EffectState.UNKNOWN
    assert result.observations == ()
    assert result.error.code == "action6_setting_unconfirmed"
    assert result.error.stage == "device_settings"
    assert result.error.details["reason"] == "confirmation_unavailable"
    assert result.error.details["stdout_hex"] == output.hex()
    assert result.error.details["local_exit_code"] == exit_code
