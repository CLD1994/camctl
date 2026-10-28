"""读取结束事务只使用进程内事实替身。"""

from contextlib import contextmanager
import ast
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src/camctl/persistence/repositories/read_end.py"
ATTEMPTS_SOURCE = SOURCE.parents[2] / "outputs/read_attempts.py"
OUTPUTS_SOURCE = SOURCE.parent / "outputs.py"
HANDLERS_SOURCE = SOURCE.parents[2] / "capture/handlers.py"
INSPECTIONS_SOURCE = SOURCE.parents[2] / "capture/segment_inspections.py"
RUNTIME_SOURCE = SOURCE.parent.parent / "runtime.py"


@dataclass(frozen=True)
class Ticket:
    attempt_id: int = 1
    operation: str = "read"
    target_id: str = "9"
    responsibility_key: str = "read/9"
    run_id: int = 7


@dataclass(frozen=True)
class End:
    stopped: bool
    bytes_read: int
    error: str | None


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchmany(self, size): return self.rows[:size]
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class Connection:
    def __init__(self, *, support=2, progress=10, source_hash="hash", unsettled=False):
        self.rows = {
            ("file_copies", 9): {"id": 9, "round": 1, "source_device_file_id": 15,
                "source_intermediate_file_id": None, "target_file_id": 20, "source_size": 10,
                "committed_bytes": progress, "reset_state": 1, "slot_device_id": "camera",
                "source_sha256": source_hash, "read_end_event_id": None, "delivery_id": 2},
            ("device_files", 15): {"checksum_support": support, "observer_action_id": 16, "source_action_id": 17},
            ("actions", 16): {"device_id": "camera", "driver_id": "driver"},
            ("actions", 17): {"device_id": "camera", "driver_id": "driver"},
            ("operation_runs", 7): {"id": 7, "kind": 3, "copy_id": 9,
                "responsibility_key": "read/9", "attempts_used": 1, "action_id": 6, "delivery_id": 2},
            ("operation_attempts", 12): {"id": 12, "status": 1, "result_event_id": None,
                "intent_event_id": 4, "copy_round": 1},
        }
        self.unsettled = unsettled
    def execute(self, sql, parameters):
        if "attempt_no=?" in sql: return Cursor([(12,)])
        if "id<>?" in sql: return Cursor([(13,)] if self.unsettled else [])
        raise AssertionError(sql)


@contextmanager
def isolated():
    modules = {}
    def stub(name, **members):
        item = ModuleType(name)
        item.__dict__.update(members)
        modules[name] = item
        return item
    enums = {"operation_runs.kind": SimpleNamespace(READ_FILE=3),
             "operation_attempts.status": SimpleNamespace(RUNNING=1),
             "device_files.checksum_support": SimpleNamespace(UNSUPPORTED=3)}
    stub("camctl.contracts.enums", enum_for=lambda name: enums[name])
    stub("camctl.contracts.json_values", json_equal=lambda a, b: a == b, parse_exact_json=lambda value: value)
    stub("camctl.contracts.values", ConsistencyError=ValueError, ObjectId=int, OperationKey=str, UtcMicros=int)
    stub("camctl.devices.read_session", ReadEnd=End)
    stub("camctl.operations.models", AttemptTicket=Ticket)
    stub("camctl.history.events", reason_for_branch=lambda *args: 9)
    stub("camctl.history.validators", EventValidationError=ValueError)
    class Plan:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.__dict__.update(kwargs)
    def update(table, identity, before, after):
        return SimpleNamespace(table=table, row_id=identity,
            before=SimpleNamespace(values=before), after=SimpleNamespace(values=after))
    stub("camctl.persistence.transaction", CommandPlan=Plan, commit_operation=None,
         event_envelope=lambda event_id, txn, kind, reason, rows, now, evidence:
            SimpleNamespace(event_id=event_id, rows=rows, evidence=evidence),
         row_facts=lambda connection, table, identity: connection.rows.get((table, identity)),
         saved_transaction_events=lambda connection, key: None, update_change=update)
    stub("camctl.persistence.repositories.outputs", _copy_round_chain_proven=lambda *args: True,
         _copy_action_id=lambda *args: 6, _verify_source_identity=lambda *args: None,
         _copy_owner_of=lambda connection, copy, state, owners: owners.update({("file_copies", 9): ("delivery", 2)}))
    class RetryReads:
        def __init__(self, connection): pass
        def _require_index(self, name): pass
    stub("camctl.persistence.repositories.retry_wait_reads", RetryWaitReads=RetryReads)
    name = "camctl.persistence.repositories._read_end_isolated"
    spec = spec_from_file_location(name, SOURCE)
    module = module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        yield module


