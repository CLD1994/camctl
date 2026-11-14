"""A 系列单元测试共用的构造帮助与静态目录替身。"""

from __future__ import annotations

from decimal import Decimal

from camctl.acceptance.ports import ParameterDefinition

CAMERA_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "single_shot"},
        "shots": {"type": "integer", "minimum": 1, "maximum": 10},
        "interval_s": {"type": "number", "multipleOf": Decimal("0.1")},
    },
    "required": ["type"],
    "additionalProperties": False,
}

CAMERA_DEFAULTS = {"shots": 1, "interval_s": Decimal("0.5")}


class StubCatalog:
    """受静态目录端口约束的替身：两台相机设备与同一参数定义。"""

    def __init__(self) -> None:
        self.devices = {"cam-1", "cam-2"}

    def action_types(self) -> frozenset[str]:
        return frozenset(
            {"camera_take_photo", "camera_record", "camera_timelapse"}
        ) | {
            "obtain_action_outputs",
            "delete_action_outputs",
            "cancel_task",
            "report_status",
        }

    def device_exists(self, device_id: str) -> bool:
        return device_id in self.devices

    def driver_id(self, device_id: str) -> str | None:
        return "camctl-adb" if device_id in self.devices else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type.startswith("camera_")

    def parameter_definition(self, device_id: str, action_type: str, parameter_type: str):
        if device_id not in self.devices or not action_type.startswith("camera_"):
            return None
        return ParameterDefinition(schema=CAMERA_DEFINITION, defaults=dict(CAMERA_DEFAULTS), preview_supported=True)


def camera_action(name: str = "shoot", *, device: str = "cam-1", group: str | None = None) -> dict:
    action = {
        "name": name,
        "type": "camera_take_photo",
        "device_id": device,
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 1000},
    }
    if group is not None:
        action["group"] = group
    return action


def manual_obtain(name: str, source_name: str | None) -> dict:
    params = (
        {"source": {"action_name": source_name}, "purpose": "manual"}
        if source_name is not None
        else {"source": {"group": "any"}, "purpose": "manual"}
    )
    return {
        "name": name,
        "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:30:00",
        "params": params,
    }


def auto_preview(name: str, source_name: str) -> dict:
    return {
        "name": name,
        "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {
            "source": {"action_name": source_name},
            "filter": "preview",
            "purpose": "auto_preview",
        },
    }


def admitted_action_row(action_type="camera_take_photo", params=None):
    """构造符合已受理动作契约的内存事实，不读取业务存储。"""
    from camctl.contracts.enums import enum_for

    camera = action_type.startswith("camera_")
    if params is None:
        params = {"type": "single_shot"}
    spec = {"target_duration_ms": 1234, "video_result_mode": "single_file"} if action_type == "camera_record" else {}
    if action_type == "obtain_action_outputs":
        spec = {"selection_mode": 1}
    return {
        "id": 1, "plan_id": 1, "input_index": 0, "name": "shoot" if camera else "control",
        "type": int(enum_for("actions.type")[action_type.upper()]),
        "device_id": "cam-1" if camera else None, "scheduled_at": 1768467600000000,
        "group_name": None, "input_fields_json": {"params": params, **({"policy": {"max_delay_ms": 1000}} if camera else {})},
        "effective_params_json": {"type": "single_shot"} if camera else None,
        "driver_id": "camctl-adb" if camera else None, "max_delay_ms": 1000 if camera else None,
        "execution_spec_json": spec, "status": 1, "execution_started": 0, "cancel_requested": 0,
        "error_code": None, "error_details_json": None, "first_window_observed_at": None,
        "expiration_reason": None, "source_resolution_state": 2 if action_type == "obtain_action_outputs" else None,
        "resolved_source_plan_id": 1 if action_type == "obtain_action_outputs" else None,
        "target_selection_state": 1 if action_type in {"delete_action_outputs", "cancel_task"} else None, "created_event_id": 1, "last_event_id": 1, "change_count": 1,
    }
