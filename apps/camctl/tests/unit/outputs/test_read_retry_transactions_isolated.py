"""读取重试事实与流程分支，不加载实际包资源或数据库。"""

import importlib.util
import ast
from enum import Enum, IntEnum
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SOURCE = Path(__file__).parents[3] / "src" / "camctl"


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchmany(self, count):
        return self.rows[:count]

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Connection:
    def __init__(self, *, last=(12, 3, "{}", 101), running=False, event=None, owner=None):
        self.last, self.running, self.event, self.owner = last, running, event, owner

    def execute(self, query, parameters=()):
        if "sqlite_master" in query:
            indexes = {"history_events_row": "CREATE INDEX history_events_row ON history_events(event_type, "
                           "json_extract(body_json, '$.rows[0].table'), json_extract(body_json, '$.rows[0].id'), id)",
                       "operation_attempts_run_status": "CREATE INDEX operation_attempts_run_status ON operation_attempts(run_id, status)",
                       "operation_attempts_without_result": "CREATE INDEX operation_attempts_without_result ON operation_attempts(run_id) WHERE result_json IS NULL"}
            return Cursor([(indexes[parameters[0]],)])
        if "SELECT id,status,result_json,result_event_id" in query:
            return Cursor([] if self.last is None else [self.last])
        if "SELECT MAX(attempt_no)" in query:
            return Cursor([(1,)])
        if "SELECT id FROM operation_attempts" in query:
            return Cursor([(1,)] if self.running else [])
        if "SELECT body_json FROM history_events" in query:
            return Cursor([] if self.event is None else [(json.dumps(self.event),)])
        if "SELECT a.status,a.cancel_requested" in query:
            return Cursor([] if self.owner is None else [self.owner])
        raise AssertionError(query)


def event(reason):
    return {"reason": reason, "evidence": {"attempt_id": 12}, "rows": [{"table": "operation_runs", "id": 7,
        "before": {"exists": True, "values": {"retry_wait_required": 1}},
        "after": {"exists": True, "values": {"retry_wait_required": 0}}}]}


