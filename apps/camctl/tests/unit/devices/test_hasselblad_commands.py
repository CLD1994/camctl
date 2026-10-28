"""参数分支必须精确成为有序 argv，而非相机默认值。"""
from decimal import Decimal

import pytest
import referencing

from camctl.devices.drivers.hasselblad.parameters import parse_photo_settings


@pytest.fixture(autouse=True)
def _isolate_schema_resources(monkeypatch):
    monkeypatch.setattr("camctl.contracts.schemas.schema_registry", lambda: referencing.Registry())


def _photo_params():
    return {"type": "hasselblad_photo", "capture_format": "raw",
            "exposure": {"mode": "P", "iso": 100},
            "white_balance": {"mode": "auto"}, "focus": {"mode": "MF"}}


@pytest.mark.parametrize("mode,fields,expected", [
    ("M", {"aperture": Decimal("4.8"), "shutter_s": "1/1500"}, [("set","iso","100"),("set","aperture","4.8"),("set","shutter","1/1500")]),
    ("A", {"aperture": Decimal("4")}, [("set","iso","100"),("set","aperture","4")]),
    ("S", {"shutter_s": "1/3000"}, [("set","iso","100"),("set","shutter","1/3000")]),
    ("P", {}, [("set","iso","100")]),
])
@pytest.mark.parametrize("wb", ["auto","daylight","cloudy","shade","tungsten","fluorescent","flash","manual"])
@pytest.mark.parametrize("focus", [{"mode":"MF"},{"mode":"MF","relative_move":-5},{"mode":"MF","relative_move":5},{"mode":"AF-S"}])
def test_photo_setup_commands_follow_selected_branches(mode, fields, expected, wb, focus):
    from camctl.devices.drivers.hasselblad.commands import PhotoPhase, photo_start_commands, PHOTO_START_CALL_LIMIT
    params = _photo_params()
    params["exposure"] = {"mode":mode,"iso":100,**fields}
    params["white_balance"] = {"mode":wb, **({"temperature_k":6500} if wb=="manual" else {})}
    params["focus"] = focus
    commands = photo_start_commands(parse_photo_settings(params))
    argv = [item.argv for item in commands]
    assert argv[:5] == [("list",),("info",),("set","drive-mode","single"),("set","capture-format","raw"),("set","exposure-mode",mode)]
    tail = [("set","wb-mode",wb)] + ([("set","wb","6500,0")] if wb=="manual" else [])
    tail += [("set","focus-mode",focus["mode"])]
    tail += ([("autofocus",)] if focus["mode"]=="AF-S" else [("focus",str(focus["relative_move"]))] if "relative_move" in focus else [])
    assert argv[5:] == expected + tail + [("capture",)]
    assert len(commands) <= PHOTO_START_CALL_LIMIT
    preparation = photo_start_commands(parse_photo_settings(params), phase=PhotoPhase.PREPARATION)
    capture = photo_start_commands(parse_photo_settings(params), phase=PhotoPhase.CAPTURE)
    lens = ([("autofocus",)] if focus["mode"] == "AF-S" else
            [("focus", str(focus["relative_move"]))] if "relative_move" in focus else [])
    assert [item.argv for item in preparation] == argv[:5] + expected + tail[:len(tail) - len(lens)]
    assert [item.argv for item in capture] == lens + [("capture",)]
    assert preparation + capture == commands


def test_capture_parser_consumes_two_complete_separate_streams():
    from camctl.devices.drivers.hasselblad.responses import parse_capture
    from camctl.host_files.photo_formats import CaptureFormat
    response = parse_capture(b"capture: OK\nsaved /work/p.JPG (2 bytes)\nsaved /work/p.JPG.thumbnail.jpg (1 bytes)\n", b"capture: camera filename was not reported\n", output_complete=True, capture_format=CaptureFormat.JPEG, output_directory="/work")
    assert response.camera_locator.status.value == "explicit_missing"


def test_short_shutter_strings_reach_both_command_paths_unchanged():
    from camctl.devices.drivers.hasselblad.commands import PhotoPhase, photo_start_commands

    for value in ("1/1500", "1/3000", "1/4000", "7/5", "32/1"):
        params = _photo_params()
        params["exposure"] = {"mode": "S", "iso": 100, "shutter_s": value}
        settings = parse_photo_settings(params)
        expected = ("set", "shutter", value)
        assert expected in [command.argv for command in photo_start_commands(settings)]
        assert expected in [command.argv for command in photo_start_commands(settings, phase=PhotoPhase.PREPARATION)]
