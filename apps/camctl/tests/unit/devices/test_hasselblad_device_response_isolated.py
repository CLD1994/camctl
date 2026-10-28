"""DEVICE 响应分类的资源隔离反例。"""

import importlib.util
import sys
from enum import StrEnum
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest


@pytest.fixture
def responses(monkeypatch):
    # 真实定位类型本身保持原路径约束；它的其他协作者在导入前隔离。
    identity = ModuleType("camctl.devices.drivers.hasselblad.identity")
    identity.CameraIdentity = lambda *args: None
    definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    definitions.DRIVER_ID = "hasselblad_cfv100c"
    monkeypatch.setitem(sys.modules, identity.__name__, identity)
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    source = Path(__file__).parents[3] / "src/camctl/devices/drivers/hasselblad"
    file_name = "camctl.devices.drivers.hasselblad._isolated_files"
    file_spec = importlib.util.spec_from_file_location(file_name, source / "files.py")
    files = importlib.util.module_from_spec(file_spec)
    monkeypatch.setitem(sys.modules, file_name, files)
    file_spec.loader.exec_module(files)
    monkeypatch.setattr(files, "json_equal", lambda left, right: left == right)
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
    name = "camctl.devices.drivers.hasselblad._isolated_responses"
    spec = importlib.util.spec_from_file_location(name, source / "responses.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, Format



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


@pytest.mark.parametrize("stderr", [b"capture: ERROR\n", b"capture: OK\n"])
def test_device_rejects_conflicting_capture_results_in_stderr(responses, stderr):
    api, fmt = responses
    with pytest.raises(api.CliParseError):
        api.parse_device_capture(b"capture: OK\ncamera file: /mnt/a.3FR\n", stderr,
            output_complete=True, capture_format=fmt.RAW, output_directory="/workspace")


@pytest.mark.parametrize("path", ["/mnt/../a.3FR", "/mnt//a.3FR", "relative/a.3FR", "/mnt/./a.3FR"])
def test_device_rejects_noncanonical_camera_locator(responses, path):
    api, fmt = responses
    with pytest.raises(api.CliParseError):
        api.parse_device_capture(f"capture: OK\ncamera file: {path}\n".encode(), b"",
            output_complete=True, capture_format=fmt.RAW, output_directory="/workspace")


def test_jpeg_pair_can_have_thumbnail_suffix_in_original_name(responses):
    api, fmt = responses
    result = api.parse_device_capture(
        b"capture: OK\nsaved /workspace/a.thumbnail.jpg (10 bytes)\n"
        b"saved /workspace/a.thumbnail.jpg.thumbnail.jpg (2 bytes)\n"
        b"camera file: /mnt/a.jpg\n", b"", output_complete=True,
        capture_format=fmt.JPEG, output_directory="/workspace")
    assert result.original.host_name == "a.thumbnail.jpg"
    assert result.preview.host_name == "a.thumbnail.jpg.thumbnail.jpg"


def test_single_jpeg_thumbnail_named_record_has_ambiguous_role(responses):
    api, fmt = responses
    result = api.parse_device_capture(
        b"capture: OK\nsaved /workspace/a.jpg.thumbnail.jpg (10 bytes)\n"
        b"camera file: /mnt/a.jpg\n", b"", output_complete=True,
        capture_format=fmt.JPEG, output_directory="/workspace")
    assert result.original is None and result.preview is None
    assert result.original_error and result.preview_error


def test_photo_result_cursor_fixes_original_binding_activity_and_next_role(responses):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    cursor = files.PhotoResultCursor(binding, "7", files.DownloadVariant.THUMBNAIL)
    assert files.PhotoResultCursor.from_json(cursor.as_json()) == cursor
    assert cursor.as_json() == {"kind": "hasselblad_photo", "device_id": "camera",
        "driver_id": "hasselblad_cfv100c", "activity_id": "7", "role": "thumbnail"}


@pytest.mark.parametrize("role,activity", [("original", "7"), ("thumbnail", "07"), ("thumbnail", "0")])
def test_photo_result_cursor_rejects_invalid_continuation(responses, role, activity):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    with pytest.raises(ValueError):
        files.PhotoResultCursor(files.DeviceBinding("camera", "hasselblad_cfv100c"), activity,
            files.DownloadVariant(role))


def test_photo_result_page_preserves_role_and_exact_camera_identity(responses):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    identity = files.CameraFileIdentity(binding, "/mnt/a.3FR", "a.3FR",
        files.DownloadVariant.ORIGINAL, "a" * 64, "serial", 10, 100)
    entry = {"identity": identity.file_identity, "locator": identity.locator(),
        "complete": True, "size_bytes": 100, "kind": "photo"}
    cursor = files.PhotoResultCursor(binding, "7", files.DownloadVariant.THUMBNAIL)
    data = {"activity_id": "7", "binding": {"device_id": "camera", "driver_id": "hasselblad_cfv100c"},
        "role": "original", "entries": [entry], "cursor": None, "next_cursor": cursor.as_json()}
    assert files.validate_photo_result_page(data, activity_id="7", binding=binding, cursor=None) == cursor


def test_photo_result_page_cannot_replace_source_with_other_role(responses):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    identity = files.CameraFileIdentity(binding, "/mnt/a.3FR", "a.3FR",
        files.DownloadVariant.THUMBNAIL, "a" * 64, "serial", 10, 100)
    entry = {"identity": identity.file_identity, "locator": identity.locator(),
        "complete": True, "size_bytes": 100, "kind": "photo"}
    cursor = files.PhotoResultCursor(binding, "7", files.DownloadVariant.THUMBNAIL)
    data = {"activity_id": "7", "binding": {"device_id": "camera", "driver_id": "hasselblad_cfv100c"},
        "role": "original", "entries": [entry], "cursor": None, "next_cursor": cursor.as_json()}
    with pytest.raises(ValueError):
        files.validate_photo_result_page(data, activity_id="7", binding=binding, cursor=None)


@pytest.fixture
def page_api(responses, monkeypatch):
    from enum import Enum
    class FileKind(Enum):
        PHOTO = "photo"
        OTHER = "other"
    modules = {
        "camctl.capture.results": {"FileKind": FileKind},
        "camctl.contracts.enums": {"enum_for": lambda name: None},
        "camctl.devices.directory": {"DirectoryCursor": type("DirectoryCursor", (), {})},
        "camctl.operations.models": {name: object for name in (
            "AttemptStatus", "AttemptTicket", "CallInfo", "CallOutcome", "EffectState", "ErrorValue",
            "EvidenceValue", "Settlement", "SettlementBasis")},
        "camctl.operations.result_format": {"read_result_document": lambda *args: None},
    }
    for name, attributes in modules.items():
        stub = ModuleType(name)
        stub.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, stub)
    source = Path(__file__).parents[3] / "src/camctl/capture/result_inputs.py"
    name = "camctl.capture._isolated_result_inputs"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, sys.modules["camctl.devices.drivers.hasselblad.files"]


def test_photo_page_decode_keeps_real_role_without_claiming_directory_scan(page_api):
    api, files = page_api
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    identity = files.CameraFileIdentity(binding, "/mnt/a.3FR", "a.3FR",
        files.DownloadVariant.ORIGINAL, "a" * 64, "serial", 10, 100)
    next_cursor = files.PhotoResultCursor(binding, "7", files.DownloadVariant.THUMBNAIL)
    data = {"activity_id": "7", "binding": {"device_id": "camera", "driver_id": binding.driver_id},
        "role": "original", "entries": [{"identity": identity.file_identity, "locator": identity.locator(),
            "complete": True, "size_bytes": 100, "kind": "photo"}],
        "cursor": None, "next_cursor": next_cursor.as_json()}
    observation = api.DeviceObservation("result_files_listed", 3, data)
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7")
    outcome = SimpleNamespace(observations=(observation,), error=None)
    page = api.page_from_outcome(ticket, outcome, binding=binding)
    assert page.entries[0].identity == identity.file_identity
    assert page.next_cursor == next_cursor
    assert not page.scan_complete and not page.set_finalized
    assert api.decode_result_cursor(next_cursor.as_json()) == next_cursor
