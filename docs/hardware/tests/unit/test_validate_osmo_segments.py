from __future__ import annotations

import importlib.util
import io
import sys
import threading
from fractions import Fraction
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "validate_osmo_segments.py"


def load_subject():
    spec = importlib.util.spec_from_file_location("validate_osmo_segments", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def valid_manifest():
    return {
        "manifest_version": 1,
        "acceptance_id": "acceptance-1",
        "source_action_instance_id": "action-1",
        "recording_kind": "ordinary_recording",
        "recording_class": "normal",
        "target_duration_ms": 1_000,
        "target_basis_ref": "task.json#action-1",
        "retained_scope_status": "complete",
        "retained_file_ids": ["segment-1"],
        "default_scope_status": "complete",
        "default_file_ids": ["segment-1"],
        "files": [
            {
                "file_id": "segment-1",
                "output_id": "output-1",
                "role": "original",
                "source_device_path": "DCIM/CAM001/segment-1.OSV",
                "source_size_bytes": 10,
                "source_sha256": "a" * 64,
                "source_evidence_ref": "capture-record.json#file-1",
                "source_locator_basis_ref": "camera-readback.json#file-1-path",
                "local_copy_path": "segment-1.OSV",
            }
        ],
    }


class MemoryPipe(io.RawIOBase):
    def __init__(self, content: bytes, fault: str | None = None):
        super().__init__()
        self.content = memoryview(content)
        self.offset = 0
        self.fault = fault
        self.closed_for_test = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.fault == "read":
            raise OSError("simulated diagnostic pipe read failure")
        if size is None or size < 0:
            size = len(self.content) - self.offset
        end = min(len(self.content), self.offset + size)
        result = self.content[self.offset:end].tobytes()
        self.offset = end
        return result

    def readinto(self, buffer):
        if self.fault == "read":
            raise OSError("simulated diagnostic pipe read failure")
        size = min(len(buffer), len(self.content) - self.offset)
        buffer[:size] = self.content[self.offset : self.offset + size]
        self.offset += size
        return size

    def close(self):
        self.closed_for_test = True
        super().close()
        if self.fault == "pipe_close":
            raise OSError("simulated diagnostic pipe close failure")


class MemoryDiagnosticOutput:
    def __init__(self, fault: str | None = None):
        self.fault = fault
        self.content = bytearray()
        self.closed_for_test = False

    def write(self, data: bytes) -> int:
        if self.fault == "write":
            raise OSError("simulated diagnostic write failure")
        self.content.extend(data)
        return len(data)

    def flush(self) -> None:
        if self.fault == "flush":
            raise OSError("simulated final diagnostic flush ENOSPC")

    def close(self) -> None:
        self.closed_for_test = True
        self.flush()
        if self.fault == "close":
            raise OSError("simulated diagnostic close failure")


class MemoryDiagnosticPath:
    def __init__(self, fault: str | None, output: MemoryDiagnosticOutput):
        self.fault = fault
        self.output = output

    def open(self, mode: str):
        assert mode == "wb"
        if self.fault == "open":
            raise OSError("simulated diagnostic open failure")
        return self.output

    def unlink(self, missing_ok: bool = False) -> None:
        assert missing_ok


class MemoryDiagnostics:
    def __init__(self, fault: str | None):
        self.output = MemoryDiagnosticOutput(fault)
        self.path = MemoryDiagnosticPath(fault, self.output)

    def create(self):
        return "memory-token", self.path

    def marker(self, token: str):
        return {"diagnostic": token}


class MemoryProcess:
    def __init__(self, *, stdout_fault: str | None = None, stderr_fault: str | None = None):
        self.stdout = MemoryPipe(b"0,0.0\n0,0.1\n", fault=stdout_fault)
        self.stderr = MemoryPipe(b"diagnostic\n", fault=stderr_fault)
        self.returncode = 17
        self.waited = False
        self.killed = False

    def poll(self):
        return self.returncode if self.waited else None

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self):
        self.waited = True
        return self.returncode


