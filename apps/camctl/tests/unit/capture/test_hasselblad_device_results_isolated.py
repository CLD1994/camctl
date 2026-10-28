"""DEVICE 结果编排使用原来源事实与有限尝试，导入时隔离 I/O 协作者。"""
import importlib.util
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    class Status(Enum):
        IN_FLIGHT = "in_flight"
        RETRY_WAIT = "retry_wait"
        LISTED = "listed"
        EXHAUSTED = "exhausted"
        CLOSED = "closed"
    class RunOutcome(Enum):
        SUCCEEDED = "succeeded"
        FAILED = "failed"
    calls = []
    async def finish(runtime, listing, **kwargs): calls.append(("finish", kwargs))
    async def conclude(runtime, action_id): calls.append(("conclude", action_id))
    async def close(runtime, action_id): calls.append(("close", action_id))
    async def original(runtime, responsibility, operation): return SimpleNamespace(target_id="7")
    async def listing(runtime, action_id): return runtime.listing
    handler = {"ListingPhase": Status, "_close_check_unconfirmed": close, "_conclude_activity": conclude,
        "_finish_listing_result": finish, "_listing_round_input": listing, "_original_ticket": original}
    class Failure:
        def __init__(self, code, details): self.code, self.details = code, details
    enum = SimpleNamespace(CAMERA_TAKE_PHOTO=1, RUNNING=2, SUCCEEDED=3, FAILED=4, CANCELED=6, EXPIRED=7,
        START=1, CONFIRMED=1)
    modules = {
        "camctl.capture.handlers": handler,
        "camctl.capture.media": {"RecordingFailure": Failure},
        "camctl.operations.attempts": {"RunOutcome": RunOutcome},
        "camctl.contracts.enums": {"enum_for": lambda name: enum},
        "camctl.host_files.capture_sources": {"CapturedArtifact": object, "archive_capture_file": None},
        "camctl.host_files.models": {"FileRef": object, "FilePurpose": object},
        "camctl.host_files.photo_formats": {"CAPTURE_FORMATS": {}},
        "camctl.capture.photo_format": {"saved_photo_format": lambda action: None},
        "camctl.operations.models": {"AttemptTicket": object, "EffectState": object},
        "camctl.operations.owned_calls": {"owned_tool_call": lambda function: function},
        "camctl.operations.result_format": {"read_result_document": lambda *args: None},
        "camctl.persistence.business": {"settle_required": None},
        "camctl.persistence.models": {"db_failure": lambda *args: ValueError(), "DbOutcomeKind": object,
            "StateDatabaseError": type("StateDatabaseError", (Exception,), {}), "db_outcome_for": lambda error: None},
        "camctl.persistence.repositories.capture": {"FinishCapture": object},
        "camctl.persistence.repositories.host_capture": {"HostPhotoComplete": object, "PrepareHostPhoto": object},
        "camctl.persistence.repositories.scheduling": {"ExpireActionRequest": object},
        "camctl.persistence.transaction": {"row_facts": lambda *args: None},
        "camctl.outputs.catalog": {"FileReference": object, "OutputCatalogFacts": object,
            "OutputDraft": object, "OutputKind": object},
    }
    for name, attributes in modules.items():
        stub = ModuleType(name)
        stub.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, stub)
    source = Path(__file__).parents[3] / "src/camctl/capture/host_photo.py"
    name = "camctl.capture._isolated_host_photo_results"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    async def source_drafts(runtime, action_id, ticket): return runtime.drafts
    async def activity(runtime, action_id): return 7
    module._device_source_drafts = source_drafts
    module._activity_id = activity
    return module, Status, RunOutcome, calls


def immediate(coroutine):
    with pytest.raises(StopIteration) as finished:
        coroutine.send(None)
    return finished.value.value


def runtime_for(listing, drafts):
    class Runtime:
        def __init__(self): self.listing, self.drafts, self.resumed = listing, drafts, False
        async def resume_capture_completions(self, action_id): self.resumed = True
        async def action(self, action_id): return {"status": 2}
    return Runtime()


def test_success_closes_original_results_and_returns_both_sources(api):
    module, status, run_status, calls = api
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=None), ticket=object())
    runtime = runtime_for(listing, ("original", "preview"))
    assert immediate(module._device_photo_results(runtime, 1)) == (("original", "preview"), None)
    assert runtime.resumed and calls == [("finish", {"end_run": run_status.SUCCEEDED}), ("conclude", 1)]


def test_retryable_result_preserves_original_and_waits_without_finishing_action(api):
    module, status, _, calls = api
    error = SimpleNamespace(code="device_error", details={"classification": "unconfirmed"})
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    assert immediate(module._device_photo_results(runtime, 1)) is None
    assert calls == [("finish", {"retry_wait": True})]


def test_invalid_preview_preserves_original_and_closes_result(api):
    module, status, run_status, calls = api
    error = SimpleNamespace(code="device_error", details={"classification": "invalid"})
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    drafts, failure = immediate(module._device_photo_results(runtime, 1))
    assert drafts == ("original",) and failure.code == "capture_failed"
    assert calls == [("finish", {"end_run": run_status.FAILED, "run_error": error}), ("conclude", 1)]


