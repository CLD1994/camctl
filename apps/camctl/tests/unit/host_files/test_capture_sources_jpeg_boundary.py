"""单张工作文件的 JPEG 标记边界；所有文件协作者由内存替身隔离。"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def capture_sources(monkeypatch):
    # 导入前隔离会读取枚举登记或包资源的协作者。
    substitutes = {
        "camctl.contracts.values": {"parse_object_id": lambda value: value},
        "camctl.host_files.models": {
            "BoundDirectories": object, "FilePurpose": object, "FileRef": object,
        },
        "camctl.host_files.paths": {
            "relative_file_path": lambda *args: None, "resolve_file": lambda *args: None,
        },
        "camctl.host_files.photo_formats": {
            "CAPTURE_FORMATS": {"jpeg": SimpleNamespace(
                original_media_type="image/jpeg", preview_media_type="image/jpeg")},
            "CaptureFormat": object, "photo_file_extension": lambda *args: None,
            "photo_media_type": lambda *args: None,
        },
        "camctl.host_files.segments": {"DEFAULT_CHUNK_SIZE_BYTES": 1 << 20},
    }
    for name, attributes in substitutes.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    name = "camctl.host_files._isolated_capture_sources_test"
    source = Path(__file__).parents[3] / "src/camctl/host_files/capture_sources.py"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def read_facts(module, monkeypatch, blocks, *, kind="source", expected_sha256=None,
               expected_size=None, media_type="image/jpeg"):
    data = b"".join(blocks)
    observed = SimpleNamespace(st_size=len(data), st_dev=1, st_ino=2)
    chunks = iter([*blocks, b""])
    synced = []
    monkeypatch.setattr(module, "_open_regular_file_at", lambda *args: (11, observed))
    monkeypatch.setattr(module, "_stat_entry", lambda *args: observed)
    monkeypatch.setattr(module, "_same_file_state", lambda *args: True)
    monkeypatch.setattr(module, "_same_identity", lambda *args: True)
    monkeypatch.setattr(module, "_close_fd", lambda *args: None)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "read", lambda fd, count: next(chunks))
        patch.setattr(module.os, "fstat", lambda fd: observed)
        patch.setattr(module.os, "fsync", lambda fd: synced.append(fd))
        facts = module._read_file_facts_at(
            10, "photo.jpg", Path("photo.jpg"), media_type,
            expected_size=len(data) if expected_size is None else expected_size,
            expected_sha256=expected_sha256, file_kind=kind,
        )
    return facts, synced


_JPEG_IMAGE = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIABAAEAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z"
)


def test_jpeg_eoi_followed_by_data_keeps_whole_file_digest_and_sync(capture_sources, monkeypatch):
    data = _JPEG_IMAGE + b"trailing data"
    facts, synced = read_facts(capture_sources, monkeypatch, [data])
    assert facts.size_bytes == len(data)
    assert facts.sha256 == hashlib.sha256(data).hexdigest()
    assert synced == [11]


# 有长度的段在内容中包含独立 JPEG 的结束标记，不应被当作外层结束。
_NESTED = b"\xff\xd8\xff\xe1\x00\x08\xff\xd8\xff\xd9xx"
_SCANS = (
    b"\xff\xda\x00\x02A\xff\x00\xd9B\xff\xd0C"
    b"\xff\xdc\x00\x04\x00\x01\xff\xc4\x00\x02"
    b"\xff\xda\x00\x02E\xff\xff\xd9"
)
_VALID = _NESTED + _SCANS + b"tail\xff\xd9"


@pytest.mark.parametrize("split", range(len(_VALID) + 1))
def test_segments_and_scans_are_independent_of_block_boundary(capture_sources, monkeypatch, split):
    # 空块代表 EOF，故端点只提供非空块。
    blocks = [part for part in (_VALID[:split], _VALID[split:]) if part]
    facts, _ = read_facts(capture_sources, monkeypatch, blocks)
    assert facts.size_bytes == len(_VALID)
    assert facts.sha256 == hashlib.sha256(_VALID).hexdigest()


def test_each_byte_can_arrive_in_a_separate_block(capture_sources, monkeypatch):
    facts, _ = read_facts(capture_sources, monkeypatch, [bytes([byte]) for byte in _VALID])
    assert facts.sha256 == hashlib.sha256(_VALID).hexdigest()


@pytest.mark.parametrize("data", [
    b"\xff\xd8\xff\xd9",  # 只检查结构边界，不要求 SOF 或 SOS。
    b"\xff\xd8\xff\xfe\x00\x06\xff\xd9xx\xff\xd9",
    b"\xff\xd8\xff\xda\x00\x02a\xff\x01b\xff\xd9",
    b"\xff\xd8\xff\xda\x00\x02a\xff\xdc\x00\x04\x00\x01\xff\xd9",
    b"\xff\xd8\xff\xda\x00\x02a\xff\xdc\x00\x04\x00\x01\xff\xda\x00\x02b\xff\xd9",
])
def test_marker_boundaries_without_additional_image_quality_checks(capture_sources, monkeypatch, data):
    facts, _ = read_facts(capture_sources, monkeypatch, [data])
    assert facts.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("data", [
    b"", b"\xff", b"\xff\xd7\xff\xd9", b"\xff\xd8", b"\xff\xd8\xff",
    b"\xff\xd8junk\xff\xd9", b"\xff\xd8\xff\xd8\xff\xd9",
    b"\xff\xd8\xff\x02\x00\x02\xff\xd9",  # 未定义标记。
    b"\xff\xd8\xff\xc8\x00\x02\xff\xd9",  # 未解释的 JPEG 扩展。
    b"\xff\xd8\xff\xf0\x00\x02\xff\xd9",
    b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xe1\x00",
    b"\xff\xd8\xff\xe1\x00\x01\xff\xd9",  # 长度小于 2。
    b"\xff\xd8\xff\xe1\x00\x06\xff\xd9",  # 段未读完。
    _NESTED,  # 只有段内嵌 JPEG 的 EOI。
    b"\xff\xd8\xff\xda\x00\x02a\xff\x00\xd9",  # 填充字节不是 EOI。
    b"\xff\xd8\xff\xda\x00\x02a\xff",  # 扫描标记截断。
    b"\xff\xd8\xff\xda\x00\x02a\xff\xdc\x00\x04\x00",  # DNL 截断。
    b"\xff\xd8\xff\xda\x00\x02a\xff\xdc\x00\x04\x00\x01data\xff\xd9",
])
@pytest.mark.parametrize("kind, stage", [("source", "source_media"), ("target", "target_conflict")])
def test_unreliable_boundary_is_rejected(capture_sources, monkeypatch, data, kind, stage):
    with pytest.raises(capture_sources.CaptureFileError) as error:
        read_facts(capture_sources, monkeypatch, [data] if data else [], kind=kind)
    assert error.value.stage == stage


def test_existing_target_requires_full_digest_including_tail(capture_sources, monkeypatch):
    with pytest.raises(capture_sources.CaptureFileError) as error:
        read_facts(capture_sources, monkeypatch, [_VALID], kind="target", expected_sha256="0" * 64)
    assert error.value.stage == "target_conflict"
    assert "SHA-256" in error.value.detail


def test_reported_size_must_cover_tail(capture_sources, monkeypatch):
    with pytest.raises(capture_sources.CaptureFileError) as error:
        read_facts(capture_sources, monkeypatch, [_VALID], expected_size=len(_VALID) - 1)
    assert error.value.stage == "source_size"


def test_existing_target_accepts_saved_digest_of_all_bytes(capture_sources, monkeypatch):
    digest = hashlib.sha256(_VALID).hexdigest()
    facts, synced = read_facts(capture_sources, monkeypatch, [_VALID], kind="target", expected_sha256=digest)
    assert facts.sha256 == digest
    assert synced == [11]


@pytest.mark.parametrize("data,accepted", [
    (b"II*\x00raw", True), (b"MM\x00*raw", True), (b"II\x00*raw", False),
])
def test_raw_tiff_header_contract_is_unchanged(capture_sources, monkeypatch, data, accepted):
    if accepted:
        facts, _ = read_facts(capture_sources, monkeypatch, [data[:2], data[2:]],
                              media_type="image/x-hasselblad-3fr")
        assert facts.sha256 == hashlib.sha256(data).hexdigest()
    else:
        with pytest.raises(capture_sources.CaptureFileError) as error:
            read_facts(capture_sources, monkeypatch, [data], media_type="image/x-hasselblad-3fr")
        assert error.value.stage == "source_media"


def test_valid_image_ending_at_eoi_is_accepted(capture_sources, monkeypatch):
    facts, _ = read_facts(capture_sources, monkeypatch, [_JPEG_IMAGE])
    assert facts.sha256 == hashlib.sha256(_JPEG_IMAGE).hexdigest()
