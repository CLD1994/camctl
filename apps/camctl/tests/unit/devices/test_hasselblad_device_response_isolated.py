"""DEVICE 响应分类的资源隔离反例。"""

import importlib
import importlib.util
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest


@pytest.fixture
def responses(monkeypatch):
    parent = importlib.import_module("camctl.devices.drivers.hasselblad")
    names = ("identity", "definitions", "files", "parameters")
    missing = object()
    before_modules = {name: sys.modules.get(parent.__name__ + "." + name, missing) for name in names}
    before_attrs = {name: getattr(parent, name, missing) for name in names}
    source = Path(__file__).parents[3] / "src/camctl/devices/drivers/hasselblad"
    with monkeypatch.context() as isolated:
        definitions = ModuleType(parent.__name__ + ".definitions")
        definitions.DRIVER_ID = "hasselblad_cfv100c"
        isolated.setitem(sys.modules, definitions.__name__, definitions)
        isolated.setattr(parent, "definitions", definitions, raising=False)
        identity_name = parent.__name__ + "._isolated_identity"
        identity_spec = importlib.util.spec_from_file_location(identity_name, source / "identity.py")
        identity = importlib.util.module_from_spec(identity_spec)
        isolated.setitem(sys.modules, identity_name, identity)
        identity_spec.loader.exec_module(identity)
        isolated.setitem(sys.modules, parent.__name__ + ".identity", identity)
        isolated.setattr(parent, "identity", identity, raising=False)
        file_name = parent.__name__ + "._isolated_files"
        file_spec = importlib.util.spec_from_file_location(file_name, source / "files.py")
        files = importlib.util.module_from_spec(file_spec)
        isolated.setitem(sys.modules, file_name, files)
        file_spec.loader.exec_module(files)
        isolated.setattr(files, "json_equal", lambda left, right: left == right)
        isolated.setitem(sys.modules, parent.__name__ + ".files", files)
        isolated.setattr(parent, "files", files, raising=False)
        models = ModuleType("camctl.host_files.models")
        models.FilePurpose = Enum("FilePurpose", {"CAPTURE_ORIGINAL": 1, "CAPTURE_PREVIEW": 2})
        isolated.setitem(sys.modules, models.__name__, models)
        formats_name = "camctl.host_files._isolated_photo_formats"
        formats_source = source.parents[2] / "host_files/photo_formats.py"
        formats_spec = importlib.util.spec_from_file_location(formats_name, formats_source)
        formats = importlib.util.module_from_spec(formats_spec)
        isolated.setitem(sys.modules, formats_name, formats)
        formats_spec.loader.exec_module(formats)
        parameters = ModuleType(parent.__name__ + ".parameters")
        parameters.CaptureFormat = formats.CaptureFormat
        parameters.CaptureFormatDescription = formats.CaptureFormatDescription
        parameters.CAPTURE_FORMATS = formats.CAPTURE_FORMATS
        isolated.setitem(sys.modules, parameters.__name__, parameters)
        isolated.setattr(parent, "parameters", parameters, raising=False)
        name = parent.__name__ + "._isolated_responses"
        spec = importlib.util.spec_from_file_location(name, source / "responses.py")
        module = importlib.util.module_from_spec(spec)
        isolated.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        yield module, formats.CaptureFormat
    for name in names:
        assert sys.modules.get(parent.__name__ + "." + name, missing) is before_modules[name]
        assert getattr(parent, name, missing) is before_attrs[name]



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


@pytest.mark.parametrize("serial,product_id", [("", 10), ("bad\x00serial", 10), ("serial", 0), ("serial", 65536), ("serial", True)])
def test_photo_source_identity_rejects_invalid_real_camera_identity(responses, serial, product_id):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    with pytest.raises(ValueError):
        files.CameraFileIdentity(binding, "/mnt/a.3FR", "a.3FR",
            files.DownloadVariant.ORIGINAL, "a" * 64, serial, product_id, 100)


