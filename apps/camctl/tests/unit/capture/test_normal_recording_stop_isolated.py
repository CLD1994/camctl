"""隔离真实包资源和数据库，检查正常 STOP 来源的事务事实。"""

from __future__ import annotations

import ast
import asyncio
from contextlib import closing
from contextlib import nullcontext
from enum import IntEnum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "persistence" / "repositories" / "normal_recording_stop.py"


class StartReturn(IntEnum):
    SENT = 1
    STARTED = 2


def load_functions(rows, events):
    tree = ast.parse(SOURCE.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    enums = {
        "actions.type": SimpleNamespace(CAMERA_RECORD=2),
        "operation_runs.kind": SimpleNamespace(START=1, STOP=2),
        "operation_runs.status": SimpleNamespace(SUCCEEDED=3),
        "operation_attempts.status": SimpleNamespace(SUCCEEDED=2),
        "operation_attempts.effect_state": SimpleNamespace(CONFIRMED=3),
        "device_activities.activity_state": SimpleNamespace(ENDED=3),
    }
    ns = {"Mapping": dict, "is_json_integer": lambda x: type(x) is int,
          "json_equal": lambda a, b: type(a) is type(b) and a == b,
          "ConsistencyError": ValueError, "MAX_OBJECT_ID": 2**63 - 1,
          "enum_for": enums.__getitem__, "StartReturn": StartReturn, "closing": closing,
          "CaptureResultPolicy": SimpleNamespace(ACTION6_VIDEO_SEGMENTS_V1=SimpleNamespace(value="action6_video_segments_v1")),
          "row_facts": lambda connection, table, identity: rows.get((table, identity)),
          "read_event": lambda connection, identity: SimpleNamespace(event=events[identity])}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), "exec"), ns)
    return ns


def facts():
    action = {"id": 1, "type": 2, "execution_spec_json": {"target_duration_ms": 9000,
              "startup_margin_ms": 1000, "start_return_meaning": 1,
              "result_policy": "action6_video_segments_v1"}}
    start_result = {"format_version": 1, "observations": [{"type": "recording_sent", "version": 1,
                    "data": {"activity_id": "2"}}]}
    start = {"id": 7, "run_id": 3, "status": 2, "effect_state": 3, "error_json": None,
             "result_event_id": 10, "result_json": start_result}
    stop = {"id": 8, "run_id": 4, "attempt_no": 1, "status": 2, "effect_state": 3,
            "error_json": None, "intent_event_id": 20, "result_event_id": 30,
            "result_json": {"observations": [{"type": "stop_confirmed", "version": 1,
                                                "data": {"activity_id": "2"}}]}}
    run = {"id": 4, "kind": 2, "action_id": 1, "activity_id": 2, "responsibility_key": "stop/1"}
    start_run = {"id": 3, "kind": 1, "action_id": 1, "activity_id": 2, "responsibility_key": "start/1"}
    activity = {"id": 2, "action_id": 1, "sent_at": 100}
    def row(table, row_id, values):
        return SimpleNamespace(table=table, row_id=row_id, after=SimpleNamespace(values=values))
    start_event = SimpleNamespace(event_id=10, event_type=12, occurred_at=100,
        rows=[row("operation_attempts", 7, {k: start[k] for k in ("status", "effect_state", "error_json", "result_json")})])
    stop_event = SimpleNamespace(event_id=30, event_type=12, occurred_at=200,
        evidence={"normal_recording_stop": {"start_result_event_id": 10, "elapsed_before_stop_ns": 10_000_000_000}},
        rows=[row("operation_attempts", 8, {k: stop[k] for k in ("status", "effect_state", "error_json", "result_json")})])
    finish = SimpleNamespace(event_type=10, reason=3, occurred_at=200,
                             rows=[row("operation_runs", 4, {"status": 3})])
    end = SimpleNamespace(event_type=13, reason=2, occurred_at=200,
                          rows=[row("device_activities", 2, {"activity_state": 3})],
                          evidence={"observation": {"stop_result_event_id": 30, "control_elapsed_ns": 10_100_000_000}})
    rows = {("actions", 1): action, ("device_activities", 2): activity,
            ("operation_attempts", 7): start, ("operation_attempts", 8): stop,
            ("operation_runs", 3): start_run, ("operation_runs", 4): run}
    return rows, {10: start_event, 30: stop_event, 31: finish, 32: end}


