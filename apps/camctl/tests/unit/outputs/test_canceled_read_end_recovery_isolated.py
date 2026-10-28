"""取消交付的原完整读取恢复；不导入实际包、资源或数据库。"""

import ast
import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[3] / "src/camctl/outputs/obtain_flow.py"


class StripImports(ast.NodeTransformer):
    def visit_Import(self, node): return None
    def visit_ImportFrom(self, node): return None


def load(name, namespace):
    tree = ast.parse(SOURCE.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              StripImports().visit(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace[name]


class CanceledRecoveryTests(unittest.TestCase):
    def test_verified_delivery_recovers_original_end_for_host_and_device(self):
        async def run(source, saved, slot):
            ticket = SimpleNamespace(target_id="9")
            end = object()
            calls = []
            class Discovery:
                async def rows(self, database, kind, now, query, **kwargs):
                    self.query = query
                    return SimpleNamespace(items=[(9, source, "camera", "driver")])
            class Database:
                async def read(self, callback): return callback(SimpleNamespace(connection=object()))
            discovery = Discovery()
            flow = SimpleNamespace(discovery=discovery, database=Database(),
                devices={"camera": SimpleNamespace(evidence="device")},
                recovery_evidence_for=None, pending_read_ends={})
            async def resume(current, held, original, extra):
                calls.append((held.ticket, held.end, extra))
            namespace = {"unfinished_read_end": lambda owned, copy_id: (ticket, (ticket, end, 100, slot)) if saved else (ticket, None),
                "HeldReadEnd": lambda ticket, end, occurred_at, **kwargs: SimpleNamespace(ticket=ticket, end=end, **kwargs),
                "_LOCAL_READ_EVIDENCE": "local", "_stored_read_attempt_config": lambda connection, copy_id: "config",
                "_resume_local_host_end": resume, "_resume_local_delivery_end": resume,
                "DeviceBinding": lambda *args: args, "ConsistencyError": ValueError}
            await load("_settle_ready_read_ends", namespace)(flow, 100)
            self.assertIn("c.verification_state IN (3,5)", discovery.query)
            return calls, ticket, end
        calls, ticket, end = asyncio.run(run(None, True, None))
        self.assertEqual(calls, [(ticket, end, "config")])
        calls, ticket, end = asyncio.run(run(15, True, None))
        self.assertEqual((calls[0][0], calls[0][1], calls[0][2].evidence), (ticket, end, "device"))
        self.assertEqual(asyncio.run(run(None, False, None))[0], [])
        self.assertEqual(asyncio.run(run(15, True, "camera"))[0], [])

    def test_publish_requires_original_succeeded_read_and_settled_attempts(self):
        source = SOURCE.parents[1] / "persistence/repositories/outputs.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "delivery_read_succeeded")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), StripImports().visit(node)], type_ignores=[])
        settled = [True]
        class Reads:
            def __init__(self, connection): pass
            def no_unsettled_attempts(self, run_id): return settled[0]
        namespace = {"closing": closing, "_RUN_KIND": SimpleNamespace(READ_FILE=3),
            "_RUN_STATUS": SimpleNamespace(SUCCEEDED=3), "_ATTEMPT_STATUS": SimpleNamespace(SUCCEEDED=2),
            "RetryWaitReads": Reads}
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        rows = [(7, 3, 1, 0, 2, "{}", 22)]
        class Cursor:
            def fetchall(self): return rows
            def close(self): pass
        connection = SimpleNamespace(execute=lambda sql, params: Cursor())
        check = namespace["delivery_read_succeeded"]
        self.assertTrue(check(connection, 5))
        original = rows[0]
        for index, value in ((1, 2), (2, 0), (3, 1), (4, 1), (5, None), (6, None)):
            changed = list(original)
            changed[index] = value
            rows[:] = [tuple(changed)]
            self.assertFalse(check(connection, 5))
        rows[:] = [original]
        settled[0] = False
        self.assertFalse(check(connection, 5))
        rows[:] = [original, original]
        self.assertFalse(check(connection, 5))

    def test_discovery_uses_only_saved_original_end_for_device_and_host(self):
        async def run(source, saved, assembly):
            ticket = SimpleNamespace(target_id="9")
            end = object()
            calls = []
            class Discovery:
                async def rows(self, database, kind, now, query, **kwargs):
                    self.query = query
                    return SimpleNamespace(items=[(9, source, "camera", "driver")])
            class Database:
                async def read(self, callback): return callback(object())
            discovery = Discovery()
            flow = SimpleNamespace(discovery=discovery, database=Database(), devices=assembly,
                recovery_evidence_for=None, pending_read_ends={})
            async def resume(current, held, original, evidence):
                calls.append((held.ticket, held.end, evidence))
            namespace = {"unfinished_read_end": lambda owned, copy_id: (ticket, (ticket, end, 100, None)) if saved else (ticket, None),
                "HeldReadEnd": lambda ticket, end, occurred_at, **kwargs: SimpleNamespace(ticket=ticket, end=end, **kwargs),
                "_LOCAL_READ_EVIDENCE": "local", "_resume_canceled_delivery_end": resume,
                "DeviceBinding": lambda *args: args, "ConsistencyError": ValueError}
            await load("_settle_canceled_read_ends", namespace)(flow, 100)
            self.assertIn("a.cancel_requested=1", discovery.query)
            self.assertIn("c.read_end_event_id IS NOT NULL", discovery.query)
            return calls, ticket, end
        calls, ticket, end = asyncio.run(run(None, True, {}))
        self.assertEqual(calls, [(ticket, end, "local")])
        calls, ticket, end = asyncio.run(run(15, True, {"camera": SimpleNamespace(evidence="device")}))
        self.assertEqual(calls, [(ticket, end, "device")])
        self.assertEqual(asyncio.run(run(None, False, {}))[0], [])
        self.assertEqual(asyncio.run(run(15, True, {}))[0], [])

    def test_file_owner_rechecks_end_and_cancel_before_saving(self):
        async def run(valid, canceled):
            trace = []
            ticket = SimpleNamespace(target_id="9")
            held = SimpleNamespace(ticket=ticket)
            runtime = SimpleNamespace(database=object())
            async def owned(flow, copy_id, callback):
                trace.append("owner")
                await callback(None)
            async def recheck(flow, copy_id, actual, **kwargs):
                trace.append(("recheck", kwargs["allow_held_slot"]))
                return valid
            async def canceled_result(flow, actual, evidence):
                self.assertIs(actual, ticket)
                trace.append("cancel")
                return "original-result" if canceled else None
            async def save(flow, pending): trace.append(("save", pending))
            namespace = {"replace": lambda value, **kwargs: value, "run_owned_read": owned,
                "current_local_read_end": recheck, "canceled_complete_read_result": canceled_result,
                "_save_read_result_and_settle": save}
            await load("_resume_canceled_delivery_end", namespace)(runtime, held, runtime, "evidence")
            return trace
        self.assertEqual(asyncio.run(run(True, True)), ["owner", ("recheck", True), "cancel", ("save", "original-result")])
        self.assertEqual(asyncio.run(run(True, False)), ["owner", ("recheck", True), "cancel"])
        self.assertEqual(asyncio.run(run(False, True)), ["owner", ("recheck", True)])


if __name__ == "__main__": unittest.main()