@pytest.fixture
def photo_driver_api(responses, monkeypatch):
    from contextlib import nullcontext
    from dataclasses import dataclass
    from decimal import Decimal
    from enum import Enum
    from camctl.devices.evidence import EvidenceContract
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    parameters = sys.modules["camctl.devices.drivers.hasselblad.parameters"]
    parameters.PhotoSettings = object
    parameters.parse_photo_settings = lambda params: params
    parent = importlib.import_module("camctl.devices.drivers.hasselblad")
    monkeypatch.setitem(sys.modules, parent.__name__ + ".responses", responses[0])
    monkeypatch.setattr(parent, "responses", responses[0], raising=False)
    definitions = sys.modules["camctl.devices.drivers.hasselblad.definitions"]
    definitions.DRIVER_DECLARATION = object()
    class AttemptStatus(Enum):
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        UNKNOWN = "unknown"
    class EffectState(Enum):
        CONFIRMED = "confirmed"
        NO_EFFECT = "no_effect"
        UNKNOWN = "unknown"
    class SettlementBasis(Enum):
        OBSERVED = "observed"
        NOT_DISPATCHED = "not_dispatched"
    class EvidenceValue:
        @staticmethod
        def of(contract): return SimpleNamespace(type=contract.type, version=contract.version, data={})
        def __new__(cls, kind, version, data): return SimpleNamespace(type=kind, version=version, data=data)
    class ErrorValue:
        def __init__(self, code, stage, details=None): self.code, self.stage, self.details = code, stage, details
    class CallOutcome:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class Result:
        @classmethod
        def from_outcome(cls, outcome, **kwargs): return SimpleNamespace(outcome=outcome)
    class SourceWorkspace:
        files_closed = True
        close_error = None
        close_errors = ()
        instances = []
        def __init__(self, staging, ticket, role):
            self.path, self.role, self.released = Path(f"/staging/result-downloads/{ticket.run_id}-{ticket.attempt_id}-{role}"), role, False
            self.__class__.instances.append(self)
        async def prepare(self): return self.path
        async def observe(self, media):
            return SimpleNamespace(sha256="a" * 64, size_bytes=100, original_name="downloaded.jpg", media_type=media,
                as_json=lambda: {"sha256": "a" * 64, "size_bytes": 100, "original_name": "downloaded.jpg", "media_type": media})
        def release(self): self.released = True
    class CameraReadError(ValueError): pass
    diagnostics_path = Path(__file__).parents[3] / "src/camctl/operations/error_diagnostics.py"
    diagnostics_name = "camctl.operations._isolated_photo_error_diagnostics"
    diagnostics_spec = importlib.util.spec_from_file_location(diagnostics_name, diagnostics_path)
    diagnostics_api = importlib.util.module_from_spec(diagnostics_spec)
    monkeypatch.setitem(sys.modules, diagnostics_name, diagnostics_api)
    diagnostics_spec.loader.exec_module(diagnostics_api)
    class RawToolOutcome: pass
    class ToolCallCancelled(Exception): pass
    class ToolStartError(Exception): pass
    class ToolStreamState(Enum):
        COMPLETE = "complete"
        UNAVAILABLE = "unavailable"
        UNCONFIRMED = "unconfirmed"
    @dataclass
    class ToolStream:
        state: object
        data: object
    @dataclass
    class ToolCallOutput:
        step_index: int
        step_name: str
        stdout: object
        stderr: object
    @dataclass
    class ToolDiagnostics:
        call_limit: int
        calls: tuple
    entered = EvidenceContract("start_entered_test", 1, "control", frozenset())
    prevented = EvidenceContract("start_prevented_test", 1, "control", frozenset())
    preparation = EvidenceContract("preparation_test", 1, "control", frozenset())
    modules = {
        "camctl.devices.bindings": None,
        "camctl.devices.capture_preparation": {"CAPTURE_PREPARATION_COMPLETED": preparation, "DEFAULT_PREPARE_LEAD_S": Decimal(10), "PreparedCaptureCache": object},
        "camctl.devices.ports": {"ControlRequest": object, "DeviceCallResult": Result},
        "camctl.devices.start_entry": {"ENTERED": entered, "PREVENTED": prevented, "entered_observation": lambda ticket: None},
        "camctl.devices.read_session": {"ReadSession": object, "SourceFile": object,
            "DeferredSourceFile": type("DeferredSourceFile", (), {}), "ReadPreparationError": CameraReadError,
            "ReadPreparationSettlement": SimpleNamespace(SETTLED=SimpleNamespace(value="settled"))},
        "camctl.host_files.downloads": {"DownloadWorkspace": object, "ResultDownloadWorkspace": SourceWorkspace},
        "camctl.host_files.capture_sources": {"CaptureFileError": type("CaptureFileError", (ValueError,), {}), "CaptureFileSpec": object,
            "CapturedArtifact": object, "prepare_capture_workspace": None, "verify_capture_files": None,
            "inspect_capture_candidate": None, "workspace_path": lambda roots, activity: roots.staging / "capture-work" / activity},
        "camctl.logging_runtime.diagnostics": {"record_error": lambda *args, **kwargs: None},
        "camctl.logging_runtime.events": {"log_context": lambda **kwargs: nullcontext(), "measure_stage": lambda *args, **kwargs: nullcontext()},
        "camctl.operations.models": {"AttemptStatus": AttemptStatus, "CallInfo": lambda **kwargs: SimpleNamespace(**kwargs), "CallOutcome": CallOutcome,
            "EffectState": EffectState, "ErrorValue": ErrorValue, "EvidenceValue": EvidenceValue,
            "Settlement": lambda basis, evidence: SimpleNamespace(basis=basis, evidence=evidence), "SettlementBasis": SettlementBasis},
        "camctl.operations.owned_calls": {"current_call_scope": lambda: None},
        "camctl.operations.error_diagnostics": {"DiagnosticError": diagnostics_api.DiagnosticError,
            "exception_diagnostic": diagnostics_api.exception_diagnostic,
            "attach_diagnostic_facts": diagnostics_api.attach_diagnostic_facts},
        "camctl.operations.process": {"RawToolOutcome": RawToolOutcome, "StopSignal": object, "ToolCallCancelled": ToolCallCancelled,
            "ToolSpec": object, "ToolStartError": ToolStartError, "execute_tool": None},
        "camctl.operations.tool_diagnostics": {"ToolDiagnostics": ToolDiagnostics, "ToolCallOutput": ToolCallOutput,
            "ToolStream": ToolStream, "ToolStreamState": ToolStreamState},
        "camctl.devices.drivers.hasselblad.commands": {"PhotoPhase": object, "PhotoStep": object,
            "PHOTO_START_CALL_LIMIT": 20, "photo_start_commands": lambda *args, **kwargs: ()},
        "camctl.devices.drivers.hasselblad.configuration": {"CameraConfig": object, "read_camera_config": lambda value: value},
    }
    for name, attributes in modules.items():
        if attributes is not None:
            stub = ModuleType(name)
            stub.__dict__.update(attributes)
            monkeypatch.setitem(sys.modules, name, stub)
    source = Path(__file__).parents[3] / "src/camctl/devices/drivers/hasselblad/driver.py"
    name = "camctl.devices.drivers.hasselblad._isolated_driver"
    spec = importlib.util.spec_from_file_location(name, source)
    driver = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, driver)
    spec.loader.exec_module(driver)
    return driver, files, SourceWorkspace, RawToolOutcome