class ReadEndTests(unittest.TestCase):
    def request(self, module, end=None):
        return module.SaveReadEnd(Ticket(), 1, 15, None, 20, 10, end or End(True, 0, None), 5)

    def test_actual_session_bytes_and_slot_release_share_event(self):
        with isolated() as module:
            request = self.request(module)
            scope = SimpleNamespace(connection=Connection(), allocate=lambda count: SimpleNamespace(first_event_id=30, txn_id=2))
            plan = module.SaveReadEndCommand(request, "key").plan(scope)
            event = plan.args[0][0]
            self.assertEqual(event.evidence["read_end"]["end"]["bytes_read"], 0)
            self.assertEqual(event.rows[0].after.values, {"read_end_event_id": 30, "slot_device_id": None})

    def test_missing_device_digest_keeps_slot(self):
        with isolated() as module:
            scope = SimpleNamespace(connection=Connection(source_hash=None), allocate=lambda count: SimpleNamespace(first_event_id=30, txn_id=2))
            event = module.SaveReadEndCommand(self.request(module), "key").plan(scope).args[0][0]
            self.assertEqual(event.rows[0].after.values, {"read_end_event_id": 30})

    def test_incomplete_progress_and_other_unsettled_call_are_rejected(self):
        with isolated() as module:
            for connection in (Connection(progress=9), Connection(unsettled=True)):
                scope = SimpleNamespace(connection=connection, allocate=lambda count: SimpleNamespace(first_event_id=30, txn_id=2))
                with self.assertRaises(ValueError): module.SaveReadEndCommand(self.request(module), "key").plan(scope)

    def test_error_or_invalid_actual_end_is_rejected(self):
        with isolated() as module:
            for end in (End(False, 0, None), End(True, 0, "failed"), End(True, 11, None)):
                with self.assertRaises(ValueError): self.request(module, end)

    def test_saved_end_requires_original_source_slot_and_round(self):
        with isolated() as module:
            for change in ({"slot_device_id": None}, {"slot_device_id": "other"}, {"round": 2}):
                connection = Connection()
                connection.rows[("file_copies", 9)].update(change)
                scope = SimpleNamespace(connection=connection,
                    allocate=lambda count: SimpleNamespace(first_event_id=30, txn_id=2))
                with self.assertRaises(ValueError):
                    module.SaveReadEndCommand(self.request(module), "key").plan(scope)

    def test_guard_rejects_boolean_round_or_extra_evidence(self):
        with isolated() as module:
            connection = Connection()
            reads = module.ReadEndReads(connection)
            for change in ({"copy_round": True}, {"committed_bytes": True}, {"extra": "unexpected"}):
                document = self.request(module).document()
                document.update(change)
                with self.assertRaises(ValueError):
                    reads.verify(connection.rows[("file_copies", 9)], document, 30)

    def test_guard_rejects_releasing_slot_without_required_source_digest(self):
        with isolated() as module:
            connection = Connection(source_hash=None)
            copy = connection.rows[("file_copies", 9)]
            row = SimpleNamespace(table="file_copies", row_id=9,
                before=SimpleNamespace(values={"read_end_event_id": None, "slot_device_id": "camera"}),
                after=SimpleNamespace(values={"read_end_event_id": 30, "slot_device_id": None}))
            event = SimpleNamespace(event_id=30, rows=(row,), evidence={"read_end": self.request(module).document()})
            context = SimpleNamespace(state_rows={"file_copies": {9: copy}}, read_end_reads=module.ReadEndReads(connection))
            with self.assertRaises(ValueError):
                module.read_end_guard(event, context)

    def test_original_key_replay_uses_saved_event_after_current_round_changes(self):
        with isolated() as module:
            request = self.request(module)
            event = {"type": 22, "reason": 9, "occurred_at": 5, "event_id": 30,
                     "body": {"evidence": {"read_end": request.document()}, "rows": [{
                         "id": 9, "table": "file_copies",
                         "before": {"values": {"read_end_event_id": None}},
                         "after": {"values": {"read_end_event_id": 30}}}]}}
            module.saved_transaction_events = lambda connection, key: [event]
            connection = Connection()
            connection.rows[("file_copies", 9)]["round"] = 2
            scope = SimpleNamespace(connection=connection)
            plan = module.SaveReadEndCommand(request, "key").plan(scope)
            self.assertTrue(plan.read_only)
            self.assertEqual(plan.args[3], 30)
            different = self.request(module, End(True, 1, None))
            with self.assertRaises(ValueError):
                module.SaveReadEndCommand(different, "key").plan(scope)


class ReadEndRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_freezes_original_session_facts_and_reuses_failed_request(self):
        tree = ast.parse(ATTEMPTS_SOURCE.read_text())
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in ("hold_read_end", "persist_read_end")]
        namespace = {"HeldReadEnd": lambda ticket, end, occurred_at, resume, evidence:
                     SimpleNamespace(ticket=ticket, end=end, occurred_at=occurred_at, resume=resume, evidence=evidence),
                     "ConsistencyError": ValueError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                     str(ATTEMPTS_SOURCE), "exec"), namespace)
        requests = []
        async def save_business(runtime, copy_id, stage, request, save, *, retry):
            requests.append((request, retry))
            if not retry:
                runtime.pending_read_business[(copy_id, stage)] = SimpleNamespace(request=request)
                raise RuntimeError("回执未知")
        namespace["save_read_business"] = save_business
        repository = ModuleType("camctl.persistence.repositories.read_end")
        repository.SaveReadEnd = lambda *values: values
        repository.save_read_end = object()
        runtime = SimpleNamespace(pending_read_ends={}, pending_read_business={}, occurred_at=lambda: 5)
        state = SimpleNamespace(round=3, target=SimpleNamespace(file_id=20), source_size=10)
        with patch.dict(sys.modules, {repository.__name__: repository}):
            with self.assertRaises(RuntimeError):
                await namespace["persist_read_end"](runtime, Ticket(), End(True, 2, None), True,
                    state=state, source_device_file_id=15, source_intermediate_file_id=None)
            state.round = 4
            runtime.occurred_at = lambda: 9
            await namespace["persist_read_end"](runtime, Ticket(), End(True, 2, None), True,
                state=state, source_device_file_id=99, source_intermediate_file_id=None)
        self.assertEqual(requests[0], (requests[1][0], False))
        self.assertTrue(requests[1][1])
        self.assertEqual(requests[1][0][1:6], (3, 15, None, 20, 10))
        self.assertEqual(requests[1][0][-1], 5)

    async def test_local_resume_rechecks_ticket_and_current_round_after_file_acquisition(self):
        tree = ast.parse(ATTEMPTS_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "current_local_read_end")
        ticket, end = Ticket(), End(True, 0, None)
        held = SimpleNamespace(ticket=ticket, end=end)
        repository = ModuleType("camctl.persistence.repositories.read_end")
        current = [ticket, end, 5, None]
        repository.load_read_end = lambda *args: current
        original = [ticket, 4]
        namespace = {"ConsistencyError": ValueError, "running_read_ticket": lambda *args: original}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(ATTEMPTS_SOURCE), "exec"), namespace)
        class Database:
            async def read(self, callback): return callback(object())
        runtime = SimpleNamespace(database=Database())
        check = namespace["current_local_read_end"]
        with patch.dict(sys.modules, {repository.__name__: repository}):
            self.assertTrue(await check(runtime, 9, held))
            current[3] = "camera"
            self.assertFalse(await check(runtime, 9, held))
            current[3] = None
            current[1] = End(True, 1, None)
            with self.assertRaises(ValueError):
                await check(runtime, 9, held)
            original[0] = Ticket(attempt_id=2)
            with self.assertRaises(ValueError):
                await check(runtime, 9, held)