def load_reads():
    class DeliveryStatus(IntEnum):
        PENDING = 1
        PREPARING = 2
        PREPARED = 3
        PUBLISHING = 4
        PUBLISHED = 5
        FAILED = 6
        CANCELED = 7
        WITHDRAWN = 8

    enums = ModuleType("camctl.contracts.enums")
    enums.enum_for = lambda name: {
        "operation_attempts.status": SimpleNamespace(RUNNING=1, FAILED=3, UNKNOWN=4),
        "operation_runs.kind": SimpleNamespace(READ_FILE=3),
        "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2),
        "deliveries.status": DeliveryStatus,
        "device_files.completion_state": SimpleNamespace(COMPLETE=3),
    }[name]
    json_values = ModuleType("camctl.contracts.json_values")
    json_values.parse_exact_json = json.loads
    values = ModuleType("camctl.contracts.values")
    values.ConsistencyError = type("ConsistencyError", (Exception,), {})
    discovery = ModuleType("camctl.outputs.read_discovery")
    discovery.read_copy_dispatch_eligible = lambda connection, copy_id: True
    discovery.device_holder = lambda connection, device_id: 9
    outputs = ModuleType("camctl.persistence.repositories.outputs")
    outputs._read_owner_eligible = lambda action: action["status"] == 2 and action["cancel_requested"] == 0
    outputs._read_is_due = lambda action, now: action["scheduled_at"] <= now
    events = ModuleType("camctl.history.events")
    events.reason_for_branch = lambda event_name, branch_name: {"REQUIRE": 1, "COMPLETE": 2, "ABANDON": 3}[branch_name]
    operations = ModuleType("camctl.persistence.repositories.operations")
    operations._RETRY_WAIT_EVENT = 6
    structure = ModuleType("camctl.persistence.retry_wait_structure")
    def verify_index(connection, name):
        if connection.execute("SELECT sql FROM sqlite_master", (name,)).fetchone() is None:
            raise ValueError(name)
    structure.verify_retry_wait_index = verify_index
    modules = {module.__name__: module for module in (enums, json_values, values, discovery, outputs, events, operations, structure)}
    spec = importlib.util.spec_from_file_location("camctl.persistence.repositories._isolated_retry_reads",
                                                  SOURCE / "persistence" / "repositories" / "retry_wait_reads.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, modules


class RetryReadFactsTests(unittest.TestCase):
    def test_recovered_unknown_requires_original_result_and_no_unsettled_call(self):
        module, modules = load_reads()
        status = SimpleNamespace(UNKNOWN=object())
        effect = SimpleNamespace(UNKNOWN=object())
        basis = SimpleNamespace(ASSUMED=object())
        models = ModuleType("camctl.operations.models")
        models.AttemptStatus, models.EffectState, models.SettlementBasis = status, effect, basis
        transaction = ModuleType("camctl.persistence.transaction")
        transaction.row_facts = lambda connection, table, identity: {
            "status": 4, "effect_state": 4, "result_json": {}, "error_json": {}}
        result = ModuleType("camctl.operations.result_format")
        outcome = SimpleNamespace(status=status.UNKNOWN, effect=effect.UNKNOWN,
            error=SimpleNamespace(code="result_not_saved", stage="recovery", details={}),
            settlement=SimpleNamespace(basis=basis.ASSUMED,
                evidence=SimpleNamespace(type="local_read_recovery", version=1, data={})),
            observations=(), call_info=None, diagnostics=None)
        result.read_result_document = lambda *args: outcome
        modules.update({m.__name__: m for m in (models, transaction, result)})
        with patch.dict(sys.modules, modules):
            reads = module.RetryWaitReads(Connection(last=(12, 4, "{}", 101)))
            self.assertEqual(reads.recovered_unknown(7, 1)[0], 12)
            outcome.error.code = "device_error"
            self.assertIsNone(reads.recovered_unknown(7, 1))
            outcome.error.code = "result_not_saved"
            outcome.settlement.evidence.type = "other"
            self.assertIsNone(reads.recovered_unknown(7, 1))
            outcome.settlement.evidence.type = "adb_foreground_recovery"
            outcome.observations = (object(),)
            self.assertIsNone(reads.recovered_unknown(7, 1))
            outcome.observations = ()
            self.assertIsNone(module.RetryWaitReads(
                Connection(last=(12, 4, "{}", 101), running=True)).recovered_unknown(7, 1))

    def test_held_check_requires_original_holder_completed_wait_and_active_owner(self):
        module, modules = load_reads()
        owner = (2, 0, 10, 9, "camera", 2, 1, 3, "camera", "driver", "camera", "driver")
        class HeldConnection(Connection):
            def __init__(self, run=(7, 2, 1, 0), **kwargs):
                super().__init__(**kwargs)
                self.run = run
            def execute(self, query, parameters=()):
                if "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs" in query:
                    return Cursor([] if self.run is None else [self.run])
                return super().execute(query, parameters)
        with patch.dict(sys.modules, modules):
            check = module.held_read_ready_for_availability
            self.assertTrue(check(HeldConnection(owner=owner, event=event(2)), 9, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(owner=owner, event=event(2)), 9, "camera", 20, 1))
            self.assertFalse(check(HeldConnection(owner=owner, event=event(2), last=(12, 4, "{}", 101)),
                                   9, "camera", 20, 1))
            self.assertFalse(check(HeldConnection(owner=owner, event=event(2)), 10, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(run=(7, 2, 1, 1), owner=owner), 9, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(run=(7, 4, 1, 0), owner=owner, event=event(2)), 9, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(owner=owner, event=event(3)), 9, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(owner=owner, event=event(2), running=True), 9, "camera", 20, 2))
            self.assertFalse(check(HeldConnection(owner=(2, 1, *owner[2:]), event=event(2)), 9, "camera", 20, 2))
            with self.assertRaises(module.ConsistencyError):
                check(HeldConnection(run=None), 9, "camera", 20, 2)

    def test_control_preserves_reservation_and_requires_settled_failure(self):
        module, modules = load_reads()
        class ReservationConnection(Connection):
            def __init__(self, rows, **kwargs):
                super().__init__(**kwargs)
                self.rows = rows
            def execute(self, query, parameters=()):
                if "SELECT c.id,r.id,r.status,r.attempts_used" in query:
                    return Cursor(self.rows)
                return super().execute(query, parameters)
        with patch.dict(sys.modules, modules):
            check = module.read_reservation_blocks_control
            self.assertFalse(check(ReservationConnection([]), "camera"))
            self.assertFalse(check(ReservationConnection([(9, 7, 2, 1, 1)]), "camera"))
            self.assertFalse(check(ReservationConnection([(9, 7, 2, 1, 0)], event=event(2)), "camera"))
            self.assertTrue(check(ReservationConnection([(9, 7, 2, 1, 0)], event=event(3)), "camera"))
            self.assertTrue(check(ReservationConnection([(9, 7, 2, 0, 0)]), "camera"))
            self.assertTrue(check(ReservationConnection([(9, 7, 2, 1, 1)], running=True), "camera"))
            self.assertTrue(check(ReservationConnection([(9, 7, 2, 1, 1)], last=(12, 1, None, None)), "camera"))
            self.assertTrue(check(ReservationConnection([(9, 7, 2, 1, 1)], last=(12, 2, "{}", 101)), "camera"))
            with self.assertRaises(module.ConsistencyError):
                check(ReservationConnection([(9, None, None, None, None)]), "camera")
            with self.assertRaises(module.ConsistencyError):
                check(ReservationConnection([(9, 7, 2, 1, 1), (10, 8, 2, 1, 1)]), "camera")

    def test_complete_and_abandon_remain_distinct(self):
        module, _ = load_reads()
        with patch.dict(sys.modules, load_reads()[1]):
            self.assertTrue(module.RetryWaitReads(Connection(event=event(2))).completed(7, 1))
            self.assertFalse(module.RetryWaitReads(Connection(event=event(3))).completed(7, 1))
            self.assertFalse(module.RetryWaitReads(Connection(event=event(1))).completed(7, 1))

    def test_unfinished_original_attempt_cannot_complete_wait(self):
        module, _ = load_reads()
        with patch.dict(sys.modules, load_reads()[1]):
            self.assertFalse(module.RetryWaitReads(Connection(last=(12, 1, None, None), event=event(2))).completed(7, 1))
            self.assertFalse(module.RetryWaitReads(Connection(running=True, event=event(2))).completed(7, 1))
            self.assertFalse(module.RetryWaitReads(Connection(last=(12, 2, "{}", 101), event=event(2))).completed(7, 1))

    def test_complete_requires_the_original_attempt_reference(self):
        module, _ = load_reads()
        wrong = event(2)
        wrong["evidence"] = {"attempt_id": 13}
        with patch.dict(sys.modules, load_reads()[1]):
            with self.assertRaises(module.ConsistencyError):
                module.RetryWaitReads(Connection(event=wrong)).completed(7, 1)

    def test_missing_history_index_stops_instead_of_scanning(self):
        module, modules = load_reads()
        class MissingIndex(Connection):
            def execute(self, query, parameters=()):
                if "sqlite_master" in query and parameters == ("history_events_row",):
                    return Cursor([])
                return super().execute(query, parameters)
        with patch.dict(sys.modules, modules):
            with self.assertRaises(module.ConsistencyError):
                module.RetryWaitReads(MissingIndex(event=event(2))).completed(7, 1)

    def test_validated_original_result_settles_running_attempt(self):
        module, modules = load_reads()
        result = SimpleNamespace(envelope=SimpleNamespace(rows=(SimpleNamespace(
            table="operation_attempts", row_id=1,
            after=SimpleNamespace(values={"status": 3, "result_json": {}, "result_event_id": 100})),)))
        with patch.dict(sys.modules, modules):
            reads = module.RetryWaitReads(Connection(running=True))
            self.assertFalse(reads.no_unsettled_attempts(7))
            self.assertTrue(reads.no_unsettled_attempts(7, (result,)))

    def test_cancelled_delivery_cannot_complete_wait(self):
        module, modules = load_reads()
        owner = (2, 1, 10, 9, "camera", 2, 1, 3, "camera", "driver", "camera", "driver")
        with patch.dict(sys.modules, modules):
            self.assertFalse(module.RetryWaitReads(Connection(owner=owner)).read_eligible(7, 20))

    def test_valid_delivery_with_original_binding_is_eligible(self):
        module, modules = load_reads()
        owner = (2, 0, 10, 9, "camera", 2, 1, 3, "camera", "driver", "camera", "driver")
        with patch.dict(sys.modules, modules):
            self.assertTrue(module.RetryWaitReads(Connection(owner=owner)).read_eligible(7, 20))

    def test_slotless_fallback_requires_proven_original_switch(self):
        module, modules = load_reads()
        owner = (2, 0, 10, 9, None, 2, 1, 3, "camera", "driver", "camera", "driver")
        with patch.dict(sys.modules, modules):
            reads = module.RetryWaitReads(Connection(owner=owner))
            reads.source_fallback = lambda run_id: False
            self.assertFalse(reads.read_eligible(7, 20))
            reads.source_fallback = lambda run_id: True
            self.assertTrue(reads.read_eligible(7, 20))

    def test_cancelled_slotless_fallback_cannot_complete_wait(self):
        module, modules = load_reads()
        owner = (2, 1, 10, 9, None, 2, 1, 3, "camera", "driver", "camera", "driver")
        with patch.dict(sys.modules, modules):
            reads = module.RetryWaitReads(Connection(owner=owner))
            reads.source_fallback = lambda run_id: True
            self.assertFalse(reads.read_eligible(7, 20))

    def test_fallback_requires_matching_failure_and_switch_event(self):
        module, modules = load_reads()
        run = {"copy_id": 9, "attempts_used": 1, "retry_wait_required": 1}
        copy = {"slot_device_id": None, "source_device_file_id": 15, "source_intermediate_file_id": None}
        attempt = {"intent_event_id": 99, "error_json": {"code": "local_read_failed", "stage": "source_read",
                   "details": {"source_failed": True}}}
        modules["camctl.persistence.repositories.operations"]._load_row = lambda connection, table, identity: {
            "operation_runs": run, "file_copies": copy, "operation_attempts": attempt}[table]
        modules["camctl.persistence.repositories.outputs"]._has_source_fallback_retry_proof = lambda connection, copy, attempt: True
        with patch.dict(sys.modules, modules):
            reads = module.RetryWaitReads(Connection())
            self.assertTrue(reads.source_fallback(7))
            modules["camctl.persistence.repositories.outputs"]._has_source_fallback_retry_proof = lambda *args: False
            self.assertFalse(reads.source_fallback(7))
            modules["camctl.persistence.repositories.outputs"]._has_source_fallback_retry_proof = lambda *args: True
            attempt["error_json"]["details"]["source_failed"] = False
            self.assertFalse(reads.source_fallback(7))

    def test_fallback_does_not_accept_unsettled_original_call(self):
        module, modules = load_reads()
        modules["camctl.persistence.repositories.operations"]._load_row = lambda connection, table, identity: {
            "operation_runs": {"copy_id": 9, "attempts_used": 1},
            "file_copies": {"slot_device_id": None, "source_device_file_id": 15, "source_intermediate_file_id": None}}[table]
        modules["camctl.persistence.repositories.outputs"]._has_source_fallback_retry_proof = lambda *args: True
        with patch.dict(sys.modules, modules):
            self.assertFalse(module.RetryWaitReads(Connection(running=True)).source_fallback(7))

    def test_fallback_proof_queries_exact_copy_attempt_and_source(self):
        source = SOURCE / "persistence" / "repositories" / "outputs.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_has_source_fallback_retry_proof")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])
        structure = ModuleType("camctl.persistence.retry_wait_structure")
        structure.verify_retry_wait_index = lambda connection, name: None
        namespace = {"closing": __import__("contextlib").closing,
            "is_json_integer": lambda value: type(value) is int, "_COPY_CHANGED_EVENT": 22,
            "_COPY_SOURCE_FALLBACK_REASON": 8, "ConsistencyError": ValueError}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        class ProofConnection:
            def __init__(self, exists): self.exists, self.parameters = exists, None
            def execute(self, query, parameters):
                self.parameters = parameters
                return Cursor([(1,)] if self.exists else [])
        copy = {"id": 9, "source_device_file_id": 15}
        attempt = {"id": 12, "result_event_id": 101}
        with patch.dict(sys.modules, {structure.__name__: structure}):
            connection = ProofConnection(True)
            self.assertTrue(namespace["_has_source_fallback_retry_proof"](connection, copy, attempt))
            self.assertEqual(connection.parameters, (22, 9, 101, 8, 12, 15))
            self.assertFalse(namespace["_has_source_fallback_retry_proof"](ProofConnection(False), copy, attempt))

    def test_changed_source_binding_is_consistency_error(self):
        module, modules = load_reads()
        owner = (2, 0, 10, 9, "camera", 2, 1, 3, "camera", "driver", "other", "driver")
        with patch.dict(sys.modules, modules):
            with self.assertRaises(module.ConsistencyError):
                module.RetryWaitReads(Connection(owner=owner)).read_eligible(7, 20)


