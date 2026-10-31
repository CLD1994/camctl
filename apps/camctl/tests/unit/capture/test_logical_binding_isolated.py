"""准备入口只读取隔离替身提供的固定身份事实。"""

import asyncio
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl import resources


@pytest.fixture
def binding_entry(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    def stub(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
        return module
    stub("camctl.persistence.models", db_failure=object(), DbOutcomeKind=object())
    stub("camctl.contracts.enums", enum_for=lambda name: None)
    stub("camctl.contracts.values", ConsistencyError=ValueError, new_operation_key=object())
    stub("camctl.devices.bindings", binding_failure_details=lambda result: None)
    stub("camctl.devices.ports", ControlRequest=object)
    stub("camctl.operations.attempts", **{name: object for name in
         ("AttemptIntent", "AttemptTarget", "BeginDisposition", "QueryPurpose", "RunOutcome", "StaleRunFinish")},
         OperationKind=SimpleNamespace(QUERY_ACTIVITY=1, STOP_RESIDUAL=2))
    stub("camctl.operations.models", **{name: object for name in
         ("AttemptStatus", "CallOutcome", "EffectState", "ErrorValue", "EvidenceValue", "Settlement", "SettlementBasis")})
    stub("camctl.operations.owned_calls", owned_tool_call=lambda f: f)
    stub("camctl.persistence.repositories.scheduling", ExpireActionRequest=object, ExpireOutcome=object)
    stub("camctl.persistence.repositories.capture", FinishResidualBindingFailure=object, RecordingFailure=object)
    transaction = stub("camctl.persistence.transaction", row_facts=lambda *args: None)
    stub("camctl.scheduling.rules", WindowPhase=object, window_phase=object())
    stub("camctl.outputs.read_discovery", DEFAULT_BATCH_SIZE=128, ReadDiscoveryOwner=object)
    stub("camctl.persistence.repositories.operations", _ATTEMPT_STATUS=object(), _EFFECT_STATE=object())
    stub("camctl.capture.handlers", _binding=lambda action: action)
    stub("camctl.devices.drivers.hasselblad.definitions", DRIVER_ID="hasselblad_cfv100c")
    identity_api = stub("camctl.devices.drivers.hasselblad.identity")
    class Identity:
        def __init__(self, serial, product_id):
            self.serial, self.product_id = serial, product_id
        def __eq__(self, other):
            return isinstance(other, Identity) and (self.serial, self.product_id) == (other.serial, other.product_id)
        @property
        def resource_key(self): return self.serial
    identity_api.CameraIdentity = Identity
    alias = stub("camctl.persistence.repositories.physical_alias", physical_alias_conflict=Mock(return_value=False))
    target = stub("camctl.persistence.repositories.availability_target", logical_held_identity_mismatch=Mock(return_value=False),
                  photo_check_identity_mismatch=Mock(return_value=False))
    stub("camctl.persistence.repositories.availability_wait_finish", saved_binding_check_failure=lambda *args: None)
    source = find_spec("camctl.capture.residual")
    spec = spec_from_file_location("camctl.capture._isolated_logical_binding", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, Identity, transaction, alias, target


@pytest.mark.parametrize("mismatch,conflict,reason", [
    (True, False, "physical_identity_mismatch"), (False, True, "physical_identity_conflict"),
    (False, False, None),
])
def test_new_action_classifies_old_logical_identity_before_any_call(binding_entry, mismatch, conflict, reason):
    module, Identity, _, alias, target = binding_entry
    target.logical_held_identity_mismatch.return_value = mismatch
    alias.physical_alias_conflict.return_value = conflict
    class Database:
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None))))
    device_call = Mock(side_effect=AssertionError("不能调用设备"))
    runtime = SimpleNamespace(binding_check=lambda binding: object(), fixed_camera_identity=Identity("new", 10),
                              database=Database(), device_call=device_call)
    action = {"id": 2, "device_id": "hb0", "driver_id": "hasselblad_cfv100c", "type": 1,
              "status": 1, "execution_started": 0}
    result = asyncio.run(module._original_binding_failure(runtime, action))
    assert (None if result is None else result["reason"]) == reason
    device_call.assert_not_called()


def test_existing_activity_uses_its_own_fixed_identity(binding_entry):
    module, Identity, transaction, _, target = binding_entry
    transaction.row_facts = lambda *args: {"expected_identity_json": {"serial": "old", "product_id": 10},
                                           "resource_key": "old"}
    class Database:
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (3,)))))
    runtime = SimpleNamespace(binding_check=lambda binding: object(), fixed_camera_identity=Identity("new", 10),
                              database=Database())
    action = {"id": 2, "device_id": "hb0", "driver_id": "hasselblad_cfv100c", "type": 1,
              "status": 1, "execution_started": 0}
    assert asyncio.run(module._original_binding_failure(runtime, action))["reason"] == "physical_identity_mismatch"
    target.logical_held_identity_mismatch.assert_not_called()


def test_saved_check_failure_precedes_restored_configuration(binding_entry, monkeypatch):
    module, Identity, _, _, _ = binding_entry
    details = {"device_id": "hb0", "expected_driver_id": "hasselblad_cfv100c", "reason": "physical_identity_mismatch"}
    sys.modules["camctl.persistence.repositories.availability_wait_finish"].saved_binding_check_failure = (
        lambda *args: (details, {"serial": "replacement", "product_id": 10}))
    class Database:
        async def read(self, callback, **kwargs): return callback(SimpleNamespace(connection=object()))
    binding_check = Mock(side_effect=AssertionError("已保存原失败必须优先"))
    runtime = SimpleNamespace(database=Database(), binding_check=binding_check, fixed_camera_identity=Identity("original", 10))
    action = {"id": 2, "device_id": "hb0", "driver_id": "hasselblad_cfv100c", "type": 1,
              "status": 1, "execution_started": 0}
    assert asyncio.run(module._original_binding_failure(runtime, action)) == details
    binding_check.assert_not_called()
