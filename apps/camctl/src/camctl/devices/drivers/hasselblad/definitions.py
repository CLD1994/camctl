"""Hasselblad CFV 100C 的同源单张能力与执行任务定义。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from camctl.contracts.json_values import JsonValue
from camctl.devices.catalog import ActionCapability, DriverDefinition
from camctl.devices.drivers.hasselblad.parameters import (
    PHOTO_COMMANDS,
    parse_photo_settings,
    photo_schema,
)
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
_PHOTO_ACTION_TYPE = "camera_take_photo"
_PHOTO_START_CALL_TIMEOUT_S = Decimal("180")
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
    local_control_recovery_supported=True,
    capture_preparation_supported=True,
)


def _photo_task_facts(output_storage: CaptureStorage = CaptureStorage.HOST) -> CaptureTask:
    return CaptureTask(
        action_type=_PHOTO_ACTION_TYPE,
        output_storage=output_storage,
        start_call_timeout_s=_PHOTO_START_CALL_TIMEOUT_S,
    )


def _photo_task(effective_params: Mapping[str, JsonValue]) -> CaptureTask:
    parse_photo_settings(effective_params)
    return _photo_task_facts()


def _deployed_photo_factory(declaration: Mapping[str, object]) -> CaptureTaskFactory:
    from camctl.devices.drivers.hasselblad.configuration import resolve_source_storage

    storage = resolve_source_storage(declaration)

    def photo_task(effective_params: Mapping[str, JsonValue]) -> CaptureTask:
        return replace(_photo_task(effective_params), output_storage=storage)

    return photo_task


def capability() -> ActionCapability:
    """返回唯一权威的单张参数定义，供说明导出与受理共同使用。"""
    task = _photo_task_facts()
    return ActionCapability(
        action_type=_PHOTO_ACTION_TYPE,
        parameter_type=PARAMETER_TYPE,
        name="Hasselblad 单张拍照",
        description=(
            "用户明确选择图片格式、曝光、白平衡和对焦后，驱动执行一次拍摄。"
            f"每次单张拍摄均将 `{PHOTO_COMMANDS.DRIVE_MODE_FIELD.value}` 设置为 "
            f"`{PHOTO_COMMANDS.DRIVE_MODE_SINGLE.value}`。"
            f"手动白平衡通过 `{PHOTO_COMMANDS.WHITE_BALANCE_FIELD.value}` 设置，"
            f"色调固定为 `{PHOTO_COMMANDS.WHITE_BALANCE_TINT.value}`。"
            "手动调焦相对移动量是开发验证临时选项；省略时不移动焦点，正值向无限远，"
            "负值向近处，单位为 CLI 调焦单位。"
            + cancellation_description(task)
        ),
        preview_supported=True,
        schema=photo_schema(),
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
