"""内部设备源读取的 UNKNOWN 预算耗尽入口；不加载 camctl 运行模块。"""

import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[3] / "src" / "camctl"
_SAVED = SimpleNamespace(value="saved")
_GRANTED = object()


class _BlockCamctlImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"隔离测试禁止导入真实 camctl 模块: {fullname}")
        return None


if any(name == "camctl" or name.startswith("camctl.") for name in sys.modules):
    raise AssertionError("隔离测试启动前已加载真实 camctl 模块")
sys.meta_path.insert(0, _BlockCamctlImports())


class _StripImports(ast.NodeTransformer):
    def visit_Import(self, node):
        return None

    def visit_ImportFrom(self, node):
        return None


def _load_function(path, name, namespace):
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name)
    isolated = _StripImports().visit(function)
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        isolated,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Copies:
    def __init__(self):
        self.grants = 0

    async def qualify(self, _candidate):
        return SimpleNamespace(disposition=_SAVED, value=SimpleNamespace(
            outcome=_GRANTED, copy_id=9))

    async def copy_state(self, copy_id):
        assert copy_id == 9
        return SimpleNamespace(round=2, verification_state=1, target_sha256=None)

    async def _grant(self, *_args):
        self.grants += 1
        raise AssertionError("UNKNOWN 预算耗尽前不应申请相机读取机会")

    @property
    def repository(self):
        return SimpleNamespace(grant_read_slot=self._grant)


def _input_types():
    class Phase:
        WAITING = "waiting"

    def step(phase, *, copy_id=None, reason=None, **_kwargs):
        return SimpleNamespace(phase=phase, copy_id=copy_id, reason=reason)

    return Phase, step


def _entry_namespace():
    phase, input_step = _input_types()
    return {
        "InputPhase": phase,
        "InputStep": input_step,
        "SaveDisposition": SimpleNamespace(SAVED=_SAVED),
        "QualificationOutcome": SimpleNamespace(GRANTED=_GRANTED),
        "OperationConfig": lambda *args: args,
        "AttemptConfig": lambda *args: args,
        "_RUN_STATUS": SimpleNamespace(FAILED=4),
        "FileCandidate": lambda *args: args,
        "_copy_candidate": lambda *args: args,
        "_ensure_checksum_support": _async_noop,
        "_exhaust_unknown_read_before_slot": _budget_exhausted,
        "_read_all": _async_empty,
        "_read_one": _async_run_state,
        "_run_state": _async_segment_run_state,
        "resume_media_saves": _async_noop,
        "retry_read_business": _async_noop,
        "retry_read_ends": _async_noop,
        "load_processing_status": lambda *_args: object(),
        "_fail_internal_read_input": _async_noop,
        "replace": lambda flow, **_changes: flow,
    }


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_empty(*_args, **_kwargs):
    return []


async def _async_run_state(*_args, **_kwargs):
    return (1, 0)


async def _async_segment_run_state(*_args, **_kwargs):
    return (1, 2, 1, 1, 0)


async def _budget_exhausted(*_args, **_kwargs):
    return SimpleNamespace(reason="budget_exhausted")