class ZeroLengthRoundTests(unittest.TestCase):
    def test_zero_progress_omitted_in_recopy_history_is_proven_only_for_empty_source(self):
        tree = ast.parse(OUTPUTS_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_copy_round_chain_proven")
        before = {"round": 1, "recopies_used": 0, "reset_state": 1,
                  "verification_state": 4, "target_sha256": "old"}
        after = {"round": 2, "recopies_used": 1, "reset_state": 2,
                 "verification_state": 1, "target_sha256": None}
        row = SimpleNamespace(table="file_copies", row_id=9,
            before=SimpleNamespace(exists=True, values=before),
            after=SimpleNamespace(exists=True, values=after))
        event = SimpleNamespace(event_type=22, reason=4, rows=(row,))
        class Events(Cursor):
            def __iter__(self): return iter(self.rows)
        connection = SimpleNamespace(execute=lambda *args: Events([event]))
        namespace = {"closing": __import__("contextlib").closing,
                     "decode_event_row": lambda value: value,
                     "_COPY_CHANGED_EVENT": 22, "_RECOPY_REASON": 4,
                     "_RESET_STATE": SimpleNamespace(READY=1, RESET_PENDING=2),
                     "_VERIFICATION": SimpleNamespace(MISMATCHED=4, NOT_PERFORMED=1)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(OUTPUTS_SOURCE), "exec"), namespace)
        chain = namespace["_copy_round_chain_proven"]
        self.assertTrue(chain(connection, 9, 1, 2, 4, 30, 0))
        self.assertFalse(chain(connection, 9, 1, 2, 4, 30, 10))

    def test_database_open_rejects_missing_or_wrong_reference_structure(self):
        tree = ast.parse(RUNTIME_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_verify_read_end_structure")
        namespace = {"DatabaseInvalidError": ValueError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(RUNTIME_SOURCE), "exec"), namespace)
        class Connection:
            def __init__(self, columns, keys): self.columns, self.keys = columns, keys
            def execute(self, sql):
                return self.columns if "table_info" in sql else self.keys
        column = (0, "read_end_event_id", "INTEGER", 0, None, 0)
        key = (0, 0, "history_events", "read_end_event_id", "id")
        check = namespace["_verify_read_end_structure"]
        check(Connection([column], [key]))
        for connection in (Connection([], [key]), Connection([column], []),
                           Connection([column], [(0, 0, "other", "read_end_event_id", "id")])):
            with self.assertRaises(ValueError): check(connection)

    def test_recopy_guard_requires_current_end_to_be_cleared(self):
        tree = ast.parse(OUTPUTS_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_copy_guard")
        namespace = {"EventValidationError": ValueError, "_COPY_CHANGED_EVENT": 22,
            "_COPY_SOURCE_FALLBACK_REASON": 8, "_RESET_REASON": 1, "_SEGMENT_REASON": 2,
            "_VERIFY_REASON": 3, "_RECOPY_REASON": 4}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(OUTPUTS_SOURCE), "exec"), namespace)
        before = {"round": 1, "recopies_used": 0, "read_end_event_id": 30}
        after = {"round": 2, "recopies_used": 1, "read_end_event_id": None, "target_sha256": None}
        row = SimpleNamespace(table="file_copies", row_id=9,
            before=SimpleNamespace(values=before), after=SimpleNamespace(values=after))
        event = SimpleNamespace(event_type=22, reason=4, rows=(row,))
        context = SimpleNamespace(state_rows={"file_copies": {9: {
            "source_size": 0, "committed_bytes": 0, "read_end_event_id": 30}}})
        namespace["_copy_guard"](event, context)
        after["read_end_event_id"] = 31
        with self.assertRaises(ValueError): namespace["_copy_guard"](event, context)
        del after["read_end_event_id"]
        with self.assertRaises(ValueError): namespace["_copy_guard"](event, context)


class InternalColdResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_and_cancel_use_original_ticket_without_device_call(self):
        tree = ast.parse(HANDLERS_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "_resume_bound_local_read")
        namespace = {"ConsistencyError": ValueError, "DeviceBinding": lambda *values: values,
                     "replace": lambda value, **kwargs: value}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(HANDLERS_SOURCE), "exec"), namespace)
        calls = []
        read = ModuleType("camctl.outputs.read_attempts")
        read.HeldReadEnd = lambda ticket, end, occurred_at, evidence: SimpleNamespace(
            ticket=ticket, end=end, occurred_at=occurred_at, evidence=evidence, resume=None)
        async def canceled(flow, ticket, evidence):
            calls.append(("canceled", ticket))
            return "result"
        async def owned(flow, copy_id, body):
            calls.append(("owned", copy_id))
            await body(None)
        read.canceled_complete_read_result = canceled
        read.run_owned_read = owned
        async def current(*args): return True
        read.current_local_read_end = current
        media = ModuleType("camctl.capture.media_flow")
        async def settle(flow, result): calls.append(("settle", result))
        async def resume(flow, held, original, context): calls.append(("local", held.ticket, context.read_end))
        media._save_internal_read_and_settle = settle
        media._resume_local_internal_end = resume
        input_copy = ModuleType("camctl.capture.input_copy")
        input_copy.InputContext = lambda **kwargs: SimpleNamespace(**kwargs)
        input_copy.OperationConfig = lambda *args: args
        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args:
                    SimpleNamespace(fetchone=lambda: (2, None, 15, "camera", "driver")))))
        flow = SimpleNamespace(max_read_attempts=3, read_idle_timeout_s=5, retry_interval_s=3,
            segment_size=10, roots=SimpleNamespace(staging="staging"), copies=lambda: object(), sessions=object())
        runtime = SimpleNamespace(media=flow, recovery_evidence_for=lambda *args: "evidence",
            database=Database(), pending_read_ends={}, wall_us=lambda: 5)
        ticket, end = Ticket(), End(True, 0, None)
        saved = (ticket, end, 4, None)
        stubs = {read.__name__: read, media.__name__: media, input_copy.__name__: input_copy}
        with patch.dict(sys.modules, stubs):
            await namespace["_resume_bound_local_read"](runtime, {"id": 6, "cancel_requested": 0}, 9, ticket, saved)
            self.assertEqual(calls, [("local", ticket, end)])
            calls.clear()
            await namespace["_resume_bound_local_read"](runtime, {"id": 6, "cancel_requested": 1}, 9, ticket, saved)
            self.assertEqual(calls, [("owned", 9), ("canceled", ticket), ("settle", "result")])
            with self.assertRaises(ValueError):
                await namespace["_resume_bound_local_read"](runtime, {"id": 6, "cancel_requested": 0},
                    9, Ticket(attempt_id=2), saved)

    async def test_canceled_inspection_settles_saved_original_inside_file_ownership(self):
        tree = ast.parse(INSPECTIONS_SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "_settle_canceled_inspection_end")
        namespace = {"ConsistencyError": ValueError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                     str(INSPECTIONS_SOURCE), "exec"), namespace)
        calls = []
        ticket, end = Ticket(), End(True, 0, None)
        read = ModuleType("camctl.outputs.read_attempts")
        read.HeldReadEnd = lambda ticket, end, occurred_at, evidence: SimpleNamespace(ticket=ticket, end=end)
        read.running_read_ticket = lambda owned, copy_id: (ticket, 4)
        async def canceled(flow, ticket, evidence):
            calls.append(("result", ticket))
            return "pending"
        async def owned(flow, copy_id, body):
            calls.append(("owned", copy_id))
            await body(None)
        read.canceled_complete_read_result, read.run_owned_read = canceled, owned
        repository = ModuleType("camctl.persistence.repositories.read_end")
        saved = [ticket, end, 5, None]
        repository.load_read_end = lambda *args: saved
        media = ModuleType("camctl.capture.media_flow")
        async def settle(flow, pending): calls.append(("save", pending))
        media._save_internal_read_and_settle = settle
        class Database:
            async def read(self, callback): return callback(object())
        flow = SimpleNamespace(database=Database(), evidence="evidence", pending_read_ends={})
        with patch.dict(sys.modules, {read.__name__: read, repository.__name__: repository, media.__name__: media}):
            await namespace["_settle_canceled_inspection_end"](flow, 9)
            self.assertEqual(calls, [("owned", 9), ("result", ticket), ("save", "pending")])
            calls.clear()
            saved[0] = Ticket(attempt_id=2)
            with self.assertRaises(ValueError):
                await namespace["_settle_canceled_inspection_end"](flow, 9)
            self.assertEqual(calls, [("owned", 9)])


if __name__ == "__main__":
    unittest.main()
