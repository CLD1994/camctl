"""命令预期独立抄自交接表，保留参数大小写和前缀。"""
from dataclasses import replace
from decimal import Decimal
import shlex

import pytest

from camctl.acceptance.schema import BodySchemaError
from camctl.devices.drivers.adb_cameras import commands
from camctl.devices.drivers.adb_cameras import timelapse_parameters
from camctl.devices.drivers.adb_cameras.timelapse_parameters import (
    TIMELAPSE_DURATIONS_S,
    timelapse_intervals_s,
)
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from .test_adb_camera_parameters import recording_params, timelapse_params


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_record_setting_lookup_rejects_fractional_seconds_before_mapping(driver):
    params = {**recording_params(driver), "duration_s": Decimal("1.234")}

    with pytest.raises(BodySchemaError):
        commands.settings_for(driver, "camera_record", params)


@pytest.mark.parametrize("exposure, exposure_commands", [
    ({"mode": "manual", "iso": 800}, (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
    )),
    ({"mode": "auto", "compensation_ev": 0}, (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10",
    )),
])
def test_action_record_omitted_aperture_sends_only_other_requested_settings(exposure, exposure_commands):
    params = {**recording_params(), "resolution": "4k30", "exposure": exposure}
    del params["aperture"]
    expected = (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
        *exposure_commands,
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100",
        "simulate_device -s bitrate 2",
    )

    assert commands.settings_for("dji-action6", "camera_record", params) == tuple(
        tuple(shlex.split(command)) for command in expected)


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("aperture, payload", [("f2.8", "1801"), ("f4.0", "9001")])
def test_action_record_explicit_aperture_keeps_original_command_before_bitrate(exposure, aperture, payload):
    params = {**recording_params(), "exposure": exposure, "aperture": aperture}

    settings = commands.settings_for("dji-action6", "camera_record", params)

    assert settings[-2:] == (
        ("dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "0x26", payload),
        ("simulate_device", "-s", "bitrate", "2"),
    )


@pytest.mark.parametrize("exposure, exposure_commands", [
    ({"mode": "manual", "iso": 800}, (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
    )),
    ({"mode": "auto", "compensation_ev": 0}, (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0100",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2e 10",
    )),
])
@pytest.mark.parametrize("aperture, aperture_commands", [
    (None, ()),
    ("f2.8", ("dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x26 1801",)),
])
def test_action_record_omitted_bitrate_keeps_other_settings_without_simulation(
        exposure, exposure_commands, aperture, aperture_commands):
    params = {**recording_params(), "resolution": "4k30", "exposure": exposure}
    del params["bitrate"]
    if aperture is None:
        del params["aperture"]
    expected = (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 0xe1 01",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
        *exposure_commands,
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 42 3d",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010109000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010108000100",
        *aperture_commands,
    )

    assert commands.settings_for("dji-action6", "camera_record", params) == tuple(
        tuple(shlex.split(command)) for command in expected)


@pytest.mark.parametrize("exposure", [
    {"mode": "manual", "iso": 800},
    {"mode": "auto", "compensation_ev": 0},
])
@pytest.mark.parametrize("bitrate, value", [("standard", "1"), ("high", "2")])
def test_action_record_explicit_bitrate_keeps_original_simulation_command(exposure, bitrate, value):
    params = {**recording_params(), "exposure": exposure, "bitrate": bitrate}

    settings = commands.settings_for("dji-action6", "camera_record", params)

    assert settings[-1] == ("simulate_device", "-s", "bitrate", value)


@pytest.mark.parametrize("iso, payload", [(100, "03"), (200, "04"), (400, "05"),
                                        (800, "06"), (1600, "07"), (3200, "08")])
def test_action_iso_command_is_exact(iso, payload):
    settings = commands.settings_for("dji-action6", "camera_record",
        {**recording_params(), "exposure": {"mode": "manual", "iso": iso}})
    assert ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "2a", payload) in settings