class UnknownReadExhaustionTests(unittest.TestCase):
    def test_media_entry_finishes_exhausted_unknown_before_slot_grant(self):
        namespace = _entry_namespace()
        run_recording_media = _load_function(
            SOURCE / "capture" / "media_flow.py", "run_recording_media", namespace)
        copies = _Copies()
        flow = SimpleNamespace(
            database=SimpleNamespace(read=lambda callback, **_kwargs: _call(callback)),
            pending_media_results={}, pending_read_results={}, file_executor=None,
            max_read_attempts=1, read_idle_timeout_s=5, retry_interval_s=0,
            occurred_at=lambda: 100, digest_for=None, digest=None, segment_size=1024,
            roots=SimpleNamespace(staging=Path("staging")), sessions=object(),
            copies=lambda: copies,
        )
        result = asyncio.run(run_recording_media(flow, 21, 1, 30))
        self.assertEqual(result.phase, "waiting")
        self.assertEqual(result.reason, "run_ended")
        self.assertEqual(copies.grants, 0)

    def test_segment_entry_finishes_exhausted_unknown_before_slot_grant(self):
        namespace = _entry_namespace()
        obtain_input = _load_function(
            SOURCE / "capture" / "segment_inspections.py", "_obtain_input", namespace)
        copies = _Copies()
        flow = SimpleNamespace(
            max_read_attempts=1, read_idle_timeout_s=5, retry_interval_s=0,
            occurred_at=lambda: 100, copies=lambda: copies,
        )
        request = SimpleNamespace(action_id=21, budget=SimpleNamespace(
            max_attempts=1, timeout_s=5, retry_interval_s=0))
        result = asyncio.run(obtain_input(flow, request, 4, 30, "recording.mp4"))
        self.assertEqual(result.phase, "waiting")
        self.assertEqual(result.reason, "run_ended")
        self.assertEqual(copies.grants, 0)

    def test_only_unknown_attempt_at_current_limit_uses_original_attempt_identity(self):
        class Attempts:
            READ_FILE = 3

        class Rejected:
            REJECTED = "rejected"

        class BeginResult:
            def __init__(self, disposition, ticket=None, reason=None):
                self.disposition, self.ticket, self.reason = disposition, ticket, reason

        run_status = SimpleNamespace(PENDING=1, ACTIVE=2)
        attempt_status = SimpleNamespace(UNKNOWN=4)
        rows = [(2, 1, 88, 4)]
        calls = []

        class Connection:
            def execute(self, *_args):
                return _Cursor(rows)

        class Database:
            async def read(self, callback, **_kwargs):
                return callback(SimpleNamespace(connection=Connection()))

        def attempt_intent(*args, **kwargs):
            return SimpleNamespace(args=args, kwargs=kwargs,
                target=kwargs.get("target", args[3]))

        def exhaust(intent, attempt_id, key, owned):
            calls.append((intent, attempt_id, key, owned))
            return BeginResult(Rejected.REJECTED, reason="budget_exhausted")

        async def save_read_business(_flow, copy_id, operation, request, save):
            self.assertEqual((copy_id, operation), (9, "unknown_read_exhaustion"))
            return SimpleNamespace(value=save(request, "stable-operation-key", object()))

        namespace = {
            "enum_for": lambda name: {
                "operation_runs.kind": SimpleNamespace(READ_FILE=3),
                "operation_runs.status": run_status,
                "operation_attempts.status": attempt_status,
            }[name],
            "OperationKind": Attempts,
            "AttemptIntent": attempt_intent,
            "AttemptTarget": lambda **kwargs: SimpleNamespace(**kwargs),
            "BeginAttemptResult": BeginResult,
            "BeginDisposition": Rejected,
            "ConsistencyError": ValueError,
            "new_operation_key": lambda: "not-used",
            "save_read_business": save_read_business,
        }
        exhaust_unknown = _load_function(
            SOURCE / "capture" / "media_flow.py",
            "_exhaust_unknown_read_before_slot", namespace)
        flow = SimpleNamespace(database=Database(),
            operations=SimpleNamespace(exhaust_blocked_read=exhaust),
            occurred_at=lambda: 100)

        class Config:
            max_attempts = 1

        result = asyncio.run(exhaust_unknown(
            flow, action_id=21, copy_id=9, copy_round=2, config=Config()))
        self.assertEqual(result.reason, "budget_exhausted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], (88, "stable-operation-key"))
        self.assertEqual(calls[0][0].target.copy_id, 9)

        calls.clear()
        rows[:] = [(2, 1, 88, 1)]
        result = asyncio.run(exhaust_unknown(
            flow, action_id=21, copy_id=9, copy_round=2, config=Config()))
        self.assertIsNone(result)
        self.assertEqual(calls, [])


async def _call(callback):
    return callback(SimpleNamespace())