def _finish_immediate(coroutine):
    with pytest.raises(StopIteration) as finished:
        coroutine.send(None)
    return finished.value.value


@pytest.mark.parametrize("role", ["original", "thumbnail"])
@pytest.mark.parametrize("capture_format, extension", [("raw", "3FR"), ("jpeg", "JPG")])
def test_photo_source_runs_identity_and_download_with_one_decreasing_deadline(photo_driver_api, role, capture_format, extension):
    from decimal import Decimal
    api, files, workspace, Raw = photo_driver_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    times = iter([0, 1_000_000_000, 2_000_000_000, 3_000_000_000])
    driver.monotonic_ns = lambda: next(times)
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial", product_id=10)
    calls = []
    async def run(*, config, name, args, timeout_s, steps):
        calls.append((name, args, timeout_s))
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        output = {"list": b"vid=0x2756 pid=0x000a bus=1 addr=2\n", "info": b"serial : serial\n", "download": b""}[name]
        raw = Raw()
        raw.output, raw.stderr, raw.output_failure, raw.error = output, b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, f"/mnt/a.{extension}", f"a.{extension}")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": capture_format, "role": role, "cursor": None, "original_identity": None,
            "sync_candidate": None, "sync_error": "主机记录不可用"})
    result = _finish_immediate(driver.observe_photo_source(request))
    assert [item[0] for item in calls] == ["list", "info", "download"]
    assert [item[2] for item in calls] == [Decimal(9), Decimal(8), Decimal(7)]
    assert calls[2][1][1:3] == (role, f"a.{extension}")
    assert result.outcome.status is api.AttemptStatus.SUCCEEDED
    observed = next(item for item in result.outcome.observations if item.type == "result_files_listed")
    assert observed.data["role"] == role and observed.data["entries"][0]["size_bytes"] == 100
    attempt = next(item for item in result.outcome.observations if item.type == "photo_result_attempt")
    assert attempt.data["comparison"] == {
        "status": "unconfirmed", "selected": "camera", "sync_artifact": None,
        "sync_error": "主机记录不可用",
    }
    assert workspace.instances[-1].released


