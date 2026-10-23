#!/usr/bin/env python3
"""验收本地 OSMO 360 II 分段副本，不与相机或 camctl 生产流程交互。"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence


REPORT_SCHEMA_VERSION = 2
MANIFEST_VERSION = 1
DEFAULT_SHORTFALL_TOLERANCE_MS = 100
DEFAULT_FRAME_COUNT_SHORTFALL_TOLERANCE = 3
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


def parse_frame_count_shortfall_tolerance(value: object) -> int:
    """只接受非负整数帧，明确拒绝布尔值和带小数的数值。"""

    if type(value) is not int:
        raise TypeError("frame-count shortfall tolerance must be an integer number of frames")
    if value < 0:
        raise ValueError("frame-count shortfall tolerance must be non-negative")
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


def derive_expected_sampling_quantity(
    task: dict[str, Any], tolerance_frames: int
) -> dict[str, Any]:
    """只用原任务的目标与间隔推导原生延时数量基准。"""

    tolerance = parse_frame_count_shortfall_tolerance(tolerance_frames)
    if task.get("recording_kind") != "native_timelapse":
        return {
            "status": "not_applicable",
            "reference_frame_count_range": None,
            "baseline_frame_count": None,
            "tolerance_frames": tolerance,
            "minimum_accepted_frame_count": None,
            "derivation": None,
            "basis": {},
            "unknown_reasons": [],
        }

    target_ms = _nonnegative_int(task.get("target_duration_ms"))
    interval_ms = _nonnegative_int(task.get("sampling_interval_ms"))
    reasons: list[str] = []
    if target_ms is None or target_ms == 0:
        reasons.append("invalid_or_missing_task_target_duration")
    if not _has_text(task.get("target_basis_ref")):
        reasons.append("missing_independent_target_basis")
    if interval_ms is None or interval_ms == 0:
        reasons.append("invalid_or_missing_sampling_interval")
    if not _has_text(task.get("sampling_interval_basis_ref")):
        reasons.append("missing_sampling_interval_basis")

    if reasons:
        return {
            "status": "unknown",
            "reference_frame_count_range": None,
            "baseline_frame_count": None,
            "tolerance_frames": tolerance,
            "minimum_accepted_frame_count": None,
            "derivation": None,
            "basis": {},
            "unknown_reasons": _unique(reasons),
        }

    assert target_ms is not None and interval_ms is not None
    baseline = (target_ms + interval_ms - 1) // interval_ms
    reference_maximum = target_ms // interval_ms + 1
    return {
        "status": "known",
        "reference_frame_count_range": {
            "minimum": baseline,
            "maximum": reference_maximum,
        },
        "baseline_frame_count": baseline,
        "tolerance_frames": tolerance,
        "minimum_accepted_frame_count": max(1, baseline - tolerance),
        "derivation": "ceil_target_duration_ms_divided_by_sampling_interval_ms",
        "basis": {
            "target_duration_ms": target_ms,
            "target_basis_ref": task["target_basis_ref"],
            "sampling_interval_ms": interval_ms,
            "sampling_interval_basis_ref": task["sampling_interval_basis_ref"],
        },
        "unknown_reasons": [],
    }


def compare_sample_count(
    expected: dict[str, Any], observed_frame_count: int | None
) -> dict[str, Any]:
    """比较一条主视频在一个完整范围内的累计可靠采样帧数。"""

    if expected.get("status") == "not_applicable":
        return {
            "status": "not_applicable",
            "observed_frame_count": observed_frame_count,
            "shortfall_frames": None,
            "excess_over_reference_maximum": None,
            "unknown_reasons": [],
        }
    minimum = expected.get("minimum_accepted_frame_count")
    if (
        expected.get("status") != "known"
        or type(minimum) is not int
        or observed_frame_count is None
        or type(observed_frame_count) is not int
        or observed_frame_count < 0
    ):
        reasons = list(expected.get("unknown_reasons", []))
        if expected.get("status") == "known":
            reasons.append("reliable_sample_frame_count_unavailable")
        elif not reasons:
            reasons.append("expected_sampling_quantity_unavailable")
        return {
            "status": "unknown",
            "observed_frame_count": observed_frame_count,
            "shortfall_frames": None,
            "excess_over_reference_maximum": None,
            "unknown_reasons": _unique(reasons),
        }

    reference_range = expected["reference_frame_count_range"]
    reference_maximum = reference_range["maximum"]
    shortfall = max(0, minimum - observed_frame_count)
    return {
        "status": "pass" if shortfall == 0 else "fail",
        "observed_frame_count": observed_frame_count,
        "shortfall_frames": shortfall,
        "excess_over_reference_maximum": max(
            0, observed_frame_count - reference_maximum
        ),
        "unknown_reasons": [],
    }


def assess_reliable_frame_count(video: dict[str, Any]) -> dict[str, Any]:
    """以完整 ffprobe counted-frame 结果为可靠数量，保留其他观测差异。"""

    counted = _nonnegative_int(video.get("ffprobe_counted_frame_count"))
    declared = _nonnegative_int(video.get("declared_frame_count"))
    coverage = video.get("frame_coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    timestamp_count = _nonnegative_int(
        coverage.get("decoded_frame_timestamp_count")
    )
    probe_succeeded = video.get("probe_exit_code") == 0
    differences = []
    for field, observed in (
        ("declared_frame_count", declared),
        ("decoded_frame_timestamp_count", timestamp_count),
    ):
        if (
            probe_succeeded
            and counted is not None
            and observed is not None
            and observed != counted
        ):
            differences.append(
                {"field": field, "observed": observed, "authoritative": counted}
            )

    reasons: list[str] = []
    if not probe_succeeded:
        reasons.append("media_probe_not_successfully_completed")
    elif counted is None:
        reasons.append("ffprobe_counted_frame_count_unavailable")
    known = not reasons
    return {
        "status": "known" if known else "unknown",
        "reliable_frame_count": counted if known else None,
        "reliable_count_basis": "ffprobe_counted_frame_count" if known else None,
        "observed_counts": {
            "declared_frame_count": declared,
            "ffprobe_counted_frame_count": counted,
            "decoded_frame_timestamp_count": timestamp_count,
        },
        "observation_differences": differences,
        "unknown_reasons": reasons,
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
        for field in ("source_device_path", "source_locator_basis_ref"):
            value = entry.get(field)
            if value is not None:
                _require_text(value, f"{prefix}.{field}")
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

    if raw["retained_scope_status"] == "complete":
        all_retained_ids = [
            entry["file_id"]
            for entry in files
            if entry["role"] == "original"
            and entry.get("file_status", "retained") == "retained"
        ]
        if set(retained_ids) != set(all_retained_ids):
            raise ManifestError(
                "retained_file_ids must cover every retained original when "
                "retained_scope_status is complete"
            )

    if any(file_id not in set(retained_ids) for file_id in default_ids):
        raise ManifestError("default_file_ids must be a subset of retained_file_ids")

    for scope_name, scope_ids in (
        ("retained", retained_ids),
        ("default", default_ids),
    ):
        seen_source_locators: dict[str, str] = {}
        for file_id in scope_ids:
            source_locator = _source_locator_key(file_map[file_id])
            if source_locator is None:
                continue
            previous_file_id = seen_source_locators.get(source_locator)
            if previous_file_id is not None:
                raise ManifestError(
                    f"duplicate reliable source locator in {scope_name} scope: "
                    f"{previous_file_id} and {file_id} ({source_locator})"
                )
            seen_source_locators[source_locator] = file_id

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


def _assess_source_locator(entry: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Return a reliable normalized source file path and its unknown reasons."""

    device_path = entry.get("source_device_path")
    locator_basis = entry.get("source_locator_basis_ref")
    reasons: list[str] = []
    normalized: str | None = None
    if not _has_text(device_path):
        reasons.append("source_device_path_unavailable")
    else:
        raw_path = device_path.strip()
        normalized = posixpath.normpath(raw_path)
        final_component = raw_path.rsplit("/", 1)[-1]
        if (
            final_component in {"", ".", ".."}
            or posixpath.basename(normalized) in {"", ".", ".."}
        ):
            reasons.append("source_device_path_not_file_locator")
            normalized = None
    if not _has_text(locator_basis):
        reasons.append("missing_source_locator_basis")
    if reasons:
        return None, _unique(reasons)
    return normalized, []


