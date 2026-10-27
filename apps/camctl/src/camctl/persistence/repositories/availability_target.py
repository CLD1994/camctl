"""可用性检查目标的固定活动事实。"""

from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ObjectId
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.start_entry import is_hasselblad_photo


def require_target_facts(check: Mapping, activity: Mapping, action: Mapping) -> None:
    """核对一项仍持有限制的旧活动，不判断调用收场或目标集合完整性。"""
    try:
        ObjectId(check["action_id"])
        ObjectId(activity["action_id"])
        ObjectId(action["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("检查与旧活动缺少有效动作身份") from error
    if (any(type(action.get(name)) is not int for name in ("type", "status", "execution_started"))
            or any(type(activity.get(name)) is not int for name in
                   ("activity_state", "occupancy_state", "ownership_mode", "dispatch_state"))):
        raise ValueError("旧活动状态必须是有效整数")
    action_status = enum_for("actions.status")
    activity_state = enum_for("device_activities.activity_state")
    occupancy = enum_for("device_activities.occupancy_state")
    ownership = enum_for("device_activities.ownership_mode")
    dispatch = enum_for("device_activities.dispatch_state")
    if (not is_hasselblad_photo(action)
            or action.get("status") != int(action_status.FAILED)
            or action.get("execution_started") != 1
            or action.get("id") == check.get("action_id")
            or activity.get("action_id") != action.get("id")
            or activity.get("activity_state") != int(activity_state.UNKNOWN)
            or activity.get("occupancy_state") != int(occupancy.HELD)
            or activity.get("ownership_mode") != int(ownership.TASK_SCOPE)
            or activity.get("dispatch_state") not in (
                int(dispatch.MAY_HAVE_DISPATCHED), int(dispatch.SUCCESS_RETURNED))
            or activity.get("availability_release_attempt_id") is not None
            or action.get("device_id") != check.get("device_id")
            or action.get("driver_id") != check.get("driver_id")):
        raise ValueError("旧活动不符合可用性检查目标的固定条件")
    try:
        identity = CameraIdentity(**check["expected_identity_json"])
        original = CameraIdentity(**activity["expected_identity_json"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("检查或旧活动缺少有效固定机身身份") from error
    if (not json_equal(check["expected_identity_json"], activity["expected_identity_json"])
            or original != identity
            or check.get("resource_key") != identity.resource_key
            or activity.get("resource_key") != identity.resource_key):
        raise ValueError("检查与旧活动的固定机身身份不一致")
