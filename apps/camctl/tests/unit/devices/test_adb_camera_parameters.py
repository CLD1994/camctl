"""候选参数的合法组合与曝光分支；不宣称真实设备已经支持。"""
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from camctl.acceptance.schema import BodySchemaError, validate_precise
from camctl.contracts.json_values import parse_exact_json
from camctl.devices.drivers.adb_cameras import definitions
from camctl.devices.drivers.adb_cameras import timelapse_parameters
from camctl.devices.drivers.adb_cameras.timelapse_parameters import (
    TIMELAPSE_DURATIONS_S, timelapse_intervals_s,
)
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


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
def test_action_record_accepts_omitted_aperture_without_a_default(exposure):
    params = {**recording_params(), "exposure": exposure}
    del params["aperture"]
    cap = capability("dji-action6", "camera_record")

    validate_precise(cap.schema, params)

    assert "aperture" not in params
    assert cap.defaults == {}


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("aperture", [None, "f8"])
def test_action_record_rejects_null_or_unknown_explicit_aperture(exposure, aperture):
    params = {**recording_params(), "exposure": exposure, "aperture": aperture}

    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-action6", "camera_record").schema, params)


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("omit_aperture", [False, True])
def test_action_record_accepts_omitted_bitrate_without_a_default(exposure, omit_aperture):
    params = {**recording_params(), "exposure": exposure}
    del params["bitrate"]
    if omit_aperture:
        del params["aperture"]
    cap = capability("dji-action6", "camera_record")

    validate_precise(cap.schema, params)

    assert "bitrate" not in params
    assert cap.defaults == {}


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("bitrate", [None, "ultra"])
def test_action_record_rejects_null_or_unknown_explicit_bitrate(exposure, bitrate):
    params = {**recording_params(), "exposure": exposure, "bitrate": bitrate}

    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-action6", "camera_record").schema, params)


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("field", [
    "type", "duration_s", "resolution", "fov", "stabilization", "exposure",
])
def test_action_record_without_aperture_or_bitrate_still_requires_other_fields(exposure, field):
    params = {**recording_params(), "exposure": exposure}
    del params["aperture"]
    del params["bitrate"]
    del params[field]

    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-action6", "camera_record").schema, params)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("duration_json", ["1", "1.0", "1e0", "10", "60", "9223372036854775"])
def test_record_duration_accepts_mathematical_integer_seconds(driver, duration_json):
    params = {**recording_params(driver), "duration_s": parse_exact_json(duration_json)}
    validate_precise(capability(driver, "camera_record").schema, params)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("duration_json", [
    "null", "true", '"10"', "0", "-1", "0.001", "1.234", "1.0005",
    "1.000000000000000000000000000001", "9223372036854775.001", "9223372036854776",
])
def test_invalid_record_duration_is_rejected(driver, duration_json):
    with pytest.raises(BodySchemaError):
        validate_precise(capability(driver, "camera_record").schema,
                         {**recording_params(driver), "duration_s": parse_exact_json(duration_json)})


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_record_duration_boundary_does_not_depend_on_decimal_context(driver):
    with localcontext() as context:
        context.prec = 3
        schema = capability(driver, "camera_record").schema
        validate_precise(schema, {**recording_params(driver), "duration_s": Decimal("9223372036854775.0")})
        with pytest.raises(BodySchemaError):
            validate_precise(schema, {**recording_params(driver), "duration_s": Decimal("9223372036854776.0")})


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_record_duration_is_required_without_a_default(driver):
    params = recording_params(driver)
    del params["duration_s"]
    cap = capability(driver, "camera_record")

    with pytest.raises(BodySchemaError):
        validate_precise(cap.schema, params)

    assert "duration_s" not in cap.defaults
    assert "default" not in cap.schema["properties"]["duration_s"]


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
    ("stabilization", "off"), ("bitrate", "high"), ("shutter", "1/60"), ("white_balance_k", 5200)])
def test_osmo_record_rejects_unopened_settings(field, value):
    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-osmo360-ii", "camera_record").schema,
                         {**recording_params("dji-osmo360-ii"), field: value})