def test_normal_stop_requires_same_transaction_and_original_start():
    rows, events = facts()
    ns = load_functions(rows, events)
    reads = SimpleNamespace(connection=object(), transaction_event=lambda identity, transaction: events[identity])
    context = SimpleNamespace(current_reads=reads, transaction=SimpleNamespace(first_event_id=30, last_event_id=32))
    ns["verify_normal_stop_group"](events[30], context, rows[("operation_attempts", 8)], rows[("operation_runs", 4)])


@pytest.mark.parametrize("case", ["short", "boolean", "different_start", "start_failed", "wrong_action",
                                  "wrong_activity", "wrong_meaning", "retry", "stop_failed",
                                  "wrong_stop_observation", "missing_source"])
def test_source_rejects_invalid_original_facts(case):
    rows, events = facts()
    source = events[30].evidence["normal_recording_stop"]
    if case == "short": source["elapsed_before_stop_ns"] -= 1
    elif case == "boolean": source["elapsed_before_stop_ns"] = True
    elif case == "different_start": source["start_result_event_id"] = 30
    elif case == "start_failed": rows[("operation_attempts", 7)]["status"] = 3
    elif case == "wrong_action": rows[("operation_runs", 3)]["action_id"] = 9
    elif case == "wrong_activity": rows[("device_activities", 2)]["action_id"] = 9
    elif case == "wrong_meaning": rows[("actions", 1)]["execution_spec_json"]["start_return_meaning"] = 2
    elif case == "retry": rows[("operation_attempts", 8)]["attempt_no"] = 2
    elif case == "stop_failed": rows[("operation_attempts", 8)]["error_json"] = {"code": "error"}
    elif case == "wrong_stop_observation": rows[("operation_attempts", 8)]["result_json"]["observations"] = []
    else: del events[30].evidence["normal_recording_stop"]
    ns = load_functions(rows, events)
    with pytest.raises(ValueError):
        ns["verify_normal_recording_stop"](object(), events[30], rows[("operation_attempts", 8)],
                                            rows[("operation_runs", 4)], rows[("actions", 1)])


@pytest.mark.parametrize("case", ["missing_read", "partial_group", "wrong_finish", "wrong_activity", "short_result"])
def test_guard_rejects_partial_or_inconsistent_group(case):
    rows, events = facts()
    reads = SimpleNamespace(connection=object(), transaction_event=lambda identity, transaction: events[identity])
    context = SimpleNamespace(current_reads=reads, transaction=SimpleNamespace(first_event_id=30, last_event_id=32))
    if case == "missing_read": context.current_reads = None
    elif case == "partial_group": context.transaction.last_event_id = 31
    elif case == "wrong_finish": events[31].rows[0].row_id = 99
    elif case == "wrong_activity": events[32].rows[0].row_id = 99
    else: events[32].evidence["observation"]["control_elapsed_ns"] = 9_999_999_999
    ns = load_functions(rows, events)
    with pytest.raises(ValueError):
        ns["verify_normal_stop_group"](events[30], context, rows[("operation_attempts", 8)], rows[("operation_runs", 4)])


@pytest.mark.parametrize("case, expected", [("saved", True), ("absent", False), ("missing_result", False)])
def test_consumer_reads_only_saved_stop_source(case, expected):
    rows, events = facts()
    if case == "absent": events[30].evidence.clear()

    class Cursor:
        def fetchone(self): return None if case == "missing_result" else (8, 30)
        def close(self): pass

    class Connection:
        def execute(self, sql, parameters):
            assert parameters == ("stop/1", 2, 3)
            return Cursor()

    ns = load_functions(rows, events)
    assert ns["read_normal_recording_stop"](Connection(), rows[("actions", 1)], 2) is expected


