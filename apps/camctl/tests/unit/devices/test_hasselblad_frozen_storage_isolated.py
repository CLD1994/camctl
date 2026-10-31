"""准备凭据必须保留原受理的来源模式，导入时隔离资源协作者。"""
import importlib.util
import sys
from enum import IntEnum
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest


@pytest.fixture
def cache_api(monkeypatch):
    class Actions(IntEnum):
        CAMERA_TAKE_PHOTO = 1
        CAMERA_RECORD = 2
        CAMERA_TIMELAPSE = 3
    modules = {
        "camctl.contracts.enums": {"enum_for": lambda name: Actions},
        "camctl.contracts.json_values": {"json_equal": lambda a, b: a == b},
        "camctl.devices.bindings": {"DeviceBinding": object},
        "camctl.devices.evidence": {"EvidenceContract": lambda *args, **kwargs: object()},
        "camctl.devices.ports": {"ControlRequest": object},
        "camctl.operations.models": {"AttemptTicket": object},
    }
    for name, attributes in modules.items():
        stub = ModuleType(name)
        stub.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, stub)
    name = "camctl.devices._isolated_capture_preparation"
    source = Path(__file__).parents[3] / "src/camctl/devices/capture_preparation.py"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module



def test_prepared_capture_cannot_change_frozen_storage(cache_api):
    cache = cache_api.PreparedCaptureCache()
    binding = SimpleNamespace(device_id="camera")
    prepared = SimpleNamespace(operation="control", target_id="7")
    actual = SimpleNamespace(operation="control", target_id="7")
    request = SimpleNamespace(binding=binding, operation="take_photo", params={}, ticket=prepared,
        output_storage="host")
    cache.remember(request, "camera-serial")
    started = SimpleNamespace(binding=binding, operation="take_photo", params={}, ticket=actual,
        prepared_by=prepared, output_storage="device")
    assert not cache.matches(started, "camera-serial")


def test_prepared_capture_matches_same_frozen_storage(cache_api):
    cache = cache_api.PreparedCaptureCache()
    binding = SimpleNamespace(device_id="camera")
    prepared = SimpleNamespace(operation="control", target_id="7")
    actual = SimpleNamespace(operation="control", target_id="7")
    cache.remember(SimpleNamespace(binding=binding, operation="take_photo", params={},
        ticket=prepared, output_storage="device"), "camera-serial")
    started = SimpleNamespace(binding=binding, operation="take_photo", params={}, ticket=actual,
        prepared_by=prepared, output_storage="device")
    assert cache.matches(started, "camera-serial")
