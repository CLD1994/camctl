"""隔离包资源及数据库，核对未派发分段读取的取消收场。"""

import ast
from functools import lru_cache
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).parents[3] / "src" / "camctl"
OUTPUTS = ROOT / "persistence" / "repositories" / "outputs.py"
OPERATIONS = ROOT / "persistence" / "repositories" / "operations.py"
INSPECTIONS = ROOT / "capture" / "segment_inspections.py"


@lru_cache(maxsize=None)
def tree(path): return ast.parse(path.read_text())


def extract(path, name, namespace):
    node = next(node for node in tree(path).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class GuardError(Exception): pass


def release_command(*, canceled=1, inspection_state=4, attempts=0, active=False):
    class Reads:
        def __init__(self, connection): pass
        def no_attempts(self, run_id): return attempts == 0
    reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
    reads.RetryWaitReads = Reads
    class Plan:
        def __init__(self, **values): self.__dict__.update(values)
    namespace = {"__name__": "isolated.outputs", "__package__": "camctl.persistence.repositories",
        "_RUN_STATUS": SimpleNamespace(PENDING=1, CANCELED=5), "_RUN_KIND": SimpleNamespace(READ_FILE=3),
        "_SEGMENT_INSPECTION_STATE": SimpleNamespace(PENDING=1, COMPLETED=2, FAILED=3, UNCONFIRMED=4),
        "_ATTEMPT_STATUS": SimpleNamespace(RUNNING=1),
        "_RUN_TERMINAL": {3, 4, 5, 6, 7}, "_SLOT_RELEASE": "release", "_SLOT_GRANT": "grant",
        "_OPERATION_CONFIGURED_EVENT": 10, "_COPY_CHANGED_EVENT": 22, "_SLOT_REASON": 6,
        "SlotOutcome": SimpleNamespace(RELEASED="released", WAIT="wait", ALREADY_RELEASED="already"),
        "SlotDecision": lambda outcome, reason=None: SimpleNamespace(outcome=outcome, reason=reason),
        "CommandPlan": Plan, "ConsistencyError": ValueError, "TransactionError": ValueError,
        "json_equal": lambda first, second: first == second,
        "_update": lambda table, identity, before, after: SimpleNamespace(table=table, row_id=identity,
            before=SimpleNamespace(exists=True, values={key: value for key, value in before.items() if value != after[key]}),
            after=SimpleNamespace(exists=True, values={key: value for key, value in after.items() if value != before[key]})),
        "_envelope": lambda event_id, txn_id, event_type, reason, rows, occurred_at: SimpleNamespace(
            event_id=event_id, transaction_id=txn_id, event_type=event_type, reason=reason, rows=rows,
            occurred_at=occurred_at, evidence={})}
    cls = extract(OUTPUTS, "_SlotChangeCommand", namespace)
    command = object.__new__(cls)
    command._request = SimpleNamespace(copy_id=9, occurred_at=10, capture_selection=None)
    command._direction = "release"
    command._owners = {("file_copies", 9): ("action", 7)}
    command._coverage = None
    command._state = {"segment_inspections": {8: {"id": 8, "state": inspection_state, "action_id": 7}},
                      "operation_attempts": {}}
    command._load_owner_action = lambda connection, copy: {"id": 7, "cancel_requested": canceled}
    def load_attempts(connection, run_id):
        if active:
            command._state["operation_attempts"][12] = {"status": 1}
    command._load_attempts = load_attempts
    copy = {"id": 9, "inspection_id": 8, "slot_device_id": "camera", "committed_bytes": 0, "source_size": 10}
    run = {"id": 11, "kind": 3, "copy_id": 9, "action_id": 7, "status": 1,
           "attempts_used": 0, "retry_wait_required": 0, "error_json": None}
    scope = SimpleNamespace(connection=None, allocate=lambda count: SimpleNamespace(first_event_id=20, txn_id=3))
    return command, copy, run, scope, reads


class ReleaseTransactionTests(unittest.TestCase):
    def test_cancelled_inspection_closes_run_and_slot_together(self):
        command, copy, run, scope, reads = release_command()
        with patch.dict(sys.modules, {reads.__name__: reads}):
            plan = command._release(scope, copy, "camera", run)
        self.assertEqual([(event.event_type, event.reason) for event in plan.events], [(10, 3), (22, 6)])
        self.assertEqual(plan.events[0].rows[0].after.values["status"], 5)
        self.assertIsNone(plan.events[1].rows[0].after.values["slot_device_id"])
        self.assertEqual(plan.events[0].transaction_id, plan.events[1].transaction_id)
        self.assertEqual(run["attempts_used"], 0)

    def test_not_cancelled_or_pending_inspection_keeps_slot(self):
        for values in ({"canceled": 0}, {"inspection_state": 1}):
            with self.subTest(values=values):
                command, copy, run, scope, reads = release_command(**values)
                with patch.dict(sys.modules, {reads.__name__: reads}):
                    plan = command._release(scope, copy, "camera", run)
                self.assertEqual(plan.result.reason, "still_reading")

    def test_existing_attempt_cannot_be_treated_as_unstarted(self):
        command, copy, run, scope, reads = release_command(attempts=1, active=True)
        with patch.dict(sys.modules, {reads.__name__: reads}):
            plan = command._release(scope, copy, "camera", run)
        self.assertEqual(plan.result.reason, "read_active")

    def test_waiting_retry_is_not_cleared(self):
        command, copy, run, scope, reads = release_command()
        run["retry_wait_required"] = 1
        with patch.dict(sys.modules, {reads.__name__: reads}):
            plan = command._release(scope, copy, "camera", run)
        self.assertEqual(plan.result.reason, "retry_wait")

    def test_replay_requires_both_original_events(self):
        command, copy, run, scope, reads = release_command()
        command._load_run = lambda connection, identity: run
        saved = [{"type": 10, "reason": 3, "occurred_at": 10, "body": {"rows": [{
            "table": "operation_runs", "id": 11, "before": {"exists": True, "values": {
                "status": 1}}, "after": {"exists": True, "values": {"status": 5}}}]}},
            {"type": 22, "reason": 6, "occurred_at": 10, "body": {"rows": [{"table": "file_copies", "id": 9,
                "before": {"exists": True, "values": {"slot_device_id": "camera"}},
                "after": {"exists": True, "values": {"slot_device_id": None}}}]}}]
        result = command._reuse(saved, None)
        self.assertTrue(result.read_only)
        saved[0]["body"]["rows"][0]["id"] = 99
        with self.assertRaises(ValueError):
            command._reuse(saved, None)


class FinishGuardTests(unittest.TestCase):
    def guard(self, *, canceled=1, attempts=0, release=True, inspection_state=4, waiting=0, owner_id=7):
        class Reads:
            def no_attempts(self, run_id): return attempts == 0
        reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        reads.RetryWaitReads = Reads
        def fail(message): raise GuardError(message)
        run = {"kind": 3, "status": 5, "retry_wait_required": 0, "attempts_used": 0,
               "copy_id": 9, "action_id": 7, "error_json": None}
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "_RUN_STATUS": SimpleNamespace(PENDING=1, CANCELED=5, FAILED=4, UNCONFIRMED=6, SUCCEEDED=3),
            "_RUN_KIND": SimpleNamespace(READ_FILE=3, PREPARE_CAPTURE=10, CHECK_DEVICE_AVAILABILITY=12), "_OPERATION_CONFIGURED_EVENT": 10,
            "_row_after_facts": lambda context, row: run, "_fail": fail, "Mapping": dict,
            "enum_for": lambda name: SimpleNamespace(PENDING=1, COMPLETED=2, FAILED=3, UNCONFIRMED=4)}
        guard = extract(OPERATIONS, "_operation_finish_guard", namespace)
        row = SimpleNamespace(table="operation_runs", row_id=11,
            before=SimpleNamespace(exists=True, values={"status": 1}),
            after=SimpleNamespace(values={"status": 5}))
        event = SimpleNamespace(rows=(row,), event_type=10, reason=3, evidence={})
        context = SimpleNamespace(state_rows={"operation_runs": {11: {"retry_wait_required": waiting, "error_json": None}},
            "file_copies": {9: {"id": 9, "inspection_id": 8, "slot_device_id": "camera"}},
            "segment_inspections": {8: {"action_id": owner_id, "state": inspection_state}},
            "actions": {7: {"cancel_requested": canceled}}},
            transaction_rows={"file_copies": {9: {"slot_device_id": None if release else "camera"}}},
            retry_wait_reads=Reads())
        with patch.dict(sys.modules, {reads.__name__: reads}):
            guard(event, context)

    def test_guard_accepts_cancel_and_atomic_release(self): self.guard()
    def test_guard_rejects_missing_release(self):
        with self.assertRaises(GuardError): self.guard(release=False)
    def test_guard_rejects_non_cancelled_owner(self):
        with self.assertRaises(GuardError): self.guard(canceled=0)
    def test_guard_rejects_existing_attempt(self):
        with self.assertRaises(GuardError): self.guard(attempts=1)
    def test_guard_rejects_unfinished_inspection(self):
        with self.assertRaises(GuardError): self.guard(inspection_state=1)
    def test_guard_rejects_existing_wait(self):
        with self.assertRaises(GuardError): self.guard(waiting=1)
    def test_guard_rejects_another_inspection_owner(self):
        with self.assertRaises(GuardError): self.guard(owner_id=8)


