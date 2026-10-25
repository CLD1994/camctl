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