@pytest.mark.parametrize("driver, interval, duration, outputs, exposure", [
    ("dji-action6", 30, 7200, "video", {"mode": "manual", "iso": 800}),
    ("dji-action6", 30, 7200, "video_raw", {"mode": "manual", "iso": 800}),
    ("dji-action6", 30, 7200, "video_jpeg", {"mode": "manual", "iso": 800}),
    ("dji-action6", 25, 1800, "video", {"mode": "auto"}),
    ("dji-action6", 25, 1800, "video_raw", {"mode": "auto"}),
    ("dji-action6", 25, 1800, "video_jpeg", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_raw", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_jpeg", {"mode": "auto"}),
    ("dji-osmo360-ii", 30, 600, "video", {"mode": "manual", "iso": 800}),
    ("dji-osmo360-ii", 40, 18000, "video", {"mode": "auto"}),
    ("dji-osmo360-ii", 8, 1800, "video", {"mode": "auto"}),
])
def test_timelapse_time_rules_are_independent_of_exposure_and_outputs(driver, interval, duration, outputs, exposure):
    cap = capability(driver, "camera_timelapse")
    validate_parameter_schema(cap.parameter_type, cap.schema)
    validate_precise(cap.schema, {"type": cap.parameter_type, "interval_s": interval,
        "duration_s": duration, "outputs": outputs, "exposure": exposure})
    assert cap.task_factory is None  # 正常结束及返回契约尚未取得。


@pytest.mark.parametrize("driver, interval, duration, outputs, exposure", [
    ("dji-action6", 30, 5400, "video", {"mode": "manual", "iso": 400}),
    ("dji-action6", 25, 6000, "video", {"mode": "auto"}),
    ("dji-action6", 8, 1800, "video_raw", {"mode": "manual", "iso": 400}),
    ("dji-osmo360-ii", 30, 5400, "video", {"mode": "manual", "iso": 800}),
    ("dji-osmo360-ii", 25, 6000, "video", {"mode": "auto"}),
    ("dji-osmo360-ii", 25, 7200, "video_raw", {"mode": "auto"}),
    ("dji-osmo360-ii", 8, 1800, "video_jpeg", {"mode": "auto"}),
])
def test_timelapse_rejects_invalid_duration_exposure_or_model_output(driver, interval, duration, outputs, exposure):
    cap = capability(driver, "camera_timelapse")
    with pytest.raises(BodySchemaError):
        validate_precise(cap.schema, {"type": cap.parameter_type, "interval_s": interval,
            "duration_s": duration, "outputs": outputs, "exposure": exposure})


def timelapse_params(driver, **changes):
    cap = capability(driver, "camera_timelapse")
    return {"type": cap.parameter_type, "interval_s": timelapse_intervals_s(driver)[0],
            "duration_s": 300, "outputs": "video", "exposure": {"mode": "auto"}, **changes}


@pytest.mark.parametrize("driver, interval, duration, accepted", [
    ("dji-action6", 1, 300, True),
    ("dji-osmo360-ii", 1, 300, False),
    ("dji-osmo360-ii", 2, 300, True),
    ("dji-action6", Decimal("0.5"), 300, False),
    ("dji-osmo360-ii", Decimal("0.5"), 300, False),
    ("dji-action6", 300, 300, False),
    ("dji-osmo360-ii", 300, 300, False),
    ("dji-action6", 3600, 3600, False),
    ("dji-osmo360-ii", 3600, 3600, False),
    ("dji-action6", 3600, 300, False),
    ("dji-osmo360-ii", 3600, 7200, True),
    ("dji-action6", 7, 300, False),
    ("dji-osmo360-ii", 7, 300, False),
    ("dji-action6", 1, 60, False),
    ("dji-osmo360-ii", 2, 60, False),
    ("dji-action6", 30, 5400, False),
    ("dji-osmo360-ii", 25, 6000, False),
    ("dji-action6", Decimal("0.50000000000000001"), 300, False),
    ("dji-osmo360-ii", Decimal("1.00000000000000001"), 300, False),
    ("dji-action6", parse_exact_json("1e0"), parse_exact_json("3e2"), True),
    ("dji-osmo360-ii", parse_exact_json("2e0"), parse_exact_json("3e2"), True),
])
def test_timelapse_exact_time_boundaries(driver, interval, duration, accepted):
    params = timelapse_params(driver, interval_s=interval, duration_s=duration)
    before = deepcopy(params)
    schema = capability(driver, "camera_timelapse").schema
    if accepted:
        validate_precise(schema, params)
    else:
        with pytest.raises(BodySchemaError):
            validate_precise(schema, params)
    assert params == before


