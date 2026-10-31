"""读取前对照本次配置与原设备文件的机身身份。"""

from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding


async def saved_source_binding(runtime, source_id, action_id, device_id, driver_id):
    """恢复原读取检查已经保存的绑定失败，保持原次配置身份。"""
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if driver_id != DRIVER_ID:
        return None
    from camctl.persistence.transaction import row_facts
    from camctl.persistence.repositories.physical_alias import source_physical_identity
    from camctl.persistence.repositories.availability_wait_finish import saved_binding_check_failure
    def read(owned):
        source = row_facts(owned.connection, "device_files", source_id)
        if source is None:
            raise ConsistencyError("原读取检查缺少设备来源")
        observer = row_facts(owned.connection, "actions", source["observer_action_id"])
        origin = row_facts(owned.connection, "actions", source["source_action_id"])
        if observer is None or origin is None:
            raise ConsistencyError("原读取检查缺少来源动作")
        fixed = source_physical_identity(owned.connection, source, observer, origin)
        saved = saved_binding_check_failure(owned.connection, action_id, resource_key=fixed.resource_key,
                                             expected_identity=fixed)
        if saved is None:
            return None
        details, current = saved
        if (details.get("device_id"), details.get("expected_driver_id")) != (device_id, driver_id):
            raise ConsistencyError("原读取检查绑定与来源不同")
        statuses = {"missing": BindingStatus.DEVICE_MISSING, "mismatch": BindingStatus.DRIVER_MISMATCH,
                    "physical_identity_mismatch": BindingStatus.PHYSICAL_IDENTITY_MISMATCH,
                    "physical_identity_conflict": BindingStatus.PHYSICAL_IDENTITY_CONFLICT}
        status = statuses.get(details.get("reason"))
        if status is None:
            raise ConsistencyError("原读取检查绑定失败原因无效")
        return BindingResult(status, DeviceBinding(device_id, driver_id), details.get("actual_driver_id"),
                             current if status is BindingStatus.PHYSICAL_IDENTITY_MISMATCH else None)
    return await runtime.database.read(read, description="读取原设备检查绑定失败")


async def source_physical_binding(runtime, source_id, device_id, driver_id):
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if driver_id != DRIVER_ID:
        return None
    assembly = runtime.devices.get(device_id)
    if assembly is None:
        raise ConsistencyError("原 Hasselblad 读取缺少设备装配")
    from camctl.devices.drivers.hasselblad.configuration import read_camera_config
    from camctl.persistence.repositories.physical_alias import (source_physical_identity,
        physical_alias_conflict, verify_source_identity_mismatch)
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.transaction import row_facts
    config = read_camera_config(assembly.driver.devices[device_id])
    def read_alias(owned):
        source = row_facts(owned.connection, "device_files", source_id)
        if source is None:
            raise ConsistencyError("读取机身冲突缺少原设备文件")
        observer = row_facts(owned.connection, "actions", source["observer_action_id"])
        origin = row_facts(owned.connection, "actions", source["source_action_id"])
        if observer is None or origin is None:
            raise ConsistencyError("读取机身冲突缺少原动作绑定")
        fixed = source_physical_identity(owned.connection, source, observer, origin)
        current = CameraIdentity(config.serial, config.product_id)
        binding = DeviceBinding(device_id, driver_id)
        if verify_source_identity_mismatch(owned.connection, source, observer, origin,
                                           {"serial": current.serial, "product_id": current.product_id}):
            return BindingResult(BindingStatus.PHYSICAL_IDENTITY_MISMATCH, binding, None,
                                 {"serial": current.serial, "product_id": current.product_id})
        if physical_alias_conflict(owned.connection, device_id, fixed):
            return BindingResult(BindingStatus.PHYSICAL_IDENTITY_CONFLICT, binding, None)
        return None
    return await runtime.database.read(read_alias, description="读取设备文件的机身责任")
