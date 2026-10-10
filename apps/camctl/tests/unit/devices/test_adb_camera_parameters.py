"""候选参数的合法组合与曝光分支；不宣称真实设备已经支持。"""
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from camctl.acceptance.schema import BodySchemaError, validate_precise
from camctl.devices.drivers.adb_cameras import definitions
from camctl.devices.parameter_schemas import validate_parameter_schema


def recording_params(driver="dji-action6"):
    if driver == "dji-action6":
        return {"type": "action6_record", "duration_s": 10,
                "resolution": "8k30", "fov": "wide", "stabilization": "off",
                "aperture": "f2.8", "bitrate": "high",
                "exposure": {"mode": "manual", "iso": 800}}
    return {"type": "osmo360ii_record", "duration_s": 10,
            "exposure": {"mode": "manual", "iso": 800}}


def capability(driver, action_type):
    return next(cap for cap in definitions.candidate_capabilities(driver)
                if cap.action_type == action_type)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_candidate_schema_is_self_contained_and_accepts_recording(driver):
    cap = capability(driver, "camera_record")
    validate_parameter_schema(cap.parameter_type, cap.schema)
    validate_precise(cap.schema, recording_params(driver))
    assert cap.defaults == {}


@pytest.mark.parametrize("duration", [Decimal("0.001"), 10, 60, Decimal("1.234")])
def test_record_duration_uses_exact_milliseconds_without_example_limit(duration):
    params = {**recording_params(), "duration_s": duration}
    validate_precise(capability("dji-action6", "camera_record").schema, params)


@pytest.mark.parametrize("duration", [None, True, "10", 0, -1, Decimal("1.0005"), 9223372036854776])
def test_invalid_record_duration_is_rejected(duration):
    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-action6", "camera_record").schema,
                         {**recording_params(), "duration_s": duration})


def test_record_duration_boundary_does_not_depend_on_decimal_context():
    with localcontext() as context:
        context.prec = 3
        schema = capability("dji-action6", "camera_record").schema
        validate_precise(schema, {**recording_params(), "duration_s": Decimal("9223372036854775.807")})
        with pytest.raises(BodySchemaError):
            validate_precise(schema, {**recording_params(), "duration_s": Decimal("9223372036854775.808")})


@pytest.mark.parametrize("exposure", [None, {}, {"mode": "manual"},
    {"mode": "auto", "iso": 800, "compensation_ev": 0},
    {"mode": "manual", "iso": 800, "compensation_ev": 0},
    {"mode": "auto"}, {"mode": "auto", "compensation_ev": 1},
    {"mode": "manual", "iso": 800, "shutter": "1/60"},
    {"mode": "manual", "iso": 800, "white_balance_k": 5200}])
def test_record_exposure_rejects_missing_inapplicable_or_unopened_fields(exposure):
    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-action6", "camera_record").schema,
                         {**recording_params(), "exposure": exposure})


@pytest.mark.parametrize("field, value", [("resolution", "4k30"),
    ("fov", "natural_wide"), ("fov", "standard"),
    ("stabilization", "rocksteady"), ("aperture", "f4.0"),
    ("bitrate", "standard")])
def test_discrete_action_record_candidates_are_accepted(field, value):
    validate_precise(capability("dji-action6", "camera_record").schema,
                     {**recording_params(), field: value})


@pytest.mark.parametrize("iso", [100, 200, 400, 800, 1600, 3200])
def test_action_manual_iso_candidates(iso):
    validate_precise(capability("dji-action6", "camera_record").schema,
                     {**recording_params(), "exposure": {"mode": "manual", "iso": iso}})


@pytest.mark.parametrize("ev", [Decimal("-0.7"), 0, Decimal("0.7")])
def test_action_auto_compensation_candidates(ev):
    validate_precise(capability("dji-action6", "camera_record").schema,
                     {**recording_params(), "exposure": {"mode": "auto", "compensation_ev": ev}})


@pytest.mark.parametrize("field, value", [("fov", "wide"), ("aperture", "f2.8"),
    ("stabilization", "off"), ("shutter", "1/60"), ("white_balance_k", 5200)])
def test_osmo_record_rejects_unopened_settings(field, value):
    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-osmo360-ii", "camera_record").schema,
                         {**recording_params("dji-osmo360-ii"), field: value})


@pytest.mark.parametrize("driver, interval, duration, outputs, exposure", [
    ("dji-action6", 30, 5400, "video", {"mode": "manual", "iso": 800}),
    ("dji-action6", 30, 5400, "video_raw", {"mode": "manual", "iso": 800}),
    ("dji-action6", 30, 5400, "video_jpeg", {"mode": "manual", "iso": 800}),
    ("dji-action6", 25, 6000, "video", {"mode": "auto"}),
    ("dji-action6", 25, 6000, "video_raw", {"mode": "auto"}),
    ("dji-action6", 25, 6000, "video_jpeg", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_raw", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_jpeg", {"mode": "auto"}),
    ("dji-osmo360-ii", 30, 600, "video", {"mode": "manual", "iso": 800}),
    ("dji-osmo360-ii", 40, 18000, "video", {"mode": "auto"}),
    ("dji-osmo360-ii", 8, 1800, "video", {"mode": "auto"}),
])
def test_timelapse_accepts_only_documented_complete_combinations(driver, interval, duration, outputs, exposure):
    cap = capability(driver, "camera_timelapse")
    validate_parameter_schema(cap.parameter_type, cap.schema)
    validate_precise(cap.schema, {"type": cap.parameter_type, "interval_s": interval,
        "duration_s": duration, "outputs": outputs, "exposure": exposure})
    assert cap.task_factory is None  # 正常结束及返回契约尚未取得。


@pytest.mark.parametrize("driver, interval, duration, outputs, exposure", [
    ("dji-action6", 30, 5400, "video", {"mode": "manual", "iso": 400}),
    ("dji-action6", 25, 1800, "video", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_raw", {"mode": "manual", "iso": 800}),
    ("dji-osmo360-ii", 30, 5400, "video", {"mode": "manual", "iso": 800}),
    ("dji-osmo360-ii", 25, 6000, "video", {"mode": "auto"}),
    ("dji-osmo360-ii", 25, 7200, "video", {"mode": "auto"}),
    ("dji-osmo360-ii", 8, 1800, "video_jpeg", {"mode": "auto"}),
])
def test_timelapse_does_not_expand_or_resolve_uncertain_combinations(driver, interval, duration, outputs, exposure):
    cap = capability(driver, "camera_timelapse")
    with pytest.raises(BodySchemaError):
        validate_precise(cap.schema, {"type": cap.parameter_type, "interval_s": interval,
            "duration_s": duration, "outputs": outputs, "exposure": exposure})


def test_candidate_definitions_are_independent_copies():
    cap = capability("dji-action6", "camera_record")
    before = deepcopy(cap.schema)
    cap.schema["required"].clear()
    assert capability("dji-action6", "camera_record").schema == before


@pytest.mark.parametrize("driver, directories", [
    ("dji-action6", ["/mnt/media_rw/emulated/DCIM", "/mnt/media_rw/sd/DCIM"]),
    ("dji-osmo360-ii", ["/mnt/media_rw/emulated/DCIM"]),
])
def test_output_scope_preserves_declared_directory_roots(driver, directories):
    assert definitions.output_scope_for(driver) == {"directories": directories}
