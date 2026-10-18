"""已部署驱动定义的进程启动登记点。

与驱动端口登记（devices.drivers.runtime）同属部署装配。内置相机
装配及其他部署适配在进程启动阶段把静态定义与运行端口一起登记。
受理校验与 describe 导出共用同一份登记结果；声明了未登记驱动的
设备在目录构建时报部署错误。候选过滤由目录按完整任务契约执行。
"""

from __future__ import annotations

from camctl.devices.catalog import DriverDefinition

__all__ = [
    "current_driver_definitions",
    "register_driver_definitions",
    "reset_driver_definitions",
]

_definitions: dict[str, DriverDefinition] = {}


def register_driver_definitions(*definitions: DriverDefinition) -> None:
    """登记已部署驱动定义；重复身份拒绝，不产生部分登记。"""
    seen: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, DriverDefinition):
            raise TypeError("登记项必须是 DriverDefinition")
        driver_id = definition.driver_id
        if not isinstance(driver_id, str) or not driver_id:
            raise ValueError(f"驱动定义身份必须是非空字符串: {driver_id!r}")
        if driver_id in seen or driver_id in _definitions:
            raise ValueError(f"驱动定义重复登记: {definition.driver_id!r}")
        seen.add(driver_id)

    from camctl.devices.drivers.runtime import current_registry

    registry = current_registry()
    for definition in definitions:
        entry = registry.entry(definition.driver_id)
        if entry is not None and entry.declaration != definition.declaration:
            raise ValueError(f"驱动定义与运行登记的声明冲突: {definition.driver_id!r}")

    _definitions.update((definition.driver_id, definition) for definition in definitions)


def current_driver_definitions() -> "DriverDefinitions":
    """登记项的即时快照；调用方不因持有快照看到后续变化。"""
    from camctl.devices.catalog import DriverDefinitions

    return DriverDefinitions(drivers=dict(_definitions))


def reset_driver_definitions() -> None:
    """清空登记；仅用于测试隔离。"""
    _definitions.clear()