def load_exhaust_command(reads_type, run):
    source = SOURCE / "persistence" / "repositories" / "operations.py"
    tree = ast.parse(source.read_text())
    command = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "_ExhaustBlockedReadCommand")
    isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                command], type_ignores=[])

    class Base:
        def __init__(self, intent, key):
            self._intent, self._key = intent, key

        def plan(self, scope):
            return "exhausted"

        def _rejected(self, reason):
            return reason

    class AttemptIntent:
        def __init__(self, maximum):
            self.kind = "read"
            self.config = SimpleNamespace(max_attempts=maximum)
            self.occurred_at = 20

    reads_module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
    reads_module.RetryWaitReads = reads_type
    namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
        "BeginAttemptCommand": Base, "ObjectId": lambda value: value, "AttemptIntent": AttemptIntent,
        "OperationKind": SimpleNamespace(READ_FILE="read"), "_RUN_STATUS": SimpleNamespace(PENDING=1, ACTIVE=2),
        "_ATTEMPT_STATUS": SimpleNamespace(FAILED=3, UNKNOWN=4), "TransactionError": ValueError,
        "_saved_transaction_events": lambda connection, key: None,
        "_find_responsibility": lambda connection, intent: 7,
        "_load_row": lambda connection, table, identity: run}
    exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
    return namespace["_ExhaustBlockedReadCommand"], AttemptIntent, reads_module