def _source_locator_key(entry: dict[str, Any]) -> str | None:
    """Return the normalized source path only when locator qualification succeeds."""

    return _assess_source_locator(entry)[0]


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


STREAM_MARKER = "$osmo_p7_stream"


class DiagnosticArtifacts:
    """把子进程 stderr 暂存到验收专用临时目录。"""

    def __init__(self, directory: Path):
        self.directory = directory
        self.session_id = uuid.uuid4().hex

    def create(self) -> tuple[str, Path]:
        token = f"{self.session_id}-{uuid.uuid4().hex}"
        return token, self.directory / f"{token}.stderr"

    def marker(self, token: str) -> dict[str, Any]:
        return {STREAM_MARKER: {"kind": "diagnostic", "token": token}}


def _stream_marker(kind: str, key: object = None) -> dict[str, Any]:
    return {STREAM_MARKER: {"kind": kind, "key": key}}


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


class DiagnosticCaptureError(OSError):
    """Raised when subprocess output cannot be saved as complete evidence."""

    def __init__(
        self, stream: str, errors: Sequence[str], exit_code: int | None
    ) -> None:
        self.stream = stream
        self.capture_errors = tuple(errors)
        self.exit_code = exit_code
        details = "; ".join(self.capture_errors)
        super().__init__(f"cannot reliably capture {stream}: {details}")


def _new_capture_state() -> dict[str, Any]:
    return {"size": 0, "errors": [], "lock": threading.Lock()}


def _record_capture_error(
    state: dict[str, Any], operation: str, error: BaseException
) -> None:
    with state["lock"]:
        state["errors"].append(f"{operation}: {error}")


def _capture_errors(state: dict[str, Any]) -> tuple[str, ...]:
    with state["lock"]:
        return tuple(state["errors"])


def _kill_process(process: Any | None, state: dict[str, Any], operation: str) -> None:
    if process is None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        _record_capture_error(state, operation, exc)


def _close_pipe(pipe: Any, state: dict[str, Any], operation: str) -> None:
    try:
        pipe.close()
    except Exception as exc:
        _record_capture_error(state, operation, exc)
        try:
            pipe.close()
        except Exception as retry_exc:
            _record_capture_error(state, f"retry {operation}", retry_exc)


def _close_diagnostic_output(
    output: Any, state: dict[str, Any], *, flush: bool
) -> None:
    if flush:
        try:
            output.flush()
        except Exception as exc:
            _record_capture_error(state, "flush diagnostic artifact", exc)
    try:
        output.close()
    except Exception as exc:
        _record_capture_error(state, "close diagnostic artifact", exc)
        try:
            output.close()
        except Exception as retry_exc:
            _record_capture_error(state, "retry close diagnostic artifact", retry_exc)


def _drain_stderr(
    pipe: Any,
    path: Path,
    state: dict[str, Any],
    process: Any | None = None,
) -> None:
    output = None
    try:
        try:
            output = path.open("wb")
        except Exception as exc:
            _record_capture_error(state, "open diagnostic artifact", exc)
        while True:
            try:
                chunk = pipe.read(64 * 1024)
            except Exception as exc:
                _record_capture_error(state, "read subprocess stderr pipe", exc)
                _kill_process(process, state, "terminate subprocess after stderr read failure")
                break
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                error = OSError("stderr pipe returned a non-bytes chunk")
                _record_capture_error(state, "read subprocess stderr pipe", error)
                _kill_process(process, state, "terminate subprocess after invalid stderr chunk")
                break
            state["size"] += len(chunk)
            if output is not None:
                try:
                    written = output.write(chunk)
                    if written != len(chunk):
                        raise OSError(f"short diagnostic write: {written}/{len(chunk)} bytes")
                except Exception as exc:
                    _record_capture_error(state, "write diagnostic artifact", exc)
                    _close_diagnostic_output(output, state, flush=False)
                    output = None
    except Exception as exc:
        _record_capture_error(state, "drain subprocess stderr", exc)
        _kill_process(process, state, "terminate subprocess after stderr drain failure")
    finally:
        if output is not None:
            _close_diagnostic_output(output, state, flush=True)
        _close_pipe(pipe, state, "close subprocess stderr pipe")


def _join_stderr_drainer(
    drainer: threading.Thread, process: Any, state: dict[str, Any]
) -> None:
    try:
        drainer.join()
    except Exception as exc:
        _record_capture_error(state, "join stderr reader thread", exc)
        _kill_process(process, state, "terminate subprocess after stderr reader join failure")
        try:
            drainer.join()
        except Exception as retry_exc:
            _record_capture_error(state, "retry join stderr reader thread", retry_exc)
    if drainer.is_alive():
        _record_capture_error(
            state,
            "join stderr reader thread",
            RuntimeError("reader thread remains alive"),
        )
        _kill_process(process, state, "terminate subprocess after live stderr reader")
        drainer.join()


def _wait_process(process: Any, state: dict[str, Any]) -> int | None:
    try:
        return process.wait()
    except Exception as exc:
        _record_capture_error(state, "wait for subprocess", exc)
        _kill_process(process, state, "terminate subprocess after wait failure")
        try:
            return process.wait()
        except Exception as retry_exc:
            _record_capture_error(state, "retry wait for subprocess", retry_exc)
            return None


def _raise_capture_error(
    stream: str, state: dict[str, Any], exit_code: int | None
) -> None:
    errors = _capture_errors(state)
    if errors:
        raise DiagnosticCaptureError(stream, errors, exit_code)


def _run_command(
    argv: Sequence[str], diagnostics: DiagnosticArtifacts
) -> dict[str, Any]:
    token, stderr_path = diagnostics.create()
    stderr_state = _new_capture_state()
    try:
        process = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        stderr_path.write_text(str(exc), encoding="utf-8")
        return {
            "command": list(argv),
            "exit_code": None,
            "stdout": "",
            "stderr": diagnostics.marker(token),
            "stderr_present": True,
            "execution_error": str(exc),
        }

    assert process.stdout is not None and process.stderr is not None
    drainer = threading.Thread(
        target=_drain_stderr,
        args=(process.stderr, stderr_path, stderr_state, process),
        name="osmo-media-stderr-drain",
        daemon=False,
    )
    try:
        drainer.start()
    except Exception as exc:
        _record_capture_error(stderr_state, "start stderr reader thread", exc)
        _kill_process(process, stderr_state, "terminate subprocess after reader start failure")
        _close_pipe(process.stdout, stderr_state, "close subprocess stdout pipe")
        _close_pipe(process.stderr, stderr_state, "close subprocess stderr pipe")
        exit_code = _wait_process(process, stderr_state)
        _raise_capture_error("subprocess output and stderr", stderr_state, exit_code)
        raise AssertionError("capture errors must have been raised")

    stdout_bytes: bytes | None = None
    try:
        stdout_bytes = process.stdout.read()
        if not isinstance(stdout_bytes, bytes):
            raise OSError("stdout pipe returned a non-bytes value")
    except Exception as exc:
        _record_capture_error(stderr_state, "read subprocess stdout pipe", exc)
        _kill_process(process, stderr_state, "terminate subprocess after stdout read failure")
    finally:
        _close_pipe(process.stdout, stderr_state, "close subprocess stdout pipe")
    exit_code = _wait_process(process, stderr_state)
    _join_stderr_drainer(drainer, process, stderr_state)
    _raise_capture_error("subprocess output and stderr", stderr_state, exit_code)
    assert stdout_bytes is not None
    if stderr_state["size"] == 0:
        stderr_path.unlink(missing_ok=True)
        stderr_value: Any = ""
    else:
        stderr_value = diagnostics.marker(token)
    return {
        "command": list(argv),
        "exit_code": exit_code,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_value,
        "stderr_present": stderr_state["size"] > 0,
        "execution_error": None,
    }


