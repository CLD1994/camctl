from decimal import Decimal

import pytest

from camctl.capture.handlers import _start_call_timeout
from camctl.capture.host_photo import is_saved_host_photo
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError


_PHOTO = int(enum_for("actions.type").CAMERA_TAKE_PHOTO)


def test_device_photo_start_uses_the_saved_complete_deadline():
    action = {"type": _PHOTO,
              "execution_spec_json": {"output_storage": "device", "start_call_timeout_s": Decimal("170.25")}}
    assert _start_call_timeout(action) == Decimal("170.25")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["host", "device"])
async def test_photo_waits_for_settings_preparation_before_creating_file_responsibilities(mocker, storage):
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import create_autospec

    from camctl.capture.baseline import BaselinePreparation, PreparationPhase
    from camctl.capture.handlers import CaptureRuntime, HandlerOutcome
    from camctl.capture.host_photo import advance_host_photo
    from camctl.persistence.repositories.host_capture import HostCaptureRepository
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.scheduling.rules import LaunchWindow

    action = {"id": 1, "type": _PHOTO, "driver_id": "hasselblad_cfv100c", "status": 2,
        "scheduled_at": 10, "max_delay_ms": 1000, "cancel_requested": 0,
        "execution_spec_json": {"output_storage": storage, "start_call_timeout_s": Decimal(180)}}
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.action.return_value = action
    runtime.owned = SimpleNamespace(connection=create_autospec(sqlite3.Connection, instance=True))
    runtime.host_capture = create_autospec(HostCaptureRepository, instance=True)
    runtime.host_capture.prepare_host_photo.return_value = DbOutcome(DbOutcomeKind.COMPLETED, object())
    runtime.pending_host_photo_prepares = {}
    runtime.roots = object()
    runtime.capture_preparation_supported = True
    runtime.wall_us = create_autospec(lambda: 0, return_value=10)
    runtime.window_of = create_autospec(lambda action: None,
        return_value=LaunchWindow(10, 1_000_010))
    mocker.patch("camctl.capture.host_photo._saved_start", autospec=True, return_value=None)
    mocker.patch("camctl.capture.handlers._handle_binding_failure", autospec=True, return_value=False)
    prepare = mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True,
        return_value=BaselinePreparation(PreparationPhase.WAITING))
    control = mocker.patch("camctl.capture.handlers._control_call", autospec=True,
        return_value=HandlerOutcome("not_granted"))

    await advance_host_photo(runtime, 1)

    prepare.assert_awaited_once_with(runtime, 1)
    runtime.host_capture.prepare_host_photo.assert_not_called()
    control.assert_not_called()