def test_photo_source_timeout_stops_before_download_and_preserves_role_error(photo_driver_api):
    from decimal import Decimal
    api, files, workspace, Raw = photo_driver_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    times = iter([0, 1_000_000_000, 2_000_000_000, 10_000_000_000])
    driver.monotonic_ns = lambda: next(times)
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial", product_id=10)
    calls = []
    async def run(*, config, name, args, timeout_s, steps):
        calls.append(name)
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = b"vid=0x2756 pid=0x000a bus=1 addr=2\n" if name == "list" else b"serial : serial\n"
        raw.stderr, raw.output_failure, raw.error = b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None, "original_identity": None,
            "sync_candidate": None, "sync_error": "主机记录不可用"})
    result = _finish_immediate(driver.observe_photo_source(request))
    assert calls == ["list", "info"]
    assert result.outcome.status is api.AttemptStatus.FAILED
    assert result.outcome.error.details["role"] == "original"
    assert result.outcome.error.details["reason"] == "source_call_timeout"
    assert all(item.type != "result_files_listed" for item in result.outcome.observations)
    assert workspace.instances[-1].released


@pytest.mark.parametrize("status,digest,expected,selected", [
    ("complete", "a" * 64, "matched", "sync"),
    ("complete", "b" * 64, "mismatch", "camera"),
    ("missing", None, "missing", "camera"),
    ("invalid", None, "invalid", "camera"),
    ("unconfirmed", None, "unconfirmed", "camera"),
])
def test_photo_source_records_one_actual_sync_comparison(photo_driver_api, status, digest, expected, selected):
    from decimal import Decimal
    api, files, _, Raw = photo_driver_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial", product_id=10)
    async def run(*, config, name, args, timeout_s, steps):
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = {"list": b"vid=0x2756 pid=0x000a bus=1 addr=2\n", "info": b"serial : serial\n", "download": b""}[name]
        raw.stderr, raw.output_failure, raw.error = b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    api.CaptureFileSpec = lambda name, size, media: SimpleNamespace(original_name=name, size_bytes=size, media_type=media)
    observed = []
    async def inspect(roots, activity_id, spec, *, owner):
        observed.append((activity_id, spec.original_name))
        artifact = None if digest is None else SimpleNamespace(size_bytes=100, sha256=digest,
            as_json=lambda: {"original_name": "local.3FR", "size_bytes": 100, "sha256": digest,
                             "media_type": "image/x-hasselblad-3fr"})
        failure = ValueError("sync failed") if status in ("invalid", "unconfirmed") else None
        return SimpleNamespace(status=status, artifact=artifact, error=failure)
    api.inspect_capture_candidate = inspect
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None,
            "original_identity": None, "sync_candidate": {"host_path": "/staging/capture-work/7/local.3FR",
                "host_name": "local.3FR", "size": 100, "media_type": "image/x-hasselblad-3fr"}, "sync_error": None})
    outcome = _finish_immediate(driver.observe_photo_source(request)).outcome
    comparison = next(item for item in outcome.observations if item.type == "photo_result_attempt").data["comparison"]
    assert outcome.status is api.AttemptStatus.SUCCEEDED
    assert observed == [("7", "local.3FR")]
    assert (comparison["status"], comparison["selected"]) == (expected, selected)
    assert (comparison["sync_error"] is not None) == (status in ("invalid", "unconfirmed"))


def test_photo_source_does_not_dispatch_to_changed_camera_binding(photo_driver_api):
    from decimal import Decimal
    api, files, workspace, _ = photo_driver_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._binding_config = lambda binding: SimpleNamespace(serial="different", product_id=10)
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None, "original_identity": None,
            "sync_candidate": None, "sync_error": "主机记录不可用"})
    result = _finish_immediate(driver.observe_photo_source(request))
    assert result.outcome.status is api.AttemptStatus.FAILED
    assert result.outcome.error.code == "device_binding_unavailable"
    assert result.outcome.error.details["reason"] == "physical_identity_mismatch"
    assert result.outcome.settlement.basis is api.SettlementBasis.NOT_DISPATCHED
    assert workspace.instances[-1].released


