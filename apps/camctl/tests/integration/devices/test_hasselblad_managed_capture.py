"""T3 将 Hasselblad 驱动、真实受管子进程与主机文件端口组合验证。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
import textwrap

import pytest

from apps.camctl.tests.integration.jpeg_fixture import JPEG_IMAGE

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, PARAMETER_TYPE
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.ports import ControlRequest
from camctl.host_files.models import BoundDirectories
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState


_ACTIVITY_ID = "701"
_DEVICE_ID = "camera-1"
_SERIAL = "CFV100C-001"
_ORIGINAL_NAME = "fixture_capture.3FR"
_PREVIEW_NAME = f"{_ORIGINAL_NAME}.thumbnail.jpg"
# RAW 替身只用于验证已观察的 TIFF 文件签名。
_ORIGINAL_BYTES = b"II*\x00small integration fixture"
_PREVIEW_BYTES = JPEG_IMAGE + b"small preview fixture"


@pytest.mark.skipif(os.name == "nt", reason="可执行脚本 shebang 按 POSIX 部署边界运行")
@pytest.mark.asyncio
async def test_default_managed_driver_and_real_capture_file_port_confirm_one_photo(tmp_path: Path):
    driver, request, roots, executable, call_log, workspace = _harness(tmp_path)

    result = await driver.control(request)

    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.outcome.error is None
    assert result.outcome.call_info is not None
    assert result.outcome.call_info.local_exit_code == 0
    assert {item.type for item in result.observations} == {"photo_taken", "hasselblad_start_entered"}
    photo = result.observations[0]
    assert photo.type == "photo_taken"
    assert photo.data["activity_id"] == _ACTIVITY_ID
    assert photo.data["original"] == {
        "original_name": _ORIGINAL_NAME,
        "size_bytes": len(_ORIGINAL_BYTES),
        "sha256": hashlib.sha256(_ORIGINAL_BYTES).hexdigest(),
        "media_type": "image/x-hasselblad-3fr",
    }
    assert photo.data["preview"] == {
        "original_name": _PREVIEW_NAME,
        "size_bytes": len(_PREVIEW_BYTES),
        "sha256": hashlib.sha256(_PREVIEW_BYTES).hexdigest(),
        "media_type": "image/jpeg",
    }
    assert workspace == roots.staging / "capture-work" / _ACTIVITY_ID
    assert (workspace / _ORIGINAL_NAME).read_bytes() == _ORIGINAL_BYTES
    assert (workspace / _PREVIEW_NAME).read_bytes() == _PREVIEW_BYTES
    assert _read_logged_argv(call_log) == _expected_argv(executable, workspace)
    assert [args[3] for args in _read_logged_argv(call_log)] == [
        "list", "info", "set", "set", "set", "set", "set", "set", "capture"
    ]
    assert all(args[1:3] == ["--pid", "0x000a"] for args in _read_logged_argv(call_log))
    assert not ({"images", "download", "stop"} & {args[3] for args in _read_logged_argv(call_log)})


@pytest.mark.skipif(os.name == "nt", reason="可执行脚本 shebang 按 POSIX 部署边界运行")
@pytest.mark.asyncio
async def test_serial_mismatch_stops_before_settings_capture_or_work_directory(tmp_path: Path):
    driver, request, roots, executable, call_log, workspace = _harness(
        tmp_path, scenario="success", observed_serial="CFV100C-OTHER"
    )

    result = await driver.control(request)

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert tuple(o.type for o in result.outcome.observations) == ("hasselblad_start_entered",)
    assert result.outcome.error is not None
    assert result.outcome.error.details["step"] == "info"
    assert result.outcome.error.details["reason"] == "serial_mismatch"
    assert result.outcome.call_info is not None
    assert result.outcome.call_info.local_exit_code == 0
    assert _read_logged_argv(call_log) == [
        [str(executable), "--pid", "0x000a", "list"],
        [str(executable), "--pid", "0x000a", "info"],
    ]
    assert not (roots.staging / "capture-work").exists()
    assert not workspace.exists()


@pytest.mark.skipif(os.name == "nt", reason="可执行脚本 shebang 按 POSIX 部署边界运行")
@pytest.mark.asyncio
async def test_nonzero_capture_retains_partial_raw_and_is_unknown_without_retry(tmp_path: Path):
    driver, request, _roots, executable, call_log, workspace = _harness(
        tmp_path, scenario="partial_exit"
    )

    result = await driver.control(request)

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.UNKNOWN
    assert tuple(o.type for o in result.outcome.observations) == ("hasselblad_start_entered",)
    assert result.outcome.call_info is not None
    assert result.outcome.call_info.local_exit_code == 17
    assert result.outcome.error is not None
    assert result.outcome.error.details["step"] == "capture"
    assert result.outcome.error.details["reason"] == "exit_nonzero"
    assert (workspace / _ORIGINAL_NAME).read_bytes() == _ORIGINAL_BYTES[:12]
    assert not (workspace / _PREVIEW_NAME).exists()
    assert _read_logged_argv(call_log) == _expected_argv(executable, workspace)
    assert sum(args[3] == "capture" for args in _read_logged_argv(call_log)) == 1


@pytest.mark.skipif(os.name == "nt", reason="可执行脚本 shebang 按 POSIX 部署边界运行")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_reason", "preview_exists"),
    [
        ("missing_preview", "source_open", False),
        ("wrong_preview_size", "source_size", True),
    ],
)
async def test_exit_zero_does_not_confirm_missing_or_wrong_length_file(
    tmp_path: Path, scenario: str, expected_reason: str, preview_exists: bool
):
    driver, request, _roots, executable, call_log, workspace = _harness(
        tmp_path, scenario=scenario
    )

    result = await driver.control(request)

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.UNKNOWN
    assert tuple(o.type for o in result.outcome.observations) == ("hasselblad_start_entered",)
    assert result.outcome.call_info is not None
    assert result.outcome.call_info.local_exit_code == 0
    assert result.outcome.error is not None
    assert result.outcome.error.details["step"] == "verify_files"
    assert result.outcome.error.details["reason"] == expected_reason
    assert (workspace / _ORIGINAL_NAME).read_bytes() == _ORIGINAL_BYTES
    assert (workspace / _PREVIEW_NAME).exists() is preview_exists
    if preview_exists:
        assert (workspace / _PREVIEW_NAME).read_bytes() == _PREVIEW_BYTES
    assert _read_logged_argv(call_log) == _expected_argv(executable, workspace)
    assert sum(args[3] == "capture" for args in _read_logged_argv(call_log)) == 1


def _harness(
    tmp_path: Path,
    *,
    scenario: str = "success",
    observed_serial: str = _SERIAL,
):
    staging = tmp_path / "staging"
    staging.mkdir()
    roots = BoundDirectories(staging=staging)
    executable = tmp_path / "hb_cli"
    call_log = tmp_path / "argv.jsonl"
    executable.write_text(
        _script_source(call_log, scenario=scenario, observed_serial=observed_serial),
        encoding="utf-8",
    )
    executable.chmod(0o700)

    devices = {
        _DEVICE_ID: {
            "kind": "camera",
            "driver": DRIVER_ID,
            "hasselblad": {
                "executable": str(executable),
                "serial": _SERIAL,
                "product_id": 10,
            },
        }
    }
    driver = HasselbladDriver(devices, roots, monotonic_ns=time.monotonic_ns)
    request = ControlRequest(
        operation="take_photo",
        binding=DeviceBinding(_DEVICE_ID, DRIVER_ID),
        params={
            "type": PARAMETER_TYPE,
            "capture_format": "raw",
            "exposure": {"mode": "P", "iso": 100},
            "white_balance": {"mode": "auto"},
            "focus": {"mode": "MF"},
        },
        ticket=AttemptTicket(
            attempt_id=1,
            operation="control",
            target_id=_ACTIVITY_ID,
            responsibility_key="capture/701",
            run_id=1,
        ),
        timeout_s=Decimal("180"),
        dispatch_check=AsyncMock(side_effect=lambda: None),
    )
    workspace = staging / "capture-work" / _ACTIVITY_ID
    return driver, request, roots, executable, call_log, workspace


def _script_source(call_log: Path, *, scenario: str, observed_serial: str) -> str:
    return "#!" + sys.executable + "\n" + textwrap.dedent(
        f"""\
        import json
        import pathlib
        import sys

        LOG = pathlib.Path({str(call_log)!r})
        SCENARIO = {scenario!r}
        SERIAL = {observed_serial!r}
        RAW = bytes.fromhex({_ORIGINAL_BYTES.hex()!r})
        PREVIEW = bytes.fromhex({_PREVIEW_BYTES.hex()!r})
        CAMERA_FILE_PATH = "/mnt/media/DCIM/B9996039.3FR"
        CAMERA_FILE_NAME = pathlib.PurePosixPath(CAMERA_FILE_PATH).name
        argv = sys.argv
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(argv, ensure_ascii=True) + "\\n")

        args = argv[1:]
        if args[:2] != ["--pid", "0x000a"]:
            raise SystemExit("unexpected fixed camera PID arguments")
        command = args[2] if len(args) >= 3 else ""
        if command == "list" and args == ["--pid", "0x000a", "list"]:
            sys.stdout.write("vid=0x2756 pid=0x000a  bus=  1 addr= 23\\n")
        elif command == "info" and args == ["--pid", "0x000a", "info"]:
            sys.stdout.write(f"serial : {{SERIAL}}\\nfirmware : v4.1.0\\n")
        elif command == "set" and args in (
            ["--pid", "0x000a", "set", "drive-mode", "single"],
            ["--pid", "0x000a", "set", "capture-format", "raw"],
            ["--pid", "0x000a", "set", "exposure-mode", "P"],
            ["--pid", "0x000a", "set", "iso", "100"],
            ["--pid", "0x000a", "set", "wb-mode", "auto"],
            ["--pid", "0x000a", "set", "focus-mode", "MF"],
        ):
            pass
        elif command == "capture" and len(args) == 5 and args[:4] == [
            "--pid", "0x000a", "capture", "--output"
        ]:
            work = pathlib.Path(args[4])
            if not work.is_absolute():
                raise SystemExit("capture output directory must be absolute")
            original = work / {_ORIGINAL_NAME!r}
            preview = work / {_PREVIEW_NAME!r}
            if SCENARIO == "partial_exit":
                original.write_bytes(RAW[:12])
                sys.stdout.write("capture: partial\\n")
                raise SystemExit(17)

            original.write_bytes(RAW)
            if SCENARIO != "missing_preview":
                preview.write_bytes(PREVIEW)
            preview_size = len(PREVIEW) + (1 if SCENARIO == "wrong_preview_size" else 0)
            sys.stdout.write(
                "capture: OK\\n"
                f"saved {{original}} ({{len(RAW)}} bytes)\\n"
                f"saved {{preview}} ({{preview_size}} bytes)\\n"
                f"camera file: {{CAMERA_FILE_PATH}}\\n"
            )
        elif command == "download" and len(args) == 7 and args[3] in ("original", "thumbnail"):
            if (
                args[4] != CAMERA_FILE_NAME
                or args[4] in ({_ORIGINAL_NAME!r}, CAMERA_FILE_PATH)
                or args[5] != "--output"
            ):
                raise SystemExit("download must use the camera-reported basename")
            work = pathlib.Path(args[6])
            (work / "tool-download-file").write_bytes(RAW if args[3] == "original" else PREVIEW)
            sys.stdout.write("download complete\\n")
        else:
            raise SystemExit("unexpected hb_cli argument vector")
        """
    )


def _read_logged_argv(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _expected_argv(executable: Path, workspace: Path) -> list[list[str]]:
    return [
        [str(executable), "--pid", "0x000a", "list"],
        [str(executable), "--pid", "0x000a", "info"],
        [str(executable), "--pid", "0x000a", "set", "drive-mode", "single"],
        [str(executable), "--pid", "0x000a", "set", "capture-format", "raw"],
        [str(executable), "--pid", "0x000a", "set", "exposure-mode", "P"],
        [str(executable), "--pid", "0x000a", "set", "iso", "100"],
        [str(executable), "--pid", "0x000a", "set", "wb-mode", "auto"],
        [str(executable), "--pid", "0x000a", "set", "focus-mode", "MF"],
        [str(executable), "--pid", "0x000a", "capture", "--output", str(workspace)],
    ]
