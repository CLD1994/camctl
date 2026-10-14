"""相机原图、缩略图及同名替换的固定身份互不混淆。"""

import pytest

from camctl.capture.files import file_identity_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, DownloadVariant


def test_fixed_identity_round_trip_and_distinct_variant_or_capture_content():
    original = CameraFileIdentity(DeviceBinding("cam-1", DRIVER_ID), "A0001.3FR",
                                  DownloadVariant.ORIGINAL, "a" * 64)
    preview = CameraFileIdentity(original.binding, original.original_name,
                                 DownloadVariant.THUMBNAIL, "b" * 64)
    replaced = CameraFileIdentity(original.binding, original.original_name,
                                  original.variant, "c" * 64)
    key = file_identity_key("cam-1", DRIVER_ID, original.file_identity)
    assert CameraFileIdentity.from_source(key, original.locator()) == original
    assert len({original.file_identity, preview.file_identity, replaced.file_identity}) == 3
    assert preview.locator()["original_name"] == "A0001.3FR"


@pytest.mark.parametrize("name", ["", ".", "..", "../file", "a/b", "a\\b", "bad\0name", "a\nname"])
def test_original_name_must_be_a_direct_tool_filename(name):
    with pytest.raises(ValueError):
        CameraFileIdentity(DeviceBinding("cam-1", DRIVER_ID), name, DownloadVariant.ORIGINAL, "a" * 64)


@pytest.mark.parametrize("change", [{"sha256": "b" * 64}, {"variant": "thumbnail"},
                                    {"original_name": "other.3FR"}, {"extra": True}])
def test_locator_cannot_redefine_original_fixed_identity(change):
    identity = CameraFileIdentity(DeviceBinding("cam-1", DRIVER_ID), "A0001.3FR", DownloadVariant.ORIGINAL, "a" * 64)
    with pytest.raises(ValueError):
        CameraFileIdentity.from_source(file_identity_key("cam-1", DRIVER_ID, identity.file_identity),
                                       dict(identity.locator(), **change))
