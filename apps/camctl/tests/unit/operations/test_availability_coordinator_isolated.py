"""协调器的事务交接与异步候选发现不依赖真实协作者。"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import itertools
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl import resources


@pytest.fixture
def coordinator_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    modules = {}
    def stub(name, **members):
        module = ModuleType(name)
        module.__dict__.update(members)
        modules[name] = module

    keys = itertools.count(1)
    stub("camctl.contracts.values", ConsistencyError=ValueError, new_operation_key=lambda: next(keys))
    stub("camctl.logging_runtime.diagnostics", DiagnosticState=type("DiagnosticState", (), {}),
         record_call_outcome=lambda *args, **kwargs: None)
    stub("camctl.operations.attempts", **{name: type(name, (), {}) for name in
        ("AttemptFinish", "AttemptIntent", "AttemptTarget", "OperationKind", "RetryWaitGate", "RunFinish", "RunOutcome")})
    stub("camctl.operations.availability_owner", AvailabilityAttemptOwner=object)
    stub("camctl.operations.models", **{name: type(name, (), {}) for name in
        ("AttemptStatus", "AttemptTicket", "CallOutcome", "EffectState", "ErrorValue", "EvidenceValue", "Settlement", "SettlementBasis")})
    stub("camctl.operations.validation", validate_outcome=lambda *args: None)

    class SharedSave:
        def __init__(self):
            self.task = None

        async def run(self, callback):
            if self.task is None:
                self.task = asyncio.create_task(callback())
            return await asyncio.shield(self.task)

        def retry_failed(self):
            if self.task is not None and self.task.done() and self.task.exception():
                self.task = None

    class OutcomeKind(Enum):
        COMPLETED = 1
        UNKNOWN = 2

    stub("camctl.persistence.business", SharedSave=SharedSave)
    stub("camctl.persistence.models", DbOutcomeKind=OutcomeKind,
         db_failure=lambda kind, message, receipt: RuntimeError(message), db_outcome_for=lambda error: None)
    stub("camctl.persistence.repositories.operations", OperationRepository=type("Repository", (), {}))
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    source = find_spec("camctl.operations.availability")
    spec = spec_from_file_location("camctl.operations._isolated_availability", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, OutcomeKind


def make_coordinator(module):
    return module.AvailabilityCoordinator(monotonic_ns=lambda: 0, occurred_at=lambda: 100,
        recovery_boundary=object(), recovery_max_event_id=None)


@dataclass(frozen=True)
class Request:
    action_id: int
    trigger_copy_id: int | None


@pytest.mark.asyncio
async def test_unknown_write_retries_original_request_and_key(coordinator_module):
    module, kind = coordinator_module
    coordinator = make_coordinator(module)
    receipt = [SimpleNamespace(kind=kind.UNKNOWN), SimpleNamespace(kind=kind.COMPLETED, value="saved")]
    seen = []

    async def write(callback, *, key):
        seen.append((key, callback("owned")))
        return receipt.pop(0)

    database = SimpleNamespace(write=write)
    callback = lambda request, key, owned: (request, key, owned)
    original = object()
    with pytest.raises(RuntimeError):
        await coordinator._write(database, (1, "create"), original, callback)
    assert await coordinator._write(database, (1, "create"), object(), callback) == "saved"
    assert seen[0] == seen[1]
    assert seen[0][1][0] is original


@pytest.mark.asyncio
async def test_repeated_candidate_shares_in_flight_advancement(coordinator_module):
    module, _ = coordinator_module
    coordinator = make_coordinator(module)
    gate = asyncio.Event()

    async def advance(*args):
        await gate.wait()
        return module.AvailabilityProgress.READY

    coordinator.advance = AsyncMock(side_effect=advance)
    request = SimpleNamespace(action_id=1, trigger_copy_id=2)
    assert await coordinator.request(None, request, None, None, None, None) is module.AvailabilityProgress.WAIT
    assert await coordinator.request(None, request, None, None, None, None) is module.AvailabilityProgress.WAIT
    coordinator.advance.assert_awaited_once()
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await coordinator.request(None, request, None, None, None, None) is module.AvailabilityProgress.READY


def install_repositories(monkeypatch):
    class CreateDisposition(Enum):
        CREATED = 1
        REUSED = 2
        WAIT = 3
        NOT_REQUIRED = 4
        IDENTITY_MISMATCH = 5

    class ResolveDisposition(Enum):
        SUCCEEDED = 1
        WAIT = 2
        TERMINAL = 3

    class WaitingFinishDisposition(Enum):
        FINISHED = 1
        WAIT = 2
        RESOLVED = 3
        TERMINAL = 4

    for name, members in (
        ("availability_create", {"CreateDisposition": CreateDisposition, "ensure_check": object()}),
        ("availability_release", {"ResolveDisposition": ResolveDisposition, "finish_if_resolved": object()}),
        ("availability_wait_finish", {"WaitingFinishDisposition": WaitingFinishDisposition,
                                     "finish_waiting_availability": object()}),
    ):
        full = "camctl.persistence.repositories." + name
        stub = ModuleType(full)
        stub.__dict__.update(members)
        monkeypatch.setitem(sys.modules, full, stub)
    return ResolveDisposition, WaitingFinishDisposition


@pytest.mark.asyncio
async def test_exhausted_wait_uses_terminal_transaction_before_new_attempt(coordinator_module, monkeypatch):
    module, _ = coordinator_module
    resolve, waiting = install_repositories(monkeypatch)
    coordinator = make_coordinator(module)
    monkeypatch.setattr(module, "_open_check", lambda owned, request: (9, 7, None))
    monkeypatch.setattr(module, "_run_facts", lambda owned, run_id: {
        "status": 2, "running_attempt": None, "attempts_used": 2})
    coordinator._write = AsyncMock(side_effect=[resolve.WAIT, waiting.FINISHED])
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(None)))
    request = Request(action_id=1, trigger_copy_id=None)

    result = await coordinator.advance(database, request, SimpleNamespace(max_attempts=2), None, None, None)

    assert result is module.AvailabilityProgress.WAIT
    assert [call.args[1][-1] for call in coordinator._write.await_args_list] == ["resolve", "finish-wait"]


@pytest.mark.asyncio
async def test_terminal_failure_is_delivered_to_original_read(coordinator_module, monkeypatch):
    module, _ = coordinator_module
    resolve, _ = install_repositories(monkeypatch)
    coordinator = make_coordinator(module)
    monkeypatch.setattr(module, "_open_check", lambda owned, request: (9, 7, 5))
    monkeypatch.setattr(module, "_run_facts", lambda owned, run_id: {"status": 4})
    coordinator._write = AsyncMock(return_value=resolve.TERMINAL)
    coordinator._deliver_failure = AsyncMock()
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(None)))
    request = Request(action_id=1, trigger_copy_id=8)

    result = await coordinator.advance(database, request, None, None, None, None)

    assert result is module.AvailabilityProgress.FAILED
    coordinator._deliver_failure.assert_awaited_once_with(database, (1, 8), 9)
