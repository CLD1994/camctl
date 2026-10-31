"""绑定读取取消引用的 UTC 时刻遵守有符号整数契约。"""

from unittest.mock import patch
from types import ModuleType
import sys

import pytest

from camctl import resources
from camctl.contracts import enums

photo = ModuleType("camctl.history.host_photo_request")
photo.HostPhotoRequestError = type("HostPhotoRequestError", (ValueError,), {})
photo.parse_host_photo_request = lambda *args: None
photo.validate_host_photo_event_structure = lambda *args: None
with (patch.object(resources, "resource_bytes", side_effect=AssertionError("禁止读取真实包资源")),
      patch.object(enums, "resource_bytes", side_effect=AssertionError("禁止读取真实包资源")),
      patch.dict(sys.modules, {photo.__name__: photo})):
    from camctl.history.validators import EventValidationError, _check_evidence_member


@pytest.mark.parametrize("occurred_at", [-100, -1, 0, 1])
def test_binding_read_cancel_accepts_signed_utc(occurred_at):
    evidence = {"error": {"code": "device_binding_unavailable"}, "current_identity": None, "cancel_event": {
        "event_id": 1, "transaction_id": 2, "occurred_at": occurred_at}}
    _check_evidence_member(1, "binding_read_finish", evidence, 3)


@pytest.mark.parametrize("occurred_at", [True, 1.5, "-1", None])
def test_binding_read_cancel_rejects_non_integer_utc(occurred_at):
    evidence = {"error": {"code": "device_binding_unavailable"}, "current_identity": None, "cancel_event": {
        "event_id": 1, "transaction_id": 2, "occurred_at": occurred_at}}
    with pytest.raises(EventValidationError):
        _check_evidence_member(1, "binding_read_finish", evidence, 3)


def test_binding_read_identity_belongs_only_to_physical_mismatch(monkeypatch):
    identity_module = ModuleType("camctl.devices.drivers.hasselblad.identity")
    class CameraIdentity:
        def __init__(self, *, serial):
            if not serial:
                raise ValueError("missing serial")
    identity_module.CameraIdentity = CameraIdentity
    monkeypatch.setitem(sys.modules, identity_module.__name__, identity_module)
    evidence = {"error": {"code": "device_binding_unavailable", "details": {
        "reason": "physical_identity_mismatch"}}, "cancel_event": None,
        "current_identity": {"serial": "observed"}}
    _check_evidence_member(1, "binding_read_finish", evidence, 3)
    evidence["current_identity"] = None
    with pytest.raises(EventValidationError):
        _check_evidence_member(1, "binding_read_finish", evidence, 3)
    evidence["current_identity"] = {"serial": "observed"}
    evidence["error"]["details"]["reason"] = "missing"
    with pytest.raises(EventValidationError):
        _check_evidence_member(1, "binding_read_finish", evidence, 3)