def test_100ms_tolerance_accepts_9900ms_against_10000ms():
    subject = load_subject()

    result = subject.compare_duration(Fraction(10_000), Fraction(9_900), 100)

    assert result["status"] == "pass"
    assert result["shortfall_ms"] == "100"
    assert result["tolerance_ms"] == 100


def test_100ms_tolerance_rejects_9899ms_against_10000ms():
    subject = load_subject()

    result = subject.compare_duration(Fraction(10_000), Fraction(9_899), 100)

    assert result["status"] == "fail"
    assert result["shortfall_ms"] == "101"


def test_zero_tolerance_rejects_any_positive_shortfall():
    subject = load_subject()

    result = subject.compare_duration(Fraction(10_000), Fraction(9_999), 0)

    assert result["status"] == "fail"
    assert result["shortfall_ms"] == "1"


def test_configured_tolerance_is_used_without_rounding():
    subject = load_subject()

    result = subject.compare_duration(Fraction(10_000), Fraction(9_750), 250)

    assert result["status"] == "pass"
    assert result["shortfall_ms"] == "250"


@pytest.mark.parametrize("value", [-1, True, 1.5, "100", None])
def test_shortfall_tolerance_requires_a_nonnegative_integer(value):
    subject = load_subject()

    with pytest.raises((TypeError, ValueError)):
        subject.parse_shortfall_tolerance_ms(value)


def test_unknown_measured_duration_stays_unknown_with_its_reason():
    subject = load_subject()

    result = subject.compare_duration(
        Fraction(10_000), None, 100, unknown_reason="stream_duration_unavailable"
    )

    assert result["status"] == "unknown"
    assert result["shortfall_ms"] is None
    assert result["unknown_reasons"] == ["stream_duration_unavailable"]


def test_two_video_streams_are_compared_independently():
    subject = load_subject()

    result = subject.compare_video_stream_durations(
        Fraction(10_000), [Fraction(9_900), Fraction(9_800)], 100
    )

    assert [stream["status"] for stream in result["streams"]] == ["pass", "fail"]
    assert [stream["shortfall_ms"] for stream in result["streams"]] == ["100", "200"]
    assert result["status"] == "fail"


def test_native_timelapse_expected_duration_uses_independent_endpoint_and_rate_basis():
    subject = load_subject()

    result = subject.derive_expected_playback_duration(
        {
            "recording_kind": "native_timelapse",
            "target_duration_ms": 10_000,
            "target_basis_ref": "task-record#target",
            "sampling_interval_ms": 2_000,
            "sampling_interval_basis_ref": "task-record#interval",
            "playback_frame_rate": "30/1",
            "sample_endpoint_rule": "start_included_end_excluded",
            "frame_rate_basis_ref": "camera-readback#fps",
            "endpoint_rule_basis_ref": "sampling-contract#endpoints",
        }
    )

    assert result["status"] == "known"
    assert result["expected_frame_count"] == 5
    assert result["expected_duration_ms_exact"] == "500/3"


def test_native_timelapse_missing_independent_frame_rate_or_endpoint_stays_unknown():
    subject = load_subject()

    result = subject.derive_expected_playback_duration(
        {
            "recording_kind": "native_timelapse",
            "target_duration_ms": 10_000,
            "target_basis_ref": "task-record#target",
            "sampling_interval_ms": 2_000,
        }
    )

    assert result["status"] == "unknown"
    assert result["expected_frame_count"] is None
    assert result["expected_duration_ms_exact"] is None
    assert "missing_independent_playback_frame_rate" in result["unknown_reasons"]
    assert "missing_independent_sample_endpoint_rule" in result["unknown_reasons"]