@pytest.mark.parametrize("field, value, suffix", [
    ("resolution", "8k30", ("18", "3703000000")),
    ("resolution", "4k30", ("18", "1003000000")),
    ("fov", "wide", ("8e", "010109000101")),
    ("fov", "natural_wide", ("8e", "010109000105")),
    ("fov", "standard", ("8e", "010109000102")),
    ("stabilization", "off", ("8e", "010108000100")),
    ("stabilization", "rocksteady", ("8e", "010108000101")),
    ("aperture", "f2.8", ("0x26", "1801")),
    ("aperture", "f4.0", ("0x26", "9001")),
    ("bitrate", "standard", ("bitrate", "1")),
    ("bitrate", "high", ("bitrate", "2")),
])
def test_action_setting_selects_complete_literal(field, value, suffix):
    settings = commands.settings_for("dji-action6", "camera_record",
                                     {**recording_params(), field: value})
    assert suffix in [cmd[-2:] for cmd in settings]


@pytest.mark.parametrize("ev, suffix", [(Decimal("-0.7"), "0e"), (0, "10"), (Decimal("0.7"), "12")])
def test_action_auto_does_not_force_manual_exposure(ev, suffix):
    settings = commands.settings_for("dji-action6", "camera_record",
        {**recording_params(), "exposure": {"mode": "auto", "compensation_ev": ev}})
    assert ("0x2e", suffix) in [cmd[-2:] for cmd in settings]
    assert ("0x2c", "0634000000") in [cmd[-2:] for cmd in settings]
    assert not any(cmd[-2] in {"2a", "28"} for cmd in settings)


@pytest.mark.parametrize("exposure, exposure_commands", [
    ({"mode": "manual", "iso": 800}, (
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "8e", "010100000101"),
        ("dji_mb_ctrl", "-r", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "1e", "0400"),
        ("dji_mb_ctrl", "-r", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "2A", "06"),
        ("dji_mb_ctrl", "-r", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "28", "013c8000"),
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "2c", "0634000000"),
    )),
    ({"mode": "auto", "compensation_ev": 0}, (
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "8e", "010100000101"),
        ("dji_mb_ctrl", "-r", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "1e", "0100"),
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "2c", "0634000000"),
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "2e", "10"),
    )),
])
def test_osmo_record_setting_requests_keep_independent_bytes_and_order(exposure, exposure_commands):
    params = {**recording_params("dji-osmo360-ii"), "exposure": {"mode": exposure["mode"]}}
    settings = commands.settings_for("dji-osmo360-ii", "camera_record", params)
    expected = (
        ("dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "0x8e", "01013f000101"),
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "e1", "38"),
        ("simulate_device", "-s", "BaseFormat", "6", "69", "0"),
        *exposure_commands,
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "42", "3d"),
        ("simulate_device", "-s", "DeviceRecordRecSettingBitRate", "2"),
    )

    assert settings == expected


def test_fixed_request_fact_and_command_are_consumed_from_the_same_task_declaration(monkeypatch):
    plans = getattr(commands, "FIXED_SETTING_PLANS", None)
    assert plans is not None, "fixed settings need one shared fact-and-command declaration"

    key = (commands.CameraModel.OSMO360II, "camera_record")
    plan = plans[key]
    manual_steps = plan.exposure["manual"]
    shutter_index = next(
        index for index, step in enumerate(manual_steps)
        if isinstance(step, commands.FixedSettingRequest) and step.kind == "shutter"
    )
    original_shutter = manual_steps[shutter_index]
    changed_shutter = replace(
        original_shutter,
        value="1/30",
        commands=(("probe", "changed-shutter"),),
    )
    changed_steps = (*manual_steps[:shutter_index], changed_shutter, *manual_steps[shutter_index + 1:])
    monkeypatch.setitem(
        plans,
        key,
        replace(plan, exposure={**plan.exposure, "manual": changed_steps}),
    )

    encoded = commands.settings_for(
        "dji-osmo360-ii",
        "camera_record",
        recording_params("dji-osmo360-ii"),
    )
    from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities
    capability = next(
        item for item in candidate_capabilities("dji-osmo360-ii")
        if item.action_type == "camera_record"
    )

    assert ("probe", "changed-shutter") in encoded
    assert "1/30 秒" in capability.description
    assert "1/60 秒" not in capability.description
    exposure_description = capability.schema["properties"]["exposure"]["oneOf"][0][
        "properties"]["mode"]["description"]
    assert "1/30 秒" in exposure_description
    assert "1/60 秒" not in exposure_description


