"""读取其他逻辑设备持有的 Hasselblad 活动限制。"""

from contextlib import closing

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.identity import CameraIdentity


def held_activity_alias_conflict(connection, device_id, identity):
    """按固定机身查找其他逻辑设备的 HELD 活动，不推断实际调用状态。"""
    if not isinstance(identity, CameraIdentity):
        raise ConsistencyError("当前 Hasselblad 配置缺少固定机身身份")
    with closing(connection.execute(
        "SELECT d.expected_identity_json,d.resource_key FROM device_activities d"
        " JOIN actions a ON a.id=d.action_id"
        " WHERE a.driver_id=? AND a.device_id<>? AND d.occupancy_state=1"
        " ORDER BY d.id", (DRIVER_ID, device_id),
    )) as cursor:
        for raw, key in cursor:
            try:
                fixed = CameraIdentity(**(parse_exact_json(raw) if isinstance(raw, str) else raw))
            except (TypeError, ValueError) as error:
                raise ConsistencyError("原活动缺少可靠固定机身身份") from error
            if key != fixed.resource_key:
                raise ConsistencyError("原活动资源键与固定机身身份不符")
            if key == identity.resource_key:
                return True
    return False
