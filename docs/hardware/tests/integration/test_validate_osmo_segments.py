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
        "source_locator_basis_ref": "synthetic-fixture#directory-entry-listing",
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
    batch_size: int | None = None,
    environment: dict[str, str] | None = None,
    memory_limit_mb: int | None = None,
    timeout_seconds: int = 30,
    parse_output: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    command = [sys.executable, str(SCRIPT_PATH), "--manifest", str(manifest_path), "--output", str(output_path)]
    if tolerance_ms is not None:
        command.extend(["--shortfall-tolerance-ms", str(tolerance_ms)])
    if batch_size is not None:
        command.extend(["--batch-size", str(batch_size)])
    preexec_fn = None
    if memory_limit_mb is not None:
        import resource

        address_space_limit = memory_limit_mb * 1024 * 1024

        def set_address_space_limit() -> None:
            resource.setrlimit(
                resource.RLIMIT_AS,
                (address_space_limit, address_space_limit),
            )

        preexec_fn = set_address_space_limit
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        preexec_fn=preexec_fn,
        timeout=timeout_seconds,
    )
    report = None
    if parse_output and output_path.is_file():
        try:
            report = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return completed, report


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


def test_decoder_nonzero_exit_preserves_large_diagnostic_when_cli_succeeds(
    tmp_path: Path,
):
    media_path = make_media(tmp_path / "segment-a.mp4")
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "ffmpeg"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:] == ['-version']:\n"
        "    print('ffmpeg version synthetic-wrapper')\n"
        "    raise SystemExit(0)\n"
        "sys.stderr.write('decoder-diagnostic-' * 262144)\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(17)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]
    diagnostic = "decoder-diagnostic-" * 262144

    completed, report = run_validator(
        manifest,
        tmp_path / "report.json",
        environment=environment,
        memory_limit_mb=384,
        timeout_seconds=60,
    )

    assert completed.returncode == 0
    assert report is not None
    checks = report["files"][0]["media"]["decode_checks"]
    assert len(checks) == 3
    assert all(check["exit_code"] == 17 for check in checks)
    assert all(check["status"] == "decode_failed" for check in checks)
    assert all(check["diagnostics"] == diagnostic for check in checks)
    diagnostic_findings = [
        finding for finding in report["findings"]
        if finding.get("category") == "decode_diagnostic"
    ]
    assert len(diagnostic_findings) == 3
    assert all(finding["diagnostics"] == diagnostic for finding in diagnostic_findings)


def test_batch_sizes_preserve_file_scope_duration_and_diagnostic_results(tmp_path: Path):
    media_path = make_media(tmp_path / "segment-a.mp4")
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
        "printf '%s\\n' 'batch-stable decoder diagnostic' >&2\n"
        "exit \"$result\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]

    first_exit, first = run_validator(
        manifest,
        tmp_path / "report-batch-1.json",
        batch_size=1,
        environment=environment,
    )
    second_exit, second = run_validator(
        manifest,
        tmp_path / "report-batch-7.json",
        batch_size=7,
        environment=environment,
    )

    assert first_exit.returncode == second_exit.returncode == 0
    assert first is not None and second is not None
    assert first["processing_batch_size"] == 1
    assert second["processing_batch_size"] == 7
    assert all(
        check["diagnostics"] == "batch-stable decoder diagnostic\n"
        for check in first["files"][0]["media"]["decode_checks"]
    )
    for result in (first, second):
        result.pop("generated_at_utc")
        result.pop("processing_batch_size")
    assert first == second