def test_actual_info_serial_mismatch_is_binding_failure(photo_driver_api):
    from decimal import Decimal
    api, files, _, Raw = photo_driver_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial", product_id=10)
    calls = []
    async def run(*, config, name, args, timeout_s, steps):
        calls.append(name)
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
                      "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = b"vid=0x2756 pid=0x000a bus=1 addr=2\n" if name == "list" else b"serial : another\n"
        raw.stderr, raw.output_failure, raw.error = b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None,
            "original_identity": None, "sync_candidate": None, "sync_error": "主机记录不可用"})
    outcome = _finish_immediate(driver.observe_photo_source(request)).outcome
    assert calls == ["list", "info"]
    assert outcome.error.code == "device_binding_unavailable"
    assert outcome.error.details["reason"] == "physical_identity_mismatch"


@pytest.mark.parametrize("content_error", [False, True])
def test_unconfirmed_file_close_cannot_return_a_settled_result(photo_driver_api, content_error):
    from decimal import Decimal
    api, files, workspace, Raw = photo_driver_api
    workspace.files_closed = False
    workspace.close_error = OSError(5, "first close failed", "/fake/source")
    second_error = OSError(9, "second close failed", "/fake/directory")
    workspace.close_errors = (workspace.close_error, second_error)
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial" if content_error else "different", product_id=10)
    primary = api.CaptureFileError("source_media")
    primary.stage = "source_media"
    async def observe(self, media): raise primary
    if content_error:
        workspace.observe = observe
    async def run(*, config, name, args, timeout_s, steps):
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
                      "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = {"list": b"vid=0x2756 pid=0x000a bus=1 addr=2\n", "info": b"serial : serial\n", "download": b""}[name]
        raw.stderr, raw.output_failure, raw.error = b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None,
            "original_identity": None, "sync_candidate": None, "sync_error": "主机记录不可用"})
    with pytest.raises(BaseExceptionGroup, match="关闭未确认") as caught:
        _finish_immediate(driver.observe_photo_source(request))
    assert workspace.close_error in caught.value.exceptions
    assert second_error in caught.value.exceptions
    assert (primary in caught.value.exceptions if content_error else
            any(isinstance(error, api.PhysicalIdentityMismatch) for error in caught.value.exceptions))
    assert not workspace.instances[-1].released
    import json
    diagnostic = json.dumps(api.exception_diagnostic(caught.value))
    assert '"errno": 5' in diagnostic and '"errno": 9' in diagnostic
    assert "/fake/source" in diagnostic and "/fake/directory" in diagnostic
    if content_error:
        assert "source_media" in diagnostic