@pytest.mark.parametrize("driver, interval, duration, outputs, exposure, payload", [
    ("dji-action6", 30, 5400, "video", {"mode": "manual", "iso": 800}, "0400002c01181500000000000000000000"),
    ("dji-action6", 30, 5400, "video_raw", {"mode": "manual", "iso": 800}, "0400032c01181500000000000000000000"),
    ("dji-action6", 30, 5400, "video_jpeg", {"mode": "manual", "iso": 800}, "0400022c01181500000000000000000000"),
    ("dji-action6", 25, 6000, "video", {"mode": "auto"}, "040000fa00701700000000000000000000"),
    ("dji-action6", 25, 6000, "video_raw", {"mode": "auto"}, "040003fa00701700000000000000000000"),
    ("dji-action6", 25, 6000, "video_jpeg", {"mode": "auto"}, "040002fa00701700000000000000000000"),
    ("dji-action6", 8, 1800, "video", {"mode": "auto"}, "0400005000080700000000000000000000"),
    ("dji-action6", 8, 1800, "video_raw", {"mode": "auto"}, "0400035000080700000000000000000000"),
    ("dji-action6", 8, 1800, "video_jpeg", {"mode": "auto"}, "0400025000080700000000000000000000"),
    ("dji-osmo360-ii", 30, 600, "video", {"mode": "manual", "iso": 800}, "0407002c015802000000000000000001"),
    ("dji-osmo360-ii", 30, 1200, "video", {"mode": "manual", "iso": 800}, "0407002c01b004000000000000000001"),
    ("dji-osmo360-ii", 30, 1800, "video", {"mode": "manual", "iso": 800}, "0407002c010807000000000000000001"),
    ("dji-osmo360-ii", 30, 3600, "video", {"mode": "manual", "iso": 800}, "0407002c01100e000000000000000001"),
    ("dji-osmo360-ii", 30, 7200, "video", {"mode": "manual", "iso": 800}, "0407002c01201c000000000000000001"),
    ("dji-osmo360-ii", 30, 10800, "video", {"mode": "manual", "iso": 800}, "0407002c01302a000000000000000001"),
    ("dji-osmo360-ii", 30, 18000, "video", {"mode": "manual", "iso": 800}, "0407002c015046000000000000000001"),
    ("dji-osmo360-ii", 40, 18000, "video", {"mode": "auto"}, "04000090015046000000000000000000"),
    ("dji-osmo360-ii", 8, 1800, "video", {"mode": "auto"}, "04000050000807000000000000000000"),
])
def test_timelapse_preserves_given_payloads_and_rejects_legacy_new_requests(driver, interval, duration, outputs, exposure, payload):
    params = _candidate_params(driver, outputs, exposure["mode"], interval, duration)
    if duration in (5400, 6000):
        with pytest.raises(BodySchemaError):
            commands.settings_for(driver, "camera_timelapse", params)
        settings = commands.settings_from_effective_params(driver, "camera_timelapse", params)
        assert [command[-2:] for command in settings if command[-2] == "6c"] == [("6c", payload)]
        return
    settings = commands.settings_for(driver, "camera_timelapse", params)
    preset_position = 2 if driver == "dji-action6" else -2
    assert settings[preset_position] == ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "6c", payload)
    if driver == "dji-osmo360-ii" and exposure["mode"] == "auto":
        assert ("8e", "010100000100") in [cmd[-2:] for cmd in settings]
        assert ("1e", "0000") in [cmd[-2:] for cmd in settings]


