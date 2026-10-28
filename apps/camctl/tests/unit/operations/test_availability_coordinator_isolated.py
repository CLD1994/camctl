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
         record_call_outcome=lambda *args, **kwargs: None,
         record_availability_error=lambda *args, **kwargs: None)
    class RunOutcome(Enum):
        SUCCEEDED = 1
        FAILED = 2
        CANCELED = 3
        EXPIRED = 4

    class AttemptStatus(Enum):
        SUCCEEDED = 1
        FAILED = 2
        UNKNOWN = 3

    class EffectState(Enum):
        NO_EFFECT = 1
        UNKNOWN = 2

    class SettlementBasis(Enum):
        NOT_DISPATCHED = 1
        ASSUMED = 2

    class Fields:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.__dict__.update(kwargs)

    class Finish(Fields):
        def __init__(self, ticket, outcome, occurred_at, *, retry_wait=False, run_finish=None,
                     recovery_max_event_id=None):
            super().__init__(ticket=ticket, outcome=outcome, occurred_at=occurred_at,
                             retry_wait=retry_wait, run_finish=run_finish, recovery_max_event_id=recovery_max_event_id)

    class RunFinish:
        def __init__(self, status, error=None):
            self.status, self.error = status, error

    class Owner:
        def __init__(self, ticket, invoke, save, cancelled_result):
            self.ticket, self.invoke, self.save, self.cancelled_result = ticket, invoke, save, cancelled_result

    class Ticket:
        def __init__(self, attempt_id, operation, target_id, responsibility_key, run_id):
            self.attempt_id, self.operation, self.target_id = attempt_id, operation, target_id
            self.responsibility_key, self.run_id = responsibility_key, run_id

    class Gate:
        def __init__(self):
            self.established_calls, self.cleared_calls = [], []

        def established(self, key, when):
            self.established_calls.append((key, when))

        def cleared(self, key):
            self.cleared_calls.append(key)

    stub("camctl.operations.attempts", AttemptFinish=Finish, RunFinish=RunFinish, RunOutcome=RunOutcome, RetryWaitGate=Gate,
         **{name: type(name, (), {}) for name in ("AttemptIntent", "AttemptTarget", "OperationKind")})
    stub("camctl.operations.availability_owner", AvailabilityAttemptOwner=Owner)
    stub("camctl.operations.models", AttemptStatus=AttemptStatus, EffectState=EffectState, SettlementBasis=SettlementBasis,
         AttemptTicket=Ticket, **{name: Fields for name in ("CallOutcome", "ErrorValue", "EvidenceValue", "Settlement")})
    stub("camctl.operations.validation", validate_outcome=lambda ticket, outcome, evidence: SimpleNamespace(outcome=outcome))

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
    stub("camctl.persistence.repositories.operations", OperationRepository=type(
        "Repository", (), {"finish_attempt": lambda self, *args: None}))
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


