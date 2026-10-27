"""只装载被测源码，隔离 camctl 包资源与实际协作者。"""

import asyncio
import ast
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SOURCE = Path(__file__).parents[3] / "src" / "camctl"


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


def load_function(path, name, collaborators):
    source = ast.parse(path.read_text())
    function = next(node for node in source.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                function], type_ignores=[])
    namespace = dict(collaborators)
    namespace.setdefault("_ATTEMPT_STATUS", SimpleNamespace(RUNNING=1, SUCCEEDED=2))
    if name == "_advance_reads":
        namespace["_complete_read_wait"] = load_function(path, "_complete_read_wait", {})
    exec(compile(ast.fix_missing_locations(isolated), str(path), "exec"), namespace)
    return namespace[name]


class RetryWakeupTests(unittest.TestCase):
    def test_earliest_deadline_and_expiration(self):
        module = load_source("isolated_retry_wakeup", SOURCE / "outputs" / "retry_wakeup.py")
        wakeup = module.ReadRetryWakeup()
        self.assertTrue(wakeup.schedule(4_000_000_000, 1_000_000_000, 10))
        self.assertFalse(wakeup.schedule(6_000_000_000, 1_000_000_000, 10))
        self.assertTrue(wakeup.schedule(3_000_000_000, 1_000_000_000, 10))
        self.assertEqual(wakeup.next_at(11, 0), 12)
        wakeup.consume_due(11)
        self.assertEqual(wakeup.next_at(11, 0), 12)
        wakeup.consume_due(12)
        self.assertIsNone(wakeup.next_at(12, 0))

    def test_discovery_returns_attempt_count_in_candidate_page(self):
        source = SOURCE / "outputs" / "read_discovery.py"
        class Cursor:
            def fetchall(self): return [(7, 2, 10, 3, 0, "camera", "driver", 8, 0, 1)]
            def close(self): pass
        class Connection:
            query = None
            def execute(self, query, parameters):
                self.query = query
                return Cursor()
        connection = Connection()
        scope = SimpleNamespace(database="db", kind="reads", device_id=None)
        discover = load_function(source, "read_candidates", {
            "positive_limit": lambda limit: None, "check_cursor": lambda scope, cursor: None,
            "database_identity": lambda connection: "db", "validate_read_basis": lambda *args: None,
            "_READ_COPY_ELIGIBLE": "eligible", "_READ_FROM": " FROM file_copies fc",
            "closing": __import__("contextlib").closing,
            "ReadCandidate": lambda *row: SimpleNamespace(attempts_used=row[-1], key=(0, 0, 0, 0, 0)),
            "Page": lambda items, position: SimpleNamespace(items=items),
        })
        page = discover(connection, scope, limit=100)
        self.assertIn("r.attempts_used", connection.query)
        self.assertEqual(page.items[0].attempts_used, 1)


class BackgroundDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_flight_flow_wakes_after_completion(self):
        diagnostics = ModuleType("camctl.operations.error_diagnostics")
        diagnostics.exception_diagnostic = lambda error: {}
        runtime = ModuleType("camctl.logging_runtime.diagnostics")

        @asynccontextmanager
        async def diagnostic_scope():
            yield

        runtime.diagnostic_scope = diagnostic_scope
        notifications = ModuleType("camctl.scheduling.notifications")
        notifications.WakeReason = SimpleNamespace(READ_OPPORTUNITY="read")
        stubs = {diagnostics.__name__: diagnostics, runtime.__name__: runtime,
                 notifications.__name__: notifications}
        with patch.dict(sys.modules, stubs):
            module = load_source("isolated_background_flow", SOURCE / "bootstrap" / "background_flow.py")
            release = asyncio.Event()
            changes = []

            async def flow(context):
                await release.wait()
                return module.FlowWakeup.IDLE

            flow.next_wakeup_at = lambda now, margin: 1.0
            owner = module.BackgroundFlow(flow)
            context = SimpleNamespace(wake=SimpleNamespace(mark_changed=changes.append))
            await owner(context)
            self.assertIsNone(owner.next_wakeup_at(2.0, 0))
            release.set()
            await owner.unconsumed_task
            await asyncio.sleep(0)
            self.assertEqual(changes, ["read"])
            owner.check_completed()
            self.assertEqual(owner.next_wakeup_at(2.0, 0), 1.0)


class BlockedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_read_candidates_do_not_issue_individual_retry_queries(self):
        candidates = tuple(SimpleNamespace(held=False, copy_id=i, device_id="camera", attempts_used=0)
                           for i in range(1, 101))
        assembly = SimpleNamespace(capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=candidates)
        async def read_one(*args): self.fail("首次读取不能逐项查询重试")
        class Database:
            count = 0
            async def read(self, operation):
                self.count += 1
                return operation(SimpleNamespace(connection=None))
        database = Database()
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=database,
            pending_read_results={}, devices={"camera": assembly}, binding_check=None)
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "plan_device_work": lambda *args, **kwargs: (),
        })
        await advance(runtime, 10)
        self.assertEqual(database.count, 1)

    async def test_selected_ready_read_keeps_wait_for_intent_transaction(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args): return None
        async def advance_read(*args):
            actions.append("intent")
            return True
        async def save(*args, **kwargs): actions.append("complete")
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return True
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20)
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(7,), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_wait_remaining": remaining, "_advance_read": advance_read,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts, retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, ["intent"])

    async def test_selected_read_that_loses_opportunity_completes_wait(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args): return None
        async def advance_read(*args): actions.append("blocked"); return False
        async def save(runtime, copy_id, operation, request, callback, *, on_completed):
            actions.append(operation)
            on_completed(True)
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return True
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20,
            retry_gate=SimpleNamespace(cleared=lambda key: None), operations=SimpleNamespace(complete_read_retry_wait=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(7,), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_wait_remaining": remaining, "_advance_read": advance_read,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts, retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, ["blocked", "complete_retry_wait"])

    async def test_slotless_fallback_starts_wait_before_capture_selection(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args):
            actions.append("clock")
            return Decimal("5")
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return True
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20)
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(8,),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        def plan(*args, **kwargs):
            actions.append("plan")
            return (work,)
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_wait_remaining": remaining, "plan_device_work": plan,
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = None
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts, retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, ["clock", "plan"])

    async def test_slotless_unproven_candidate_does_not_start_wait(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args): actions.append("clock")
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return False
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20)
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(8,),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_wait_remaining": remaining,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, [])

    async def test_restarted_fallback_wait_starts_a_new_monotonic_interval(self):
        source_path = SOURCE / "operations" / "attempts.py"
        tree = ast.parse(source_path.read_text())
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name in ("retry_wait_remaining_s", "RetryWaitGate")]
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    *selected], type_ignores=[])
        namespace = {"dataclass": dataclass, "field": field, "Decimal": Decimal, "__name__": __name__}
        exec(compile(ast.fix_missing_locations(isolated), str(source_path), "exec"), namespace)
        gate = namespace["RetryWaitGate"]()
        args = {"attempts_used": 1, "retry_wait_required": True, "max_attempts_used": 3, "interval_s": Decimal("5")}
        self.assertEqual(gate.remaining("read/7", now_ns=100_000_000_000, **args), Decimal("5"))
        self.assertIsNone(gate.remaining("read/7", now_ns=105_000_000_000, **args))
        restarted = namespace["RetryWaitGate"]()
        self.assertEqual(restarted.remaining("read/7", now_ns=1_000_000_000, **args), Decimal("5"))
        self.assertIsNone(restarted.remaining("read/7", now_ns=1_000_000_000, **{**args, "retry_wait_required": False}))

    async def test_slotless_fallback_completes_wait_while_capture_blocks(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args): return None
        async def save(runtime, copy_id, operation, request, callback, *, on_completed):
            actions.append((operation, request))
            on_completed(True)
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return True
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20,
            retry_gate=SimpleNamespace(cleared=lambda key: actions.append(("clear", key))),
            operations=SimpleNamespace(complete_read_retry_wait=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(8,),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_wait_remaining": remaining,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts, retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, [("complete_retry_wait", (11, 12, 20)), ("clear", "read/7")])

    async def test_slotless_fallback_exhausts_after_wait_was_cleared(self):
        actions = []
        candidate = SimpleNamespace(held=False, copy_id=7, device_id="camera", attempts_used=1)
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=1, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 0, 3, "{}", 101)
        async def save(runtime, copy_id, operation, request, callback):
            actions.append((operation, request[1]))
            return SimpleNamespace(value=SimpleNamespace(reason="budget_exhausted"))
        async def fail_delivery(runtime, facts, now): actions.append(("delivery", facts["id"]))
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        class Reads:
            def __init__(self, connection): pass
            def read_eligible(self, run_id, now): return True
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20,
            retry_gate=SimpleNamespace(cleared=lambda key: actions.append(("clear", key))),
            operations=SimpleNamespace(exhaust_blocked_read=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(), dispatch_captures=(8,),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "_read_facts": lambda connection, copy_id: {"id": copy_id},
            "_read_intent": lambda assembly, facts, now: object(), "_fail_read_delivery": fail_delivery,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        retry_reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retry_reads.RetryWaitReads = Reads
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts, retry_reads.__name__: retry_reads}):
            await advance(runtime, 10)
        self.assertEqual(actions, [("exhaust_read", 12), ("clear", "read/7"), ("delivery", 7)])

    async def test_running_original_read_is_not_exhausted(self):
        candidate = SimpleNamespace(held=True, copy_id=7, device_id="camera")
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=2, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 2, 0, 1, None, None)
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
                                  pending_read_results={}, devices={"camera": assembly}, binding_check=None)
        work = SimpleNamespace(device_id="camera", yield_reads=(7,), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one, "plan_device_work": lambda *args, **kwargs: (work,),
        })
        await advance(runtime, 10)

    async def test_elapsed_blocked_read_saves_wait_completion(self):
        actions = []
        candidate = SimpleNamespace(held=True, copy_id=7, device_id="camera")
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 1, 1, 3, "{}", 101)
        async def remaining(*args): return None
        async def save(runtime, copy_id, operation, request, callback, *, on_completed):
            actions.append((operation, request))
            on_completed(True)
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20,
            retry_gate=SimpleNamespace(cleared=lambda key: actions.append(("clear", key))),
            operations=SimpleNamespace(complete_read_retry_wait=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(7,), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one,
            "_read_wait_remaining": remaining, "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts}):
            await advance(runtime, 10)
        self.assertEqual(actions, [("complete_retry_wait", (11, 12, 20)), ("clear", "read/7")])

    async def test_exhausted_blocked_read_finishes_without_new_attempt(self):
        actions = []
        candidate = SimpleNamespace(held=True, copy_id=7, device_id="camera")
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=2, capture_read_parallel=False)

        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 2, 1, 3, "{}", 101)
        async def fail_delivery(runtime, facts, now): actions.append(("delivery", facts["id"]))
        async def save(runtime, copy_id, operation, request, callback, **kwargs):
            actions.append((operation, request[1]))
            return SimpleNamespace(value=SimpleNamespace(reason="budget_exhausted"))
        async def dispatch(*args): self.fail("读取预算耗尽时不能派发")

        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))

        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None,
            occurred_at=lambda: 20, retry_gate=SimpleNamespace(cleared=lambda key: actions.append(("clear", key))),
            operations=SimpleNamespace(exhaust_blocked_read=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(7,), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one,
            "_read_facts": lambda connection, copy_id: {"id": copy_id},
            "_read_intent": lambda assembly, facts, now: object(), "_fail_read_delivery": fail_delivery,
            "_advance_read": dispatch, "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts}):
            await advance(runtime, 10)
        self.assertEqual(actions, [("exhaust_read", 12), ("clear", "read/7"), ("delivery", 7)])

    async def test_failed_qualification_does_not_finish_delivery(self):
        actions = []
        candidate = SimpleNamespace(held=True, copy_id=7, device_id="camera")
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=2, capture_read_parallel=False)
        async def reads(database, kind, now): return SimpleNamespace(items=(candidate,))
        async def read_one(database, query, parameters): return (11, 12, 2, 1, 3, "{}", 101)
        async def save(*args, **kwargs): return SimpleNamespace(value=SimpleNamespace(reason="qualification_changed"))
        async def fail_delivery(*args): actions.append("failed")
        class Database:
            async def read(self, operation): return operation(SimpleNamespace(connection=None))
        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
            pending_read_results={}, devices={"camera": assembly}, binding_check=None, occurred_at=lambda: 20,
            retry_gate=SimpleNamespace(cleared=lambda key: actions.append("cleared")),
            operations=SimpleNamespace(exhaust_blocked_read=None))
        work = SimpleNamespace(device_id="camera", yield_reads=(7,), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "read_one": read_one,
            "_read_facts": lambda connection, copy_id: {"id": copy_id},
            "_read_intent": lambda assembly, facts, now: object(), "_fail_read_delivery": fail_delivery,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = save
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts}):
            await advance(runtime, 10)
        self.assertEqual(actions, [])

    async def test_elapsed_interval_does_not_schedule_an_immediate_retry(self):
        scheduled = []

        async def read_one(database, query, parameters):
            return (1, 1, 3)

        source_path = SOURCE / "operations" / "attempts.py"
        tree = ast.parse(source_path.read_text())
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name in ("retry_wait_remaining_s", "RetryWaitGate")]
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    *selected], type_ignores=[])
        namespace = {"dataclass": dataclass, "field": field, "Decimal": Decimal, "__name__": __name__}
        exec(compile(ast.fix_missing_locations(isolated), str(source_path), "exec"), namespace)
        gate = namespace["RetryWaitGate"]()
        gate.established("read/7", 1_000_000_000)
        runtime = SimpleNamespace(database=None, retry_gate=gate, monotonic_ns=lambda: 6_000_000_000,
                                  on_read_retry_deadline=lambda target, now: scheduled.append((target, now)))
        remaining = load_function(SOURCE / "outputs" / "obtain_flow.py", "_read_wait_remaining", {
            "read_one": read_one,
        })
        self.assertIsNone(await remaining(runtime, 7, Decimal("5"), 3))
        self.assertEqual(scheduled, [])

    async def test_pending_interval_schedules_exact_deadline(self):
        scheduled = []

        async def read_one(database, query, parameters):
            return (1, 1, 3)

        gate = SimpleNamespace(remaining=lambda *args, **kwargs: Decimal("0.125"))
        runtime = SimpleNamespace(database=None, retry_gate=gate, monotonic_ns=lambda: 1_000_000_000,
                                  on_read_retry_deadline=lambda target, now: scheduled.append((target, now)))
        remaining = load_function(SOURCE / "outputs" / "obtain_flow.py", "_read_wait_remaining", {
            "read_one": read_one,
        })
        self.assertEqual(await remaining(runtime, 7, Decimal("5"), 3), Decimal("0.125"))
        self.assertEqual(scheduled, [(1_125_000_000, 1_000_000_000)])

    async def test_capture_block_observes_retry_without_dispatch(self):
        calls = []
        candidate = SimpleNamespace(held=True, copy_id=7, device_id="camera")
        assembly = SimpleNamespace(retry_interval_s=5, max_read_attempts=3,
                                   capture_read_parallel=False)

        async def reads(database, kind, now):
            return SimpleNamespace(items=(candidate,))

        async def remaining(runtime, copy_id, interval, maximum):
            calls.append((copy_id, interval, maximum))
            return 1

        async def read_one(database, query, parameters):
            return (11, 12, 1, 1, 3, "{}", 101)

        async def dispatch(*args):
            self.fail("拍摄阻挡期间不能派发读取")

        work = SimpleNamespace(device_id="camera", yield_reads=(7,), dispatch_captures=(),
                               capture_read_parallel=False, grant_reads=(), resume_reads=())
        class Database:
            async def read(self, operation):
                return operation(SimpleNamespace(connection=None))

        runtime = SimpleNamespace(discovery=SimpleNamespace(reads=reads), database=Database(),
                                  pending_read_results={}, devices={"camera": assembly},
                                  binding_check=None)
        advance = load_function(SOURCE / "outputs" / "obtain_flow.py", "_advance_reads", {
            "_read_wait_remaining": remaining, "_advance_read": dispatch,
            "read_one": read_one,
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        read_attempts = ModuleType("camctl.outputs.read_attempts")
        read_attempts.save_read_business = None
        with patch.dict(sys.modules, {read_attempts.__name__: read_attempts}):
            await advance(runtime, 10)
        self.assertEqual(calls, [(7, 5, 3)])


if __name__ == "__main__":
    unittest.main()
