"""检查原调用的公共诊断消费者，隔离登记、通道和包资源。"""

import ast
import asyncio
import importlib.abc
import importlib.util
import json
import sys
import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "logging_runtime" / "diagnostics.py"


@dataclass(frozen=True)
class AttemptTicket:
    run_id: int
    attempt_id: int
    operation: str


@dataclass(frozen=True)
class ErrorValue:
    code: str
    stage: str
    details: dict


@dataclass(frozen=True)
class ToolStream:
    state: str
    data: bytes | None


@dataclass(frozen=True)
class ToolCall:
    name: str
    stdout: ToolStream
    stderr: ToolStream


@dataclass(frozen=True)
class ToolDiagnostics:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class RawToolOutcome:
    output: bytes | None
    stderr: bytes | None
    exit: object


@dataclass(frozen=True)
class ActualStep:
    name: str
    returned_monotonic_ns: int
    outcome: RawToolOutcome | None
    error: BaseException | None = None


@dataclass(frozen=True)
class CallOutcome:
    error: ErrorValue | None
    diagnostics: ToolDiagnostics | None


@dataclass(frozen=True)
class LogRecord:
    level: object
    message: str
    fallback: object = None
    structured: bool = False


class LogReceipt:
    def __init__(self):
        self.admission_result = None
        self.callbacks = []

    def add_admission_callback(self, callback):
        self.callbacks.append(callback)

    def complete_admission(self, result):
        self.admission_result = result
        for callback in self.callbacks:
            callback(result)

    def complete_write(self, result):
        pass

    async def wait_admission(self):
        return self.admission_result


class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"禁止导入真实包: {fullname}")
        return None