def _tool_version(executable: str, diagnostics: DiagnosticArtifacts) -> dict[str, Any]:
    result = _run_command([executable, "-version"], diagnostics)
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
    path: Path,
    ffprobe: str,
    main_video_indexes: set[int],
    diagnostics: DiagnosticArtifacts,
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
    previous: dict[int, Fraction] = {}
    max_gaps: dict[int, Fraction] = {}
    token, stderr_path = diagnostics.create()
    stderr_state = _new_capture_state()
    parse_error: str | None = None
    exit_code: int | None = None
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        message = str(exc).encode("utf-8")
        try:
            with stderr_path.open("wb") as output:
                if output.write(message) != len(message):
                    raise OSError("short write for ffprobe startup diagnostic")
                output.flush()
        except Exception as artifact_exc:
            raise DiagnosticCaptureError(
                "ffprobe startup stderr", [f"write launch diagnostic: {artifact_exc}"], None
            ) from exc
        stderr_state["size"] = len(message)
    else:
        assert process.stdout is not None and process.stderr is not None
        drainer = threading.Thread(
            target=_drain_stderr,
            args=(process.stderr, stderr_path, stderr_state, process),
            name="osmo-frame-stderr-drain",
            daemon=False,
        )
        try:
            drainer.start()
        except Exception as exc:
            _record_capture_error(stderr_state, "start ffprobe stderr reader thread", exc)
            _kill_process(process, stderr_state, "terminate ffprobe after reader start failure")
            _close_pipe(process.stdout, stderr_state, "close ffprobe frame stdout pipe")
            _close_pipe(process.stderr, stderr_state, "close ffprobe frame stderr pipe")
            exit_code = _wait_process(process, stderr_state)
            _raise_capture_error("ffprobe frame output and stderr", stderr_state, exit_code)
            raise AssertionError("capture errors must have been raised")

        text_stream: io.TextIOWrapper | None = None
        processing_error: Exception | None = None
        try:
            text_stream = io.TextIOWrapper(
                process.stdout, encoding="utf-8", errors="replace", newline=""
            )
            try:
                for row in csv.reader(text_stream):
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
                            max_gaps[stream_index] = max(
                                max_gaps.get(stream_index, Fraction(0)), gap
                            )
                    previous[stream_index] = timestamp
            except csv.Error as exc:
                parse_error = str(exc)
                for record in coverage.values():
                    record["parse_error"] = parse_error
                try:
                    while text_stream.read(64 * 1024):
                        pass
                except Exception as read_exc:
                    _record_capture_error(
                        stderr_state, "read remaining ffprobe frame stdout", read_exc
                    )
                    _kill_process(
                        process, stderr_state, "terminate ffprobe after frame stdout read failure"
                    )
            except Exception as exc:
                processing_error = exc
                _record_capture_error(stderr_state, "read ffprobe frame stdout", exc)
                _kill_process(
                    process, stderr_state, "terminate ffprobe after frame stdout read failure"
                )
        except Exception as exc:
            processing_error = exc
            _record_capture_error(stderr_state, "initialize ffprobe frame stdout reader", exc)
            _kill_process(
                process,
                stderr_state,
                "terminate ffprobe after frame stdout reader initialization failure",
            )
        finally:
            if text_stream is None:
                _close_pipe(
                    process.stdout, stderr_state, "close ffprobe frame stdout pipe"
                )
            else:
                try:
                    text_stream.close()
                except Exception as exc:
                    _record_capture_error(stderr_state, "close ffprobe frame stdout pipe", exc)
                    _close_pipe(
                        process.stdout,
                        stderr_state,
                        "retry close ffprobe frame stdout pipe",
                    )
            exit_code = _wait_process(process, stderr_state)
            _join_stderr_drainer(drainer, process, stderr_state)
        _raise_capture_error("ffprobe frame output and stderr", stderr_state, exit_code)
        if processing_error is not None:
            raise processing_error

    for stream_index, record in coverage.items():
        record["probe_exit_code"] = exit_code
        record["probe_stderr"] = (
            diagnostics.marker(token) if stderr_state["size"] else ""
        )
        max_gap = max_gaps.get(stream_index)
        record["max_interframe_gap_ms_exact"] = (
            _fraction_text(max_gap * 1000) if max_gap is not None else None
        )
    return coverage


def _decode_stream(
    path: Path,
    ffmpeg: str,
    stream_index: int,
    diagnostics: DiagnosticArtifacts,
) -> dict[str, Any]:
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
    result = _run_command(argv, diagnostics)
    return {
        "command": result["command"],
        "exit_code": result["exit_code"],
        "status": "decoded" if result["exit_code"] == 0 else "decode_failed",
        "diagnostics_present": result["stderr_present"],
        "diagnostics": result["stderr"],
        "execution_error": result["execution_error"],
    }