@pytest.mark.parametrize("interval, duration, outputs, exposure, payload", [
    (8, 1800, "video", {"mode": "auto"},
     "0400005000080700000000000000000000"),
    (8, 1800, "video_raw", {"mode": "auto"},
     "0400035000080700000000000000000000"),
    (8, 1800, "video_jpeg", {"mode": "auto"},
     "0400025000080700000000000000000000"),
])
def test_action_timelapse_applies_selected_full_preset_once_before_exposure(
        interval, duration, outputs, exposure, payload):
    params = {"type": "action6_timelapse", "interval_s": interval,
              "duration_s": duration, "outputs": outputs, "exposure": exposure}
    exposure_commands = (
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 8e 010100000101",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 1E 0400",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 2a 06",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 28 013C8000",
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x2c 0634000000",
    ) if exposure["mode"] == "manual" else (
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c 0x1e 0100",
    )
    expected = (
        "dji_mb_ctrl -S test -R diag -g 1 -t 0 -s 2 -c e1 02",
        "dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 18 1003000000",
        f"dji_mb_ctrl -R diag -g 1 -t 0 -s 2 -c 6c {payload}",
        *exposure_commands,
    )
    assert commands.settings_for("dji-action6", "camera_timelapse", params) == tuple(
        tuple(shlex.split(command)) for command in expected)


ACTION6_CANDIDATE_TEMPLATES = (
    ("video", "manual", "0400002c01181500000000000000000000"),
    ("video_raw", "manual", "0400032c01181500000000000000000000"),
    ("video_jpeg", "manual", "0400022c01181500000000000000000000"),
    ("video", "auto", "0400005000080700000000000000000000"),
    ("video_raw", "auto", "0400035000080700000000000000000000"),
    ("video_jpeg", "auto", "0400025000080700000000000000000000"),
)
OSMO_CANDIDATE_TEMPLATES = (
    ("video", "manual", "0407002c015802000000000000000001"),
    ("video", "auto", "04000050000807000000000000000000"),
)
ALL_CANDIDATE_TEMPLATES = (
    *(("dji-action6", *case) for case in ACTION6_CANDIDATE_TEMPLATES),
    *(("dji-osmo360-ii", *case) for case in OSMO_CANDIDATE_TEMPLATES),
)


def _candidate_params(driver, outputs, exposure_mode, interval_s, duration_s):
    parameter_type = "action6_timelapse" if driver == "dji-action6" else "osmo360ii_timelapse"
    exposure = ({"mode": "manual", "iso": 800} if exposure_mode == "manual"
                else {"mode": "auto"})
    if driver == "dji-osmo360-ii":
        return {"type": parameter_type, "interval_s": interval_s, "duration_s": duration_s,
                "bitrate": "high", "exposure": {"mode": exposure_mode}}
    return {"type": parameter_type, "interval_s": interval_s, "duration_s": duration_s,
            "outputs": outputs, "exposure": exposure}


def _payload_from_settings(settings):
    payload_commands = [command for command in settings if command[-2] == "6c"]
    assert len(payload_commands) == 1
    return bytes.fromhex(payload_commands[0][-1])


def _narrow_time_domain(monkeypatch, field, excluded, model):
    if field == "interval_s":
        original = timelapse_parameters.timelapse_intervals_s
        narrowed = tuple(value for value in original(model) if value != excluded)
        assert len(narrowed) == len(original(model)) - 1

        def current_intervals(requested_model):
            return narrowed if requested_model == model else original(requested_model)

        monkeypatch.setattr(timelapse_parameters, "timelapse_intervals_s", current_intervals)
        return
    narrowed = tuple(value for value in TIMELAPSE_DURATIONS_S if value != excluded)
    assert len(narrowed) == len(TIMELAPSE_DURATIONS_S) - 1
    monkeypatch.setattr(timelapse_parameters, "TIMELAPSE_DURATIONS_S", narrowed)


