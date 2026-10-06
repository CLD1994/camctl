"""已部署驱动定义的进程启动登记点。

与驱动端口登记（devices.drivers.runtime）同属部署装配：第一版
没有内置厂商映射，部署适配（或集成测试的受约束替身）在进程启动
阶段把驱动定义与运行端口一起登记。受理校验与 describe 导出共用
同一份登记结果；声明了未登记驱动定义的设备在目录构建时报部署错
误。正式厂商接入时，内置映射与进程登记必须保持一致。
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