def _inspect_file(
    entry: dict[str, Any],
    manifest_path: Path,
    ffprobe: str,
    ffmpeg: str,
    diagnostics: DiagnosticArtifacts,
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
            "locator_basis_ref": entry.get("source_locator_basis_ref"),
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
            "byte_identity_status": "unknown",
            "source_locator_status": "unknown",
            "identity_unknown_reasons": [],
            "byte_identity_unknown_reasons": [],
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
    byte_identity_reasons: list[str] = []
    if not local_copy["exists"]:
        byte_identity_reasons.append("local_copy_missing")
    else:
        try:
            local_copy["size_bytes"] = local_path.stat().st_size
            local_copy["sha256"] = _file_sha256(local_path)
        except OSError as exc:
            byte_identity_reasons.append(f"local_copy_read_error:{exc}")

    source_size = entry.get("source_size_bytes")
    source_sha = entry.get("source_sha256")
    source_basis = entry.get("source_evidence_ref")
    if not _has_text(source_basis):
        byte_identity_reasons.append("missing_source_identity_basis")
    if source_size is None:
        byte_identity_reasons.append("source_size_unavailable")
    elif local_copy["size_bytes"] is not None and source_size != local_copy["size_bytes"]:
        byte_identity_reasons.append("source_copy_size_mismatch")
    if source_sha is None:
        byte_identity_reasons.append("source_sha256_unavailable")
    elif local_copy["sha256"] is not None and source_sha.lower() != local_copy["sha256"]:
        byte_identity_reasons.append("source_copy_sha256_mismatch")

    identity_mismatches = {"source_copy_size_mismatch", "source_copy_sha256_mismatch"}
    if any(reason in identity_mismatches for reason in byte_identity_reasons):
        local_copy["byte_identity_status"] = "mismatch"
    elif not byte_identity_reasons:
        local_copy["byte_identity_status"] = "match"

    source_locator_key, locator_reasons = _assess_source_locator(entry)
    if source_locator_key is not None:
        local_copy["source_locator_status"] = "known"

    local_copy["byte_identity_unknown_reasons"] = _unique(byte_identity_reasons)
    local_copy["identity_unknown_reasons"] = _unique(
        [*byte_identity_reasons, *locator_reasons]
    )
    if local_copy["byte_identity_status"] == "mismatch":
        local_copy["identity_status"] = "mismatch"
    elif (
        local_copy["byte_identity_status"] == "match"
        and local_copy["source_locator_status"] == "known"
    ):
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
    probe = _run_command(probe_argv, diagnostics)
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
    coverage = (
        _frame_coverage(local_path, ffprobe, main_indexes, diagnostics)
        if main_indexes
        else {}
    )

    for ordinal, (stream_index, stream) in enumerate(main_entries):
        duration_fields = _stream_duration_fields(stream)
        duration_ms = _stream_duration_ms(stream)[0]
        decode = _decode_stream(local_path, ffmpeg, stream_index, diagnostics)
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
            "frame_count_assessment": assess_reliable_frame_count(
                {
                    "probe_exit_code": media["probe_exit_code"],
                    "declared_frame_count": _parse_index(stream.get("nb_frames")),
                    "ffprobe_counted_frame_count": _parse_index(
                        stream.get("nb_read_frames")
                    ),
                    "frame_coverage": coverage.get(stream_index, {}),
                }
            ),
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
        decode = _decode_stream(local_path, ffmpeg, stream_index, diagnostics)
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



def _validate_streamed_header(
    metadata: dict[str, Any], scope_shapes: dict[str, bool]
) -> dict[str, Any]:
    version = metadata.get("manifest_version")
    if type(version) is not int or version != MANIFEST_VERSION:
        raise ManifestError(f"manifest_version must be {MANIFEST_VERSION}")
    _require_text(metadata.get("acceptance_id"), "acceptance_id")
    source_action_id = metadata.get("source_action_instance_id")
    if source_action_id is not None:
        _require_text(source_action_id, "source_action_instance_id")
    recording_kind = metadata.get("recording_kind")
    if not isinstance(recording_kind, str) or recording_kind not in {
        "ordinary_recording",
        "native_timelapse",
    }:
        raise ManifestError("recording_kind must be ordinary_recording or native_timelapse")
    recording_class = metadata.get("recording_class")
    if not isinstance(recording_class, str) or recording_class not in {"normal", "abnormal"}:
        raise ManifestError("recording_class must be normal or abnormal")
    target_ms = _nonnegative_int(metadata.get("target_duration_ms"))
    if target_ms is None or target_ms == 0:
        raise ManifestError("target_duration_ms must be a positive integer")
    for field in ("retained_scope_status", "default_scope_status"):
        value = metadata.get(field)
        if not isinstance(value, str) or value not in {"complete", "unknown"}:
            raise ManifestError(f"{field} must be complete or unknown")
    for field in ("retained_file_ids", "default_file_ids"):
        if not scope_shapes.get(field, False):
            raise ManifestError(f"{field} must be an array")
    metadata["target_duration_ms"] = target_ms
    metadata["source_action_instance_id"] = source_action_id
    return metadata


def _validate_streamed_file(entry: object, ordinal: int) -> dict[str, Any]:
    prefix = f"files[{ordinal}]"
    if not isinstance(entry, dict):
        raise ManifestError(f"{prefix} must be a JSON object")
    file_id = _require_text(entry.get("file_id"), f"{prefix}.file_id")
    role = entry.get("role")
    if not isinstance(role, str) or role not in {"original", "preview"}:
        raise ManifestError(f"{prefix}.role must be original or preview")
    state = entry.get("file_status", "retained")
    if not isinstance(state, str) or state not in {"retained", "observed_empty"}:
        raise ManifestError(f"{prefix}.file_status must be retained or observed_empty")
    _require_text(entry.get("local_copy_path"), f"{prefix}.local_copy_path")
    for field in ("source_device_path", "source_locator_basis_ref"):
        value = entry.get(field)
        if value is not None:
            _require_text(value, f"{prefix}.{field}")
    source_size = entry.get("source_size_bytes")
    if source_size is not None and _nonnegative_int(source_size) is None:
        raise ManifestError(
            f"{prefix}.source_size_bytes must be a non-negative integer or null"
        )
    source_hash = entry.get("source_sha256")
    if source_hash is not None and (
        not isinstance(source_hash, str) or SHA256_PATTERN.fullmatch(source_hash) is None
    ):
        raise ManifestError(
            f"{prefix}.source_sha256 must be 64 hexadecimal characters or null"
        )
    output_id = entry.get("output_id")
    if output_id is not None:
        _require_text(output_id, f"{prefix}.output_id")
    if state == "observed_empty" and output_id is not None:
        raise ManifestError(f"{prefix}.observed_empty must not have output_id")
    if state == "observed_empty" and source_size not in {None, 0}:
        raise ManifestError(f"{prefix}.observed_empty must have source_size_bytes 0 or null")
    if role == "preview" and not _has_text(entry.get("preview_of_output_id")):
        raise ManifestError(f"{prefix}.preview files require preview_of_output_id")
    normalized = dict(entry)
    normalized["file_id"] = file_id
    return normalized


def _json_compatible_value(value: Any) -> Any:
    """Match json.loads numeric values while keeping each manifest item bounded."""

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_json_compatible_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_compatible_value(item) for key, item in value.items()}
    return value


