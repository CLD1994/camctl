"""已保存成功检查由原机身、期限和完整工具事实独立核对。"""

from copy import deepcopy
from decimal import Decimal
from enum import IntEnum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from camctl import resources
from camctl.contracts import enums
from camctl.devices.evidence import DeviceObservation
from camctl.operations import result_format
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.tool_diagnostics import ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState

_driver = ModuleType("camctl.devices.drivers.hasselblad.definitions")
_driver.DRIVER_ID = "hasselblad_cfv100c"
_responses = ModuleType("camctl.devices.drivers.hasselblad.responses")
_responses.parse_devices = lambda data: [SimpleNamespace(vendor_id=0x2756, product_id=10)]
_responses.parse_serial = lambda data: "CAMERA-1"
_responses.doctor_passed = lambda stdout, stderr: stdout == b"doctor: PASS\n" and not stderr
with patch.dict(sys.modules, {_driver.__name__: _driver, _responses.__name__: _responses}), patch.object(
        resources, "resource_bytes", side_effect=AssertionError("单元测试不能读取包资源")), patch.object(
        enums, "resource_bytes", side_effect=AssertionError("单元测试不能读取包资源")):
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    source = find_spec("camctl.persistence.repositories.availability_observation")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_availability", source.origin)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    require_saved_availability = module.require_saved_availability


@pytest.fixture
def facts(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    driver = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    driver.DRIVER_ID = "hasselblad_cfv100c"
    monkeypatch.setitem(sys.modules, driver.__name__, driver)
    definitions = {
        "operation_attempts.status": IntEnum("Status", {"SUCCEEDED": 2}),
        "operation_attempts.effect_state": IntEnum("Effect", {"NO_EFFECT": 2}),
    }
    monkeypatch.setattr(result_format, "enum_for", lambda column: definitions[column])
    identity = CameraIdentity("CAMERA-1", 10)
    check = {"id": 31, "action_id": 7, "driver_id": driver.DRIVER_ID, "rule_version": 1,
             "expected_identity_json": {"serial": identity.serial, "product_id": identity.product_id},
             "resource_key": identity.resource_key}
    run = {"id": 3, "action_id": 7, "availability_check_id": 31}
    names = ("list", "info", "doctor")
    outputs = (b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n", b"serial : CAMERA-1\n", b"doctor: PASS\n")
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
        "finished_monotonic_ns": 3_000_000_000, "steps": steps})
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("availability_returned", 1, {})),
        observations=(observation,), diagnostics=ToolDiagnostics(3, calls))
    attempt = {"run_id": 3, "status": 2, "effect_state": 2, "timeout_s_json": Decimal("4"),
               "result_json": result_format.result_document(outcome), "error_json": None}
    return attempt, run, check


@pytest.mark.parametrize("fault", [None, "check", "run", "binding", "deadline", "identity", "doctor"])
def test_success_requires_original_complete_evidence(facts, fault):
    attempt, run, check = deepcopy(facts)
    if fault == "check":
        check["id"] = 32
    elif fault == "run":
        attempt["run_id"] = 4
    elif fault == "binding":
        check["resource_key"] = "wrong"
    elif fault == "deadline":
        attempt["timeout_s_json"] = Decimal("3")
    elif fault == "identity":
        attempt["result_json"]["observations"][0]["data"]["observed_serial"] = "OTHER"
    elif fault == "doctor":
        attempt["result_json"]["diagnostics"]["calls"][2]["stdout"]["hex"] = b"doctor: FAIL\n".hex()
    if fault is None:
        require_saved_availability(attempt, run, check)
    else:
        with pytest.raises(ValueError):
            require_saved_availability(attempt, run, check)