def test_consumer_rejects_corrupt_saved_source_instead_of_assuming_absent():
    rows, events = facts()
    events[30].evidence["normal_recording_stop"]["elapsed_before_stop_ns"] = 1

    class Cursor:
        def fetchone(self): return (8, 30)
        def close(self): pass

    ns = load_functions(rows, events)
    with pytest.raises(ValueError):
        ns["read_normal_recording_stop"](SimpleNamespace(execute=lambda sql, parameters: Cursor()),
                                          rows[("actions", 1)], 2)


@pytest.mark.parametrize("case, has_source", [
    ("normal", True), ("delayed", True), ("cancel", False), ("recovery", False),
    ("retry", False), ("ticket_retry", False), ("no_anchor", False), ("missing_start", False),
    ("failed_with_effect", False),
])
def test_stop_owner_only_forms_normal_source_from_eligible_actual_result(case, has_source):
    handlers = SOURCE.parents[2] / "capture" / "handlers.py"
    tree = ast.parse(handlers.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stop_call")
    function.decorator_list = []
    confirmed = object()
    succeeded = object()
    failure = object()
    state = SimpleNamespace(anchor_from_current_session=case != "no_anchor", stop_target_ns=10_000_000_000,
                            stop_attempts_used=1 if case == "retry" else 0)
    results = []
    ticket = SimpleNamespace(target_id="2", run_id=4, attempt_id=2 if case == "ticket_retry" else 1)

    class Cursor:
        def fetchone(self): return None if case == "missing_start" else (10,)
        def close(self): pass

    class Database:
        async def write(self, callback, **kwargs):
            return SimpleNamespace(kind="completed", value=SimpleNamespace(disposition="granted", ticket=ticket))

        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: Cursor())))

    class Stopper:
        async def stop(self, request):
            return SimpleNamespace(outcome=None, error=None, diagnostics_check=None)

    class Runtime:
        database = Database()
        operations = SimpleNamespace(begin_attempt=None)
        stopper = Stopper()
        stop_config = SimpleNamespace(retry_interval_s=1, max_attempts=3, timeout_s=10)
        on_recording_deadline = None
        def monotonic_ns(self): return 20_000_000_000 if case == "delayed" else 10_000_000_000
        def wall_us(self): return 200
        async def retry_wait_remaining(self, *args, **kwargs): return None
        async def action(self, identity): return {"cancel_requested": case == "cancel"}
        async def finish(self, ticket, call, **kwargs): results.append(kwargs["host_timer_stop"])

    async def recording_state(action_id): return state
    async def activity_id(runtime, action_id): return 2
    class Source:
        def __init__(self, start_result_event_id, elapsed_before_stop_ns):
            self.start_result_event_id = start_result_event_id
            self.elapsed_before_stop_ns = elapsed_before_stop_ns

    tasks = ModuleType("camctl.devices.tasks")
    tasks.CaptureResultPolicy = SimpleNamespace(
        ACTION6_VIDEO_SEGMENTS_V1=SimpleNamespace(value="action6_video_segments_v1"),
        OSMO360II_RECORD_SEGMENTS_V1=SimpleNamespace(value="osmo"))
    models = ModuleType("camctl.capture.models")
    models.NormalRecordingStop = Source
    call = SimpleNamespace(status=failure if case == "failed_with_effect" else succeeded,
                           error=object() if case == "failed_with_effect" else None, effect=confirmed)
    ns = {"_ACTION_TYPE": SimpleNamespace(CAMERA_RECORD=2, CAMERA_TIMELAPSE=3),
          "_ATTEMPT_STATUS": SimpleNamespace(SUCCEEDED=2), "_EFFECT_STATE": SimpleNamespace(CONFIRMED=3),
          "EffectState": SimpleNamespace(CONFIRMED=confirmed), "AttemptStatus": SimpleNamespace(SUCCEEDED=succeeded),
          "AttemptIntent": lambda **kwargs: kwargs, "AttemptTarget": lambda **kwargs: kwargs,
          "OperationKind": SimpleNamespace(STOP="stop"), "new_operation_key": lambda: "key",
          "DbOutcomeKind": SimpleNamespace(COMPLETED="completed"), "BeginDisposition": SimpleNamespace(GRANTED="granted"),
          "_recording_port": lambda runtime: SimpleNamespace(recording_state=recording_state),
          "_activity_id_of": activity_id, "closing": closing, "measure_stage": lambda *args, **kwargs: nullcontext(),
          "ControlRequest": lambda **kwargs: kwargs, "_binding": lambda action: object(),
          "_target_duration_ms": lambda action: 9000, "_operation_outcome": lambda *args, **kwargs: (call, True),
          "HostTimerStopSave": lambda elapsed, source=None: SimpleNamespace(elapsed=elapsed, source=source),
          "RunOutcome": SimpleNamespace(SUCCEEDED="success"), "OutcomeValidationError": ValueError,
          "HandlerOutcome": lambda *args: args}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(handlers), "exec"), ns)
    action = {"id": 1, "type": 2, "device_id": "camera", "effective_params_json": {},
              "cancel_requested": False, "execution_spec_json": {"result_policy": "action6_video_segments_v1",
              "startup_margin_ms": 1000}}
    with patch.dict(sys.modules, {tasks.__name__: tasks, models.__name__: models}):
        asyncio.run(ns["_stop_call"](Runtime(), action, normal_recording=case != "recovery"))
    assert (results[0].source is not None) is has_source
    if has_source:
        assert results[0].source.start_result_event_id == 10
        assert results[0].source.elapsed_before_stop_ns == (20_000_000_000 if case == "delayed" else 10_000_000_000)


