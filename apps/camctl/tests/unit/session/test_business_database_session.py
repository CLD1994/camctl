"""会话只交付完整操作，锁与派生维护按真实结果收场。"""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.contracts.clock import ClockPort
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.persistence.models import DbEnqueueTimeoutError, DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.session import CloseDecision
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.session.clock import ClockCheckInput
from camctl.session.locks import AdmissionLease, SessionLease
from camctl.session.service import (AcceptanceRepositoryPort, SessionContext, SessionPaths,
    SessionRepositoryPort, SnapshotOwnerPort, StateDbFailure, _run_work_facts, _try_close, run_session)
from camctl.session.work import WorkFacts, classify_work
from support.business_database import inline_business_database

pytestmark = pytest.mark.asyncio


def world():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    owned = OwnedConnection(connection, DatabaseMetadata("camctl", "a" * 32, 1,
        "/staging", "/ready", "/processing"))
    database = inline_business_database(owned)
    repository = create_autospec(SessionRepositoryPort, instance=True, spec_set=True)
    clock = create_autospec(ClockPort, instance=True, spec_set=True)
    clock.utc_micros.return_value = 1000
    session = create_autospec(SessionLease, instance=True, spec_set=True)
    admission = create_autospec(AdmissionLease, instance=True, spec_set=True)
    context = SessionContext(CommandMode.RUN, None, clock, AsyncMock(return_value=database),
        create_autospec(AcceptanceRepositoryPort, instance=True, spec_set=True), repository,
        SessionPaths("/session", "/admission"), lambda _: ClockCheckInput(1000, 0, 0, 1),
        lambda: session, lambda: admission,
        lambda _: WorkFacts(0, 0, False, False, False, False, False, False))
    def acquire(command, key, current):
        assert current is owned
        command.acquire_and_register()
        return DbOutcome(DbOutcomeKind.COMPLETED)
    def close(command, key, current):
        assert current is owned
        decision = classify_work(command.facts_query(current.connection))
        command.release_admission()
        return DbOutcome(DbOutcomeKind.COMPLETED, CloseDecision(True, decision))
    repository.acquire_admission.side_effect = acquire
    repository.close_admission.side_effect = close
    return context, database, owned, repository, session, admission


async def test_normal_session_publishes_database_after_lock_and_closes_after_owner_settlement():
    context, database, owned, repository, session, admission = world()
    owner = create_autospec(SnapshotOwnerPort, instance=True, spec_set=True)
    order = []
    context.acquire_session = lambda: (order.append("lock"), session)[1]
    async def open_database():
        assert order == ["lock"]
        return database
    context.open_database = open_database
    async def opened(current):
        assert current is context.database is database
        context.snapshot_owner = owner
    context.on_session_open = opened
    owner.stop_new_work.side_effect = lambda: order.append("stop-snapshot")
    async def settled():
        assert "stop-snapshot" in order
        database.executor.close.assert_not_called()
        order.append("settled-snapshot")
    owner.settle.side_effect = settled

    result = await run_session(context, None)

    assert result.succeeded
    owner.start.assert_awaited_once()
    owner.discover.assert_awaited_once()
    owner.settle.assert_awaited_once()
    database.executor.close.assert_awaited_once()
    assert order[-1] == "settled-snapshot"
    repository.acquire_admission.assert_called_once()
    repository.close_admission.assert_called_once()


async def test_admission_records_actual_acquire_and_one_release_through_final_cleanup():
    context, _, owned, _, _, _ = world()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        result = await run_session(context, None)
    assert result.succeeded
    records = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    admission_records = [record for record in records if record["stage"] == "session.admission"]
    assert [record["event"] for record in admission_records] == ["acquired", "released"]
    assert all(record["level"] == "INFO" and record["database_id"] == owned.metadata.instance_id
               for record in admission_records)


async def test_failed_admission_release_keeps_failure_without_a_released_event():
    context, _, _, _, _, admission = world()
    admission.close.side_effect = OSError("actual release failure")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        result = await run_session(context, None)
    assert not result.succeeded
    records = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    assert [record["event"] for record in records if record["stage"] == "session.admission"] == [
        "acquired"]


async def test_acquire_parent_cancel_still_takes_lease_before_final_release():
    context, database, owned, repository, session, admission = world()
    started, release = asyncio.Event(), asyncio.Event()
    original_write = database.write
    async def delayed_write(callback, **kwargs):
        if kwargs.get("description") == "取得接纳资格":
            result = await original_write(callback, **kwargs)
            started.set()
            await release.wait()
            return result
        return await original_write(callback, **kwargs)
    database.write = delayed_write
    task = asyncio.create_task(run_session(context, None))
    await started.wait()
    task.cancel("stop after actual acquire")
    await asyncio.sleep(0)
    admission.close.assert_not_called()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    admission.close.assert_called_once()
    repository.close_admission.assert_not_called()
    database.executor.close.assert_awaited_once()