STABLE_FORMAT_CASES = (
    ("dji-action6", 1, 300, "0400000a002c0100000000000000000000"),
    ("dji-action6", 2, 600, "0400001400580200000000000000000000"),
    ("dji-osmo360-ii", 2, 600, "04000014005802000000000000000000"),
)


@pytest.mark.parametrize("driver, interval, duration, expected", STABLE_FORMAT_CASES)
@pytest.mark.parametrize("narrow_field", ["interval_s", "duration_s"])
def test_trusted_timelapse_encoding_keeps_first_format_after_current_domain_narrows(
        monkeypatch, driver, interval, duration, expected, narrow_field):
    params = _candidate_params(driver, "video", "auto", interval, duration)
    assert _payload_from_settings(commands.settings_for(driver, "camera_timelapse", params)) == bytes.fromhex(expected)

    _narrow_time_domain(monkeypatch, narrow_field,
                        interval if narrow_field == "interval_s" else duration, driver)

    with pytest.raises(BodySchemaError):
        commands.settings_for(driver, "camera_timelapse", params)
    assert _payload_from_settings(
        commands.settings_from_effective_params(driver, "camera_timelapse", params)
    ) == bytes.fromhex(expected)


def test_trusted_first_format_can_explain_1_301_while_new_request_still_uses_current_schema():
    params = _candidate_params("dji-action6", "video", "auto", 1, 301)

    with pytest.raises(BodySchemaError):
        commands.settings_for("dji-action6", "camera_timelapse", params)
    assert _payload_from_settings(
        commands.settings_from_effective_params("dji-action6", "camera_timelapse", params)
    ) == bytes.fromhex("0400000a002d0100000000000000000000")


@pytest.mark.parametrize("outputs, exposure_mode, template", ACTION6_CANDIDATE_TEMPLATES)
def test_action6_timelapse_candidate_covers_every_allowed_time_pair_and_preserves_template(
        outputs, exposure_mode, template):
    pairs = [(interval, duration) for interval in timelapse_intervals_s("dji-action6")
             for duration in TIMELAPSE_DURATIONS_S if duration > interval]
    assert len(pairs) == 134
    template_bytes = bytes.fromhex(template)

    for interval, duration in pairs:
        settings = commands.settings_for(
            "dji-action6", "camera_timelapse",
            _candidate_params("dji-action6", outputs, exposure_mode, interval, duration),
        )
        payload = _payload_from_settings(settings)
        assert len(payload) == 17
        assert payload[:3] == template_bytes[:3]
        assert payload[3:5] == (interval * 10).to_bytes(2, "little")
        assert payload[5:7] == duration.to_bytes(2, "little")
        assert payload[7:] == template_bytes[7:]


@pytest.mark.parametrize("outputs, exposure_mode, template", OSMO_CANDIDATE_TEMPLATES)
def test_osmo_timelapse_candidate_covers_every_allowed_time_pair_and_preserves_template(
        outputs, exposure_mode, template):
    pairs = [(interval, duration) for interval in timelapse_intervals_s("dji-osmo360-ii")
             for duration in TIMELAPSE_DURATIONS_S if duration > interval]
    assert len(pairs) == 126
    template_bytes = bytes.fromhex(template)

    for interval, duration in pairs:
        settings = commands.settings_for(
            "dji-osmo360-ii", "camera_timelapse",
            _candidate_params("dji-osmo360-ii", outputs, exposure_mode, interval, duration),
        )
        payload = _payload_from_settings(settings)
        assert len(payload) == 16
        assert payload[:3] == template_bytes[:3]
        assert payload[3:5] == (interval * 10).to_bytes(2, "little")
        assert payload[5:7] == duration.to_bytes(2, "little")
        assert payload[7:] == template_bytes[7:]


