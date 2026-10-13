"""主机原图和预览保留独立文件身份，并显式区分同批配对空间。"""

import pytest

from camctl.outputs.catalog import (
    FileReference, OutputCatalogFacts, OutputDraft, OutputKind,
    validate_output_registration,
)


def _host(kind, file_id, **pair):
    return OutputDraft(
        kind, FileReference(intermediate_file_id=file_id), True,
        sha256="a" * 64, original_name="capture.3fr" if kind is OutputKind.ORIGINAL else "capture.jpg",
        media_type="image/x-hasselblad-3fr" if kind is OutputKind.ORIGINAL else "image/jpeg",
        **pair,
    )


def test_host_preview_pairs_with_host_original_in_reversed_input_order():
    original = _host(OutputKind.ORIGINAL, 11)
    preview = _host(OutputKind.PREVIEW, 12, original_batch_intermediate_file_id=11)
    changes = validate_output_registration((preview, original), OutputCatalogFacts(1, True))
    assert changes.outputs[0].intermediate_file_id == 12
    assert changes.outputs[0].original_batch_intermediate_file_id == 11
    assert changes.outputs[1].intermediate_file_id == 11
    assert changes.outputs[1].original_name == "capture.3fr"


def test_device_id_collision_cannot_satisfy_host_original_reference():
    device_original = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=11), True)
    preview = _host(OutputKind.PREVIEW, 12, original_batch_intermediate_file_id=11)
    with pytest.raises(ValueError):
        validate_output_registration((device_original, preview), OutputCatalogFacts(1, True))


def test_host_original_does_not_accept_origin_reference():
    with pytest.raises(ValueError):
        _host(OutputKind.ORIGINAL, 11, original_batch_intermediate_file_id=12)


def test_host_preview_cannot_claim_two_origin_spaces():
    with pytest.raises(ValueError):
        _host(OutputKind.PREVIEW, 12, original_batch_file_id=11,
              original_batch_intermediate_file_id=11)
