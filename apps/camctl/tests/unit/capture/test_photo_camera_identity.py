import pytest

from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.persistence.repositories.capture_facts import _require_photo_camera


@pytest.mark.parametrize("camera,resource,valid", [
    ({"serial": "CAMERA-1", "product_id": 10}, CameraIdentity("CAMERA-1", 10).resource_key, True),
    ({"serial": "CAMERA-2", "product_id": 10}, CameraIdentity("CAMERA-1", 10).resource_key, False),
    ({"serial": "CAMERA-1", "product_id": 11}, CameraIdentity("CAMERA-1", 10).resource_key, False),
    ({"serial": "CAMERA-1", "product_id": 10}, CameraIdentity("CAMERA-2", 10).resource_key, False),
])
def test_photo_identity_must_match_fixed_activity(camera, resource, valid):
    activity = {"expected_identity_json": {"serial": "CAMERA-1", "product_id": 10},
                "resource_key": resource}
    if valid:
        _require_photo_camera(camera, activity)
    else:
        with pytest.raises(ValueError):
            _require_photo_camera(camera, activity)
