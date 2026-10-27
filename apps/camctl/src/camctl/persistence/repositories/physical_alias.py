"""读取其他逻辑设备尚未结束的 Hasselblad 责任。"""

from contextlib import closing

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.identity import CameraIdentity


def physical_alias_conflict(connection, device_id, identity):
    """检查活动限制、未结束活动调用和仍需要设备字节的读取责任。"""
    if not isinstance(identity, CameraIdentity):
        raise ConsistencyError("当前 Hasselblad 配置缺少固定机身身份")
    activity_queries = (
        "SELECT d.expected_identity_json,d.resource_key FROM device_activities d"
        " JOIN actions a ON a.id=d.action_id"
        " WHERE d.resource_key=? AND a.driver_id=? AND a.device_id<>? AND d.occupancy_state=1 LIMIT 1",
        "SELECT d.expected_identity_json,d.resource_key FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id"
        " JOIN device_activities d ON d.id=r.activity_id JOIN actions a ON a.id=d.action_id"
        " WHERE d.resource_key=? AND a.driver_id=? AND a.device_id<>? AND t.status=1 LIMIT 1",
    )
    for sql in activity_queries:
        with closing(connection.execute(sql, (identity.resource_key, DRIVER_ID, device_id))) as cursor:
            row = cursor.fetchone()
        if row is not None:
            raw, key = row
            try:
                fixed = CameraIdentity(**(parse_exact_json(raw) if isinstance(raw, str) else raw))
            except (TypeError, ValueError) as error:
                raise ConsistencyError("原活动缺少可靠固定机身身份") from error
            if key != fixed.resource_key:
                raise ConsistencyError("原活动资源键与固定机身身份不符")
            return True
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity
    select = (
        "SELECT d.expected_identity_json,d.resource_key,f.identity_key,f.locator_json,"
        "a.device_id,a.driver_id,o.device_id,o.driver_id FROM operation_runs r"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN device_files f ON f.id=c.source_device_file_id"
        " JOIN device_activities d ON d.action_id=f.source_action_id"
        " JOIN actions a ON a.id=f.observer_action_id"
        " JOIN actions o ON o.id=f.source_action_id"
    )
    read_queries = (
        select + " WHERE d.resource_key=? AND a.driver_id=? AND a.device_id<>?"
        " AND r.kind=3 AND r.status IN (1,2) AND c.committed_bytes<c.source_size LIMIT 1",
        select + " JOIN operation_attempts t ON t.run_id=r.id AND t.status=1"
        " WHERE d.resource_key=? AND a.driver_id=? AND a.device_id<>? AND r.kind=3 LIMIT 1",
    )
    for sql in read_queries:
        with closing(connection.execute(sql, (identity.resource_key, DRIVER_ID, device_id))) as cursor:
            row = cursor.fetchone()
        if row is not None:
            raw, key, source_key, locator, source_device, source_driver, origin_device, origin_driver = row
            try:
                fixed = CameraIdentity(**(parse_exact_json(raw) if isinstance(raw, str) else raw))
                source = CameraFileIdentity.from_source(
                    source_key, parse_exact_json(locator) if isinstance(locator, str) else locator)
            except (TypeError, ValueError) as error:
                raise ConsistencyError("设备读取缺少可靠固定机身身份") from error
            if (key != fixed.resource_key or (source.serial, source.product_id) != (fixed.serial, fixed.product_id)
                    or (source.binding.device_id, source.binding.driver_id) != (source_device, source_driver)
                    or (source_device, source_driver) != (origin_device, origin_driver)):
                raise ConsistencyError("设备读取来源与原活动机身或绑定不一致")
            return True
    return False


def action_physical_identity(connection, action_id):
    """从原活动取得固定机身，不从当前配置补齐历史事实。"""
    with closing(connection.execute(
        "SELECT expected_identity_json,resource_key FROM device_activities WHERE action_id=?",
        (action_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("原动作缺少固定机身活动")
    try:
        raw, key = row
        fixed = CameraIdentity(**(parse_exact_json(raw) if isinstance(raw, str) else raw))
    except (TypeError, ValueError) as error:
        raise ConsistencyError("原活动缺少可靠固定机身身份") from error
    if key != fixed.resource_key:
        raise ConsistencyError("原活动资源键与固定机身身份不符")
    return fixed


def source_physical_alias_conflict(connection, source, observer, origin):
    """核对设备文件原绑定与固定机身后检查其他逻辑设备的责任。"""
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity

    try:
        locator = source["locator_json"]
        file_identity = CameraFileIdentity.from_source(
            source["identity_key"], parse_exact_json(locator) if isinstance(locator, str) else locator)
        binding = (observer["device_id"], observer["driver_id"])
        if ((origin["device_id"], origin["driver_id"]) != binding
                or (file_identity.binding.device_id, file_identity.binding.driver_id) != binding):
            raise ValueError("来源绑定不一致")
        fixed = action_physical_identity(connection, source["source_action_id"])
        if (file_identity.serial, file_identity.product_id) != (fixed.serial, fixed.product_id):
            raise ValueError("来源机身与原活动不一致")
    except (TypeError, ValueError, KeyError) as error:
        raise ConsistencyError("设备文件缺少可靠原绑定或机身身份") from error
    return physical_alias_conflict(connection, binding[0], fixed)