@pytest.mark.asyncio
async def test_last_failed_attempt_finishes_without_retry_wait(coordinator_module, monkeypatch):
    module, _ = coordinator_module
    coordinator = make_coordinator(module)
    coordinator._write = AsyncMock()
    monkeypatch.setattr(module, "_action_facts", lambda owned, action_id: {
        "cancel_requested": 0, "status": 1, "scheduled_at": 100, "max_delay_ms": 10})
    driver_module = ModuleType("camctl.devices.drivers.hasselblad.driver")
    driver_module.AvailabilityCancelled = type("AvailabilityCancelled", (asyncio.CancelledError,), {})
    intent_module = ModuleType("camctl.persistence.repositories.availability_intent")
    intent_module.recheck_availability_dispatch = lambda *args: SimpleNamespace(allowed=False, reason="device_busy")
    release_module = ModuleType("camctl.persistence.repositories.availability_release")
    release_module.finish_availability = object()
    for item in (driver_module, intent_module, release_module):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    coordinator.operations.finish_attempt = object()
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=None))))
    ticket = SimpleNamespace(attempt_id=2, run_id=7)
    request = SimpleNamespace(action_id=1, trigger_copy_id=None)
    config = SimpleNamespace(max_attempts=2)
    owner = coordinator._attempt_owner(database, ticket, request, config, None, None,
                                       SimpleNamespace(device_id="camera"), 9)
    outcome = module.CallOutcome(status=module.AttemptStatus.FAILED, effect=module.EffectState.NO_EFFECT,
        error=module.ErrorValue("device_error", "availability"), settlement=object())

    await owner.save(outcome, object())

    finish = coordinator._write.await_args.args[2]
    assert finish.run_finish.status is module.RunOutcome.FAILED
    assert finish.retry_wait is False


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_id", [None, 9])
async def test_successful_check_delivers_original_result_to_release_transaction(coordinator_module, monkeypatch, copy_id):
    module, _ = coordinator_module
    coordinator = make_coordinator(module)
    coordinator._write = AsyncMock()
    driver_module = ModuleType("camctl.devices.drivers.hasselblad.driver")
    driver_module.AvailabilityCancelled = type("AvailabilityCancelled", (asyncio.CancelledError,), {})
    intent_module = ModuleType("camctl.persistence.repositories.availability_intent")
    intent_module.recheck_availability_dispatch = lambda *args: SimpleNamespace(allowed=True)
    release_module = ModuleType("camctl.persistence.repositories.availability_release")
    release_module.finish_availability = object()
    for item in (driver_module, intent_module, release_module):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(SimpleNamespace(connection=None))))
    ticket = SimpleNamespace(attempt_id=1, run_id=7)
    request = SimpleNamespace(action_id=1, trigger_copy_id=copy_id,
        identity=SimpleNamespace(serial="original", product_id=10), occurred_at=100, trusted_wall_now=100)
    # replace() 需要正式请求的不可变值对象。
    @dataclass(frozen=True)
    class CheckRequest:
        action_id: int
        trigger_copy_id: int | None
        identity: object
        occurred_at: int
        trusted_wall_now: int
    request = CheckRequest(**vars(request))
    outcome = module.CallOutcome(status=module.AttemptStatus.SUCCEEDED, effect=module.EffectState.UNKNOWN, error=None)
    actual = SimpleNamespace(settled=True, to_device_result=lambda identity: SimpleNamespace(outcome=outcome, diagnostic_error=None))
    driver = SimpleNamespace(check_availability=AsyncMock(return_value=actual))
    binding = SimpleNamespace(device_id="camera")
    config = SimpleNamespace(max_attempts=2, timeout_s=5)
    owner = coordinator._attempt_owner(database, ticket, request, config, driver, None, binding, 9)

    observed = await owner.invoke()
    await owner.save(observed, object())

    driver.check_availability.assert_awaited_once_with(binding, expected_serial="original", expected_product_id=10, timeout_s=5)
    saved = coordinator._write.await_args.args
    assert saved[1] == (1, copy_id, "result", 7, 1)
    assert saved[2].ticket is ticket and saved[2].run_finish.status is module.RunOutcome.SUCCEEDED
    assert saved[3] is release_module.finish_availability


@pytest.mark.asyncio
@pytest.mark.parametrize("current_max,canceled,waiting_result", [(2, 0, "WAIT"), (2, 1, "FINISHED")])
async def test_recovery_saves_unknown_then_uses_current_budget_and_trigger(
        coordinator_module, monkeypatch, current_max, canceled, waiting_result):
    module, _ = coordinator_module
    _, waiting = install_repositories(monkeypatch)
    class Boundary(Enum):
        HOST_LOCAL_SETTLED = 1
        HOST_POWER_CYCLE = 2
    recovery = ModuleType("camctl.operations.recovery")
    recovery.RecoveryBoundary = Boundary
    monkeypatch.setitem(sys.modules, recovery.__name__, recovery)
    coordinator = make_coordinator(module)
    coordinator.recovery_boundary, coordinator.recovery_max_event_id = Boundary.HOST_LOCAL_SETTLED, 20
    coordinator.operations.finish_attempt = object()
    coordinator._write = AsyncMock(side_effect=[None, waiting[waiting_result]])
    monkeypatch.setattr(module, "_action_facts", lambda owned, action_id: {
        "cancel_requested": canceled, "status": 1, "scheduled_at": 100, "max_delay_ms": 10})
    database = SimpleNamespace(read=AsyncMock(side_effect=lambda callback: callback(None)))
    run = {"id": 7, "status": 2, "attempts_used": 1, "max_attempts_used": 1,
           "intent_event_id": 19, "responsibility_key": "availability/9"}
    request = Request(1, None)

    await coordinator._recover_attempt(database, run, 9, request, None, None,
                                       SimpleNamespace(max_attempts=current_max))

    finish = coordinator._write.await_args_list[0].args[2]
    assert finish.outcome.outcome.status is module.AttemptStatus.UNKNOWN
    assert finish.run_finish is None and finish.recovery_max_event_id == 20
    assert coordinator._write.await_args_list[1].args[2][-1] == current_max
