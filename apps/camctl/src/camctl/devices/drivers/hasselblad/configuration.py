"""Hasselblad 本地相机配置的纯解析和校验。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from unicodedata import category

from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID

__all__ = ["CameraConfig", "read_camera_config"]


@dataclass(frozen=True)
class CameraConfig:
    executable: str
    serial: str
    product_id: int = 10


def read_camera_config(declaration: Mapping[str, object]) -> CameraConfig:
    """解析一个已冻结或原始设备声明，不探测任何主机或相机资源。"""
    if not isinstance(declaration, Mapping):
        raise ValueError("设备声明必须是表")
    if declaration.get("kind") != "camera" or declaration.get("driver") != DRIVER_ID:
        raise ValueError("设备声明不是 Hasselblad 相机")

    raw = declaration.get("hasselblad")
    if not isinstance(raw, Mapping):
        raise ValueError("Hasselblad 配置必须是表")
    unknown = set(raw) - {"executable", "serial", "product_id"}
    if unknown:
        raise ValueError(f"Hasselblad 配置包含未知字段: {sorted(unknown)}")
    missing = {"executable", "serial"} - set(raw)
    if missing:
        raise ValueError(f"Hasselblad 配置缺少必填字段: {sorted(missing)}")

    executable = raw["executable"]
    if (
        not isinstance(executable, str)
        or not executable
        or "\0" in executable
        or not PurePosixPath(executable).is_absolute()
    ):
        raise ValueError("Hasselblad executable 必须是非空绝对 POSIX 路径")

    serial = raw["serial"]
    if not isinstance(serial, str) or not serial:
        raise ValueError("Hasselblad serial 必须是非空字符串")
    if any(category(character) == "Cc" for character in serial):
        raise ValueError("Hasselblad serial 不能包含控制字符")

    product_id = raw.get("product_id", 10)
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        raise ValueError("Hasselblad product_id 必须是整数")
    if not 1 <= product_id <= 65535:
        raise ValueError("Hasselblad product_id 必须在 1～65535 之间")

    return CameraConfig(executable, serial, product_id)
