"""Hasselblad 单张参数、能力 Schema 与设备参数元数据的唯一来源。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType

from camctl.host_files.photo_formats import CAPTURE_FORMATS, CaptureFormat, CaptureFormatDescription

from camctl.acceptance.schema import BodySchemaError, validate_precise
from camctl.contracts.json_values import JsonValue, is_json_integer

__all__ = [
    "APERTURE_FIELD",
    "CAPTURE_FORMATS",
    "CaptureFormat",
    "CaptureFormatDescription",
    "EXPOSURE_MODE_DEFINITIONS",
    "ExposureMode",
    "ExposureModeDefinition",
    "FOCUS_MODE_DEFINITIONS",
    "FocusMode",
    "FocusModeDefinition",
    "PHOTO_COMMANDS",
    "PHOTO_FIELDS",
    "PhotoCommandVocabulary",
    "PhotoExposure",
    "PhotoFieldDefinition",
    "PhotoFocus",
    "PhotoSettings",
    "PhotoScalarOption",
    "PhotoWhiteBalance",
    "SHUTTER_FIELD",
    "WHITE_BALANCE_MODE_DEFINITIONS",
    "WhiteBalanceMode",
    "WhiteBalanceModeDefinition",
    "parse_photo_settings",
    "photo_schema",
]


class ExposureMode(StrEnum):
    MANUAL = "M"
    APERTURE_PRIORITY = "A"
    SHUTTER_PRIORITY = "S"
    PROGRAM = "P"


class WhiteBalanceMode(StrEnum):
    AUTO = "auto"
    DAYLIGHT = "daylight"
    CLOUDY = "cloudy"
    SHADE = "shade"
    TUNGSTEN = "tungsten"
    FLUORESCENT = "fluorescent"
    FLASH = "flash"
    MANUAL = "manual"


class FocusMode(StrEnum):
    MANUAL = "MF"
    SINGLE_AUTOFOCUS = "AF-S"


@dataclass(frozen=True, slots=True)
class PhotoScalarOption:
    value: str | int | Decimal
    title: str


@dataclass(frozen=True, slots=True)
class PhotoFieldDefinition:
    name: str
    title: str
    json_type: str
    command_field: str | None = None
    options: tuple[PhotoScalarOption, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ExposureModeDefinition:
    title: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WhiteBalanceModeDefinition:
    title: str
    uses_temperature: bool = False


@dataclass(frozen=True, slots=True)
class FocusModeDefinition:
    title: str
    supports_relative_move: bool = False
    follow_up_command: str | None = None


class PhotoCommandVocabulary(StrEnum):
    """Hasselblad 工具使用的固定命令字面量。"""

    SET = "set"
    DRIVE_MODE_FIELD = "drive-mode"
    DRIVE_MODE_SINGLE = "single"
    CAPTURE_FORMAT_FIELD = "capture-format"
    EXPOSURE_MODE_FIELD = "exposure-mode"
    WHITE_BALANCE_MODE_FIELD = "wb-mode"
    WHITE_BALANCE_FIELD = "wb"
    WHITE_BALANCE_TINT = "0"
    FOCUS_MODE_FIELD = "focus-mode"
    RELATIVE_FOCUS = "focus"
    AUTOFOCUS = "autofocus"
    CAPTURE = "capture"


PHOTO_COMMANDS = PhotoCommandVocabulary


def _finite_fraction(value: Fraction) -> Decimal | None:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    if denominator != 1:
        return None
    scale = max(twos, fives)
    coefficient = value.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = tuple(int(digit) for digit in str(abs(coefficient)))
    return Decimal((int(coefficient < 0), digits, -scale))


def _round_fraction_to_three_significant_digits(value: Fraction) -> Decimal:
    """用整数运算把循环分数按三位有效数字 half-up 舍入一次。"""
    numerator = abs(value.numerator)
    denominator = value.denominator
    if numerator == 0:
        return Decimal(0)

    exponent = len(str(numerator)) - len(str(denominator))
    if exponent >= 0:
        if numerator < denominator * 10**exponent:
            exponent -= 1
    elif numerator * 10 ** (-exponent) < denominator:
        exponent -= 1

    decimal_places = 2 - exponent
    if decimal_places >= 0:
        scaled_numerator = numerator * 10**decimal_places
        scaled_denominator = denominator
    else:
        scaled_numerator = numerator
        scaled_denominator = denominator * 10 ** (-decimal_places)
    coefficient, remainder = divmod(scaled_numerator, scaled_denominator)
    if 2 * remainder >= scaled_denominator:
        coefficient += 1
    if coefficient == 1000:
        coefficient //= 10
        decimal_places -= 1

    digits = tuple(int(digit) for digit in str(coefficient))
    return Decimal((int(value.numerator < 0), digits, -decimal_places))


def _shutter_seconds(value: Decimal | Fraction) -> Decimal:
    if isinstance(value, Decimal):
        return value
    exact = _finite_fraction(value)
    if exact is not None:
        return exact
    return _round_fraction_to_three_significant_digits(value)


_ISO_VALUES = (64, 100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600)
_APERTURE_VALUES = tuple(Decimal(value) for value in (
    "4", "4.8", "5.6", "6.8", "8", "9.5", "11", "13", "16", "19", "22", "27", "32",
))
_SHUTTER_INPUTS: tuple[tuple[str, Decimal | Fraction], ...] = (
    ("32 秒", Decimal("32")), ("23 秒", Decimal("23")), ("16 秒", Decimal("16")),
    ("11 秒", Decimal("11")), ("8 秒", Decimal("8")), ("6 秒", Decimal("6")),
    ("4 秒", Decimal("4")), ("3 秒", Decimal("3")), ("2 秒", Decimal("2")),
    ("1.4 秒", Decimal("1.4")), ("1 秒", Decimal("1")), ("0.7 秒", Decimal("0.7")),
    ("0.5 秒", Decimal("0.5")), ("0.3 秒", Decimal("0.3")), ("0.25 秒", Decimal("0.25")),
    ("1/6 秒", Fraction(1, 6)), ("0.125 秒", Decimal("0.125")), ("0.1 秒", Decimal("0.1")),
    ("1/15 秒", Fraction(1, 15)), ("1/20 秒", Fraction(1, 20)), ("1/30 秒", Fraction(1, 30)),
    ("1/45 秒", Fraction(1, 45)), ("1/60 秒", Fraction(1, 60)), ("1/90 秒", Fraction(1, 90)),
    ("1/125 秒", Fraction(1, 125)), ("1/180 秒", Fraction(1, 180)),
    ("1/250 秒", Fraction(1, 250)), ("1/350 秒", Fraction(1, 350)),
    ("1/500 秒", Fraction(1, 500)), ("1/1000 秒", Fraction(1, 1000)),
    ("1/1500 秒", Fraction(1, 1500)), ("1/2000 秒", Fraction(1, 2000)),
    ("1/3000 秒", Fraction(1, 3000)), ("1/4000 秒", Fraction(1, 4000)),
)


def _options(values: tuple[tuple[str | int | Decimal, str], ...]) -> tuple[PhotoScalarOption, ...]:
    return tuple(PhotoScalarOption(value, title) for value, title in values)


_iso_options = _options(tuple((value, str(value)) for value in _ISO_VALUES))
_aperture_options = _options(tuple((value, f"f/{value:f}") for value in _APERTURE_VALUES))
_shutter_options = _options(tuple((
    _shutter_seconds(value), title,
) for title, value in _SHUTTER_INPUTS))

PHOTO_FIELDS: Mapping[str, PhotoFieldDefinition] = MappingProxyType({
    "iso": PhotoFieldDefinition("iso", "ISO 感光度", "integer", command_field="iso", options=_iso_options),
    "aperture": PhotoFieldDefinition(
        "aperture", "光圈", "number", command_field="aperture", options=_aperture_options,
    ),
    "shutter_s": PhotoFieldDefinition(
        "shutter_s", "快门", "number", command_field="shutter", options=_shutter_options,
    ),
    "temperature_k": PhotoFieldDefinition(
        "temperature_k", "色温", "integer", minimum=2000, maximum=10000,
        description="单位为 K。",
    ),
    "relative_move": PhotoFieldDefinition(
        "relative_move", "手动调焦相对移动量", "integer", command_field="focus",
        options=_options(((-5, "-5"), (5, "5"))),
        description="开发验证临时选项；未填写时不移动焦点；正值向无限远，负值向近处。单位为 CLI 调焦单位。",
    ),
})

APERTURE_FIELD = PHOTO_FIELDS["aperture"]
SHUTTER_FIELD = PHOTO_FIELDS["shutter_s"]

EXPOSURE_MODE_DEFINITIONS: Mapping[ExposureMode, ExposureModeDefinition] = MappingProxyType({
    ExposureMode.MANUAL: ExposureModeDefinition("手动曝光", ("iso", "aperture", "shutter_s")),
    ExposureMode.APERTURE_PRIORITY: ExposureModeDefinition("光圈优先", ("iso", "aperture")),
    ExposureMode.SHUTTER_PRIORITY: ExposureModeDefinition("快门优先", ("iso", "shutter_s")),
    ExposureMode.PROGRAM: ExposureModeDefinition("程序曝光", ("iso",)),
})

WHITE_BALANCE_MODE_DEFINITIONS: Mapping[WhiteBalanceMode, WhiteBalanceModeDefinition] = MappingProxyType({
    WhiteBalanceMode.AUTO: WhiteBalanceModeDefinition("自动白平衡"),
    WhiteBalanceMode.DAYLIGHT: WhiteBalanceModeDefinition("日光"),
    WhiteBalanceMode.CLOUDY: WhiteBalanceModeDefinition("阴天"),
    WhiteBalanceMode.SHADE: WhiteBalanceModeDefinition("阴影"),
    WhiteBalanceMode.TUNGSTEN: WhiteBalanceModeDefinition("钨丝灯"),
    WhiteBalanceMode.FLUORESCENT: WhiteBalanceModeDefinition("荧光灯"),
    WhiteBalanceMode.FLASH: WhiteBalanceModeDefinition("闪光灯"),
    WhiteBalanceMode.MANUAL: WhiteBalanceModeDefinition("手动白平衡", uses_temperature=True),
})

FOCUS_MODE_DEFINITIONS: Mapping[FocusMode, FocusModeDefinition] = MappingProxyType({
    FocusMode.MANUAL: FocusModeDefinition("手动对焦", supports_relative_move=True, follow_up_command="focus"),
    FocusMode.SINGLE_AUTOFOCUS: FocusModeDefinition("单次自动对焦", follow_up_command="autofocus"),
})


@dataclass(frozen=True, slots=True)
class PhotoExposure:
    mode: ExposureMode
    iso: int
    aperture: Decimal | None
    shutter_s: Decimal | None


@dataclass(frozen=True, slots=True)
class PhotoWhiteBalance:
    mode: WhiteBalanceMode
    temperature_k: int | None


@dataclass(frozen=True, slots=True)
class PhotoFocus:
    mode: FocusMode
    relative_move: int | None


@dataclass(frozen=True, slots=True)
class PhotoSettings:
    capture_format: CaptureFormat
    exposure: PhotoExposure
    white_balance: PhotoWhiteBalance
    focus: PhotoFocus


def _field_schema(field: PhotoFieldDefinition) -> dict[str, object]:
    schema: dict[str, object] = {"type": field.json_type, "title": field.title}
    if field.options:
        schema["oneOf"] = [
            {"const": option.value, "title": option.title}
            for option in field.options
        ]
    if field.minimum is not None:
        schema["minimum"] = field.minimum
    if field.maximum is not None:
        schema["maximum"] = field.maximum
    if field.description is not None:
        schema["description"] = field.description
    return schema


def _mode_field_schema(mode: StrEnum, title: str) -> dict[str, object]:
    return {"type": "string", "const": mode.value, "title": title}


def _exposure_branch(mode: ExposureMode, definition: ExposureModeDefinition) -> dict[str, object]:
    properties: dict[str, object] = {"mode": _mode_field_schema(mode, "曝光模式")}
    properties.update({field: _field_schema(PHOTO_FIELDS[field]) for field in definition.fields})
    return {
        "type": "object",
        "title": definition.title,
        "properties": properties,
        "required": ["mode", *definition.fields],
        "additionalProperties": False,
    }


def _white_balance_branch(
    mode: WhiteBalanceMode, definition: WhiteBalanceModeDefinition,
) -> dict[str, object]:
    properties: dict[str, object] = {"mode": _mode_field_schema(mode, "白平衡模式")}
    required = ["mode"]
    if definition.uses_temperature:
        properties["temperature_k"] = _field_schema(PHOTO_FIELDS["temperature_k"])
        required.append("temperature_k")
    return {
        "type": "object",
        "title": definition.title,
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _focus_branch(mode: FocusMode, definition: FocusModeDefinition) -> dict[str, object]:
    properties: dict[str, object] = {"mode": _mode_field_schema(mode, "对焦模式")}
    if definition.supports_relative_move:
        properties["relative_move"] = _field_schema(PHOTO_FIELDS["relative_move"])
    return {
        "type": "object",
        "title": definition.title,
        "properties": properties,
        "required": ["mode"],
        "additionalProperties": False,
    }


_PHOTO_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "hasselblad_photo"},
        "capture_format": {
            "type": "string",
            "title": "图片格式",
            "oneOf": [
                {"const": definition.command_value, "title": definition.title}
                for definition in CAPTURE_FORMATS.values()
            ],
        },
        "exposure": {
            "type": "object",
            "title": "曝光设置",
            "oneOf": [
                _exposure_branch(mode, definition)
                for mode, definition in EXPOSURE_MODE_DEFINITIONS.items()
            ],
        },
        "white_balance": {
            "type": "object",
            "title": "白平衡设置",
            "oneOf": [
                _white_balance_branch(mode, definition)
                for mode, definition in WHITE_BALANCE_MODE_DEFINITIONS.items()
            ],
        },
        "focus": {
            "type": "object",
            "title": "对焦设置",
            "oneOf": [
                _focus_branch(mode, definition)
                for mode, definition in FOCUS_MODE_DEFINITIONS.items()
            ],
        },
    },
    "required": ["type", "capture_format", "exposure", "white_balance", "focus"],
    "additionalProperties": False,
}


def photo_schema() -> dict[str, object]:
    """返回 Hasselblad 完整参数 Schema 的独立副本。"""
    from copy import deepcopy

    return deepcopy(_PHOTO_SCHEMA)


def _decimal(value: JsonValue, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise BodySchemaError(f"Hasselblad 参数 {field} 必须是精确 JSON 数值")
    return value if isinstance(value, Decimal) else Decimal(value)


def _integer(value: JsonValue, field: str) -> int:
    if not is_json_integer(value):
        raise BodySchemaError(f"Hasselblad 参数 {field} 必须是整数")
    return int(value)


def parse_photo_settings(params: Mapping[str, JsonValue]) -> PhotoSettings:
    """严格校验完整参数，再解析为驱动内部使用的精确值对象。"""
    if not isinstance(params, Mapping):
        raise BodySchemaError("Hasselblad 参数必须是对象")
    document = dict(params)
    validate_precise(_PHOTO_SCHEMA, document, label="Hasselblad 拍摄参数")

    capture_format = CaptureFormat(document["capture_format"])
    exposure_raw = document["exposure"]
    exposure_mode = ExposureMode(exposure_raw["mode"])
    exposure = PhotoExposure(
        mode=exposure_mode,
        iso=_integer(exposure_raw["iso"], "exposure.iso"),
        aperture=(
            _decimal(exposure_raw["aperture"], "exposure.aperture")
            if "aperture" in exposure_raw else None
        ),
        shutter_s=(
            _decimal(exposure_raw["shutter_s"], "exposure.shutter_s")
            if "shutter_s" in exposure_raw else None
        ),
    )

    white_balance_raw = document["white_balance"]
    white_balance_mode = WhiteBalanceMode(white_balance_raw["mode"])
    white_balance = PhotoWhiteBalance(
        mode=white_balance_mode,
        temperature_k=(
            _integer(white_balance_raw["temperature_k"], "white_balance.temperature_k")
            if "temperature_k" in white_balance_raw else None
        ),
    )

    focus_raw = document["focus"]
    focus_mode = FocusMode(focus_raw["mode"])
    focus = PhotoFocus(
        mode=focus_mode,
        relative_move=(
            _integer(focus_raw["relative_move"], "focus.relative_move")
            if "relative_move" in focus_raw else None
        ),
    )
    return PhotoSettings(capture_format, exposure, white_balance, focus)