def test_complete_manifest_cannot_omit_another_retained_original(tmp_path: Path):
    readable = make_media(tmp_path / "segment-a.mp4")
    unreadable_path = tmp_path / "segment-b.mp4"
    entries = [
        file_entry(readable, "segment-a", "output-a"),
        {
            "file_id": "segment-b",
            "output_id": "output-b",
            "role": "original",
            "file_status": "retained",
            "source_device_path": "DCIM/CAM001/segment-b.OSV",
            "source_size_bytes": 123,
            "source_sha256": "b" * 64,
            "source_evidence_ref": "synthetic-fixture#segment-b-bytes",
            "source_locator_basis_ref": "synthetic-fixture#segment-b-path",
            "local_copy_path": unreadable_path.name,
        },
    ]
    manifest = manifest_for(
        tmp_path,
        entries,
        retained_file_ids=["segment-a"],
        default_file_ids=["segment-a"],
    )
    output = tmp_path / "report.json"

    completed, report = run_validator(manifest, output)

    assert completed.returncode == 2
    assert "retained_file_ids must cover every retained original" in completed.stderr
    assert report is None
    assert not output.exists()


def test_same_source_path_with_new_basis_and_copy_is_rejected_as_duplicate(tmp_path: Path):
    first_media = make_media(tmp_path / "segment-a.mp4")
    second_media = tmp_path / "segment-b.mp4"
    shutil.copyfile(first_media, second_media)
    first = file_entry(first_media, "segment-a", "output-a")
    second = file_entry(second_media, "segment-b", "output-b")
    second["source_device_path"] = first["source_device_path"]
    second["source_locator_basis_ref"] = "another-real-record#same-device-path"
    manifest = manifest_for(
        tmp_path,
        [first, second],
        target_duration_ms=2_000,
        recording_class="abnormal",
    )
    output = tmp_path / "report.json"

    completed, report = run_validator(manifest, output)

    assert completed.returncode == 2
    assert "duplicate reliable source locator" in completed.stderr
    assert report is None
    assert not output.exists()
    assert first_media.read_bytes() == second_media.read_bytes()


def test_same_hash_at_distinct_source_paths_counts_each_local_copy_once(tmp_path: Path):
    first_media = make_media(tmp_path / "segment-a.mp4")
    second_media = tmp_path / "segment-b.mp4"
    shutil.copyfile(first_media, second_media)
    first = file_entry(first_media, "segment-a", "output-a")
    second = file_entry(second_media, "segment-b", "output-b")
    second["source_locator_basis_ref"] = first["source_locator_basis_ref"]
    second["source_evidence_ref"] = first["source_evidence_ref"]
    manifest = manifest_for(
        tmp_path,
        [first, second],
        target_duration_ms=2_000,
        recording_class="abnormal",
    )

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report is not None
    assert report["files"][0]["source"]["sha256"] == report["files"][1]["source"]["sha256"]
    comparison = report["scopes"]["all_retained"]["local_copy_duration_comparison"]
    assert comparison["status"] == "pass"
    assert [stream["measured_duration_ms_exact"] for stream in comparison["streams"]] == [
        "2000",
        "2000",
    ]


def test_missing_source_locator_keeps_matching_bytes_but_source_scope_unknown(
    tmp_path: Path,
):
    media_path = make_media(tmp_path / "segment-a.mp4")
    entry = file_entry(media_path, "segment-a", "output-a")
    entry["source_locator_basis_ref"] = None
    manifest = manifest_for(tmp_path, [entry])

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report is not None
    local_copy = report["files"][0]["local_copy"]
    assert local_copy["byte_identity_status"] == "match"
    assert local_copy["source_locator_status"] == "unknown"
    assert local_copy["identity_status"] == "unknown"
    assert report["scopes"]["all_retained"]["source_copy_identity_status"] == "unknown"
    assert report["scopes"]["all_retained"]["source_duration_status"] == "unknown"


def test_non_file_locator_keeps_bytes_and_media_but_source_scope_unknown(
    tmp_path: Path,
):
    first_media = make_media(tmp_path / "segment-a.mp4")
    second_media = tmp_path / "segment-b.mp4"
    shutil.copyfile(first_media, second_media)
    first = file_entry(first_media, "segment-a", "output-a")
    second = file_entry(second_media, "segment-b", "output-b")
    first["source_device_path"] = "."
    second["source_device_path"] = "segment/.."
    manifest = manifest_for(
        tmp_path,
        [first, second],
        target_duration_ms=2_000,
        recording_class="abnormal",
        default_scope_status="unknown",
    )

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report is not None
    for file_result in report["files"]:
        assert file_result["local_copy"]["byte_identity_status"] == "match"
        assert file_result["local_copy"]["source_locator_status"] == "unknown"
        assert file_result["local_copy"]["identity_status"] == "unknown"
        assert "source_device_path_not_file_locator" in file_result["local_copy"][
            "identity_unknown_reasons"
        ]
        assert file_result["media"]["status"] == "probed"
    retained = report["scopes"]["all_retained"]
    assert retained["local_copy_duration_comparison"]["status"] == "pass"
    assert retained["source_copy_identity_status"] == "unknown"
    assert retained["source_duration_status"] == "unknown"


