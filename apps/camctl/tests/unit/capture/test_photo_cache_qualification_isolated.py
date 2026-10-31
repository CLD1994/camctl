"""照片缓存资格与设备读取预算的纯格式约束。"""
import importlib.util
import sys
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def api(monkeypatch):
    values = ModuleType("camctl.contracts.values")
    values.MAX_OBJECT_ID = 2**63 - 1
    def object_id(value):
        if type(value) is not int or not 0 < value <= values.MAX_OBJECT_ID: raise ValueError()
        return value
    values.ObjectId, values.UtcMicros = object_id, lambda value: value
    bindings = ModuleType("camctl.devices.bindings")
    bindings.BindingResult = object
    bindings.BindingStatus = Enum("BindingStatus", "DEVICE_MISSING DRIVER_MISMATCH UNAVAILABLE")
    paths = ModuleType("camctl.host_files.paths"); paths.validate_file_extension = lambda *args, **kwargs: None
    diagnostics = ModuleType("camctl.logging_runtime.diagnostics"); diagnostics.DiagnosticState = type("DiagnosticState", (), {})
    for stub in (values, bindings, paths, diagnostics): monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).parents[3] / "src/camctl/outputs/qualification.py"
    name = "camctl.outputs._isolated_photo_cache_qualification"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def candidate(api, **changes):
    values = dict(action_id=1, item_id=2, processing_id=None, output_id=3,
        source_device_file_id=None, source_intermediate_file_id=4, target_extension="part",
        delivery_extension="jpg", delivery_display_name="image.jpg",
        config=api.OperationConfig(3, Decimal(10), Decimal(1)), occurred_at=100,
        reuse_photo_page_event_id=5)
    values.update(changes)
    return api.FileCandidate(**values)


def test_photo_cache_preserves_device_fallback_budget(api):
    saved = candidate(api)
    assert saved.reuse_photo_page_event_id == 5 and saved.config.max_attempts == 3


def test_sync_cache_uses_the_original_start_event(api):
    saved = candidate(api, reuse_photo_page_event_id=None, reuse_photo_start_event_id=8)
    assert saved.reuse_photo_start_event_id == 8 and saved.config.max_attempts == 3


def test_cache_cannot_claim_both_source_events(api):
    with pytest.raises(ValueError):
        candidate(api, reuse_photo_start_event_id=8)


@pytest.mark.parametrize("changes", [
    {"reuse_photo_page_event_id": None},
    {"reuse_inspection_id": 6},
    {"source_device_file_id": 7, "source_intermediate_file_id": None},
])
def test_photo_cache_requires_its_own_exclusive_source_proof(api, changes):
    with pytest.raises(ValueError): candidate(api, **changes)
