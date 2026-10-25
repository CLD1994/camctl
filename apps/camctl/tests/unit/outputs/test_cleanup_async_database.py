"""清理数据库阶段在异步回执后应用状态，取消不丢原申请。"""

import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.operations.attempts import BeginAttemptResult, BeginDisposition
from camctl.operations.models import AttemptStatus
from camctl.outputs import cleanup_flow, cleanup_results
from camctl.outputs.cleanup_discovery import cleanup_item_batches
from camctl.outputs.cleanup_ownership import save_cleanup_request
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind

from .test_cleanup_query_save_responsibility import (
    _ITEM_ID, _TICKET, _actual_result, _runtime, _saved, original_config_reader,
)

pytestmark = pytest.mark.asyncio


def _port(runtime):
    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = runtime.database.metadata
    owned = runtime._test_owned
    database.read.side_effect = lambda callback, **kwargs: callback(owned)
    runtime.database = database
    return database, owned


def _member_saved():
    return DbOutcome(DbOutcomeKind.COMPLETED, cleanup_flow.CleanupItemSaved(
        cleanup_flow.CleanupItemDisposition.SAVED, _ITEM_ID))


async def test_cancel_during_member_save_applies_real_receipt_before_propagating():
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database, owned = _port(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    request = cleanup_flow.FinishCleanupItem(
        _ITEM_ID, cleanup_flow.CleanupOutcomeChoice.DELETED, 1)
    result = _member_saved()
    runtime.outputs.finish_cleanup_item.return_value = result

    async def write(callback, *, key, description):
        entered.set()
        await release.wait()
        assert runtime.pending_saves[("finish_cleanup_item", _ITEM_ID)].key == key
        return callback(owned)

    database.write.side_effect = write
    task = asyncio.create_task(save_cleanup_request(runtime, "finish_cleanup_item", request))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel("original cancellation")
    await asyncio.sleep(0)
    assert not task.done() and runtime.pending_saves
    release.set()
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await task
    assert runtime.pending_saves == {}
    runtime.outputs.finish_cleanup_item.assert_called_once_with(
        request, database.write.call_args.kwargs["key"], owned)


async def test_unknown_reentry_uses_next_database_owned_and_original_request_key():
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database, old = _port(runtime)
    fresh = replace(old, connection=create_autospec(sqlite3.Connection, instance=True))
    request = cleanup_flow.FinishCleanupItem(
        _ITEM_ID, cleanup_flow.CleanupOutcomeChoice.DELETED, 1)
    runtime.outputs.finish_cleanup_item.side_effect = (
        DbOutcome(DbOutcomeKind.UNKNOWN), _member_saved())
    callbacks = []

    async def write(callback, *, key, description):
        current = old if not callbacks else fresh
        callbacks.append(current)
        return callback(current)

    database.write.side_effect = write
    first = await save_cleanup_request(runtime, "finish_cleanup_item", request)
    assert first.kind is DbOutcomeKind.UNKNOWN
    original = runtime.pending_saves[("finish_cleanup_item", _ITEM_ID)]
    old.connection.execute.side_effect = AssertionError("旧连接不能在 UNKNOWN 后复用")
    second = await save_cleanup_request(runtime, "finish_cleanup_item", replace(request, occurred_at=2))
    assert second.kind is DbOutcomeKind.COMPLETED
    assert runtime.pending_saves == {}
    assert callbacks == [old, fresh]
    assert [call.args for call in runtime.outputs.finish_cleanup_item.call_args_list] == [
        (request, original.key, old), (request, original.key, fresh)]


async def test_cancel_after_intent_commit_retains_granted_ticket(monkeypatch):
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database, owned = _port(runtime)
    verify = create_autospec(cleanup_flow.verify_cleanup_transaction, return_value=(object(),))
    monkeypatch.setattr(cleanup_flow, "verify_cleanup_transaction", verify)
    runtime.outputs.begin_cleanup_attempt.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, _TICKET))
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, *, key):
        entered.set()
        await release.wait()
        return callback(owned)

    database.write.side_effect = write
    task = asyncio.create_task(cleanup_flow._begin_query_attempt(runtime, _ITEM_ID, 11))
    await asyncio.wait_for(entered.wait(), 1)
    pending = runtime.pending_intents[_ITEM_ID]
    task.cancel("intent waiter")
    await asyncio.sleep(0)
    assert not task.done() and pending.ticket is None
    release.set()
    with pytest.raises(asyncio.CancelledError, match="intent waiter"):
        await task
    assert pending.ticket is _TICKET and pending.dispatched is False
    verify.assert_called_once_with(owned, pending.key)
    runtime.driver.query_file.assert_not_awaited()


