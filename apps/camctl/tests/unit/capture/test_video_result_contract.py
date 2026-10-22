"""视频结果模式在首次受理时显式固定并由保存读取严格校验。"""

from dataclasses import replace

import pytest

from camctl.capture.models import (
    CaptureDefinition, CaptureInput, build_capture_spec, validate_capture_spec,
)
from camctl.devices.tasks import CaptureResultPolicy, CaptureTask, VideoResultMode


def test_new_record_definition_freezes_explicit_default_single_file_mode():
    task = CaptureTask("camera_record", target_duration_s=10, stop_supported=True)
    spec = build_capture_spec("camera_record", task)
    assert spec["video_result_mode"] == VideoResultMode.SINGLE_FILE.value
    assert validate_capture_spec("camera_record", spec) == spec
    definition = CaptureDefinition.build(CaptureInput("camera_record", {}, task))
    assert definition.video_result_mode is VideoResultMode.SINGLE_FILE


def test_mode_is_frozen_per_task_and_does_not_change_photo_definition():
    task = CaptureTask("camera_record", target_duration_s=10, stop_supported=True,
        video_result_mode=VideoResultMode.SEGMENTED,
        result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
        product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                        "exact_count": None, "min_size_bytes": 1,
                        "require_pairing": False},))
    assert build_capture_spec("camera_record", task)["video_result_mode"] == "segmented"
    assert build_capture_spec("camera_record", task)["result_policy"] == \
        CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    assert build_capture_spec("camera_take_photo", None) == {}


@pytest.mark.parametrize("action_type,task", [
    ("camera_record", CaptureTask("camera_record", target_duration_s=10, stop_supported=True)),
    ("camera_timelapse", CaptureTask("camera_timelapse", target_duration_s=10,
        duration_based=True, wait_after_send=False, stop_supported=True,
        end_control=1, start_return_meaning=1, completion_mode=1)),
])
def test_saved_video_definition_rejects_missing_or_invalid_mode(action_type, task):
    spec = build_capture_spec(action_type, task)
    missing = dict(spec)
    del missing["video_result_mode"]
    with pytest.raises(ValueError, match="结果模式"):
        validate_capture_spec(action_type, missing)
    for bad in (None, True, "other"):
        with pytest.raises(ValueError):
            validate_capture_spec(action_type, {**spec, "video_result_mode": bad})


def test_single_file_mode_cannot_enter_an_osmo_segment_policy():
    task = CaptureTask("camera_record", target_duration_s=10, stop_supported=True,
        video_result_mode=VideoResultMode.SINGLE_FILE,
        result_policy=CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1,
        product_rules=({"kind": "video", "format_id": "osv", "min_count": 1,
                       "exact_count": None, "require_pairing": False,
                       "min_size_bytes": 1},))
    with pytest.raises(ValueError, match="segmented"):
        build_capture_spec("camera_record", task)


def test_segmented_action6_timelapse_policy_validates_mp4_without_changing_photos():
    task = CaptureTask("camera_timelapse", target_duration_s=10,
        duration_based=True, wait_after_send=False, stop_supported=True,
        end_control=1, start_return_meaning=1, completion_mode=1,
        video_result_mode=VideoResultMode.SEGMENTED,
        result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
        product_rules=(
            {"kind": "video", "format_id": "mp4", "min_count": 1,
             "exact_count": None, "min_size_bytes": 1, "require_pairing": False},
            {"kind": "photo", "format_id": "dng", "min_count": 1,
             "exact_count": None, "min_size_bytes": 1, "require_pairing": False},
        ))
    spec = build_capture_spec("camera_timelapse", task)
    assert spec["result_policy"] == "action6_video_segments_v1"
    assert [rule["format_id"] for rule in spec["product_rules"]] == ["mp4", "dng"]


def test_segmented_video_requires_strategy_and_strategy_specific_format():
    missing_policy = CaptureTask("camera_record", target_duration_s=10,
        stop_supported=True,
        video_result_mode=VideoResultMode.SEGMENTED,
        product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                       "exact_count": None, "min_size_bytes": 1,
                       "require_pairing": False},))
    with pytest.raises(ValueError, match="必须保存设备结果策略"):
        build_capture_spec("camera_record", missing_policy)

    wrong_format = CaptureTask("camera_record", target_duration_s=10,
        stop_supported=True,
        video_result_mode=VideoResultMode.SEGMENTED,
        result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
        product_rules=({"kind": "video", "format_id": "osv", "min_count": 1,
                       "exact_count": None, "min_size_bytes": 1,
                       "require_pairing": False},))
    with pytest.raises(ValueError, match="MP4"):
        build_capture_spec("camera_record", wrong_format)
