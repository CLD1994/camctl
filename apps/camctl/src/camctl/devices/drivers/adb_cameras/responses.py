"""Action6 普通录像的响应解释与运行假设标识。"""

from dataclasses import dataclass
from enum import StrEnum
import re

from camctl.contracts.json_values import JsonValue
from camctl.contracts.values import parse_object_id
from camctl.devices.adb_transport import InterpretedFacts
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.operations.models import EffectState, ErrorValue
from camctl.operations.process import RawToolOutcome
from .contracts import CameraCall


_ACK_FIELDS = frozenset({"activity_id", "response_payload", "confirmation_basis"})
ACTION6_RECORD_ACK_BASIS = "action6_record_ack/v1"
_ACK_PHASES = {
    CameraCall.SETTING: ("start_recording", EvidenceContract(
        "setting_applied", 1, "control", _ACK_FIELDS, identity_field="activity_id")),
    CameraCall.START: ("start_recording", EvidenceContract(
        "start_confirmed", 1, "control", _ACK_FIELDS, identity_field="activity_id")),
    CameraCall.STOP: ("stop_recording", EvidenceContract(
        "stop_confirmed", 1, "stop", _ACK_FIELDS, identity_field="activity_id")),
}
ACTION6_RECORD_OBSERVATIONS = tuple(contract for _, contract in _ACK_PHASES.values())


class _FailureReason(StrEnum):
    TRANSPORT_ERROR = "transport_error"
    OUTPUT_INCOMPLETE = "output_incomplete"
    EXIT_MISSING = "exit_missing"
    PROCESS_SIGNALLED = "process_signalled"
    EXIT_NONZERO = "exit_nonzero"
    STDERR_UNAVAILABLE = "stderr_unavailable"
    STDERR_PRESENT = "stderr_present"
    STDOUT_UNAVAILABLE = "stdout_unavailable"
    RESPONSE_MISSING = "response_missing"
    RESPONSE_MULTIPLE = "response_multiple"
    RESPONSE_MALFORMED = "response_malformed"
    RESPONSE_LENGTH_MISMATCH = "response_length_mismatch"
    RESPONSE_PAYLOAD_UNCONFIRMED = "response_payload_unconfirmed"
    CONFIRMATION_UNAVAILABLE = "confirmation_unavailable"


def _call_details(raw: RawToolOutcome) -> dict[str, JsonValue]:
    return {
        "local_exit_code": None if raw.exit is None else raw.exit.exit_code,
        "local_signal": None if raw.exit is None else raw.exit.signal,
        "transport_error": raw.error,
        "output_failure": raw.output_failure,
        "stderr_hex": None if raw.stderr is None else raw.stderr.hex(),
    }


def _call_failure(raw: RawToolOutcome) -> _FailureReason | None:
    if raw.error is not None:
        return _FailureReason.TRANSPORT_ERROR
    if raw.output_failure is not None:
        return _FailureReason.OUTPUT_INCOMPLETE
    if raw.exit is None:
        return _FailureReason.EXIT_MISSING
    if raw.exit.signal is not None:
        return _FailureReason.PROCESS_SIGNALLED
    if raw.exit.exit_code != 0:
        return _FailureReason.EXIT_NONZERO
    if raw.stderr is None:
        return _FailureReason.STDERR_UNAVAILABLE
    if raw.stderr != b"":
        return _FailureReason.STDERR_PRESENT
    if raw.output is None:
        return _FailureReason.STDOUT_UNAVAILABLE
    return None


def _response_failure(output: bytes | None, details: dict[str, JsonValue]) -> _FailureReason | None:
    if output is None:
        details["response_count"] = None
        return _FailureReason.STDOUT_UNAVAILABLE
    count = output.count(b"Resp message,")
    details["response_count"] = count
    if count == 0:
        return _FailureReason.RESPONSE_MISSING
    if count != 1:
        return _FailureReason.RESPONSE_MULTIPLE
    block = output.split(b"Resp message,", 1)[1]
    match = re.fullmatch(rb"\s*len = (0|[1-9][0-9]*), data:\s*([0-9a-fA-F \t\r\n]*)", block)
    if match is None:
        details["response_fragment_hex"] = block.hex()
        return _FailureReason.RESPONSE_MALFORMED
    try:
        declared_length = int(match[1])
        details["declared_length"] = declared_length
        payload = bytes.fromhex(match[2].decode("ascii"))
    except ValueError:
        details["response_fragment_hex"] = block.hex()
        return _FailureReason.RESPONSE_MALFORMED
    details["response_payload"] = payload.hex()
    if len(payload) != declared_length:
        return _FailureReason.RESPONSE_LENGTH_MISMATCH
    if payload != b"\x00":
        return _FailureReason.RESPONSE_PAYLOAD_UNCONFIRMED
    return None


@dataclass(frozen=True)
class Action6DjiInterpreter:
    kind: CameraCall
    operation: str
    activity_id: str

    def __post_init__(self):
        if not isinstance(self.kind, CameraCall) or self.kind not in _ACK_PHASES:
            raise ValueError("Action6 响应解释器只接受录像设置、开始和停止")
        operation, _ = _ACK_PHASES[self.kind]
        if self.operation != operation:
            raise ValueError("Action6 响应阶段与普通录像操作不符")
        parse_object_id(self.activity_id)

    def interpret(self, raw: RawToolOutcome) -> InterpretedFacts:
        details = _call_details(raw)
        response_failure = _response_failure(raw.output, details)
        details["response_reason"] = None if response_failure is None else response_failure.value
        failure = _call_failure(raw) or response_failure
        if failure is not None:
            details["reason"] = failure.value
            return InterpretedFacts((), ErrorValue(
                "action6_response_unconfirmed", "device_response", details), EffectState.UNKNOWN)
        _, contract = _ACK_PHASES[self.kind]
        observation = DeviceObservation(contract.type, contract.version, {
            "activity_id": self.activity_id, "response_payload": "00",
            "confirmation_basis": ACTION6_RECORD_ACK_BASIS,
        })
        return InterpretedFacts((observation,), None, EffectState.CONFIRMED)


class Action6UnconfirmedSettingInterpreter:
    def interpret(self, raw: RawToolOutcome) -> InterpretedFacts:
        details = _call_details(raw)
        details.update(reason=_FailureReason.CONFIRMATION_UNAVAILABLE.value,
                       stdout_hex=None if raw.output is None else raw.output.hex())
        return InterpretedFacts((), ErrorValue(
            "action6_setting_unconfirmed", "device_settings", details), EffectState.UNKNOWN)
