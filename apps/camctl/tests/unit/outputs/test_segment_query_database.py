"""显式来源查询的原保存责任跨异步数据库等待保持不变。"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.outputs import segment_query
from camctl.outputs.segment_query import PendingSegmentQuery
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database

pytestmark = pytest.mark.asyncio
KEY = OperationKey("a" * 32)


def runtime_for():
    owned = OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing"))
    return SimpleNamespace(database=inline_business_database(owned),
        operations=Mock(spec=OperationRepository))


async def test_cancel_during_result_save_applies_actual_receipt_before_raising(monkeypatch):
    runtime = runtime_for()
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    pending = PendingSegmentQuery(1, intent=object(), intent_key=KEY,
        ticket=object(), dispatched=True, finish=object(), finish_key=KEY)
    original_request = pending.finish
    started, release = asyncio.Event(), asyncio.Event()
    original_write = runtime.database.write

    async def pause(callback, *, key, description):
        started.set()
        await release.wait()
        return await original_write(callback, key=key, description=description)

    monkeypatch.setattr(runtime.database, "write", pause)
    task = asyncio.create_task(segment_query._save_result(runtime, pending))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending.finish is None
    assert pending.ticket is None
    assert pending.intent is None
    assert pending.dispatched is False
    assert runtime.operations.finish_attempt.call_args.args[:2] == (original_request, KEY)


async def test_unknown_business_save_preserves_original_request_and_key():
    runtime = runtime_for()
    original, replacement = object(), object()
    save = Mock(side_effect=[DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("lost")),
                             DbOutcome(DbOutcomeKind.COMPLETED)])
    pending = PendingSegmentQuery(1, business=(save, original, KEY))
    with pytest.raises(ConsistencyError):
        await segment_query._save(runtime, pending, save, replacement)
    assert pending.business == (save, original, KEY)
    await segment_query._save(runtime, pending, save, replacement)
    assert pending.business is None
    assert [call.args[:2] for call in save.call_args_list] == [(original, KEY), (original, KEY)]


async def test_target_owner_and_latest_are_read_in_one_callback(monkeypatch):
    runtime = runtime_for()
    active = False
    reads = 0
    original_read = runtime.database.read

    async def read(callback, *, description):
        nonlocal reads
        reads += 1
        def guarded(owned):
            nonlocal active
            active = True
            try:
                return callback(owned)
            finally:
                active = False
        return await original_read(guarded, description=description)

    def target(_connection, _item):
        assert active
        return {"status": 2}, {"action_id": 7}, {}, None, None

    def owner(*_args):
        assert active
        return {"status": 2, "cancel_requested": 0}

    def latest(*_args):
        assert active
        return None

    monkeypatch.setattr(runtime.database, "read", read)
    monkeypatch.setattr(segment_query, "_target", target)
    monkeypatch.setattr(segment_query, "row_facts", owner)
    monkeypatch.setattr(segment_query, "_latest", latest)
    assert await segment_query._advance_one(runtime, PendingSegmentQuery(1)) is True
    assert reads == 1


@pytest.mark.parametrize("initial_result", ("canceled", "unknown"))
async def test_original_unsent_intent_resumes_without_cold_recovery(monkeypatch, initial_result):
    from unittest.mock import AsyncMock
    from camctl.devices.bindings import DeviceBinding
    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginAttemptResult,
        BeginDisposition, OperationKind,
    )
    from camctl.operations.models import AttemptTicket

    runtime = runtime_for()
    config = AttemptConfig(2, timeout_s=1, retry_interval_s=1)
    intent = AttemptIntent("query", 7, OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(obtain_item_id=1), None, config, 1000)
    ticket = AttemptTicket(1, "query", "1", "obtain-exists/1", 8)
    pending = PendingSegmentQuery(1, intent=intent, intent_key=KEY)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket))
    started, release = asyncio.Event(), asyncio.Event()
    original_write = runtime.database.write

    async def pause(callback, *, key, description):
        started.set()
        await release.wait()
        return await original_write(callback, key=key, description=description)

    monkeypatch.setattr(runtime.database, "write", pause)
    if initial_result == "canceled":
        task = asyncio.create_task(segment_query._begin(runtime, pending))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pending.ticket == ticket
    else:
        release.set()
        runtime.operations.begin_attempt.side_effect = [
            DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("lost intent receipt")),
            DbOutcome(DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, ticket)),
        ]
        with pytest.raises(ConsistencyError):
            await segment_query._begin(runtime, pending)
        assert pending.ticket is None
    assert pending.intent is intent
    assert pending.intent_key == KEY
    assert pending.dispatched is False
    assert runtime.operations.begin_attempt.call_args.args[:2] == (intent, KEY)

    class QueryReached(Exception):
        pass

    async def query(request):
        assert request.ticket == ticket
        assert pending.dispatched is True
        raise QueryReached()

    last = (8, 2, 1, 0, 9, 1, 1, None, 100)
    binding = DeviceBinding("camera", "driver")
    facts = ({"status": 1}, {"action_id": 7}, {"id": 3, "availability": 5},
        {"id": 4, "identity_key": "file", "locator_json": {}}, binding,
        {"id": 7, "status": 2, "cancel_requested": 0}, last, None)
    monkeypatch.setattr(segment_query, "_read_facts", AsyncMock(return_value=facts))
    monkeypatch.setattr(segment_query, "_recover", AsyncMock(side_effect=AssertionError("cold recovery")))
    runtime.binding_check = None
    runtime.devices = {"camera": SimpleNamespace(query_config=config,
        query_driver=SimpleNamespace(query_file=query))}
    with pytest.raises(QueryReached):
        await segment_query._advance_one(runtime, pending)
    # 未取得可靠回执时核实同一原键；已可靠交付的票据直接进入原调用。
    calls = runtime.operations.begin_attempt.call_args_list
    assert len(calls) == (1 if initial_result == "canceled" else 2)
    assert all(call.args[:2] == (intent, KEY) for call in calls)


def dispatch_runtime(monkeypatch, *, registered=False, ticket_known=False):
    from unittest.mock import create_autospec
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.evidence import EvidenceRegistry
    from camctl.devices.ports import FileQueryDriver
    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginAttemptResult,
        BeginDisposition, OperationKind,
    )
    from camctl.operations.models import AttemptTicket

    runtime = runtime_for()
    config = AttemptConfig(2, timeout_s=1, retry_interval_s=1)
    ticket = AttemptTicket(1, "query", "1", "obtain-exists/1", 8)
    intent = AttemptIntent("query", 7, OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(obtain_item_id=1), None, config, 1000)
    pending = PendingSegmentQuery(1, intent=intent if registered else None,
        intent_key=KEY if registered else None,
        ticket=ticket if ticket_known else None)
    last = (8, 2, 1, 0, 9, 1, 1, None, 100)
    state = {"item_status": 1, "owner_status": 2, "canceled": 0,
             "last": last if registered else None, "callback": False}
    binding = DeviceBinding("camera", "driver")

    def target(_connection, item_id):
        assert state["callback"] and item_id == 1
        return ({"status": state["item_status"]}, {"action_id": 7},
            {"id": 3, "availability": 5},
            {"id": 4, "identity_key": "file", "locator_json": {}}, binding)

    def owner(_connection, table, identity):
        assert state["callback"] and (table, identity) == ("actions", 7)
        return {"id": 7, "status": state["owner_status"],
                "cancel_requested": state["canceled"]}

    def latest(_connection, item_id):
        assert state["callback"] and item_id == 1
        return state["last"]

    original_read = runtime.database.read

    async def read(callback, *, description):
        def guarded(owned):
            state["callback"] = True
            try:
                return callback(owned)
            finally:
                state["callback"] = False
        return await original_read(guarded, description=description)

    def begin(*_args):
        state["last"] = last
        return DbOutcome(DbOutcomeKind.COMPLETED,
            BeginAttemptResult(BeginDisposition.GRANTED, ticket))

    runtime.operations.begin_attempt.side_effect = begin
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime.binding_check = None
    runtime.occurred_at = lambda: 1200
    driver = create_autospec(FileQueryDriver, instance=True, spec_set=True)
    driver.query_file.side_effect = AssertionError("已失去资格的 QUERY 不得进入设备")
    runtime.devices = {"camera": SimpleNamespace(query_config=config,
        query_driver=driver, evidence=EvidenceRegistry(()))}
    monkeypatch.setattr(runtime.database, "read", read)
    monkeypatch.setattr(segment_query, "_target", target)
    monkeypatch.setattr(segment_query, "row_facts", owner)
    monkeypatch.setattr(segment_query, "_latest", latest)
    return runtime, pending, state, ticket, driver


def end_qualification(state, reason):
    if reason == "canceled":
        state["canceled"] = 1
    elif reason == "owner_ended":
        state["owner_status"] = 3
    elif reason == "item_ended":
        state["item_status"] = 3
    elif reason == "run_ended":
        state["last"] = (8, 5, 1, 0, 9, 1, 1, None, 100)
    else:
        raise AssertionError(reason)


@pytest.mark.parametrize("registered", [False, True], ids=["new", "verify-original-key"])
@pytest.mark.parametrize("reason", ["canceled", "owner_ended", "item_ended", "run_ended"])
async def test_begin_wait_rechecks_current_qualification_and_ends_original_unsent_query(
    monkeypatch, registered, reason,
):
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import AttemptStatus, EffectState, SettlementBasis

    runtime, pending, state, ticket, driver = dispatch_runtime(monkeypatch, registered=registered)
    started, release = asyncio.Event(), asyncio.Event()
    original_write = runtime.database.write

    async def pause(callback, *, key, description):
        receipt = await original_write(callback, key=key, description=description)
        if description == "QUERY v2原意图保存":
            started.set()
            await release.wait()
        return receipt

    monkeypatch.setattr(runtime.database, "write", pause)
    task = asyncio.create_task(segment_query._advance_one(runtime, pending))
    await started.wait()
    end_qualification(state, reason)
    release.set()
    assert await task is True
    driver.query_file.assert_not_called()
    assert pending.dispatched is False
    saved = runtime.operations.finish_attempt.call_args.args[0]
    assert saved.ticket == ticket and saved.run_finish.status is RunOutcome.CANCELED
    outcome = saved.outcome.outcome
    assert outcome.status is AttemptStatus.FAILED and outcome.effect is EffectState.NO_EFFECT
    assert outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert outcome.settlement.evidence.type == "dispatch_prevented"
    assert dict(outcome.settlement.evidence.data) == {}
    assert outcome.observations == () and outcome.call_info is None
    assert pending.finish is None and pending.ticket is None


async def test_known_unsent_ticket_is_settled_when_next_round_already_sees_cancel(monkeypatch):
    runtime, pending, state, ticket, driver = dispatch_runtime(
        monkeypatch, registered=True, ticket_known=True)
    state["canceled"] = 1
    assert await segment_query._advance_one(runtime, pending) is True
    driver.query_file.assert_not_called()
    assert runtime.operations.finish_attempt.call_args.args[0].ticket == ticket
    runtime.operations.begin_attempt.assert_not_called()


async def test_unknown_prevented_query_result_keeps_original_request_and_key(monkeypatch):
    runtime, pending, state, ticket, driver = dispatch_runtime(
        monkeypatch, registered=True, ticket_known=True)
    state["canceled"] = 1
    runtime.operations.finish_attempt.side_effect = [
        DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("lost result receipt")),
        DbOutcome(DbOutcomeKind.COMPLETED),
    ]
    with pytest.raises(ConsistencyError):
        await segment_query._advance_one(runtime, pending)
    original, result_key = pending.finish, pending.finish_key
    assert original.ticket == ticket and pending.ticket == ticket
    assert pending.intent_key == KEY and not pending.dispatched
    assert await segment_query._advance_one(runtime, pending) is True
    driver.query_file.assert_not_called()
    assert [call.args[:2] for call in runtime.operations.finish_attempt.call_args_list] == [
        (original, result_key), (original, result_key)]


async def test_current_original_ticket_can_dispatch_after_begin(monkeypatch):
    runtime, pending, _state, ticket, driver = dispatch_runtime(monkeypatch)

    class QueryReached(Exception):
        pass

    async def query(request):
        assert request.ticket == ticket
        assert request.params["file_id"] == "4"
        assert pending.dispatched
        raise QueryReached()

    driver.query_file.side_effect = query
    with pytest.raises(QueryReached):
        await segment_query._advance_one(runtime, pending)
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("identity", ["run", "attempt"])
async def test_different_latest_ticket_after_begin_keeps_original_pending_and_refuses_dispatch(
    monkeypatch, identity,
):
    runtime, pending, state, ticket, driver = dispatch_runtime(monkeypatch)
    original_write = runtime.database.write

    async def changed(callback, *, key, description):
        receipt = await original_write(callback, key=key, description=description)
        last = list(state["last"])
        last[0 if identity == "run" else 5] += 1
        state["last"] = tuple(last)
        return receipt

    monkeypatch.setattr(runtime.database, "write", changed)
    with pytest.raises(ConsistencyError):
        await segment_query._advance_one(runtime, pending)
    driver.query_file.assert_not_called()
    runtime.operations.finish_attempt.assert_not_called()
    assert pending.ticket == ticket and pending.intent is not None
    assert not pending.dispatched
