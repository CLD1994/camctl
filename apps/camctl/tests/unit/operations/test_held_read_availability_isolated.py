"""持槽读取沿原顺序触发检查的资格分类。"""

import ast
from contextlib import closing
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize("holder,blocked,status,used,waiting,completed,eligible,expected", [
    (7, False, 2, 1, 0, True, True, True),
    (8, False, 2, 1, 0, True, True, False),
    (7, True, 2, 1, 0, True, True, False),
    (7, False, 2, 1, 1, True, True, False),
    (7, False, 2, 1, 0, False, True, False),
    (7, False, 2, 1, 0, True, False, False),
    (7, False, 4, 1, 0, True, True, False),
    (7, False, 2, 0, 0, True, True, False),
])
def test_held_check_requires_original_settled_retry(
        monkeypatch, holder, blocked, status, used, waiting, completed, eligible, expected):
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/retry_wait_reads.py"
    source = ast.parse(path.read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == "held_read_ready_for_availability")
    program = ast.Module(body=[function], type_ignores=[])
    discovery = ModuleType("camctl.outputs.read_discovery")
    discovery.device_holder = lambda connection, device: holder
    monkeypatch.setitem(sys.modules, discovery.__name__, discovery)
    class Cursor:
        def fetchmany(self, limit): return [(9, status, used, waiting)]
        def close(self): pass
    class Connection:
        def execute(self, *args): return Cursor()
    enums = {"operation_runs.kind": SimpleNamespace(READ_FILE=3),
             "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2)}
    namespace = {"__package__": "camctl.persistence.repositories", "enum_for": lambda name: enums[name],
                 "closing": closing, "ConsistencyError": ValueError,
                 "read_attempt_budget_available": lambda connection, copy_id, maximum:
                     (9, status, used, waiting) if status in (1, 2) and used < maximum else None,
                 "RetryWaitReads": lambda connection: SimpleNamespace(
                     completed=lambda run, count: completed and not blocked,
                     read_eligible=lambda run, now: eligible)}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    assert namespace["held_read_ready_for_availability"](Connection(), 7, "camera", 100, 2) is expected
