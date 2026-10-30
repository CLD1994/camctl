"""公开批次仓储及其业务守卫，事务和事实端口均为内存替身。"""

from dataclasses import dataclass
from enum import Enum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.contracts.values import OperationKey

_KEY = OperationKey("a" * 32)
from camctl.persistence.repositories.file_batch_reads import FileBatchReads


@pytest.fixture
def world(monkeypatch):
    from camctl import resources
    from camctl.contracts import enums
    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("禁止读取包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("禁止读取包资源"))
    state = SimpleNamespace(holder=None, ready=True, first=1, busy=False, finished=False,
        saved=None, prior=None, plans=[], guards={},
        record={"id": 7, "action_id": 1, "device_id": "camera", "acquired_event_id": 50, "released_event_id": None})
    connection = Mock()
    connection.execute.side_effect = lambda *_: SimpleNamespace(fetchone=lambda: state.prior)
    scope = SimpleNamespace(connection=connection,
        allocate=Mock(return_value=SimpleNamespace(first_event_id=100, txn_id=20)))

    class Facts(FileBatchReads):
        def supported(self): return True
        def action(self, action_id): return {"id": action_id, "type": 4}
        def holder(self, device_id): return state.holder
        def ready(self, action_id, device_id, now): return state.ready
        def first(self, device_id, now): return state.first
        def busy(self, action_id, device_id, now): return state.busy
        def finished(self, action_id, device_id): return state.finished

    class Kind(Enum):
        COMPLETED = "completed"
        ROLLED_BACK = "rolled_back"
        UNKNOWN = "unknown"

    @dataclass
    class Outcome:
        kind: object
        value: object = None
        error: object = None
        boundary: object = None

    def plan(events, owners, state_rows, result=None, read_only=False):
        return SimpleNamespace(events=events, owners=owners, state_rows=state_rows, result=result, read_only=read_only)

    def create(table, identity, values):
        return SimpleNamespace(table=table, row_id=identity,
            before=SimpleNamespace(exists=False, values={}),
            after=SimpleNamespace(exists=True, values=values))

    def update(table, identity, before, after):
        row = create(table, identity, after)
        row.before = SimpleNamespace(exists=True, values=before)
        return row

    def commit(command, key, owned):
        assert owned == "owned" and key == _KEY
        try:
            value = command.plan(scope)
            state.plans.append(value)
            return SimpleNamespace(kind="completed", result=value.result, boundary=None)
        except ValueError as error:
            return SimpleNamespace(kind="rolled_back", error=error)

    transaction = ModuleType("camctl.persistence.transaction")
    for name, value in {"CommandPlan": plan, "commit_operation": commit,
        "event_envelope": lambda event_id, txn_id, event_type, reason, rows, at: SimpleNamespace(
            event_id=event_id, transaction_id=txn_id, event_type=event_type, reason=reason, rows=rows, occurred_at=at),
        "next_row_id": lambda *_: 7, "row_change": create, "update_change": update,
        "row_facts": lambda *_: dict(state.record), "saved_transaction_events": lambda *_: state.saved}.items():
        setattr(transaction, name, value)
    registry = ModuleType("camctl.history.events")
    registry.load_event_registry = lambda: {"events": {"FILE_BATCH_CHANGED": {
        "id": 140, "branches": {"GRANT": {"reason": 17}, "RELEASE": {"reason": 18}}}}}
    validators = ModuleType("camctl.history.validators")
    validators.EventValidationError = ValueError
    validators.register_guard = lambda name, guard: state.guards.update({name: guard})
    models = ModuleType("camctl.persistence.models")
    models.DbOutcome, models.DbOutcomeKind = Outcome, Kind
    models.db_failure = lambda error_type, message, outcome: error_type(message)
    models.db_outcome_for = lambda error: getattr(error, "actual", None)
    for module in (transaction, registry, validators, models):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    source = find_spec("camctl.persistence.repositories.file_batches")
    spec = spec_from_file_location("camctl.persistence.repositories._file_batches_unit", source.origin)
    api = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.file_batches", api)
    monkeypatch.setattr("camctl.contracts.values.new_operation_key", lambda: _KEY)
    monkeypatch.setattr(api, "FileBatchReads", Facts)
    api.register_file_batch_guards()
    state.repo, state.scope, state.reads = api.FileBatchRepository(), scope, Facts(connection)
    state.request, state.kinds = BatchRequest(1, "camera", 10), Kind
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_canceled_waiter_accepts_confirmed_result_and_preserves_unknown(world, completed):
    import asyncio
    from camctl.outputs.batches import acquire_file_batch
    from camctl.persistence.models import DbOutcome
    pending = {}
    original = asyncio.CancelledError("原等待取消")
    original.actual = DbOutcome(world.kinds.COMPLETED if completed else world.kinds.UNKNOWN,
        BatchOutcome.GRANTED if completed else None)

    async def write(save, *, key):
        raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        await acquire_file_batch(SimpleNamespace(write=write), world.request, pending=pending)
    assert caught.value is original
    assert bool(pending) is not completed


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["UNKNOWN", "ROLLED_BACK"])
async def test_acquire_retains_original_request_until_result_is_confirmed(world, failed):
    from camctl.outputs.batches import acquire_file_batch
    from camctl.persistence.models import DbOutcome
    pending, attempts = {}, []

    async def write(save, *, key):
        attempts.append(key)
        if len(attempts) == 1:
            return DbOutcome(getattr(world.kinds, failed))
        return save("owned")

    database = SimpleNamespace(write=write)
    with pytest.raises(ValueError):
        await acquire_file_batch(database, world.request, pending=pending)
    assert pending == {(1, "camera", False): (world.request, _KEY)}
    assert await acquire_file_batch(database, BatchRequest(1, "camera", 99), pending=pending)
    assert attempts == [_KEY, _KEY] and not pending
    assert world.plans[-1].events[0].occurred_at == 10


