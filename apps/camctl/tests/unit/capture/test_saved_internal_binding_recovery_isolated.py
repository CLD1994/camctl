"""内部读取绑定失败冷恢复，不导入实际包或数据库。"""

import ast
import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"


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


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class RecoveryTests(unittest.TestCase):
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
        self.assertEqual(asyncio.run(run([(9, "error")])),
            (9, {"reason": "physical_identity_mismatch"}, "original-key", {"serial": "original"}))
        with self.assertRaises(ValueError): asyncio.run(run([(9, "error"), (10, "error")]))

    def test_saved_failure_precedes_configuration_and_preserves_original_key(self):
        async def run(disposition):
            calls = []
            class Database:
                async def read(self, callback, **kwargs):
                    connection = SimpleNamespace(execute=lambda sql, args: Cursor([]))
                    return callback(SimpleNamespace(connection=connection))
            async def saved(runtime, action):
                return (9, {"reason": "missing", "device_id": "camera", "expected_driver_id": "driver"}, "original-key", None)
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


if __name__ == "__main__": unittest.main()
