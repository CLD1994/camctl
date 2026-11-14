"""可用性绑定恢复仅使用替身，不读取真实包资源。"""

import asyncio
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from camctl import resources


@pytest.fixture
def assembly_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    def stub(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
        return module
    config_read = Mock(side_effect=AssertionError("原失败必须使用原次机身身份"))
    stub("camctl.bootstrap.capture_assembly", _device_attempts=object(), _hasselblad_identity=config_read,
         query_attempt_config=lambda declaration: SimpleNamespace(max_attempts=3))
    stub("camctl.devices.bindings", DeviceBinding=lambda device, driver: (device, driver),
         binding_failure_details=lambda result: result.details)
    stub("camctl.devices.drivers.hasselblad.definitions", DRIVER_ID="hb")
    class Identity:
        def __init__(self, serial, product_id): self.serial, self.product_id = serial, product_id
        @property
        def resource_key(self): return self.serial
    stub("camctl.devices.drivers.hasselblad.identity", CameraIdentity=Identity)
    stub("camctl.devices.drivers.hasselblad.files", camera_file_identity_from_source=lambda *args:
         SimpleNamespace(serial="source", product_id=10, binding=("camera", "hb")))
    stub("camctl.operations.availability", AvailabilityProgress=object())
    class Request:
        def __init__(self, action_id, identity, now, lead, occurred, trigger_copy_id=None, **kwargs):
            self.action_id, self.identity, self.trigger_copy_id = action_id, identity, trigger_copy_id
    stub("camctl.persistence.repositories.availability_create", CreateAvailabilityCheck=Request)
    stub("camctl.persistence.repositories.transactions", row_facts=lambda connection, table, ident: {"identity_key": "key", "locator_json": {}})
    wait = stub("camctl.persistence.repositories.availability_wait_finish")
    source = find_spec("camctl.bootstrap.availability_assembly")
    spec = spec_from_file_location("camctl.bootstrap._isolated_availability_assembly", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, wait, config_read


@pytest.mark.parametrize("declaration", [None, {"driver": "other"}, {"driver": "hb", "identity": "changed"}])
def test_saved_conflict_keeps_original_identity_even_if_current_configuration_changed(assembly_module, declaration):
    module, wait, config_read = assembly_module
    details = {"device_id": "camera", "expected_driver_id": "hb", "reason": "physical_identity_conflict"}
    original = {"serial": "source", "product_id": 10}
    wait.saved_binding_check_failure = lambda *args, **kwargs: (details, original)
    coordinator = SimpleNamespace(finish_binding=AsyncMock(return_value=True))
    assembly = module.AvailabilityAssembly(coordinator, {} if declaration is None else {"camera": declaration},
                                           SimpleNamespace(entry=lambda name: SimpleNamespace(evidence=object())), {})
    class Database:
        async def read(self, callback, **kwargs): return callback(SimpleNamespace(connection=object()))
    runtime = SimpleNamespace(database=Database(), occurred_at=lambda: 10)
    candidate = SimpleNamespace(source_file_id=7, action_id=1, copy_id=8, device_id="camera")
    result = SimpleNamespace(details=details, current_identity=None)
    assert asyncio.run(assembly.finish_read_binding(runtime, candidate, result))
    assert coordinator.finish_binding.await_args.args[-1] == original
    config_read.assert_not_called()


@pytest.mark.parametrize("reason", ["missing", "mismatch"])
def test_missing_current_identity_still_discovers_prior_failed_photo_check(assembly_module, monkeypatch, reason):
    module, _, _ = assembly_module
    monkeypatch.setattr(module, "read_binding_checks_page",
                        lambda owned, request, *, after=0: SimpleNamespace(entries=((9, 7, None),), next_after=None))
    transaction = sys.modules["camctl.persistence.repositories.transactions"]
    transaction.row_facts = lambda connection, table, ident: {"expected_identity_json": {"serial": "old", "product_id": 10}}
    monkeypatch.setattr(module, "row_facts", transaction.row_facts)
    preparation = ModuleType("camctl.capture.preparation")
    preparation.effective_preparation_lead_s = lambda *args: 0
    monkeypatch.setitem(sys.modules, preparation.__name__, preparation)
    coordinator = SimpleNamespace(finish_binding=AsyncMock(return_value=False))
    assembly = module.AvailabilityAssembly(coordinator, {},
        SimpleNamespace(entry=lambda name: SimpleNamespace(evidence=object())), {})
    class Database:
        async def read(self, callback, **kwargs): return callback(SimpleNamespace(connection=object()))
    runtime = SimpleNamespace(database=Database(), wall_us=lambda: 10, fixed_camera_identity=None,
                              query_config=SimpleNamespace(max_attempts=1))
    action = {"id": 1, "device_id": "camera"}
    details = {"device_id": "camera", "expected_driver_id": "hb", "reason": reason}
    assert not asyncio.run(assembly.finish_capture_binding(runtime, action, details))
    request = coordinator.finish_binding.await_args.args[1]
    assert (request.identity.serial, request.identity.product_id) == ("old", 10)


def test_session_shares_one_owner_and_adapts_driver_cancellation(assembly_module, monkeypatch):
    module, _, _ = assembly_module
    availability = sys.modules["camctl.operations.availability"]
    owners = []

    class Coordinator:
        def __init__(self, **kwargs):
            self.options = kwargs
            owners.append(self)

        def pending(self, action_id):
            return action_id == 8

        def required_settlements(self):
            return 1

        def stop_new_work(self):
            self.stopped = True

        async def settle(self):
            self.settled = True

    availability.AvailabilityCoordinator = Coordinator
    driver = ModuleType("camctl.devices.drivers.hasselblad.driver")

    class AvailabilityCancelled(asyncio.CancelledError):
        def __init__(self, result):
            self.result = result

    driver.AvailabilityCancelled = AvailabilityCancelled
    monkeypatch.setitem(sys.modules, driver.__name__, driver)
    storage = ModuleType("camctl.persistence.repositories.availability_storage")
    storage.AvailabilityStorage = lambda: object()
    monkeypatch.setitem(sys.modules, storage.__name__, storage)
    registry = SimpleNamespace(entry=lambda name: SimpleNamespace(driver=SimpleNamespace(default_prepare_lead_s=3)))
    session = module.AvailabilitySession({"camera": {"driver": "hb"}}, registry,
        monotonic_ns=lambda: 0, occurred_at=lambda: 1, recovery_boundary=None, recovery_max_event_id=None)
    assert owners == [session.coordinator]
    assert session.preparation_leads == {"camera": 3}
    assert session.pending(8) and session.required_settlements() == 1
    result = object()
    assert session.coordinator.options["cancelled_result"](AvailabilityCancelled(result)) is result
    assert session.coordinator.options["cancelled_result"](asyncio.CancelledError()) is None
    session.stop_new_work()
    asyncio.run(session.settle())
    assert session.coordinator.stopped and session.coordinator.settled
