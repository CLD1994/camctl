"""进程内驱动登记点：部署装配取得运行端口的唯一来源。

正常 CLI 及默认会话装配先按内置相机契约登记静态定义和运行端口；
尚缺真实契约的部分保持候选。部署适配也可显式登记其他驱动，会话
经 current_registry 按原设备声明解析端口。静态登记提供参数规则，
运行登记提供端口与证据；具体驱动的同源装配保证二者一致。冲突的
重复身份属于装配错误；reset_drivers 只供测试隔离进程状态使用。
"""

from __future__ import annotations

from typing import Any

from camctl.devices.drivers.registry import DriverEntry, DriverRegistry

__all__ = [
    "current_registry",
    "register_drivers",
    "reset_drivers",
]

_entries: dict[str, DriverEntry] = {}


def register_drivers(*entries: DriverEntry) -> None:
    """登记本进程可用的驱动接入项；重复 driver_id 明确拒绝。"""
    for entry in entries:
        if entry.driver_id in _entries and _entries[entry.driver_id] is not entry:
            raise ValueError(f"驱动重复登记: {entry.driver_id!r}")
        _entries[entry.driver_id] = entry


def current_registry() -> DriverRegistry:
    """按当前登记构造注册表快照；未登记时为空注册表。"""
    return DriverRegistry(tuple(_entries.values()))


def reset_drivers(*keep: Any) -> None:
    """清空进程登记；仅测试隔离使用。"""
    _entries.clear()
