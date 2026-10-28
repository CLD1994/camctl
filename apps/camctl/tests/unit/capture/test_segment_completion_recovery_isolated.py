"""分段完成选择与恢复不导入包资源或真实协作者。"""

from __future__ import annotations

import ast
import asyncio
from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[3] / "src" / "camctl" / "capture"


def extract(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class Evidence(Enum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class Timing:
    def __init__(self, duration_ms, startup_margin_ms):
        self.duration_ms = duration_ms
        self.startup_margin_ms = startup_margin_ms

    def recording_lower_bound_ns(self, elapsed):
        return max(0, elapsed - self.startup_margin_ms * 1_000_000)


@pytest.mark.parametrize("margin", [0, 1, 10, 100])
@pytest.mark.parametrize("elapsed, expected", [
    (9_000_000_000, Evidence.UNKNOWN),
    (9_000_000_001, Evidence.CONFIRMED),
    (10_000_000_000, Evidence.CONFIRMED),
])
def test_osmo_overrecord_uses_strict_control_lower_bound(margin, elapsed, expected):
    control_facts = extract(ROOT / "segment_completion.py", "_control_facts", {
        "CaptureTiming": Timing, "EvidenceState": Evidence,
    })
    action = {"execution_spec_json": {"target_duration_ms": 9000}}
    complete, overrun = control_facts(SimpleNamespace(repair_margin_s=margin), action,
                                      {"control_elapsed_ns": elapsed}, strict_overrun=True)
    assert complete is Evidence.CONFIRMED
    assert overrun is expected


def test_control_elapsed_missing_does_not_invent_evidence():
    control_facts = extract(ROOT / "segment_completion.py", "_control_facts", {
        "CaptureTiming": Timing, "EvidenceState": Evidence,
    })
    assert control_facts(None, {}, {"control_elapsed_ns": None}, strict_overrun=True) == (
        Evidence.UNKNOWN, Evidence.UNKNOWN)


def test_osmo_confirmed_overrecord_selects_two_of_three_segments():
    scope_states = SimpleNamespace(NONEMPTY="nonempty", EMPTY="empty", UNKNOWN="unknown")
    bases = SimpleNamespace(FIRST_N="first_n", UNKNOWN="unknown", EMPTY_PREFIX="empty",
                            ALL_QUALIFYING="all", PREFIX_FIRST_N="prefix_first_n",
                            CONTIGUOUS_PREFIX="prefix")
    overrecord = SimpleNamespace(CONFIRMED=object())
    namespace = {"_collection_complete": lambda facts: True, "EvidenceState": Evidence,
                 "DefaultScopeState": scope_states, "DefaultScopeBasis": bases,
                 "OverrecordStatus": overrecord, "_start_gap": lambda state, start: None,
                 "DefaultSegmentScope": lambda **kwargs: SimpleNamespace(**kwargs),
                 "default_segment_count": extract(ROOT / "segments.py", "default_segment_count",
                                                  {"MAX_OBJECT_ID": 2**63 - 1, "_SEGMENT_DURATION_MS": 4500})}
    scope = extract(ROOT / "segments.py", "_scope", namespace)
    state = SimpleNamespace(set_ref=object(), order_unknown=False,
                            first_original=SimpleNamespace(key=1, cursor="first"),
                            prefix_unknown=False, nonzero_original_count=3,
                            target_duration_ms=9000, last_nonzero_cursor="third",
                            first_n_cursor="second")
    facts = SimpleNamespace(start=Evidence.CONFIRMED, start_key=1)
    result = scope(state, facts, overrecord=overrecord.CONFIRMED, first_gap=None, gap_count=0)
    assert (result.member_count, result.first_cursor, result.last_cursor) == (2, "first", "second")


@pytest.mark.parametrize("state, inspection, expected", [
    (2, None, []), (2, (1,), ["recover"]),
    (3, (1,), ["release", "saved"]), (3, None, ["release", "saved"]),
])
def test_existing_action6_responsibility_precedes_reclassification(state, inspection, expected):
    calls = []
    saved = object()
    ref = object()

    class Connection:
        def execute(self, sql, args):
            assert "segment_inspections" in sql and args == (31,)
            return SimpleNamespace(fetchone=lambda: inspection)

    class Database:
        async def read(self, callback):
            return callback(SimpleNamespace(connection=Connection()))

    repository = SimpleNamespace(read_set=lambda ref, owned: {"id": 31, "state": state},
                                 read_decision=lambda ref, owned: saved)

    async def publish(runtime, action, actual_ref, decision):
        assert actual_ref is ref and decision is saved
        calls.append("saved")

    async def recover(runtime, action):
        calls.append("recover")

    async def release(runtime, action_id):
        assert action_id == 1
        calls.append("release")

    module = ModuleType("camctl.capture.action6_recovery")
    module.advance_action6_recovery = recover
    completion = ModuleType("camctl.capture.segment_completion")
    completion.release_segment_occupancy = release
    namespace = {"enum_for": lambda column: SimpleNamespace(ASSESSED=3),
                 "publish_action6_segment_result": publish}
    resume = extract(ROOT / "action6_completion.py", "_resume_action6_segment_decision", namespace)
    with patch.dict(sys.modules, {module.__name__: module, completion.__name__: completion}):
        result = asyncio.run(resume(SimpleNamespace(database=Database()), {"id": 1}, ref, repository))
    assert calls == expected
    assert result is bool(expected)


@pytest.mark.parametrize("native", [False, True])
def test_action6_entry_resumes_before_reclassifying(native):
    calls = []
    action = {"id": 1, "type": 2, "cancel_requested": False}
    ref = object()

    async def prepare(runtime, actual_action, listing, **kwargs):
        return ref

    async def resume(runtime, actual_action, actual_ref, repository):
        assert actual_action is action and actual_ref is ref
        calls.append("resume")
        return True

    async def classify(*args, **kwargs):
        pytest.fail("已有责任不能重新分类")

    async def listing(runtime, action):
        return object()

    async def runtime_action(action_id):
        return action

    capture = ModuleType("camctl.capture")
    capture.__path__ = []
    capture.handlers = ModuleType("camctl.capture.handlers")
    capture.handlers._local_recording_listing = listing
    completion = ModuleType("camctl.capture.segment_completion")
    completion.release_segment_occupancy = object()
    completion._control_facts = object()
    completion._save = object()
    recording = ModuleType("camctl.capture.recording")
    recording.RecordingPhase = object()
    models = ModuleType("camctl.capture.segment_models")
    models.EvidenceState = Evidence
    storage = ModuleType("camctl.capture.segment_storage")
    storage.SegmentResultRef = type("SegmentResultRef", (), {})
    modules = {m.__name__: m for m in (capture, capture.handlers, completion, recording, models, storage)}
    namespace = {"prepare_action6_segment_set": prepare, "SegmentRepository": object,
                 "_resume_action6_segment_decision": resume,
                 "classify_action6_segment_set": classify}
    name = "advance_action6_native_result" if native else "advance_action6_segmented_recording"
    entry = extract(ROOT / "action6_completion.py", name, namespace)
    runtime = SimpleNamespace(action=runtime_action, database=object())
    with patch.dict(sys.modules, modules):
        if native:
            asyncio.run(entry(runtime, 1))
        else:
            asyncio.run(entry(runtime, action, object()))
    assert calls == ["resume"]