async def test_cancel_after_query_return_marks_saved_without_redispatch():
    runtime = _runtime(_actual_result(True), _saved(AttemptStatus.SUCCEEDED))
    database, owned = _port(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, *, key):
        entered.set()
        await release.wait()
        return callback(owned)

    database.write.side_effect = write
    task = asyncio.create_task(cleanup_flow._run_query(runtime, _TICKET, _ITEM_ID))
    await asyncio.wait_for(entered.wait(), 1)
    pending = runtime.pending_queries[(_TICKET.run_id, _TICKET.attempt_id)]
    task.cancel("query save waiter")
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError, match="query save waiter"):
        await task
    assert pending.phase is cleanup_flow.CleanupQueryPhase.SAVED
    assert await cleanup_flow._run_query(runtime, _TICKET, _ITEM_ID) is True
    runtime.driver.query_file.assert_awaited_once()
    runtime.operations.finish_attempt.assert_called_once()


async def test_query_return_enters_pending_without_another_binding_read():
    runtime = _runtime(_actual_result(True), _saved(AttemptStatus.SUCCEEDED))
    binding = runtime.binding_of.return_value
    returned = False

    async def binding_of(item_id):
        assert not returned, "实际返回进入 pending 前不能再等待数据库"
        return binding

    async def query(request):
        nonlocal returned
        returned = True
        return _actual_result(True)

    runtime.binding_of.side_effect = binding_of
    runtime.driver.query_file.side_effect = query
    assert await cleanup_flow._run_query(runtime, _TICKET, _ITEM_ID) is True
    pending = runtime.pending_queries[(_TICKET.run_id, _TICKET.attempt_id)]
    assert pending.phase is cleanup_flow.CleanupQueryPhase.SAVED


