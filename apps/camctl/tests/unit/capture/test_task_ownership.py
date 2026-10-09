"""固定定义保存驱动归属声明，运行基准另行保存。"""
from dataclasses import replace

import pytest

from camctl.capture.models import activity_capabilities, build_capture_spec, validate_capture_spec
from camctl.contracts.enums import enum_for
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn

OWNERSHIP = enum_for("device_activities.ownership_mode")


def task(action_type="camera_record"):
    source = CaptureTask(action_type, target_duration_s=10, stop_supported=True,
                         ownership_mode=OWNERSHIP.BASELINE_COMPARISON,
                         output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]})
    if action_type == "camera_timelapse":
        source = replace(source, duration_based=True, wait_after_send=True,
                         end_control=EndControl.DEVICE, start_return_meaning=StartReturn.SENT,
                         completion_mode=CompletionMode.TIME_AND_OUTPUTS, result_wait_margin_s=0)
    return source


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_fixed_definition_preserves_ownership_and_nonempty_scope(action_type):
    source = task(action_type)
    spec = build_capture_spec(action_type, source)
    assert spec["target_duration_ms"] == 10000
    assert spec["ownership_mode"] == 2
    assert spec["output_scope"] == {"directories": ["/mnt/media_rw/emulated/DCIM"]}
    assert "baseline" not in spec
    capabilities = activity_capabilities(action_type, spec)
    assert capabilities.ownership_mode == 2
    assert capabilities.output_scope_json == spec["output_scope"]


def test_fixed_definition_does_not_share_mutable_driver_scope():
    source = task()
    spec = build_capture_spec("camera_record", source)
    source.output_scope["directories"].append("/other")
    assert spec["output_scope"] == {"directories": ["/mnt/media_rw/emulated/DCIM"]}


@pytest.mark.parametrize("ownership, scope", [
    (None, {"prefix": "task"}), (2, None), (2, {}), (2, []), (True, {"prefix": "task"}),
    (3, {"prefix": "task"}), (2, {"prefix": object()}), (2, {1: "task"}),
])
def test_invalid_or_incomplete_ownership_declaration_is_rejected(ownership, scope):
    with pytest.raises(ValueError):
        build_capture_spec("camera_record", replace(task(), ownership_mode=ownership, output_scope=scope))


@pytest.mark.parametrize("changes", [
    {"ownership_mode": 2}, {"output_scope": {"prefix": "task"}},
    {"ownership_mode": True, "output_scope": {"prefix": "task"}},
    {"ownership_mode": 2, "output_scope": {}},
    {"ownership_mode": 2, "output_scope": {"prefix": "task"}, "baseline_entries": []},
])
def test_saved_definition_validates_same_declaration_and_rejects_runtime_facts(changes):
    with pytest.raises(ValueError):
        validate_capture_spec("camera_record", {"target_duration_ms": 10000, **changes})
