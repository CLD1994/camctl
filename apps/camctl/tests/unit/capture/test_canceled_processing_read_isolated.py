"""取消录像内部读取的原事实、机会和文件拥有者；全部使用内存替身。"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from camctl.capture import canceled_internal_reads, handlers
from camctl.capture.recovery import RecoveryBoundary
from camctl.history.events import load_event_registry, reason_for_branch

class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def close(self): pass


class ProcessingCancelTests(unittest.TestCase):
    def test_cancel_handler_waits_for_original_processing_before_terminal(self):
        async def run(ready):
            calls = []
            async def action(action_id):
                return {"id": action_id, "type": 2, "execution_started": 1, "execution_spec_json": {}}
            async def settle(scope, action_id):
                self.assertIs(scope.pending_read_results, runtime.pending_read_results)
                self.assertEqual(action_id, 4)
                calls.append("read_settlement")
                return ready
            async def save(request):
                self.assertEqual((request.action_id, request.occurred_at), (4, 20))
                calls.append("cancel_finish")
            runtime = SimpleNamespace(pending_start_results={}, pending_baselines={}, action=action,
                save_capture_completion=save, wall_us=lambda: 20, database=object(), operations=object(),
                pending_read_results={}, pending_read_business={}, pending_read_ends={},
                continuing_read_tickets={}, pending_segment_inspection_results={}, file_executor=None,
                evidence=None, recovery_boundary=RecoveryBoundary.UNCONFIRMED, recovery_max_event_id=None,
                recovery_evidence_for=None, roots=None, on_recovery_diagnostic=None,
                last_recovery_diagnostic=None)
            with patch.object(canceled_internal_reads, "settle_canceled_processing_read", settle):
                await handlers._finish_canceled_capture(runtime, 4)
            return calls
        self.assertEqual(asyncio.run(run(False)), ["read_settlement"])
        self.assertEqual(asyncio.run(run(True)), ["read_settlement", "cancel_finish"])

    def test_release_transaction_waits_for_other_original_call(self):
        from camctl.outputs.slots import SlotOutcome, SlotRequest
        from camctl.persistence.repositories.outputs import OutputsRepository

        class Connection:
            unsettled = True
            def execute(self, sql, args):
                if "history_transactions WHERE operation_key" in sql: return Cursor([])
                if "SELECT id FROM operation_runs WHERE responsibility_key" in sql: return Cursor([(7,)])
                if "SELECT id FROM operation_attempts WHERE run_id" in sql: return Cursor([])
                if "SELECT 1 FROM operation_attempts t JOIN operation_runs" in sql:
                    return Cursor([(1,)] if self.unsettled else [])
                table = sql.split("FROM ")[1].split()[0]
                rows = {
                    ("file_copies", 9): {"id": 9, "delivery_id": None, "processing_id": 3,
                        "source_device_file_id": 15, "slot_device_id": "camera",
                        "committed_bytes": 10, "source_size": 10},
                    ("recording_processing", 3): {"id": 3, "action_id": 4},
                    ("device_files", 15): {"id": 15, "observer_action_id": 5, "source_action_id": 6},
                    ("actions", 4): {"id": 4, "status": 2, "cancel_requested": 1},
                    ("actions", 5): {"id": 5, "device_id": "camera", "driver_id": "driver"},
                    ("actions", 6): {"id": 6, "device_id": "camera", "driver_id": "driver"},
                    ("operation_runs", 7): {"id": 7, "status": 4, "retry_wait_required": 0,
                        "error_json": '{"code":"device_binding_unavailable"}'}}
                values = rows[(table, args[0])]
                cursor = Cursor([tuple(values.values())])
                cursor.description = [(key,) for key in values]
                return cursor
        class Storage:
            def commit_operation(self, command, key, owned):
                self.plan = command.plan(SimpleNamespace(connection=owned.connection,
                    allocate=lambda count: SimpleNamespace(first_event_id=200, txn_id=3)))
                return SimpleNamespace(kind="completed", result=self.plan.result)
        connection = Connection()
        storage = Storage()
        repository = OutputsRepository(storage=storage)
        with patch("camctl.persistence.repositories.binding_read_cancel.load_saved_binding_read_failure"):
            outcome = repository.release_read_slot(SlotRequest(9, 20), "key", SimpleNamespace(connection=connection))
            self.assertEqual((outcome.value.outcome, outcome.value.reason), (SlotOutcome.WAIT, "action_active"))
            self.assertTrue(storage.plan.read_only)
            connection.unsettled = False
            outcome = repository.release_read_slot(SlotRequest(9, 20), "key", SimpleNamespace(connection=connection))
            self.assertEqual(outcome.value.outcome, SlotOutcome.RELEASED)
            self.assertEqual(storage.plan.events[0].rows[0].after.values, {"slot_device_id": None})

    def test_release_guard_independently_requires_all_original_calls_settled(self):
        from camctl.history.validators import EventValidationError
        from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
        from camctl.persistence.repositories.validation import build_business_rules

        class Connection:
            unsettled = False
            def execute(self, sql, args):
                return Cursor([(1,)] if self.unsettled else [])

        connection = Connection()
        reads = RetryWaitReads(connection)
        copy = {"id": 9, "processing_id": 3, "source_device_file_id": 15,
                "committed_bytes": 10, "source_size": 10}
        run = {"id": 7, "copy_id": 9, "kind": 3, "status": 4, "retry_wait_required": 0,
               "error_json": {"code": "device_binding_unavailable"}}
        context = SimpleNamespace(retry_wait_reads=reads, validated_events=(), state_rows={
            "file_copies": {9: copy}, "operation_runs": {7: run},
            "device_files": {15: {"observer_action_id": 5, "source_action_id": 6}},
            "recording_processing": {3: {"action_id": 4}},
            "actions": {4: {"id": 4, "status": 2, "cancel_requested": 1},
                        5: {"device_id": "camera", "driver_id": "driver"},
                        6: {"device_id": "camera", "driver_id": "driver"}}})
        row = SimpleNamespace(table="file_copies", row_id=9,
            before=SimpleNamespace(exists=True, values={"slot_device_id": "camera"}),
            after=SimpleNamespace(values={"slot_device_id": None}))
        event = SimpleNamespace(event_type=load_event_registry()["events"]["COPY_CHANGED"]["id"],
                                reason=reason_for_branch("COPY_CHANGED", "SLOT"), rows=(row,))
        guard = build_business_rules()["read_slot"]
        with patch.object(RetryWaitReads, "no_unsettled_attempts", return_value=True), \
             patch("camctl.persistence.repositories.binding_read_cancel.load_saved_binding_read_failure"):
            guard(event, context)
            connection.unsettled = True
            with self.assertRaises(EventValidationError):
                guard(event, context)

    def test_terminal_recovery_pages_without_device_assembly(self):
        async def run():
            calls, notifications = [], []
            class Database:
                async def read(self, callback):
                    connection = SimpleNamespace(execute=lambda sql, params: SimpleNamespace(fetchall=lambda: [(i,) for i in range(1, 129)]))
                    return callback(SimpleNamespace(connection=connection))
            async def settle(scope, action_id): calls.append((scope, action_id))
            shared = {}
            deps = SimpleNamespace(capture_canceled_read_after=0, capture_read_results={}, capture_read_business={},
                capture_read_ends={}, capture_continuing_reads={}, capture_segment_inspection_results=shared,
                work_files=SimpleNamespace(executor=object()))
            context = SimpleNamespace(database=Database(), clock=SimpleNamespace(utc_micros=lambda: 20),
                wake=SimpleNamespace(mark_changed=lambda reason: notifications.append(reason)))
            from camctl.bootstrap import lifecycle
            from camctl.scheduling.notifications import WakeReason
            with patch.object(canceled_internal_reads, "settle_canceled_processing_read", settle):
                await lifecycle._resume_canceled_internal_reads(deps, context)
            self.assertEqual(deps.capture_canceled_read_after, 128)
            self.assertEqual(notifications, [WakeReason.ACTIVITY_FINISHED])
            self.assertEqual(len(calls), 128)
            self.assertIs(calls[0][0].pending_segment_inspection_results, shared)
        asyncio.run(run())

    def test_release_requires_original_failed_read_and_all_calls_settled(self):
        from camctl.history.validators import EventValidationError
        from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
        from camctl.persistence.repositories.validation import build_business_rules

        class Connection:
            unsettled = False
            def execute(self, sql, args):
                return Cursor([(1,)] if self.unsettled else [])
        connection = Connection()
        reads = RetryWaitReads(connection)
        error = {"code": "device_binding_unavailable"}
        action = {"id": 4, "status": 2, "cancel_requested": 1}
        context = SimpleNamespace(retry_wait_reads=reads, validated_events=(), state_rows={
            "file_copies": {9: {"id": 9, "processing_id": 3, "source_device_file_id": 15,
                "committed_bytes": 10, "source_size": 10}},
            "operation_runs": {7: {"id": 7, "copy_id": 9, "kind": 3, "status": 4,
                "retry_wait_required": 0, "error_json": error}},
            "device_files": {15: {"observer_action_id": 5, "source_action_id": 6}},
            "recording_processing": {3: {"action_id": 4}},
            "actions": {4: action, 5: {"device_id": "camera", "driver_id": "driver"},
                        6: {"device_id": "camera", "driver_id": "driver"}}})
        row = SimpleNamespace(table="file_copies", row_id=9,
            before=SimpleNamespace(exists=True, values={"slot_device_id": "camera"}),
            after=SimpleNamespace(values={"slot_device_id": None}))
        event = SimpleNamespace(event_type=load_event_registry()["events"]["COPY_CHANGED"]["id"],
                                reason=reason_for_branch("COPY_CHANGED", "SLOT"), rows=(row,))
        guard = build_business_rules()["read_slot"]
        with patch.object(RetryWaitReads, "no_unsettled_attempts", return_value=True), \
             patch("camctl.persistence.repositories.binding_read_cancel.load_saved_binding_read_failure") as saved:
            guard(event, context)
            saved.assert_called_once_with(connection, 4, 9, error)
            connection.unsettled = True
            with self.assertRaises(EventValidationError): guard(event, context)
            action["cancel_requested"] = 0
            saved.reset_mock()
            guard(event, context)
            saved.assert_not_called()

    def test_file_ownership_and_original_key_precede_release(self):
        from camctl.outputs.slots import SlotOutcome
        from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads

        async def run(*, ready=True, busy=False, pending_result=False, pending_business=False, changed=False):
            trace = []
            class Connection:
                def execute(self, sql, args):
                    if "SELECT c.id,ob.device_id" in sql:
                        return Cursor([])
                    if "SELECT c.id,c.target_file_id" in sql:
                        return Cursor([(9, 20, "camera", 7)])
                    table = sql.split("FROM ")[1].split()[0]
                    values = {"file_copies": {"id": 9, "processing_id": 3},
                              "operation_runs": {"id": 7, "status": 5},
                              "actions": {"id": 4, "status": 2, "cancel_requested": 1}}[table]
                    cursor = Cursor([tuple(values.values())])
                    cursor.description = [(key,) for key in values]
                    return cursor
            class Database:
                async def read(self, callback): return callback(SimpleNamespace(connection=Connection()))
            executor = SimpleNamespace(unfinished_files=lambda: (20,) if busy else ())
            result = SimpleNamespace(ticket=SimpleNamespace(target_id="9"))
            flow = SimpleNamespace(database=Database(), file_executor=executor, occurred_at=lambda: 25,
                pending_read_business={(4, "capture_binding_failure"): object()} if pending_business else {},
                pending_read_results={(7, 1): result} if pending_result else {})
            availability = [ready]
            async def owned(runtime, copy_id, callback):
                trace.append("owner")
                if changed: availability[0] = False
                return await callback(None)
            async def save(runtime, copy_id, stage, request, method, **kwargs):
                trace.append(("release", copy_id, request.occurred_at))
                return SimpleNamespace(value=SimpleNamespace(outcome=SlotOutcome.RELEASED))
            with patch.object(RetryWaitReads, "no_unsettled_attempts", side_effect=lambda *args: availability[0]), \
                 patch.object(canceled_internal_reads, "run_owned_read", owned), \
                 patch.object(canceled_internal_reads, "save_read_business", save):
                result = await canceled_internal_reads.settle_canceled_processing_read(flow, 4)
            return result, trace
        self.assertEqual(asyncio.run(run()), (True, ["owner", ("release", 9, 25)]))
        for values in ({"ready": False}, {"busy": True}, {"pending_result": True}, {"pending_business": True}):
            with self.subTest(values=values): self.assertEqual(asyncio.run(run(**values)), (False, []))
        self.assertEqual(asyncio.run(run(changed=True)), (False, ["owner"]))

    def test_slot_unknown_retries_existing_key_even_if_already_released(self):
        from camctl.outputs.slots import SlotOutcome
        from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads

        async def run():
            prior = SimpleNamespace(key="original-release-key", request=object())
            pending = {(9, "release_read_slot"): prior}
            class Connection:
                def execute(self, sql, args):
                    if "SELECT c.id,ob.device_id" in sql: return Cursor([])
                    if "SELECT c.id,c.target_file_id" in sql: return Cursor([(9, 20, None, 7)])
                    table = sql.split("FROM ")[1].split()[0]
                    values = {"file_copies": {"id": 9, "processing_id": 3},
                              "operation_runs": {"id": 7, "status": 5},
                              "actions": {"id": 4, "status": 2, "cancel_requested": 1}}[table]
                    cursor = Cursor([tuple(values.values())])
                    cursor.description = [(key,) for key in values]
                    return cursor
            class Database:
                async def read(self, callback): return callback(SimpleNamespace(connection=Connection()))
            flow = SimpleNamespace(database=Database(), file_executor=SimpleNamespace(unfinished_files=lambda: ()),
                occurred_at=lambda: 25, pending_read_business=pending, pending_read_results={})
            calls = []
            async def owned(runtime, copy_id, callback): return await callback(None)
            async def save(runtime, copy_id, stage, request, method, **kwargs):
                actual = runtime.pending_read_business[(copy_id, stage)]
                calls.append((actual.key, actual.request, kwargs["retry"]))
                del runtime.pending_read_business[(copy_id, stage)]
                return SimpleNamespace(value=SimpleNamespace(outcome=SlotOutcome.ALREADY_RELEASED))
            with patch.object(RetryWaitReads, "no_unsettled_attempts", return_value=True), \
                 patch.object(canceled_internal_reads, "run_owned_read", owned), \
                 patch.object(canceled_internal_reads, "save_read_business", save):
                self.assertTrue(await canceled_internal_reads.settle_canceled_processing_read(flow, 4))
            self.assertEqual(calls, [("original-release-key", prior.request, True)])
        asyncio.run(run())


if __name__ == "__main__": unittest.main()
