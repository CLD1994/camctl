"""进程内驱动登记点：部署装配取得运行端口的唯一来源。

第一版没有内置厂商映射；部署适配（或集成测试的受约束替身）在进程
启动阶段登记驱动接入项，会话装配经 current_registry 按设备声明解析
端口。登记与静态能力目录（devices/catalog）是两个来源：前者提供运
行端口与证据契约，后者提供参数规则，正式厂商接入时须保证二者对同
一驱动一致。重复登记同一 driver_id 属于装配错误；reset_drivers 只
供测试隔离进程状态使用。
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
