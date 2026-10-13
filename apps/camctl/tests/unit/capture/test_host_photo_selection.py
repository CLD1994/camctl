from decimal import Decimal

import pytest

from camctl.capture.handlers import _start_call_timeout
from camctl.capture.host_photo import is_saved_host_photo
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError


_PHOTO = int(enum_for("actions.type").CAMERA_TAKE_PHOTO)


def test_host_photo_is_selected_only_by_saved_execution_spec():
    hasselblad = {
        "type": _PHOTO,
        "driver_id": "hasselblad_cfv100c",
        "execution_spec_json": {},
    }
    assert not is_saved_host_photo(hasselblad)
    assert _start_call_timeout(hasselblad) == Decimal("30")

    saved_host_photo = {
        **hasselblad,
        "execution_spec_json": {
            "output_storage": "host",
            "start_call_timeout_s": Decimal("180"),
        },
    }
    assert is_saved_host_photo(saved_host_photo)
    assert _start_call_timeout(saved_host_photo) == Decimal("180")


def test_host_photo_timeout_comes_from_the_persisted_spec_for_any_driver():
    action = {
        "type": _PHOTO,
        "driver_id": "unrelated_driver",
        "execution_spec_json": {
            "output_storage": "host",
            "start_call_timeout_s": 240,
        },
    }
    assert is_saved_host_photo(action)
    assert _start_call_timeout(action) == Decimal("240")


@pytest.mark.parametrize("value", [None, True, 0, -1, "180"])
def test_host_photo_requires_a_valid_saved_deadline(value):
    action = {
        "type": _PHOTO,
        "execution_spec_json": {
            "output_storage": "host",
            "start_call_timeout_s": value,
        },
    }
    with pytest.raises(ConsistencyError, match="固定调用期限"):
        _start_call_timeout(action)
