"""Hasselblad 本地相机配置的纯解析和校验。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.tasks import CaptureStorage

__all__ = ["CameraConfig", "read_camera_config", "resolve_source_storage"]


@dataclass(frozen=True)
class CameraConfig:
    executable: str
    serial: str
    product_id: int = 10
    output_storage: CaptureStorage = CaptureStorage.HOST


def read_camera_config(declaration: Mapping[str, object]) -> CameraConfig:
    """解析一个已冻结或原始设备声明，不探测任何主机或相机资源。"""
    if not isinstance(declaration, Mapping):
        raise ValueError("设备声明必须是表")
    if declaration.get("kind") != "camera" or declaration.get("driver") != DRIVER_ID:
        raise ValueError("设备声明不是 Hasselblad 相机")
    if "output_storage" in declaration:
        raise ValueError("output_storage 必须位于 Hasselblad 配置表")

    raw = declaration.get("hasselblad")
    if not isinstance(raw, Mapping):
        raise ValueError("Hasselblad 配置必须是表")
    unknown = set(raw) - {"executable", "serial", "product_id", "output_storage"}
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
    product_id = raw.get("product_id", 10)
    CameraIdentity(serial, product_id)

    output_storage = raw.get("output_storage", CaptureStorage.HOST.value)
    if not isinstance(output_storage, str):
        raise ValueError("Hasselblad output_storage 必须是字符串")
    try:
        storage = CaptureStorage(output_storage)
    except ValueError as error:
        raise ValueError("Hasselblad output_storage 不合法") from error

    return CameraConfig(executable, serial, product_id, storage)


def resolve_source_storage(declaration: Mapping[str, object]) -> CaptureStorage:
    """复用 Hasselblad 配置校验解析拍摄正式源，不访问设备或主机文件。"""
    return read_camera_config(declaration).output_storage
