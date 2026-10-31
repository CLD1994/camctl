"""相机原图、缩略图及不同机内路径的固定身份互不混淆。"""

import inspect
import json
from dataclasses import replace

import pytest

from camctl.capture.files import file_identity_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import (
    CameraFileIdentity,
    CameraFileLocator,
    CameraLocatorStatus,
    DeferredCameraFileIdentity,
    DownloadVariant,
    camera_file_identity_from_source,
    deferred_photo_identities,
)


def _identity(*, variant=DownloadVariant.ORIGINAL, sha256="a" * 64):
    return CameraFileIdentity(
        DeviceBinding("cam-1", DRIVER_ID),
        camera_path="/mnt/media/DCIM/B9996039.JPG",
        camera_name="B9996039.JPG",
        variant=variant,
        sha256=sha256,
        serial="SERIAL",
        product_id=10,
        size_bytes=123,
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
        serial="SERIAL", product_id=10, size_bytes=123,
    )
    key = file_identity_key("cam-1", DRIVER_ID, identity.file_identity)
    restored = CameraFileIdentity.from_source(key, identity.locator())

    assert host_name != restored.camera_name
    assert restored.camera_path == "/mnt/media/DCIM/B9996039.JPG"
    assert restored.camera_name == "B9996039.JPG"
    assert CameraFileLocator.from_json(locator.as_json()) == locator


def test_deferred_source_identity_round_trip_has_no_delivery_size_or_checksum():
    original = DeferredCameraFileIdentity(
        DeviceBinding("cam-1", DRIVER_ID), "17", "/mnt/media/DCIM/B9996039.JPG",
        "B9996039.JPG", DownloadVariant.ORIGINAL, "SERIAL", 10)
    preview = replace(original, variant=DownloadVariant.THUMBNAIL)
    next_activity = replace(original, activity_id="18")
    key = file_identity_key("cam-1", DRIVER_ID, original.file_identity)
    assert DeferredCameraFileIdentity.from_source(key, original.locator()) == original
    assert camera_file_identity_from_source(key, original.locator()) == original
    assert len({original.file_identity, preview.file_identity, next_activity.file_identity}) == 3
    assert "size_bytes" not in original.locator()
    assert "sha256" not in original.locator()


def test_deferred_photo_identities_derive_both_roles_from_one_capture():
    binding = DeviceBinding("cam-1", DRIVER_ID)
    observation = {
        "activity_id": "17",
        "camera_locator": {"status": "path", "camera_path": "/mnt/DCIM/B1.JPG", "camera_name": "B1.JPG"},
        "camera_identity": {"serial": "SERIAL", "product_id": 10},
    }
    original, preview = deferred_photo_identities(binding, observation)
    assert original.variant is DownloadVariant.ORIGINAL
    assert preview.variant is DownloadVariant.THUMBNAIL
    assert (original.binding, original.activity_id, original.camera_path, original.serial) == (
        binding, "17", "/mnt/DCIM/B1.JPG", "SERIAL")
    assert replace(original, variant=DownloadVariant.THUMBNAIL) == preview


@pytest.mark.parametrize("change", [
    {"activity_id": "0"},
    {"camera_locator": {"status": "explicit_missing", "camera_path": None, "camera_name": None}},
    {"camera_identity": {"serial": "SERIAL"}},
])
def test_deferred_photo_identity_rejects_incomplete_start_observation(change):
    observation = {
        "activity_id": "17",
        "camera_locator": {"status": "path", "camera_path": "/mnt/DCIM/B1.JPG", "camera_name": "B1.JPG"},
        "camera_identity": {"serial": "SERIAL", "product_id": 10},
    }
    with pytest.raises(ValueError):
        deferred_photo_identities(DeviceBinding("cam-1", DRIVER_ID), dict(observation, **change))


@pytest.mark.parametrize("change", [
    {"activity_id": "18"}, {"serial": "OTHER"}, {"product_id": 11},
    {"variant": "thumbnail"}, {"camera_name": "other.JPG"},
    {"size_bytes": 123}, {"sha256": "a" * 64},
])
def test_deferred_source_locator_cannot_redefine_original_identity(change):
    original = DeferredCameraFileIdentity(
        DeviceBinding("cam-1", DRIVER_ID), "17", "/mnt/media/DCIM/B9996039.JPG",
        "B9996039.JPG", DownloadVariant.ORIGINAL, "SERIAL", 10)
    with pytest.raises(ValueError):
        DeferredCameraFileIdentity.from_source(
            file_identity_key("cam-1", DRIVER_ID, original.file_identity),
            dict(original.locator(), **change))


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
    assert camera_file_identity_from_source(key, original.locator()) == original
    assert len({original.file_identity, preview.file_identity, replaced.file_identity}) == 3
    assert preview.locator()["camera_name"] == "B9996039.JPG"
    assert preview.locator()["camera_path"] == "/mnt/media/DCIM/B9996039.JPG"
    assert len({original.file_identity, replace(original, serial="OTHER").file_identity,
                replace(original, product_id=11).file_identity,
                replace(original, size_bytes=124).file_identity}) == 4


@pytest.mark.parametrize("change", [
    {"serial": ""}, {"serial": "bad\nserial"}, {"product_id": 0},
    {"product_id": True}, {"product_id": 65536}, {"size_bytes": 0},
    {"size_bytes": True},
])
def test_original_camera_and_length_must_be_valid(change):
    with pytest.raises(ValueError):
        replace(_identity(), **change)


def test_old_file_identity_without_camera_is_rejected():
    opaque = json.dumps(["hb-download-v2", "/mnt/media/B.JPG", "B.JPG", "original", "a" * 64])
    with pytest.raises(ValueError):
        CameraFileIdentity.from_source(file_identity_key("cam-1", DRIVER_ID, opaque), {})


def test_locator_boolean_is_not_the_fixed_numeric_size():
    identity = replace(_identity(), size_bytes=1)
    with pytest.raises(ValueError):
        CameraFileIdentity.from_source(
            file_identity_key("cam-1", DRIVER_ID, identity.file_identity),
            dict(identity.locator(), size_bytes=True),
        )


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
            serial="SERIAL", product_id=10, size_bytes=123,
        )


@pytest.mark.parametrize("change", [
    {"sha256": "b" * 64},
    {"variant": "thumbnail"},
    {"camera_name": "other.JPG"},
    {"camera_path": "/mnt/media/DCIM/other.JPG"},
    {"serial": "OTHER"},
    {"product_id": 11},
    {"size_bytes": 124},
    {"extra": True},
])
def test_locator_cannot_redefine_original_fixed_identity(change):
    identity = _identity()
    with pytest.raises(ValueError):
        CameraFileIdentity.from_source(
            file_identity_key("cam-1", DRIVER_ID, identity.file_identity),
            dict(identity.locator(), **change),
        )