@pytest.mark.parametrize("original, requested, matches", [
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, {"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, True),
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, {"start_result_event_id": 11, "elapsed_before_stop_ns": 10}, False),
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, {"start_result_event_id": 10, "elapsed_before_stop_ns": 11}, False),
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, None, False),
    (None, {"start_result_event_id": 10, "elapsed_before_stop_ns": 10}, False),
])
def test_result_key_compares_original_normal_source(original, requested, matches):
    path = SOURCE.parent / "operations.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FinishAttemptCommand")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_reuse")
    ns = {"TransactionError": ValueError, "_ATTEMPT_RESULT_EVENT": 12,
          "_RESULT_REASON": {"success": 1}, "json_equal": lambda a, b: a == b}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), "exec"), ns)
    command = SimpleNamespace(_normal_recording_stop=None if requested is None else SimpleNamespace(document=lambda: requested))
    saved = [{"type": 12, "reason": 1, "body": {"evidence": {} if original is None else {"normal_recording_stop": original}, "rows": []}}]
    with pytest.raises(ValueError) as error:
        ns["_reuse"](command, object(), saved)
    assert ("缺少尝试事实" in str(error.value)) is matches
    assert ("改变正常停止来源" in str(error.value)) is not matches


@pytest.mark.parametrize("value, valid", [
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 0}, True),
    ({"start_result_event_id": True, "elapsed_before_stop_ns": 0}, False),
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": -1}, False),
    ({"start_result_event_id": 10}, False),
    ({"start_result_event_id": 10, "elapsed_before_stop_ns": 0, "extra": 1}, False),
])
def test_event_decoder_validates_stop_source_shape(value, valid):
    models_path = SOURCE.parents[2] / "capture" / "models.py"
    validators_path = SOURCE.parents[2] / "history" / "validators.py"
    model_tree = ast.parse(models_path.read_text())
    model = next(node for node in model_tree.body if isinstance(node, ast.ClassDef) and node.name == "NormalRecordingStop")
    from dataclasses import dataclass
    model_ns = {"dataclass": dataclass, "is_json_integer": lambda x: type(x) is int, "MAX_OBJECT_ID": 2**63 - 1}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[model], type_ignores=[])), str(models_path), "exec"), model_ns)
    tree = ast.parse(validators_path.read_text())
    check = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_check_evidence_member")
    def fail(message): raise ValueError(message)
    ns = {"Mapping": dict, "_fail": fail}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[check], type_ignores=[])), str(validators_path), "exec"), ns)
    models = ModuleType("camctl.capture.models")
    models.NormalRecordingStop = model_ns["NormalRecordingStop"]
    with patch.dict(sys.modules, {models.__name__: models}):
        if valid:
            ns["_check_evidence_member"](30, "normal_recording_stop", value, 1)
        else:
            with pytest.raises(ValueError):
                ns["_check_evidence_member"](30, "normal_recording_stop", value, 1)