def _initialize_index(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-8192")
    connection.executescript(
        """
        CREATE TABLE files (
            ordinal INTEGER PRIMARY KEY,
            file_id TEXT NOT NULL UNIQUE,
            output_id TEXT UNIQUE,
            role TEXT NOT NULL,
            file_status TEXT NOT NULL,
            source_device_path TEXT,
            source_locator_basis_ref TEXT,
            local_copy_path TEXT NOT NULL,
            entry_json TEXT NOT NULL,
            result_json TEXT
        );
        CREATE TABLE scope_ids (
            scope TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            PRIMARY KEY (scope, ordinal),
            UNIQUE (scope, file_id)
        );
        CREATE TABLE findings (
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
            result_json TEXT NOT NULL
        );
        CREATE TABLE scope_reasons (
            scope TEXT NOT NULL,
            stream_ordinal INTEGER NOT NULL,
            position INTEGER NOT NULL,
            reason TEXT NOT NULL,
            UNIQUE (scope, stream_ordinal, reason)
        );
        """
    )


def _load_manifest_index(
    manifest_path: Path, connection: sqlite3.Connection
) -> dict[str, Any]:
    try:
        import ijson
        from ijson.common import ObjectBuilder
    except ImportError as exc:
        raise ManifestError(
            "缺少流式 JSON 依赖 ijson；请安装 "
            "docs/hardware/requirements-osmo-media-validation.txt"
        ) from exc

    metadata: dict[str, Any] = {}
    scope_shapes = {"retained_file_ids": False, "default_file_ids": False}
    scope_positions = {"retained_file_ids": 0, "default_file_ids": 0}
    root_is_object = False
    files_is_array = False
    file_ordinal = 0
    builder: Any = None
    builder_depth = 0

    try:
        with manifest_path.open("rb") as source:
            for prefix, event, value in ijson.parse(source):
                if builder is not None:
                    builder.event(event, value)
                    if event in {"start_map", "start_array"}:
                        builder_depth += 1
                    elif event in {"end_map", "end_array"}:
                        builder_depth -= 1
                    if builder_depth == 0:
                        entry = _json_compatible_value(
                            _validate_streamed_file(builder.value, file_ordinal)
                        )
                        file_id = entry["file_id"]
                        output_id = entry.get("output_id")
                        try:
                            connection.execute(
                                """
                                INSERT INTO files (
                                    ordinal, file_id, output_id, role, file_status,
                                    source_device_path, source_locator_basis_ref,
                                    local_copy_path, entry_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    file_ordinal,
                                    file_id,
                                    output_id.strip() if isinstance(output_id, str) else None,
                                    entry["role"],
                                    entry.get("file_status", "retained"),
                                    entry.get("source_device_path"),
                                    entry.get("source_locator_basis_ref"),
                                    entry["local_copy_path"],
                                    json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            if connection.execute(
                                "SELECT 1 FROM files WHERE file_id = ?", (file_id,)
                            ).fetchone():
                                raise ManifestError(f"duplicate file_id: {file_id}") from exc
                            if output_id is not None:
                                raise ManifestError(
                                    f"duplicate output_id: {output_id.strip()}"
                                ) from exc
                            raise ManifestError(f"invalid files[{file_ordinal}]") from exc
                        file_ordinal += 1
                        builder = None
                        builder_depth = 0
                    continue

                if prefix == "" and event == "start_map":
                    root_is_object = True
                    continue
                if prefix == "files" and event == "start_array":
                    connection.execute("DELETE FROM files")
                    files_is_array = True
                    file_ordinal = 0
                    continue
                if prefix == "files" and event not in {"end_array"}:
                    # 重复 files 键遵循最后成员；非数组会清除先前索引。
                    if event != "start_array":
                        connection.execute("DELETE FROM files")
                        files_is_array = False
                        file_ordinal = 0
                    continue
                if prefix == "files.item":
                    if event != "start_map":
                        raise ManifestError(f"files[{file_ordinal}] must be a JSON object")
                    builder = ObjectBuilder()
                    builder.event(event, value)
                    builder_depth = 1
                    continue

                if prefix in scope_shapes and event == "start_array":
                    connection.execute(
                        "DELETE FROM scope_ids WHERE scope = ?", (prefix,)
                    )
                    scope_shapes[prefix] = True
                    scope_positions[prefix] = 0
                    metadata.pop(prefix, None)
                    continue
                if prefix in scope_shapes and event not in {"end_array"}:
                    # 非数组成员按最后一个同名 JSON 成员处理。
                    if event != "start_array":
                        connection.execute(
                            "DELETE FROM scope_ids WHERE scope = ?", (prefix,)
                        )
                        scope_shapes[prefix] = False
                    continue
                if prefix.endswith(".item") and prefix.rsplit(".", 1)[0] in scope_shapes:
                    scope_name = prefix.rsplit(".", 1)[0]
                    if event != "string":
                        raise ManifestError(f"{scope_name} entries must be strings")
                    file_id = _require_text(value, f"{scope_name}[{scope_positions[scope_name]}]")
                    try:
                        connection.execute(
                            "INSERT INTO scope_ids (scope, ordinal, file_id) VALUES (?, ?, ?)",
                            (scope_name, scope_positions[scope_name], file_id),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ManifestError(
                            f"{scope_name} contains duplicate file_id: {file_id}"
                        ) from exc
                    scope_positions[scope_name] += 1
                    continue

                if prefix in {
                    "manifest_version",
                    "acceptance_id",
                    "source_action_instance_id",
                    "recording_kind",
                    "recording_class",
                    "target_duration_ms",
                    "target_basis_ref",
                    "retained_scope_status",
                    "default_scope_status",
                    "sampling_interval_ms",
                    "sampling_interval_basis_ref",
                    "playback_frame_rate",
                    "frame_rate_basis_ref",
                    "sample_endpoint_rule",
                    "endpoint_rule_basis_ref",
                }:
                    if event in {"string", "number", "null", "boolean"}:
                        metadata[prefix] = value
                    elif event in {"start_map", "start_array"}:
                        metadata[prefix] = {} if event == "start_map" else []
    except ManifestError:
        raise
    except (OSError, ijson.JSONError, UnicodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc

    if not root_is_object:
        raise ManifestError("manifest root must be a JSON object")
    if not files_is_array:
        raise ManifestError("files must be an array")
    normalized = _validate_streamed_header(metadata, scope_shapes)
    _validate_index_scopes(connection, normalized)
    return normalized


def _validate_index_scopes(
    connection: sqlite3.Connection, manifest: dict[str, Any]
) -> None:
    for scope in ("retained_file_ids", "default_file_ids"):
        unknown = connection.execute(
            """
            SELECT ids.file_id
            FROM scope_ids AS ids
            LEFT JOIN files ON files.file_id = ids.file_id
            WHERE ids.scope = ? AND files.file_id IS NULL
            LIMIT 1
            """,
            (scope,),
        ).fetchone()
        if unknown:
            raise ManifestError(f"{scope} references unknown file_id: {unknown[0]}")

    invalid_retained = connection.execute(
        """
        SELECT ids.file_id
        FROM scope_ids AS ids
        JOIN files ON files.file_id = ids.file_id
        WHERE ids.scope = 'retained_file_ids'
          AND (files.role != 'original' OR files.file_status != 'retained')
        LIMIT 1
        """
    ).fetchone()
    if invalid_retained:
        raise ManifestError("retained_file_ids may contain only retained original files")

    if manifest["retained_scope_status"] == "complete":
        total = connection.execute(
            """
            SELECT COUNT(*) FROM files
            WHERE role = 'original' AND file_status = 'retained'
            """
        ).fetchone()[0]
        selected = connection.execute(
            "SELECT COUNT(*) FROM scope_ids WHERE scope = 'retained_file_ids'"
        ).fetchone()[0]
        missing = connection.execute(
            """
            SELECT file_id FROM files
            WHERE role = 'original' AND file_status = 'retained'
            EXCEPT
            SELECT file_id FROM scope_ids WHERE scope = 'retained_file_ids'
            LIMIT 1
            """
        ).fetchone()
        if total != selected or missing:
            raise ManifestError(
                "retained_file_ids must cover every retained original when "
                "retained_scope_status is complete"
            )

    default_outside_retained = connection.execute(
        """
        SELECT delivery.file_id
        FROM scope_ids AS delivery
        LEFT JOIN scope_ids AS retained
          ON retained.scope = 'retained_file_ids' AND retained.file_id = delivery.file_id
        WHERE delivery.scope = 'default_file_ids' AND retained.file_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if default_outside_retained:
        raise ManifestError("default_file_ids must be a subset of retained_file_ids")

    if (
        manifest["recording_class"] == "normal"
        and manifest["default_scope_status"] == "complete"
    ):
        retained_count = connection.execute(
            "SELECT COUNT(*) FROM scope_ids WHERE scope = 'retained_file_ids'"
        ).fetchone()[0]
        default_count = connection.execute(
            "SELECT COUNT(*) FROM scope_ids WHERE scope = 'default_file_ids'"
        ).fetchone()[0]
        order_mismatch = connection.execute(
            """
            SELECT 1
            FROM scope_ids AS retained
            LEFT JOIN scope_ids AS delivery
              ON delivery.scope = 'default_file_ids' AND delivery.ordinal = retained.ordinal
            WHERE retained.scope = 'retained_file_ids'
              AND delivery.file_id IS NOT retained.file_id
            LIMIT 1
            """
        ).fetchone()
        if retained_count != default_count or order_mismatch:
            raise ManifestError(
                "normal recording default_file_ids must equal retained_file_ids"
            )

    connection.execute("DELETE FROM scope_reasons")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reliable_locators (
            scope TEXT NOT NULL,
            locator TEXT NOT NULL,
            file_id TEXT NOT NULL,
            PRIMARY KEY (scope, locator)
        )
        """
    )
    connection.execute("DELETE FROM reliable_locators")
    for scope, scope_label in (
        ("retained_file_ids", "retained"),
        ("default_file_ids", "default"),
    ):
        rows = connection.execute(
            """
            SELECT ids.file_id, files.entry_json
            FROM scope_ids AS ids
            JOIN files ON files.file_id = ids.file_id
            WHERE ids.scope = ?
            ORDER BY ids.ordinal
            """,
            (scope,),
        )
        while batch := rows.fetchmany(256):
            for file_id, entry_json in batch:
                locator = _source_locator_key(json.loads(entry_json))
                if locator is None:
                    continue
                try:
                    connection.execute(
                        "INSERT INTO reliable_locators (scope, locator, file_id) VALUES (?, ?, ?)",
                        (scope_label, locator, file_id),
                    )
                except sqlite3.IntegrityError as exc:
                    previous = connection.execute(
                        "SELECT file_id FROM reliable_locators WHERE scope = ? AND locator = ?",
                        (scope_label, locator),
                    ).fetchone()[0]
                    raise ManifestError(
                        f"duplicate reliable source locator in {scope_label} scope: "
                        f"{previous} and {file_id} ({locator})"
                    ) from exc


def _output_path_conflict(
    output_path: Path, manifest_path: Path, connection: sqlite3.Connection
) -> Path | None:
    if output_path == manifest_path:
        return manifest_path
    rows = connection.execute("SELECT local_copy_path FROM files ORDER BY ordinal")
    while batch := rows.fetchmany(256):
        for (local_copy_path,) in batch:
            copy_path = _resolve_copy_path(manifest_path, local_copy_path)
            if output_path == copy_path:
                return copy_path
    return None


def _append_finding(connection: sqlite3.Connection, finding: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO findings (result_json) VALUES (?)",
        (json.dumps(finding, ensure_ascii=False, separators=(",", ":")),),
    )


def _append_file_findings(
    connection: sqlite3.Connection, file_result: dict[str, Any]
) -> None:
    media = file_result["media"]
    local_copy = file_result["local_copy"]
    file_id = file_result["file_id"]
    if local_copy["identity_status"] != "match":
        _append_finding(
            connection,
            {
                "file_id": file_id,
                "category": "source_copy_identity",
                "status": local_copy["identity_status"],
                "reasons": local_copy["identity_unknown_reasons"],
            },
        )
    if media.get("probe_stderr"):
        _append_finding(
            connection,
            {
                "file_id": file_id,
                "category": "probe_diagnostic",
                "exit_code": media.get("probe_exit_code"),
                "diagnostics": media["probe_stderr"],
            },
        )
    for check in media["decode_checks"]:
        if check.get("status") == "decode_failed" or check.get("diagnostics_present"):
            _append_finding(
                connection,
                {
                    "file_id": file_id,
                    "category": "decode_diagnostic",
                    "stream_index": check.get("stream_index"),
                    "stream_type": check.get("stream_type"),
                    "exit_code": check.get("exit_code"),
                    "diagnostics": check.get("diagnostics", ""),
                },
            )
    main_video_count = len(media.get("main_videos", []))
    if file_result["role"] == "original" and main_video_count != EXPECTED_MAIN_VIDEO_STREAMS:
        _append_finding(
            connection,
            {
                "file_id": file_id,
                "category": "main_video_stream_count",
                "status": "unknown",
                "observed": main_video_count,
                "required": EXPECTED_MAIN_VIDEO_STREAMS,
            },
        )
    for video in media.get("main_videos", []):
        assessment = video.get("frame_count_assessment", {})
        differences = assessment.get("observation_differences", [])
        if differences:
            _append_finding(
                connection,
                {
                    "file_id": file_id,
                    "category": "frame_count_observation_difference",
                    "status": "observed_difference",
                    "main_video_ordinal": video.get("main_video_ordinal"),
                    "reliable_frame_count": assessment.get("reliable_frame_count"),
                    "reliable_count_basis": assessment.get("reliable_count_basis"),
                    "differences": differences,
                },
            )


def _expected_fraction(expected: dict[str, Any]) -> Fraction | None:
    raw = expected.get("expected_duration_ms_exact")
    if not isinstance(raw, str):
        return None
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(int(raw))
    except (ValueError, ZeroDivisionError):
        return None


def _build_scope_indexed(
    connection: sqlite3.Connection,
    scope_name: str,
    scope_key: str,
    declared_status: str,
    expected: dict[str, Any],
    expected_sampling_quantity: dict[str, Any],
    tolerance_ms: int,
    batch_size: int,
) -> dict[str, Any]:
    totals = [Fraction(0), Fraction(0)]
    contributing = [0, 0]
    sample_totals = [0, 0]
    sample_contributing = [0, 0]
    file_count = connection.execute(
        "SELECT COUNT(*) FROM scope_ids WHERE scope = ?", (scope_key,)
    ).fetchone()[0]
    identity_matches = 0
    identity_mismatches = 0
    rows = connection.execute(
        """
        SELECT ids.ordinal, ids.file_id, files.result_json
        FROM scope_ids AS ids
        JOIN files ON files.file_id = ids.file_id
        WHERE ids.scope = ?
        ORDER BY ids.ordinal
        """,
        (scope_key,),
    )
    while batch := rows.fetchmany(batch_size):
        for ordinal, file_id, result_json in batch:
            result = json.loads(result_json)
            media = result["media"]
            identity_status = result["local_copy"]["identity_status"]
            identity_matches += identity_status == "match"
            identity_mismatches += identity_status == "mismatch"
            videos = media.get("main_videos", [])
            for stream_ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
                position = ordinal * 4
                if media.get("probe_exit_code") != 0:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO scope_reasons
                            (scope, stream_ordinal, position, reason)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            scope_name,
                            stream_ordinal,
                            position,
                            f"{file_id}:media_probe_not_successfully_completed",
                        ),
                    )
                    position += 1
                if len(videos) > EXPECTED_MAIN_VIDEO_STREAMS:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO scope_reasons
                            (scope, stream_ordinal, position, reason)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            scope_name,
                            stream_ordinal,
                            position,
                            f"{file_id}:unexpected_main_video_stream_count",
                        ),
                    )
                    position += 1
                duration = _file_duration_ms(result, stream_ordinal)
                if duration is None:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO scope_reasons
                            (scope, stream_ordinal, position, reason)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            scope_name,
                            stream_ordinal,
                            position,
                            f"{file_id}:main_video_{stream_ordinal}_duration_unavailable",
                        ),
                    )
                else:
                    totals[stream_ordinal] += duration
                    contributing[stream_ordinal] += 1

                if expected_sampling_quantity["status"] != "not_applicable":
                    video = videos[stream_ordinal] if stream_ordinal < len(videos) else None
                    assessment = (
                        video.get("frame_count_assessment", {})
                        if isinstance(video, dict)
                        else {}
                    )
                    frame_count = _nonnegative_int(
                        assessment.get("reliable_frame_count")
                    )
                    if assessment.get("status") == "known" and frame_count is not None:
                        sample_totals[stream_ordinal] += frame_count
                        sample_contributing[stream_ordinal] += 1

    if declared_status == "unknown":
        for stream_ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
            connection.execute(
                """
                INSERT OR IGNORE INTO scope_reasons
                    (scope, stream_ordinal, position, reason)
                VALUES (?, ?, ?, ?)
                """,
                (
                    scope_name,
                    stream_ordinal,
                    2**63 - 1,
                    f"{scope_name}_scope_not_confirmed_complete",
                ),
            )

    expected_ms = _expected_fraction(expected)
    streams: list[dict[str, Any]] = []
    statuses: list[str] = []
    for stream_ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
        reason_count = connection.execute(
            "SELECT COUNT(*) FROM scope_reasons WHERE scope = ? AND stream_ordinal = ?",
            (scope_name, stream_ordinal),
        ).fetchone()[0]
        measured = totals[stream_ordinal] if reason_count == 0 else None
        comparison = compare_duration(expected_ms, measured, tolerance_ms)
        if reason_count:
            unknown_reasons: list[Any] = []
            if expected_ms is None:
                unknown_reasons.append("expected_playback_duration_unavailable")
            unknown_reasons.append(
                _stream_marker(
                    "scope_reasons",
                    {"scope": scope_name, "stream_ordinal": stream_ordinal},
                )
            )
            comparison["unknown_reasons"] = unknown_reasons
        comparison["main_video_ordinal"] = stream_ordinal
        comparison["observed_partial_duration_ms_exact"] = _fraction_text(
            totals[stream_ordinal]
        )
        comparison["contributing_file_count"] = contributing[stream_ordinal]
        comparison["required_file_count"] = file_count
        streams.append(comparison)
        statuses.append(comparison["status"])

    sample_streams: list[dict[str, Any]] = []
    sample_statuses: list[str] = []
    for stream_ordinal in range(EXPECTED_MAIN_VIDEO_STREAMS):
        sample_reasons: list[str] = []
        if expected_sampling_quantity["status"] != "not_applicable":
            if declared_status == "unknown":
                sample_reasons.append(f"{scope_name}_scope_not_confirmed_complete")
            if sample_contributing[stream_ordinal] != file_count:
                sample_reasons.append(
                    "one_or_more_required_file_frame_counts_unavailable"
                )
        sample_scope_complete = (
            expected_sampling_quantity["status"] != "not_applicable"
            and declared_status == "complete"
            and sample_contributing[stream_ordinal] == file_count
        )
        sample_measured = (
            sample_totals[stream_ordinal] if sample_scope_complete else None
        )
        sample_comparison = compare_sample_count(
            expected_sampling_quantity, sample_measured
        )
        sample_comparison["main_video_ordinal"] = stream_ordinal
        sample_comparison["observed_partial_frame_count"] = sample_totals[
            stream_ordinal
        ]
        sample_comparison["reliable_file_count"] = sample_contributing[
            stream_ordinal
        ]
        sample_comparison["required_file_count"] = file_count
        sample_comparison["unknown_reasons"] = _unique(
            [*sample_comparison["unknown_reasons"], *sample_reasons]
        )
        sample_streams.append(sample_comparison)
        sample_statuses.append(sample_comparison["status"])

    local_status = _combine_statuses(statuses)
    local_sample_status = (
        "not_applicable"
        if sample_statuses and all(status == "not_applicable" for status in sample_statuses)
        else _combine_statuses(sample_statuses)
    )
    if file_count == 0:
        identity_status = "empty_scope"
    elif identity_mismatches:
        identity_status = "mismatch"
    elif identity_matches == file_count:
        identity_status = "verified"
    else:
        identity_status = "unknown"
    return {
        "scope_id": scope_name,
        "declared_scope_status": declared_status,
        "file_ids": _stream_marker("scope_file_ids", scope_key),
        "local_copy_duration_comparison": {
            "status": local_status,
            "expected_basis": expected,
            "streams": streams,
            "cross_segment_continuity": "not_assessed",
            "continuity_unknown_reason": "summed_stream_duration_does_not_prove_no_gap_or_overlap",
        },
        "local_copy_sampling_quantity_comparison": {
            "status": local_sample_status,
            "expected_basis": expected_sampling_quantity,
            "streams": sample_streams,
        },
        "source_copy_identity_status": identity_status,
        "source_duration_status": (
            local_status
            if identity_status in {"verified", "empty_scope"}
            else "unknown"
        ),
        "source_sampling_quantity_status": (
            local_sample_status
            if identity_status in {"verified", "empty_scope"}
            else (
                "not_applicable"
                if local_sample_status == "not_applicable"
                else "unknown"
            )
        ),
    }


def _write_json_string_from_chunks(stream: Any, chunks: Iterable[str]) -> None:
    stream.write('"')
    for chunk in chunks:
        if chunk:
            stream.write(json.dumps(chunk, ensure_ascii=False)[1:-1])
    stream.write('"')


def _diagnostic_chunks(path: Path) -> Iterable[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            text = decoder.decode(chunk)
            if text:
                yield text
        final_text = decoder.decode(b"", final=True)
        if final_text:
            yield final_text


def _write_streamed_value(
    stream: Any,
    value: Any,
    connection: sqlite3.Connection,
    diagnostic_dir: Path,
    diagnostic_session_id: str,
    batch_size: int,
) -> None:
    if isinstance(value, dict) and set(value) == {STREAM_MARKER}:
        marker = value[STREAM_MARKER]
        kind = marker.get("kind") if isinstance(marker, dict) else None
        if kind == "diagnostic":
            token = marker.get("token")
            if not isinstance(token, str) or not token.startswith(diagnostic_session_id + "-"):
                raise ValueError("invalid staged diagnostic reference")
            path = diagnostic_dir / f"{token}.stderr"
            _write_json_string_from_chunks(stream, _diagnostic_chunks(path))
            return
        if kind == "files":
            stream.write("[")
            rows = connection.execute(
                "SELECT result_json FROM files ORDER BY ordinal"
            )
            first = True
            while batch := rows.fetchmany(batch_size):
                for (result_json,) in batch:
                    if not first:
                        stream.write(",")
                    first = False
                    _write_streamed_value(
                        stream,
                        json.loads(result_json),
                        connection,
                        diagnostic_dir,
                        diagnostic_session_id,
                        batch_size,
                    )
            stream.write("]")
            return
        if kind == "findings":
            stream.write("[")
            rows = connection.execute(
                "SELECT result_json FROM findings ORDER BY ordinal"
            )
            first = True
            while batch := rows.fetchmany(batch_size):
                for (finding_json,) in batch:
                    if not first:
                        stream.write(",")
                    first = False
                    _write_streamed_value(
                        stream,
                        json.loads(finding_json),
                        connection,
                        diagnostic_dir,
                        diagnostic_session_id,
                        batch_size,
                    )
            stream.write("]")
            return
        if kind == "scope_file_ids":
            scope_key = marker.get("key")
            stream.write("[")
            rows = connection.execute(
                "SELECT file_id FROM scope_ids WHERE scope = ? ORDER BY ordinal",
                (scope_key,),
            )
            first = True
            while batch := rows.fetchmany(batch_size):
                for (file_id,) in batch:
                    if not first:
                        stream.write(",")
                    first = False
                    stream.write(json.dumps(file_id, ensure_ascii=False))
            stream.write("]")
            return
        if kind == "scope_reasons":
            key = marker.get("key")
            if not isinstance(key, dict):
                raise ValueError("invalid streamed scope reason reference")
            rows = connection.execute(
                """
                SELECT reason FROM scope_reasons
                WHERE scope = ? AND stream_ordinal = ?
                ORDER BY position, reason
                """,
                (key["scope"], key["stream_ordinal"]),
            )

            def joined_reasons() -> Iterable[str]:
                first_reason = True
                while batch := rows.fetchmany(batch_size):
                    for (reason,) in batch:
                        if not first_reason:
                            yield ";"
                        first_reason = False
                        yield reason

            _write_json_string_from_chunks(stream, joined_reasons())
            return
        raise ValueError("unknown streamed report value")

    if isinstance(value, dict):
        stream.write("{")
        for position, key in enumerate(sorted(value)):
            if position:
                stream.write(",")
            stream.write(json.dumps(key, ensure_ascii=False))
            stream.write(":")
            _write_streamed_value(
                stream,
                value[key],
                connection,
                diagnostic_dir,
                diagnostic_session_id,
                batch_size,
            )
        stream.write("}")
        return
    if isinstance(value, list):
        stream.write("[")
        for position, item in enumerate(value):
            if position:
                stream.write(",")
            _write_streamed_value(
                stream,
                item,
                connection,
                diagnostic_dir,
                diagnostic_session_id,
                batch_size,
            )
        stream.write("]")
        return
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _write_report_atomic(
    path: Path,
    report: dict[str, Any],
    connection: sqlite3.Connection,
    diagnostics: DiagnosticArtifacts,
    batch_size: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _write_streamed_value(
                stream,
                report,
                connection,
                diagnostics.directory,
                diagnostics.session_id,
                batch_size,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _positive_batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("batch size must be a positive integer") from exc
    if parsed <= 0 or str(parsed) != value:
        raise argparse.ArgumentTypeError("batch size must be a positive integer")
    return parsed


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
    parser.add_argument(
        "--frame-count-shortfall-tolerance",
        type=int,
        default=DEFAULT_FRAME_COUNT_SHORTFALL_TOLERANCE,
        help=(
            "原生延时整体帧数短缺容差（非负整数帧，默认 "
            f"{DEFAULT_FRAME_COUNT_SHORTFALL_TOLERANCE}，允许 0）"
        ),
    )
    parser.add_argument("--output", required=True, type=Path, help="验收证据 JSON 输出路径")
    parser.add_argument(
        "--batch-size",
        type=_positive_batch_size,
        default=64,
        help="临时 SQLite 索引和最终报告写出的批次大小（默认 64）",
    )
    return parser


def _run_indexed_validation(
    manifest: dict[str, Any],
    manifest_path: Path,
    output_path: Path,
    connection: sqlite3.Connection,
    diagnostics: DiagnosticArtifacts,
    tolerance: int,
    frame_count_tolerance: int,
    batch_size: int,
) -> bool:
    ffprobe_version = _tool_version("ffprobe", diagnostics)
    ffmpeg_version = _tool_version("ffmpeg", diagnostics)
    tool_errors = [
        {
            "tool": name,
            "error": tool["execution_error"] or tool["stderr"],
        }
        for name, tool in (("ffprobe", ffprobe_version), ("ffmpeg", ffmpeg_version))
        if tool["exit_code"] != 0
    ]

    if manifest.get("source_action_instance_id") is None:
        _append_finding(
            connection,
            {
                "category": "source_action_identity",
                "status": "unknown",
                "reason": "source_action_instance_id_not_available_in_manifest",
            },
        )

    rows = connection.execute(
        "SELECT ordinal, entry_json FROM files ORDER BY ordinal"
    )
    while batch := rows.fetchmany(batch_size):
        for ordinal, entry_json in batch:
            entry = json.loads(entry_json)
            result = _inspect_file(entry, manifest_path, "ffprobe", "ffmpeg", diagnostics)
            result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "UPDATE files SET result_json = ? WHERE ordinal = ?",
                (result_json, ordinal),
            )
            _append_file_findings(connection, result)
        connection.commit()

    expected = derive_expected_playback_duration(manifest)
    expected_sampling_quantity = derive_expected_sampling_quantity(
        manifest, frame_count_tolerance
    )
    scopes = {
        "all_retained": _build_scope_indexed(
            connection,
            "all_retained",
            "retained_file_ids",
            manifest["retained_scope_status"],
            expected,
            expected_sampling_quantity,
            tolerance,
            batch_size,
        ),
        "default_delivery": _build_scope_indexed(
            connection,
            "default_delivery",
            "default_file_ids",
            manifest["default_scope_status"],
            expected,
            expected_sampling_quantity,
            tolerance,
            batch_size,
        ),
    }
    if expected["status"] == "unknown":
        _append_finding(
            connection,
            {
                "category": "expected_playback_duration",
                "status": "unknown",
                "reasons": expected["unknown_reasons"],
            },
        )
    if expected_sampling_quantity["status"] == "unknown":
        _append_finding(
            connection,
            {
                "category": "expected_sampling_quantity",
                "status": "unknown",
                "reasons": expected_sampling_quantity["unknown_reasons"],
            },
        )
    for scope in scopes.values():
        comparison = scope["local_copy_duration_comparison"]
        if comparison["status"] != "pass":
            _append_finding(
                connection,
                {
                    "category": "scope_duration",
                    "scope_id": scope["scope_id"],
                    "status": comparison["status"],
                    "streams": comparison["streams"],
                },
            )
        sample_comparison = scope["local_copy_sampling_quantity_comparison"]
        if sample_comparison["status"] not in {"pass", "not_applicable"}:
            _append_finding(
                connection,
                {
                    "category": "scope_sampling_quantity",
                    "scope_id": scope["scope_id"],
                    "status": sample_comparison["status"],
                    "streams": sample_comparison["streams"],
                },
            )

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": _utc_now(),
        "execution_status": (
            "completed_with_tool_errors" if tool_errors else "completed"
        ),
        "acceptance_id": manifest["acceptance_id"],
        "source_action_instance_id": manifest["source_action_instance_id"],
        "source_action_instance_id_status": (
            "known" if manifest["source_action_instance_id"] is not None else "unknown"
        ),
        "recording_kind": manifest["recording_kind"],
        "recording_class": manifest["recording_class"],
        "task_target_duration_ms": manifest["target_duration_ms"],
        "shortfall_tolerance_ms": tolerance,
        "frame_count_shortfall_tolerance": frame_count_tolerance,
        "processing_batch_size": batch_size,
        "expected_playback": expected,
        "expected_sampling_quantity": expected_sampling_quantity,
        "tools": {"ffprobe": ffprobe_version, "ffmpeg": ffmpeg_version},
        "tool_errors": tool_errors,
        "files": _stream_marker("files"),
        "scopes": scopes,
        "findings": _stream_marker("findings"),
        "exit_code_meaning": "report_generation_status_only; media_findings_remain_authoritative",
    }
    _write_report_atomic(output_path, report, connection, diagnostics, batch_size)
    return bool(tool_errors)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tolerance = parse_shortfall_tolerance_ms(args.shortfall_tolerance_ms)
    except (TypeError, ValueError) as exc:
        _parser().error(str(exc))
    try:
        frame_count_tolerance = parse_frame_count_shortfall_tolerance(
            args.frame_count_shortfall_tolerance
        )
    except (TypeError, ValueError) as exc:
        _parser().error(str(exc))

    manifest_path = args.manifest.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if output_path == manifest_path:
        print("验收报告输出路径不能覆盖输入清单", file=sys.stderr)
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="osmo-media-validation-") as temporary_root:
            temporary_directory = Path(temporary_root)
            database_path = temporary_directory / "acceptance-index.sqlite3"
            diagnostics_directory = temporary_directory / "diagnostics"
            diagnostics_directory.mkdir()
            diagnostics = DiagnosticArtifacts(diagnostics_directory)
            with sqlite3.connect(database_path) as connection:
                _initialize_index(connection)
                manifest = _load_manifest_index(manifest_path, connection)
                conflict = _output_path_conflict(output_path, manifest_path, connection)
                if conflict is not None:
                    print(
                        f"验收报告输出路径不能覆盖输入文件：{conflict}",
                        file=sys.stderr,
                    )
                    return 2
                tool_errors = _run_indexed_validation(
                    manifest,
                    manifest_path,
                    output_path,
                    connection,
                    diagnostics,
                    tolerance,
                    frame_count_tolerance,
                    args.batch_size,
                )
    except ManifestError as exc:
        print(f"验收清单无效：{exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"无法完成媒体验收：{exc}", file=sys.stderr)
        return 2
    print(f"已写入验收证据：{output_path}")
    print(
        "媒体结论见报告中的 scopes、findings 与逐流 decode 诊断；"
        "退出码 0 仅表示报告已生成。"
    )
    return 2 if tool_errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