async def test_cancel_during_request_reads_keeps_ticket_undispatched(monkeypatch):
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind

    runtime = _runtime(_actual_result(True), _saved(AttemptStatus.SUCCEEDED))
    request = AttemptIntent(operation="query", action_id=11,
        kind=OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=_ITEM_ID), query_purpose=None,
        config=runtime.query_config, occurred_at=1)
    pending = cleanup_flow.PendingCleanupIntent(runtime.database.metadata.instance_id,
        _ITEM_ID, request, new_operation_key(),
        cleanup_flow.CleanupQueryConsumer.BEFORE_DELETE, ticket=_TICKET)
    runtime.pending_intents[_ITEM_ID] = pending
    original_read = runtime._test_owned.connection.execute.side_effect

    def read(statement, parameters=()):
        cursor = original_read(statement, parameters)
        if "SELECT status FROM cleanup_items" in statement:
            cursor.fetchone.return_value = (3,)
        elif "SELECT action_id FROM cleanup_items" in statement:
            cursor.fetchone.return_value = (11,)
        return cursor

    runtime._test_owned.connection.execute.side_effect = read
    entered = asyncio.Event()

    async def prepare(runtime, item_id, operation, ticket):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(cleanup_flow, "_control_request",
        create_autospec(cleanup_flow._control_request, side_effect=prepare))
    task = asyncio.create_task(cleanup_flow._run_query(runtime, _TICKET, _ITEM_ID))
    await asyncio.wait_for(entered.wait(), 1)
    assert pending.dispatched is False
    task.cancel("request read cancelled")
    with pytest.raises(asyncio.CancelledError, match="request read cancelled"):
        await task
    assert pending.ticket is _TICKET and pending.dispatched is False
    assert runtime.active_calls == set() and runtime.pending_queries == {}
    runtime.driver.query_file.assert_not_awaited()


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("change", ["cancel", "stop", "terminal", "missing"])
@pytest.mark.parametrize("dispatched,unsettled,cancel_query_available", [
    (False, False, True), (True, True, True), (True, False, False), (True, False, True),
])
async def test_dispatch_rechecks_facts_after_request_preparation(
        monkeypatch, operation, change, dispatched, unsettled, cancel_query_available):
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
    from camctl.operations.models import AttemptTicket, SettlementBasis
    from camctl.persistence.repositories import outputs

    calls_allowed = (operation == "query" and change == "cancel"
                     and dispatched and not unsettled and cancel_query_available)
    runtime = _runtime(_actual_result(True), _saved(
        AttemptStatus.SUCCEEDED if calls_allowed else AttemptStatus.FAILED))
    runtime.driver = create_autospec(cleanup_flow.DeleteDriverPort, instance=True)
    runtime.driver.query_file.return_value = _actual_result(True)
    runtime.outputs.finish_cleanup_attempt.side_effect = lambda finish, key, owned: _saved(finish.outcome.outcome.status)
    runtime.operations.finish_attempt.side_effect = lambda finish, key, owned: _saved(finish.outcome.outcome.status)
    runtime.driver.delete.return_value = cleanup_flow._not_dispatched("delete")
    ticket = AttemptTicket(2, operation, str(_ITEM_ID),
        f"{'delete' if operation == 'delete' else 'exists'}/{_ITEM_ID}", 7)
    runtime.evidence = cleanup_flow._not_dispatched_evidence(runtime.evidence, ticket)
    consumer = (None if operation == "delete"
                else cleanup_flow.CleanupQueryConsumer.BEFORE_DELETE)
    request = AttemptIntent(operation=operation, action_id=11,
        kind=(OperationKind.DELETE_FILE if operation == "delete"
              else OperationKind.CHECK_FILE_EXISTS),
        target=AttemptTarget(cleanup_item_id=_ITEM_ID), query_purpose=None,
        config=runtime.query_config, occurred_at=1)
    intent = cleanup_flow.PendingCleanupIntent(runtime.database.metadata.instance_id,
        _ITEM_ID, request, new_operation_key(), consumer, ticket=ticket)
    runtime.pending_intents[_ITEM_ID] = intent
    canceled = False
    member_status = 3
    original_read = runtime._test_owned.connection.execute.side_effect

    def read(statement, parameters=()):
        cursor = original_read(statement, parameters)
        if "SELECT action_id,status FROM cleanup_items" in statement:
            cursor.fetchone.return_value = (None if member_status is None else (11, member_status))
        elif "SELECT status FROM cleanup_items" in statement:
            cursor.fetchone.return_value = (3,)
        elif "SELECT action_id FROM cleanup_items" in statement:
            cursor.fetchone.return_value = (11,)
        elif "SELECT cancel_requested FROM actions" in statement:
            cursor.fetchone.return_value = (int(canceled),)
        return cursor

    runtime._test_owned.connection.execute.side_effect = read
    monkeypatch.setattr(outputs, "_member_delete_dispatch_state",
        create_autospec(outputs._member_delete_dispatch_state,
            return_value=(dispatched, unsettled)))
    monkeypatch.setattr(cleanup_results, "cleanup_cancel_query_available",
        create_autospec(cleanup_results.cleanup_cancel_query_available,
            return_value=cancel_query_available))
    entered, release = asyncio.Event(), asyncio.Event()
    original_prepare = cleanup_flow._prepare_cleanup_call

    async def prepare(*args):
        prepared = await original_prepare(*args)
        entered.set()
        await release.wait()
        return prepared

    monkeypatch.setattr(cleanup_flow, "_prepare_cleanup_call",
        create_autospec(original_prepare, side_effect=prepare))
    monkeypatch.setattr(cleanup_flow, "_consume_delete_return",
        create_autospec(cleanup_flow._consume_delete_return,
            return_value=cleanup_flow.CleanupStep("saved")))
    task = asyncio.create_task(
        cleanup_flow._delete_once(runtime, _ITEM_ID, 11, 71) if operation == "delete"
        else cleanup_flow._run_query(runtime, ticket, _ITEM_ID))
    await asyncio.wait_for(entered.wait(), 1)
    if change == "cancel":
        canceled = True
    elif change == "stop":
        runtime.allow_new_calls = False
    elif change == "terminal":
        member_status = 4
    else:
        member_status = None
    release.set()
    await task
    if calls_allowed:
        runtime.driver.query_file.assert_awaited_once()
        assert intent.dispatched is True
    else:
        runtime.driver.delete.assert_not_awaited()
        runtime.driver.query_file.assert_not_awaited()
        assert intent.dispatched is False
        pending = (runtime.pending_deletes[_ITEM_ID] if operation == "delete"
                   else runtime.pending_queries[(ticket.run_id, ticket.attempt_id)])
        assert pending.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert runtime.active_calls == set()


