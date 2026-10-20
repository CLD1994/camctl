"""相机原图、缩略图及不同机内路径的固定身份互不混淆。"""

import inspect

import pytest

from camctl.capture.files import file_identity_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import (
    CameraFileIdentity,
    CameraFileLocator,
    CameraLocatorStatus,
    DownloadVariant,
)


def _identity(*, variant=DownloadVariant.ORIGINAL, sha256="a" * 64):
    return CameraFileIdentity(
        DeviceBinding("cam-1", DRIVER_ID),
        camera_path="/mnt/media/DCIM/B9996039.JPG",
        camera_name="B9996039.JPG",
        variant=variant,
        sha256=sha256,
    )


def test_camera_locator_is_independent_from_host_filename():
    if not {"camera_path", "camera_name"} <= set(inspect.signature(CameraFileIdentity).parameters):
        pytest.fail("CameraFileIdentity must preserve its fixed camera path and camera basename")

    host_name = "capture_host_17.JPG"
    locator = CameraFileLocator(
        CameraLocatorStatus.PATH,
        camera_path="/mnt/media/DCIM/B9996039.JPG",
        camera_name="B9996039.JPG",
    )
    identity = CameraFileIdentity(
        DeviceBinding("cam-1", DRIVER_ID),
        camera_path=locator.camera_path,
        camera_name=locator.camera_name,
        variant=DownloadVariant.ORIGINAL,
        sha256="a" * 64,
    )
    key = file_identity_key("cam-1", DRIVER_ID, identity.file_identity)
    restored = CameraFileIdentity.from_source(key, identity.locator())

    assert host_name != restored.camera_name
    assert restored.camera_path == "/mnt/media/DCIM/B9996039.JPG"
    assert restored.camera_name == "B9996039.JPG"
    assert CameraFileLocator.from_json(locator.as_json()) == locator


def test_camera_locator_distinguishes_explicit_missing_and_invalid():
    explicit_missing = CameraFileLocator(CameraLocatorStatus.EXPLICIT_MISSING)
    assert explicit_missing.as_json() == {
        "status": "explicit_missing",
        "camera_path": None,
        "camera_name": None,
    }
    assert CameraFileLocator.from_json(explicit_missing.as_json()) == explicit_missing

    with pytest.raises(ValueError):
        CameraFileLocator(
            CameraLocatorStatus.EXPLICIT_MISSING,
            camera_path="/mnt/media/B9996039.JPG",
            camera_name="B9996039.JPG",
        )
    with pytest.raises(ValueError):
        CameraFileLocator(
            CameraLocatorStatus.PATH,
            camera_path="/mnt/../B9996039.JPG",
            camera_name="B9996039.JPG",
        )
    with pytest.raises(ValueError):
        CameraFileLocator.from_json({
            "status": "unreported",
            "camera_path": None,
            "camera_name": None,
        })


def test_fixed_identity_round_trip_and_distinct_variant_or_capture_content():
    original = _identity()
    preview = _identity(variant=DownloadVariant.THUMBNAIL, sha256="b" * 64)
    replaced = _identity(sha256="c" * 64)
    key = file_identity_key("cam-1", DRIVER_ID, original.file_identity)

    assert CameraFileIdentity.from_source(key, original.locator()) == original
    assert len({original.file_identity, preview.file_identity, replaced.file_identity}) == 3
    assert preview.locator()["camera_name"] == "B9996039.JPG"
    assert preview.locator()["camera_path"] == "/mnt/media/DCIM/B9996039.JPG"


@pytest.mark.parametrize("path", [
    "",
    "relative/B9996039.JPG",
    "/",
    "//mnt/media/B9996039.JPG",
    "/mnt//B9996039.JPG",
    "/mnt/./B9996039.JPG",
    "/mnt/../B9996039.JPG",
    "/mnt/media/B9996039.JPG/",
    "/mnt/media/bad\x00name.JPG",
    "/mnt/media/bad\nname.JPG",
    "/mnt/media/bad\rname.JPG",
    "/mnt/media/bad\tname.JPG",
    "/mnt/media/bad\u2028name.JPG",
])
def test_camera_path_must_be_a_complete_canonical_absolute_posix_path(path):
    with pytest.raises(ValueError):
        CameraFileIdentity(
            DeviceBinding("cam-1", DRIVER_ID),
            camera_path=path,
            camera_name=path.rsplit("/", 1)[-1],
            variant=DownloadVariant.ORIGINAL,
            sha256="a" * 64,
        )


@pytest.mark.parametrize("change", [
    {"sha256": "b" * 64},
    {"variant": "thumbnail"},
    {"camera_name": "other.JPG"},
    {"camera_path": "/mnt/media/DCIM/other.JPG"},
    {"extra": True},
])
def test_locator_cannot_redefine_original_fixed_identity(change):
    identity = _identity()
    with pytest.raises(ValueError):
        CameraFileIdentity.from_source(
            file_identity_key("cam-1", DRIVER_ID, identity.file_identity),
            dict(identity.locator(), **change),
        )
