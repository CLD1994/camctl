"""未派发检查读取的取消、原结果收场与读取机会保护。"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camctl.capture import media_flow, segment_inspections
from camctl.contracts.enums import enum_for
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError
from camctl.outputs import read_attempts
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.business import SharedSave
from camctl.persistence.repositories import operations, outputs
from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
from camctl.persistence.repositories.transactions import TransactionScope


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class ReleaseTransactionTests(unittest.TestCase):
    def release(self, *, canceled=1, inspection_state=4, attempts=0, active=False, waiting=0, saved=None):
        copy = {"id": 9, "inspection_id": 8, "delivery_id": None, "processing_id": None,
                "source_device_file_id": 15, "slot_device_id": "camera", "committed_bytes": 0, "source_size": 10}
        run = {"id": 11, "kind": 3, "copy_id": 9, "action_id": 7, "status": 1,
               "attempts_used": 0, "retry_wait_required": waiting, "error_json": None}
        facts = {("file_copies", 9): copy, ("segment_inspections", 8): {"id": 8, "state": inspection_state, "action_id": 7},
                 ("device_files", 15): {"id": 15, "observer_action_id": 7, "source_action_id": 7},
                 ("actions", 7): {"id": 7, "device_id": "camera", "driver_id": "driver", "cancel_requested": canceled},
                 ("operation_runs", 11): run, ("operation_attempts", 12): {"id": 12, "status": 1}}
        class Connection:
            def execute(self, query, params):
                if "responsibility_key" in query: return Cursor([(11,)])
                if "SELECT 1 FROM operation_attempts" in query: return Cursor([(1,)] if attempts else [])
                if "SELECT id FROM operation_attempts" in query: return Cursor([(12,)] if active else [])
                raise AssertionError(query)
        connection = Connection()
        class Storage:
            def commit_operation(self, command, key, owned):
                self.plan = command.plan(TransactionScope(connection, 2, 19))
                return SimpleNamespace(kind="completed", result=self.plan.result)
        storage = Storage()
        with patch.object(outputs, "row_facts", side_effect=lambda conn, table, identity: facts.get((table, identity))), \
             patch.object(outputs, "saved_transaction_events", return_value=saved):
            outcome = outputs.OutputsRepository(storage=storage).release_read_slot(
                SlotRequest(9, 10), "original-operation", SimpleNamespace(connection=connection))
        return outcome.value, storage.plan, run

    def test_cancelled_inspection_closes_run_and_slot_together(self):
        result, plan, run = self.release()
        self.assertEqual(result.outcome, SlotOutcome.RELEASED)
        events = load_event_registry()["events"]
        self.assertEqual([(event.event_type, event.reason) for event in plan.events], [
            (events["OPERATION_CONFIGURED"]["id"], events["OPERATION_CONFIGURED"]["branches"]["FINISH"]["reason"]),
            (events["COPY_CHANGED"]["id"], events["COPY_CHANGED"]["branches"]["SLOT"]["reason"]),
        ])
        self.assertEqual(plan.events[0].rows[0].after.values["status"], int(enum_for("operation_runs.status").CANCELED))
        self.assertIsNone(plan.events[1].rows[0].after.values["slot_device_id"])
        self.assertEqual(plan.events[0].transaction_id, plan.events[1].transaction_id)
        self.assertEqual(run["attempts_used"], 0)

    def test_not_cancelled_or_pending_inspection_keeps_slot(self):
        for values in ({"canceled": 0}, {"inspection_state": 1}):
            with self.subTest(values=values):
                result, plan, _ = self.release(**values)
                self.assertEqual(result.reason, "still_reading")
                self.assertTrue(plan.read_only)

    def test_existing_attempt_cannot_be_treated_as_unstarted(self):
        result, plan, _ = self.release(attempts=1, active=True)
        self.assertEqual(result.reason, "read_active")
        self.assertTrue(plan.read_only)

    def test_waiting_retry_is_not_cleared(self):
        result, plan, _ = self.release(waiting=1)
        self.assertEqual(result.reason, "retry_wait")
        self.assertTrue(plan.read_only)

    def test_replay_requires_both_original_events(self):
        registry = load_event_registry()["events"]
        events = [{"type": registry["OPERATION_CONFIGURED"]["id"], "reason": registry["OPERATION_CONFIGURED"]["branches"]["FINISH"]["reason"], "occurred_at": 10, "body": {"rows": [{
            "table": "operation_runs", "id": 11, "before": {"exists": True, "values": {"status": 1}},
            "after": {"exists": True, "values": {"status": 5}}}]}},
            {"type": registry["COPY_CHANGED"]["id"], "reason": registry["COPY_CHANGED"]["branches"]["SLOT"]["reason"], "occurred_at": 10, "body": {"rows": [{
            "table": "file_copies", "id": 9, "before": {"exists": True, "values": {"slot_device_id": "camera"}},
            "after": {"exists": True, "values": {"slot_device_id": None}}}]}}]
        result, plan, _ = self.release(saved=events)
        self.assertEqual(result.outcome, SlotOutcome.RELEASED)
        self.assertTrue(plan.read_only)
        events[0]["body"]["rows"][0]["id"] = 99
        with self.assertRaises(ValueError): self.release(saved=events)


class FinishGuardTests(unittest.TestCase):
    def guard(self, *, canceled=1, attempts=0, release=True, inspection_state=4, waiting=0, owner_id=7):
        guards = {}
        operations.register_operation_guards(guards.__setitem__)
        run = {"kind": 3, "status": 1, "retry_wait_required": waiting, "attempts_used": 0,
               "copy_id": 9, "action_id": 7, "error_json": None}
        row = SimpleNamespace(table="operation_runs", row_id=11,
            before=SimpleNamespace(exists=True, values={"status": 1}), after=SimpleNamespace(values={"status": 5}))
        event = SimpleNamespace(rows=(row,), event_type=load_event_registry()["events"]["OPERATION_CONFIGURED"]["id"], reason=3, evidence={})
        context = SimpleNamespace(state_rows={"operation_runs": {11: run},
            "file_copies": {9: {"id": 9, "inspection_id": 8, "slot_device_id": "camera"}},
            "segment_inspections": {8: {"action_id": owner_id, "state": inspection_state}},
            "actions": {7: {"cancel_requested": canceled}}},
            transaction_rows={"file_copies": {9: {"slot_device_id": None if release else "camera"}}},
            retry_wait_reads=RetryWaitReads(object()))
        with patch.object(RetryWaitReads, "no_attempts", return_value=attempts == 0):
            guards["operation_finish"](event, context)

    def test_guard_accepts_cancel_and_atomic_release(self): self.guard()
    def test_guard_rejects_missing_release(self):
        with self.assertRaises(EventValidationError): self.guard(release=False)
    def test_guard_rejects_non_cancelled_owner(self):
        with self.assertRaises(EventValidationError): self.guard(canceled=0)
    def test_guard_rejects_existing_attempt(self):
        with self.assertRaises(EventValidationError): self.guard(attempts=1)
    def test_guard_rejects_unfinished_inspection(self):
        with self.assertRaises(EventValidationError): self.guard(inspection_state=1)
    def test_guard_rejects_existing_wait(self):
        with self.assertRaises(EventValidationError): self.guard(waiting=1)
    def test_guard_rejects_another_inspection_owner(self):
        with self.assertRaises(EventValidationError): self.guard(owner_id=8)


class SharedSaveOwnershipTests(unittest.TestCase):
    def test_only_finished_failed_save_can_be_claimed(self):
        shared = SharedSave()
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
            return SimpleNamespace(value=SimpleNamespace(outcome={"released": SlotOutcome.RELEASED, "wait": SlotOutcome.WAIT}[slot_outcome]))
        async def settle_result(flow, pending, *, retry):
            if existing_result:
                self.assertIs(pending, flow.pending_read_results[(11, 1)])
            else:
                self.assertEqual((pending.ticket.run_id, pending.ticket.attempt_id), (11, 1))
            calls.append(("save_result", retry))
            state["run_status"] = 5
            flow.continuing_read_tickets.pop(9)
            flow.pending_read_results.pop((11, 1), None)
        def prevented(flow, ticket, prevented, evidence):
            self.assertEqual((ticket.run_id, ticket.attempt_id, ticket.target_id), (11, 1, "9"))
            calls.append(("not_dispatched", prevented.reason, prevented.canceled))
            return SimpleNamespace(ticket=ticket)
        async def settle_end(flow, copy_id):
            calls.append(("saved_end", copy_id))
            if end_id is not None:
                state["run_status"] = 5
                state["slot"] = None
        pending = {(9, "read_intent"): SimpleNamespace(request="original", save="original-save",
                    shared=SimpleNamespace(failed=lambda: pending_failed), inspection_cancel_recovery=[])} if pending_intent else {}
        runtime = SimpleNamespace(database=Database(), pending_read_business=pending,
                                  continuing_read_tickets={} if continuation is None else {9: continuation},
                                  pending_read_results={(11, 1): object()} if existing_result else {}, evidence=object())
        with patch.object(read_attempts, "save_read_business", save), \
             patch.object(read_attempts, "prevented_read_result", prevented), \
             patch.object(media_flow, "_save_internal_read_and_settle", settle_result), \
             patch.object(segment_inspections, "_settle_canceled_inspection_end", settle_end):
            result = await segment_inspections.settle_terminal_inspection_read_slot(runtime, 9, 10)
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
        self.assertEqual(calls, [("save_result", True), ("release_read_slot", True)])

    async def test_restart_without_local_continuation_keeps_original_running_ticket(self):
        result, calls = await self.run_settlement(run_status=2)
        self.assertFalse(result)
        self.assertEqual(calls, [("saved_end", 9)])

    async def test_canceled_saved_end_settles_original_even_after_slot_release(self):
        result, calls = await self.run_settlement(run_status=2, end_id=30, slot=None)
        self.assertTrue(result)
        self.assertEqual(calls, [("saved_end", 9)])

    async def test_already_dispatched_ticket_is_not_declared_unstarted(self):
        continuation = SimpleNamespace(ticket=SimpleNamespace(run_id=11, attempt_id=1, target_id="9"),
                                       never_dispatched=False, inspection_cancel_recovery=True)
        result, calls = await self.run_settlement(run_status=2, continuation=continuation)
        self.assertFalse(result)
        self.assertEqual(calls, [("saved_end", 9)])

    async def test_running_original_intent_blocks_release(self):
        result, calls = await self.run_settlement(pending_intent=True, run_status=2)
        self.assertFalse(result)
        self.assertEqual(calls, [("read_intent", True), ("saved_end", 9)])

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