@pytest.mark.parametrize("interval, duration, expected", [
    (1, 300, "0400030a002c0100000000000000000000"),
    (30, 600, "0400032c01580200000000000000000000"),
    (3600, 18000, "040003a08c504600000000000000000000"),
])
def test_timelapse_candidate_encodes_shortest_carry_and_maximum_values(interval, duration, expected):
    settings = commands.settings_for(
        "dji-action6", "camera_timelapse",
        _candidate_params("dji-action6", "video_raw", "auto", interval, duration),
    )

    assert _payload_from_settings(settings) == bytes.fromhex(expected)


@pytest.mark.parametrize("interval", [1, Decimal("1.0"), Decimal("1e0")])
def test_timelapse_candidate_treats_exactly_equal_interval_numbers_as_the_same_value(interval):
    settings = commands.settings_for(
        "dji-action6", "camera_timelapse",
        _candidate_params("dji-action6", "video", "auto", interval, 300),
    )

    assert _payload_from_settings(settings) == bytes.fromhex(
        "0400000a002c0100000000000000000000")


def test_timelapse_candidate_rejects_neighboring_unequal_interval_value():
    with pytest.raises(BodySchemaError):
        commands.settings_for(
            "dji-action6", "camera_timelapse",
            _candidate_params("dji-action6", "video", "auto", Decimal("1.00000000000000001"), 300),
        )


def test_osmo_new_one_second_request_is_rejected_but_saved_parameters_keep_original_bytes():
    params = _candidate_params("dji-osmo360-ii", "video", "auto", 1, 300)

    with pytest.raises(BodySchemaError):
        commands.settings_for("dji-osmo360-ii", "camera_timelapse", params)

    settings = commands.settings_from_effective_params("dji-osmo360-ii", "camera_timelapse", params)
    payloads = [command[-1] for command in settings if command[-2] == "6c"]
    assert payloads == ["0400000a002c01000000000000000000"]


AUTO_TEMPLATE_FAILURE_CASES = (
    ("dji-action6", "video", "040000fa00701700000000000000000000", 25, 6000),
    ("dji-action6", "video_raw", "040003fa00701700000000000000000000", 25, 6000),
    ("dji-action6", "video_jpeg", "040002fa00701700000000000000000000", 25, 6000),
    ("dji-osmo360-ii", "video", "04000090015046000000000000000000", 40, 18000),
)


@pytest.mark.parametrize("driver, outputs, legacy_payload, legacy_interval, legacy_duration",
                         AUTO_TEMPLATE_FAILURE_CASES)
@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_auto_candidate_requires_its_fixed_template_and_keeps_exact_legacy_literal(
        monkeypatch, driver, outputs, legacy_payload, legacy_interval, legacy_duration, damage):
    model = commands.CameraModel(driver)
    presets = commands.TIMELAPSE_PRESETS[model]
    fixed = next(preset for preset in presets
                 if (preset.interval_s, preset.duration_s, preset.outputs.value, preset.exposure_mode)
                 == (8, 1800, outputs, "auto"))
    if damage == "missing":
        changed = tuple(preset for preset in presets if preset is not fixed)
        error = CapabilityNotDeclaredError
    else:
        malformed = replace(fixed, command=(*fixed.command[:-1], "not-hex"))
        changed = tuple(malformed if preset is fixed else preset for preset in presets)
        error = ValueError
    monkeypatch.setitem(commands.TIMELAPSE_PRESETS, model, changed)

    with pytest.raises(error):
        commands.settings_for(driver, "camera_timelapse",
                              _candidate_params(driver, outputs, "auto",
                                                1 if driver == "dji-action6" else 2, 300))

    legacy_params = _candidate_params(driver, outputs, "auto", legacy_interval, legacy_duration)
    assert _payload_from_settings(
        commands.settings_from_effective_params(driver, "camera_timelapse", legacy_params)
    ) == bytes.fromhex(legacy_payload)