class ExhaustReadTests(unittest.TestCase):
    def test_saved_exhaustion_reuses_original_attempt_and_configuration(self):
        source = SOURCE / "persistence" / "repositories" / "operations.py"
        tree = ast.parse(source.read_text())
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BeginAttemptCommand")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_reuse_read_exhausted")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    method], type_ignores=[])
        run = {"max_attempts_used": 2, "timeout_s_json": 4, "retry_interval_s_json": 5,
               "status": 4, "retry_wait_required": 0,
               "error_json": {"code": "read_attempts_exhausted", "stage": "source_read", "details": {}}}
        namespace = {"_OPERATION_CONFIGURED_EVENT": 10, "_RUN_STATUS": SimpleNamespace(FAILED=4),
            "_find_responsibility": lambda connection, intent: 7,
            "_load_row": lambda connection, table, identity: run,
            "_load_flow_context": lambda connection, kind, action, target, purpose, state:
                state.update({"file_copies": {9: {"round": 1}}}),
            "_verify_run_intent": lambda *args: None, "_verify_run_identity": lambda *args: None,
            "json_equal": lambda left, right: left == right, "TransactionError": ValueError}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        intent = SimpleNamespace(kind=3, action_id=1, target=SimpleNamespace(copy_id=9), query_purpose=None,
            config=SimpleNamespace(max_attempts=2, timeout_s=4, retry_interval_s=5), occurred_at=20, copy_round=1)
        command = SimpleNamespace(_intent=intent, _exhaust_attempt_id=12, _state={}, _rejected=lambda reason: reason)
        event = {"type": 10, "reason": 3, "occurred_at": 20,
                 "body": {"evidence": {"attempt_id": 12}, "rows": [{"table": "operation_runs", "id": 7,
                    "after": {"values": {"status": 4, "retry_wait_required": 0, "error_json": run["error_json"]}}}]}}
        reuse = namespace["_reuse_read_exhausted"]
        self.assertEqual(reuse(command, None, [event]), "budget_exhausted")
        command._exhaust_attempt_id = 13
        with self.assertRaises(ValueError): reuse(command, None, [event])
        command._exhaust_attempt_id = 12
        intent.config.max_attempts = 3
        with self.assertRaises(ValueError): reuse(command, None, [event])

    def test_unknown_result_is_not_rewritten_as_explicit_read_failure(self):
        class Reads:
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return (12, 4, "{}", 101)
            def recovered_unknown(self, run_id, used): return None
            def read_eligible(self, run_id, now): raise AssertionError("未知结果不应进入明确失败收场")

        command, intent, module = load_exhaust_command(Reads, {"status": 2, "attempts_used": 2})
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")

    def test_recovered_unknown_exhaustion_checks_owner_without_slot(self):
        class Reads:
            eligible = True
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return (12, 4, "{}", 101)
            def recovered_unknown(self, run_id, used): return (12, 4, "{}", 101)
            def read_eligible(self, run_id, now, *, allow_unheld=False):
                assert allow_unheld
                return self.eligible

        command, intent, module = load_exhaust_command(Reads, {"status": 2, "attempts_used": 2})
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "exhausted")
            Reads.eligible = False
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")
            self.assertEqual(command(intent(3), 12, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")
            self.assertEqual(command(intent(2), 13, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")

    def test_reduced_budget_finishes_settled_read(self):
        class Reads:
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return (12, 3, "{}", 101)
            def read_eligible(self, run_id, now): return True

        command, intent, module = load_exhaust_command(Reads, {"status": 2, "attempts_used": 2})
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "exhausted")

    def test_cancelled_owner_does_not_finish_read(self):
        class Reads:
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return (12, 3, "{}", 101)
            def read_eligible(self, run_id, now): return False

        command, intent, module = load_exhaust_command(Reads, {"status": 2, "attempts_used": 2})
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")


class CompleteReadCommandTests(unittest.TestCase):
    def load_command(self, *, saved, run, eligible=True, last=(12, 3, "{}", 101)):
        source = SOURCE / "persistence" / "repositories" / "operations.py"
        tree = ast.parse(source.read_text())
        command = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "_CompleteReadRetryWaitCommand")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    command], type_ignores=[])
        class Reads:
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return last
            def read_eligible(self, run_id, now): return eligible
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        module.RetryWaitReads = Reads
        class Plan:
            def __init__(self, **values): self.__dict__.update(values)
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "ObjectId": lambda value: value, "UtcMicros": lambda value: value,
            "TransactionError": ValueError, "CommandPlan": Plan, "_RETRY_WAIT_EVENT": 6,
            "_RUN_KIND": SimpleNamespace(READ_FILE=3), "_RUN_STATUS": SimpleNamespace(PENDING=1, ACTIVE=2),
            "OperationKind": SimpleNamespace(READ_FILE=3), "AttemptTarget": lambda **values: values,
            "_saved_transaction_events": lambda connection, key: saved,
            "_load_row": lambda connection, table, identity: run,
            "_load_flow_context": lambda *args: None, "_verify_run_identity": lambda *args: None,
            "_run_owner_ref": lambda run, state: ("delivery", 8),
            "_update": lambda *args: args,
            "_envelope": lambda *args: SimpleNamespace(evidence=args[-1])}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        return namespace["_CompleteReadRetryWaitCommand"], module

    def test_completed_wait_records_original_attempt(self):
        run = {"kind": 3, "action_id": 1, "copy_id": 9, "status": 2,
               "retry_wait_required": 1, "attempts_used": 1}
        command, module = self.load_command(saved=None, run=run)
        scope = SimpleNamespace(connection=None,
            allocate=lambda count: SimpleNamespace(first_event_id=200, txn_id=3))
        with patch.dict(sys.modules, {module.__name__: module}):
            result = command(7, 12, 20, "key").plan(scope)
        self.assertTrue(result.result)
        self.assertEqual(result.events[0].evidence, {"attempt_id": 12})

    def test_cancelled_work_does_not_clear_wait(self):
        run = {"kind": 3, "action_id": 1, "copy_id": 9, "status": 2,
               "retry_wait_required": 1, "attempts_used": 1}
        command, module = self.load_command(saved=None, run=run, eligible=False)
        with patch.dict(sys.modules, {module.__name__: module}):
            result = command(7, 12, 20, "key").plan(SimpleNamespace(connection=None))
        self.assertFalse(result.result)
        self.assertTrue(result.read_only)

    def test_replay_rejects_different_attempt(self):
        saved = [{"type": 6, "reason": 2, "occurred_at": 20, "event_id": 200,
                  "body": {"evidence": {"attempt_id": 13}, "rows": [{"id": 7, "table": "operation_runs"}]}}]
        command, module = self.load_command(saved=saved, run=None)
        with self.assertRaises(ValueError):
            command(7, 12, 20, "key").plan(SimpleNamespace(connection=None))

    def test_unfinished_attempt_does_not_finish_read(self):
        class Reads:
            def __init__(self, connection): pass
            def last_attempt(self, run_id, used): return None
            def read_eligible(self, run_id, now): raise AssertionError("未收场尝试不可进入资格判断")

        command, intent, module = load_exhaust_command(Reads, {"status": 2, "attempts_used": 2})
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(command(intent(2), 12, "key").plan(SimpleNamespace(connection=None)), "qualification_changed")


