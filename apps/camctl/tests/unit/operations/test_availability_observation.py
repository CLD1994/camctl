from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.tool_diagnostics import ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState
from camctl.persistence.repositories.availability_observation import require_available


def _result():
    outputs = (b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n", b"serial : CAMERA-1\n", b"doctor: PASS\n")
    names = ("list", "info", "doctor")
    calls = tuple(ToolCallOutput(i, name, ToolStream(ToolStreamState.COMPLETE, output),
                                 ToolStream(ToolStreamState.COMPLETE, b""))
                  for i, (name, output) in enumerate(zip(names, outputs), 1))
    steps = [{"name": name, "returned_monotonic_ns": i * 1_000_000_000,
              "exit": {"exit_code": 0, "signal": None}, "error": None,
              "used_grace_s": None, "signal_failures": [], "output_failure": None}
             for i, name in enumerate(names, 1)]
    observation = DeviceObservation("device_availability", 1, {
        "check_id": "31", "expected_serial": "CAMERA-1", "expected_product_id": 10,
        "observed_serial": "CAMERA-1", "started_monotonic_ns": 0,
        "finished_monotonic_ns": 3_000_000_000, "steps": steps,
    })
    return CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("availability_returned", 1, {})),
        observations=(observation,), diagnostics=ToolDiagnostics(3, calls))


def test_complete_observation_is_accepted():
    require_available(_result(), 31, CameraIdentity("CAMERA-1", 10), Decimal("4"))


@pytest.mark.parametrize("failure", ["deadline", "doctor", "identity", "exit", "boolean_exit", "order", "multiple", "version"])
def test_forged_success_is_rejected(failure):
    outcome = _result()
    observation = outcome.observations[0]
    data = dict(observation.data)
    steps = [dict(step) for step in data["steps"]]
    calls = list(outcome.diagnostics.calls)
    if failure == "doctor":
        calls[2] = replace(calls[2], stdout=ToolStream(ToolStreamState.COMPLETE, b"doctor: FAIL\n"))
    elif failure == "identity":
        data["observed_serial"] = "OTHER"
    elif failure == "exit":
        steps[2]["exit"] = {"exit_code": 1, "signal": None}
    elif failure == "boolean_exit":
        steps[2]["exit"] = {"exit_code": False, "signal": None}
    elif failure == "order":
        steps[1]["returned_monotonic_ns"] = 4_000_000_000
    elif failure == "multiple":
        calls[0] = replace(calls[0], stdout=ToolStream(ToolStreamState.COMPLETE,
            calls[0].stdout.data * 2))
    data["steps"] = steps
    outcome = replace(outcome, observations=(replace(observation, data=data,
                          version=True if failure == "version" else observation.version),),
                      diagnostics=replace(outcome.diagnostics, calls=tuple(calls)))
    with pytest.raises(ValueError):
        require_available(outcome, 31, CameraIdentity("CAMERA-1", 10),
                          Decimal("3") if failure == "deadline" else Decimal("4"))