@pytest.mark.parametrize("driver, outputs, exposure, template",
                         ALL_CANDIDATE_TEMPLATES)
def test_candidate_template_identity_is_independent_of_history_order(
        monkeypatch, driver, outputs, exposure, template):
    model = commands.CameraModel(driver)
    fixed_interval, fixed_duration = (
        (30, 5400) if driver == "dji-action6" and exposure == "manual" else
        (8, 1800) if exposure == "auto" else
        (30, 600)
    )
    presets = commands.TIMELAPSE_PRESETS[model]
    changed = []
    for preset in presets:
        same_partition = preset.outputs.value == outputs and preset.exposure_mode == exposure
        is_fixed = (preset.interval_s, preset.duration_s) == (fixed_interval, fixed_duration)
        if same_partition and not is_fixed:
            marked = bytearray.fromhex(preset.command[-1])
            marked[0] ^= 0x80
            preset = replace(preset, command=(*preset.command[:-1], marked.hex()))
        changed.append(preset)

    for ordered in (tuple(changed), tuple(reversed(changed))):
        monkeypatch.setitem(commands.TIMELAPSE_PRESETS, model, ordered)
        actual = _payload_from_settings(commands.settings_for(
            driver, "camera_timelapse",
            _candidate_params(driver, outputs, exposure, 1 if model is commands.CameraModel.ACTION6 else 2, 300)))
        expected = bytearray.fromhex(template)
        expected[3:5] = (10 if model is commands.CameraModel.ACTION6 else 20).to_bytes(2, "little")
        expected[5:7] = (300).to_bytes(2, "little")
        assert actual == bytes(expected)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_timelapse_missing_template_is_a_mapping_error(monkeypatch, driver):
    model = commands.CameraModel(driver)
    presets = commands.TIMELAPSE_PRESETS[model]
    monkeypatch.setitem(commands.TIMELAPSE_PRESETS, model, tuple(
        preset for preset in presets
        if not (preset.outputs is commands.OutputSelection.VIDEO and preset.exposure_mode == "auto")
    ))

    with pytest.raises(CapabilityNotDeclaredError):
        commands.settings_for(
            driver, "camera_timelapse",
            _candidate_params(driver, "video", "auto", 1 if driver == "dji-action6" else 2, 300),
        )


def test_timelapse_bad_template_is_rejected_before_returning_settings(monkeypatch):
    model = commands.CameraModel.ACTION6
    presets = commands.TIMELAPSE_PRESETS[model]
    selected = next(preset for preset in presets
                    if (preset.interval_s, preset.duration_s, preset.outputs, preset.exposure_mode)
                    == (8, 1800, commands.OutputSelection.VIDEO_RAW, "auto"))
    malformed = replace(selected, command=(*selected.command[:-1], "not-hex"))
    monkeypatch.setitem(commands.TIMELAPSE_PRESETS, model, tuple(
        malformed if preset is selected else preset for preset in presets
    ))

    with pytest.raises(ValueError):
        commands.settings_for(
            "dji-action6", "camera_timelapse",
            _candidate_params("dji-action6", "video_raw", "auto", 8, 1800),
        )


def test_timelapse_effective_interval_that_exceeds_replacement_window_is_rejected():
    params = _candidate_params("dji-action6", "video", "auto", 6554, 10800)

    with pytest.raises(ValueError):
        commands.settings_from_effective_params("dji-action6", "camera_timelapse", params)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
def test_timelapse_invalid_parameters_are_distinct_from_missing_mapping(driver):
    with pytest.raises(BodySchemaError):
        commands.settings_for(driver, "camera_timelapse", timelapse_params(driver, interval_s=300))


