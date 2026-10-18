"""Hasselblad CFV 100C 的同源单张能力与执行任务定义。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from camctl.contracts.json_values import JsonValue
from camctl.devices.catalog import ActionCapability, DriverDefinition
from camctl.devices.ports import DriverDeclaration
from camctl.devices.tasks import CaptureStorage, CaptureTask, CaptureTaskFactory
from camctl.devices.task_metadata import cancellation_description

__all__ = [
    "DRIVER_ID",
    "PARAMETER_TYPE",
    "capability",
    "driver_definition",
]


DRIVER_ID = "hasselblad_cfv100c"
PARAMETER_TYPE = "hasselblad_photo"
DRIVER_DECLARATION = DriverDeclaration(
    control_supported=True,
    stop_supported=False,
    query_supported=False,
    result_supported=False,
    read_supported=True,
    digest_supported=False,
    delete_supported=False,
    directory_supported=False,
    local_read_recovery_supported=True,
)

_PHOTO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": PARAMETER_TYPE}},
    "required": ["type"],
    "additionalProperties": False,
}


def _photo_task(_effective_params: Mapping[str, JsonValue]) -> CaptureTask:
    return CaptureTask(
        action_type="camera_take_photo",
        output_storage=CaptureStorage.HOST,
        start_call_timeout_s=Decimal("180"),
    )


def _deployed_photo_factory(declaration: Mapping[str, object]) -> CaptureTaskFactory:
    from camctl.devices.drivers.hasselblad.configuration import resolve_source_storage

    storage = resolve_source_storage(declaration)

    def photo_task(effective_params: Mapping[str, JsonValue]) -> CaptureTask:
        return replace(_photo_task(effective_params), output_storage=storage)

    return photo_task


def capability() -> ActionCapability:
    """返回唯一权威的单张参数定义，供说明导出与受理共同使用。"""
    task = _photo_task({"type": PARAMETER_TYPE})
    return ActionCapability(
        action_type="camera_take_photo",
        parameter_type=PARAMETER_TYPE,
        name="Hasselblad 单张 RAW",
        description=("执行一次单张 RAW 拍摄；驱动沿用机身已有曝光、白平衡和对焦设置。"
                     + cancellation_description(task)),
        preview_supported=True,
        schema=deepcopy(_PHOTO_SCHEMA),
        defaults={},
        task_factory=_photo_task,
        deployment_task_factory=_deployed_photo_factory,
    )


def driver_definition() -> DriverDefinition:
    """构造由 Hasselblad 静态单张能力组成的驱动定义。"""
    from camctl.devices.drivers.hasselblad.configuration import resolve_source_storage

    photo = capability()
    return DriverDefinition(
        DRIVER_ID,
        {photo.action_type: (photo,)},
        DRIVER_DECLARATION,
        resolve_source_storage,
    )
