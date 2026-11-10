"""主机照片的来源身份与正式原图、预览关联保持一致。"""

import pytest

from camctl.capture.photo_pair_rules import (
    _HostPhotoPairError, _host_photo_registered_pair, _host_photo_source_pair,
)
from camctl.contracts.enums import enum_for
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import relative_file_path
from camctl.host_files.photo_formats import CaptureFormat, photo_file_extension


def _source(file_id, purpose):
    return {"id": file_id, "owner_action_id": 1, "owner_delivery_id": None,
        "purpose": int(enum_for("intermediate_files.purpose")[purpose.name]),
        "relative_path": relative_file_path(purpose, file_id, photo_file_extension(purpose, CaptureFormat.RAW)),
        "retention_state": int(enum_for("intermediate_files.retention_state").REQUIRED),
        "cleanup_state": int(enum_for("intermediate_files.cleanup_state").NOT_NEEDED),
        "size_bytes": 5, "sha256": "a" * 64}


def test_complete_original_preview_are_bound_to_registered_outputs():
    action = {"effective_params_json": {"capture_format": "raw"}}
    original, preview = _source(8, FilePurpose.CAPTURE_ORIGINAL), _source(9, FilePurpose.CAPTURE_PREVIEW)
    sources = _host_photo_source_pair({9: preview, 8: original}, 1, host_spec=True, action=action)
    kinds = enum_for("outputs.kind")
    assert sources[int(kinds.ORIGINAL)] is original
    assert sources[int(kinds.PREVIEW)] is preview
    outputs = {
        11: {"source_action_id": 1, "kind": int(kinds.ORIGINAL), "intermediate_file_id": 8,
             "device_file_id": None, "original_name": "image.3fr", "media_type": "image/x-hasselblad-3fr"},
        12: {"source_action_id": 1, "kind": int(kinds.PREVIEW), "intermediate_file_id": 9,
             "device_file_id": None, "original_name": "preview.jpg", "media_type": "image/jpeg"},
    }
    # 媒体类型由照片格式契约提供，测试只固定身份与关联期望。
    from camctl.host_files.photo_formats import photo_media_type
    outputs[11]["media_type"] = photo_media_type(FilePurpose.CAPTURE_ORIGINAL, CaptureFormat.RAW)
    outputs[12]["media_type"] = photo_media_type(FilePurpose.CAPTURE_PREVIEW, CaptureFormat.RAW)
    _host_photo_registered_pair(outputs, {12: {"output_id": 12, "original_output_id": 11}},
                                1, sources, action=action)
    outputs[12]["intermediate_file_id"] = 8
    with pytest.raises(_HostPhotoPairError):
        _host_photo_registered_pair(outputs, {12: {"output_id": 12, "original_output_id": 11}},
                                    1, sources, action=action)


@pytest.mark.parametrize("change", ["missing", "wrong_owner", "incomplete", "bad_digest"])
def test_host_photo_rejects_missing_or_unreliable_source(change):
    original, preview = _source(8, FilePurpose.CAPTURE_ORIGINAL), _source(9, FilePurpose.CAPTURE_PREVIEW)
    rows = {8: original, 9: preview}
    if change == "missing": rows.pop(9)
    if change == "wrong_owner": preview["owner_action_id"] = 2
    if change == "incomplete": preview["size_bytes"] = None
    if change == "bad_digest": preview["sha256"] = "invalid"
    with pytest.raises(_HostPhotoPairError):
        _host_photo_source_pair(rows, 1, host_spec=True,
                                action={"effective_params_json": {"capture_format": "raw"}})
