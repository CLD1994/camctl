"""命令预期独立抄自交接表，保留参数大小写和前缀。"""
from decimal import Decimal
import shlex

import pytest

from camctl.devices.drivers.adb_cameras import commands
from .test_adb_camera_parameters import recording_params


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


def test_osmo_manual_settings_keep_own_flags_and_no_unprovided_adjustments():
    settings = commands.settings_for("dji-osmo360-ii", "camera_record", recording_params("dji-osmo360-ii"))
    assert settings[:3] == (
        ("dji_mb_ctrl", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "0x8e", "01013f000101"),
        ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "e1", "38"),
        ("simulate_device", "-s", "BaseFormat", "6", "69", "0"),
    )
    assert ("dji_mb_ctrl", "-r", "-S", "test", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "28", "013c8000") in settings
    assert ("DeviceRecordRecSettingBitRate", "2") in [cmd[-2:] for cmd in settings]
    assert not any(cmd[-2] == "0x26" or cmd[-1].startswith("010108") for cmd in settings)


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
def test_timelapse_uses_full_given_payload(driver, interval, duration, outputs, exposure, payload):
    params = {"type": "action6_timelapse" if driver == "dji-action6" else "osmo360ii_timelapse",
              "interval_s": interval, "duration_s": duration, "outputs": outputs, "exposure": exposure}
    settings = commands.settings_for(driver, "camera_timelapse", params)
    preset_position = 2 if driver == "dji-action6" else -1
    assert settings[preset_position] == ("dji_mb_ctrl", "-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c", "6c", payload)
    if driver == "dji-osmo360-ii" and exposure["mode"] == "auto":
        assert ("8e", "010100000100") in [cmd[-2:] for cmd in settings]
        assert ("1e", "0000") in [cmd[-2:] for cmd in settings]


@pytest.mark.parametrize("interval, duration, outputs, exposure, payload", [
    (30, 5400, "video", {"mode": "manual", "iso": 800},
     "0400002c01181500000000000000000000"),
    (30, 5400, "video_raw", {"mode": "manual", "iso": 800},
     "0400032c01181500000000000000000000"),
    (30, 5400, "video_jpeg", {"mode": "manual", "iso": 800},
     "0400022c01181500000000000000000000"),
    (25, 6000, "video", {"mode": "auto"},
     "040000fa00701700000000000000000000"),
    (25, 6000, "video_raw", {"mode": "auto"},
     "040003fa00701700000000000000000000"),
    (25, 6000, "video_jpeg", {"mode": "auto"},
     "040002fa00701700000000000000000000"),
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
