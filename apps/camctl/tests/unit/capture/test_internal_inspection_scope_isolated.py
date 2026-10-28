"""实际轻量恢复上下文与检查保存者；只使用内存协作者。"""

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).parents[3] / "src/camctl/capture"


class StripImports(ast.NodeTransformer):
    def visit_Import(self, node): return None
    def visit_ImportFrom(self, node): return None


def load(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              StripImports().visit(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


@dataclass(frozen=True)
class ResultRequest:
    inspection_id: int
    state: int
    result_json: dict
    occurred_at: int


class Shared:
    def retry_failed(self): pass
    async def run(self, callback): return await callback()


class Values(SimpleNamespace):
    def __getitem__(self, name): return getattr(self, name)


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class InspectionScopeTests(unittest.TestCase):
    def test_original_result_and_inspection_unknown_recover_with_shared_keys(self):
        async def run():
            ns = {"SimpleNamespace": SimpleNamespace}
            completed, unknown = "completed", "unknown"
            writes, read_keys, releases = [], [], []
            inspection = SimpleNamespace(inspection_id=8, state=1, target_json={"metric": "duration_s"})
            slot = ["camera"]
            error = {"code": "device_binding_unavailable", "stage": "execution", "details": {"reason": "missing"}}
            class Connection:
                def execute(self, sql, params):
                    if sql.startswith("SELECT c.inspection_id,c.committed_bytes"):
                        return Cursor([(8, 10, 10, None, None, 1, None, 7, 4, error, 0, 1)])
                    if sql.startswith("SELECT 1 FROM operation_attempts"):
                        return Cursor([])
                    if sql.startswith("SELECT c.inspection_id,c.slot_device_id"):
                        return Cursor([(8, slot[0], 4, 0, inspection.state, 0, 55)])
                    if sql.startswith("SELECT id FROM file_copies WHERE inspection_id"):
                        return Cursor([(9,)])
                    raise AssertionError(sql)
            class Repository:
                def read(self, identity, owned): return inspection
                def save_result(self, request, key, owned):
                    writes.append((request, key))
                    if len(writes) == 1: return SimpleNamespace(kind=unknown, error="unknown")
                    inspection.state = 3
                    return SimpleNamespace(kind=completed, value=inspection)
            class Database:
                async def read(self, callback, **kwargs): return callback(SimpleNamespace(connection=Connection()))
                async def write(self, callback, **kwargs): return callback(SimpleNamespace(connection=Connection()))
            class Outputs:
                release_read_slot = object()
            @dataclass(frozen=True)
            class SlotRequest:
                copy_id: int
                occurred_at: int
            async def save_business(flow, copy_id, stage, request, method):
                releases.append((copy_id, stage, request))
                slot[0] = None
                return SimpleNamespace(value=SimpleNamespace(outcome="released"))
            ticket = SimpleNamespace(target_id="9", run_id=7, attempt_id=1)
            finish = SimpleNamespace(ticket=ticket, occurred_at=17,
                outcome=SimpleNamespace(outcome=SimpleNamespace(status="succeeded")),
                run_finish=SimpleNamespace(status="failed"))
            pending = SimpleNamespace(finish=finish, key="original-read-key", business=None,
                preserved_run_terminal=False, settlement=Shared(), result_save=Shared())
            original_results = {(7, 1): pending}
            inspections = {}
            async def save_read_result(flow, actual):
                read_keys.append(actual.key)
                if len(read_keys) == 1: raise RuntimeError("read receipt unknown")
                return actual
            async def read_one(database, sql, params):
                return (None, 8) if "processing_id" in sql else (4,)
            def forget(flow, value): flow.pending_read_results.pop((7, 1), None)
            async def retry_business(flow): pass
            ns.update({"PendingReadResult": type(pending), "PendingStoppedRead": type("Stopped", (), {}),
                "OperationRepository": lambda: object(), "OutputsRepository": Outputs,
                "DbOutcomeKind": SimpleNamespace(COMPLETED=completed), "_DbOutcomeKind": SimpleNamespace(COMPLETED=completed),
                "_RUN_STATUS": Values(FAILED=4, ACTIVE=2, PENDING=1, SUCCEEDED=3, CANCELED=5),
                "_INSPECTION_STATE": SimpleNamespace(PENDING=1, FAILED=3),
                "AttemptStatus": SimpleNamespace(SUCCEEDED="succeeded"), "RunOutcome": SimpleNamespace(FAILED="failed"),
                "enum_for": lambda name: {"operation_runs.kind": SimpleNamespace(READ_FILE=3),
                    "operation_runs.status": SimpleNamespace(FAILED=4),
                    "operation_attempts.status": SimpleNamespace(RUNNING=1)}[name],
                "parse_exact_json": lambda value: value, "Mapping": dict,
                "SegmentInspectionRepository": Repository, "InspectionResultSave": ResultRequest,
                "CoverageMetric": lambda value: value, "VideoMetric": lambda *args: args,
                "_result_document": lambda metric, **kwargs: {"metric": metric, **kwargs},
                "new_operation_key": lambda: "original-inspection-key", "db_outcome_errors": lambda outcome: (),
                "record_error": lambda *args, **kwargs: None, "ConsistencyError": ValueError,
                "save_read_result": save_read_result, "_read_one": read_one, "forget_read_result": forget,
                "retry_read_business": retry_business, "save_read_business": save_business,
                "SlotRequest": SlotRequest, "SlotOutcome": SimpleNamespace(RELEASED="released", ALREADY_RELEASED="already")})
            segment = ROOT / "segment_inspections.py"
            media = ROOT / "media_flow.py"
            for name in ("_save_result", "settle_inspection_read_failure", "settle_terminal_inspection_read_slot", "resume_pending_inspection_results"):
                ns[name] = load(segment, name, ns)
            for name in ("internal_read_scope", "_save_internal_read_result", "_save_internal_read_and_settle", "resume_prepared_internal_reads"):
                ns[name] = load(media, name, ns)
            args = dict(pending_read_results=original_results, pending_read_business={}, pending_read_ends={},
                        continuing_read_tickets={}, pending_segment_inspection_results=inspections, occurred_at=lambda: 20)
            with self.assertRaisesRegex(RuntimeError, "read receipt unknown"):
                await ns["resume_prepared_internal_reads"](Database(), **args)
            self.assertIn((7, 1), original_results)
            await ns["resume_prepared_internal_reads"](Database(), **args)
            self.assertEqual(read_keys, ["original-read-key", "original-read-key"])
            self.assertEqual([key for _, key in writes], ["original-inspection-key", "original-inspection-key"])
            self.assertFalse(inspections)
            self.assertEqual(len(releases), 1)
            await ns["resume_prepared_internal_reads"](Database(), **args)
            self.assertFalse(original_results)
            self.assertEqual(len(releases), 1)
        asyncio.run(run())


if __name__ == "__main__": unittest.main()
