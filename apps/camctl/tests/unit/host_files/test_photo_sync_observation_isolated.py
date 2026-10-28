"""同步文件观察分类只消费受管文件调用的真实返回。"""
import importlib.util
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    model = ModuleType("camctl.host_files.models")
    model.BoundDirectories, model.FileRef = object, object
    model.FilePurpose = Enum("FilePurpose", {"CAPTURE_ORIGINAL": "original", "CAPTURE_PREVIEW": "preview"})
    paths = ModuleType("camctl.host_files.paths")
    paths.relative_file_path = paths.resolve_file = lambda *args: None
    formats = ModuleType("camctl.host_files.photo_formats")
    formats.CaptureFormat = Enum("CaptureFormat", {"RAW": "raw"})
    formats.CAPTURE_FORMATS = {formats.CaptureFormat.RAW: SimpleNamespace(original_media_type="image/x-hasselblad-3fr", preview_media_type="image/jpeg")}
    formats.photo_file_extension = formats.photo_media_type = lambda *args: None
    jpeg = ModuleType("camctl.host_files.jpeg_boundary")
    jpeg.JpegBoundary = object
    segments = ModuleType("camctl.host_files.segments")
    segments.DEFAULT_CHUNK_SIZE_BYTES = 1024
    for stub in (model, paths, formats, jpeg, segments):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    source = Path(__file__).parents[3] / "src/camctl/host_files/capture_sources.py"
    name = "camctl.host_files._isolated_sync_observer"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def immediate(coroutine):
    with pytest.raises(StopIteration) as finished:
        coroutine.send(None)
    return finished.value.value


@pytest.mark.parametrize("stage,expected", [("source_type", "invalid"), ("source_size", "invalid"),
    ("source_media", "invalid"), ("source_read", "unconfirmed"), ("workspace_sync", "unconfirmed")])
def test_sync_candidate_preserves_actual_failure(api, stage, expected):
    error = api.CaptureFileError(stage, detail="actual failure")
    async def run(*args): raise error
    api._run_file_io = run
    result = immediate(api.inspect_capture_candidate(SimpleNamespace(staging=Path("/staging")), "7",
        api.CaptureFileSpec("a.3FR", 100, "image/x-hasselblad-3fr")))
    assert result.status == expected and result.error is error and result.artifact is None


def test_missing_sync_candidate_is_distinct_from_unconfirmed_read(api):
    async def run(*args): return None
    api._run_file_io = run
    result = immediate(api.inspect_capture_candidate(SimpleNamespace(staging=Path("/staging")), "7",
        api.CaptureFileSpec("a.3FR", 100, "image/x-hasselblad-3fr")))
    assert result.status == "missing" and result.error is None and result.artifact is None
