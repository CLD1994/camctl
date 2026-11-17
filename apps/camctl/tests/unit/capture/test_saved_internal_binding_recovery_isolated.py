"""内部读取绑定失败冷恢复，不导入实际包或数据库。"""

import ast
import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
CAPTURE = SOURCE.parent.parent / "persistence/repositories/capture.py"
MEDIA = SOURCE.parent / "media_flow.py"


class StripImports(ast.NodeTransformer):
    def visit_Import(self, node): return None
    def visit_ImportFrom(self, node): return None


def load(name, namespace, source=SOURCE):
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              StripImports().visit(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[name]


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class RecoveryTests(unittest.TestCase):
    def test_legacy_pending_business_retires_then_settles_original_inspection(self):
        from unittest.mock import AsyncMock, create_autospec, patch
        from camctl.capture import media_flow, segment_inspections
        from camctl.capture.input_copy import InputPhase
        from camctl.outputs import read_attempts
        from camctl.persistence.business import BusinessDatabase
        from camctl.persistence.models import DbOutcome, DbOutcomeKind
        from camctl.persistence.repositories.capture import CaptureResult, FinishDisposition

        async def run():
            calls = []
            ticket = SimpleNamespace(target_id="9", run_id=5, attempt_id=6)
            request = SimpleNamespace(action_id=4)
            owned = SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(
                [(8,)] if "SELECT inspection_id" in sql else [(9,)])))
            database = create_autospec(BusinessDatabase, instance=True)
            database.read.side_effect = lambda callback, **kwargs: callback(owned)
            database.write.side_effect = lambda callback, **kwargs: callback(owned)

            def save_business(actual, key, connection):
                self.assertIs(actual, request)
                self.assertEqual(key, "original-business-key")
                self.assertIs(connection, owned)
                self.assertNotIn((5, 6), flow.pending_read_results)
                self.assertIs(flow.pending_read_business[(4, "capture_binding_failure")], business)
                calls.append(("original_business", key))
                return DbOutcome(DbOutcomeKind.COMPLETED,
                    value=CaptureResult(2, 2, (), FinishDisposition.RETIRED))

            business = read_attempts.PendingReadBusiness(save_business, request, "original-business-key")
            pending = read_attempts.PendingReadResult(
                SimpleNamespace(ticket=ticket, occurred_at=17), "original-read-key", business)
            flow = SimpleNamespace(database=database, occurred_at=lambda: 20,
                pending_read_results={(5, 6): pending}, pending_read_business={},
                continuing_read_tickets={}, pending_media_results={}, file_executor=None)

            async def save_result(actual_flow, value, *, retry=False):
                self.assertIs(actual_flow, flow)
                self.assertIs(value, pending)
                self.assertEqual(value.key, "original-read-key")
                self.assertTrue(retry)
                calls.append(("read_result", value.key))
                return value

            async def settle(actual_flow, copy_id, occurred_at):
                self.assertIs(actual_flow, flow)
                calls.append(("inspection", copy_id, occurred_at))
                return True

            async def release(actual_flow, copy_id, occurred_at):
                self.assertIs(actual_flow, flow)
                calls.append(("release", copy_id, occurred_at))
                return True

            with (patch.object(media_flow, "save_read_result", save_result),
                  patch.object(media_flow, "resume_media_saves", AsyncMock(return_value=False)),
                  patch.object(media_flow, "_fail_internal_read_input", AsyncMock()),
                  patch.object(read_attempts, "retry_read_business", AsyncMock()),
                  patch.object(read_attempts, "retry_read_ends", AsyncMock()),
                  patch.object(segment_inspections, "settle_inspection_read_failure", settle),
                  patch.object(segment_inspections, "settle_terminal_inspection_read_slot", release)):
                result = await media_flow.run_recording_media(flow, 4, 1, 30)
            self.assertEqual((result.phase, result.copy_id, result.reason),
                             (InputPhase.WAITING, 9, "run_ended"))
            self.assertFalse(flow.pending_read_results)
            self.assertFalse(flow.pending_read_business)
            return calls

        self.assertEqual(asyncio.run(run()), [
            ("read_result", "original-read-key"),
            ("original_business", "original-business-key"),
            ("inspection", 9, 17), ("release", 9, 20)])

    def test_original_binding_business_key_is_reused_or_retired_for_inspection(self):
        calls = []
        rows = {("actions", 4): {"id": 4, "plan_id": 2, "status": 2,
                    "device_id": "camera", "driver_id": "driver"},
                ("file_copies", 9): {"id": 9, "inspection_id": 8},
                ("segment_inspections", 8): {"id": 8, "action_id": 4},
                ("plans", 2): {"id": 2, "status": 2}}
        saved = [None]
        class Plan:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        ns = {"saved_transaction_events": lambda connection, key: saved[0],
            "saved_binding_read_failure": lambda connection, key, action, copy, error: calls.append((key, action, copy)),
            "row_facts": lambda connection, table, identity: rows.get((table, identity)),
            "CommandPlan": Plan, "CaptureResult": lambda *args: SimpleNamespace(disposition=args[-1]),
            "FinishDisposition": SimpleNamespace(RETIRED="retired"), "ConsistencyError": ValueError,
            "TransactionError": ValueError}
        cls = load("_FinishBindingFailureCommand", ns, CAPTURE)
        request = SimpleNamespace(read_result_key="read-key", read_copy_id=9, action_id=4,
            failure=SimpleNamespace(details={"device_id": "camera", "expected_driver_id": "driver"}))
        command = cls(request, "business-key")
        scope = SimpleNamespace(connection=object())
        result = command.plan(scope)
        self.assertTrue(result.read_only)
        self.assertEqual(result.result.disposition, "retired")
        self.assertEqual(calls, [("read-key", 4, 9)])
        saved[0] = ["committed"]
        command._reuse = lambda scope, events: ("reused", events)
        self.assertEqual(command.plan(scope), ("reused", ["committed"]))
        rows[("segment_inspections", 8)]["action_id"] = 99
        saved[0] = None
        with self.assertRaises(ValueError): command.plan(scope)

    def test_find_requires_unique_original_failure_and_uses_stored_identity(self):
        async def run(rows):
            connection = SimpleNamespace(execute=lambda sql, args: Cursor(rows))
            class Database:
                async def read(self, callback, **kwargs): return callback(SimpleNamespace(connection=connection))
            error = {"details": {"reason": "physical_identity_mismatch"}}
            namespace = {"closing": closing, "parse_exact_json": lambda value: error,
                "load_saved_binding_read_failure": lambda connection, action, copy, value:
                    ("original-key", {"serial": "original"}, 17), "ConsistencyError": ValueError}
            return await load("_saved_internal_binding_failure", namespace)(SimpleNamespace(database=Database()), {"id": 4})
        self.assertIsNone(asyncio.run(run([])))
        self.assertEqual(asyncio.run(run([(9, "error", 2, None)])),
            (9, {"reason": "physical_identity_mismatch"}, "original-key", {"serial": "original"}, 17, None))
        with self.assertRaises(ValueError): asyncio.run(run([(9, "error", 2, None), (10, "error", 3, None)]))

    def test_saved_failure_precedes_configuration_and_preserves_original_key(self):
        async def run(disposition):
            calls = []
            class Database:
                async def read(self, callback, **kwargs):
                    connection = SimpleNamespace(execute=lambda sql, args: Cursor([]))
                    return callback(SimpleNamespace(connection=connection))
            async def saved(runtime, action):
                return (9, {"reason": "missing", "device_id": "camera", "expected_driver_id": "driver"}, "original-key", None, 17, None)
            async def request(runtime, action, details):
                calls.append(("request", details))
                return SimpleNamespace()
            async def save(scope, action_id, stage, value, method):
                calls.append(("save", value.read_result_key, value.read_copy_id))
                return SimpleNamespace(kind="completed", value=SimpleNamespace(disposition=disposition))
            def replaced(value, **kwargs):
                value.__dict__.update(kwargs)
                return value
            namespace = {"_saved_internal_binding_failure": saved, "_binding": lambda action: object(),
                "BindingStatus": SimpleNamespace(DEVICE_MISSING=1, DRIVER_MISMATCH=2,
                    PHYSICAL_IDENTITY_MISMATCH=3, PHYSICAL_IDENTITY_CONFLICT=4),
                "BindingResult": lambda *args: args, "closing": closing,
                "_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1), "_binding_failure_request": request,
                "_read_result_scope": lambda runtime: runtime, "save_read_business": save,
                "replace": replaced, "DbOutcomeKind": SimpleNamespace(COMPLETED="completed"),
                "FinishDisposition": SimpleNamespace(RETIRED="retired"), "ConsistencyError": ValueError}
            runtime = SimpleNamespace(database=Database(), capture=SimpleNamespace(finish_binding_failure=object()))
            action = {"id": 4, "cancel_requested": 0}
            result = await load("_handle_binding_failure", namespace)(runtime, action)
            return result, calls
        for disposition, expected in (("saved", True), ("retired", False)):
            result, calls = asyncio.run(run(disposition))
            self.assertEqual(result, expected)
            self.assertEqual(calls[-1], ("save", "original-key", 9))

    def test_inspection_failure_settles_before_original_segment_decision(self):
        async def run(saved_result, canceled, released):
            calls = []
            class Database:
                async def read(self, callback, **kwargs):
                    return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor([]))))
            async def saved(runtime, action):
                return (9, {"reason": "missing", "device_id": "camera", "expected_driver_id": "driver"}, "original-key", None, 17, 8)
            async def settle(flow, copy_id, occurred_at):
                calls.append(("inspection", copy_id, occurred_at))
                return saved_result
            async def release(flow, copy_id, occurred_at):
                calls.append(("release", copy_id))
                return released
            async def action(action_id): return {"cancel_requested": canceled}
            namespace = {"_saved_internal_binding_failure": saved, "_binding": lambda action: object(),
                "BindingStatus": SimpleNamespace(DEVICE_MISSING=1, DRIVER_MISMATCH=2,
                    PHYSICAL_IDENTITY_MISMATCH=3, PHYSICAL_IDENTITY_CONFLICT=4),
                "BindingResult": lambda *args: args, "closing": closing,
                "_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1),
                "settle_inspection_read_failure": settle, "settle_terminal_inspection_read_slot": release,
                "ConsistencyError": ValueError}
            flow = object()
            runtime = SimpleNamespace(database=Database(), media=flow, wall_us=lambda: 20, action=action)
            result = await load("_handle_binding_failure", namespace)(runtime, {"id": 4, "cancel_requested": 0})
            return result, calls
        self.assertEqual(asyncio.run(run(True, False, True)), (False, [("inspection", 9, 17), ("release", 9)]))
        self.assertEqual(asyncio.run(run(True, False, False)), (True, [("inspection", 9, 17), ("release", 9)]))
        self.assertEqual(asyncio.run(run(False, False, False)), (True, [("inspection", 9, 17)]))
        self.assertEqual(asyncio.run(run(False, True, False)), (False, [("inspection", 9, 17)]))


if __name__ == "__main__": unittest.main()