class SharedSaveOwnershipTests(unittest.TestCase):
    def test_only_finished_failed_save_can_be_claimed(self):
        shared_type = extract(ROOT / "persistence" / "business.py", "SharedSave", {})
        shared = shared_type()
        self.assertFalse(shared.failed())
        shared._task = SimpleNamespace(done=lambda: False)
        self.assertFalse(shared.failed())
        shared._task = SimpleNamespace(done=lambda: True, cancelled=lambda: False,
                                       exception=lambda: None, result=lambda: SimpleNamespace(error=None))
        self.assertFalse(shared.failed())
        shared._task = SimpleNamespace(done=lambda: True, cancelled=lambda: False,
                                       exception=lambda: None, result=lambda: SimpleNamespace(error=ValueError()))
        self.assertTrue(shared.failed())
        shared.retry_failed()
        self.assertIsNone(shared._task)


class InspectionSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def run_settlement(self, *, pending_intent=False, run_status=1, unfinished=False, slot_outcome="released",
                             pending_failed=True, recovered_ticket=False, canceled=1,
                             continuation=None, existing_result=False, end_id=None, slot="camera"):
        calls = []
        state = {"run_status": run_status, "slot": slot}
        class Cursor:
            def __init__(self, value): self.value = value
            def fetchall(self): return self.value
            def fetchone(self): return self.value
        class Connection:
            def execute(self, query, parameters):
                if "SELECT c.inspection_id" in query:
                    return Cursor([(8, state["slot"], state["run_status"], 0, 4, canceled, end_id)])
                if "SELECT 1 FROM operation_attempts" in query:
                    return Cursor((1,) if unfinished else None)
                raise AssertionError(query)
        class Database:
            async def read(self, operation, **kwargs): return operation(SimpleNamespace(connection=Connection()))
        async def save(flow, copy_id, operation, request, repository, **kwargs):
            calls.append((operation, kwargs.get("retry", False)))
            if operation == "read_intent":
                flow.pending_read_business.pop((copy_id, operation))
                if recovered_ticket:
                    flow.continuing_read_tickets[copy_id] = SimpleNamespace(
                        ticket=SimpleNamespace(run_id=11, attempt_id=1, target_id="9"),
                        never_dispatched=True, inspection_cancel_recovery=True)
                    state["run_status"] = 2
                return SimpleNamespace(value=SimpleNamespace())
            return SimpleNamespace(value=SimpleNamespace(outcome=slot_outcome))
        async def settle_result(flow, pending, *, retry):
            calls.append(("save_result", retry))
            state["run_status"] = 5
            flow.continuing_read_tickets.pop(9)
            flow.pending_read_results.pop((11, 1), None)
        def prevented(flow, ticket, prevented, evidence):
            calls.append(("not_dispatched", prevented.reason, prevented.canceled))
            return SimpleNamespace(ticket=ticket)
        async def settle_end(flow, copy_id):
            calls.append(("saved_end", copy_id))
            state["run_status"] = 5
            state["slot"] = None
        pending = {(9, "read_intent"): SimpleNamespace(request="original", save="original-save",
                    shared=SimpleNamespace(failed=lambda: pending_failed), inspection_cancel_recovery=[])} if pending_intent else {}
        runtime = SimpleNamespace(database=Database(), pending_read_business=pending,
                                  continuing_read_tickets={} if continuation is None else {9: continuation},
                                  pending_read_results={(11, 1): object()} if existing_result else {}, evidence=object())
        module = ModuleType("camctl.outputs.read_attempts")
        module.save_read_business = save
        module.prevented_read_result = prevented
        module.ReadDispatchPrevented = lambda reason, canceled: SimpleNamespace(reason=reason, canceled=canceled)
        media = ModuleType("camctl.capture.media_flow")
        media._save_internal_read_and_settle = settle_result
        statuses = {"PENDING": 1, "ACTIVE": 2, "SUCCEEDED": 3, "FAILED": 4, "CANCELED": 5}
        namespace = {"__name__": "isolated.inspections", "enum_for": lambda name: SimpleNamespace(
            READ_FILE=3) if name == "operation_runs.kind" else SimpleNamespace(RUNNING=1),
            "ConsistencyError": ValueError, "_INSPECTION_STATE": SimpleNamespace(PENDING=1),
            "_RUN_STATUS": statuses, "SlotOutcome": SimpleNamespace(RELEASED="released", ALREADY_RELEASED="already"),
            "SlotRequest": lambda copy_id, now: (copy_id, now),
            "_settle_canceled_inspection_end": settle_end,
            "OutputsRepository": lambda: SimpleNamespace(release_read_slot="release")}
        settle = extract(INSPECTIONS, "settle_terminal_inspection_read_slot", namespace)
        with patch.dict(sys.modules, {module.__name__: module, media.__name__: media}):
            result = await settle(runtime, 9, 10)
        return result, calls

    async def test_pending_original_intent_is_reconciled_before_zero_attempt_release(self):
        result, calls = await self.run_settlement(pending_intent=True)
        self.assertTrue(result)
        self.assertEqual(calls, [("read_intent", True), ("release_read_slot", False)])

    async def test_inflight_original_intent_is_not_taken_over(self):
        result, calls = await self.run_settlement(pending_intent=True, pending_failed=False)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_unknown_receipt_granted_ticket_is_saved_as_not_dispatched_before_release(self):
        result, calls = await self.run_settlement(pending_intent=True, recovered_ticket=True)
        self.assertTrue(result)
        self.assertEqual(calls, [("read_intent", True), ("not_dispatched", "canceled_before_dispatch", True),
                                 ("save_result", False), ("release_read_slot", False)])

    async def test_non_cancelled_granted_ticket_is_not_synthesized(self):
        result, calls = await self.run_settlement(pending_intent=True, recovered_ticket=True, canceled=0)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_recovered_ticket_survives_waiter_interruption(self):
        continuation = SimpleNamespace(ticket=SimpleNamespace(run_id=11, attempt_id=1, target_id="9"),
                                       never_dispatched=True, inspection_cancel_recovery=True)
        result, calls = await self.run_settlement(run_status=2, continuation=continuation)
        self.assertTrue(result)
        self.assertEqual(calls, [("not_dispatched", "canceled_before_dispatch", True),
                                 ("save_result", False), ("release_read_slot", False)])

    async def test_unknown_result_is_retried_without_creating_another_result(self):
        continuation = SimpleNamespace(ticket=SimpleNamespace(run_id=11, attempt_id=1, target_id="9"),
                                       never_dispatched=True, inspection_cancel_recovery=True)
        result, calls = await self.run_settlement(run_status=2, continuation=continuation, existing_result=True)
        self.assertTrue(result)
        self.assertEqual(calls, [("save_result", True), ("release_read_slot", False)])

    async def test_restart_without_local_continuation_keeps_original_running_ticket(self):
        result, calls = await self.run_settlement(run_status=2)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_canceled_saved_end_settles_original_even_after_slot_release(self):
        result, calls = await self.run_settlement(run_status=2, end_id=30, slot=None)
        self.assertTrue(result)
        self.assertEqual(calls, [("saved_end", 9)])

    async def test_already_dispatched_ticket_is_not_declared_unstarted(self):
        continuation = SimpleNamespace(ticket=SimpleNamespace(run_id=11, attempt_id=1, target_id="9"),
                                       never_dispatched=False, inspection_cancel_recovery=True)
        result, calls = await self.run_settlement(run_status=2, continuation=continuation)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_running_original_intent_blocks_release(self):
        result, calls = await self.run_settlement(pending_intent=True, run_status=2)
        self.assertFalse(result)
        self.assertEqual(calls, [("read_intent", True)])

    async def test_unfinished_attempt_blocks_release(self):
        result, calls = await self.run_settlement(unfinished=True)
        self.assertFalse(result)
        self.assertEqual(calls, [])

    async def test_rejected_release_is_not_reported_as_completed(self):
        result, calls = await self.run_settlement(slot_outcome="wait")
        self.assertFalse(result)
        self.assertEqual(calls, [("release_read_slot", False)])


if __name__ == "__main__":
    unittest.main()
