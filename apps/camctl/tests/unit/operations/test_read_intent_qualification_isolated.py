"""隔离业务数据库，核对读取意图的动作资格。"""

import ast
import asyncio
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from camctl.history.validators import EventValidationError
from camctl.persistence.repositories.validation import build_business_rules

sys.dont_write_bytecode = True
SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "persistence" / "repositories" / "operations.py"


class EligibleReadReached(Exception):
    pass


@lru_cache(maxsize=None)
def source_tree(source):
    return ast.parse(source.read_text())


def extract(name, namespace, source=SOURCE):
    tree = source_tree(source)
    node = next(node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[name]


def batch_connection(source):
    """为设备副本提供由原动作持有的批次；主机副本没有设备。"""
    class Cursor:
        def __init__(self, rows):
            self.rows = rows
        def fetchone(self):
            return self.rows[0] if self.rows else None
        def fetchall(self):
            return self.rows
    class Connection:
        def execute(self, sql, args=()):
            if "SELECT a.device_id FROM file_copies" in sql:
                return Cursor([("camera",)] if source == "device" else [])
            if "SELECT 1 FROM file_copies c JOIN operation_runs" in sql:
                return Cursor([])
            if "SELECT format_version" in sql:
                return Cursor([(9,)])
            if "FROM device_file_batches" in sql:
                return Cursor([(1, 7)])
            raise AssertionError(sql)
    return Connection()


class ReadPlanTests(unittest.TestCase):
    def test_original_intent_replay_is_consumed_before_new_qualification(self):
        namespace = {"_saved_transaction_events": lambda connection, key: [{"saved": True}],
                     "AttemptIntent": type("AttemptIntent", (), {"kind": "READ_FILE"}),
                     "OperationKind": SimpleNamespace(CHECK_DEVICE_AVAILABILITY="CHECK"), "TransactionError": ValueError}
        command_class = extract("BeginAttemptCommand", namespace)
        intent = namespace["AttemptIntent"]()
        command = command_class(intent, "key")
        command._reuse = lambda connection, saved: ("original", saved)
        self.assertEqual(command.plan(SimpleNamespace(connection=None)), ("original", [{"saved": True}]))

    def plan(self, action, parent_table, source):
        class AttemptIntent:
            kind = "READ_FILE"
            action_id = 7
            target = SimpleNamespace(copy_id=9)
            query_purpose = None
            config = SimpleNamespace(timeout_s=1, retry_interval_s=1)
        copy = {"id": 9, "delivery_id": None, "processing_id": None, "inspection_id": None,
                "source_device_file_id": 15 if source == "device" else None}
        copy[{"deliveries": "delivery_id", "recording_processing": "processing_id",
              "segment_inspections": "inspection_id"}[parent_table]] = 8
        run = {"id": 11, "action_id": 7, "copy_id": 9, "attempts_used": 1, "retry_wait_required": 1}
        class Plan:
            def __init__(self, **values): self.__dict__.update(values)
        def load_context(connection, kind, action_id, target, purpose, state):
            state["file_copies"] = {9: copy}
            state[parent_table] = {8: {"action_id": 7}}
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "AttemptIntent": AttemptIntent, "OperationKind": SimpleNamespace(READ_FILE="READ_FILE",
                PREPARE_CAPTURE="PREPARE_CAPTURE", START="START", RESOLVE_OUTPUT_SCOPE="SCOPE",
                CHECK_CAPTURE_RESULTS="RESULTS", CHECK_DEVICE_AVAILABILITY="CHECK"), "TransactionError": ValueError, "ConsistencyError": ValueError,
            "_saved_transaction_events": lambda connection, key: None, "responsibility_key": lambda intent: "read/9",
            "_load_row": lambda connection, table, identity: action if table == "actions" else run,
            "_load_flow_context": load_context, "intent_recovery_locked": lambda connection, intent: False,
            "_verify_obtain_config": lambda *args: None, "_intent_identity": lambda intent: {"kind": 3, "copy_id": 9},
            "_find_responsibility": lambda connection, intent: 11, "CommandPlan": Plan,
            "_load_read_attempt_context": lambda *args: (_ for _ in ()).throw(EligibleReadReached()),
            "BeginAttemptResult": lambda disposition, reason: SimpleNamespace(disposition=disposition, reason=reason),
            "BeginDisposition": SimpleNamespace(REJECTED="rejected")}
        command_class = extract("BeginAttemptCommand", namespace)
        command = command_class(AttemptIntent(), "key")
        def verify_existing(current, intent, connection):
            self.assertEqual(current["action_id"], intent.action_id)
            self.assertEqual(command._state[parent_table][8]["action_id"], intent.action_id)
        command._verify_existing_run = verify_existing
        action["type"] = 4 if parent_table == "deliveries" else 2
        scope = SimpleNamespace(connection=batch_connection(source), allocate=lambda count: self.fail("不合格动作不能分配事件"))
        result = command.plan(scope)
        return result, run

    def test_cancel_after_device_grant_does_not_consume_attempt(self):
        result, run = self.plan({"id": 7, "status": 2, "cancel_requested": 1}, "deliveries", "device")
        self.assertTrue(result.read_only)
        self.assertEqual(result.result.reason, "owner_not_running")
        self.assertEqual((run["attempts_used"], run["retry_wait_required"]), (1, 1))

    def test_cancelled_host_read_does_not_consume_attempt(self):
        result, run = self.plan({"id": 7, "status": 2, "cancel_requested": 1}, "deliveries", "host")
        self.assertTrue(result.read_only)
        self.assertEqual(result.result.reason, "owner_not_running")
        self.assertEqual((run["attempts_used"], run["retry_wait_required"]), (1, 1))

    def test_terminal_capture_owner_does_not_start_internal_read(self):
        result, _ = self.plan({"id": 7, "status": 3, "cancel_requested": 0}, "recording_processing", "device")
        self.assertEqual(result.result.reason, "owner_not_running")

    def test_cancelled_inspection_owner_does_not_start_internal_read(self):
        result, _ = self.plan({"id": 7, "status": 2, "cancel_requested": 1}, "segment_inspections", "device")
        self.assertEqual(result.result.reason, "owner_not_running")

    def test_active_host_and_device_continue_to_source_checks(self):
        for source in ("host", "device"):
            with self.subTest(source=source):
                with self.assertRaises(EligibleReadReached):
                    self.plan({"id": 7, "status": 2, "cancel_requested": 0}, "deliveries", source)


