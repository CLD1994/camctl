"""Hasselblad 单张参数的同源 Schema 与精确解析契约。"""

from decimal import Decimal

import pytest
import referencing


from camctl.acceptance.schema import BodySchemaError
from camctl.devices.drivers.hasselblad.parameters import (
    CAPTURE_FORMATS,
    EXPOSURE_MODE_DEFINITIONS,
    FOCUS_MODE_DEFINITIONS,
    WHITE_BALANCE_MODE_DEFINITIONS,
    CaptureFormat,
    PhotoSettings,
    parse_photo_settings,
    photo_schema,
)


@pytest.fixture(autouse=True)
def _isolate_schema_resources(monkeypatch):
    monkeypatch.setattr("camctl.contracts.schemas.schema_registry", lambda: referencing.Registry())

def _photo():
    return {
        "type": "hasselblad_photo",
        "capture_format": "raw",
        "exposure": {"mode": "M", "iso": 800, "aperture": 8, "shutter_s": "1/125"},
        "white_balance": {"mode": "manual", "temperature_k": 5500},
        "focus": {"mode": "MF", "relative_move": 5},
    }


def _branches(schema, field):
    return schema["properties"][field]["oneOf"]


def _branch_value(branch, field="mode"):
    return branch["properties"][field]["const"]


def _options(field_schema):
    return [branch["const"] for branch in field_schema["oneOf"]]


def test_repeated_photo_validation_checks_each_input_against_independent_rule():
    exported = photo_schema()
    exported["properties"]["capture_format"]["oneOf"].clear()
    assert parse_photo_settings(_photo()).capture_format is CaptureFormat.RAW
    with pytest.raises(BodySchemaError):
        parse_photo_settings({**_photo(), "capture_format": "unsupported"})
    assert parse_photo_settings({**_photo(), "capture_format": "jpeg"}).capture_format is CaptureFormat.JPEG


def test_photo_schema_has_closed_independent_branches():
    schema = photo_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["required"] == ["type", "capture_format", "exposure", "white_balance", "focus"]
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["properties"]["type"]["const"] == "hasselblad_photo"
    assert schema["additionalProperties"] is False
    assert "default" not in schema

    formats = schema["properties"]["capture_format"]
    assert formats["type"] == "string"
    assert {choice["const"] for choice in formats["oneOf"]} == {
        item.command_value for item in CAPTURE_FORMATS.values()
    }
    assert all(choice.get("title") for choice in formats["oneOf"])
    assert len(formats["oneOf"]) == 2

    exposure = _branches(schema, "exposure")
    exposure_definitions = {
        mode.value: definition for mode, definition in EXPOSURE_MODE_DEFINITIONS.items()
    }
    assert len(exposure) == len(exposure_definitions) == 4
    assert {_branch_value(branch) for branch in exposure} == set(exposure_definitions)
    for branch in exposure:
        mode = _branch_value(branch)
        expected_fields = {"mode", *exposure_definitions[mode].fields}
        assert branch["type"] == "object"
        assert set(branch["properties"]) == expected_fields
        assert set(branch["required"]) == expected_fields
        assert branch["additionalProperties"] is False
        assert branch["title"] == exposure_definitions[mode].title
        assert branch["properties"]["mode"]["title"] == "曝光模式"
        assert all(branch["properties"][key].get("title") for key in expected_fields - {"mode"})

    white_balance = _branches(schema, "white_balance")
    white_balance_definitions = {
        mode.value: definition for mode, definition in WHITE_BALANCE_MODE_DEFINITIONS.items()
    }
    assert len(white_balance) == len(white_balance_definitions) == 8
    assert {_branch_value(branch) for branch in white_balance} == set(white_balance_definitions)
    for branch in white_balance:
        mode = _branch_value(branch)
        definition = white_balance_definitions[mode]
        expected = {"mode", "temperature_k"} if definition.uses_temperature else {"mode"}
        assert set(branch["properties"]) == expected
        assert set(branch["required"]) == expected
        assert branch["additionalProperties"] is False
        assert branch["title"] == definition.title
        assert branch["properties"]["mode"]["title"] == "白平衡模式"
        assert "tint" not in branch["properties"]

    focus = _branches(schema, "focus")
    focus_definitions = {mode.value: definition for mode, definition in FOCUS_MODE_DEFINITIONS.items()}
    assert len(focus) == len(focus_definitions) == 2
    assert {_branch_value(branch) for branch in focus} == set(focus_definitions)
    for branch in focus:
        mode = _branch_value(branch)
        expected = {"mode", "relative_move"} if focus_definitions[mode].supports_relative_move else {"mode"}
        assert set(branch["properties"]) == expected
        assert branch["required"] == ["mode"]
        assert branch["additionalProperties"] is False

    # 格式与三个对象分支互相独立，合法分支可以交叉组合。
    jpeg_program_af = _photo()
    jpeg_program_af.update(capture_format="jpeg")
    jpeg_program_af["exposure"] = {"mode": "P", "iso": 1600}
    jpeg_program_af["white_balance"] = {"mode": "cloudy"}
    jpeg_program_af["focus"] = {"mode": "AF-S"}
    assert parse_photo_settings(jpeg_program_af).capture_format is CaptureFormat.JPEG

    for field in schema["required"]:
        incomplete = _photo()
        del incomplete[field]
        with pytest.raises(BodySchemaError):
            parse_photo_settings(incomplete)
    for bad in (
        {**_photo(), "focus": None},
        {**_photo(), "capture_format": 7},
        {**_photo(), "exposure": {"mode": "M", "iso": True, "aperture": 8, "shutter_s": "1/125"}},
        {**_photo(), "white_balance": {"mode": "manual", "temperature_k": 5500, "tint": 0}},
        {**_photo(), "exposure": {"mode": "M", "iso": 800, "aperture": 8, "shutter_s": "1/125", "extra": 1}},
        {**_photo(), "exposure": {"mode": "P", "iso": 800, "aperture": 8}},
        {**_photo(), "white_balance": {"mode": "auto", "temperature_k": 5500}},
        {**_photo(), "focus": {"mode": "AF-S", "relative_move": 5}},
        {**_photo(), "extra": 1},
    ):
        with pytest.raises(BodySchemaError):
            parse_photo_settings(bad)


