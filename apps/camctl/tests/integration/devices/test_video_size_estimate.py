"""真实同源 Catalog 的元数据、参数关联与精确编码边界。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from video_estimate_helpers import (
    catalog_for_capabilities,
    record_capability,
    record_catalog,
)

from camctl.acceptance.schema import RuleError
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.cli import encode_describe_document
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import SchemaValidationError
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog


def _direct_estimate(bitrate=130):
    return {"bitrate_mbps": bitrate, "duration": {
        "method": "direct", "seconds": {"source": "constant", "value": 60},
    }}


def _frames_estimate(frames):
    return {"bitrate_mbps": 175, "duration": {
        "method": "timelapse_frames",
        "frames": {"source": "constant", "value": frames},
        "playback_fps": {"source": "constant", "value": 30},
    }}


def test_describe_preserves_decimal_reference():
    parsed = parse_exact_json(encode_describe_document(
        record_catalog(_direct_estimate(Decimal("130.125"))).describe_document()
    ).decode("utf-8"))
    parameter = parsed["devices"][0]["actions"][0]["parameter_types"][0]
    assert parameter["video_size_estimate"]["bitrate_mbps"] == Decimal("130.125")


@pytest.mark.parametrize("frames", [240, Decimal("240.0"), Decimal("2.4e2")])
def test_mathematical_integer_frames_survive_export(frames):
    payload = encode_describe_document(record_catalog(_frames_estimate(frames)).describe_document())
    parsed = parse_exact_json(payload.decode("utf-8"))
    parameter = parsed["devices"][0]["actions"][0]["parameter_types"][0]
    assert parameter["video_size_estimate"]["duration"]["frames"]["value"] == 240


@pytest.mark.parametrize("frames", [Decimal("240.5"), Decimal("1.00000000000000000001")])
def test_fractional_frames_cannot_be_rounded_into_integer(frames):
    document = record_catalog(_frames_estimate(frames)).describe_document()
    with pytest.raises(SchemaValidationError):
        encode_describe_document(document)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"),
                                   Decimal("-Infinity"), True, False, 130.0])
@pytest.mark.parametrize("position", ["bitrate", "seconds", "frames", "lookup"])
def test_invalid_source_number_rejects_entire_document(value, position):
    estimate = _direct_estimate()
    if position == "bitrate":
        estimate["bitrate_mbps"] = value
    elif position == "seconds":
        estimate["duration"]["seconds"]["value"] = value
    elif position == "frames":
        estimate = _frames_estimate(value)
    else:
        estimate["bitrate_mbps"] = {"by": "/mode", "values": {"high": value}}
    with pytest.raises(SchemaValidationError):
        encode_describe_document(record_catalog(estimate).describe_document())


def test_estimate_does_not_hide_invalid_parameter_type_association():
    capability = record_capability(_direct_estimate())
    capability.schema["properties"]["type"]["const"] = "different_type"
    with pytest.raises(RuleError):
        catalog_for_capabilities(capability).describe_document()


def test_estimate_does_not_hide_duplicate_parameter_types():
    capability = record_capability(_direct_estimate())
    duplicate = replace(capability, video_size_estimate=_direct_estimate(175))
    with pytest.raises(RuleError):
        catalog_for_capabilities(capability, duplicate)


def test_estimate_does_not_hide_inconsistent_action_identity():
    capability = record_capability(_direct_estimate())
    config = load_config({"devices": {
        "cam-1": {"kind": "camera", "driver": "estimate_demo"},
    }}, ConfigDefaults())
    definitions = DriverDefinitions({"estimate_demo": DriverDefinition(
        "estimate_demo", {"camera_timelapse": (capability,)},
    )})
    with pytest.raises(RuleError):
        build_catalog(config, definitions)


@pytest.mark.parametrize("estimate", [{}, {"bitrate_mbps": 130},
                                       {**_direct_estimate(), "unknown": 1}])
def test_invalid_estimate_rejects_export_even_with_another_valid_type(estimate):
    valid = record_capability(_direct_estimate())
    invalid = replace(valid, parameter_type="invalid_estimate", schema={
        **valid.schema, "properties": {"type": {"const": "invalid_estimate"}},
    }, video_size_estimate=estimate)
    document = catalog_for_capabilities(valid, invalid).describe_document()
    with pytest.raises(SchemaValidationError):
        encode_describe_document(document)
    assert len(document["devices"][0]["actions"][0]["parameter_types"]) == 2


def test_photo_estimate_rejects_entire_export():
    photo = replace(record_capability(_direct_estimate()), action_type="camera_take_photo")
    with pytest.raises(SchemaValidationError):
        encode_describe_document(catalog_for_capabilities(photo).describe_document())


def test_same_parameter_type_keeps_device_and_action_estimate_scope():
    record = record_capability(_direct_estimate(95))
    timelapse = replace(record, action_type="camera_timelapse", video_size_estimate=_direct_estimate(175))
    other_record = replace(record, video_size_estimate=_direct_estimate(130))
    other_timelapse = replace(timelapse, video_size_estimate=_direct_estimate(200))
    config = load_config({"devices": {
        "cam-1": {"kind": "camera", "driver": "first"},
        "cam-2": {"kind": "camera", "driver": "second"},
    }}, ConfigDefaults())
    definitions = DriverDefinitions({
        "first": DriverDefinition("first", {
            "camera_record": (record,), "camera_timelapse": (timelapse,),
        }),
        "second": DriverDefinition("second", {
            "camera_record": (other_record,), "camera_timelapse": (other_timelapse,),
        }),
    })
    document = build_catalog(config, definitions).describe_document()
    encode_describe_document(document)
    actual = {(device["device_id"], action["type"]):
              action["parameter_types"][0]["video_size_estimate"]["bitrate_mbps"]
              for device in document["devices"] for action in device["actions"]}
    assert actual == {
        ("cam-1", "camera_record"): 95, ("cam-1", "camera_timelapse"): 175,
        ("cam-2", "camera_record"): 130, ("cam-2", "camera_timelapse"): 200,
    }
