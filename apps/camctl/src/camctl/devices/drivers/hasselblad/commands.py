"""同一有序命令槽派生参数 argv 和整次 START 的工具数量界限。"""
from dataclasses import dataclass
from enum import StrEnum
from decimal import Decimal

from .parameters import (
    PHOTO_COMMANDS as C, PHOTO_FIELDS, CAPTURE_FORMATS,
    EXPOSURE_MODE_DEFINITIONS, WHITE_BALANCE_MODE_DEFINITIONS,
    FOCUS_MODE_DEFINITIONS, PhotoSettings,
)


class PhotoStep(StrEnum):
    LIST = "list"
    INFO = "info"
    DRIVE_MODE = "drive_mode_single"
    FORMAT = "capture_format"
    EXPOSURE_MODE = "exposure_mode"
    ISO = "iso"
    APERTURE = "aperture"
    SHUTTER = "shutter"
    WB_MODE = "white_balance_mode"
    WB = "white_balance"
    FOCUS_MODE = "focus_mode"
    FOCUS = "focus"
    AUTOFOCUS = "autofocus"
    CAPTURE = "capture"


PHOTO_START_CALL_LIMIT = len(PhotoStep)


@dataclass(frozen=True)
class PhotoCommand:
    name: PhotoStep
    argv: tuple[str, ...]


def _number(value: int | Decimal) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def photo_start_commands(settings: PhotoSettings) -> tuple[PhotoCommand, ...]:
    """只展开已校验的保存值，不重新计算或舍入目录值。"""
    exposure, wb, focus = settings.exposure, settings.white_balance, settings.focus
    exposure_fields = EXPOSURE_MODE_DEFINITIONS[exposure.mode].fields
    commands = []
    for slot in PhotoStep:
        match slot:
            case PhotoStep.LIST | PhotoStep.INFO:
                argv = (slot.value,)
            case PhotoStep.DRIVE_MODE:
                argv = (C.SET, C.DRIVE_MODE_FIELD, C.DRIVE_MODE_SINGLE)
            case PhotoStep.FORMAT:
                argv = (C.SET, C.CAPTURE_FORMAT_FIELD, CAPTURE_FORMATS[settings.capture_format].command_value)
            case PhotoStep.EXPOSURE_MODE:
                argv = (C.SET, C.EXPOSURE_MODE_FIELD, exposure.mode.value)
            case PhotoStep.ISO | PhotoStep.APERTURE | PhotoStep.SHUTTER:
                field = "shutter_s" if slot is PhotoStep.SHUTTER else slot.value
                if field not in exposure_fields:
                    continue
                argv = (C.SET, PHOTO_FIELDS[field].command_field, _number(getattr(exposure, field)))
            case PhotoStep.WB_MODE:
                argv = (C.SET, C.WHITE_BALANCE_MODE_FIELD, wb.mode.value)
            case PhotoStep.WB:
                if not WHITE_BALANCE_MODE_DEFINITIONS[wb.mode].uses_temperature:
                    continue
                argv = (C.SET, C.WHITE_BALANCE_FIELD, f"{wb.temperature_k},{C.WHITE_BALANCE_TINT}")
            case PhotoStep.FOCUS_MODE:
                argv = (C.SET, C.FOCUS_MODE_FIELD, focus.mode.value)
            case PhotoStep.FOCUS:
                if not FOCUS_MODE_DEFINITIONS[focus.mode].supports_relative_move or focus.relative_move is None:
                    continue
                argv = (FOCUS_MODE_DEFINITIONS[focus.mode].follow_up_command, str(focus.relative_move))
            case PhotoStep.AUTOFOCUS:
                if FOCUS_MODE_DEFINITIONS[focus.mode].supports_relative_move:
                    continue
                argv = (FOCUS_MODE_DEFINITIONS[focus.mode].follow_up_command,)
            case PhotoStep.CAPTURE:
                argv = (C.CAPTURE,)
        commands.append(PhotoCommand(slot, tuple(str(arg) for arg in argv)))
    return tuple(commands)
