"""DEVICE 响应分类的资源隔离反例。"""

import importlib
import sys
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def responses(monkeypatch):
    files = ModuleType("camctl.devices.drivers.hasselblad.files")
    class Status(StrEnum):
        PATH = "path"
        EXPLICIT_MISSING = "explicit_missing"
    @dataclass(frozen=True)
    class Locator:
        status: Status
        camera_path: str | None = None
        camera_name: str | None = None
    files.CameraLocatorStatus = Status
    files.CameraFileLocator = Locator
    parameters = ModuleType("camctl.devices.drivers.hasselblad.parameters")
    class Format(StrEnum):
        RAW = "raw"
        JPEG = "jpeg"
    parameters.CaptureFormat = Format
    parameters.CaptureFormatDescription = object
    parameters.CAPTURE_FORMATS = {
        Format.RAW: SimpleNamespace(original_extension=".3fr", original_media_type="image/x-hasselblad-3fr", preview_extension=".jpg", preview_media_type="image/jpeg"),
        Format.JPEG: SimpleNamespace(original_extension=".jpg", original_media_type="image/jpeg", preview_extension=".jpg", preview_media_type="image/jpeg"),
    }
    monkeypatch.setitem(sys.modules, files.__name__, files)
    monkeypatch.setitem(sys.modules, parameters.__name__, parameters)
    name = "camctl.devices.drivers.hasselblad.responses"
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    yield module, Format
    monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.mark.parametrize("saved", [
    b"", b"saved /workspace/a.3FR (bad bytes)\n",
    b"saved /elsewhere/a.3FR (10 bytes)\n",
    b"saved /workspace/a.3FR (10 bytes)\nsaved /workspace/a.3FR (10 bytes)\n",
])
def test_device_keeps_camera_fact_if_host_records_are_unusable(responses, saved):
    api, fmt = responses
    result = api.parse_device_capture(b"capture: OK\n" + saved + b"camera file: /mnt/DCIM/a.3FR\n", b"",
        output_complete=True, capture_format=fmt.RAW, output_directory="/workspace")
    assert result.camera_locator.camera_path == "/mnt/DCIM/a.3FR"
    assert result.original is None and result.preview is None
    assert result.host_records_error is not None


@pytest.mark.parametrize("output", [
    b"capture: OK\ncapture: OK\ncamera file: /mnt/DCIM/a.3FR\n",
    b"capture: OK\ncamera file: /mnt/DCIM/a.3FR\ncamera file: /mnt/DCIM/b.3FR\n",
    b"capture: OK\ncapture: camera filename was not reported\n",
])
def test_device_rejects_ambiguous_control_or_camera_locator(responses, output):
    api, fmt = responses
    with pytest.raises(api.CliParseError):
        api.parse_device_capture(output, b"", output_complete=True,
            capture_format=fmt.RAW, output_directory="/workspace")


@pytest.mark.parametrize("format_name,extension", [("RAW", "3FR"), ("JPEG", "JPG")])
def test_device_preserves_valid_paired_host_records(responses, format_name, extension):
    api, fmt = responses
    output = (f"capture: OK\nsaved /workspace/a.{extension} (10 bytes)\n"
              f"saved /workspace/a.{extension}.thumbnail.jpg (2 bytes)\n"
              f"camera file: /mnt/DCIM/b.{extension}\n").encode()
    result = api.parse_device_capture(output, b"", output_complete=True,
        capture_format=getattr(fmt, format_name), output_directory="/workspace")
    assert result.camera_locator.camera_name == f"b.{extension}"
    assert result.original.host_name == f"a.{extension}"
    assert result.preview.host_name == f"a.{extension}.thumbnail.jpg"
    assert result.host_records_error is None


def test_device_rejects_conflicting_control_marker(responses):
    api, fmt = responses
    with pytest.raises(api.CliParseError):
        api.parse_device_capture(b"capture: OK\ncapture: ERROR\ncamera file: /mnt/a.3FR\n", b"",
            output_complete=True, capture_format=fmt.RAW, output_directory="/workspace")


@pytest.mark.parametrize("record,valid_role,missing_role", [
    (b"saved /workspace/a.3FR (10 bytes)\n", "original", "preview"),
    (b"saved /workspace/a.3FR.thumbnail.jpg (2 bytes)\n", "preview", "original"),
])
def test_device_keeps_independently_identified_single_host_record(responses, record, valid_role, missing_role):
    api, fmt = responses
    result = api.parse_device_capture(b"capture: OK\n" + record + b"camera file: /mnt/a.3FR\n", b"",
        output_complete=True, capture_format=fmt.RAW, output_directory="/workspace")
    assert getattr(result, valid_role) is not None
    assert getattr(result, missing_role) is None
    assert getattr(result, valid_role + "_error") is None
    assert getattr(result, missing_role + "_error") is not None


def test_invalid_preview_record_does_not_remove_independently_valid_original(responses):
    api, fmt = responses
    result = api.parse_device_capture(
        b"capture: OK\nsaved /workspace/a.3FR (10 bytes)\n"
        b"saved /outside/a.3FR.thumbnail.jpg (2 bytes)\n"
        b"camera file: /mnt/a.3FR\n", b"", output_complete=True,
        capture_format=fmt.RAW, output_directory="/workspace")
    assert result.original.host_name == "a.3FR"
    assert result.original_error is None
    assert result.preview is None
    assert result.preview_error is not None