def diagnostic_module():
    modules = {}
    def stub(name, **values):
        module = ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module
    class AdmissionKind(Enum):
        ACCEPTED = "accepted"
        FALLBACK_STDERR = "fallback"
    class LogLevel(Enum):
        ERROR = "error"
    def encode(diagnostics):
        def stream(value):
            return {"state": value.state, "hex": None if value.data is None else value.data.hex()}
        return {"calls": [{"name": call.name, "stdout": stream(call.stdout), "stderr": stream(call.stderr)}
                          for call in diagnostics.calls]}
    stub("camctl.logging_runtime.admission", AdmissionDecision=lambda kind, level: SimpleNamespace(kind=kind, level=level),
         AdmissionKind=AdmissionKind)
    stub("camctl.logging_runtime.models", LogLevel=LogLevel, LogWriteResult=type("LogWriteResult", (), {}))
    stub("camctl.logging_runtime.receipt", LogReceipt=LogReceipt)
    stub("camctl.logging_runtime.service", AdmissionResult=lambda decision, *args, **values: SimpleNamespace(decision=decision, **values),
         LogChannel=type("LogChannel", (), {}), LogRecord=LogRecord, admission_result_for=lambda error: None)
    stub("camctl.logging_runtime.formatting", log_metadata=lambda level: {})
    stub("camctl.operations.error_diagnostics", DiagnosticError=type("DiagnosticError", (Exception,), {}),
         exception_diagnostic=lambda error, **kwargs: {"type": type(error).__name__, "message": str(error)})
    stub("camctl.operations.models", AttemptTicket=AttemptTicket, CallOutcome=CallOutcome, ErrorValue=ErrorValue)
    stub("camctl.operations.process", RawToolOutcome=RawToolOutcome)
    stub("camctl.operations.result_format", error_document=lambda error: {
        "code": error.code, "stage": error.stage, "details": error.details})
    stub("camctl.operations.tool_diagnostics", ToolDiagnostics=ToolDiagnostics, ToolStream=ToolStream,
         encode_diagnostics=encode)
    spec = importlib.util.spec_from_file_location("isolated_h4_diagnostics", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {spec.name: module, **modules}), patch.object(sys, "meta_path", [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(module)
    return module, AdmissionKind


class DiagnosticConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_channel_falls_back_once_with_original_tool_bytes(self):
        module, _ = diagnostic_module()
        messages = []
        class FullChannel:
            def submit(self, record, *, receipt):
                raise RuntimeError("channel full")
        error = RuntimeError("transport failed")
        ticket = AttemptTicket(7, 2, "availability")
        streams = ToolDiagnostics((ToolCall("doctor", ToolStream("unconfirmed", b"partial"),
                                             ToolStream("complete", b"error")),))
        state = module.DiagnosticState()
        with module.error_diagnostics_context(FullChannel(), messages.append):
            for _ in range(2):
                module.record_availability_error(error, ticket=ticket, diagnostics=streams, state=state)
            await module.flush_diagnostics()
        self.assertEqual(len(messages), 1)
        document = json.loads(messages[0])
        self.assertEqual(bytes.fromhex(document["diagnostics"]["calls"][0]["stdout"]["hex"]), b"partial")
        self.assertIs(module.diagnostic_state_for(error), state)

    async def test_unsettled_check_delivers_original_partial_streams_once(self):
        module, admission_kind = diagnostic_module()
        records = []
        class Channel:
            def submit(self, record, *, receipt):
                records.append(record)
                receipt.complete_admission(SimpleNamespace(decision=SimpleNamespace(kind=admission_kind.ACCEPTED)))
                return receipt
        streams = ToolDiagnostics((ToolCall("list", ToolStream("complete", b"old output"), ToolStream("complete", b"")),
            ToolCall("info", ToolStream("unconfirmed", b"partial"), ToolStream("unconfirmed", b"failure"))))
        error = RuntimeError("transport did not settle")
        ticket = AttemptTicket(7, 2, "availability")
        state = module.DiagnosticState()
        original_steps = (ActualStep("list", 10, RawToolOutcome(b"old output", b"", {"exit_code": 0})),
                          ActualStep("info", 20, None, error))
        with module.error_diagnostics_context(Channel(), lambda message: self.fail(message)):
            for _ in range(2):
                module.record_availability_error(error, ticket=ticket, diagnostics=streams,
                    details={"reason": "transport_failed", "calls": original_steps}, state=state)
            await module.flush_diagnostics()
        self.assertEqual(len(records), 1)
        document = json.loads(records[0].message)
        self.assertEqual(document["error"]["message"], "transport did not settle")
        self.assertEqual(document["details"]["reason"], "transport_failed")
        self.assertEqual(document["details"]["calls"][0]["outcome"]["exit"]["exit_code"], 0)
        self.assertEqual(document["details"]["calls"][1]["error"]["message"], "transport did not settle")
        calls = document["diagnostics"]["calls"]
        self.assertEqual([bytes.fromhex(call["stdout"]["hex"]) for call in calls], [b"old output", b"partial"])
        self.assertEqual([bytes.fromhex(call["stderr"]["hex"]) for call in calls], [b"", b"failure"])
        with self.assertRaises(ValueError):
            module.record_availability_error(error, ticket=AttemptTicket(7, 2, "read"), diagnostics=streams)

    async def test_failed_check_delivers_original_exception_and_complete_streams_once(self):
        module, admission_kind = diagnostic_module()
        records = []
        class Channel:
            def submit(self, record, *, receipt):
                records.append(record)
                receipt.complete_admission(SimpleNamespace(decision=SimpleNamespace(kind=admission_kind.ACCEPTED)))
                return receipt
        streams = ToolDiagnostics((ToolCall("list", ToolStream("complete", b"list ok"), ToolStream("complete", b"")),
            ToolCall("info", ToolStream("complete", b"serial"), ToolStream("complete", b"info warning")),
            ToolCall("doctor", ToolStream("unconfirmed", b"partial out"), ToolStream("complete", b"failure\xff"))))
        error = ErrorValue("device_error", "availability", {"reason": "transport_failed"})
        outcome = CallOutcome(error, streams)
        ticket = AttemptTicket(7, 2, "availability")
        original_error = RuntimeError("original transport failure")
        state = module.DiagnosticState()
        with module.error_diagnostics_context(Channel(), lambda message: self.fail(message)):
            module.record_call_outcome(outcome, ticket=ticket, run_error=original_error,
                                       context={"action_id": 12}, state=state, include_tool_stdout=True)
            module.record_call_outcome(outcome, ticket=ticket, run_error=original_error,
                                       context={"action_id": 12}, state=state, include_tool_stdout=True)
            await module.flush_diagnostics()
        self.assertEqual(len(records), 1)
        document = json.loads(records[0].message)
        self.assertEqual(document["error"]["code"], "device_error")
        self.assertEqual(document["run_error"]["message"], "original transport failure")
        calls = document["outcome"]["diagnostics"]["calls"]
        self.assertEqual([bytes.fromhex(call["stdout"]["hex"]) for call in calls],
                         [b"list ok", b"serial", b"partial out"])
        self.assertEqual([bytes.fromhex(call["stderr"]["hex"]) for call in calls],
                         [b"", b"info warning", b"failure\xff"])
        self.assertIs(module.diagnostic_state_for(original_error), state)


class AvailabilityResultSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_diagnostic_precedes_failed_reads_and_result_reuses_finish(self):
        source = SOURCE.parents[1] / "operations" / "availability.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "availability_result_saver")
        program = ast.Module(body=[function], type_ignores=[])
        steps, writes = [], []
        class AttemptStatus(Enum):
            SUCCEEDED = "succeeded"
            FAILED = "failed"
        class RunOutcome(Enum):
            SUCCEEDED = "succeeded"
            FAILED = "failed"
            CANCELED = "canceled"
            EXPIRED = "expired"
        @dataclass(frozen=True)
        class AttemptFinish:
            ticket: object
            outcome: object
            occurred_at: int
            run_finish: object = None
            retry_wait: bool = False
        class Database:
            count = 0
            async def read(self, callback):
                self.count += 1
                steps.append("read")
                if self.count <= 2:
                    raise RuntimeError("database unavailable")
                return {"cancel_requested": 0, "status": 2, "scheduled_at": 10, "max_delay_ms": 100}
        async def write(database, identity, finish, callback):
            steps.append("write")
            writes.append((identity, finish))
            if len(writes) == 1:
                raise RuntimeError("receipt unknown")
        def record(outcome, **kwargs):
            steps.append(("diagnostic", outcome, kwargs))
        def record_unsettled(error, **kwargs):
            steps.append(("unsettled", error, kwargs))
        namespace = {"record_call_outcome": record, "CallOutcome": CallOutcome,
            "record_availability_error": record_unsettled, "ConsistencyError": RuntimeError,
            "validate_outcome": lambda ticket, outcome, evidence: outcome,
            "AttemptStatus": AttemptStatus, "AttemptFinish": AttemptFinish,
            "RunOutcome": RunOutcome, "RunFinish": lambda status, error=None: SimpleNamespace(status=status, error=error),
            "ErrorValue": ErrorValue, "_action_facts": lambda owned, action_id: None}
        exec(compile(ast.fix_missing_locations(program), str(source), "exec"), namespace)
        ticket = SimpleNamespace(run_id=7, attempt_id=1, responsibility_key="availability/31")
        request = SimpleNamespace(action_id=12, trigger_copy_id=None)
        diagnostic = object()
        original_error = RuntimeError("tool failed")
        outcome = SimpleNamespace(status=AttemptStatus.FAILED,
                                  error=ErrorValue("device_error", "availability", {"reason": "tool_failed"}))
        actual = SimpleNamespace(settled=True, to_device_result=lambda identity: SimpleNamespace(
            outcome=outcome, diagnostic_error=original_error))
        gate = SimpleNamespace(established=lambda *args: steps.append("gate"))
        save = namespace["availability_result_saver"](ticket=ticket, request=request,
            config=SimpleNamespace(max_attempts=2), evidence=object(), binding=SimpleNamespace(device_id="camera"),
            check_id=31, database=Database(), write=write, finish_availability=object(), finish_attempt=object(),
            occurred_at=lambda: 20, monotonic_ns=lambda: 30, retry_gate=gate)
        unsettled = SimpleNamespace(settled=False, reason="transport_failed",
            calls=(SimpleNamespace(error=original_error),), tool_diagnostics=lambda: "original streams")
        with self.assertRaisesRegex(RuntimeError, "尚未确认"):
            await save(unsettled, diagnostic)
        self.assertEqual(steps[0][0], "unsettled")
        self.assertIs(steps[0][1], original_error)
        self.assertEqual(steps[0][2]["diagnostics"], "original streams")
        self.assertEqual(steps[0][2]["details"]["calls"], unsettled.calls)
        steps.clear()
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await save(actual, diagnostic)
        self.assertEqual([step if isinstance(step, str) else step[0] for step in steps],
                         ["diagnostic", "read", "diagnostic", "read"])
        for step in (steps[0], steps[2]):
            self.assertIs(step[1], outcome)
            self.assertIs(step[2]["run_error"], original_error)
            self.assertIs(step[2]["state"], diagnostic)
            self.assertTrue(step[2]["include_tool_stdout"])
        with self.assertRaisesRegex(RuntimeError, "receipt unknown"):
            await save(actual, diagnostic)
        await save(actual, diagnostic)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][0], (12, None, "result", 7, 1))
        self.assertEqual(writes[1][0], writes[0][0])
        self.assertIs(writes[1][1], writes[0][1])


class AvailabilityOwnerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_unsettled_result_remains_owned_and_is_not_reinvoked(self):
        source = SOURCE.parents[1] / "operations" / "availability_owner.py"
        modules = {}
        def stub(name, **values):
            module = ModuleType(name)
            module.__dict__.update(values)
            modules[name] = module
        class SharedSave:
            def __init__(self): self.task = None
            async def run(self, callback):
                if self.task is None:
                    self.task = asyncio.create_task(callback())
                return await asyncio.shield(self.task)
            def retry_failed(self):
                if self.task is not None and self.task.done() and self.task.exception() is not None:
                    self.task = None
        stub("camctl.logging_runtime.diagnostics", DiagnosticState=lambda: object())
        stub("camctl.operations.owned_calls", owned_tool_call=lambda function: function)
        stub("camctl.persistence.business", SharedSave=SharedSave)
        spec = importlib.util.spec_from_file_location("isolated_h4_owner", source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module, **modules}), patch.object(
                sys, "meta_path", [BlockCamctl(), *sys.meta_path]):
            spec.loader.exec_module(module)
        started = asyncio.Event()
        calls, saves = [], []
        actual = SimpleNamespace(settled=False)
        async def invoke():
            calls.append("invoke")
            started.set()
            await asyncio.Event().wait()
        async def save(value, diagnostic):
            saves.append(value)
            raise RuntimeError("actual call unsettled")
        owner = module.AvailabilityAttemptOwner("original ticket", invoke, save, lambda error: actual)
        advance = asyncio.create_task(owner.advance())
        await started.wait()
        owner.stop()
        with self.assertRaisesRegex(RuntimeError, "unsettled"):
            await advance
        with self.assertRaisesRegex(RuntimeError, "unsettled"):
            await owner.settle()
        self.assertIs(owner.actual, actual)
        self.assertFalse(owner.saved)
        self.assertEqual(calls, ["invoke"])
        self.assertEqual(saves, [actual, actual])


if __name__ == "__main__":
    unittest.main()