def test_native_timelapse_requires_a_task_basis_for_sampling_interval():
    subject = load_subject()

    result = subject.derive_expected_playback_duration(
        {
            "recording_kind": "native_timelapse",
            "target_duration_ms": 10_000,
            "target_basis_ref": "task-record#target",
            "sampling_interval_ms": 2_000,
            "playback_frame_rate": "30/1",
            "sample_endpoint_rule": "start_included_end_excluded",
            "frame_rate_basis_ref": "camera-readback#fps",
            "endpoint_rule_basis_ref": "sampling-contract#endpoints",
        }
    )

    assert result["status"] == "unknown"
    assert result["expected_frame_count"] is None
    assert "missing_sampling_interval_basis" in result["unknown_reasons"]


def test_manifest_preserves_independent_file_and_source_copy_identity():
    subject = load_subject()

    result = subject.validate_manifest(valid_manifest())

    assert result["retained_file_ids"] == ["segment-1"]
    assert result["default_file_ids"] == ["segment-1"]
    assert result["files"][0]["source_device_path"] == "DCIM/CAM001/segment-1.OSV"
    assert result["files"][0]["output_id"] == "output-1"
    assert result["files"][0]["source_evidence_ref"] == "capture-record.json#file-1"


def test_manifest_preserves_missing_source_action_identity_without_inventing_one():
    subject = load_subject()
    manifest = valid_manifest()
    manifest["source_action_instance_id"] = None

    result = subject.validate_manifest(manifest)

    assert result["source_action_instance_id"] is None
    assert result["target_basis_ref"] == "task.json#action-1"


def test_manifest_can_identify_a_local_file_without_claiming_a_formal_output_id():
    subject = load_subject()
    manifest = valid_manifest()
    manifest["files"][0]["output_id"] = None

    result = subject.validate_manifest(manifest)

    assert result["files"][0]["file_id"] == "segment-1"
    assert result["files"][0]["output_id"] is None


def test_normal_manifest_requires_default_scope_to_equal_all_retained_originals():
    subject = load_subject()
    manifest = valid_manifest()
    manifest["default_file_ids"] = []

    with pytest.raises(subject.ManifestError):
        subject.validate_manifest(manifest)


def test_abnormal_manifest_keeps_partial_default_scope_separate_from_retained_scope():
    subject = load_subject()
    manifest = valid_manifest()
    second = dict(manifest["files"][0])
    second.update(
        file_id="segment-2",
        output_id="output-2",
        source_device_path="DCIM/CAM001/segment-2.OSV",
        source_locator_basis_ref="camera-readback.json#file-2-path",
        local_copy_path="segment-2.OSV",
    )
    manifest["files"].append(second)
    manifest.update(
        recording_class="abnormal",
        retained_file_ids=["segment-1", "segment-2"],
        default_file_ids=["segment-1"],
    )

    result = subject.validate_manifest(manifest)

    assert result["retained_file_ids"] == ["segment-1", "segment-2"]
    assert result["default_file_ids"] == ["segment-1"]


@pytest.mark.parametrize("recording_class", ["normal", "abnormal"])
def test_complete_retained_scope_must_cover_every_retained_original(recording_class):
    subject = load_subject()
    manifest = valid_manifest()
    second = dict(manifest["files"][0])
    second.update(
        file_id="segment-2",
        output_id="output-2",
        source_device_path="DCIM/CAM001/segment-2.OSV",
        source_locator_basis_ref="camera-readback.json#file-2-path",
        local_copy_path="segment-2.OSV",
    )
    manifest["files"].append(second)
    manifest.update(
        recording_class=recording_class,
        retained_scope_status="complete",
        retained_file_ids=["segment-1"],
        default_scope_status="unknown",
        default_file_ids=["segment-1"],
    )

    with pytest.raises(subject.ManifestError, match="retained_file_ids must cover"):
        subject.validate_manifest(manifest)


