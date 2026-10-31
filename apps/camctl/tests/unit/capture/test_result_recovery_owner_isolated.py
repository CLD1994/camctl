"""恢复结果在消费者确定处置前由原拥有者持有。"""

import ast
import asyncio
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_download_ownership(monkeypatch):
    downloads = ModuleType("camctl.host_files.downloads")
    downloads.download_owned = lambda ticket: False
    monkeypatch.setitem(sys.modules, downloads.__name__, downloads)


def recovery_method():
    path = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CaptureRuntime")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "recover_attempt")
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[])

    @dataclass(frozen=True)
    class Finish:
        ticket: object
        outcome: object
        occurred_at: int
        retry_wait: bool = False

    @dataclass(frozen=True)
    class Pending:
        key: object
        finish: object
        observation: object
        returned_ns: int = 0
        result_listing: object = None
        result_disposition_ready: bool = True

    class Entries:
        def __init__(self, capture, database, ref):
            self.last_ref = ref

    def hold(runtime, saved, evidence):
        ticket = saved.ref.ticket
        runtime.pending_start_results[(ticket.run_id, ticket.attempt_id)] = Pending(
            "original", Finish(ticket, SimpleNamespace(outcome=saved.page.outcome), saved.occurred_at), None,
            result_listing=Entries(None, None, saved.ref), result_disposition_ready=False)

    ns = {"_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1), "PendingCallResult": Pending,
          "AttemptFinish": Finish, "new_operation_key": lambda: "original",
          "validate_outcome": lambda ticket, outcome, evidence: SimpleNamespace(outcome=outcome),
          "_recovered_call_outcome": lambda kind="adb_foreground_recovery": ("unknown", kind),
          "is_hasselblad_photo": lambda action: action["photo"], "SavedResultEntries": Entries,
          "_result_page_returned": lambda saved: saved.page.next_cursor is None or saved.page.outcome.error is not None,
          "_hold_recovered_result_page": hold,
          "ConsistencyError": RuntimeError, "closing": lambda value: value}
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), ns)
    return ns["recover_attempt"]


class Database:
    def __init__(self, last):
        self.last = last

    async def read(self, callback, *, description):
        if description == "读取拍摄当前责任":
            return (11,)
        if description == "读取拍摄直接事实":
            return {"status": 1}
        return self.last


@pytest.mark.parametrize("kind", ["none", "partial", "error", "complete"])
@pytest.mark.parametrize("photo", [False, True])
def test_recovered_original_waits_for_consumer(kind, photo):
    ticket = SimpleNamespace(run_id=9, attempt_id=1, operation="result")
    outcome = SimpleNamespace(error=object() if kind == "error" else None)
    saved = None if kind == "none" else SimpleNamespace(
        ref=SimpleNamespace(ticket=ticket), occurred_at=123,
        page=SimpleNamespace(next_cursor="next" if kind == "partial" else None, outcome=outcome))
    calls = []
    async def evidence(*args):
        return object()
    async def save(*args, **kwargs):
        calls.append(args)
    async def action_id(ticket):
        return 7
    async def action(identity):
        return {"photo": photo}
    runtime = SimpleNamespace(pending_start_results={}, database=Database(saved),
        capture=object(), wall_us=lambda: 456, monotonic_ns=lambda: 789,
        _attempt_recovery_evidence=evidence, _save_call_result=save,
        action_id_of_ticket=action_id, action=action,
        last_recovery_diagnostic=None)

    assert asyncio.run(recovery_method()(runtime, ticket))
    pending = runtime.pending_start_results[(9, 1)]
    assert not calls
    assert not pending.result_disposition_ready
    assert pending.result_listing is not None
    if kind in ("error", "complete"):
        assert pending.finish.outcome.outcome is outcome
        assert pending.finish.occurred_at == 123
    else:
        assert pending.finish.outcome.outcome == ("unknown",
            "local_result_recovery" if photo else "adb_foreground_recovery")


def test_existing_unsettled_owner_is_not_replaced_or_saved():
    ticket = SimpleNamespace(run_id=9, attempt_id=1, operation="result")
    owner = SimpleNamespace(result_disposition_ready=False)
    calls = []
    async def save(*args, **kwargs):
        calls.append(args)
    runtime = SimpleNamespace(pending_start_results={(9, 1): owner},
        _validate_call_result=lambda identity, pending: pending,
        _save_call_result=save, last_recovery_diagnostic=None)
    assert asyncio.run(recovery_method()(runtime, ticket))
    assert runtime.pending_start_results[(9, 1)] is owner
    assert not calls


def test_consumer_disposition_keeps_original_diagnostic_owner():
    path = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PendingCallResult")
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    ns = {"dataclass": dataclass, "field": field, "DiagnosticState": object, "SharedSave": object}
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), ns)
    original = ns["PendingCallResult"]("key", object(), None, result_disposition_ready=False)
    settled = replace(original, result_disposition_ready=True)
    assert settled.diagnostic is original.diagnostic
    assert settled.shared is original.shared
    assert settled.key == original.key


def extracted_function(name, namespace, source="handlers.py"):
    path = Path(__file__).parents[3] / "src/camctl/capture" / source
    tree = ast.parse(path.read_text())
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
    return namespace[name]


