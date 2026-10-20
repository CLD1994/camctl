from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "validate_osmo_segments.py"
FFMPEG = shutil.which("ffmpeg")


@pytest.fixture(scope="module", autouse=True)
def require_media_tools():
    assert shutil.which("ffmpeg"), "ffmpeg is required for media integration tests"
    assert shutil.which("ffprobe"), "ffprobe is required for media integration tests"


def make_media(
    destination: Path,
    *,
    video_stream_count: int = 2,
    duration_seconds: str = "1",
    include_audio: bool = True,
    include_attached_picture: bool = False,
) -> Path:
    command = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for index in range(video_stream_count):
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=64x64:rate=10:duration={duration_seconds}",
            ]
        )
    if include_attached_picture:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:size=64x64:rate=1:duration=1",
            ]
        )
    if include_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=1000:sample_rate=8000:duration={duration_seconds}",
            ]
        )
    for index in range(video_stream_count):
        command.extend(["-map", f"{index}:v:0"])
    if include_attached_picture:
        command.extend(["-map", f"{video_stream_count}:v:0"])
    if include_audio:
        audio_input_index = video_stream_count + int(include_attached_picture)
        command.extend(["-map", f"{audio_input_index}:a:0"])
    command.extend(["-c:v", "mpeg4", "-q:v", "5"])
    if include_attached_picture:
        command.extend(
            [
                f"-c:v:{video_stream_count}",
                "mjpeg",
                f"-frames:v:{video_stream_count}",
                "1",
                f"-disposition:v:{video_stream_count}",
                "attached_pic",
            ]
        )
    if include_audio:
        command.extend(["-c:a", "aac"])
    command.extend(["-shortest", "-threads", "1", str(destination)])
    subprocess.run(command, check=True, capture_output=True, text=True)
    return destination


def file_entry(path: Path, file_id: str, output_id: str) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "file_id": file_id,
        "output_id": output_id,
        "role": "original",
        "file_status": "retained",
        "source_device_path": f"DCIM/CAM001/{file_id}.OSV",
        "source_size_bytes": path.stat().st_size,
        "source_sha256": digest,
        "source_evidence_ref": f"synthetic-fixture#{file_id}",
        "local_copy_path": path.name,
    }