def test_unknown_retained_scope_can_keep_a_partial_list_of_retained_originals():
    subject = load_subject()
    manifest = valid_manifest()
    second = dict(manifest["files"][0])
    second.update(
        file_id="segment-2",
        output_id="output-2",
        source_device_path="DCIM/CAM001/segment-2.OSV",
        source_locator_basis_ref="camera-readback.json#file-2-path",
        local_copy_path="segment-2.OSV",
    )
    manifest["files"].append(second)
    manifest.update(
        retained_scope_status="unknown",
        retained_file_ids=["segment-1"],
        default_scope_status="unknown",
        default_file_ids=["segment-1"],
    )

    result = subject.validate_manifest(manifest)

    assert result["retained_scope_status"] == "unknown"
    assert result["retained_file_ids"] == ["segment-1"]


def test_preview_and_observed_empty_files_do_not_expand_complete_retained_scope():
    subject = load_subject()
    manifest = valid_manifest()
    preview = dict(manifest["files"][0])
    preview.update(
        file_id="preview-1",
        output_id="preview-output-1",
        role="preview",
        source_device_path="DCIM/CAM001/segment-1.LRF",
        source_locator_basis_ref="camera-readback.json#preview-1-path",
        local_copy_path="segment-1.LRF",
        preview_of_output_id="output-1",
    )
    empty = dict(manifest["files"][0])
    empty.update(
        file_id="empty-tail",
        output_id=None,
        source_device_path="DCIM/CAM001/empty-tail.OSV",
        source_locator_basis_ref="camera-readback.json#empty-tail-path",
        source_size_bytes=0,
        file_status="observed_empty",
        local_copy_path="empty-tail.OSV",
    )
    manifest["files"].extend([preview, empty])

    result = subject.validate_manifest(manifest)

    assert result["retained_file_ids"] == ["segment-1"]


@pytest.mark.parametrize("local_copy_path", ["segment-1.OSV", "second-copy.OSV"])
def test_complete_scope_rejects_duplicate_reliable_source_locator(local_copy_path):
    subject = load_subject()
    manifest = valid_manifest()
    second = dict(manifest["files"][0])
    second.update(
        file_id="segment-alias",
        output_id="output-alias",
        source_locator_basis_ref="camera-readback.json#independent-record-for-same-path",
        local_copy_path=local_copy_path,
    )
    manifest["files"].append(second)
    manifest.update(
        recording_class="abnormal",
        retained_file_ids=["segment-1", "segment-alias"],
        default_scope_status="unknown",
        default_file_ids=["segment-1"],
    )

    with pytest.raises(subject.ManifestError, match="duplicate reliable source locator"):
        subject.validate_manifest(manifest)


def test_same_bytes_at_distinct_reliable_source_paths_remain_distinct_files():
    subject = load_subject()
    manifest = valid_manifest()
    second = dict(manifest["files"][0])
    second.update(
        file_id="segment-2",
        output_id="output-2",
        source_device_path="DCIM/CAM001/segment-2.OSV",
        source_locator_basis_ref=manifest["files"][0]["source_locator_basis_ref"],
        local_copy_path="segment-2.OSV",
    )
    manifest["files"].append(second)
    manifest.update(
        recording_class="abnormal",
        retained_file_ids=["segment-1", "segment-2"],
        default_scope_status="unknown",
        default_file_ids=["segment-1"],
    )

    result = subject.validate_manifest(manifest)

    assert result["retained_file_ids"] == ["segment-1", "segment-2"]


def test_local_source_hash_cannot_make_a_missing_locator_reliable():
    subject = load_subject()
    entry = valid_manifest()["files"][0]
    entry["source_locator_basis_ref"] = None

    assert subject._source_locator_key(entry) is None