def test_photo_domains_are_exact_and_positive():
    schema = photo_schema()
    manual = next(branch for branch in _branches(schema, "exposure") if _branch_value(branch) == "M")
    iso_values = _options(manual["properties"]["iso"])
    aperture_values = _options(manual["properties"]["aperture"])
    shutter_values = _options(manual["properties"]["shutter_s"])

    assert len(iso_values) == 10 and len(set(iso_values)) == 10
    assert len(aperture_values) == 13 and len(set(aperture_values)) == 13
    assert len(shutter_values) == 34 and len(set(shutter_values)) == 34
    assert {64, 800, 25600}.issubset(iso_values)
    assert {Decimal("4"), Decimal("4.8"), Decimal("32")}.issubset(aperture_values)
    assert manual["properties"]["shutter_s"]["type"] == "string"
    assert all(isinstance(value, str) for value in shutter_values)
    assert shutter_values[:3] == ["32/1", "23/1", "16/1"]
    assert shutter_values[-4:] == ["1/1500", "1/2000", "1/3000", "1/4000"]
    assert {"7/5", "1/125"}.issubset(shutter_values)
    assert all(value > 0 for value in aperture_values)

    for field, values in (("iso", iso_values), ("aperture", aperture_values), ("shutter_s", shutter_values)):
        for value in values:
            params = _photo()
            params["exposure"][field] = value
            parse_photo_settings(params)

    for field, outside in (("iso", 65), ("aperture", Decimal("4.1")), ("shutter_s", "1/1499")):
        params = _photo()
        params["exposure"][field] = outside
        with pytest.raises(BodySchemaError):
            parse_photo_settings(params)


def test_shutter_strings_are_preserved_and_unregistered_inputs_rejected():
    manual = next(branch for branch in _branches(photo_schema(), "exposure") if _branch_value(branch) == "M")
    choices = {choice["const"]: choice["title"] for choice in manual["properties"]["shutter_s"]["oneOf"]}
    assert choices["1/6"] == "1/6 秒"
    assert choices["1/180"] == "1/180 秒"
    assert choices["7/5"] == "1.4 秒"
    for value in ("32/1", "7/5", "1/1500", "1/3000", "1/4000"):
        params = _photo()
        params["exposure"]["shutter_s"] = value
        assert parse_photo_settings(params).exposure.shutter_s == value
    for outside in (32, Decimal("0.000667"), "0.000667", "2/3000", "32", None, True):
        params = _photo()
        params["exposure"]["shutter_s"] = outside
        with pytest.raises(BodySchemaError):
            parse_photo_settings(params)


def test_manual_white_balance_integer_bounds():
    for value in (2000, 5500, 10000):
        params = _photo()
        params["white_balance"]["temperature_k"] = value
        assert parse_photo_settings(params).white_balance.temperature_k == value
    for value in (1999, 10001, Decimal("5500.5"), True, "5500", None):
        params = _photo()
        params["white_balance"]["temperature_k"] = value
        with pytest.raises(BodySchemaError):
            parse_photo_settings(params)
    missing = _photo()
    del missing["white_balance"]["temperature_k"]
    with pytest.raises(BodySchemaError):
        parse_photo_settings(missing)


def test_manual_focus_absence_differs_from_zero():
    omitted = _photo()
    del omitted["focus"]["relative_move"]
    assert parse_photo_settings(omitted).focus.relative_move is None

    for value in (-5, 5):
        params = _photo()
        params["focus"]["relative_move"] = value
        assert parse_photo_settings(params).focus.relative_move == value

    zero = _photo()
    zero["focus"]["relative_move"] = 0
    with pytest.raises(BodySchemaError):
        parse_photo_settings(zero)

    auto_with_move = _photo()
    auto_with_move["focus"] = {"mode": "AF-S", "relative_move": 5}
    with pytest.raises(BodySchemaError):
        parse_photo_settings(auto_with_move)


def test_parse_photo_settings_preserves_machine_values_and_exact_numbers():
    params = _photo()
    params["capture_format"] = "jpeg"
    params["exposure"]["aperture"] = Decimal("4.8")
    params["exposure"]["shutter_s"] = "1/3000"
    parsed = parse_photo_settings(params)

    assert isinstance(parsed, PhotoSettings)
    assert parsed.capture_format is CaptureFormat.JPEG
    assert parsed.exposure.iso == 800
    assert parsed.exposure.aperture == Decimal("4.8")
    assert parsed.exposure.shutter_s == "1/3000"
    assert parsed.white_balance.temperature_k == 5500
    assert parsed.focus.relative_move == 5
