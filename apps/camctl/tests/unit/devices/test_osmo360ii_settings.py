"""OSMO 可选参数直接决定每次拍摄的设置，固定值由驱动维护。"""

from copy import deepcopy

import pytest

from camctl.acceptance.schema import BodySchemaError, validate_precise
from camctl.devices.drivers.adb_cameras.commands import settings_for
from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract


def params(kind="record", mode="manual", bitrate="high"):
    result = {"type": "osmo360ii_" + kind, "duration_s": 300,
              "exposure": {"mode": mode}, "bitrate": bitrate}
    if kind == "timelapse":
        result["interval_s"] = 2
    return result


def capability(kind):
    return next(cap for cap in osmo360ii_contract().capabilities()
                if cap.parameter_type == "osmo360ii_" + kind)


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("bitrate, value", [("standard", "1"), ("high", "2")])
def test_selected_bitrate_is_encoded_for_every_capture_mode(kind, mode, bitrate, value):
    selected = params(kind, mode, bitrate)
    validate_precise(capability(kind).schema, selected)
    settings = settings_for("dji-osmo360-ii", "camera_" + kind, selected)
    bitrate_commands = [command for command in settings
                        if command[:3] == ("simulate_device", "-s", "DeviceRecordRecSettingBitRate")]
    assert bitrate_commands == [("simulate_device", "-s", "DeviceRecordRecSettingBitRate", value)]


@pytest.mark.parametrize("kind", ["record", "timelapse"])
def test_capability_exposes_only_selectable_capture_fields(kind):
    schema = capability(kind).schema
    fields = {"type", "duration_s", "bitrate", "exposure"}
    if kind == "timelapse":
        fields.add("interval_s")
    assert set(schema["properties"]) == set(schema["required"]) == fields
    for branch in schema["properties"]["exposure"]["oneOf"]:
        assert set(branch["properties"]) == {"mode"}
    assert capability(kind).defaults == {}


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("value", [None, True, 1, "120", "ultra"])
def test_unknown_or_non_string_bitrate_is_rejected(kind, value):
    with pytest.raises(BodySchemaError):
        validate_precise(capability(kind).schema, params(kind, bitrate=value))


@pytest.mark.parametrize("kind", ["record", "timelapse"])
def test_bitrate_must_be_saved_in_each_action(kind):
    selected = params(kind)
    del selected["bitrate"]
    with pytest.raises(BodySchemaError):
        validate_precise(capability(kind).schema, selected)


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("field, value", [("iso", 800), ("compensation_ev", 0),
                                         ("shutter", "1/60"), ("white_balance", 5200)])
def test_fixed_exposure_values_are_not_input_fields(kind, field, value):
    selected = deepcopy(params(kind))
    selected["exposure"][field] = value
    with pytest.raises(BodySchemaError):
        validate_precise(capability(kind).schema, selected)


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_timelapse_video_output_is_internal_to_the_driver(mode):
    selected = params("timelapse", mode)
    selected["outputs"] = "video"
    with pytest.raises(BodySchemaError):
        validate_precise(capability("timelapse").schema, selected)
