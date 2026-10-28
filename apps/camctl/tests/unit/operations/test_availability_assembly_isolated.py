"""业务候选接到共同检查入口，隔离驱动、状态库与包资源。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl import resources


@pytest.fixture
def assembly(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    modules = {}
    def stub(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        modules[name] = module

    @dataclass(frozen=True)
    class Identity:
        serial: str
        product_id: int

    @dataclass(frozen=True)
    class Binding:
        device_id: str
        driver_id: str

    class Progress(Enum):
        READY = 1
        WAIT = 2

    @dataclass(frozen=True)
    class Request:
        action_id: int
        identity: Identity
        trusted_wall_now: int
        prepare_lead_s: Decimal
        occurred_at: int
        trigger_copy_id: int | None = None

    class Files:
        @staticmethod
        def from_source(identity, locator):
            return SimpleNamespace(serial="serial", product_id=10, binding=Binding("camera", "hb"))

    stub("camctl.bootstrap.capture_assembly", _hasselblad_identity=lambda value: Identity("serial", 10),
         query_attempt_config=lambda value: "query-config")
    stub("camctl.devices.bindings", DeviceBinding=Binding)
    stub("camctl.devices.drivers.hasselblad.definitions", DRIVER_ID="hb")
    stub("camctl.devices.drivers.hasselblad.files", CameraFileIdentity=Files)
    stub("camctl.devices.drivers.hasselblad.identity", CameraIdentity=Identity)
    stub("camctl.operations.availability", AvailabilityProgress=Progress)
    stub("camctl.persistence.repositories.availability_create", CreateAvailabilityCheck=Request)
    stub("camctl.persistence.repositories.retry_wait_reads", read_reservation_blocks_control=lambda connection, device: True)
    stub("camctl.persistence.transaction", row_facts=lambda connection, table, identity: connection.source)
    stub("camctl.capture.preparation", effective_preparation_lead_s=lambda runtime, action: Decimal("4"))
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    source = find_spec("camctl.bootstrap.availability_assembly")
    spec = spec_from_file_location("camctl.bootstrap._isolated_availability_assembly", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    entry = SimpleNamespace(driver=object(), evidence=object())
    coordinator = SimpleNamespace(request=AsyncMock(return_value=Progress.WAIT))
    instance = module.AvailabilityAssembly(coordinator, {"camera": {"driver": "hb"}},
        SimpleNamespace(entry=lambda driver: entry), {"camera": Decimal("4")})
    return instance, coordinator, entry, Identity, Binding, Progress


@pytest.mark.asyncio
async def test_capture_requests_check_before_new_activity(assembly):
    instance, coordinator, entry, Identity, Binding, Progress = assembly
    connection = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None))
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=connection))))
    runtime = SimpleNamespace(database=database, fixed_camera_identity=Identity("serial", 10),
        query_config="query-config", wall_us=lambda: 100)
    action = {"id": 1, "device_id": "camera", "driver_id": "hb", "type": 1, "status": 1}

    assert await instance.capture(runtime, action) is Progress.WAIT
    args = coordinator.request.await_args.args
    assert args[1].action_id == 1 and args[1].prepare_lead_s == Decimal("4")
    assert args[2:] == ("query-config", entry.driver, entry.evidence, Binding("camera", "hb"))


@pytest.mark.asyncio
async def test_device_read_requests_same_coordinator_with_fixed_source(assembly):
    instance, coordinator, entry, Identity, Binding, Progress = assembly
    connection = SimpleNamespace(source={"identity_key": "source", "locator_json": {}})
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=connection))))
    runtime = SimpleNamespace(database=database, occurred_at=lambda: 100)
    candidate = SimpleNamespace(driver_id="hb", device_id="camera", held=False,
                                source_file_id=12, action_id=3, copy_id=9)

    assert await instance.read(runtime, candidate) is Progress.WAIT
    args = coordinator.request.await_args.args
    assert args[1].trigger_copy_id == 9 and args[1].identity == Identity("serial", 10)
    assert args[2:] == ("query-config", entry.driver, entry.evidence, Binding("camera", "hb"))


@pytest.mark.asyncio
async def test_existing_read_does_not_request_new_check(assembly):
    instance, coordinator, _, _, _, Progress = assembly
    candidate = SimpleNamespace(driver_id="hb", device_id="camera", held=True)
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=None))))

    assert await instance.read(SimpleNamespace(database=database), candidate) is Progress.READY
    coordinator.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_settled_reserved_read_can_request_check(assembly, monkeypatch):
    instance, coordinator, _, _, _, Progress = assembly
    retry = sys.modules["camctl.persistence.repositories.retry_wait_reads"]
    monkeypatch.setattr(retry, "read_reservation_blocks_control", lambda connection, device: False)
    connection = SimpleNamespace(source={"identity_key": "source", "locator_json": {}})
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=connection))))
    runtime = SimpleNamespace(database=database, occurred_at=lambda: 100)
    candidate = SimpleNamespace(driver_id="hb", device_id="camera", held=True,
                                source_file_id=12, action_id=3, copy_id=9)

    assert await instance.read(runtime, candidate) is Progress.WAIT
    assert coordinator.request.await_args.args[1].trigger_copy_id == 9
