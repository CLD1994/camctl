"""OSMO 命令响应的限定解释；命令接受与参数回读分别表达。"""

from dataclasses import dataclass
import re

from camctl.contracts.values import parse_object_id
from camctl.devices.adb_transport import InterpretedFacts
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.devices.tasks import StartReturn
from camctl.operations.models import EffectState, ErrorValue
from .contracts import CameraCall
from .responses import STOP_RESPONSE_PAYLOADS, _call_details, _call_failure, _simulation_call_failure


_FIELDS = frozenset({"activity_id", "response_payload", "confirmation_basis"})
OSMO360II_OBSERVATIONS = tuple(EvidenceContract(name, 1, operation, _FIELDS, identity_field="activity_id")
    for name, operation in (("setting_applied", "control"), ("setting_command_accepted", "control"),
        ("start_confirmed", "control"), ("recording_sent", "control"), ("timelapse_sent", "control"), ("stop_confirmed", "stop")))


def _response(output, details):
    if output is None:
        return "stdout_unavailable"
    count = output.count(b"Resp message,")
    details["response_count"] = count
    if count != 1:
        return "response_missing" if count == 0 else "response_multiple"
    block = output.split(b"Resp message,", 1)[1]
    match = re.fullmatch(rb"\s*len = (0|[1-9][0-9]*), data:\s*([0-9a-fA-F \t\r\n]*)", block)
    if match is None:
        details["response_fragment_hex"] = block.hex()
        return "response_malformed"
    try:
        details["declared_length"] = int(match[1])
        payload = bytes.fromhex(match[2].decode("ascii"))
    except ValueError:
        details["response_fragment_hex"] = block.hex()
        return "response_malformed"
    details["response_payload"] = payload.hex()
    if len(payload) != details["declared_length"]:
        return "response_length_mismatch"
    return None


def _failure(details, reason, *, stage="device_response"):
    details["reason"] = reason
    return InterpretedFacts((), ErrorValue("osmo360ii_response_unconfirmed", stage, details), EffectState.UNKNOWN)


@dataclass(frozen=True)
class Osmo360IIDjiInterpreter:
    kind: CameraCall
    operation: str
    activity_id: str
    command: tuple[str, ...]
    start_return_meaning: int | None = None

    def __post_init__(self):
        operations = ({"stop_recording", "stop_timelapse"} if self.kind is CameraCall.STOP
                      else {"start_recording", "start_timelapse"})
        if (not isinstance(self.kind, CameraCall)
                or self.kind not in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP)
                or self.operation not in operations):
            raise ValueError("OSMO 响应阶段与拍摄操作不符")
        if not self.command or self.command[0] != "dji_mb_ctrl":
            raise ValueError("OSMO 响应要求原明确的 dji_mb_ctrl 命令")
        parse_object_id(self.activity_id)

    def interpret(self, raw):
        details = _call_details(raw)
        response_failure = _response(raw.output, details)
        details["response_reason"] = response_failure
        call_failure = _call_failure(raw)
        if call_failure is not None or response_failure is not None:
            return _failure(details, call_failure.value if call_failure is not None else response_failure)
        accepted = {"00"}
        # 设置响应按原生延时命令限定；停止响应对应原操作的停止命令。
        if (self.operation == "start_timelapse" and self.kind is CameraCall.SETTING
                and self.command[-3:] == ("-c", "1e", "0000")):
            accepted.add("ee")
        stop_command = ("-c", "02", "00") if self.operation == "stop_recording" else ("-c", "01", "00")
        if self.kind is CameraCall.STOP and self.command[-3:] == stop_command:
            accepted = STOP_RESPONSE_PAYLOADS
        if details["response_payload"] not in accepted:
            return _failure(details, "response_payload_unconfirmed")
        name = ("setting_applied" if self.kind is CameraCall.SETTING else "stop_confirmed"
                if self.kind is CameraCall.STOP else "timelapse_sent"
                if self.operation == "start_timelapse" else "start_confirmed")
        if name == "start_confirmed" and self.start_return_meaning == StartReturn.SENT:
            name = "recording_sent"
        observation = DeviceObservation(name, 1, {"activity_id": self.activity_id,
            "response_payload": details["response_payload"], "confirmation_basis":
                "osmo360ii_record_ack/v1" if self.operation.endswith("recording")
                else "osmo360ii_timelapse_response/v1"})
        return InterpretedFacts((observation,), None, EffectState.CONFIRMED)


@dataclass(frozen=True)
class Osmo360IISimulationInterpreter:
    activity_id: str

    def __post_init__(self):
        parse_object_id(self.activity_id)

    def interpret(self, raw):
        details = _call_details(raw)
        failure = _simulation_call_failure(raw)
        if failure is not None:
            return _failure(details, failure.value, stage="device_settings")
        observation = DeviceObservation("setting_command_accepted", 1, {"activity_id": self.activity_id,
            "response_payload": None, "confirmation_basis": "osmo360ii_simulate_device_exit/v1"})
        return InterpretedFacts((observation,), None, EffectState.CONFIRMED)