async def test_discovery_reads_one_finite_page_before_delivering_and_closes_early():
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    connection = runtime._test_owned.connection
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.side_effect = None
    connection.execute.return_value = cursor
    cursor.fetchall.return_value = [(3,), (9,)]
    batches = cleanup_item_batches(runtime.database, batch_size=2)
    assert await anext(batches) == (3, 9)
    assert connection.execute.call_args.args[1] == (0, 2)
    await batches.aclose()
    assert connection.execute.call_count == 1


async def test_discovery_resumes_from_last_id_and_propagates_read_error():
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    connection = runtime._test_owned.connection
    connection.execute.side_effect = None
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    connection.execute.return_value = cursor
    failure = RuntimeError("page read failed")
    cursor.fetchall.side_effect = [[(3,), (9,)], [(12,)], failure]
    batches = cleanup_item_batches(runtime.database, batch_size=2)
    assert await anext(batches) == (3, 9)
    assert await anext(batches) == (12,)
    with pytest.raises(RuntimeError) as caught:
        await anext(batches)
    assert caught.value is failure
    assert [call.args[1] for call in connection.execute.call_args_list] == [(0, 2), (9, 2), (12, 2)]


async def test_recovery_reads_original_query_through_database_and_keeps_identity(monkeypatch):
    from camctl.operations.recovery import RecoveryBoundary
    from camctl.outputs import cleanup_recovery
    from camctl.outputs.cleanup_recovery import recover_cleanup_call

    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database, owned = _port(runtime)
    runtime.binding_of = AsyncMock(return_value=None)
    runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    runtime.recovery_max_event_id = 10
    runtime.recovery_evidence_for = lambda binding, operation: runtime.evidence
    monkeypatch.setattr(cleanup_recovery, "running_cleanup_attempt",
        create_autospec(cleanup_results.running_cleanup_attempt, return_value=(_TICKET, 8)))
    monkeypatch.setattr(cleanup_flow, "_cancel_requested",
        create_autospec(cleanup_flow._cancel_requested, return_value=False))
    monkeypatch.setattr(cleanup_flow, "_item_action",
        create_autospec(cleanup_flow._item_action, return_value=11))
    monkeypatch.setattr(cleanup_flow, "_latest_possible_delete",
        create_autospec(cleanup_flow._latest_possible_delete, return_value=False))
    owned.connection.execute.side_effect = None
    owned.connection.execute.return_value.fetchone.return_value = (71,)
    await recover_cleanup_call(runtime, _ITEM_ID)
    pending = runtime.pending_queries[(_TICKET.run_id, _TICKET.attempt_id)]
    assert pending.ticket is _TICKET
    assert pending.database_id == database.metadata.instance_id
    assert pending.recovery_max_event_id == 10
    assert pending.consumer is cleanup_flow.CleanupQueryConsumer.BEFORE_DELETE
    runtime.driver.query_file.assert_not_awaited()


async def test_session_factory_and_binding_use_the_supplied_database():
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.devices.drivers.registry import DriverRegistry

    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database, owned = _port(runtime)
    owned.connection.execute.side_effect = None
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    owned.connection.execute.return_value = cursor
    cursor.fetchone.return_value = None
    factory = session_cleanup_assembly(devices={},
        drivers=create_autospec(DriverRegistry, instance=True),
        max_delete_attempts=3, max_query_attempts=3,
        occurred_at=lambda: 1, monotonic_ns=lambda: 2)
    assembled = await factory(database)
    assert assembled.database is database
    assert await assembled.binding_of(_ITEM_ID) is None
    assert database.read.await_count == 2
    assert owned.connection.execute.call_args.args[1] == (_ITEM_ID,)
    database.close.assert_not_awaited()