class RetryGuardTests(unittest.TestCase):
    def load_guard(self, eligible):
        source = SOURCE / "persistence" / "repositories" / "operations.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_retry_guard")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])

        class Reads:
            def last_attempt(self, run_id, attempts_used): return (12, 3, "{}", 101)
            def read_eligible(self, run_id, now): return eligible

        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        module.RetryWaitReads = Reads
        def fail(message): raise ValueError(message)
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "_RETRY_WAIT_EVENT": 6, "_RUN_KIND": SimpleNamespace(READ_FILE=3, CHECK_DEVICE_AVAILABILITY=12),
            "load_event_registry": lambda: {"events": {"RETRY_WAIT_CHANGED": {"branches": {"COMPLETE": {"reason": 2}}}}},
            "_RUN_STATUS": SimpleNamespace(PENDING=1, ACTIVE=2), "_fail": fail,
            "_row_after_facts": lambda context, row: {"kind": 3, "attempts_used": 1}}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        return namespace["_retry_guard"], Reads(), module

    def test_guard_accepts_settled_read_and_matching_attempt(self):
        guard, reads, module = self.load_guard(True)
        row = SimpleNamespace(table="operation_runs", row_id=7,
                              after=SimpleNamespace(values={"retry_wait_required": 0}))
        event = SimpleNamespace(rows=(row,), event_type=6, reason=2,
                                evidence={"attempt_id": 12}, occurred_at=20)
        with patch.dict(sys.modules, {module.__name__: module}):
            guard(event, SimpleNamespace(retry_wait_reads=reads))

    def test_guard_rechecks_cancelled_owner(self):
        guard, reads, module = self.load_guard(False)
        row = SimpleNamespace(table="operation_runs", row_id=7,
                              after=SimpleNamespace(values={"retry_wait_required": 0}))
        event = SimpleNamespace(rows=(row,), event_type=6, reason=2,
                                evidence={"attempt_id": 12}, occurred_at=20)
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaises(ValueError):
                guard(event, SimpleNamespace(retry_wait_reads=reads))

    def test_guard_rejects_another_attempt(self):
        guard, reads, module = self.load_guard(True)
        row = SimpleNamespace(table="operation_runs", row_id=7,
                              after=SimpleNamespace(values={"retry_wait_required": 0}))
        event = SimpleNamespace(rows=(row,), event_type=6, reason=2,
                                evidence={"attempt_id": 13}, occurred_at=20)
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaises(ValueError):
                guard(event, SimpleNamespace(retry_wait_reads=reads))