def test_timelapse_interval_selector_is_model_specific_and_rejects_unknown_models():
    select_intervals = getattr(timelapse_parameters, "timelapse_intervals_s", None)
    assert callable(select_intervals), "延时时间参数必须提供按型号选择间隔的接口"

    action6 = select_intervals("dji-action6")
    osmo = select_intervals("dji-osmo360-ii")

    assert action6[0] == 1
    assert osmo[0] == 2
    assert osmo == action6[1:]
    with pytest.raises(ValueError):
        select_intervals("unknown-camera")


def test_osmo_timelapse_rejects_new_one_second_interval_without_mutating_input():
    params = timelapse_params("dji-osmo360-ii", interval_s=1, duration_s=300)
    before = deepcopy(params)

    with pytest.raises(BodySchemaError):
        validate_precise(capability("dji-osmo360-ii", "camera_timelapse").schema, params)

    assert params == before


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_timelapse_all_authoritative_time_pairs_follow_strict_mathematical_order(driver):
    intervals = timelapse_intervals_s(driver)
    durations = TIMELAPSE_DURATIONS_S
    expected_count = 134 if driver == "dji-action6" else 126
    assert len(intervals) == (18 if driver == "dji-action6" else 17)
    assert len(durations) == 8
    cap = capability(driver, "camera_timelapse")
    assert cap.schema["properties"]["interval_s"]["type"] == "number"
    accepted_time_pairs = []
    for interval in intervals:
        for duration in durations:
            params = timelapse_params(driver, interval_s=interval, duration_s=duration)
            try:
                validate_precise(cap.schema, params)
            except BodySchemaError:
                assert duration <= interval
            else:
                assert duration > interval
                accepted_time_pairs.append((interval, duration))
    assert len(accepted_time_pairs) == expected_count
    assert cap.defaults == {}
    assert cap.task_factory is None


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("field", ["type", "interval_s", "duration_s", "outputs", "exposure"])
def test_timelapse_requires_every_parameter_without_defaults(driver, field):
    params = timelapse_params(driver)
    del params[field]
    before = deepcopy(params)
    with pytest.raises(BodySchemaError):
        validate_precise(capability(driver, "camera_timelapse").schema, params)
    assert params == before


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("field, value", [
    ("interval_s", True), ("interval_s", "1"), ("interval_s", None),
    ("duration_s", True), ("duration_s", "300"), ("duration_s", None),
    ("duration_s", Decimal("300.00000000000000001")),
    ("exposure", {"mode": "auto", "compensation_ev": 0}),
    ("exposure", {"mode": "auto", "iso": 800}),
    ("exposure", {"mode": "manual"}),
    ("exposure", {"mode": "manual", "iso": True}),
    ("exposure", {"mode": "manual", "iso": "800"}),
    ("exposure", {"mode": "manual", "iso": 800, "compensation_ev": 0}),
    ("extra", 1),
])
def test_timelapse_rejects_wrong_types_branch_mixing_and_extra_fields(driver, field, value):
    with pytest.raises(BodySchemaError):
        validate_precise(capability(driver, "camera_timelapse").schema,
                         timelapse_params(driver, **{field: value}))


@pytest.mark.parametrize("exposure", [{"mode": "auto"}, {"mode": "manual", "iso": 800}])
@pytest.mark.parametrize("driver, interval_json, expected_interval", [
    ("dji-action6", "1", Decimal("1")),
    ("dji-action6", "1.0", Decimal("1.0")),
    ("dji-action6", "1e0", Decimal("1")),
    ("dji-osmo360-ii", "2", Decimal("2")),
    ("dji-osmo360-ii", "2.0", Decimal("2.0")),
    ("dji-osmo360-ii", "2e0", Decimal("2")),
])
def test_timelapse_preserves_exact_equivalent_input_and_exposure(driver, exposure, interval_json, expected_interval):
    params = timelapse_params(driver, interval_s=parse_exact_json(interval_json), exposure=exposure)
    before = deepcopy(params)
    cap = capability(driver, "camera_timelapse")
    validate_parameter_schema(cap.parameter_type, cap.schema)
    validate_precise(cap.schema, params)
    assert params["interval_s"] == expected_interval
    assert params == before


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
