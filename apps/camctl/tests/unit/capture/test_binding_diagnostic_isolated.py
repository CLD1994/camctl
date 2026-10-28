"""准备绑定错误只在真实业务结果成立后交付诊断。"""

import asyncio
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import itertools
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camctl import resources


@pytest.fixture
def preparation_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    def stub(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
        return module
    stub("camctl.capture.baseline", BaselinePreparation=object, PreparationPhase=object)
    stub("camctl.capture.models", ActivityReleaseSave=object)
    stub("camctl.contracts.enums", enum_for=lambda name: SimpleNamespace(PREPARE_CAPTURE=1))
    keys = itertools.count(1)
    stub("camctl.contracts.values", ConsistencyError=ValueError, OperationKey=int, new_operation_key=lambda: next(keys))
    stub("camctl.devices.bindings", DeviceBinding=object)
    stub("camctl.devices.capture_preparation", CAPTURE_ACTION_TYPES=frozenset({1}),
         capture_operation=object(), preparation_lead_s=object())
    stub("camctl.devices.ports", CapturePreparationDriver=object, ControlRequest=object, DeviceCallResult=object)
    stub("camctl.devices.evidence", EvidenceRegistry=object)
    stub("camctl.operations.attempts", **{name: type(name, (), {}) for name in
         ("AttemptConfig", "AttemptFinish", "AttemptIntent", "AttemptTarget", "OperationKind", "RunFinish", "RunOutcome")})
    class ErrorValue:
        def __init__(self, code, stage, details): self.code, self.stage, self.details = code, stage, details
    stub("camctl.operations.models", ErrorValue=ErrorValue, **{name: type(name, (), {}) for name in
         ("AttemptStatus", "AttemptTicket", "CallOutcome", "EffectState", "EvidenceValue", "Settlement", "SettlementBasis")})
    stub("camctl.operations.owned_calls", owned_tool_call=lambda function: function)
    stub("camctl.operations.diagnostic_controls", is_capture_control_error=lambda *args: False)
    class State: pass
    stub("camctl.logging_runtime.diagnostics", DiagnosticState=State, record_call_outcome=lambda *args, **kwargs: None,
         record_error=lambda *args, **kwargs: None)
    stub("camctl.logging_runtime.events", measure_stage=object())
    stub("camctl.operations.validation", validate_outcome=object())
    class SharedSave:
        def __init__(self): self.result = None
        async def run(self, callback):
            if self.result is None: self.result = await callback()
            return self.result
        def retry_failed(self): pass
    async def settle(awaitable): return await awaitable
    stub("camctl.persistence.business", SharedSave=SharedSave, settle_required=settle)
    stub("camctl.persistence.models", db_failure=lambda *args: ValueError(), DbOutcomeKind=SimpleNamespace(COMPLETED=1),
         db_outcome_for=lambda error: None)
    stub("camctl.persistence.repositories.operations", OperationRepository=object)
    stub("camctl.persistence.repositories.capture", CaptureRepository=object)
    stub("camctl.persistence.repositories.scheduling", SchedulingRepository=object)
    stub("camctl.persistence.transaction", row_facts=object())
    source = find_spec("camctl.capture.preparation")
    spec = spec_from_file_location("camctl.capture._isolated_binding_diagnostic", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("outcome,reason,expected", [
    ("failed", None, True), ("already", "saved", True),
    ("already", None, False), ("rejected", "canceled", False), ("rejected", "not_eligible", False),
])
def test_binding_diagnostic_follows_actual_saved_outcome(preparation_module, monkeypatch, outcome, reason, expected):
    module = preparation_module
    delivered, seen = [], set()
    def consume(error, *, stage, context, state):
        if id(state) not in seen:
            seen.add(id(state))
            delivered.append((error.code, error.details, context))
    monkeypatch.setattr(module, "record_error", consume)
    value = SimpleNamespace(outcome=SimpleNamespace(value=outcome), reason=reason)
    database = SimpleNamespace(write=AsyncMock(return_value=SimpleNamespace(kind=1, value=value)))
    scheduling = SimpleNamespace(fail_binding_preparation=object())
    runtime = SimpleNamespace(database=database, preparation_cache=module.RuntimePreparationCache(),
                              operations=object(), scheduling=scheduling, capture=object())
    request = SimpleNamespace(details={"device_id": "camera", "expected_driver_id": "hb", "reason": "physical_identity_mismatch"})
    async def run():
        await module._end(runtime, 1, "fail_binding_preparation", request)
        if expected:
            await module._end(runtime, 1, "fail_binding_preparation", request)
    asyncio.run(run())
    assert len(delivered) == int(expected)
    if expected:
        assert delivered[0] == ("device_binding_unavailable", request.details, {"action_id": 1})
