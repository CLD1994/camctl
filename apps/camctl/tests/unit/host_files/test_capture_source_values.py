"""拍摄文件说明和恢复字段的局部输入合同，不访问真实文件。"""

import hashlib
from pathlib import Path

import pytest

from camctl.host_files.capture_sources import (
    CaptureFileError,
    CaptureFileSpec,
    CapturedArtifact,
    workspace_path,
)

from camctl.host_files.models import BoundDirectories


def test_workspace_path_requires_a_canonical_in_range_object_id() -> None:
    roots = BoundDirectories(staging=Path("/uncreated/staging"))

    assert workspace_path(roots, "42") == roots.staging / "capture-work" / "42"
    for activity_id in ("0", "01", "+1", "9223372036854775808", 42, True):
        with pytest.raises(CaptureFileError):
            workspace_path(roots, activity_id)


_RAW = b"II*\x00" + b"3fr-payload"
_RAW_SPEC = CaptureFileSpec("Hasselblad Original 1.3FR", len(_RAW), "image/x-hasselblad-3fr")


def test_capture_file_spec_rejects_non_direct_names_and_invalid_size() -> None:
    for name in ("", ".", "..", "../camera.3fr", "camera\\photo.3fr", "bad\x00name", "bad\nname"):
        with pytest.raises(CaptureFileError):
            CaptureFileSpec(name, 1, "image/x-hasselblad-3fr")

    for size in (0, -1, True, 1.0):
        with pytest.raises(CaptureFileError):
            CaptureFileSpec("camera.3fr", size, "image/x-hasselblad-3fr")

    with pytest.raises(CaptureFileError):
        CaptureFileSpec("camera.bin", 1, "application/octet-stream")


def test_captured_artifact_json_has_exactly_the_recovery_fields() -> None:
    artifact = CapturedArtifact(
        original_name=_RAW_SPEC.original_name,
        size_bytes=len(_RAW),
        sha256=hashlib.sha256(_RAW).hexdigest(),
        media_type=_RAW_SPEC.media_type,
    )
    expected = {
        "original_name": "Hasselblad Original 1.3FR",
        "size_bytes": len(_RAW),
        "sha256": hashlib.sha256(_RAW).hexdigest(),
        "media_type": "image/x-hasselblad-3fr",
    }

    assert artifact.as_json() == expected
    assert CapturedArtifact.from_json(expected) == artifact
    with pytest.raises(CaptureFileError):
        CapturedArtifact.from_json({**expected, "path": "capture-work/42/file"})
    with pytest.raises(CaptureFileError):
        CapturedArtifact.from_json({key: value for key, value in expected.items() if key != "media_type"})
    with pytest.raises(CaptureFileError):
        CapturedArtifact.from_json({**expected, "sha256": "not-a-digest"})
