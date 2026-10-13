"""统一的内置相机静态定义和运行驱动登记入口。"""

from __future__ import annotations

__all__ = ["register_builtin_drivers"]


def register_builtin_drivers(config) -> None:
    """按同一部署快照登记 ADB 与 Hasselblad 内置相机。"""
    from camctl.devices.drivers.adb_cameras.registration import (
        register_builtin_camera_drivers,
    )
    from camctl.devices.drivers.hasselblad.registration import (
        register_builtin_hasselblad_driver,
    )

    register_builtin_camera_drivers(config)
    register_builtin_hasselblad_driver(config)