def test_diagnostic_close_failure_keeps_old_report_inputs_and_tempdir(tmp_path: Path):
    media_path = make_media(tmp_path / "segment-a.mp4")
    manifest_path = manifest_for(
        tmp_path, [file_entry(media_path, "segment-a", "output-a")]
    )
    report_path = tmp_path / "report.json"
    old_report = b"previous report bytes must remain untouched\n"
    report_path.write_bytes(old_report)
    original_manifest = manifest_path.read_bytes()
    original_media = media_path.read_bytes()
    temporary_root = tmp_path / "isolated-temp"
    temporary_root.mkdir()
    launcher = tmp_path / "run_with_diagnostic_close_failure.py"
    launcher.write_text(
        "from pathlib import Path\n"
        "import runpy, sys\n"
        "original_open = Path.open\n"
        "class FailingClose:\n"
        "    def __init__(self, wrapped): self.wrapped = wrapped\n"
        "    def write(self, data): return self.wrapped.write(data)\n"
        "    def flush(self): return self.wrapped.flush()\n"
        "    def close(self):\n"
        "        self.wrapped.close()\n"
        "        raise OSError('simulated final buffered flush ENOSPC')\n"
        "def injected_open(path, mode='r', *args, **kwargs):\n"
        "    opened = original_open(path, mode, *args, **kwargs)\n"
        "    if mode == 'wb' and path.suffix == '.stderr':\n"
        "        return FailingClose(opened)\n"
        "    return opened\n"
        "Path.open = injected_open\n"
        "script, *arguments = sys.argv[1:]\n"
        "sys.argv = [script, *arguments]\n"
        "runpy.run_path(script, run_name='__main__')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["TMPDIR"] = str(temporary_root)
    completed = subprocess.run(
        [
            sys.executable,
            str(launcher),
            str(SCRIPT_PATH),
            "--manifest",
            str(manifest_path),
            "--output",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "simulated final buffered flush ENOSPC" in completed.stderr
    assert report_path.read_bytes() == old_report
    assert manifest_path.read_bytes() == original_manifest
    assert media_path.read_bytes() == original_media
    assert list(temporary_root.iterdir()) == []


def test_frame_stream_and_large_stderr_are_consumed_without_truncation(tmp_path: Path):
    media_path = make_media(tmp_path / "segment-a.mp4")
    manifest = manifest_for(tmp_path, [file_entry(media_path, "segment-a", "output-a")])
    real_ffprobe = shutil.which("ffprobe")
    assert real_ffprobe is not None
    wrapper = tmp_path / "ffprobe-streaming"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"real = {real_ffprobe!r}\n"
        "args = sys.argv[1:]\n"
        "if '-show_frames' not in args:\n"
        "    os.execv(real, [real, *args])\n"
        "sys.stderr.write('frame-probe-diagnostic-' * 16384)\n"
        "sys.stderr.flush()\n"
        "for index in range(3_000_000):\n"
        "    timestamp = f'{index / 10:.1f}'\n"
        "    sys.stdout.write(f'0,{timestamp}\\n1,{timestamp}\\n')\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper_link = wrapper_dir / "ffprobe"
    wrapper_link.symlink_to(wrapper)
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]

    completed, report = run_validator(
        manifest,
        tmp_path / "report.json",
        environment=environment,
        memory_limit_mb=384,
        timeout_seconds=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert report is not None
    coverage = report["files"][0]["media"]["main_videos"][0]["frame_coverage"]
    assert coverage["decoded_frame_timestamp_count"] == 3_000_000
    assert coverage["last_timestamp_seconds_exact"] == "2999999/10"
    assert coverage["probe_stderr"] == "frame-probe-diagnostic-" * 16384


def test_malformed_manifest_does_not_replace_existing_report(tmp_path: Path):
    media_path = make_media(tmp_path / "segment-a.mp4")
    manifest_path = manifest_for(
        tmp_path, [file_entry(media_path, "segment-a", "output-a")]
    )
    with manifest_path.open("a", encoding="utf-8") as stream:
        stream.write(" ")
        stream.write('{"files":[{"file_id":"partial"')
    output = tmp_path / "report.json"
    output.write_text("previous complete report\n", encoding="utf-8")

    completed, report = run_validator(manifest_path, output)

    assert completed.returncode == 2
    assert report is None
    assert output.read_text(encoding="utf-8") == "previous complete report\n"


def test_streaming_manifest_accepts_json_decimal_in_ignored_extension_field(
    tmp_path: Path,
):
    media_path = make_media(tmp_path / "segment-a.mp4")
    entry = file_entry(media_path, "segment-a", "output-a")
    entry["producer_extension"] = {"ratio": 1.25, "sample": 2.0}
    manifest = manifest_for(tmp_path, [entry])

    completed, report = run_validator(manifest, tmp_path / "report.json")

    assert completed.returncode == 0
    assert report is not None
    assert report["files"][0]["file_id"] == "segment-a"


def test_large_manifest_and_report_fit_bounded_process_memory(tmp_path: Path):
    import ijson

    file_count = 100_000
    manifest_path = tmp_path / "large-manifest.json"
    empty_copy_path = tmp_path / "empty-copy.OSV"
    empty_copy_path.touch()
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    with manifest_path.open("w", encoding="utf-8") as stream:
        stream.write(
            '{"manifest_version":1,'
            '"acceptance_id":"large-manifest-case",'
            '"source_action_instance_id":null,'
            '"recording_kind":"ordinary_recording",'
            '"recording_class":"normal",'
            '"target_duration_ms":1000,'
            '"target_basis_ref":"task-record#target",'
            '"retained_scope_status":"unknown",'
            '"retained_file_ids":[],"default_scope_status":"unknown",'
            '"default_file_ids":[],"files":['
        )
        for index in range(file_count):
            if index:
                stream.write(",")
            entry = {
                "file_id": f"segment-{index:06d}",
                "output_id": None,
                "role": "original",
                "file_status": "observed_empty",
                "source_device_path": f"DCIM/CAM001/segment-{index:06d}.OSV",
                "source_size_bytes": 0,
                "source_sha256": empty_sha256,
                "source_evidence_ref": "capture-list#empty-file-identities",
                "source_locator_basis_ref": "capture-list#device-paths",
                "local_copy_path": empty_copy_path.name,
            }
            json.dump(entry, stream, separators=(",", ":"))
        stream.write("]}")
    output_path = tmp_path / "large-report.json"
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    for tool in ("ffprobe", "ffmpeg"):
        wrapper = wrapper_dir / tool
        wrapper.write_text(
            "#!/bin/sh\nprintf '%s\\n' '" + tool + " version synthetic-wrapper'\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(wrapper_dir) + os.pathsep + environment["PATH"]

    completed, report = run_validator(
        manifest_path,
        output_path,
        environment=environment,
        memory_limit_mb=192,
        timeout_seconds=60,
        parse_output=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert report is None
    with output_path.open("rb") as stream:
        file_results = ijson.items(stream, "files.item")
        observed_count = 0
        first_file_id = None
        last_file_id = None
        for file_result in file_results:
            observed_count += 1
            file_id = file_result["file_id"]
            if first_file_id is None:
                first_file_id = file_id
            last_file_id = file_id
    assert observed_count == file_count
    assert first_file_id == "segment-000000"
    assert last_file_id == f"segment-{file_count - 1:06d}"


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