class ExhaustGuardTests(unittest.TestCase):
    def test_unknown_guard_checks_owner_copy_evidence_and_budget(self):
        source = SOURCE / "persistence" / "repositories" / "operations.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_operation_finish_guard")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])
        class Reads:
            connection = object()
            valid = True
            def last_attempt(self, run_id, used): return (12, 4, "{}", 101)
            def recovered_unknown(self, run_id, used): return (12, 4, "{}", 101) if self.valid else None
        reads_module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        reads_module.RetryWaitReads = Reads
        outputs = ModuleType("camctl.persistence.repositories.outputs")
        owner = {"id": 1, "status": 2, "cancel_requested": 0, "scheduled_at": 10}
        outputs._slot_owner_action = lambda context, copy: owner
        outputs._read_owner_eligible = lambda action: action["status"] == 2 and action["cancel_requested"] == 0
        outputs._read_is_due = lambda action, now: action["scheduled_at"] <= now
        outputs._internal_input_requirement = lambda processing: processing["reason"]
        discovery = ModuleType("camctl.outputs.read_discovery")
        copy_ready = [False]
        discovery.read_copy_dispatch_eligible = lambda connection, copy_id: not copy_ready[0]
        facts = {"kind": 3, "status": 4, "retry_wait_required": 0, "attempts_used": 2,
                 "max_attempts_used": 2, "copy_id": 9, "action_id": 1,
                 "error_json": {"code": "read_attempts_exhausted"}}
        def fail(message): raise ValueError(message)
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "_OPERATION_CONFIGURED_EVENT": 10, "_RUN_KIND": SimpleNamespace(READ_FILE=3, PREPARE_CAPTURE=10, CHECK_DEVICE_AVAILABILITY=12),
            "_RUN_STATUS": SimpleNamespace(PENDING=1, FAILED=4, CANCELED=5, UNCONFIRMED=6, SUCCEEDED=3),
            "_ATTEMPT_STATUS": SimpleNamespace(FAILED=3, UNKNOWN=4), "Mapping": dict,
            "_fail": fail, "_row_after_facts": lambda context, row: facts}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        row = SimpleNamespace(table="operation_runs", row_id=7,
            before=SimpleNamespace(exists=True, values={"retry_wait_required": 0}),
            after=SimpleNamespace(values={"status": 4}))
        event = SimpleNamespace(rows=(row,), event_type=10, reason=3, evidence={"attempt_id": 12}, occurred_at=20)
        reads = Reads()
        copy = {"id": 9, "source_device_file_id": None, "delivery_id": None, "processing_id": 8}
        processing = {"reason": None}
        context = SimpleNamespace(retry_wait_reads=reads, state_rows={"file_copies": {9: copy},
            "recording_processing": {8: processing}})
        modules = {m.__name__: m for m in (reads_module, outputs, discovery)}
        guard = namespace["_operation_finish_guard"]
        with patch.dict(sys.modules, modules):
            guard(event, context)
            for target, key, changed in ((owner, "cancel_requested", 1), (processing, "reason", "input_not_needed"),
                                          (facts, "max_attempts_used", 3)):
                original = target[key]
                target[key] = changed
                with self.assertRaises(ValueError): guard(event, context)
                target[key] = original
            copy_ready[0] = True
            with self.assertRaises(ValueError): guard(event, context)
            copy_ready[0] = False
            reads.valid = False
            with self.assertRaises(ValueError): guard(event, context)

    def test_settled_failure_requires_matching_attempt_reference(self):
        source = SOURCE / "persistence" / "repositories" / "operations.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_operation_finish_guard")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])
        class Reads:
            def last_attempt(self, run_id, used): return (12, 3, "{}", 101)
            def read_eligible(self, run_id, now): return True
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        module.RetryWaitReads = Reads
        facts = {"kind": 3, "status": 4, "retry_wait_required": 0, "attempts_used": 2,
                 "max_attempts_used": 2, "copy_id": 9, "error_json": {"code": "read_attempts_exhausted"}}
        def fail(message): raise ValueError(message)
        namespace = {"__name__": "isolated.operations", "__package__": "camctl.persistence.repositories",
            "_OPERATION_CONFIGURED_EVENT": 10, "_RUN_KIND": SimpleNamespace(READ_FILE=3, PREPARE_CAPTURE=10, CHECK_DEVICE_AVAILABILITY=12),
            "_RUN_STATUS": SimpleNamespace(PENDING=1, FAILED=4, CANCELED=5, UNCONFIRMED=6, SUCCEEDED=3),
            "_ATTEMPT_STATUS": SimpleNamespace(FAILED=3, UNKNOWN=4), "Mapping": dict,
            "_fail": fail, "_row_after_facts": lambda context, row: facts}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        row = SimpleNamespace(table="operation_runs", row_id=7, before=SimpleNamespace(exists=True, values={"retry_wait_required": 1}),
                              after=SimpleNamespace(values={"status": 4}))
        event = SimpleNamespace(rows=(row,), event_type=10, reason=3, evidence={}, occurred_at=20)
        context = SimpleNamespace(retry_wait_reads=Reads(), state_rows={"file_copies": {9: {
            "source_device_file_id": 15, "delivery_id": 8}}})
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaises(ValueError):
                namespace["_operation_finish_guard"](event, context)
            event.evidence = {"attempt_id": 12}
            namespace["_operation_finish_guard"](event, context)


