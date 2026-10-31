"""照片缓存原申请格式不依赖运行资源。"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def api(monkeypatch):
    values = ModuleType("camctl.contracts.values")
    values.MAX_OBJECT_ID = 2**63 - 1
    def object_id(value):
        if type(value) is not int or not 0 < value <= values.MAX_OBJECT_ID:
            raise ValueError("invalid identity")
        return value
    def utc(value):
        if type(value) is not int:
            raise ValueError("invalid timestamp")
        return value
    values.ObjectId, values.UtcMicros = object_id, utc
    json_values = ModuleType("camctl.contracts.json_values")
    json_values.is_json_integer = lambda value: type(value) is int
    for stub in (values, json_values):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).parents[3] / "src/camctl/history/photo_cache_request.py"
    name = "camctl.history._isolated_photo_cache_request"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def request():
    return {"action_id": 1, "file_id": 2, "device_file_id": 3, "result_page_event_id": 4,
            "selected": "camera", "size_bytes": 100, "sha256": "a" * 64,
            "occurred_at": 123, "archive_error": None}


def test_cache_request_preserves_original_source_and_actual_archive_error(api):
    document = request()
    document["archive_error"] = {"type": "OSError", "message": "archive failed"}
    saved = api.parse_photo_cache_request(document)
    assert saved.result_page_event_id == 4 and saved.selected == "camera"
    assert saved.archive_error == document["archive_error"]


@pytest.mark.parametrize("field,value", [("file_id", True), ("result_page_event_id", 0),
    ("size_bytes", 0), ("sha256", "bad"), ("selected", "other"), ("archive_error", "bad")])
def test_cache_request_rejects_invalid_identity_or_fact(api, field, value):
    document = request()
    document[field] = value
    with pytest.raises(ValueError):
        api.parse_photo_cache_request(document)
