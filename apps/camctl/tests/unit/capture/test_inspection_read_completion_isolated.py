"""检查原读取收场与来源成功资格；不导入实际包资源或数据库。"""

import ast
from contextlib import closing
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[3] / "src" / "camctl"


def extract(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def close(self): pass


class ReadCompletionTests(unittest.TestCase):
    def setUp(self):
        enums = {"operation_runs.kind": SimpleNamespace(READ_FILE=3),
                 "operation_runs.status": SimpleNamespace(SUCCEEDED=3),
                 "operation_attempts.status": SimpleNamespace(SUCCEEDED=2),
                 "file_copies.verification_state": SimpleNamespace(MATCHED=3, SOURCE_CHECKSUM_UNAVAILABLE=5)}
        self.ns = {"closing": closing, "enum_for": enums.__getitem__}
        self.check = extract(ROOT / "persistence/repositories/segment_inspections.py", "inspection_input_read_succeeded", self.ns)
        self.rows = [(15, 10, 10, 3, "hash", 7, 6, 3, 1, 0, 2, "{}", 21)]
        self.connection = SimpleNamespace(execute=lambda sql, params: Cursor(self.rows))
        self.inspection = {"id": 8, "action_id": 6, "source_device_file_id": 15}
        self.settled = True
        owner = self
        class Reads:
            def __init__(self, connection): pass
            def no_unsettled_attempts(self, run_id): return owner.settled
        self.module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        self.module.RetryWaitReads = Reads

    def check_current(self):
        with patch.dict(sys.modules, {self.module.__name__: self.module}):
            return self.check(self.connection, self.inspection)

    def test_accepts_only_original_completed_read(self):
        self.assertTrue(self.check_current())
        original = self.rows[0]
        for index, value in ((0, 16), (2, 9), (3, 1), (4, None), (6, 99),
                             (7, 2), (8, 0), (9, 1), (10, 1), (11, None), (12, None)):
            with self.subTest(index=index):
                changed = list(original)
                changed[index] = value
                self.rows[:] = [tuple(changed)]
                self.assertFalse(self.check_current())
        self.rows[:] = [original, original]
        self.assertFalse(self.check_current())
        self.rows[:] = [original]
        self.settled = False
        self.assertFalse(self.check_current())

    def test_read_failure_outcomes_do_not_require_probe_input(self):
        ns = {"_STATE": SimpleNamespace(COMPLETED=2), "Mapping": dict}
        requires = extract(ROOT / "persistence/repositories/segment_inspections.py", "_result_requires_read", ns)
        self.assertTrue(requires(2, {"tool": {"ran": False}}))
        self.assertTrue(requires(4, {"tool": {"ran": True}}))
        self.assertTrue(requires(3, {"failure": {"code": "probe_failed"}}))
        self.assertFalse(requires(3, {"tool": {"ran": False}, "failure": {"code": "read_failed"}}))

    def test_cold_end_uses_original_ticket_and_rejects_other_ticket(self):
        saved = [("original", "actual_end", 10, None)]
        module = ModuleType("camctl.persistence.repositories.read_end")
        module.load_read_end = lambda copy_id, owned: saved[0]
        ns = {"running_read_ticket": lambda owned, copy_id: ("original", 4),
              "ConsistencyError": ValueError}
        load = extract(ROOT / "outputs/read_attempts.py", "unfinished_read_end", ns)
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(load(object(), 9), ("original", saved[0]))
            saved[0] = None
            self.assertEqual(load(object(), 9), ("original", None))
            saved[0] = ("other", "actual_end", 10, None)
            with self.assertRaises(ValueError):
                load(object(), 9)
            ns["running_read_ticket"] = lambda owned, copy_id: None
            self.assertIsNone(load(object(), 9))

    def test_source_success_requires_pending_inspection_and_read_to_finish(self):
        models = ModuleType("camctl.capture.segment_models")
        models.CoverageStatus = SimpleNamespace(NOT_PERFORMED=object())
        tasks = ModuleType("camctl.devices.tasks")
        tasks.CaptureResultPolicy = SimpleNamespace(ACTION6_VIDEO_SEGMENTS_V1=SimpleNamespace(value="action6"))
        inspections = ModuleType("camctl.persistence.repositories.segment_inspections")
        inspections._result_requires_read = lambda state, result: result["requires_read"]
        inspections.inspection_input_read_succeeded = lambda connection, inspection: inspection["read_finished"]
        row = {"state": 1, "result_json": {"requires_read": True}, "read_finished": False}
        ns = {"closing": closing, "row_facts": lambda db, table, identity: row,
              "enum_for": lambda name: SimpleNamespace(PENDING=1), "ConsistencyError": ValueError}
        verify = extract(ROOT / "persistence/repositories/segment_transactions.py", "_verify_action6_inspections_finished", ns)
        db = SimpleNamespace(execute=lambda sql, params: Cursor([(8,)] if params[2] == 0 else []))
        action = {"id": 6, "execution_spec_json": {"result_policy": "action6"}}
        assessment = SimpleNamespace(coverage_status=object())
        with patch.dict(sys.modules, {models.__name__: models, tasks.__name__: tasks, inspections.__name__: inspections}):
            with self.assertRaises(ValueError): verify(db, action, {"id": 7}, assessment)
            row["state"] = 2
            with self.assertRaises(ValueError): verify(db, action, {"id": 7}, assessment)
            row["read_finished"] = True
            verify(db, action, {"id": 7}, assessment)
            assessment.coverage_status = models.CoverageStatus.NOT_PERFORMED
            with self.assertRaises(ValueError): verify(db, action, {"id": 7}, assessment)


if __name__ == "__main__":
    unittest.main()