class DeliveryCancellationTests(unittest.TestCase):
    def test_cancel_after_read_exhaustion_does_not_fail_delivery(self):
        source = SOURCE / "persistence" / "repositories" / "outputs.py"
        tree = ast.parse(source.read_text())
        owner = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "_ReadDeliveryFailCommand")
        plan = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "plan")
        handoff = ast.parse((SOURCE / "outputs" / "handoff.py").read_text())
        disposition = next(node for node in handoff.body
                           if isinstance(node, ast.ClassDef) and node.name == "FailureSaveDisposition")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    disposition, plan], type_ignores=[])
        namespace = {"saved_transaction_events": lambda connection, key: None,
            "Enum": Enum,
            "_DELIVERY_STATUS": SimpleNamespace(FAILED=6, CANCELED=7, WITHDRAWN=8),
            "FailureSaveOutcome": lambda disposition: disposition}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        command = SimpleNamespace(_key="key", _command=SimpleNamespace(delivery_id=9),
            _load_delivery=lambda connection, identity: ({"status": 2}, {}),
            _owner_action=lambda connection, delivery: {"cancel_requested": 1},
            _decision=lambda result, read_only: (result, read_only))
        self.assertEqual(namespace["plan"](command, SimpleNamespace(connection=None)),
                         (namespace["FailureSaveDisposition"].CANCELED, True))

    def test_settlement_rejects_unfinished_read_without_loading_history(self):
        source = SOURCE / "persistence" / "repositories" / "outputs.py"
        tree = ast.parse(source.read_text())
        owner = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "_ReadDeliveryFailCommand")
        function = next(node for node in owner.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_require_read_finished")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])
        class Reads:
            def __init__(self, connection): pass
            def no_unsettled_attempts(self, run_id): return False
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        module.RetryWaitReads = Reads
        namespace = {"__name__": "isolated.outputs", "__package__": "camctl.persistence.repositories",
                     "enum_for": lambda name: SimpleNamespace(FAILED=4), "ConsistencyError": ValueError}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaises(ValueError):
                namespace["_require_read_finished"](None, None, {"id": 7, "status": 4})

    def test_source_protection_release_rechecks_unsettled_read(self):
        source = SOURCE / "persistence" / "repositories" / "outputs.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_read_permission_guard")
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                    function], type_ignores=[])
        class Reads:
            def no_unsettled_attempts(self, run_id, validated=()): return False
        module = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        module.RetryWaitReads = Reads
        namespace = {"__name__": "isolated.outputs", "__package__": "camctl.persistence.repositories",
            "_READ_PERMISSION_EVENT": 4, "_GRANT_REASON": 1, "_REJECT_REASON": 2, "_RELEASE_REASON": 3,
            "_DELIVERY_STATUS": SimpleNamespace(FAILED=6, CANCELED=7, WITHDRAWN=8),
            "_RUN_TERMINAL": {3, 4, 5, 6, 7}, "EventValidationError": ValueError}
        exec(compile(ast.fix_missing_locations(isolated), str(source), "exec"), namespace)
        row = SimpleNamespace(table="obtain_items", row_id=2,
            before=SimpleNamespace(exists=True, values={"source_dependency": 1}),
            after=SimpleNamespace(values={"source_dependency": 0}))
        event = SimpleNamespace(event_type=4, reason=3, rows=(row,))
        context = SimpleNamespace(retry_wait_reads=Reads(), validated_events=(), state_rows={
            "obtain_items": {2: {"delivery_id": 8}}, "deliveries": {8: {"status": 6}},
            "file_copies": {9: {"id": 9, "delivery_id": 8}},
            "operation_runs": {7: {"id": 7, "copy_id": 9, "status": 4, "retry_wait_required": 0}}},
            complete_rows=lambda table, column, identity: {7: {"id": 7, "status": 4, "retry_wait_required": 0}})
        with patch.dict(sys.modules, {module.__name__: module}):
            with self.assertRaises(ValueError):
                namespace["_read_permission_guard"](event, context)

if __name__ == "__main__":
    unittest.main()