@pytest.fixture
def photo_page_guard_api(photo_driver_api, page_api, monkeypatch):
    import json
    driver, files, workspace, Raw = photo_driver_api
    page_input, _ = page_api
    evidence = __import__("camctl.devices.evidence", fromlist=["EvidenceContract"])
    start_entry = sys.modules["camctl.devices.start_entry"]
    start_entry.is_hasselblad_photo = lambda action: True
    start_entry.entry_from_rows = lambda *args: None
    start_entry.EntryPhase = object
    enums = sys.modules["camctl.contracts.enums"]
    enums.enum_for = lambda name: SimpleNamespace()
    actions = {"id": 1, "device_id": "camera", "driver_id": "hasselblad_cfv100c",
        "execution_spec_json": {"output_storage": "device"}, "effective_params_json": {"capture_format": "raw"}}
    attempts = {"id": 5, "intent_event_id": 20}
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, identity: {"actions": actions, "operation_attempts": attempts}.get(table)
    transaction.event_envelope = transaction.update_change = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    capture_files = ModuleType("camctl.capture.files")
    capture_files.file_identity_key = lambda device, driver_id, identity: json.dumps([device, driver_id, identity], separators=(",", ":"))
    monkeypatch.setitem(sys.modules, capture_files.__name__, capture_files)
    models = ModuleType("camctl.operations.models")
    models.AttemptStatus, models.EffectState = driver.AttemptStatus, driver.EffectState
    monkeypatch.setitem(sys.modules, models.__name__, models)
    diagnostics = ModuleType("camctl.operations.tool_diagnostics")
    diagnostics.ToolStreamState = driver.ToolStreamState
    monkeypatch.setitem(sys.modules, diagnostics.__name__, diagnostics)
    photo_format = ModuleType("camctl.capture.photo_format")
    photo_format.saved_photo_format = lambda action: sys.modules["camctl.devices.drivers.hasselblad.parameters"].CaptureFormat.RAW
    monkeypatch.setitem(sys.modules, photo_format.__name__, photo_format)
    class ArtifactFact:
        @classmethod
        def from_json(cls, value):
            if (not isinstance(value, dict) or set(value) != {"original_name", "size_bytes", "sha256", "media_type"}
                    or not isinstance(value["original_name"], str) or not value["original_name"]
                    or "/" in value["original_name"] or "\\" in value["original_name"]
                    or type(value["size_bytes"]) is not int or value["size_bytes"] <= 0
                    or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64
                    or any(ch not in "0123456789abcdef" for ch in value["sha256"])
                    or value["media_type"] not in ("image/jpeg", "image/x-hasselblad-3fr")):
                raise ValueError("invalid artifact")
            return SimpleNamespace(**value)
    monkeypatch.setattr(sys.modules["camctl.host_files.capture_sources"], "CapturedArtifact", ArtifactFact)
    formats = ModuleType("camctl.host_files.photo_formats")
    formats.CAPTURE_FORMATS = sys.modules["camctl.devices.drivers.hasselblad.parameters"].CAPTURE_FORMATS
    monkeypatch.setitem(sys.modules, formats.__name__, formats)
    source = Path(__file__).parents[3] / "src/camctl/persistence/repositories/capture_facts.py"
    name = "camctl.persistence.repositories._isolated_photo_capture_facts"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, driver, files, page_input, Raw


def test_photo_source_guard_rejects_artifact_that_differs_from_actual_page(photo_page_guard_api):
    from decimal import Decimal
    import hashlib
    guard, api, files, page_api, Raw = photo_page_guard_api
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._binding_config = lambda binding: SimpleNamespace(serial="serial", product_id=10)
    async def run(*, config, name, args, timeout_s, steps):
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = {"list": b"vid=0x2756 pid=0x000a bus=1 addr=2\n", "info": b"serial : serial\n", "download": b""}[name]
        steps[-1]["stdout_size_bytes"] = len(raw.output)
        steps[-1]["stdout_sha256"] = hashlib.sha256(raw.output).hexdigest()
        raw.stderr, raw.output_failure, raw.error = b"", None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    ticket = SimpleNamespace(operation="result", target_id="7", responsibility_key="results/7", run_id=9, attempt_id=2)
    locator = files.CameraFileLocator(files.CameraLocatorStatus.PATH, "/mnt/a.3FR", "a.3FR")
    request = SimpleNamespace(operation="result", ticket=ticket, timeout_s=Decimal(10), binding=binding,
        params={"activity_id": "7", "camera_identity": {"serial": "serial", "product_id": 10},
            "camera_locator": locator.as_json(), "capture_format": "raw", "role": "original", "cursor": None,
            "original_identity": None, "sync_candidate": None, "sync_error": "主机记录不可用"})
    outcome = _finish_immediate(driver.observe_photo_source(request)).outcome
    page = page_api.page_from_outcome(ticket, outcome, binding=binding)
    saved = SimpleNamespace(page=page, ref=SimpleNamespace(ticket=ticket, page_no=1, event_id=40))
    reader = guard.PhotoResultReads(object())
    reader.read_device_start = lambda action, activity_id, before_event_id, output_directory=None: (
        12, {"camera_identity": {"serial": "serial", "product_id": 10},
             "original": None, "original_error": "主机记录不可用"}, locator)
    reader._prior_role_pages = lambda **kwargs: ()
    _, identity = reader.verify_device_page(1, 7, 5, saved, before_page_event_id=40)
    assert identity.sha256 == "a" * 64
    reader._prior_role_pages = lambda **kwargs: (38,)
    with pytest.raises(ValueError):
        reader.verify_device_page(1, 7, 5, saved, before_page_event_id=40)
    reader._prior_role_pages = lambda **kwargs: ()
    attempt = next(item for item in outcome.observations if item.type == "photo_result_attempt")
    attempt.data["comparison"]["selected"] = "sync"
    with pytest.raises(ValueError):
        reader.verify_device_page(1, 7, 5, saved, before_page_event_id=40)
    attempt.data["comparison"]["selected"] = "camera"
    attempt.data["steps"][-1]["artifact"]["sha256"] = "b" * 64
    with pytest.raises(ValueError):
        reader.verify_device_page(1, 7, 5, saved, before_page_event_id=40)