class ReadGuardTests(unittest.TestCase):
    def check(self, owner_id, run_action_id, status, canceled, source, parent_table,
              *, hasselblad=False, preparation_held=False, capture_held=False):
        from camctl.persistence.repositories.preparation import PreparationReads
        from camctl.persistence.repositories import physical_alias, capture_selection
        from camctl.scheduling import resources as scheduling
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        copy = {"id": 9, "delivery_id": None, "processing_id": None, "inspection_id": None,
                "slot_device_id": "camera" if source == "device" else None,
                "source_device_file_id": 15 if source == "device" else None,
                "source_intermediate_file_id": 19 if source == "host" else None, "round": 1}
        copy[{"deliveries": "delivery_id", "recording_processing": "processing_id",
              "segment_inspections": "inspection_id"}[parent_table]] = 8
        action = {"id": owner_id, "status": status, "cancel_requested": canceled}
        state = {"file_copies": {9: copy}, parent_table: {8: {"action_id": owner_id}},
                 "actions": {owner_id: action, 16: {"device_id": "camera", "driver_id": DRIVER_ID if hasselblad else "other"}},
                 "device_files": {15: {"observer_action_id": 16, "source_action_id": 16}},
                 "intermediate_files": {19: {}}}
        attempt = SimpleNamespace(table="operation_attempts", before=SimpleNamespace(exists=False),
            after=SimpleNamespace(values={"run_id": 11, "intent_event_id": 20, "result_event_id": None, "result_json": None,
                "error_json": None, "status": 1, "effect_state": 1, "attempt_no": 1, "copy_round": 1}))
        run_row = SimpleNamespace(table="operation_runs", row_id=11, before=SimpleNamespace(exists=False),
            after=SimpleNamespace(values={"id": 11, "kind": 3, "copy_id": 9,
                                          "action_id": run_action_id, "attempts_used": 1}))
        event = SimpleNamespace(event_id=20, rows=(attempt, run_row), evidence={}, occurred_at=10)
        context = SimpleNamespace(state_rows=state, preparation_reads=PreparationReads(None), obtain_check_reads=None,
                                  transaction=SimpleNamespace(first_event_id=20))
        guard = build_business_rules()["attempt_intent"]
        from camctl.persistence.repositories.file_batch_reads import FileBatchReads
        context.file_batch_reads = FileBatchReads(batch_connection(source))
        action["type"] = 4 if parent_table == "deliveries" else 2
        with ExitStack() as stack:
            stack.enter_context(patch.object(PreparationReads, "device_start_in_flight", return_value=False))
            stack.enter_context(patch.object(PreparationReads, "availability_in_flight", return_value=False))
            stack.enter_context(patch.object(PreparationReads, "capture_holds_device", return_value=capture_held))
            stack.enter_context(patch.object(physical_alias, "source_physical_alias_conflict", return_value=False))
            stack.enter_context(patch.object(physical_alias, "source_physical_identity", return_value="identity"))
            stack.enter_context(patch.object(capture_selection.CaptureSelectionInput, "from_document", return_value=None))
            stack.enter_context(patch.object(capture_selection, "read_capture_priority", return_value=None))
            stack.enter_context(patch.object(scheduling, "current_preparation_holder", return_value=object() if preparation_held else None))
            guard(event, context)

    def test_device_and_host_eligible_owners_remain_accepted(self):
        for source in ("device", "host"):
            with self.subTest(source=source):
                self.check(7, 7, 2, 0, source, "deliveries")

    def test_cancellation_is_rejected_for_device_and_host(self):
        for source in ("device", "host"):
            with self.subTest(source=source):
                with self.assertRaises(EventValidationError):
                    self.check(7, 7, 2, 1, source, "deliveries")

    def test_internal_read_uses_processing_owner(self):
        with self.assertRaises(EventValidationError):
            self.check(7, 7, 3, 0, "device", "recording_processing")

    def test_active_internal_processing_and_inspection_are_accepted(self):
        for parent in ("recording_processing", "segment_inspections"):
            with self.subTest(parent=parent):
                self.check(7, 7, 2, 0, "device", parent)

    def test_trigger_action_cannot_replace_the_copy_owner(self):
        with self.assertRaises(EventValidationError):
            self.check(8, 7, 2, 0, "host", "deliveries")

    def test_hasselblad_read_does_not_pass_capture_or_preparation(self):
        for reason in ("preparation_held", "capture_held"):
            with self.subTest(reason=reason), self.assertRaises(EventValidationError):
                self.check(7, 7, 2, 0, "device", "deliveries", hasselblad=True, **{reason: True})
        self.check(7, 7, 2, 0, "device", "deliveries", hasselblad=True)


class ReadConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_running_ticket_resumes_even_on_last_allowed_attempt(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        for used in (1, 3):
            with self.subTest(attempts_used=used):
                ticket = SimpleNamespace(run_id=11, attempt_id=used, target_id="9")
                calls = []
                resume_port = object()
                async def save(runtime, copy_id, operation, request, repository, *, retry):
                    calls.append((copy_id, operation, request.ticket, repository, retry))
                    return SimpleNamespace(value=SimpleNamespace(disposition="resumed"))
                class Database:
                    async def read(self, callback): return callback(None)
                runtime = SimpleNamespace(database=Database(), pending_read_business={},
                    continuing_read_tickets={}, last_recovery_diagnostic="old",
                    operations=SimpleNamespace(resume_read=resume_port,
                        begin_attempt=lambda *_: self.fail("原票据续传不能增加尝试")))
                intent = SimpleNamespace(target=SimpleNamespace(copy_id=9),
                    config=SimpleNamespace(max_attempts=3), occurred_at=10)
                namespace = {"running_read_ticket": lambda *_: (ticket, 5),
                    "read_host_boundary": lambda *_: True, "save_read_business": save,
                    "ReadResumeRequest": lambda original, config, at: SimpleNamespace(ticket=original, config=config, at=at),
                    "ReadResumeDisposition": SimpleNamespace(NOT_RUNNING="not_running")}
                acquire = extract("acquire_read_attempt", namespace, source)
                self.assertEqual(await acquire(runtime, intent), (ticket, None))
                self.assertEqual(calls, [(9, "read_resume", ticket, resume_port, True)])
                self.assertEqual(runtime.continuing_read_tickets, {})

    async def test_rejected_intent_does_not_produce_ticket_or_continuation(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        async def save(runtime, copy_id, operation, intent, repository, *, on_completed, retry):
            result = SimpleNamespace(disposition="rejected", reason="owner_not_running")
            on_completed(result)
            return SimpleNamespace(value=result)
        class Database:
            async def read(self, operation): return operation(None)
        runtime = SimpleNamespace(database=Database(), pending_read_business={}, continuing_read_tickets={},
                                  operations=SimpleNamespace(begin_attempt=None))
        intent = SimpleNamespace(target=SimpleNamespace(copy_id=9))
        namespace = {"running_read_ticket": lambda owned, copy_id: None,
                     "save_read_business": save, "BeginDisposition": SimpleNamespace(GRANTED="granted"), "replace": replace}
        acquire = extract("acquire_read_attempt", namespace, source)
        ticket, reason = await acquire(runtime, intent)
        self.assertIsNone(ticket)
        self.assertEqual(reason, "owner_not_running")
        self.assertEqual(runtime.continuing_read_tickets, {})

    async def test_reconciled_original_intent_marks_its_continuation_for_inspection_owner(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        continuation = extract("ReadContinuation", {"dataclass": dataclass}, source)
        ticket = SimpleNamespace(run_id=11, attempt_id=1, target_id="9")
        pending = SimpleNamespace(inspection_cancel_recovery=[True])
        async def save(runtime, copy_id, operation, intent, repository, *, on_completed, retry):
            result = SimpleNamespace(disposition="granted", ticket=ticket)
            on_completed(result)
            return SimpleNamespace(value=result)
        runtime = SimpleNamespace(database=None, pending_read_business={(9, "read_intent"): pending},
                                  continuing_read_tickets={}, operations=SimpleNamespace(begin_attempt=None))
        intent = SimpleNamespace(target=SimpleNamespace(copy_id=9))
        namespace = {"save_read_business": save, "BeginDisposition": SimpleNamespace(GRANTED="granted"),
                     "ReadContinuation": continuation, "replace": replace}
        acquire = extract("acquire_read_attempt", namespace, source)
        recovered, reason = await acquire(runtime, intent)
        self.assertIs(recovered, ticket)
        self.assertIsNone(reason)
        self.assertTrue(runtime.continuing_read_tickets[9].never_dispatched)
        self.assertTrue(runtime.continuing_read_tickets[9].inspection_cancel_recovery)

    async def test_waiter_cancellation_after_grant_preserves_unstarted_recovery_owner(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        continuation = extract("ReadContinuation", {"dataclass": dataclass}, source)
        ticket = SimpleNamespace(run_id=11, attempt_id=1, target_id="9")
        async def save(runtime, copy_id, operation, intent, repository, *, on_completed, retry):
            on_completed(SimpleNamespace(disposition="granted", ticket=ticket))
            raise asyncio.CancelledError()
        class Database:
            async def read(self, operation): return operation(None)
        runtime = SimpleNamespace(database=Database(), pending_read_business={}, continuing_read_tickets={},
                                  operations=SimpleNamespace(begin_attempt=None))
        intent = SimpleNamespace(target=SimpleNamespace(copy_id=9))
        namespace = {"running_read_ticket": lambda owned, copy_id: None, "save_read_business": save,
                     "BeginDisposition": SimpleNamespace(GRANTED="granted"),
                     "ReadContinuation": continuation, "replace": replace}
        acquire = extract("acquire_read_attempt", namespace, source)
        with self.assertRaises(asyncio.CancelledError):
            await acquire(runtime, intent)
        saved = runtime.continuing_read_tickets[9]
        self.assertIs(saved.ticket, ticket)
        self.assertTrue(saved.never_dispatched)
        self.assertTrue(saved.inspection_cancel_recovery)

    async def test_recovered_ticket_records_not_dispatched_without_device_effect(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        continuation = extract("ReadContinuation", {"dataclass": dataclass}, source)
        ticket = SimpleNamespace(run_id=11, attempt_id=1, target_id="9")
        runtime = SimpleNamespace(continuing_read_tickets={9: continuation(ticket, True, inspection_cancel_recovery=True)},
                                  pending_read_ends={}, occurred_at=lambda: 10)
        evidence_module = ModuleType("camctl.devices.evidence")
        evidence_module.EvidenceContract = lambda *args: args
        evidence = SimpleNamespace(with_contract=lambda contract: contract)
        namespace = {"AttemptStatus": SimpleNamespace(FAILED="failed"), "EffectState": SimpleNamespace(NO_EFFECT="no_effect"),
            "SettlementBasis": SimpleNamespace(NOT_DISPATCHED="not_dispatched"),
            "RunOutcome": SimpleNamespace(CANCELED="canceled"), "ErrorValue": lambda code, stage: (code, stage),
            "CallOutcome": lambda **values: SimpleNamespace(**values),
            "Settlement": lambda basis, value: SimpleNamespace(basis=basis, evidence=value),
            "EvidenceValue": lambda *args: args, "RunFinish": lambda status: SimpleNamespace(status=status),
            "AttemptFinish": lambda ticket, outcome, occurred_at, **values: SimpleNamespace(
                ticket=ticket, outcome=outcome, occurred_at=occurred_at, **values),
            "validate_outcome": lambda ticket, outcome, evidence: outcome,
            "hold_read_result": lambda runtime, finish, key: finish}
        prevented = extract("prevented_read_result", namespace, source)
        with patch.dict(sys.modules, {evidence_module.__name__: evidence_module}):
            result = prevented(runtime, ticket, SimpleNamespace(terminal=False, canceled=True,
                               reason="canceled_before_dispatch"), evidence)
        self.assertIs(result.ticket, ticket)
        self.assertEqual(result.outcome.effect, "no_effect")
        self.assertEqual(result.outcome.settlement.basis, "not_dispatched")
        self.assertEqual(result.run_finish.status, "canceled")
        self.assertFalse(result.retry_wait)

    async def test_cold_recovered_read_waits_without_forging_not_dispatched(self):
        source = SOURCE.parents[2] / "outputs" / "read_attempts.py"
        ticket = SimpleNamespace(run_id=11, attempt_id=1, target_id="9")
        runtime = SimpleNamespace(continuing_read_tickets={}, pending_read_ends={})
        evidence_module = ModuleType("camctl.devices.evidence")
        evidence_module.EvidenceContract = object
        prevented = extract("prevented_read_result", {}, source)
        with patch.dict(sys.modules, {evidence_module.__name__: evidence_module}):
            result = prevented(runtime, ticket, SimpleNamespace(terminal=False, canceled=False,
                               reason="capture_priority"), object())
        self.assertIsNone(result)
        self.assertEqual(runtime.continuing_read_tickets, {})


if __name__ == "__main__":
    unittest.main()
