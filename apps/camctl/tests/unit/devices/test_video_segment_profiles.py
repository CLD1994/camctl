from camctl.devices.segment_profiles import (
    SegmentFileKind,
    profile_for_policy,
)


def test_action6_directory_rollover_and_datetime_independent():
    profile = profile_for_policy("action6_video_segments_v1")
    first = profile.classify_file("/mnt/media_rw/emulated/DCIM/DJI_001/DJI_20000101080621_0999_D.MP4")
    second = profile.classify_file("/mnt/media_rw/emulated/DCIM/DJI_002/DJI_19990101000000_0001_D.MP4")
    assert first.kind is SegmentFileKind.VIDEO
    assert first.key.successor() == second.key


def test_action6_photos_have_no_video_position():
    profile = profile_for_policy("action6_video_segments_v1")
    photo = profile.classify_file("/DCIM/DJI_001/DJI_20000101080621_0002_D.DNG")
    assert photo.kind is SegmentFileKind.PHOTO
    assert photo.key is None


def test_action6_preview_uses_full_case_sensitive_stem():
    profile = profile_for_policy("action6_video_segments_v1")
    video = profile.classify_file("/DCIM/DJI_001/DJI_20000101080621_0002_D.MP4")
    preview = profile.classify_file("/DCIM/DJI_001/DJI_20000101080621_0002_D.lrf")
    assert preview.kind is SegmentFileKind.PREVIEW
    assert preview.pairing_identity == video.pairing_identity


def test_profile_does_not_parse_another_devices_video():
    profile = profile_for_policy("osmo360ii_record_segments_v1")
    assert profile.classify_file("/DCIM/DJI_001/DJI_20000101080621_0002_D.MP4").key is None
    parsed = profile.classify_file("/DCIM/CAM_001/CAM_20000101080621_0002_D.OSV")
    assert parsed.kind is SegmentFileKind.VIDEO
    assert parsed.key.directory_number == 1
    assert profile.allow_empty_tail


def test_action6_empty_video_has_no_tail_exemption():
    assert not profile_for_policy("action6_video_segments_v1").allow_empty_tail