def test_sync_candidate_must_equal_actual_start_response(photo_page_guard_api):
    from dataclasses import dataclass
    guard, _, _, _, _ = photo_page_guard_api
    @dataclass
    class Candidate:
        host_path: str
        host_name: str
        size: int
        media_type: str
    candidate = Candidate("/staging/capture-work/7/file.3FR", "file.3FR", 100, "image/x-hasselblad-3fr")
    response = SimpleNamespace(original=candidate, preview=None, original_error=None, preview_error="无预览")
    data = {"original": {"host_path": candidate.host_path, "host_name": candidate.host_name,
                         "size": 100, "media_type": candidate.media_type},
            "preview": None, "original_error": None, "preview_error": "无预览"}
    guard._require_sync_start_response(response, data)
    data["original"]["host_path"] = "/staging/capture-work/7/other.3FR"
    with pytest.raises(ValueError):
        guard._require_sync_start_response(response, data)
    data["original"] = None
    with pytest.raises(ValueError):
        guard._require_sync_start_response(response, data)


def test_device_start_saves_reliable_camera_effect_without_verifying_bad_host_copy(photo_driver_api):
    from decimal import Decimal
    from enum import Enum
    api, files, _, Raw = photo_driver_api
    class Step(Enum):
        CAPTURE = "capture"
        LIST = "list"
        INFO = "info"
    api.PhotoStep = Step
    api.PhotoPhase = SimpleNamespace(PREPARATION="preparation", CAPTURE="capture")
    api.photo_start_commands = lambda *args, **kwargs: (SimpleNamespace(name=Step.CAPTURE, argv=("capture",)),)
    api.entered_observation = lambda ticket: api.DeviceObservation("test_entered", 1, {"activity_id": "7"})
    async def prepare(roots, activity_id): return Path("/staging/capture-work/7")
    async def forbidden_verify(*args): raise AssertionError("DEVICE control must not verify local file content")
    api.prepare_capture_workspace, api.verify_capture_files = prepare, forbidden_verify
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._prepared_captures = SimpleNamespace(clear=lambda binding: None)
    fmt = sys.modules["camctl.devices.drivers.hasselblad.parameters"].CaptureFormat.RAW
    driver._request_config = lambda request: ("7", SimpleNamespace(serial="serial", product_id=10),
                                                SimpleNamespace(capture_format=fmt))
    async def run(*, config, name, args, timeout_s, steps):
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = b"capture: OK\nsaved /outside/a.3FR (bad bytes)\ncamera file: /mnt/a.3FR\n"
        raw.stderr, raw.output_failure, raw.error, raw.used_grace_s = b"", None, None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    async def dispatch(): return None
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    request = SimpleNamespace(prepared_by=None, binding=binding, ticket=SimpleNamespace(target_id="7", run_id=9, attempt_id=1),
        output_storage="device", timeout_s=Decimal(10), dispatch_check=dispatch)
    result = _finish_immediate(driver._capture_control(request, prepare_only=False))
    assert result.outcome.status is api.AttemptStatus.SUCCEEDED
    assert result.outcome.effect is api.EffectState.CONFIRMED
    photo = next(item for item in result.outcome.observations if item.type == "photo_taken")
    assert photo.version == 2 and photo.data["camera_locator"]["camera_path"] == "/mnt/a.3FR"
    assert photo.data["original"] is None and photo.data["original_error"]