@pytest.mark.asyncio
async def test_release_retries_original_save_even_when_holder_disappears(world):
    from camctl.outputs.batches import retry_file_batches
    from camctl.persistence.models import DbOutcome
    world.holder, world.finished = (7, 1), True
    pending = {(1, "camera", True): (world.request, _KEY)}
    saved = []

    async def write(save, *, key):
        outcome = save("owned")
        saved.append((key, outcome))
        return DbOutcome(world.kinds.UNKNOWN)

    database = SimpleNamespace(write=write)
    with pytest.raises(ValueError):
        await retry_file_batches(database, pending)
    event = world.plans[-1].events[0]
    world.saved = [{"type": event.event_type, "reason": event.reason,
        "occurred_at": event.occurred_at, "body": {"rows": [{"id": 7,
        "after": {"values": event.rows[0].after.values}}]}}]
    world.record["released_event_id"] = 100
    world.holder = None

    async def confirmed(save, *, key):
        return save("owned")

    database.write = confirmed
    await retry_file_batches(database, pending)
    assert not pending and world.plans[-1].read_only
    assert world.plans[-1].result is BatchOutcome.RELEASED


def test_acquire_saves_one_batch_and_its_original_event_reference(world):
    result = world.repo.acquire(world.request, _KEY, "owned")
    assert result.kind is world.kinds.COMPLETED and result.value is BatchOutcome.GRANTED
    event = world.plans[-1].events[0]
    assert (event.event_type, event.reason) == (140, 17)
    assert event.rows[0].after.values == {"action_id": 1, "device_id": "camera",
        "acquired_event_id": 100, "released_event_id": None}
    assert world.plans[-1].owners == {("device_file_batches", 7): ("action", 1)}


@pytest.mark.parametrize("holder,ready,first,expected", [
    (None, False, 1, BatchOutcome.WAIT),
    (None, True, 2, BatchOutcome.WAIT),
    ((7, 2), True, 1, BatchOutcome.WAIT),
    ((7, 1), False, 2, BatchOutcome.HELD),
])
def test_waiting_and_existing_owner_never_create_new_events(world, holder, ready, first, expected):
    world.holder, world.ready, world.first = holder, ready, first
    result = world.repo.acquire(world.request, _KEY, "owned")
    assert result.value is expected
    assert world.plans[-1].read_only and world.plans[-1].events == ()
    world.scope.allocate.assert_not_called()


@pytest.mark.parametrize("finished,expected", [(False, BatchOutcome.HELD), (True, BatchOutcome.RELEASED)])
def test_release_waits_for_complete_original_responsibility(world, finished, expected):
    world.holder, world.finished = (7, 1), finished
    result = world.repo.release(world.request, _KEY, "owned")
    assert result.value is expected
    if finished:
        event = world.plans[-1].events[0]
        assert event.reason == 18
        assert event.rows[0].after.values == {"released_event_id": 100}
    else:
        assert world.plans[-1].events == ()


def test_finished_batch_cannot_be_reopened_by_late_candidate(world):
    world.prior = (80,)
    assert world.repo.acquire(world.request, _KEY, "owned").value is BatchOutcome.FINISHED
    world.scope.allocate.assert_not_called()


def test_guard_rechecks_original_facts_instead_of_accepting_grant_body(world):
    world.repo.acquire(world.request, _KEY, "owned")
    event = world.plans[-1].events[0]
    world.ready = False
    with pytest.raises(ValueError):
        world.guards["file_batch"](event, SimpleNamespace(file_batch_reads=world.reads))


def test_guard_rejects_release_when_query_still_needs_settlement(world):
    world.holder, world.finished = (7, 1), True
    world.repo.release(world.request, _KEY, "owned")
    event = world.plans[-1].events[0]
    world.finished = False
    with pytest.raises(ValueError):
        world.guards["file_batch"](event, SimpleNamespace(file_batch_reads=world.reads))


@pytest.mark.parametrize("changed_time", [False, True])
def test_original_key_reuses_only_identical_request(world, changed_time):
    world.repo.acquire(world.request, _KEY, "owned")
    event = world.plans[-1].events[0]
    values = event.rows[0].after.values
    world.record.update(values)
    world.saved = [{"type": event.event_type, "reason": event.reason,
        "occurred_at": event.occurred_at, "body": {"rows": [{"id": 7, "after": {"values": values}}]}}]
    request = BatchRequest(1, "camera", 11 if changed_time else 10)
    result = world.repo.acquire(request, _KEY, "owned")
    if changed_time:
        assert result.kind is world.kinds.ROLLED_BACK
        assert isinstance(result.error, ValueError)
    else:
        assert result.kind is world.kinds.COMPLETED and result.value is BatchOutcome.GRANTED
        assert world.plans[-1].read_only and world.plans[-1].events == ()
    world.scope.allocate.assert_called_once_with(1)
