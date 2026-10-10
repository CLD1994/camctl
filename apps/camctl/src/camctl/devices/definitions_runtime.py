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
    for definition in definitions:
        if definition.driver_id in _definitions:
            raise ValueError(f"驱动定义重复登记: {definition.driver_id!r}")
        _definitions[definition.driver_id] = definition


def current_driver_definitions() -> "DriverDefinitions":
    """登记项的即时快照；调用方不因持有快照看到后续变化。"""
    from camctl.devices.catalog import DriverDefinitions

    return DriverDefinitions(drivers=dict(_definitions))


def reset_driver_definitions() -> None:
    """清空登记；仅用于测试隔离。"""
    _definitions.clear()
