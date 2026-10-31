"""设备结果消费者使用内存替身验证原结果交接。"""

import ast
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


SOURCE = Path(__file__).parents[3] / "src/camctl"


def source_function(relative, name, namespace):
    path = SOURCE / relative
    node = next(item for item in ast.parse(path.read_text()).body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
    return namespace[name]


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


PHASE = SimpleNamespace(IN_FLIGHT="in_flight", RETRY_WAIT="wait", EXHAUSTED="exhausted")
UNKNOWN = object()
FAILED = object()
UNCONFIRMED = object()


class MemoryDatabase:
    def __init__(self):
        self.connection = object()

    async def read(self, callback, **kwargs):
        return callback(self)


def environment(listing, *, used=1, maximum=3):
    events = []

    class Repository:
        def read_prepared_result_ref(self, action_id, owned):
            return None
        def prepare_result_set(self, action_id, occurred_at, owned):
            events.append("prepare")
            return SimpleNamespace(source_kind="retained", activity_id=7)

    async def resume(*args):
        pass
    async def register(runtime, action_id, actual):
        events.append("files")
        assert actual is listing
    async def get_listing(runtime, action_id):
        return listing
    async def activity(runtime, action_id):
        return 7
    async def finish(runtime, actual, **kwargs):
        assert actual is listing
        events.append(("finish", kwargs))
    common = SimpleNamespace(ListingPhase=PHASE, _listing_round=get_listing,
        _activity_id_of=activity, _finish_listing_result=finish,
        _listing_finalized=lambda value: False,
        RunOutcome=SimpleNamespace(CANCELED="canceled"))
    async def action(action_id):
        return {"cancel_requested": False}
    runtime = SimpleNamespace(database=MemoryDatabase(), pending_segment_writes={},
        action=action, wall_us=lambda: 123, check_config=SimpleNamespace(max_attempts=maximum))
    ns = {"resume_segment_writes": resume, "register_segment_observations": register,
          "SegmentRepository": Repository,
          "row_facts": lambda connection, table, identity: {"attempts_used": used},
          "_control_facts": lambda *args: (None, None)}
    capture = module("camctl.capture", handlers=common)
    current = module("camctl.persistence.repositories.segment_current", current_decision=object())
    return runtime, ns, common, events, {capture.__name__: capture, current.__name__: current}


def test_osmo_recording_result_error_retries_original_attempt_within_budget():
    listing = SimpleNamespace(phase="listed", ticket=SimpleNamespace(run_id=8),
        outcome=SimpleNamespace(error="read_error"), already_saved=False)
    runtime, ns, _, events, modules = environment(listing, used=2, maximum=3)
    advance = source_function("capture/segment_completion.py", "advance_segmented_recording", ns)
    with patch.dict(sys.modules, modules):
        asyncio.run(advance(runtime, {"id": 4}, object()))
    assert events == ["files", ("finish", {"retry_wait": True})]


@pytest.mark.parametrize("status", [UNKNOWN, FAILED], ids=["recovery_unknown", "actual_read_error"])
def test_osmo_native_read_error_remains_unconfirmed_and_retries(status):
    listing = SimpleNamespace(phase="listed", ticket=SimpleNamespace(run_id=8),
        outcome=SimpleNamespace(status=status, error="read_error"), already_saved=False)
    runtime, ns, _, events, modules = environment(listing)
    result = SimpleNamespace(UNMET=object(), SUFFICIENT=object(), UNCONFIRMED=UNCONFIRMED)
    native_ns = {"CurrentSegmentResult": result,
        "SegmentMemberRole": SimpleNamespace(ORIGINAL=object()),
        "OwnershipState": SimpleNamespace(OWNED=object()),
        "FileCompletionState": SimpleNamespace(COMPLETE=object()),
        "NativeClassification": lambda current_result, *args: SimpleNamespace(current_result=current_result),
        "RecordingFailure": lambda *args: object()}
    classify = source_function("capture/native_segments.py", "classify_native_members", native_ns)
    observed = []
    def classify_and_record(*args, **kwargs):
        value = classify(*args, **kwargs)
        observed.append(value.current_result)
        return value
    @asynccontextmanager
    async def members(database, value):
        yield lambda: iter(())
    ordered = module("camctl.persistence.repositories.segment_listing", ordered_listing_members=members)
    modules[ordered.__name__] = ordered
    ns.update(classify_native_members=classify_and_record, CurrentSegmentResult=result,
              SegmentSourceKind=SimpleNamespace(COMPLETE_SCAN="complete"))
    advance = source_function("capture/native_completion.py", "advance_native_result", ns)
    with patch.dict(sys.modules, modules):
        asyncio.run(advance(runtime, 4))
    assert observed == [UNCONFIRMED]
    assert events == ["files", "prepare", ("finish", {"retry_wait": True})]


def test_action6_cancel_saves_original_before_preparing_set():
    listing = SimpleNamespace(phase="listed", ticket=SimpleNamespace(run_id=8),
        outcome=SimpleNamespace(error="read_error"), already_saved=False)
    runtime, ns, _, events, modules = environment(listing)
    class StopAfterPrepare(Exception):
        pass
    repository = ns["SegmentRepository"]
    def prepare(self, action_id, occurred_at, owned):
        events.append("prepare")
        raise StopAfterPrepare
    repository.prepare_result_set = prepare
    segment = module("camctl.capture.segment_completion", _prepare_members=object(), _save=object(),
        register_segment_observations=ns["register_segment_observations"],
        resume_segment_writes=ns["resume_segment_writes"])
    modules[segment.__name__] = segment
    advance = source_function("capture/action6_completion.py", "prepare_action6_segment_set", ns)
    with patch.dict(sys.modules, modules), pytest.raises(StopAfterPrepare):
        asyncio.run(advance(runtime, {"id": 4}, listing, canceled=True))
    assert events == ["files", ("finish", {"end_run": "canceled"}), "prepare"]


def test_unknown_without_pages_is_accepted_by_both_segment_consumers():
    entries = module("camctl.capture.result_scans", SavedResultEntries=type("SavedResultEntries", (), {}))
    operations = module("camctl.operations.models", AttemptStatus=SimpleNamespace(UNKNOWN=UNKNOWN))
    modules = {entries.__name__: entries, operations.__name__: operations}
    ticket = object()
    listing = SimpleNamespace(entries=(), already_saved=False, ticket=ticket,
        outcome=SimpleNamespace(status=UNKNOWN))
    class Capture:
        def read_last_result_page(self, actual, owned):
            assert actual is ticket
            return None
    runtime = SimpleNamespace(database=MemoryDatabase(), capture=Capture())
    register = source_function("capture/segment_completion.py", "register_segment_observations", {})
    ordered = source_function("persistence/repositories/segment_listing.py", "ordered_listing_members",
                              {"asynccontextmanager": asynccontextmanager})
    async def consume():
        await register(runtime, 4, listing)
        async with ordered(runtime.database, listing) as members:
            assert list(members()) == []
    with patch.dict(sys.modules, modules):
        asyncio.run(consume())
