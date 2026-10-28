"""隔离包资源与 SQLite，核对打开时的索引结构检查。"""

import ast
from contextlib import closing
from functools import lru_cache
import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).parents[3] / "src" / "camctl"
INDEXES = {
    "actions_pending_capture_selection": "CREATE INDEX actions_pending_capture_selection ON actions(device_id, driver_id, scheduled_at, plan_id, input_index, id) WHERE status = 1 AND execution_started = 0 AND cancel_requested = 0",
    "history_events_row": "CREATE INDEX history_events_row ON history_events(event_type, json_extract(body_json, '$.rows[0].table'), json_extract(body_json, '$.rows[0].id'), id)",
    "operation_attempts_run_status": "CREATE INDEX operation_attempts_run_status ON operation_attempts(run_id, status)",
    "operation_attempts_without_result": "CREATE INDEX operation_attempts_without_result ON operation_attempts(run_id) WHERE result_json IS NULL",
}


class Cursor:
    def __init__(self, value): self.value = value
    def fetchone(self): return self.value
    def close(self): pass


class Connection:
    def __init__(self, indexes): self.indexes = indexes
    def execute(self, query, parameters):
        value = self.indexes.get(parameters[0])
        return Cursor(None if value is None else (value,))


def load_structure():
    recovery_source = ast.parse((SOURCE / "persistence" / "recovery_structure.py").read_text())
    tokens = next(node for node in recovery_source.body if isinstance(node, ast.FunctionDef) and node.name == "_tokens")
    namespace = {"_TOKENS": re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*(?:\n|$)|/\*.*?\*/|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", re.S)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[tokens], type_ignores=[])), "tokens", "exec"), namespace)
    recovery = ModuleType("camctl.persistence.recovery_structure")
    recovery._tokens = namespace["_tokens"]
    resources = ModuleType("camctl.resources")
    resources.ResourceError = type("ResourceError", (RuntimeError,), {})
    resources.resource_bytes = lambda name: (";\n".join(INDEXES.values()) + ";").encode()
    modules = {recovery.__name__: recovery, resources.__name__: resources}
    spec = importlib.util.spec_from_file_location("isolated_retry_structure", SOURCE / "persistence" / "retry_wait_structure.py")
    structure = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(structure)
    return structure


class RetryStructureTests(unittest.TestCase):
    def test_authoritative_indexes_are_accepted(self):
        load_structure().verify_retry_wait_structure(Connection(INDEXES))

    def test_each_missing_index_is_rejected(self):
        structure = load_structure()
        for name in INDEXES:
            with self.subTest(name=name):
                indexes = dict(INDEXES)
                del indexes[name]
                with self.assertRaises(ValueError):
                    structure.verify_retry_wait_structure(Connection(indexes))

    def test_each_changed_index_is_rejected(self):
        structure = load_structure()
        for name in INDEXES:
            with self.subTest(name=name):
                indexes = dict(INDEXES)
                indexes[name] = indexes[name].replace("run_id", "id").replace("event_type", "id").replace("device_id", "id")
                with self.assertRaises(ValueError):
                    structure.verify_retry_wait_structure(Connection(indexes))

    def test_equivalent_whitespace_and_case_are_accepted(self):
        indexes = {name: value.replace("CREATE INDEX", "create /* comment */ index") for name, value in INDEXES.items()}
        load_structure().verify_retry_wait_structure(Connection(indexes))

    def test_partial_index_predicate_is_part_of_the_definition(self):
        indexes = dict(INDEXES)
        indexes["operation_attempts_without_result"] = indexes["operation_attempts_without_result"].replace(
            "result_json IS NULL", "result_json IS NOT NULL")
        with self.assertRaises(ValueError):
            load_structure().verify_retry_wait_structure(Connection(indexes))

    def test_missing_authoritative_definition_is_rejected(self):
        structure = load_structure()
        structure.resource_bytes = lambda name: b""
        with self.assertRaises(ValueError):
            structure.verify_retry_wait_structure(Connection(INDEXES))

    def test_unreadable_authority_is_rejected(self):
        structure = load_structure()
        def fail(name): raise structure.ResourceError("unavailable")
        structure.resource_bytes = fail
        with self.assertRaises(ValueError):
            structure.verify_retry_wait_structure(Connection(INDEXES))

    def test_open_structure_translates_index_failure(self):
        source = ast.parse((SOURCE / "persistence" / "runtime.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_verify_structure")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        events = ModuleType("camctl.history.events")
        events.business_tables_for_format = lambda version: ()
        events.load_event_registry = lambda: {"tables": {}}
        structure = ModuleType("camctl.persistence.retry_wait_structure")
        def reject(connection): raise ValueError("missing index")
        structure.verify_retry_wait_structure = reject
        class Invalid(Exception): pass
        namespace = {"sqlite3": sqlite3, "expected_table_names": lambda: set(),
                     "_verify_format_constraints": lambda connection, version: None, "DatabaseInvalidError": Invalid}
        exec(compile(ast.fix_missing_locations(isolated), "runtime", "exec"), namespace)
        connection = SimpleNamespace(execute=lambda query: iter(()))
        with patch.dict(sys.modules, {events.__name__: events, structure.__name__: structure}):
            with self.assertRaisesRegex(Invalid, "missing index"):
                namespace["_verify_structure"](connection, 6)

    def test_open_existing_closes_connection_when_index_check_fails(self):
        source = ast.parse((SOURCE / "persistence" / "runtime.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "open_existing")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        class DatabaseError(Exception): pass
        class Invalid(DatabaseError): pass
        class FakeConnection:
            closed = False
            def execute(self, query): return Cursor(("wal",))
            def close(self): self.closed = True
        connection = FakeConnection()
        mode = SimpleNamespace(value="ro")
        class Modes:
            EXISTING_RW = object()
        path = SimpleNamespace(is_dir=lambda: False, exists=lambda: True,
                               resolve=lambda: SimpleNamespace(as_uri=lambda: "db"))
        sqlite = SimpleNamespace(connect=lambda *args, **kwargs: connection, Error=sqlite3.Error)
        def reject(connection, version): raise Invalid("missing index")
        namespace = {"sqlite3": sqlite, "DbOpenMode": Modes, "StateDatabaseError": DatabaseError,
            "DatabaseInvalidError": Invalid, "ensure_runtime_library": lambda: None,
            "_set_and_verify": lambda *args: None, "_read_metadata": lambda connection: SimpleNamespace(format_version=6),
            "_verify_structure": reject}
        exec(compile(ast.fix_missing_locations(isolated), "runtime", "exec"), namespace)
        with self.assertRaisesRegex(Invalid, "missing index"):
            namespace["open_existing"](path, mode, SimpleNamespace(busy_timeout_ms=1))
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
