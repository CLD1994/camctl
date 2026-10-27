"""只装载被测源码，隔离 camctl 包资源与实际协作者。"""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src" / "camctl"


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


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


if __name__ == "__main__":
    unittest.main()
