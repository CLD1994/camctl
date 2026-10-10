"""无设备 IO 的内置相机登记；静态与运行定义来自同一契约。"""

import time

from camctl.acceptance.schema import RuleError
from camctl.devices.catalog import DriverDefinition
from camctl.devices.definitions_runtime import current_driver_definitions, register_driver_definitions
from camctl.devices.drivers.registry import DriverEntry
from camctl.devices.drivers.runtime import current_registry, register_drivers
from .commands import CameraModel
from .contracts import CameraContract
from .driver import AdbCameraDriver
from .transport import AdbTransport


# 真实响应、结束和文件工具契约未补齐的相机只保留候选定义。
_BUILTIN_CONTRACTS = tuple(CameraContract(model) for model in CameraModel)


def builtin_camera_contracts():
    return _BUILTIN_CONTRACTS


def register_builtin_camera_drivers(config):
    """验证全部身份后登记；重复同源装配更新本次配置绑定。"""
    definitions = current_driver_definitions().drivers
    registry = current_registry()
    pending, refresh, seen = [], [], set()
    for contract in builtin_camera_contracts():
        driver_id = contract.driver_id
        if driver_id in seen:
            raise RuleError(f"内置相机契约重复: {driver_id!r}")
        seen.add(driver_id)
        definition = DriverDefinition(driver_id, {
            capability.action_type: (capability,) for capability in contract.capabilities()})
        previous_definition = definitions.get(driver_id)
        previous = registry.entry(driver_id)
        if previous_definition is not None or previous is not None:
            if (previous_definition != definition or previous is None
                    or not isinstance(previous.driver, AdbCameraDriver)
                    or previous.driver.contract is not contract):
                raise RuleError(f"内置相机身份与既有登记冲突: {driver_id!r}")
            refresh.append(previous.driver)
            continue
        driver = AdbCameraDriver(contract, config.devices, AdbTransport(),
                                 terminate_grace_s=config.adb.terminate_grace_s,
                                 monotonic_ns=time.monotonic_ns)
        pending.append((definition, DriverEntry(driver_id, driver, driver.declaration,
                                                contract.evidence, contract.status)))
    # 上面的纯检查完成后才修改两个登记点，不产生单边登记。
    register_driver_definitions(*(definition for definition, _ in pending))
    register_drivers(*(entry for _, entry in pending))
    for driver in refresh:
        driver.devices = config.devices
        driver.terminate_grace_s = config.adb.terminate_grace_s