def test_host_start_confirms_only_after_local_pair_validation(photo_driver_api):
    from decimal import Decimal
    from enum import Enum
    api, files, _, Raw = photo_driver_api
    class Step(Enum):
        CAPTURE = "capture"
        LIST = "list"
        INFO = "info"
    class Artifact:
        def __init__(self, spec):
            self.original_name, self.size_bytes, self.media_type = spec.original_name, spec.size_bytes, spec.media_type
        def as_json(self):
            return {"original_name": self.original_name, "size_bytes": self.size_bytes,
                    "media_type": self.media_type, "sha256": "a" * 64}
    api.PhotoStep = Step
    api.PhotoPhase = SimpleNamespace(PREPARATION="preparation", CAPTURE="capture")
    api.photo_start_commands = lambda *args, **kwargs: (SimpleNamespace(name=Step.CAPTURE, argv=("capture",)),)
    api.entered_observation = lambda ticket: api.DeviceObservation("test_entered", 1, {"activity_id": "7"})
    api.CaptureFileSpec = lambda name, size, media: SimpleNamespace(original_name=name, size_bytes=size, media_type=media)
    api.CapturedArtifact = Artifact
    async def prepare(roots, activity_id): return Path("/staging/capture-work/7")
    validated = []
    async def verify(roots, activity_id, specs, *, trusted_preview_name):
        validated.append((tuple(spec.original_name for spec in specs), trusted_preview_name))
        return tuple(Artifact(spec) for spec in specs)
    api.prepare_capture_workspace, api.verify_capture_files = prepare, verify
    driver = object.__new__(api.HasselbladDriver)
    driver.roots = SimpleNamespace(staging=Path("/staging"))
    driver.monotonic_ns = lambda: 0
    driver._prepared_captures = SimpleNamespace(clear=lambda binding: None)
    fmt = sys.modules["camctl.devices.drivers.hasselblad.parameters"].CaptureFormat.RAW
    driver._request_config = lambda request: ("7", SimpleNamespace(serial="serial", product_id=10),
                                                SimpleNamespace(capture_format=fmt))
    async def run(*, config, name, args, timeout_s, steps):
        steps.append({"name": name, "local_exit_code": 0, "local_signal": None,
            "transport_error": None, "output_failure": None})
        raw = Raw()
        raw.output = (b"capture: OK\nsaved /staging/capture-work/7/a.3FR (10 bytes)\n"
            b"saved /staging/capture-work/7/a.3FR.thumbnail.jpg (2 bytes)\n"
            b"capture: camera filename was not reported\n")
        raw.stderr, raw.output_failure, raw.error, raw.used_grace_s = b"", None, None, None
        raw.exit = SimpleNamespace(exit_code=0, signal=None)
        return raw
    driver._run_step = run
    async def dispatch(): return None
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    request = SimpleNamespace(prepared_by=None, binding=binding, ticket=SimpleNamespace(target_id="7", run_id=9, attempt_id=1),
        output_storage="host", timeout_s=Decimal(10), dispatch_check=dispatch)
    result = _finish_immediate(driver._capture_control(request, prepare_only=False))
    assert result.outcome.status is api.AttemptStatus.SUCCEEDED
    assert validated == [(("a.3FR", "a.3FR.thumbnail.jpg"), "a.3FR.thumbnail.jpg")]
    assert next(item for item in result.outcome.observations if item.type == "photo_taken").version == 1


@pytest.mark.parametrize("status,size,digest,expected,selected", [
    ("complete", 100, "a" * 64, "matched", "sync"),
    ("complete", 99, "a" * 64, "mismatch", "camera"),
    ("complete", 100, "b" * 64, "mismatch", "camera"),
    ("missing", None, None, "missing", "camera"),
    ("invalid", None, None, "invalid", "camera"),
    ("unconfirmed", None, None, "unconfirmed", "camera"),
])
def test_single_photo_comparison_decides_unique_cache(responses, status, size, digest, expected, selected):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    binding = files.DeviceBinding("camera", "hasselblad_cfv100c")
    source = files.CameraFileIdentity(binding, "/mnt/a.3FR", "a.3FR",
        files.DownloadVariant.ORIGINAL, "a" * 64, "serial", 10, 100)
    result = files.decide_photo_cache(source, status=status, size_bytes=size, sha256=digest)
    assert result.comparison == expected and result.selected == selected


@pytest.mark.parametrize("status,size,digest", [("complete", 0, "a" * 64),
    ("complete", 100, "bad"), ("invalid", 100, "a" * 64), ("unknown", None, None)])
def test_photo_comparison_rejects_unreliable_or_contradictory_content(responses, status, size, digest):
    files = sys.modules["camctl.devices.drivers.hasselblad.files"]
    source = files.CameraFileIdentity(files.DeviceBinding("camera", "hasselblad_cfv100c"),
        "/mnt/a.3FR", "a.3FR", files.DownloadVariant.ORIGINAL, "a" * 64, "serial", 10, 100)
    with pytest.raises(ValueError):
        files.decide_photo_cache(source, status=status, size_bytes=size, sha256=digest)