def test_discontinued_consumer_registers_facts_before_canceling_original_run():
    calls = []
    listing = object()
    async def held(runtime, ticket):
        return listing
    async def register(runtime, action_id, value):
        calls.append(("register", action_id, value))
    async def qualify(runtime, action_id, value):
        return value
    async def finish(runtime, value, **kwargs):
        calls.append(("finish", value, kwargs))
    ns = {"_held_listing": held, "_register_listing": register, "_qualify_listing_collection": qualify,
          "_NaturalTimeDeferred": RuntimeError,
          "_finish_listing_result": finish, "_listing_finalized": lambda value: False,
          "RunOutcome": SimpleNamespace(CANCELED="canceled")}
    consume = extracted_function("_settle_discontinued_listing", ns)
    asyncio.run(consume(object(), 4, object(), cancel_run=True))
    assert calls == [("register", 4, listing),
                     ("finish", listing, {"end_run": "canceled", "finalize_output_set": False})]


def test_discontinued_binding_failure_leaves_run_for_binding_consumer():
    calls = []
    async def held(runtime, ticket):
        return "listing"
    async def register(runtime, action_id, value):
        calls.append("facts")
    async def qualify(runtime, action_id, value):
        return value
    async def finish(runtime, value, **kwargs):
        calls.append(kwargs)
    ns = {"_held_listing": held, "_register_listing": register, "_qualify_listing_collection": qualify,
          "_NaturalTimeDeferred": RuntimeError,
          "_finish_listing_result": finish, "_listing_finalized": lambda value: False,
          "RunOutcome": SimpleNamespace(CANCELED="canceled")}
    consume = extracted_function("_settle_discontinued_listing", ns)
    asyncio.run(consume(object(), 4, object()))
    assert calls == ["facts", {"end_run": None, "finalize_output_set": False}]


def test_running_recovered_result_is_returned_to_its_consumer():
    ticket = SimpleNamespace(run_id=9, attempt_id=1)
    listing = object()
    async def activity(runtime, action_id):
        return 20
    async def original(runtime, responsibility, operation):
        return ticket
    async def held(runtime, value):
        return listing if runtime.recovered else None
    class Database:
        async def read(self, callback, *, description):
            return None
    class Runtime:
        recovered = False
        database = Database()
        pending_result_scans = {}
        async def resume_file_observations(self, action_id):
            pass
        async def last_attempt(self, responsibility):
            return (1,)
        async def _attempt_recovery_evidence(self, value):
            return object()
        async def recover_attempt(self, value, *, retry_wait):
            self.recovered = True
            return True
    ns = {"_activity_id_of": activity, "_original_ticket": original, "_held_listing": held,
          "_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1), "_result_page_returned": lambda value: False}
    advance = extracted_function("_listing_round_input", ns)
    assert asyncio.run(advance(Runtime(), 4)) is listing


def test_result_consumer_keeps_original_key_and_submits_selected_retry():
    @dataclass(frozen=True)
    class Finish:
        ticket: object
        outcome: object
        retry_wait: bool = False
        run_finish: object = None
    @dataclass(frozen=True)
    class Pending:
        key: str
        finish: object
        result_set: object = None
        completion_page: object = None
        finalized_page: object = None
        result_disposition_ready: bool = False
    ticket = SimpleNamespace(run_id=9, attempt_id=1)
    outcome = object()
    pending = Pending("original-key", Finish(ticket, SimpleNamespace(outcome=outcome)))
    listing = SimpleNamespace(already_saved=False, ticket=ticket, outcome=outcome, completion_page=None)
    class Runtime:
        pending_start_results = {(9, 1): pending}
        async def save_held_result(self, value):
            self.saved = self.pending_start_results[(9, 1)]
    ns = {"replace": replace, "ConsistencyError": RuntimeError}
    finish = extracted_function("_finish_listing_result", ns)
    runtime = Runtime()
    asyncio.run(finish(runtime, listing, retry_wait=True))
    assert runtime.saved.key == "original-key"
    assert runtime.saved.finish.ticket is ticket
    assert runtime.saved.finish.outcome.outcome is outcome
    assert runtime.saved.finish.retry_wait
    assert runtime.saved.result_disposition_ready


@pytest.mark.parametrize("complete", [False, True])
def test_scope_consumes_existing_result_without_trigger(complete):
    events = []
    async def qualify(runtime, owner, listing):
        return listing
    async def register(runtime, owner, listing):
        events.append("facts")
    async def finish(runtime, listing, **kwargs):
        events.append(kwargs)
    async def release(runtime, owner):
        events.append("release")
    common = SimpleNamespace(_qualify_listing_collection=qualify, _register_listing=register,
        _finish_listing_result=finish, _listing_finalized=lambda listing: complete,
        _NaturalTimeDeferred=RuntimeError)
    capture = ModuleType("camctl.capture")
    capture.handlers = common
    segment = ModuleType("camctl.capture.segment_completion")
    segment.release_segment_occupancy = release
    ns = {"RunOutcome": SimpleNamespace(SUCCEEDED="succeeded")}
    consume = extracted_function("settle_scope_listing", ns, "scope_closure.py")
    listing = SimpleNamespace(outcome=SimpleNamespace(error=None), already_saved=False)
    with patch.dict(sys.modules, {"camctl.capture": capture, segment.__name__: segment}):
        assert asyncio.run(consume(object(), 4, listing)) is complete
    assert events == (["facts", {"end_run": "succeeded", "finalize_output_set": True}, "release"]
                      if complete else ["facts", {"retry_wait": True}])
