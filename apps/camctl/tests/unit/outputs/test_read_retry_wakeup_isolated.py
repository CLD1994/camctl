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
            "plan_device_work": lambda *args, **kwargs: (work,),
        })
        await advance(runtime, 10)
        self.assertEqual(calls, [(7, 5, 3)])


if __name__ == "__main__":
    unittest.main()
