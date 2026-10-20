#!/usr/bin/env python3
"""验收本地 OSMO 360 II 分段副本，不与相机或 camctl 生产流程交互。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence


REPORT_SCHEMA_VERSION = 1
MANIFEST_VERSION = 1
DEFAULT_SHORTFALL_TOLERANCE_MS = 100
EXPECTED_MAIN_VIDEO_STREAMS = 2
SAMPLE_ENDPOINT_RULES = {
    "start_included_end_excluded",
    "start_and_end_included",
}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ManifestError(ValueError):
    """验收清单结构错误。"""


def parse_shortfall_tolerance_ms(value: object) -> int:
    """只接受非负整数，明确拒绝布尔值和带小数的数值。"""

    if type(value) is not int:
        raise TypeError("shortfall tolerance must be an integer number of milliseconds")
    if value < 0:
        raise ValueError("shortfall tolerance must be non-negative")
    return value


def _to_fraction(value: int | Decimal | Fraction) -> Fraction:
    if isinstance(value, bool):
        raise TypeError("boolean is not a duration")
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("duration must be finite")
        result = Fraction(value)
    elif type(value) is int:
        result = Fraction(value)
    else:
        raise TypeError("duration must be an integer, Decimal, Fraction, or None")
    if result < 0:
        raise ValueError("duration must be non-negative")
    return result


def _fraction_text(value: Fraction | None) -> str | None:
    if value is None:
        return None
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _fraction_decimal_text(value: Fraction | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 40
        decimal_value = Decimal(value.numerator) / Decimal(value.denominator)
    result = format(decimal_value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _decimal_fraction(raw: object) -> Fraction | None:
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return Fraction(value)


def _nonnegative_int(raw: object) -> int | None:
    if type(raw) is not int or raw < 0:
        return None
    return raw


def _positive_fraction(raw: object) -> Fraction | None:
    if not isinstance(raw, str):
        return None
    parts = raw.split("/")
    if len(parts) not in {1, 2} or any(not part.isdecimal() for part in parts):
        return None
    try:
        value = Fraction(int(parts[0]), int(parts[1]) if len(parts) == 2 else 1)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def compare_duration(
    expected_ms: int | Decimal | Fraction | None,
    measured_ms: int | Decimal | Fraction | None,
    tolerance_ms: int,
    *,
    unknown_reason: str | None = None,
) -> dict[str, Any]:
    """按精确有理数比较单路视频总时长，不先舍入到容限边界。"""

    tolerance = parse_shortfall_tolerance_ms(tolerance_ms)
    expected = _to_fraction(expected_ms) if expected_ms is not None else None
    measured = _to_fraction(measured_ms) if measured_ms is not None else None
    reasons: list[str] = []
    if expected is None:
        reasons.append("expected_playback_duration_unavailable")
    if measured is None:
        reasons.append(unknown_reason or "measured_video_duration_unavailable")
    if expected is None or measured is None:
        return {
            "status": "unknown",
            "expected_duration_ms_exact": _fraction_text(expected),
            "measured_duration_ms_exact": _fraction_text(measured),
            "shortfall_ms": None,
            "tolerance_ms": tolerance,
            "unknown_reasons": _unique(reasons),
        }

    shortfall = max(Fraction(0), expected - measured)
    return {
        "status": "pass" if shortfall <= tolerance else "fail",
        "expected_duration_ms_exact": _fraction_text(expected),
        "measured_duration_ms_exact": _fraction_text(measured),
        "shortfall_ms": _fraction_text(shortfall),
        "tolerance_ms": tolerance,
        "unknown_reasons": [],
    }


def compare_video_stream_durations(
    expected_ms: int | Decimal | Fraction | None,
    measured_durations_ms: Sequence[int | Decimal | Fraction | None],
    tolerance_ms: int,
) -> dict[str, Any]:
    """分别比较两路主视频；缺流时不把另一条流当作替代值。"""

    tolerance = parse_shortfall_tolerance_ms(tolerance_ms)
    streams: list[dict[str, Any]] = []
    for ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
        if ordinal >= len(measured_durations_ms):
            comparison = compare_duration(
                expected_ms,
                None,
                tolerance,
                unknown_reason="required_main_video_stream_missing",
            )
        else:
            comparison = compare_duration(
                expected_ms, measured_durations_ms[ordinal], tolerance
            )
        streams.append({"main_video_ordinal": ordinal, **comparison})

    status = _combine_statuses([item["status"] for item in streams])
    if len(measured_durations_ms) != EXPECTED_MAIN_VIDEO_STREAMS and status != "fail":
        status = "unknown"
    return {
        "status": status,
        "required_main_video_stream_count": EXPECTED_MAIN_VIDEO_STREAMS,
        "observed_main_video_stream_count": len(measured_durations_ms),
        "streams": streams,
    }


def derive_expected_playback_duration(task: dict[str, Any]) -> dict[str, Any]:
    """从任务事实和独立原生延时依据推导预期，不读媒体实测值。"""

    kind = task.get("recording_kind")
    target_ms = _nonnegative_int(task.get("target_duration_ms"))
    reasons: list[str] = []
    if target_ms is None or target_ms == 0:
        reasons.append("invalid_or_missing_task_target_duration")
    if not _has_text(task.get("target_basis_ref")):
        reasons.append("missing_independent_target_basis")

    if kind == "ordinary_recording":
        if reasons:
            return _unknown_expected(reasons)
        return {
            "status": "known",
            "expected_duration_ms_exact": str(target_ms),
            "expected_duration_ms_decimal": str(target_ms),
            "expected_frame_count": None,
            "derivation": "ordinary_recording_task_target",
            "basis": {"target_basis_ref": task["target_basis_ref"]},
            "unknown_reasons": [],
        }

    if kind != "native_timelapse":
        return _unknown_expected([*reasons, "unsupported_recording_kind"])

    interval_ms = _nonnegative_int(task.get("sampling_interval_ms"))
    if interval_ms is None or interval_ms == 0:
        reasons.append("invalid_or_missing_sampling_interval")
    if not _has_text(task.get("sampling_interval_basis_ref")):
        reasons.append("missing_sampling_interval_basis")
    frame_rate = _positive_fraction(task.get("playback_frame_rate"))
    if frame_rate is None:
        reasons.append("missing_independent_playback_frame_rate")
    if not _has_text(task.get("frame_rate_basis_ref")):
        reasons.append("missing_playback_frame_rate_basis")

    endpoint_rule = task.get("sample_endpoint_rule")
    if not isinstance(endpoint_rule, str) or endpoint_rule not in SAMPLE_ENDPOINT_RULES:
        reasons.append("missing_independent_sample_endpoint_rule")
    if not _has_text(task.get("endpoint_rule_basis_ref")):
        reasons.append("missing_sample_endpoint_rule_basis")

    if reasons:
        return _unknown_expected(reasons)

    assert target_ms is not None and interval_ms is not None and frame_rate is not None
    if endpoint_rule == "start_included_end_excluded":
        expected_frames = (target_ms + interval_ms - 1) // interval_ms
    else:
        expected_frames = target_ms // interval_ms + 1
    expected_ms = Fraction(expected_frames * 1000, 1) / frame_rate
    return {
        "status": "known",
        "expected_duration_ms_exact": _fraction_text(expected_ms),
        "expected_duration_ms_decimal": _fraction_decimal_text(expected_ms),
        "expected_frame_count": expected_frames,
        "derivation": "verified_capture_target_interval_endpoint_rule_and_playback_rate",
        "basis": {
            "target_basis_ref": task["target_basis_ref"],
            "sampling_interval_ms": interval_ms,
            "sampling_interval_basis_ref": task["sampling_interval_basis_ref"],
            "playback_frame_rate": task["playback_frame_rate"],
            "frame_rate_basis_ref": task["frame_rate_basis_ref"],
            "sample_endpoint_rule": endpoint_rule,
            "endpoint_rule_basis_ref": task["endpoint_rule_basis_ref"],
        },
        "unknown_reasons": [],
    }


def _unknown_expected(reasons: Iterable[str]) -> dict[str, Any]:
    return {
        "status": "unknown",
        "expected_duration_ms_exact": None,
        "expected_duration_ms_decimal": None,
        "expected_frame_count": None,
        "derivation": None,
        "basis": {},
        "unknown_reasons": _unique(list(reasons)),
    }


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _combine_statuses(statuses: Sequence[str]) -> str:
    if any(status == "fail" for status in statuses):
        return "fail"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_text(value: object, field: str) -> str:
    if not _has_text(value):
        raise ManifestError(f"{field} must be a non-empty string")
    return value.strip()


def validate_manifest(raw: object) -> dict[str, Any]:
    """核对清单结构，保持源身份、保留集合和默认集合相互独立。"""

    if not isinstance(raw, dict):
        raise ManifestError("manifest root must be a JSON object")
    version = raw.get("manifest_version")
    if type(version) is not int or version != MANIFEST_VERSION:
        raise ManifestError(f"manifest_version must be {MANIFEST_VERSION}")
    _require_text(raw.get("acceptance_id"), "acceptance_id")
    source_action_id = raw.get("source_action_instance_id")
    if source_action_id is not None:
        _require_text(source_action_id, "source_action_instance_id")
    recording_kind = raw.get("recording_kind")
    if not isinstance(recording_kind, str) or recording_kind not in {
        "ordinary_recording",
        "native_timelapse",
    }:
        raise ManifestError("recording_kind must be ordinary_recording or native_timelapse")
    recording_class = raw.get("recording_class")
    if not isinstance(recording_class, str) or recording_class not in {"normal", "abnormal"}:
        raise ManifestError("recording_class must be normal or abnormal")
    target_ms = _nonnegative_int(raw.get("target_duration_ms"))
    if target_ms is None or target_ms == 0:
        raise ManifestError("target_duration_ms must be a positive integer")

    for field, allowed in (
        ("retained_scope_status", {"complete", "unknown"}),
        ("default_scope_status", {"complete", "unknown"}),
    ):
        value = raw.get(field)
        if not isinstance(value, str) or value not in allowed:
            raise ManifestError(f"{field} must be one of {sorted(allowed)}")

    files = raw.get("files")
    if not isinstance(files, list):
        raise ManifestError("files must be an array")
    file_ids: set[str] = set()
    output_ids: set[str] = set()
    for position, entry in enumerate(files):
        prefix = f"files[{position}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{prefix} must be a JSON object")
        file_id = _require_text(entry.get("file_id"), f"{prefix}.file_id")
        if file_id in file_ids:
            raise ManifestError(f"duplicate file_id: {file_id}")
        file_ids.add(file_id)
        role = entry.get("role")
        if not isinstance(role, str) or role not in {"original", "preview"}:
            raise ManifestError(f"{prefix}.role must be original or preview")
        state = entry.get("file_status", "retained")
        if not isinstance(state, str) or state not in {"retained", "observed_empty"}:
            raise ManifestError(f"{prefix}.file_status must be retained or observed_empty")
        _require_text(entry.get("local_copy_path"), f"{prefix}.local_copy_path")
        source_size = entry.get("source_size_bytes")
        if source_size is not None and _nonnegative_int(source_size) is None:
            raise ManifestError(
                f"{prefix}.source_size_bytes must be a non-negative integer or null"
            )
        source_hash = entry.get("source_sha256")
        if source_hash is not None and (
            not isinstance(source_hash, str)
            or SHA256_PATTERN.fullmatch(source_hash) is None
        ):
            raise ManifestError(
                f"{prefix}.source_sha256 must be 64 hexadecimal characters or null"
            )
        output_id = entry.get("output_id")
        if output_id is not None:
            output_id = _require_text(output_id, f"{prefix}.output_id")
            if output_id in output_ids:
                raise ManifestError(f"duplicate output_id: {output_id}")
            output_ids.add(output_id)
        if state == "observed_empty" and output_id is not None:
            raise ManifestError(f"{prefix}.observed_empty must not have output_id")
        if state == "observed_empty" and source_size not in {None, 0}:
            raise ManifestError(
                f"{prefix}.observed_empty must have source_size_bytes 0 or null"
            )
        if role == "preview" and not _has_text(entry.get("preview_of_output_id")):
            raise ManifestError(f"{prefix}.preview files require preview_of_output_id")

    retained_ids = _unique_file_ids(raw.get("retained_file_ids"), file_ids, "retained_file_ids")
    default_ids = _unique_file_ids(raw.get("default_file_ids"), file_ids, "default_file_ids")
    file_map = {entry["file_id"]: entry for entry in files}
    for file_id in retained_ids:
        entry = file_map[file_id]
        if entry.get("role") != "original" or entry.get("file_status", "retained") != "retained":
            raise ManifestError("retained_file_ids may contain only retained original files")
    if any(file_id not in set(retained_ids) for file_id in default_ids):
        raise ManifestError("default_file_ids must be a subset of retained_file_ids")

    if (
        raw["recording_class"] == "normal"
        and raw["default_scope_status"] == "complete"
        and retained_ids != default_ids
    ):
        raise ManifestError("normal recording default_file_ids must equal retained_file_ids")

    normalized = dict(raw)
    normalized["files"] = files
    normalized["retained_file_ids"] = retained_ids
    normalized["default_file_ids"] = default_ids
    normalized["target_duration_ms"] = target_ms
    normalized["source_action_instance_id"] = source_action_id
    return normalized


def _unique_file_ids(raw: object, available: set[str], field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ManifestError(f"{field} must be an array")
    result: list[str] = []
    for position, value in enumerate(raw):
        file_id = _require_text(value, f"{field}[{position}]")
        if file_id not in available:
            raise ManifestError(f"{field} references unknown file_id: {file_id}")
        if file_id in result:
            raise ManifestError(f"{field} contains duplicate file_id: {file_id}")
        result.append(file_id)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_copy_path(manifest_path: Path, raw_path: str) -> Path:
    copy_path = Path(raw_path).expanduser()
    if not copy_path.is_absolute():
        copy_path = manifest_path.parent / copy_path
    return copy_path.resolve()


def _run_command(argv: Sequence[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return {
            "command": list(argv),
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "execution_error": str(exc),
        }
    return {
        "command": list(argv),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "execution_error": None,
    }


def _tool_version(executable: str) -> dict[str, Any]:
    result = _run_command([executable, "-version"])
    lines = result["stdout"].splitlines()
    return {
        "executable": shutil.which(executable),
        "version": lines[0] if lines else None,
        "exit_code": result["exit_code"],
        "stderr": result["stderr"],
        "execution_error": result["execution_error"],
    }


def _stream_duration_ms(stream: dict[str, Any]) -> tuple[Fraction | None, str | None]:
    duration_ts = stream.get("duration_ts")
    if type(duration_ts) is int and duration_ts >= 0:
        duration_ticks = duration_ts
    elif isinstance(duration_ts, str) and duration_ts.isdecimal():
        duration_ticks = int(duration_ts)
    else:
        duration_ticks = None
    time_base = _positive_fraction(stream.get("time_base"))
    if duration_ticks is not None and time_base is not None:
        return Fraction(duration_ticks) * time_base * 1000, "duration_ts_times_time_base"
    duration = _decimal_fraction(stream.get("duration"))
    if duration is not None:
        return duration * 1000, "ffprobe_stream_duration"
    return None, None


def _stream_duration_fields(stream: dict[str, Any]) -> dict[str, Any]:
    selected, selected_basis = _stream_duration_ms(stream)
    raw_duration = _decimal_fraction(stream.get("duration"))
    raw_duration_ms = raw_duration * 1000 if raw_duration is not None else None
    ticks_ms: Fraction | None = None
    ticks = stream.get("duration_ts")
    if type(ticks) is int and ticks >= 0:
        ticks_count = ticks
    elif isinstance(ticks, str) and ticks.isdecimal():
        ticks_count = int(ticks)
    else:
        ticks_count = None
    time_base = _positive_fraction(stream.get("time_base"))
    if ticks_count is not None and time_base is not None:
        ticks_ms = Fraction(ticks_count) * time_base * 1000
    return {
        "duration_ms_exact": _fraction_text(selected),
        "duration_basis": selected_basis,
        "duration_raw_field_ms_exact": _fraction_text(raw_duration_ms),
        "duration_ticks_ms_exact": _fraction_text(ticks_ms),
        "duration_ticks_minus_raw_field_ms_exact": (
            _fraction_text(ticks_ms - raw_duration_ms)
            if ticks_ms is not None and raw_duration_ms is not None
            else None
        ),
    }


def _format_duration_ms(format_info: dict[str, Any]) -> Fraction | None:
    duration = _decimal_fraction(format_info.get("duration"))
    return duration * 1000 if duration is not None else None


def _parse_index(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _frame_coverage(
    path: Path, ffprobe: str, main_video_indexes: set[int]
) -> dict[int, dict[str, Any]]:
    coverage: dict[int, dict[str, Any]] = {
        stream_index: {
            "decoded_frame_timestamp_count": 0,
            "missing_timestamp_count": 0,
            "first_timestamp_seconds_exact": None,
            "last_timestamp_seconds_exact": None,
            "timestamps_monotonic": True,
            "max_interframe_gap_ms_exact": None,
            "probe_exit_code": None,
            "probe_stderr": "",
        }
        for stream_index in main_video_indexes
    }
    argv = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_frames",
        "-show_entries",
        "frame=stream_index,best_effort_timestamp_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = _run_command(argv)
    previous: dict[int, Fraction] = {}
    max_gaps: dict[int, Fraction] = {}
    try:
        for row in csv.reader(result["stdout"].splitlines()):
            if len(row) < 2:
                continue
            stream_index = _parse_index(row[0])
            if stream_index not in coverage:
                continue
            timestamp = _decimal_fraction(row[1])
            record = coverage[stream_index]
            if timestamp is None:
                record["missing_timestamp_count"] += 1
                continue
            record["decoded_frame_timestamp_count"] += 1
            if record["first_timestamp_seconds_exact"] is None:
                record["first_timestamp_seconds_exact"] = _fraction_text(timestamp)
            record["last_timestamp_seconds_exact"] = _fraction_text(timestamp)
            if stream_index in previous:
                gap = timestamp - previous[stream_index]
                if gap < 0:
                    record["timestamps_monotonic"] = False
                else:
                    max_gaps[stream_index] = max(max_gaps.get(stream_index, Fraction(0)), gap)
            previous[stream_index] = timestamp
    except csv.Error as exc:
        for record in coverage.values():
            record["parse_error"] = str(exc)

    for stream_index, record in coverage.items():
        record["probe_exit_code"] = result["exit_code"]
        record["probe_stderr"] = result["stderr"]
        max_gap = max_gaps.get(stream_index)
        record["max_interframe_gap_ms_exact"] = (
            _fraction_text(max_gap * 1000) if max_gap is not None else None
        )
    return coverage


def _decode_stream(path: Path, ffmpeg: str, stream_index: int) -> dict[str, Any]:
    argv = [
        ffmpeg,
        "-nostdin",
        "-v",
        "warning",
        "-nostats",
        "-i",
        str(path),
        "-map",
        f"0:{stream_index}",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    result = _run_command(argv)
    return {
        "command": result["command"],
        "exit_code": result["exit_code"],
        "status": "decoded" if result["exit_code"] == 0 else "decode_failed",
        "diagnostics_present": bool(result["stderr"]),
        "diagnostics": result["stderr"],
        "execution_error": result["execution_error"],
    }


def _inspect_file(
    entry: dict[str, Any], manifest_path: Path, ffprobe: str, ffmpeg: str
) -> dict[str, Any]:
    local_path = _resolve_copy_path(manifest_path, entry["local_copy_path"])
    result: dict[str, Any] = {
        "file_id": entry["file_id"],
        "output_id": entry.get("output_id"),
        "role": entry["role"],
        "file_status": entry.get("file_status", "retained"),
        "preview_of_output_id": entry.get("preview_of_output_id"),
        "source": {
            "device_path": entry.get("source_device_path"),
            "size_bytes": entry.get("source_size_bytes"),
            "sha256": entry.get("source_sha256"),
            "evidence_ref": entry.get("source_evidence_ref"),
        },
        "local_copy": {
            "path": str(local_path),
            "exists": local_path.is_file(),
            "size_bytes": None,
            "sha256": None,
            "identity_status": "unknown",
            "identity_unknown_reasons": [],
        },
        "media": {
            "status": "not_assessed",
            "probe_command": None,
            "probe_exit_code": None,
            "probe_stderr": "",
            "format": {},
            "main_videos": [],
            "audio_streams": [],
            "attached_picture_streams": [],
            "other_streams": [],
            "decode_checks": [],
        },
    }

    local_copy = result["local_copy"]
    if not local_copy["exists"]:
        local_copy["identity_unknown_reasons"].append("local_copy_missing")
    else:
        try:
            local_copy["size_bytes"] = local_path.stat().st_size
            local_copy["sha256"] = _file_sha256(local_path)
        except OSError as exc:
            local_copy["identity_unknown_reasons"].append(f"local_copy_read_error:{exc}")

    source_size = entry.get("source_size_bytes")
    source_sha = entry.get("source_sha256")
    source_basis = entry.get("source_evidence_ref")
    if not _has_text(source_basis):
        local_copy["identity_unknown_reasons"].append("missing_source_identity_basis")
    if source_size is None:
        local_copy["identity_unknown_reasons"].append("source_size_unavailable")
    elif local_copy["size_bytes"] is not None and source_size != local_copy["size_bytes"]:
        local_copy["identity_unknown_reasons"].append("source_copy_size_mismatch")
    if source_sha is None:
        local_copy["identity_unknown_reasons"].append("source_sha256_unavailable")
    elif local_copy["sha256"] is not None and source_sha.lower() != local_copy["sha256"]:
        local_copy["identity_unknown_reasons"].append("source_copy_sha256_mismatch")

    identity_mismatches = {"source_copy_size_mismatch", "source_copy_sha256_mismatch"}
    if any(reason in identity_mismatches for reason in local_copy["identity_unknown_reasons"]):
        local_copy["identity_status"] = "mismatch"
    elif not local_copy["identity_unknown_reasons"]:
        local_copy["identity_status"] = "match"

    if entry["role"] == "preview":
        result["media"]["status"] = "excluded_preview_not_used_for_main_video_duration"
        return result
    if not local_copy["exists"] or local_copy["size_bytes"] is None:
        result["media"]["status"] = "unavailable_local_copy"
        return result
    if local_copy["size_bytes"] == 0:
        result["media"]["status"] = "zero_byte_file_no_media_duration"
        return result

    probe_argv = [
        ffprobe,
        "-v",
        "warning",
        "-count_frames",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(local_path),
    ]
    probe = _run_command(probe_argv)
    media = result["media"]
    media["probe_command"] = probe["command"]
    media["probe_exit_code"] = probe["exit_code"]
    media["probe_stderr"] = probe["stderr"]
    media["probe_execution_error"] = probe["execution_error"]
    try:
        probe_document = json.loads(probe["stdout"])
    except json.JSONDecodeError as exc:
        media["status"] = "probe_failed"
        media["probe_parse_error"] = str(exc)
        return result

    streams = probe_document.get("streams", [])
    format_info = probe_document.get("format", {})
    if not isinstance(streams, list) or not isinstance(format_info, dict):
        media["status"] = "probe_output_invalid"
        return result

    container_duration = _format_duration_ms(format_info)
    format_size = _parse_index(format_info.get("size"))
    media["format"] = {
        "format_name": format_info.get("format_name"),
        "duration_raw_seconds": format_info.get("duration"),
        "duration_ms_exact": _fraction_text(container_duration),
        "size_bytes_reported_by_ffprobe": _nonnegative_int(format_size),
        "raw": format_info,
    }

    main_entries: list[tuple[int, dict[str, Any]]] = []
    audio_entries: list[tuple[int, dict[str, Any]]] = []
    attached_entries: list[tuple[int, dict[str, Any]]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_index = _parse_index(stream.get("index"))
        if stream_index is None:
            continue
        codec_type = stream.get("codec_type")
        disposition = stream.get("disposition")
        is_attached_picture = (
            codec_type == "video"
            and isinstance(disposition, dict)
            and _parse_index(disposition.get("attached_pic")) == 1
        )
        if codec_type == "video" and is_attached_picture:
            attached_entries.append((stream_index, stream))
        elif codec_type == "video":
            main_entries.append((stream_index, stream))
        elif codec_type == "audio":
            audio_entries.append((stream_index, stream))
        else:
            media["other_streams"].append({"index": stream_index, "raw": stream})

    main_entries.sort(key=lambda pair: pair[0])
    audio_entries.sort(key=lambda pair: pair[0])
    attached_entries.sort(key=lambda pair: pair[0])
    main_indexes = {stream_index for stream_index, _ in main_entries}
    coverage = _frame_coverage(local_path, ffprobe, main_indexes) if main_indexes else {}

    for ordinal, (stream_index, stream) in enumerate(main_entries):
        duration_fields = _stream_duration_fields(stream)
        duration_ms = _stream_duration_ms(stream)[0]
        decode = _decode_stream(local_path, ffmpeg, stream_index)
        video_result = {
            "main_video_ordinal": ordinal,
            "stream_index": stream_index,
            "codec_name": stream.get("codec_name"),
            "duration_raw_seconds": stream.get("duration"),
            "duration_ts": stream.get("duration_ts"),
            "time_base": stream.get("time_base"),
            **duration_fields,
            "r_frame_rate": stream.get("r_frame_rate"),
            "avg_frame_rate": stream.get("avg_frame_rate"),
            "declared_frame_count": _parse_index(stream.get("nb_frames")),
            "ffprobe_counted_frame_count": _parse_index(stream.get("nb_read_frames")),
            "frame_coverage": coverage.get(stream_index, {}),
            "decode": decode,
            "raw_stream_metadata": stream,
        }
        if duration_ms is not None and container_duration is not None:
            video_result["container_minus_video_duration_ms_exact"] = _fraction_text(
                container_duration - duration_ms
            )
        else:
            video_result["container_minus_video_duration_ms_exact"] = None
        media["main_videos"].append(video_result)
        media["decode_checks"].append(
            {"stream_index": stream_index, "stream_type": "video", **decode}
        )

    for stream_index, stream in attached_entries:
        media["attached_picture_streams"].append(
            {"stream_index": stream_index, "codec_name": stream.get("codec_name"), "raw": stream}
        )
        media["decode_checks"].append(
            {
                "stream_index": stream_index,
                "stream_type": "video",
                "status": "skipped",
                "diagnostics_present": False,
                "diagnostics": "",
                "skip_reason": "attached_picture_excluded_from_main_video_duration",
            }
        )

    for stream_index, stream in audio_entries:
        duration_fields = _stream_duration_fields(stream)
        decode = _decode_stream(local_path, ffmpeg, stream_index)
        media["audio_streams"].append(
            {
                "stream_index": stream_index,
                "codec_name": stream.get("codec_name"),
                "duration_raw_seconds": stream.get("duration"),
                "duration_ts": stream.get("duration_ts"),
                "time_base": stream.get("time_base"),
                **duration_fields,
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "decode": decode,
                "raw_stream_metadata": stream,
            }
        )
        media["decode_checks"].append(
            {"stream_index": stream_index, "stream_type": "audio", **decode}
        )

    if probe["exit_code"] == 0 and main_entries:
        media["status"] = "probed"
    elif probe["exit_code"] == 0:
        media["status"] = "no_main_video_stream"
    else:
        media["status"] = "probe_nonzero_exit_with_observations"
    return result


def _file_duration_ms(file_result: dict[str, Any], ordinal: int) -> Fraction | None:
    videos = file_result["media"].get("main_videos", [])
    if ordinal >= len(videos):
        return None
    raw_value = videos[ordinal].get("duration_ms_exact")
    if not isinstance(raw_value, str):
        return None
    try:
        if "/" in raw_value:
            numerator, denominator = raw_value.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(int(raw_value))
    except (ValueError, ZeroDivisionError):
        return None


def _scope_result(
    scope_name: str,
    file_ids: list[str],
    declared_status: str,
    file_results_by_id: dict[str, dict[str, Any]],
    expected: dict[str, Any],
    tolerance_ms: int,
) -> dict[str, Any]:
    expected_ms: Fraction | None = None
    expected_exact = expected.get("expected_duration_ms_exact")
    if isinstance(expected_exact, str):
        if "/" in expected_exact:
            numerator, denominator = expected_exact.split("/", 1)
            expected_ms = Fraction(int(numerator), int(denominator))
        else:
            expected_ms = Fraction(int(expected_exact))

    stream_results: list[dict[str, Any]] = []
    statuses: list[str] = []
    for ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
        total = Fraction(0)
        contributing_count = 0
        reasons: list[str] = []
        for file_id in file_ids:
            file_result = file_results_by_id[file_id]
            media = file_result["media"]
            videos = media.get("main_videos", [])
            if media.get("probe_exit_code") != 0:
                reasons.append(f"{file_id}:media_probe_not_successfully_completed")
            if len(videos) > EXPECTED_MAIN_VIDEO_STREAMS:
                reasons.append(f"{file_id}:unexpected_main_video_stream_count")
            duration = _file_duration_ms(file_result, ordinal)
            if duration is None:
                reasons.append(f"{file_id}:main_video_{ordinal}_duration_unavailable")
            else:
                total += duration
                contributing_count += 1
        if declared_status == "unknown":
            reasons.append(f"{scope_name}_scope_not_confirmed_complete")
        measured = total if not reasons else None
        comparison = compare_duration(
            expected_ms,
            measured,
            tolerance_ms,
            unknown_reason=";".join(_unique(reasons)) or None,
        )
        comparison["main_video_ordinal"] = ordinal
        comparison["observed_partial_duration_ms_exact"] = _fraction_text(total)
        comparison["contributing_file_count"] = contributing_count
        comparison["required_file_count"] = len(file_ids)
        stream_results.append(comparison)
        statuses.append(comparison["status"])

    local_status = _combine_statuses(statuses)
    identity_statuses = [
        file_results_by_id[file_id]["local_copy"]["identity_status"] for file_id in file_ids
    ]
    if not file_ids:
        identity_status = "empty_scope"
    elif any(status == "mismatch" for status in identity_statuses):
        identity_status = "mismatch"
    elif all(status == "match" for status in identity_statuses):
        identity_status = "verified"
    else:
        identity_status = "unknown"
    source_duration_status = (
        local_status if identity_status in {"verified", "empty_scope"} else "unknown"
    )

    return {
        "scope_id": scope_name,
        "declared_scope_status": declared_status,
        "file_ids": file_ids,
        "local_copy_duration_comparison": {
            "status": local_status,
            "expected_basis": expected,
            "streams": stream_results,
            "cross_segment_continuity": "not_assessed",
            "continuity_unknown_reason": "summed_stream_duration_does_not_prove_no_gap_or_overlap",
        },
        "source_copy_identity_status": identity_status,
        "source_duration_status": source_duration_status,
    }


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    return validate_manifest(raw)


def _output_path_conflict(
    output_path: Path, manifest_path: Path, manifest: dict[str, Any]
) -> Path | None:
    """拒绝让报告覆盖清单或任何被测本地副本。"""

    if output_path == manifest_path:
        return manifest_path
    for entry in manifest["files"]:
        copy_path = _resolve_copy_path(manifest_path, entry["local_copy_path"])
        if output_path == copy_path:
            return copy_path
    return None


def build_report(
    manifest: dict[str, Any],
    manifest_path: Path,
    tolerance_ms: int = DEFAULT_SHORTFALL_TOLERANCE_MS,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> dict[str, Any]:
    tolerance = parse_shortfall_tolerance_ms(tolerance_ms)
    expected = derive_expected_playback_duration(manifest)
    ffprobe_version = _tool_version(ffprobe)
    ffmpeg_version = _tool_version(ffmpeg)
    tool_errors = [
        {"tool": name, "error": tool["execution_error"] or tool["stderr"]}
        for name, tool in (("ffprobe", ffprobe_version), ("ffmpeg", ffmpeg_version))
        if tool["exit_code"] != 0
    ]
    file_results = [
        _inspect_file(entry, manifest_path, ffprobe, ffmpeg) for entry in manifest["files"]
    ]
    by_id = {entry["file_id"]: entry for entry in file_results}
    scopes = {
        "all_retained": _scope_result(
            "all_retained",
            manifest["retained_file_ids"],
            manifest["retained_scope_status"],
            by_id,
            expected,
            tolerance,
        ),
        "default_delivery": _scope_result(
            "default_delivery",
            manifest["default_file_ids"],
            manifest["default_scope_status"],
            by_id,
            expected,
            tolerance,
        ),
    }
    findings = _collect_findings(file_results, scopes, expected)
    if manifest.get("source_action_instance_id") is None:
        findings.insert(
            0,
            {
                "category": "source_action_identity",
                "status": "unknown",
                "reason": "source_action_instance_id_not_available_in_manifest",
            },
        )
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": _utc_now(),
        "execution_status": "completed_with_tool_errors" if tool_errors else "completed",
        "acceptance_id": manifest["acceptance_id"],
        "source_action_instance_id": manifest["source_action_instance_id"],
        "source_action_instance_id_status": (
            "known" if manifest["source_action_instance_id"] is not None else "unknown"
        ),
        "recording_kind": manifest["recording_kind"],
        "recording_class": manifest["recording_class"],
        "task_target_duration_ms": manifest["target_duration_ms"],
        "shortfall_tolerance_ms": tolerance,
        "expected_playback": expected,
        "tools": {"ffprobe": ffprobe_version, "ffmpeg": ffmpeg_version},
        "tool_errors": tool_errors,
        "files": file_results,
        "scopes": scopes,
        "findings": findings,
        "exit_code_meaning": "report_generation_status_only; media_findings_remain_authoritative",
    }


def _collect_findings(
    file_results: list[dict[str, Any]],
    scopes: dict[str, dict[str, Any]],
    expected: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for file_result in file_results:
        media = file_result["media"]
        if file_result["local_copy"]["identity_status"] != "match":
            findings.append(
                {
                    "file_id": file_result["file_id"],
                    "category": "source_copy_identity",
                    "status": file_result["local_copy"]["identity_status"],
                    "reasons": file_result["local_copy"]["identity_unknown_reasons"],
                }
            )
        if media.get("probe_stderr"):
            findings.append(
                {
                    "file_id": file_result["file_id"],
                    "category": "probe_diagnostic",
                    "exit_code": media.get("probe_exit_code"),
                    "diagnostics": media["probe_stderr"],
                }
            )
        for check in media["decode_checks"]:
            if check.get("status") == "decode_failed" or check.get("diagnostics_present"):
                findings.append(
                    {
                        "file_id": file_result["file_id"],
                        "category": "decode_diagnostic",
                        "stream_index": check.get("stream_index"),
                        "stream_type": check.get("stream_type"),
                        "exit_code": check.get("exit_code"),
                        "diagnostics": check.get("diagnostics", ""),
                    }
                )
        main_video_count = len(media.get("main_videos", []))
        if main_video_count != EXPECTED_MAIN_VIDEO_STREAMS:
            findings.append(
                {
                    "file_id": file_result["file_id"],
                    "category": "main_video_stream_count",
                    "status": "unknown",
                    "observed": main_video_count,
                    "required": EXPECTED_MAIN_VIDEO_STREAMS,
                }
            )

    if expected["status"] == "unknown":
        findings.append(
            {
                "category": "expected_playback_duration",
                "status": "unknown",
                "reasons": expected["unknown_reasons"],
            }
        )
    for scope in scopes.values():
        comparison = scope["local_copy_duration_comparison"]
        if comparison["status"] != "pass":
            findings.append(
                {
                    "category": "scope_duration",
                    "scope_id": scope["scope_id"],
                    "status": comparison["status"],
                    "streams": comparison["streams"],
                }
            )
    return findings


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对已取回的 OSMO 360 II 本地分段副本进行独立媒体验收。"
    )
    parser.add_argument("--manifest", required=True, type=Path, help="本地验收清单 JSON")
    parser.add_argument(
        "--shortfall-tolerance-ms",
        type=int,
        default=DEFAULT_SHORTFALL_TOLERANCE_MS,
        help="整体时长短缺容限（非负整数毫秒，默认 100，允许 0）",
    )
    parser.add_argument("--output", required=True, type=Path, help="验收证据 JSON 输出路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tolerance = parse_shortfall_tolerance_ms(args.shortfall_tolerance_ms)
    except (TypeError, ValueError) as exc:
        _parser().error(str(exc))

    manifest_path = args.manifest.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if output_path == manifest_path:
        print("验收报告输出路径不能覆盖输入清单", file=sys.stderr)
        return 2
    try:
        manifest = _read_manifest(manifest_path)
    except ManifestError as exc:
        print(f"验收清单无效：{exc}", file=sys.stderr)
        return 2

    conflict = _output_path_conflict(output_path, manifest_path, manifest)
    if conflict is not None:
        print(f"验收报告输出路径不能覆盖输入文件：{conflict}", file=sys.stderr)
        return 2

    report = build_report(manifest, manifest_path, tolerance)
    try:
        _write_json(output_path, report)
    except OSError as exc:
        print(f"无法写入验收报告：{exc}", file=sys.stderr)
        return 2
    print(f"已写入验收证据：{output_path}")
    print(
        "媒体结论见报告中的 scopes、findings 与逐流 decode 诊断；"
        "退出码 0 仅表示报告已生成。"
    )
    return 2 if report["tool_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
