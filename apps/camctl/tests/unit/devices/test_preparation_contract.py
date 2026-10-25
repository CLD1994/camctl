"""相机共用准备凭据的身份、参数快照及会话边界。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.devices.capture_preparation import (
    CAPTURE_ACTION_TYPES,
    CAPTURE_OPERATIONS,
    DEFAULT_PREPARE_LEAD_S,
    PreparedCaptureCache,
    capture_operation,
)
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket


def _preparation(device="camera"):
    return ControlRequest(
        "take_photo", DeviceBinding(device, "hasselblad_cfv100c"),
        {"focus": {"mode": "MF", "relative_move": 5}},
        ticket=AttemptTicket(1, "control", "7", "prepare/7/session", 11),
    )


def _start(preparation):
    return replace(preparation, ticket=AttemptTicket(1, "control", "7", "start/7", 12),
                   prepared_by=preparation.ticket)


@pytest.mark.parametrize("action,operation", [
    ("CAMERA_TAKE_PHOTO", "take_photo"),
    ("CAMERA_RECORD", "start_recording"),
    ("CAMERA_TIMELAPSE", "start_timelapse"),
])
def test_control_operation_comes_from_capture_action_contract(action, operation):
    action_type = int(getattr(enum_for("actions.type"), action))
    assert capture_operation(action_type) == operation
    assert action_type in CAPTURE_ACTION_TYPES
    assert DEFAULT_PREPARE_LEAD_S == Decimal("10")
    with pytest.raises(TypeError):
        CAPTURE_OPERATIONS[action_type] = "other"


def test_unknown_action_has_no_capture_operation():
    with pytest.raises(ValueError):
        capture_operation(0)


@pytest.mark.parametrize("changed", [
    "binding", "serial", "route", "activity", "operation", "params", "bool", "ticket", "missing_ticket",
])
def test_prepared_capture_requires_complete_original_identity(changed):
    cache = PreparedCaptureCache()
    preparation = _preparation()
    if changed == "bool":
        preparation = replace(preparation, params={"focus": {"mode": "MF", "relative_move": 1}})
    serial = ("serial", 10) if changed == "route" else "serial"
    cache.remember(preparation, serial)
    start = _start(preparation)
    assert cache.matches(start, serial)
    if changed == "binding":
        start = replace(start, binding=DeviceBinding("camera", "other"))
    elif changed == "serial":
        serial = "different"
    elif changed == "route":
        serial = ("serial", 11)
    elif changed == "activity":
        start = replace(start, ticket=replace(start.ticket, target_id="8"))
    elif changed == "operation":
        start = replace(start, operation="start_recording")
    elif changed in ("params", "bool"):
        start = replace(start, params={"focus": {"mode": "MF", "relative_move":
                                                 6 if changed == "params" else True}})
    elif changed == "ticket":
        start = replace(start, prepared_by=replace(start.prepared_by, run_id=99))
    else:
        start = replace(start, ticket=None)
    assert not cache.matches(start, serial)


def test_parameter_snapshot_does_not_follow_later_input_mutation():
    cache = PreparedCaptureCache()
    preparation = _preparation()
    cache.remember(preparation, "serial")
    preparation.params["focus"]["relative_move"] = -5
    assert not cache.matches(_start(preparation), "serial")
    original = replace(_start(preparation), params={"focus": {"mode": "MF", "relative_move": 5}})
    assert cache.matches(original, "serial")


def test_device_replacement_and_session_clear_drop_only_matching_ownership():
    cache = PreparedCaptureCache()
    first, other = _preparation(), _preparation("other")
    cache.remember(first, "serial")
    cache.remember(other, "other-serial")
    replacement = replace(first, ticket=replace(first.ticket, run_id=13))
    cache.remember(replacement, "serial")
    assert len(cache) == 2
    assert not cache.matches(_start(first), "serial")
    assert cache.matches(_start(replacement), "serial")
    cache.clear(first.binding)
    assert not cache.matches(_start(replacement), "serial")
    assert cache.matches(_start(other), "other-serial")
    cache.clear()
    assert not cache.matches(_start(other), "other-serial")
