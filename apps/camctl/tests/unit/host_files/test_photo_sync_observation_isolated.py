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


@pytest.mark.parametrize("primary", [None, "source_media"])
def test_close_failure_preserves_owner_even_when_media_is_invalid(api, primary):
    error = OSError("close failed")
    def close(fd): raise error
    api.os = SimpleNamespace(close=close)
    calls = []
    owner = SimpleNamespace(closed_file=lambda fd, failure: calls.append((fd, failure)))
    actual = None if primary is None else api.CaptureFileError(primary)
    if actual is None:
        with pytest.raises(api.CaptureFileError):
            api._close_fd(17, "source_close", Path("/fake"), owner=owner)
    else:
        api._close_fd(17, "source_close", Path("/fake"), actual, owner=owner)
        assert actual.additional_errors == (error,)
    assert calls == [(17, error)]


@pytest.mark.parametrize("content,stage", [(b"II*\x00", "source_close"), (b"BAD!", "source_media")])
def test_actual_facts_read_tracks_open_and_failed_close(api, content, stage):
    facts = SimpleNamespace(st_mode=0o100644, st_size=4, st_dev=1, st_ino=2, st_mtime_ns=1, st_ctime_ns=1)
    blocks = iter((content, b""))
    failure = OSError("close failed")
    def close(fd): raise failure
    api.os = SimpleNamespace(open=lambda *args, **kwargs: 17, fstat=lambda fd: facts,
        read=lambda fd, size: next(blocks), fsync=lambda fd: None, close=close, O_RDONLY=0)
    api._stat_entry = lambda *args: facts
    tracked = []
    owner = SimpleNamespace(opened_file=lambda fd: tracked.append(("open", fd)),
        closed_file=lambda fd, error: tracked.append(("close", fd, error)))
    with pytest.raises(api.CaptureFileError) as caught:
        api._read_file_facts_at(4, "file.3FR", Path("/fake/file.3FR"), "image/x-hasselblad-3fr",
            expected_size=4, expected_sha256=None, file_kind="source", owner=owner)
    assert caught.value.stage == stage
    assert tracked == [("open", 17), ("close", 17, failure)]


@pytest.mark.parametrize("selected,expected_workspace,is_result", [
    ("sync", "/staging/capture-work/7", False),
    ("camera", "/staging/result-downloads/9-2-original", True),
])
def test_cache_archives_only_fixed_selected_workspace(api, monkeypatch, selected, expected_workspace, is_result):
    downloads = ModuleType("camctl.host_files.downloads")
    downloads.result_download_path = lambda staging, ticket, role: staging / "result-downloads" / f"{ticket.run_id}-{ticket.attempt_id}-{role}"
    monkeypatch.setitem(sys.modules, downloads.__name__, downloads)
    api.photo_media_type = lambda purpose, capture_format: "image/x-hasselblad-3fr"
    api.photo_file_extension = lambda purpose, capture_format: "3fr"
    api.relative_file_path = lambda purpose, file_id, extension: "original/1.3fr"
    api.resolve_file = lambda target, roots: SimpleNamespace(path=Path("/staging/original/1.3fr"))
    calls = []
    async def run(*args): calls.append(args)
    api._run_file_io = run
    roots = SimpleNamespace(staging=Path("/staging"))
    ticket = SimpleNamespace(target_id="7", run_id=9, attempt_id=2)
    purpose = api.FilePurpose.CAPTURE_ORIGINAL
    target = SimpleNamespace(purpose=purpose, file_id=1, relative_path="original/1.3fr")
    artifact = api.CapturedArtifact("actual.3FR", 4, "a" * 64, "image/x-hasselblad-3fr")
    owner = object()
    immediate(api.archive_photo_cache(roots, "7", ticket, "original", selected, artifact, target,
        purpose=purpose, capture_format=api.CaptureFormat.RAW, owner=owner))
    assert len(calls) == 1 and calls[0][0] is api._archive_capture_file
    assert calls[0][2] == Path(expected_workspace)
    assert calls[0][-2:] == (is_result, owner)