def test_exhausted_result_preserves_original_and_reports_unconfirmed(api):
    module, status, _, calls = api
    listing = SimpleNamespace(phase=status.EXHAUSTED, outcome=None, ticket=None)
    runtime = runtime_for(listing, ("original",))
    drafts, failure = immediate(module._device_photo_results(runtime, 1))
    assert drafts == ("original",) and failure.code == "capture_result_unconfirmed"
    assert failure.details == {"activity_id": "7", "reason": "outputs_unknown"}
    assert calls == [("close", 1), ("conclude", 1)]


def test_saved_invalid_result_is_not_replayed(api):
    module, status, _, calls = api
    error = SimpleNamespace(code="device_error", details={"classification": "invalid"})
    listing = SimpleNamespace(phase=status.CLOSED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    drafts, failure = immediate(module._device_photo_results(runtime, 1))
    assert drafts == ("original",) and failure.code == "capture_failed"
    assert calls == [("conclude", 1)]


def test_binding_failure_remains_distinct_and_closes_original_result(api):
    module, status, run_status, calls = api
    details = {"device_id": "camera", "expected_driver_id": "hasselblad_cfv100c",
               "actual_driver_id": "hasselblad_cfv100c", "reason": "physical_identity_mismatch"}
    error = SimpleNamespace(code="device_binding_unavailable", details=details)
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    drafts, failure = immediate(module._device_photo_results(runtime, 1))
    assert drafts == ("original",) and failure.code == "device_binding_unavailable"
    assert failure.details == details
    assert calls == [("finish", {"end_run": run_status.FAILED, "run_error": error}), ("conclude", 1)]


@pytest.mark.parametrize("fails", [False, True])
def test_role_cache_archives_selected_source_and_persists_actual_outcome(api, monkeypatch, fails):
    module, _, _, _ = api
    calls = []
    class ArchiveError(ValueError): pass
    async def archive(*args, **kwargs):
        calls.append(("archive", args[4]))
        if fails: raise ArchiveError("archive failed")
    class Artifact:
        size_bytes, sha256 = 100, "a" * 64
        @classmethod
        def from_json(cls, raw): return cls()
    module.CapturedArtifact = Artifact
    module.new_operation_key = lambda: "test-key"
    module._file_ref = lambda prepared, root: prepared
    async def prepare(runtime, action_id):
        return SimpleNamespace(original=SimpleNamespace(file_id=2, purpose="original"), preview=None)
    async def save(runtime, action_id, role, request=None): calls.append(("save", role, request))
    module._prepare_files, module._save_photo_cache = prepare, save
    module._owned_repository = lambda runtime, action_id: (object(), SimpleNamespace(staging=Path("/staging")))
    module.row_facts = lambda *args: {"size_bytes": None, "sha256": None, "last_error_json": None}
    cache = ModuleType("camctl.history.photo_cache_request")
    cache.PhotoCacheComplete = lambda *args: args
    sources = sys.modules["camctl.host_files.capture_sources"]
    sources.CaptureFileError, sources.archive_photo_cache = ArchiveError, archive
    downloads = ModuleType("camctl.host_files.downloads")
    class Owner:
        files_closed = True
        def __init__(self, *args): pass
        def claim(self): return True
        def release(self): calls.append(("release",))
    downloads.ResultDownloadWorkspace = Owner
    tasks = ModuleType("camctl.host_files.tasks")
    tasks.AsyncFileTask = lambda *args: SimpleNamespace(body=args[-1])
    tasks.FileTaskId = lambda value: value
    tasks.FileTaskError = type("FileTaskError", (ValueError,), {})
    diagnostics = ModuleType("camctl.logging_runtime.diagnostics")
    diagnostics.record_error = lambda *args, **kwargs: calls.append(("diagnostic",))
    errors = ModuleType("camctl.operations.error_diagnostics")
    errors.exception_diagnostic = lambda error: {"message": str(error)}
    for stub in (cache, downloads, tasks, diagnostics, errors):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    class Database:
        async def read(self, function, **kwargs): return function(SimpleNamespace(connection=object()))
    class Executor:
        async def run_owned_async_file_task(self, task): await task.body(None)
    class Runtime:
        database, file_executor, pending_host_photo_completions = Database(), Executor(), {}
        wall_us = lambda self: 500
        async def action(self, action_id): return {}
    outcome = SimpleNamespace(observations=(SimpleNamespace(type="photo_result_attempt", version=2,
        data={"role": "original", "comparison": {"selected": "camera", "sync_artifact": None},
              "steps": [{"artifact": {}}]}),))
    ticket = SimpleNamespace(target_id="7")
    saved = SimpleNamespace(page=SimpleNamespace(outcome=outcome, entries=(SimpleNamespace(size_bytes=100),)),
                            ref=SimpleNamespace(ticket=ticket, event_id=4))
    immediate(module.archive_device_photo_cache(Runtime(), 1, saved, 3))
    assert calls[0] == ("archive", "camera")
    result = next(item for item in calls if item[0] == "save")
    assert result[1] == "original" and result[2][-1] == ({"message": "archive failed"} if fails else None)
    assert (any(item[0] == "diagnostic" for item in calls)) is fails