def manifest_for(
    root: Path,
    files: list[dict],
    *,
    target_duration_ms: int = 1_000,
    recording_kind: str = "ordinary_recording",
    recording_class: str = "normal",
    retained_file_ids: list[str] | None = None,
    default_file_ids: list[str] | None = None,
    retained_scope_status: str = "complete",
    default_scope_status: str = "complete",
) -> Path:
    retained = retained_file_ids or [entry["file_id"] for entry in files]
    default = retained if default_file_ids is None else default_file_ids
    document = {
        "manifest_version": 1,
        "acceptance_id": "synthetic-integration-case",
        "source_action_instance_id": "synthetic-source-action",
        "recording_kind": recording_kind,
        "recording_class": recording_class,
        "target_duration_ms": target_duration_ms,
        "target_basis_ref": "synthetic-task-record#target",
        "retained_scope_status": retained_scope_status,
        "retained_file_ids": retained,
        "default_scope_status": default_scope_status,
        "default_file_ids": default,
        "files": files,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def run_validator(
    manifest_path: Path,
    output_path: Path,
    *,
    tolerance_ms: int | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    command = [sys.executable, str(SCRIPT_PATH), "--manifest", str(manifest_path), "--output", str(output_path)]
    if tolerance_ms is not None:
        command.extend(["--shortfall-tolerance-ms", str(tolerance_ms)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    return completed, json.loads(output_path.read_text(encoding="utf-8"))


def test_dual_video_and_audio_are_probed_and_each_stream_is_decoded(tmp_path: Path):
    media_path = make_media(tmp_path / "segment-a.mp4")
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report["shortfall_tolerance_ms"] == 100
    file_result = report["files"][0]
    assert file_result["local_copy"]["identity_status"] == "match"
    assert len(file_result["media"]["main_videos"]) == 2
    assert len(file_result["media"]["audio_streams"]) == 1
    assert [check["status"] for check in file_result["media"]["decode_checks"]] == [
        "decoded",
        "decoded",
        "decoded",
    ]
    assert all(
        video["frame_coverage"]["decoded_frame_timestamp_count"] == 10
        for video in file_result["media"]["main_videos"]
    )
    assert report["scopes"]["all_retained"]["source_duration_status"] == "pass"
    assert report["scopes"]["default_delivery"]["source_duration_status"] == "pass"


def test_attached_picture_is_excluded_from_the_two_main_video_streams(tmp_path: Path):
    media_path = make_media(
        tmp_path / "segment-with-cover.mp4", include_attached_picture=True
    )
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    file_result = report["files"][0]
    assert len(file_result["media"]["main_videos"]) == 2
    assert len(file_result["media"]["attached_picture_streams"]) == 1
    cover_check = next(
        check
        for check in file_result["media"]["decode_checks"]
        if check.get("skip_reason")
    )
    assert cover_check["status"] == "skipped"
    assert cover_check["skip_reason"] == "attached_picture_excluded_from_main_video_duration"


def test_range_outside_default_delivery_cannot_cover_default_shortfall(tmp_path: Path):
    first = make_media(tmp_path / "segment-a.mp4")
    second = make_media(tmp_path / "segment-b.mp4")
    manifest = manifest_for(
        tmp_path,
        [file_entry(first, "segment-a", "output-a"), file_entry(second, "segment-b", "output-b")],
        target_duration_ms=2_000,
        recording_class="abnormal",
        retained_file_ids=["segment-a", "segment-b"],
        default_file_ids=["segment-a"],
    )

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    retained = report["scopes"]["all_retained"]["local_copy_duration_comparison"]
    default = report["scopes"]["default_delivery"]["local_copy_duration_comparison"]
    assert retained["status"] == "pass"
    assert [stream["measured_duration_ms_exact"] for stream in retained["streams"]] == [
        "2000",
        "2000",
    ]
    assert default["status"] == "fail"
    assert [stream["shortfall_ms"] for stream in default["streams"]] == ["1000", "1000"]
    assert report["scopes"]["default_delivery"]["file_ids"] == ["segment-a"]


def test_missing_second_main_video_keeps_scope_unknown(tmp_path: Path):
    media_path = make_media(tmp_path / "single-video.mp4", video_stream_count=1)
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    file_result = report["files"][0]
    assert len(file_result["media"]["main_videos"]) == 1
    scope = report["scopes"]["all_retained"]["local_copy_duration_comparison"]
    assert scope["status"] == "unknown"
    assert scope["streams"][0]["status"] == "pass"
    assert scope["streams"][1]["status"] == "unknown"


def test_native_timelapse_without_independent_rate_and_endpoint_stays_unknown(tmp_path: Path):
    media_path = make_media(tmp_path / "timelapse.mp4")
    manifest = manifest_for(
        tmp_path,
        [file_entry(media_path, "segment-a", "output-a")],
        recording_kind="native_timelapse",
    )

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report["expected_playback"]["status"] == "unknown"
    assert report["expected_playback"]["expected_frame_count"] is None
    assert "missing_independent_playback_frame_rate" in report["expected_playback"]["unknown_reasons"]
    assert "missing_independent_sample_endpoint_rule" in report["expected_playback"]["unknown_reasons"]
    assert report["scopes"]["all_retained"]["source_duration_status"] == "unknown"


def test_cli_override_is_recorded_and_nonzero_exit_does_not_erase_decoder_diagnostics(
    tmp_path: Path,
):
    media_path = make_media(tmp_path / "short-segment.mp4", duration_seconds="0.9")
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])
    real_ffmpeg = shutil.which("ffmpeg")
    assert real_ffmpeg is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "ffmpeg"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"{shlex.quote(real_ffmpeg)} \"$@\"\n"
        "result=$?\n"
        "printf '%s\\n' 'nonfatal fixture decoder diagnostic' >&2\n"
        "exit \"$result\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]

    completed, report = run_validator(
        manifest,
        tmp_path / "report.json",
        tolerance_ms=99,
        environment=environment,
    )

    assert completed.returncode == 0
    assert report["shortfall_tolerance_ms"] == 99
    comparison = report["scopes"]["all_retained"]["local_copy_duration_comparison"]
    assert comparison["status"] == "fail"
    assert all(check["exit_code"] == 0 for check in report["files"][0]["media"]["decode_checks"])
    assert all(
        "nonfatal fixture decoder diagnostic" in check["diagnostics"]
        for check in report["files"][0]["media"]["decode_checks"]
    )


@pytest.mark.parametrize("collision_target", ["manifest", "media"])
def test_report_output_cannot_replace_its_manifest_or_media_input(
    tmp_path: Path, collision_target: str
):
    media_path = make_media(tmp_path / "segment-a.mp4")
    original_media = media_path.read_bytes()
    manifest_path = manifest_for(
        tmp_path, [file_entry(media_path, "segment-a", "output-a")]
    )
    original_manifest = manifest_path.read_bytes()
    output_path = manifest_path if collision_target == "manifest" else media_path

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert media_path.read_bytes() == original_media
    assert manifest_path.read_bytes() == original_manifest


def test_report_writer_does_not_reuse_a_temporary_path_that_is_a_media_input(
    tmp_path: Path,
):
    output_path = tmp_path / "report.json"
    media_path = tmp_path / "report.json.tmp"
    generated_media = make_media(tmp_path / "report.json.tmp.mp4")
    generated_media.rename(media_path)
    original_media = media_path.read_bytes()
    manifest_path = manifest_for(
        tmp_path, [file_entry(media_path, "segment-a", "output-a")]
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert media_path.read_bytes() == original_media
    assert json.loads(output_path.read_text(encoding="utf-8"))["execution_status"] == "completed"


def test_invalid_manifest_error_never_overwrites_a_referenced_media_input(
    tmp_path: Path,
):
    media_path = make_media(tmp_path / "segment-a.mp4")
    original_media = media_path.read_bytes()
    manifest_path = manifest_for(
        tmp_path, [file_entry(media_path, "segment-a", "output-a")]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_manifest = manifest_path.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(manifest_path),
            "--output",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert media_path.read_bytes() == original_media
    assert manifest_path.read_bytes() == original_manifest