@pytest.mark.parametrize(
    ("source_device_path", "source_locator_basis_ref", "expected_locator"),
    [
        (".", "camera-readback.json#dot", None),
        ("segment/..", "camera-readback.json#parent", None),
        ("CAM001/segment/..", "camera-readback.json#directory", None),
        ("DCIM/CAM001/segment-1.OSV", "camera-readback.json#valid", "DCIM/CAM001/segment-1.OSV"),
        (None, "camera-readback.json#missing-path", None),
        ("DCIM/CAM001/segment-1.OSV", None, None),
    ],
)
def test_manifest_locator_qualification_matches_normalized_file_path(
    source_device_path, source_locator_basis_ref, expected_locator
):
    subject = load_subject()
    manifest = valid_manifest()
    entry = manifest["files"][0]
    entry["source_device_path"] = source_device_path
    entry["source_locator_basis_ref"] = source_locator_basis_ref

    validated = subject.validate_manifest(manifest)
    locator, unknown_reasons = subject._assess_source_locator(entry)

    assert subject._source_locator_key(entry) == expected_locator
    assert locator == expected_locator
    assert bool(unknown_reasons) is (expected_locator is None)
    assert validated["files"][0]["source_device_path"] == source_device_path


@pytest.mark.parametrize(
    ("entrypoint", "fault"),
    [
        (entrypoint, fault)
        for entrypoint in ("command", "frame")
        for fault in (
            "open",
            "write",
            "flush",
            "close",
            "read",
            "pipe_close",
            "stdout_read",
            "stdout_close",
        )
    ]
    + [("frame", "stdout_wrapper")],
)
def test_subprocess_capture_errors_are_reported_after_all_pipes_and_process_settle(
    monkeypatch, entrypoint, fault
):
    subject = load_subject()
    stdout_fault = {"stdout_read": "read", "stdout_close": "pipe_close"}.get(fault)
    stderr_fault = fault if fault in {"read", "pipe_close"} else None
    process = MemoryProcess(stdout_fault=stdout_fault, stderr_fault=stderr_fault)
    diagnostics = MemoryDiagnostics(
        fault if fault in {"open", "write", "flush", "close"} else None
    )
    spawned_threads = []
    thread_errors = []
    original_thread = subject.threading.Thread

    class TrackingThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned_threads.append(self)

    monkeypatch.setattr(subject.threading, "Thread", TrackingThread)
    monkeypatch.setattr(
        subject.threading,
        "excepthook",
        lambda args: thread_errors.append(args.exc_value),
    )
    monkeypatch.setattr(
        subject.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    if entrypoint == "frame" and fault == "stdout_wrapper":
        def fail_text_stream(*args, **kwargs):
            raise OSError("simulated frame stdout wrapper failure")

        monkeypatch.setattr(subject.io, "TextIOWrapper", fail_text_stream)

    with pytest.raises(subject.DiagnosticCaptureError) as captured:
        if entrypoint == "command":
            subject._run_command(["synthetic-tool"], diagnostics)
        else:
            subject._frame_coverage(Path("synthetic-media"), "ffprobe", {0}, diagnostics)

    assert captured.value.exit_code == (
        -9 if fault in {"read", "stdout_read", "stdout_wrapper"} else 17
    )
    assert captured.value.capture_errors
    assert process.waited
    assert process.stdout.closed_for_test
    assert process.stderr.closed_for_test
    assert all(not thread.is_alive() for thread in spawned_threads)
    assert not thread_errors
    if fault in {"read", "stdout_read", "stdout_wrapper"}:
        assert process.killed


def test_normal_manifest_can_preserve_unknown_collection_or_default_scope():
    subject = load_subject()
    manifest = valid_manifest()
    manifest.update(
        retained_scope_status="unknown",
        default_scope_status="unknown",
        default_file_ids=[],
    )

    result = subject.validate_manifest(manifest)

    assert result["retained_scope_status"] == "unknown"
    assert result["default_scope_status"] == "unknown"
    assert result["default_file_ids"] == []


def test_manifest_rejects_boolean_target_duration():
    subject = load_subject()
    manifest = valid_manifest()
    manifest["target_duration_ms"] = True

    with pytest.raises(subject.ManifestError):
        subject.validate_manifest(manifest)
