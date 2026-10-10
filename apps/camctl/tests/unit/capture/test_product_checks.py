"""集合未知和未取得的文件事实优先于产物不合格。"""
from dataclasses import replace

import pytest

from camctl.capture.results import (
    CaptureFile, CaptureFileSet, FileKind, ProductRequirements, assess_capture_files,
)

_VIDEO = ProductRequirements(frozenset({FileKind.VIDEO}))
_FILE = CaptureFile("video", FileKind.VIDEO, True, True)


def test_scan_end_without_finality_is_unknown():
    result = assess_capture_files(CaptureFileSet((_FILE,), False), _VIDEO)
    assert not result.is_complete and not result.explicitly_unmet


@pytest.mark.parametrize("override", [
    {"read_error": True}, {"ownership_confirmed": False}, {"complete": False},
])
def test_unknown_file_fact_precedes_missing_kind(override):
    requirements = ProductRequirements(frozenset({FileKind.PHOTO, FileKind.VIDEO}))
    result = assess_capture_files(CaptureFileSet((replace(_FILE, **override),), True), requirements)
    assert not result.is_complete and not result.explicitly_unmet


def test_one_pass_file_iterator_preserves_all_requirements():
    files = iter((_FILE, CaptureFile("photo", FileKind.PHOTO, True, True)))
    requirements = ProductRequirements(frozenset({FileKind.PHOTO, FileKind.VIDEO}))
    result = assess_capture_files(CaptureFileSet(files, True), requirements)
    assert result.is_complete and not result.explicitly_unmet


def _rule(**kwargs):
    from camctl.capture.results import ProductRule
    return ProductRule(FileKind.PHOTO, **kwargs)


def _formatted_file(format_id, **overrides):
    from camctl.capture.results import CaptureFile
    return CaptureFile("photo", FileKind.PHOTO, True, True, format_id=format_id, **overrides)


def test_raw_does_not_satisfy_jpeg_requirement():
    requirement = ProductRequirements(frozenset({FileKind.PHOTO}), (_rule(format_id="jpeg"),))
    result = assess_capture_files(CaptureFileSet((_formatted_file("raw"),), True), requirement)
    assert not result.is_complete and result.explicitly_unmet
    assert result.rules[0].state.value == "failed" and result.rules[0].count == 0


def test_unknown_format_does_not_prove_jpeg_missing():
    requirement = ProductRequirements(frozenset({FileKind.PHOTO}), (_rule(format_id="jpeg"),))
    result = assess_capture_files(CaptureFileSet((_formatted_file(None),), True), requirement)
    assert not result.is_complete and not result.explicitly_unmet
    assert result.rules[0].state.value == "unknown"


@pytest.mark.parametrize("count,expected", [(0, "failed"), (1, "failed"), (2, "passed"), (3, "failed")])
def test_exact_count_only_when_task_declares_it(count, expected):
    requirement = ProductRequirements(frozenset({FileKind.PHOTO}), (_rule(exact_count=2),))
    files = (CaptureFile(str(i), FileKind.PHOTO, True, True) for i in range(count))
    result = assess_capture_files(CaptureFileSet(files, True), requirement)
    assert result.rules[0].state.value == expected
    assert result.is_complete is (expected == "passed")


@pytest.mark.parametrize("pairing,expected", [(True, "passed"), (False, "failed"), (None, "unknown")])
def test_pairing_only_when_task_requires_it(pairing, expected):
    requirement = ProductRequirements(frozenset({FileKind.PHOTO}), (_rule(require_pairing=True),))
    file = _formatted_file("jpeg", pairing_confirmed=pairing)
    result = assess_capture_files(CaptureFileSet((file,), True), requirement)
    assert result.rules[0].state.value == expected
    assert result.is_complete is (expected == "passed")
    assert result.explicitly_unmet is (expected == "failed")


def test_segmented_video_has_no_implied_exact_count():
    from camctl.capture.results import ProductRule
    requirement = ProductRequirements(frozenset({FileKind.VIDEO}), (ProductRule(FileKind.VIDEO),))
    result = assess_capture_files(CaptureFileSet((replace(_FILE, file_id=str(i)) for i in range(3)), True), requirement)
    assert result.is_complete and result.file_count == 3


def test_large_unknown_set_has_bounded_diagnostics_and_exact_counts():
    files = (CaptureFile(str(i), FileKind.VIDEO, False, False, True) for i in range(1000))
    result = assess_capture_files(CaptureFileSet(files, True), _VIDEO)
    assert result.file_count == result.incomplete_count == result.ownership_pending_count == result.read_error_count == 1000
    assert len(result.incomplete_files) == len(result.ownership_pending) == len(result.read_errors) == 128


_RULE_DOCUMENT = {"kind": "photo", "format_id": "jpeg", "min_count": 1, "exact_count": None, "require_pairing": False}


def test_product_rules_are_fixed_with_original_task():
    from camctl.capture.models import build_capture_spec, validate_capture_spec
    from camctl.devices.tasks import CaptureTask
    original = dict(_RULE_DOCUMENT)
    task = CaptureTask("camera_record", target_duration_s=10, stop_supported=True, product_rules=(original,))
    spec = build_capture_spec("camera_record", task)
    original["format_id"] = "raw"
    assert spec["product_rules"] == [_RULE_DOCUMENT]
    assert validate_capture_spec("camera_record", spec) == spec


@pytest.mark.parametrize("changes", [
    {"extra": True}, {"kind": "invalid"}, {"min_count": True}, {"min_count": -1},
    {"exact_count": False}, {"exact_count": 0}, {"format_id": ""}, {"require_pairing": 1},
])
def test_invalid_saved_product_rule_is_rejected(changes):
    from camctl.capture.models import validate_capture_spec
    with pytest.raises(ValueError):
        validate_capture_spec("camera_record", {"target_duration_ms": 10000, "product_rules": [{**_RULE_DOCUMENT, **changes}]})
