"""可用性检查目标的固定活动事实。"""

from collections.abc import Mapping
from contextlib import closing

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ObjectId
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.start_entry import is_hasselblad_photo


def iter_held_activity_ids(connection, resource_key: str, *, batch_size: int = 128):
    """按固定机身资源键有界枚举 HELD 活动；读取期间须保持原写事务。"""
    if not isinstance(resource_key, str) or not resource_key:
        raise ValueError("设备资源键必须是非空字符串")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("活动读取每批必须为 1 至 128 项")
    held = int(enum_for("device_activities.occupancy_state").HELD)
    after = 0
    while True:
        with closing(connection.execute(
            "SELECT id FROM device_activities WHERE resource_key=? AND occupancy_state=?"
            " AND id>? ORDER BY id LIMIT ?", (resource_key, held, after, batch_size),
        )) as cursor:
            page = tuple(cursor.fetchmany(batch_size))
        if not page:
            return
        ids = tuple(ObjectId(row[0]) for row in page)
        if any(left >= right for left, right in zip((after, *ids), ids)):
            raise ValueError("活动读取页不是严格递增的有效身份")
        yield ids
        after = ids[-1]
        if len(page) < batch_size:
            return


def iter_logical_held_activities(connection, device_id: str, *, batch_size: int = 128):
    """按原动作身份有界枚举逻辑设备的 HELD 活动，包括终态动作。"""
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("逻辑设备身份必须是非空字符串")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("活动读取每批必须为 1 至 128 项")
    held = int(enum_for("device_activities.occupancy_state").HELD)
    after = 0
    while True:
        with closing(connection.execute(
            "SELECT a.id,d.id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE a.device_id=? AND a.id>? AND d.occupancy_state=? ORDER BY a.id LIMIT ?",
            (device_id, after, held, batch_size),
        )) as cursor:
            page = tuple(cursor.fetchmany(batch_size))
        if not page:
            return
        pairs = tuple((ObjectId(row[0]), ObjectId(row[1])) for row in page)
        ids = tuple(pair[0] for pair in pairs)
        if any(left >= right for left, right in zip((after, *ids), ids)):
            raise ValueError("动作读取页不是严格递增的有效身份")
        yield pairs
        after = ids[-1]
        if len(page) < batch_size:
            return


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
