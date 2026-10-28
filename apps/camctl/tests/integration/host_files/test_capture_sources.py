"""T3 主机拍摄来源文件的可靠验证、归档与重启恢复。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import sys
import threading
from pathlib import Path

import pytest

from apps.camctl.tests.integration.jpeg_fixture import JPEG_IMAGE

import camctl.host_files.capture_sources as capture_sources
from camctl.host_files.capture_sources import (
    CaptureFileError,
    CaptureFileSpec,
    CapturedArtifact,
    archive_capture_file,
    prepare_capture_workspace,
    verify_capture_files,
    workspace_path,
)
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef

pytestmark = pytest.mark.asyncio

_RAW = b"II*\x00" + b"3fr-payload"
_JPEG = JPEG_IMAGE + b"jpeg-payload"
_RAW_SPEC = CaptureFileSpec("Hasselblad Original 1.3FR", len(_RAW), "image/x-hasselblad-3fr")
_JPEG_SPEC = CaptureFileSpec("Hasselblad Preview 1.JPG", len(_JPEG), "image/jpeg")


def _roots(tmp_path: Path) -> BoundDirectories:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "capture-originals").mkdir()
    (staging / "capture-previews").mkdir()
    return BoundDirectories(staging=staging)


def _target(roots: BoundDirectories, *, original: bool = True) -> FileRef:
    purpose = (
        FilePurpose.CAPTURE_ORIGINAL if original else FilePurpose.CAPTURE_PREVIEW
    )
    directory = "capture-originals" if original else "capture-previews"
    extension = "3fr" if original else "jpg"
    file_id = 701 if original else 702
    return FileRef(
        file_id=file_id,
        purpose=purpose,
        relative_path=f"{directory}/{file_id}.{extension}",
        root=roots.staging,
    )


async def _prepared_workspace(tmp_path: Path) -> tuple[BoundDirectories, Path]:
    roots = _roots(tmp_path)
    return roots, await prepare_capture_workspace(roots, "42")


async def test_capture_file_spec_rejects_non_direct_names_and_invalid_size() -> None:
    for name in ("", ".", "..", "../camera.3fr", "camera\\photo.3fr", "bad\x00name", "bad\nname"):
        with pytest.raises(CaptureFileError):
            CaptureFileSpec(name, 1, "image/x-hasselblad-3fr")

    for size in (0, -1, True, 1.0):
        with pytest.raises(CaptureFileError):
            CaptureFileSpec("camera.3fr", size, "image/x-hasselblad-3fr")

    with pytest.raises(CaptureFileError):
        CaptureFileSpec("camera.bin", 1, "application/octet-stream")


async def test_workspace_path_requires_a_canonical_in_range_object_id(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)

    assert workspace_path(roots, "42") == roots.staging / "capture-work" / "42"

    for activity_id in ("0", "01", "+1", "9223372036854775808", 42, True):
        with pytest.raises(CaptureFileError):
            workspace_path(roots, activity_id)


async def test_workspace_creation_is_exclusive_and_preserves_existing_files(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    marker = workspace / "keep.bin"
    marker.write_bytes(b"owned by the existing activity")

    with pytest.raises(CaptureFileError) as failure:
        await prepare_capture_workspace(roots, "42")

    assert failure.value.stage == "workspace_create"
    assert marker.read_bytes() == b"owned by the existing activity"


async def test_new_activity_retries_staging_sync_after_parent_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    work_parent = roots.staging / "capture-work"
    first_workspace = work_parent / "42"
    next_workspace = work_parent / "43"
    staging_stat = os.stat(roots.staging)
    real_fsync = os.fsync
    staging_syncs: list[tuple[bool, bool]] = []

    def fail_first_staging_sync(fd: int) -> None:
        observed = os.fstat(fd)
        if (observed.st_dev, observed.st_ino) == (staging_stat.st_dev, staging_stat.st_ino):
            staging_syncs.append((work_parent.is_dir(), next_workspace.exists()))
            if len(staging_syncs) == 1:
                raise OSError(errno.EIO, "injected staging directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(capture_sources.os, "fsync", fail_first_staging_sync)

    with pytest.raises(CaptureFileError) as failure:
        await prepare_capture_workspace(roots, "42")

    assert failure.value.stage == "staging_sync"
    assert work_parent.is_dir()
    assert first_workspace.exists() is False

    created = await prepare_capture_workspace(roots, "43")

    assert created == next_workspace
    assert staging_syncs == [(True, False), (True, False)]
    assert first_workspace.exists() is False
    assert next_workspace.is_dir()


async def test_cancel_waits_for_started_file_io_worker_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, _ = await _prepared_workspace(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_verification(
        _roots: BoundDirectories,
        _path: Path,
        _specs: tuple[CaptureFileSpec, ...],
    ) -> tuple[CapturedArtifact, ...]:
        entered.set()
        release.wait()
        finished.set()
        return ()

    monkeypatch.setattr(capture_sources, "_verify_capture_files", blocked_verification)
    task = asyncio.create_task(verify_capture_files(roots, "42", (_RAW_SPEC,)))
    assert await asyncio.to_thread(entered.wait, 2)

    task.cancel()
    try:
        await asyncio.sleep(0)
        remained_pending = not task.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert remained_pending
    assert await asyncio.to_thread(finished.wait, 2)


async def test_captured_artifact_json_has_exactly_the_recovery_fields() -> None:
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


async def test_verify_hashes_both_media_and_syncs_files_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    raw_path = workspace / _RAW_SPEC.original_name
    jpeg_path = workspace / _JPEG_SPEC.original_name
    raw_path.write_bytes(_RAW)
    jpeg_path.write_bytes(_JPEG)

    real_fsync = os.fsync
    sync_count = 0

    def record_real_sync(fd: int) -> None:
        nonlocal sync_count
        sync_count += 1
        real_fsync(fd)

    monkeypatch.setattr(capture_sources.os, "fsync", record_real_sync)
    artifacts = await verify_capture_files(
        roots, "42", (_RAW_SPEC, _JPEG_SPEC)
    )

    assert artifacts == (
        CapturedArtifact(
            original_name=_RAW_SPEC.original_name,
            size_bytes=len(_RAW),
            sha256=hashlib.sha256(_RAW).hexdigest(),
            media_type="image/x-hasselblad-3fr",
        ),
        CapturedArtifact(
            original_name=_JPEG_SPEC.original_name,
            size_bytes=len(_JPEG),
            sha256=hashlib.sha256(_JPEG).hexdigest(),
            media_type="image/jpeg",
        ),
    )
    assert raw_path.read_bytes() == _RAW
    assert jpeg_path.read_bytes() == _JPEG
    assert sync_count == 3


@pytest.mark.parametrize(
    ("present", "expected_stage"),
    [("wrong_size", "source_size"), ("missing_preview", "source_open")],
)
async def test_verify_failure_preserves_all_work_files(
    tmp_path: Path, present: str, expected_stage: str
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    raw_path = workspace / _RAW_SPEC.original_name
    jpeg_path = workspace / _JPEG_SPEC.original_name
    raw_path.write_bytes(_RAW)
    jpeg_path.write_bytes(_JPEG)
    specs = (_RAW_SPEC, _JPEG_SPEC)
    if present == "wrong_size":
        specs = (CaptureFileSpec(_RAW_SPEC.original_name, len(_RAW) + 1, _RAW_SPEC.media_type), _JPEG_SPEC)
    else:
        jpeg_path.unlink()

    with pytest.raises(CaptureFileError) as failure:
        await verify_capture_files(roots, "42", specs)

    assert failure.value.stage == expected_stage
    assert raw_path.read_bytes() == _RAW
    assert jpeg_path.exists() is (present != "missing_preview")
    if jpeg_path.exists():
        assert jpeg_path.read_bytes() == _JPEG


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 默认不支持无特权创建符号链接")
async def test_verify_rejects_a_symlink_without_reading_its_target(tmp_path: Path) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    raw_path = workspace / _RAW_SPEC.original_name
    external = tmp_path / "outside.3fr"
    external.write_bytes(_RAW)
    raw_path.symlink_to(external)
    (workspace / _JPEG_SPEC.original_name).write_bytes(_JPEG)

    with pytest.raises(CaptureFileError) as failure:
        await verify_capture_files(roots, "42", (_RAW_SPEC, _JPEG_SPEC))

    assert failure.value.stage == "source_type"
    assert external.read_bytes() == _RAW
    assert raw_path.is_symlink()


async def test_verify_rejects_a_directory_instead_of_a_source_file(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    (workspace / _RAW_SPEC.original_name).mkdir()
    (workspace / _JPEG_SPEC.original_name).write_bytes(_JPEG)

    with pytest.raises(CaptureFileError) as failure:
        await verify_capture_files(roots, "42", (_RAW_SPEC, _JPEG_SPEC))

    assert failure.value.stage == "source_type"
    assert (workspace / _RAW_SPEC.original_name).is_dir()
    assert (workspace / _JPEG_SPEC.original_name).read_bytes() == _JPEG


@pytest.mark.parametrize(
    ("name", "payload", "media_type"),
    [
        ("bad.3fr", b"II?\x00" + b"payload", "image/x-hasselblad-3fr"),
        ("bad.jpg", b"\x00\xd8" + b"payload" + b"\xff\xd9", "image/jpeg"),
        ("bad-tail.jpg", b"\xff\xd8" + b"payload" + b"\xff\x00", "image/jpeg"),
    ],
)
async def test_verify_rejects_unrecognized_tiff_or_jpeg_markers(
    tmp_path: Path, name: str, payload: bytes, media_type: str
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    source = workspace / name
    source.write_bytes(payload)
    spec = CaptureFileSpec(name, len(payload), media_type)

    with pytest.raises(CaptureFileError) as failure:
        await verify_capture_files(roots, "42", (spec,))

    assert failure.value.stage == "source_media"
    assert source.read_bytes() == payload


async def test_archive_moves_validated_source_to_its_registered_role(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    source = workspace / _RAW_SPEC.original_name
    source.write_bytes(_RAW)
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )
    target = _target(roots)
    target_path = roots.staging / target.relative_path

    await archive_capture_file(roots, "42", artifact, target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert source.exists() is False
    assert target_path.read_bytes() == _RAW


async def test_archive_refuses_to_overwrite_an_unknown_target(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    source = workspace / _RAW_SPEC.original_name
    source.write_bytes(_RAW)
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )
    target = _target(roots)
    target_path = roots.staging / target.relative_path
    target_path.write_bytes(b"unknown existing file")

    with pytest.raises(CaptureFileError) as failure:
        await archive_capture_file(roots, "42", artifact, target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert failure.value.stage == "target_conflict"
    assert source.read_bytes() == _RAW
    assert target_path.read_bytes() == b"unknown existing file"


async def test_archive_retry_reuses_the_matching_target_after_source_move(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    source = workspace / _RAW_SPEC.original_name
    source.write_bytes(_RAW)
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )
    target = _target(roots)
    target_path = roots.staging / target.relative_path

    await archive_capture_file(roots, "42", artifact, target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")
    await archive_capture_file(roots, "42", artifact, target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert source.exists() is False
    assert target_path.read_bytes() == _RAW


async def test_archive_refuses_when_both_source_and_target_are_missing(
    tmp_path: Path,
) -> None:
    roots, _ = await _prepared_workspace(tmp_path)
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )

    with pytest.raises(CaptureFileError) as failure:
        await archive_capture_file(roots, "42", artifact, _target(roots), purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert failure.value.stage == "source_and_target_missing"


async def test_archive_rechecks_source_content_before_moving(
    tmp_path: Path,
) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    source = workspace / _RAW_SPEC.original_name
    source.write_bytes(b"II*\x00" + b"3fr-payloaf")
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )
    target_path = roots.staging / _target(roots).relative_path

    with pytest.raises(CaptureFileError):
        await archive_capture_file(roots, "42", artifact, _target(roots), purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert source.exists()
    assert len(source.read_bytes()) == artifact.size_bytes
    assert target_path.exists() is False


async def test_archive_requires_target_role_to_match_media_type(tmp_path: Path) -> None:
    roots, workspace = await _prepared_workspace(tmp_path)
    (workspace / _RAW_SPEC.original_name).write_bytes(_RAW)
    artifact = CapturedArtifact(
        _RAW_SPEC.original_name,
        len(_RAW),
        hashlib.sha256(_RAW).hexdigest(),
        _RAW_SPEC.media_type,
    )

    with pytest.raises(CaptureFileError) as failure:
        await archive_capture_file(roots, "42", artifact, _target(roots, original=False), purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="raw")

    assert failure.value.stage == "target_role"


async def test_archive_uses_explicit_role_for_two_jpegs(tmp_path):
    roots, workspace = await _prepared_workspace(tmp_path)
    original_bytes = JPEG_IMAGE + b"original"
    preview_bytes = JPEG_IMAGE + b"preview"
    original = CapturedArtifact("capture.jpg", len(original_bytes), hashlib.sha256(original_bytes).hexdigest(), "image/jpeg")
    preview = CapturedArtifact("capture.jpg.thumbnail.jpg", len(preview_bytes), hashlib.sha256(preview_bytes).hexdigest(), "image/jpeg")
    (workspace / original.original_name).write_bytes(original_bytes)
    (workspace / preview.original_name).write_bytes(preview_bytes)
    original_target = FileRef(701, FilePurpose.CAPTURE_ORIGINAL, "capture-originals/701.jpg", roots.staging)
    preview_target = _target(roots, original=False)
    with pytest.raises(CaptureFileError) as wrong_role:
        await archive_capture_file(roots, "42", original, preview_target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="jpeg")
    assert wrong_role.value.stage == "target_role"
    await archive_capture_file(roots, "42", original, original_target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="jpeg")
    await archive_capture_file(roots, "42", preview, preview_target, purpose=FilePurpose.CAPTURE_PREVIEW, capture_format="jpeg")
    assert (roots.staging / original_target.relative_path).read_bytes() == original_bytes
    assert (roots.staging / preview_target.relative_path).read_bytes() == preview_bytes


async def test_jpeg_archive_rejects_non_jpeg_content_without_removing_source(tmp_path):
    roots, workspace = await _prepared_workspace(tmp_path)
    invalid = b"II*\x00not-jpeg"
    source = workspace / "capture.jpg"
    source.write_bytes(invalid)
    artifact = CapturedArtifact(source.name, len(invalid), hashlib.sha256(invalid).hexdigest(), "image/jpeg")
    target = FileRef(701, FilePurpose.CAPTURE_ORIGINAL, "capture-originals/701.jpg", roots.staging)
    with pytest.raises(CaptureFileError) as failure:
        await archive_capture_file(roots, "42", artifact, target, purpose=FilePurpose.CAPTURE_ORIGINAL, capture_format="jpeg")
    assert failure.value.stage == "source_media"
    assert source.read_bytes() == invalid
    assert not (roots.staging / target.relative_path).exists()


async def test_verify_rejects_two_names_for_the_same_physical_capture_file(tmp_path):
    roots, workspace = await _prepared_workspace(tmp_path)
    original = workspace / "capture.jpg"
    original.write_bytes(_JPEG)
    os.link(original, workspace / "capture.jpg.thumbnail.jpg")
    specs = (CaptureFileSpec("capture.jpg", len(_JPEG), "image/jpeg"),
             CaptureFileSpec("capture.jpg.thumbnail.jpg", len(_JPEG), "image/jpeg"))
    with pytest.raises(CaptureFileError) as failure:
        await verify_capture_files(roots, "42", specs)
    assert failure.value.stage == "source_identity"
    assert original.read_bytes() == _JPEG