@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_failed_acquire_keeps_worker_acquired_lease_for_cleanup(kind):
    context, database, owned, repository, session, admission = world()
    def acquire(command, key, current):
        command.acquire_and_register()
        return DbOutcome(kind, error=ValueError("actual acquire result"))
    repository.acquire_admission.side_effect = acquire
    result = await run_session(context, None)
    assert result.reason == "state_db_error"
    admission.close.assert_called_once()
    database.executor.close.assert_awaited_once()


async def test_snapshot_failure_is_fatal_and_still_stops_and_settles_owner():
    context, database, owned, repository, session, admission = world()
    owner = create_autospec(SnapshotOwnerPort, instance=True, spec_set=True)
    context.snapshot_owner = owner
    owner.discover.side_effect = StateDbFailure("snapshot SQL failed")
    result = await run_session(context, None)
    assert result.reason == "state_db_error"
    assert "snapshot SQL failed" in str(result.details)
    owner.stop_new_work.assert_called()
    owner.settle.assert_awaited_once()
    database.executor.close.assert_awaited_once()


async def test_close_queries_persistent_work_inside_same_write_as_release():
    context, database, owned, repository, session, admission = world()
    context.database = database
    observed = []
    def facts(connection):
        assert connection is owned.connection
        observed.append("facts")
        return WorkFacts(0, 0, False, False, False, False, False, False)
    context.facts_query = facts
    admission.close.side_effect = lambda: observed.append("release")
    result = await _try_close(context, database, admission)
    assert result.succeeded
    assert observed == ["facts", "release"]


@pytest.mark.parametrize("stored", [None, 3])
async def test_work_facts_adds_loop_owned_count_without_changing_unknown(stored):
    context, database, owned, repository, session, admission = world()
    context.database = database
    class Local:
        def required_settlements(self):
            return 2
    context.local_work = Local()
    context.facts_query = lambda _: WorkFacts(0, stored, False, False, False, False, False, False)
    result = await _run_work_facts(context)
    assert result.required_settlements == (None if stored is None else 5)


async def test_startup_clock_policy_enqueue_timeout_is_state_db_error_and_emergency_settles():
    context, database, owned, repository, session, admission = world()
    timeout = DbEnqueueTimeoutError("startup policy queue full")
    _raise_on_database_operation(
        database, method="read", description="启动时钟依据", error=timeout)
    context.emergency_settlement = AsyncMock(return_value=[])

    result = await run_session(context, None)

    assert result.reason == "state_db_error"
    assert "startup policy queue full" in str(result.details)
    context.emergency_settlement.assert_awaited_once()
    repository.acquire_admission.assert_not_called()


async def test_clock_lower_bound_enqueue_timeout_is_state_db_error_and_emergency_settles():
    context, database, owned, repository, session, admission = world()
    context.clock_policy = lambda _: ClockCheckInput(None, 0, 0, 1)
    timeout = DbEnqueueTimeoutError("clock update queue full")
    _raise_on_database_operation(
        database, method="write", description="可信时间下界更新", error=timeout)
    context.emergency_settlement = AsyncMock(return_value=[])

    result = await run_session(context, None)

    assert result.reason == "state_db_error"
    assert "clock update queue full" in str(result.details)
    context.emergency_settlement.assert_awaited_once()
    repository.acquire_admission.assert_not_called()


async def test_deadline_read_enqueue_timeout_stops_after_first_flow_and_emergency_settles():
    context, database, owned, repository, session, admission = world()
    timeout = DbEnqueueTimeoutError("deadline queue full")
    _raise_on_database_operation(
        database, method="read", description="下一业务截止", error=timeout)
    context.emergency_settlement = AsyncMock(return_value=[])
    flow_calls = 0

    async def ordinary_flow(_context):
        nonlocal flow_calls
        flow_calls += 1

    context.flows = {"ordinary": ordinary_flow}
    context.facts_query = lambda _: WorkFacts(
        1, 0, False, False, False, False, False, False)

    result = await run_session(context, None)

    assert result.reason == "state_db_error"
    assert "deadline queue full" in str(result.details)
    repository.acquire_admission.assert_called_once()
    repository.close_admission.assert_not_called()
    assert flow_calls == 1
    context.emergency_settlement.assert_awaited_once()


def _raise_on_database_operation(database, *, method, description, error):
    original = getattr(database, method)

    async def guarded(callback, **kwargs):
        if kwargs.get("description") == description:
            raise error
        return await original(callback, **kwargs)

    setattr(database, method, guarded)
