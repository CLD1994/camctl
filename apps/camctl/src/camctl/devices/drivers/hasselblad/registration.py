"""Hasselblad 静态能力与运行驱动的同源登记。"""

from __future__ import annotations

import time
from pathlib import Path

from camctl.acceptance.schema import RuleError
from camctl.devices.definitions_runtime import (
    current_driver_definitions,
    register_driver_definitions,
)
from camctl.devices.drivers.registry import DriverEntry, DriverStatus
from camctl.devices.drivers.runtime import current_registry, register_drivers
from camctl.host_files.models import BoundDirectories

from .configuration import read_camera_config
from .definitions import DRIVER_ID, driver_definition
from .driver import EVIDENCE, HasselbladDriver

__all__ = ["register_builtin_hasselblad_driver", "begin_builtin_hasselblad_session"]


def begin_builtin_hasselblad_session() -> None:
    """取得执行会话资格后清除准备凭据，普通登记不改变原会话。"""
    entry = current_registry().entry(DRIVER_ID)
    if entry is not None and isinstance(entry.driver, HasselbladDriver):
        entry.driver.begin_session()


def _validate_serial_bindings(config) -> None:
    seen: dict[str, str] = {}
    for target_id, declaration in config.devices.items():
        if declaration.get("driver") != DRIVER_ID:
            continue
        camera = read_camera_config(declaration)
        previous = seen.get(camera.serial)
        if previous is not None:
            raise RuleError(
                f"Hasselblad 序列号重复绑定: {previous!r} 与 {target_id!r}"
            )
        seen[camera.serial] = target_id


def _is_same_driver_entry(entry: DriverEntry) -> bool:
    driver = entry.driver
    return (
        isinstance(driver, HasselbladDriver)
        and driver.driver_id == DRIVER_ID
        and entry.evidence is EVIDENCE
        and entry.declaration == driver.declaration
        and entry.status is DriverStatus.DEVICE_VERIFICATION_PENDING
    )


def register_builtin_hasselblad_driver(config) -> None:
    """登记固定 Hasselblad 能力；同一驱动身份再次装配时刷新配置绑定。"""
    _validate_serial_bindings(config)

    definition = driver_definition()
    definitions = current_driver_definitions().drivers
    previous_definition = definitions.get(DRIVER_ID)
    previous = current_registry().entry(DRIVER_ID)
    if previous_definition is not None or previous is not None:
        if (
            previous_definition != definition
            or previous is None
            or not _is_same_driver_entry(previous)
            or previous.declaration != definition.declaration
        ):
            raise RuleError(f"内置 Hasselblad 身份与既有登记冲突: {DRIVER_ID!r}")
        previous.driver.devices = config.devices
        previous.driver.roots = BoundDirectories(staging=Path(config.paths.staging))
        return

    driver = HasselbladDriver(
        config.devices,
        BoundDirectories(staging=Path(config.paths.staging)),
        monotonic_ns=time.monotonic_ns,
    )
    entry = DriverEntry(
        driver_id=DRIVER_ID,
        driver=driver,
        declaration=driver.declaration,
        evidence=EVIDENCE,
        status=DriverStatus.DEVICE_VERIFICATION_PENDING,
    )
    if entry.declaration != definition.declaration:
        raise RuleError(f"内置 Hasselblad 静态与运行声明冲突: {DRIVER_ID!r}")
    # 在修改任一登记点前完成静态能力及运行端口构造和身份验证。
    register_driver_definitions(definition)
    register_drivers(entry)
