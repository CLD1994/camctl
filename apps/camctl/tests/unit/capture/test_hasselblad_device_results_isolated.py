"""DEVICE 结果编排保留原来源事实，文件协作者由局部替身隔离。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture import handlers, host_photo
from camctl.capture.handlers import ListingPhase
from camctl.contracts.enums import enum_for
from camctl.operations.attempts import RunOutcome


@pytest.fixture
def api(monkeypatch):
    Status = ListingPhase
    calls = []
    async def finish(runtime, listing, **kwargs): calls.append(("finish", kwargs))
    async def conclude(runtime, action_id): calls.append(("conclude", action_id))
    async def close(runtime, action_id): calls.append(("close", action_id))
    async def original(runtime, responsibility, operation): return SimpleNamespace(target_id="7")
    async def listing(runtime, action_id): return runtime.listing
    async def register(runtime, action_id, listing):
        if getattr(runtime, "registration_error", None) is not None:
            raise runtime.registration_error
        runtime.registered = True
        calls.append(("register", action_id))
    handler = {"ListingPhase": Status, "_close_check_unconfirmed": close, "_conclude_activity": conclude,
        "_finish_listing_result": finish, "_listing_round_input": listing, "_original_ticket": original,
        "_register_listing": register}
    for name, value in handler.items():
        monkeypatch.setattr(handlers, name, value)
    module = host_photo
    async def source_drafts(runtime, action_id, ticket):
        if runtime.listing.phase in (Status.LISTED, Status.CLOSED):
            assert runtime.registered, "原可靠照片页必须先登记才能形成产物"
        return runtime.drafts
    async def activity(runtime, action_id): return 7
    monkeypatch.setattr(module, "_device_source_drafts", source_drafts)
    monkeypatch.setattr(module, "_activity_id", activity)
    return module, Status, RunOutcome, calls


def immediate(coroutine):
    with pytest.raises(StopIteration) as finished:
        coroutine.send(None)
    return finished.value.value


def runtime_for(listing, drafts):
    class Runtime:
        def __init__(self):
            self.listing, self.drafts, self.resumed = listing, drafts, False
            self.registered = False
        async def resume_capture_completions(self, action_id): self.resumed = True
        async def action(self, action_id):
            return {"status": int(enum_for("actions.status").RUNNING)}
    return Runtime()


def test_success_closes_original_results_and_returns_both_sources(api):
    module, status, run_status, calls = api
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=None), ticket=object())
    runtime = runtime_for(listing, ("original", "preview"))
    assert immediate(module._device_photo_results(runtime, 1)) == (("original", "preview"), None)
    assert runtime.resumed and calls == [("register", 1), ("finish", {"end_run": run_status.SUCCEEDED}), ("conclude", 1)]


def test_retryable_result_preserves_original_and_waits_without_finishing_action(api):
    module, status, _, calls = api
    error = SimpleNamespace(code="device_error", details={"classification": "unconfirmed"})
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    assert immediate(module._device_photo_results(runtime, 1)) is None
    assert calls == [("register", 1), ("finish", {"retry_wait": True})]


def test_invalid_preview_preserves_original_and_closes_result(api):
    module, status, run_status, calls = api
    error = SimpleNamespace(code="device_error", details={"classification": "invalid"})
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=error), ticket=object())
    runtime = runtime_for(listing, ("original",))
    drafts, failure = immediate(module._device_photo_results(runtime, 1))
    assert drafts == ("original",) and failure.code == "capture_failed"
    assert calls == [("register", 1), ("finish", {"end_run": run_status.FAILED, "run_error": error}), ("conclude", 1)]


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
    assert calls == [("register", 1), ("conclude", 1)]


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
    assert calls == [("register", 1), ("finish", {"end_run": run_status.FAILED, "run_error": error}), ("conclude", 1)]


def test_result_registration_failure_preserves_original_for_recovery(api):
    module, status, _, calls = api
    listing = SimpleNamespace(phase=status.LISTED, outcome=SimpleNamespace(error=None), ticket=object())
    runtime = runtime_for(listing, ("original", "preview"))
    runtime.registration_error = ValueError("原照片页尚未可靠登记")
    with pytest.raises(ValueError) as failed:
        immediate(module._device_photo_results(runtime, 1))
    assert failed.value is runtime.registration_error
    assert not calls


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
    monkeypatch.setattr(module, "CapturedArtifact", Artifact)
    monkeypatch.setattr(module, "new_operation_key", lambda: "test-key")
    monkeypatch.setattr(module, "_file_ref", lambda prepared, root: prepared)
    async def prepare(runtime, action_id):
        return SimpleNamespace(original=SimpleNamespace(file_id=2, purpose="original"), preview=None)
    async def save(runtime, action_id, role, request=None): calls.append(("save", role, request))
    monkeypatch.setattr(module, "_prepare_files", prepare)
    monkeypatch.setattr(module, "_save_photo_cache", save)
    monkeypatch.setattr(module, "_owned_repository",
        lambda runtime, action_id: (object(), SimpleNamespace(staging=Path("/staging"))))
    monkeypatch.setattr(module, "row_facts",
        lambda *args: {"size_bytes": None, "sha256": None, "last_error_json": None})
    monkeypatch.setattr(module, "saved_photo_format", lambda action: None)
    from camctl.host_files import capture_sources as sources, downloads
    from camctl.logging_runtime import diagnostics
    from camctl.operations import error_diagnostics
    monkeypatch.setattr(sources, "CaptureFileError", ArchiveError)
    monkeypatch.setattr(sources, "archive_photo_cache", archive)
    class Owner:
        files_closed = True
        close_errors = ()
        def __init__(self, *args): pass
        def claim(self): return True
        def release(self): calls.append(("release",))
    monkeypatch.setattr(downloads, "ResultDownloadWorkspace", Owner)
    monkeypatch.setattr(diagnostics, "record_error",
        lambda *args, **kwargs: calls.append(("diagnostic",)))
    monkeypatch.setattr(error_diagnostics, "exception_diagnostic",
        lambda error: {"message": str(error)})
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
    assert result[1] == "original" and result[2].archive_error == ({"message": "archive failed"} if fails else None)
    assert (any(item[0] == "diagnostic" for item in calls)) is fails
