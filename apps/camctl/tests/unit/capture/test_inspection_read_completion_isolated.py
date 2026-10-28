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
    node = next(item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == name)
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

    def test_local_recovery_only_consumes_finished_inspection(self):
        module = ModuleType("camctl.capture.segment_inspections")
        module._step_from_ref = lambda inspection, copy_id: (inspection.state, copy_id)
        enums = ModuleType("camctl.contracts.enums")
        enums.enum_for = lambda name: SimpleNamespace(PENDING=1)
        read = extract(ROOT / "capture/action6_recovery.py", "_saved_inspection_without_media", {})
        with patch.dict(sys.modules, {module.__name__: module, enums.__name__: enums}):
            self.assertIsNone(read(SimpleNamespace(inspection=None, copy=None)))
            self.assertIsNone(read(SimpleNamespace(inspection=SimpleNamespace(state=1), copy=None)))
            self.assertEqual(read(SimpleNamespace(inspection=SimpleNamespace(state=3), copy=SimpleNamespace(copy_id=9))), (3, 9))

    def test_binding_failure_result_verifies_original_copy_and_error(self):
        calls = []
        binding = ModuleType("camctl.persistence.repositories.binding_read_cancel")
        binding.load_saved_binding_read_failure = lambda connection, action, copy, error: calls.append((action, copy, error))
        ns = {"closing": closing, "Mapping": dict, "ConsistencyError": ValueError}
        ns["_binding_read_error"] = extract(ROOT / "persistence/repositories/segment_inspections.py", "_binding_read_error", ns)
        check = extract(ROOT / "persistence/repositories/segment_inspections.py", "_binding_read_result", ns)
        rows = [(9,)]
        connection = SimpleNamespace(execute=lambda sql, params: Cursor(rows))
        error = {"code": "device_binding_unavailable", "details": {"reason": "missing"}}
        document = {"failure": {"code": "read_failed", "read_error": error}}
        with patch.dict(sys.modules, {binding.__name__: binding}):
            self.assertTrue(check(connection, {"id": 8, "action_id": 6}, document))
            self.assertEqual(calls, [(6, 9, error)])
            rows[:] = []
            with self.assertRaises(ValueError): check(connection, {"id": 8, "action_id": 6}, document)
            self.assertFalse(check(connection, {"id": 8, "action_id": 6}, {"failure": {"code": "read_failed"}}))

    def test_canceled_owner_retires_unsubmitted_binding_inspection(self):
        inspection = {"id": 8, "action_id": 6, "state": 1}
        action = {"id": 6, "status": 2, "cancel_requested": 1}
        class Plan:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        namespace = {"saved_transaction_events": lambda connection, key: None,
            "row_facts": lambda connection, table, identity: inspection if table == "segment_inspections" else action,
            "_STATE": SimpleNamespace(PENDING=1), "_binding_read_result": lambda *args: True,
            "_inspection_ref": lambda row: SimpleNamespace(state=row["state"]),
            "CommandPlan": Plan, "ConsistencyError": ValueError}
        command = extract(ROOT / "persistence/repositories/segment_inspections.py", "_SaveInspectionResultCommand", namespace)
        request = SimpleNamespace(inspection_id=8, state=3, result_json={"failure": "binding"})
        scope = SimpleNamespace(connection=object(), allocate=lambda n: self.fail("取消后不能产生检查事件"))
        plan = command(request, "original-operation").plan(scope)
        self.assertTrue(plan.read_only)
        self.assertEqual(plan.result.state, 1)

    def test_independent_inspection_guard_rechecks_cancel_and_original_read(self):
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        class Reads:
            connection = object()
        module.RetryWaitReads = Reads
        checks = []
        namespace = {"_EVENT": {"id": 38}, "_RESULT": 2, "_CREATE": 1,
            "_STATE": SimpleNamespace(PENDING=1), "EventValidationError": ValueError,
            "_binding_read_error": lambda doc: {"code": "device_binding_unavailable"},
            "_binding_read_result": lambda connection, inspection, doc: checks.append(inspection["id"]),
            "_result_requires_read": lambda state, doc: False}
        guard = extract(ROOT / "persistence/repositories/segment_inspections.py", "_segment_inspection_guard", namespace)
        row = SimpleNamespace(table="segment_inspections", row_id=8, after=SimpleNamespace(values={
            "state": 3, "result_event_id": 20, "result_json": {"failure": "binding"}}))
        event = SimpleNamespace(event_type=38, reason=2, event_id=20, rows=[row])
        inspection = {"id": 8, "action_id": 6, "state": 1}
        action = {"id": 6, "status": 2, "cancel_requested": 0}
        context = SimpleNamespace(state_rows={"actions": {6: action}, "segment_inspections": {8: inspection}}, retry_wait_reads=Reads())
        with patch.dict(sys.modules, {module.__name__: module}):
            guard(event, context)
            self.assertEqual(checks, [8])
            action["cancel_requested"] = 1
            with self.assertRaises(ValueError): guard(event, context)
            action["cancel_requested"] = 0
            context.retry_wait_reads = None
            with self.assertRaises(ValueError): guard(event, context)

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
