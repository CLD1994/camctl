"""检查原读取收场、绑定失败及来源成功资格。"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camctl.capture import action6_recovery, segment_inspections
from camctl.capture.inspection_rules import inspection_result_requires_read
from camctl.capture.segment_models import CoverageStatus
from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CaptureResultPolicy
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError
from camctl.outputs import read_attempts
from camctl.persistence.repositories import binding_read_cancel, read_end
from camctl.persistence.repositories import segment_inspections as repository
from camctl.persistence.repositories import segment_transactions
from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
from camctl.persistence.repositories.transactions import TransactionScope


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def close(self): pass


class ReadCompletionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [(15, 10, 10, 3, "hash", 7, 6, 3, 1, 0, 2, "{}", 21)]
        self.connection = SimpleNamespace(execute=lambda sql, params: Cursor(self.rows))
        self.inspection = {"id": 8, "action_id": 6, "source_device_file_id": 15}
        self.settled = True

    def check_current(self):
        with patch.object(RetryWaitReads, "no_unsettled_attempts", return_value=self.settled):
            return repository.inspection_input_read_succeeded(self.connection, self.inspection)

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
        self.assertTrue(inspection_result_requires_read(2, {"tool": {"ran": False}}))
        self.assertTrue(inspection_result_requires_read(4, {"tool": {"ran": True}}))
        self.assertTrue(inspection_result_requires_read(3, {"failure": {"code": "probe_failed"}}))
        self.assertFalse(inspection_result_requires_read(3, {"tool": {"ran": False}, "failure": {"code": "read_failed"}}))

    def test_local_recovery_only_consumes_finished_inspection(self):
        with patch.object(segment_inspections, "_step_from_ref", side_effect=lambda inspection, copy_id: (inspection.state, copy_id)):
            self.assertIsNone(action6_recovery._saved_inspection_without_media(SimpleNamespace(inspection=None, copy=None)))
            self.assertIsNone(action6_recovery._saved_inspection_without_media(SimpleNamespace(inspection=SimpleNamespace(state=1), copy=None)))
            self.assertEqual(action6_recovery._saved_inspection_without_media(
                SimpleNamespace(inspection=SimpleNamespace(state=3), copy=SimpleNamespace(copy_id=9))), (3, 9))
            with self.assertRaises(ConsistencyError):
                action6_recovery._saved_inspection_without_media(SimpleNamespace(inspection=SimpleNamespace(state=2), copy=None))

    def test_binding_failure_result_verifies_original_copy_and_error(self):
        calls = []
        rows = [(9,)]
        connection = SimpleNamespace(execute=lambda sql, params: Cursor(rows))
        error = {"code": "device_binding_unavailable", "details": {"reason": "missing"}}
        document = {"failure": {"code": "read_failed", "read_error": error}}
        with patch.object(binding_read_cancel, "load_saved_binding_read_failure", side_effect=lambda connection, action, copy, error: calls.append((action, copy, error))):
            self.assertTrue(repository._binding_read_result(connection, {"id": 8, "action_id": 6}, document))
            self.assertEqual(calls, [(6, 9, error)])
            rows[:] = []
            with self.assertRaises(ConsistencyError): repository._binding_read_result(connection, {"id": 8, "action_id": 6}, document)
            self.assertFalse(repository._binding_read_result(connection, {"id": 8, "action_id": 6}, {"failure": {"code": "read_failed"}}))

    def test_canceled_owner_retires_unsubmitted_binding_inspection(self):
        inspection = {"id": 8, "action_id": 6, "set_id": 2, "member_id": 3, "source_device_file_id": 15,
                      "state": 1, "target_json": {}, "budget_json": {}, "result_json": None}
        action = {"id": 6, "status": 2, "cancel_requested": 1}
        plans = []
        def commit(command, key, owned):
            plan = command.plan(TransactionScope(owned.connection, 0, 0))
            plans.append(plan)
            return SimpleNamespace(kind="completed", result=plan.result)
        request = repository.InspectionResultSave(8, 3, {"failure": "binding"}, 10)
        with patch.object(repository, "commit_operation", side_effect=commit), \
             patch.object(repository, "saved_transaction_events", return_value=None), \
             patch.object(repository, "row_facts", side_effect=lambda connection, table, identity: inspection if table == "segment_inspections" else action), \
             patch.object(repository, "_binding_read_result", return_value=True):
            outcome = repository.SegmentInspectionRepository().save_result(request, "original-operation", SimpleNamespace(connection=object()))
        self.assertTrue(plans[0].read_only)
        self.assertEqual(outcome.value.state, 1)

    def test_independent_inspection_guard_rechecks_cancel_and_original_read(self):
        guards = {}
        repository.register_segment_inspection_guard(guards.__setitem__)
        error = {"code": "device_binding_unavailable", "details": {"reason": "missing"}}
        document = {"failure": {"code": "read_failed", "read_error": error}}
        row = SimpleNamespace(table="segment_inspections", row_id=8, after=SimpleNamespace(values={
            "state": 3, "result_event_id": 20, "result_json": document}))
        definition = load_event_registry()["events"]["SEGMENT_INSPECTION"]
        event = SimpleNamespace(event_type=definition["id"], reason=definition["branches"]["RESULT"]["reason"], event_id=20, rows=[row])
        inspection = {"id": 8, "action_id": 6, "state": 1}
        action = {"id": 6, "status": 2, "cancel_requested": 0}
        context = SimpleNamespace(state_rows={"actions": {6: action}, "segment_inspections": {8: inspection}},
                                  retry_wait_reads=RetryWaitReads(object()))
        with patch.object(repository, "_binding_read_result") as check:
            guards["segment_inspection"](event, context)
            check.assert_called_once_with(context.retry_wait_reads.connection, inspection, document)
            action["cancel_requested"] = 1
            with self.assertRaises(EventValidationError): guards["segment_inspection"](event, context)
            action["cancel_requested"] = 0
            context.retry_wait_reads = None
            with self.assertRaises(EventValidationError): guards["segment_inspection"](event, context)

    def test_cold_end_uses_original_ticket_and_rejects_other_ticket(self):
        saved = [("original", "actual_end", 10, None)]
        with patch.object(read_attempts, "running_read_ticket", return_value=("original", 4)) as running, \
             patch.object(read_end, "load_read_end", side_effect=lambda *args: saved[0]):
            self.assertEqual(read_attempts.unfinished_read_end(object(), 9), ("original", saved[0]))
            saved[0] = None
            self.assertEqual(read_attempts.unfinished_read_end(object(), 9), ("original", None))
            saved[0] = ("other", "actual_end", 10, None)
            with self.assertRaises(ConsistencyError): read_attempts.unfinished_read_end(object(), 9)
            running.return_value = None
            self.assertIsNone(read_attempts.unfinished_read_end(object(), 9))

    def test_source_success_requires_pending_inspection_and_read_to_finish(self):
        row = {"state": 1, "result_json": {"tool": {"ran": True}}, "read_finished": False}
        db = SimpleNamespace(execute=lambda sql, params: Cursor([(8,)] if params[2] == 0 else []))
        action = {"id": 6, "execution_spec_json": {"result_policy": CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value}}
        assessment = SimpleNamespace(coverage_status=object())
        with patch.object(segment_transactions, "row_facts", return_value=row), \
             patch.object(repository, "inspection_input_read_succeeded", side_effect=lambda connection, inspection: inspection["read_finished"]):
            with self.assertRaises(ConsistencyError): segment_transactions._verify_action6_inspections_finished(db, action, {"id": 7}, assessment)
            row["state"] = 2
            with self.assertRaises(ConsistencyError): segment_transactions._verify_action6_inspections_finished(db, action, {"id": 7}, assessment)
            row["read_finished"] = True
            segment_transactions._verify_action6_inspections_finished(db, action, {"id": 7}, assessment)
            assessment.coverage_status = CoverageStatus.NOT_PERFORMED
            with self.assertRaises(ConsistencyError): segment_transactions._verify_action6_inspections_finished(db, action, {"id": 7}, assessment)


if __name__ == "__main__":
    unittest.main()
