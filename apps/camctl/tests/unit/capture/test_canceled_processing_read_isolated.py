"""取消录像内部读取的原事实、机会和文件拥有者；全部使用内存替身。"""

import ast
import asyncio
from contextlib import closing
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[3] / "src/camctl"
OUTPUTS = ROOT / "persistence/repositories/outputs.py"
CANCELED = ROOT / "capture/canceled_internal_reads.py"
LIFECYCLE = ROOT / "bootstrap/lifecycle.py"
HANDLERS = ROOT / "capture/handlers.py"


class StripImports(ast.NodeTransformer):
    def visit_Import(self, node): return None
    def visit_ImportFrom(self, node): return None


def load(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), StripImports().visit(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class ProcessingCancelTests(unittest.TestCase):
    def test_cancel_handler_waits_for_original_processing_before_terminal(self):
        async def run(ready):
            calls = []
            async def action(action_id):
                return {"id": action_id, "type": 2, "execution_started": 1, "execution_spec_json": {}}
            async def settle(scope, action_id):
                calls.append("read_settlement")
                return ready
            async def save(request): calls.append("cancel_finish")
            runtime = SimpleNamespace(pending_baselines={}, action=action, save_capture_completion=save, wall_us=lambda: 20)
            policy = SimpleNamespace(ACTION6_VIDEO_SEGMENTS_V1=SimpleNamespace(value="action6"),
                OSMO360II_RECORD_SEGMENTS_V1=SimpleNamespace(value="osmo"),
                OSMO_EMPTY_TAIL_CLASSIFICATION_V1=SimpleNamespace(value="empty"))
            ns = {"CaptureResultPolicy": policy, "_ACTION_TYPE": SimpleNamespace(CAMERA_RECORD=2),
                "_read_result_scope": lambda value: value, "settle_canceled_processing_read": settle,
                "FinishCanceledCapture": lambda **kwargs: kwargs}
            await load(HANDLERS, "_finish_canceled_capture", ns)(runtime, 4)
            return calls
        self.assertEqual(asyncio.run(run(False)), ["read_settlement"])
        self.assertEqual(asyncio.run(run(True)), ["read_settlement", "cancel_finish"])

    def test_release_transaction_waits_for_other_original_call(self):
        current = [False]
        ns = {"_RUN_STATUS": SimpleNamespace(PENDING=1), "_RUN_KIND": SimpleNamespace(READ_FILE=3),
            "_RUN_TERMINAL": {3, 4, 5}, "_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1),
            "SlotOutcome": SimpleNamespace(WAIT="wait", RELEASED="released", ALREADY_RELEASED="already"),
            "ConsistencyError": ValueError, "TransactionError": ValueError,
            "_canceled_binding_processing_settled": lambda *args: current[0]}
        cls = load(OUTPUTS, "_SlotChangeCommand", ns)
        command = object.__new__(cls)
        command._request = SimpleNamespace(capture_selection=None)
        command._state = {"operation_attempts": {}}
        command._load_attempts = lambda *args: None
        command._load_owner_action = lambda *args: {"id": 4, "status": 2, "cancel_requested": 1}
        command._decision = lambda outcome, reason=None: (outcome, reason)
        command._save = lambda *args: ("released", None)
        copy = {"id": 9, "slot_device_id": "camera", "processing_id": 3, "committed_bytes": 10, "source_size": 10}
        run = {"id": 7, "status": 4, "retry_wait_required": 0}
        scope = SimpleNamespace(connection=object())
        self.assertEqual(command._release(scope, copy, "camera", run), ("wait", "action_active"))
        current[0] = True
        self.assertEqual(command._release(scope, copy, "camera", run), ("released", None))

    def test_release_guard_independently_requires_all_original_calls_settled(self):
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        class Reads:
            connection = object()
            def no_unsettled_attempts(self, run_id, validated): return True
        module.RetryWaitReads = Reads
        ready = [True]
        copy = {"id": 9, "processing_id": 3, "source_device_file_id": 15,
                "committed_bytes": 10, "source_size": 10}
        source = {"observer_action_id": 5, "source_action_id": 6}
        owner = {"device_id": "camera", "driver_id": "driver"}
        run = {"id": 7, "status": 4, "retry_wait_required": 0}
        facts = {("file_copies", 9): copy, ("device_files", 15): source,
                 ("actions", 5): owner, ("actions", 6): owner}
        ns = {"__name__": "isolated.outputs", "__package__": "camctl.persistence.repositories",
            "_COPY_CHANGED_EVENT": 22, "_SLOT_REASON": 6, "EventValidationError": ValueError,
            "_guard_facts": lambda context, table, identity: facts[(table, identity)],
            "_slot_run": lambda context, copy_id: run, "_slot_owner_action": lambda context, copy: {"id": 4},
            "RetryWaitReads": Reads,
            "_canceled_binding_processing_settled": lambda *args: ready[0], "_RUN_TERMINAL": {3, 4, 5}}
        guard = load(OUTPUTS, "_read_slot_guard", ns)
        row = SimpleNamespace(table="file_copies", row_id=9,
            before=SimpleNamespace(exists=True, values={"slot_device_id": "camera"}),
            after=SimpleNamespace(values={"slot_device_id": None}))
        event = SimpleNamespace(event_type=22, reason=6, rows=(row,))
        context = SimpleNamespace(retry_wait_reads=Reads(), validated_events=())
        with patch.dict(sys.modules, {module.__name__: module}):
            guard(event, context)
            ready[0] = False
            with self.assertRaises(ValueError): guard(event, context)

    def test_terminal_recovery_pages_without_device_assembly(self):
        async def run():
            calls, notifications = [], []
            class Database:
                async def read(self, callback):
                    connection = SimpleNamespace(execute=lambda sql, params: SimpleNamespace(fetchall=lambda: [(i,) for i in range(1, 129)]))
                    return callback(SimpleNamespace(connection=connection))
            async def settle(scope, action_id): calls.append((scope, action_id))
            shared = {}
            deps = SimpleNamespace(capture_canceled_read_after=0, capture_read_results={}, capture_read_business={},
                capture_read_ends={}, capture_continuing_reads={}, capture_segment_inspection_results=shared,
                work_files=SimpleNamespace(executor=object()))
            context = SimpleNamespace(database=Database(), clock=SimpleNamespace(utc_micros=lambda: 20),
                wake=SimpleNamespace(mark_changed=lambda reason: notifications.append(reason)))
            ns = {"settle_canceled_processing_read": settle, "OperationRepository": lambda: object(),
                "internal_read_scope": lambda database, operations, **kwargs: SimpleNamespace(**kwargs),
                "WakeReason": SimpleNamespace(ACTIVITY_FINISHED="wake")}
            await load(LIFECYCLE, "_resume_canceled_internal_reads", ns)(deps, context)
            self.assertEqual(deps.capture_canceled_read_after, 128)
            self.assertEqual(notifications, ["wake"])
            self.assertEqual(len(calls), 128)
            self.assertIs(calls[0][0].pending_segment_inspection_results, shared)
        # The extracted function imports only fake collaborators.
        asyncio.run(run())

    def test_release_requires_original_failed_read_and_all_calls_settled(self):
        binding = ModuleType("camctl.persistence.repositories.binding_read_cancel")
        calls = []
        binding.load_saved_binding_read_failure = lambda connection, action, copy, error: calls.append((action, copy, error))
        ns = {"__name__": "isolated.outputs", "__package__": "camctl.persistence.repositories",
              "closing": closing, "Mapping": dict, "_RUN_STATUS": SimpleNamespace(FAILED=4),
              "load_saved_binding_read_failure": binding.load_saved_binding_read_failure}
        check = load(OUTPUTS, "_canceled_binding_processing_settled", ns)
        rows = []
        connection = SimpleNamespace(execute=lambda sql, args: Cursor(rows))
        copy = {"id": 9, "processing_id": 3}
        run = {"id": 7, "status": 4, "error_json": {"code": "device_binding_unavailable"}}
        action = {"id": 4, "status": 2, "cancel_requested": 1}
        with patch.dict(sys.modules, {binding.__name__: binding}):
            self.assertTrue(check(connection, copy, run, action))
            self.assertEqual(calls[-1][:2], (4, 9))
            rows.append((1,))
            self.assertFalse(check(connection, copy, run, action))
            action["cancel_requested"] = 0
            self.assertIsNone(check(connection, copy, run, action))

    def test_file_ownership_and_original_key_precede_release(self):
        async def run(*, ready=True, busy=False, pending_result=False, pending_business=False, changed=False):
            trace = []
            current = (9, 20, "camera", ready)
            class Database:
                async def read(self, callback): return callback(object())
            executor = SimpleNamespace(unfinished_files=lambda: (20,) if busy else ())
            result = SimpleNamespace(ticket=SimpleNamespace(target_id="9"))
            flow = SimpleNamespace(database=Database(), file_executor=executor, occurred_at=lambda: 25,
                pending_read_business={(4, "capture_binding_failure"): object()} if pending_business else {},
                pending_read_results={(7, 1): result} if pending_result else {})
            async def owned(runtime, copy_id, callback):
                trace.append("owner")
                if changed: candidates[0] = (9, 20, "camera", False)
                return await callback(None)
            async def save(runtime, copy_id, stage, request, method, **kwargs):
                trace.append(("release", copy_id, request.occurred_at))
                return SimpleNamespace(value=SimpleNamespace(outcome="released"))
            candidates = [current]
            ns = {"_candidate": lambda owned, action: candidates[0], "run_owned_read": owned,
                "save_read_business": save, "SlotRequest": lambda copy, now: SimpleNamespace(copy_id=copy, occurred_at=now),
                "OutputsRepository": lambda: SimpleNamespace(release_read_slot=object()),
                "SlotOutcome": SimpleNamespace(RELEASED="released", ALREADY_RELEASED="already")}
            result = await load(CANCELED, "settle_canceled_processing_read", ns)(flow, 4)
            return result, trace
        self.assertEqual(asyncio.run(run()), (True, ["owner", ("release", 9, 25)]))
        for values in ({"ready": False}, {"busy": True}, {"pending_result": True}, {"pending_business": True}):
            with self.subTest(values=values): self.assertEqual(asyncio.run(run(**values)), (False, []))
        self.assertEqual(asyncio.run(run(changed=True)), (False, ["owner"]))

    def test_slot_unknown_retries_existing_key_even_if_already_released(self):
        async def run():
            prior = SimpleNamespace(key="original-release-key", request=object())
            pending = {(9, "release_read_slot"): prior}
            flow = SimpleNamespace(database=SimpleNamespace(read=lambda callback: call(callback)),
                file_executor=SimpleNamespace(unfinished_files=lambda: ()), occurred_at=lambda: 25,
                pending_read_business=pending, pending_read_results={})
            calls = []
            async def owned(runtime, copy_id, callback): return await callback(None)
            async def save(runtime, copy_id, stage, request, method, **kwargs):
                actual = runtime.pending_read_business[(copy_id, stage)]
                calls.append((actual.key, actual.request, kwargs["retry"]))
                del runtime.pending_read_business[(copy_id, stage)]
                return SimpleNamespace(value=SimpleNamespace(outcome="already"))
            ns = {"_candidate": lambda owned, action: (9, 20, None, True), "run_owned_read": owned,
                "save_read_business": save, "SlotRequest": lambda copy, now: object(),
                "OutputsRepository": lambda: SimpleNamespace(release_read_slot=object()),
                "SlotOutcome": SimpleNamespace(RELEASED="released", ALREADY_RELEASED="already")}
            self.assertTrue(await load(CANCELED, "settle_canceled_processing_read", ns)(flow, 4))
            self.assertEqual(calls, [("original-release-key", prior.request, True)])
        async def call(callback): return callback(object())
        asyncio.run(run())


if __name__ == "__main__": unittest.main()