@pytest.mark.parametrize("driver, action, control, suffix", [
    ("dji-action6", "camera_record", "start", ("02", "01")),
    ("dji-action6", "camera_record", "stop", ("02", "00")),
    ("dji-action6", "camera_timelapse", "start", ("01", "01")),
    ("dji-osmo360-ii", "camera_record", "start", ("02", "01")),
    ("dji-osmo360-ii", "camera_record", "stop", ("02", "00")),
    ("dji-osmo360-ii", "camera_timelapse", "start", ("01", "01")),
    ("dji-osmo360-ii", "camera_timelapse", "stop", ("01", "00")),
])
def test_control_literals(driver, action, control, suffix):
    assert getattr(commands, f"{control}_for")(driver, action)[-2:] == suffix


def test_action_timelapse_stop_remains_unavailable():
    with pytest.raises(ValueError):
        commands.stop_for("dji-action6", "camera_timelapse")


def test_uncertain_commands_remain_named_candidates():
    pending = commands.pending_commands("dji-osmo360-ii")
    assert any(item.command[-1] == "040000fa00201c000000000000000000" and item.reason for item in pending)
    assert any(item.command[-1] == "010109000100" and item.reason for item in pending)
    pending = commands.pending_commands("dji-action6")
    assert not any(item.command[-2] == "6c" for item in pending)
    assert any(item.command[-1] == "9001" and item.reason for item in pending)


def test_command_lookup_rejects_unknown_binding_and_unvalidated_parameters():
    with pytest.raises(ValueError):
        commands.start_for("other-camera", "camera_record")
    with pytest.raises(ValueError):
        commands.settings_for("dji-action6", "camera_record", {**recording_params(), "aperture": "f8"})


@pytest.mark.parametrize("patch", [
    {"type": "osmo360ii_timelapse"}, {"extra": 1}, {"interval_s": True},
    {"interval_s": "30"}, {"interval_s": Decimal("NaN")}, {"interval_s": 30.0},
    {"duration_s": False}, {"duration_s": "5400"}, {"duration_s": Decimal("5400.001")},
    {"exposure": None}, {"exposure": {"mode": "manual", "iso": 400}},
    {"exposure": {"mode": "auto", "compensation_ev": 0}},
    {"exposure": {"mode": "manual", "iso": True}}, {"outputs": []},
])
def test_original_effective_encoding_keeps_structure_and_model_checks(patch):
    params = {"type": "action6_timelapse", "interval_s": 30, "duration_s": 5400,
              "outputs": "video", "exposure": {"mode": "manual", "iso": 800}, **patch}
    with pytest.raises(ValueError):
        commands.settings_from_effective_params("dji-action6", "camera_timelapse", params)


def test_original_effective_encoding_distinguishes_missing_mapping():
    params = {"type": "action6_timelapse", "interval_s": Decimal("1"), "duration_s": Decimal("301"),
              "outputs": "unmapped-output", "exposure": {"mode": "auto"}}
    with pytest.raises(CapabilityNotDeclaredError):
        commands.settings_from_effective_params("dji-action6", "camera_timelapse", params)


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("patch", [
    {"type": "action6_timelapse"}, {"duration_s": True}, {"duration_s": Decimal("10.001")},
    {"exposure": {"mode": "manual", "iso": True}},
    {"exposure": {"mode": "auto", "compensation_ev": False}},
])
def test_original_record_encoding_rejects_uninterpretable_structure(driver, patch):
    with pytest.raises(ValueError):
        commands.settings_from_effective_params(driver, "camera_record", {**recording_params(driver), **patch})


def test_original_record_encoding_reports_absent_mapping_without_changing_parameter():
    params = {**recording_params(), "resolution": "unmapped"}
    with pytest.raises(CapabilityNotDeclaredError):
        commands.settings_from_effective_params("dji-action6", "camera_record", params)
    assert params["resolution"] == "unmapped"
