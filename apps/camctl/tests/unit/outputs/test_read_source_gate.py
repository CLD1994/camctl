"""源准备等待期间的停止必须在实际开源前生效。"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture.media_flow import DriverReadSessions
from camctl.devices.ports import ReadDriver
from camctl.devices.read_session import SourceFile
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.models import AttemptTicket
from camctl.outputs.read_attempts import run_owned_read
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.session.supervision import Supervisor
from support.business_database import inline_business_database


@pytest.mark.asyncio
@pytest.mark.parametrize("host_source", [False, True])
async def test_owned_read_stop_during_source_facts_keeps_unsent_ticket(monkeypatch, host_source):
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = (7, None)
    owned.connection.execute.return_value = cursor
    database = inline_business_database(owned)
    source_waiting, release = asyncio.Event(), asyncio.Event()
    original_read = database.read
    controls = []

    read_count = 0
    async def read(callback, *, description="业务读取"):
        nonlocal read_count
        read_count += 1
        if description == "媒体读取源事实" or (host_source and read_count == 2):
            source_waiting.set()
            await release.wait()
        return await original_read(callback, description=description)

    monkeypatch.setattr(database, "read", read)
    monkeypatch.setattr("camctl.capture.media_flow.load_confirmed_source",
        lambda owned, identity: SourceFile("source", {}, 12))
    # 当前业务事实由真实callback读取；源等待只控制执行时刻。
    def execute(query, parameters=()):
        cursor.fetchone.return_value = ((2, 0, 1, 1, 1, None,
            None if host_source else 9, 9 if host_source else None, None if host_source else "camera")
            if "SELECT a.status" in query else (("original.bin",) if "SELECT relative_path" in query else (7, None)))
        return cursor
    owned.connection.execute.side_effect = execute
    from camctl.host_files.io import LocalSourceReader
    reader = create_autospec(LocalSourceReader, spec_set=True)
    monkeypatch.setattr("camctl.outputs.obtain_flow.LocalSourceReader", reader)
    driver = create_autospec(ReadDriver, instance=True, spec_set=True)
    driver.open_read = AsyncMock(return_value=object())
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    pending = {2: ticket}
    supervisor = Supervisor()
    from camctl.outputs.read_attempts import ReadContinuation
    from camctl.operations.attempts import RetryWaitGate, AttemptConfig
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE, _finish_read_attempt
    operations = create_autospec(OperationRepository(), instance=True, spec_set=True)
    operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime = SimpleNamespace(database=database, file_executor=FileTaskExecutor(supervisor),
        operations=operations, pending_read_results={}, pending_read_ends={},
        continuing_read_tickets={2: ReadContinuation(ticket, True)}, retry_gate=RetryWaitGate(),
        occurred_at=lambda: 91, monotonic_ns=lambda: 10)
    assembly = SimpleNamespace(evidence=_LOCAL_READ_EVIDENCE)

    async def body(control):
        controls.append(control)
        if host_source:
            from pathlib import Path
            from camctl.outputs.obtain_flow import LocalReadSessions
            from camctl.outputs.handoff import DeliveryDirectories
            sessions = LocalReadSessions(database, DeliveryDirectories(Path("/staging"), Path("/ready"),
                Path("/processing"))).for_attempt(ticket, control=control)
        else:
            sessions = DriverReadSessions(database, driver, ticket, control=control)
        prevented = await sessions.open_session(9, 0, on_start=lambda: pending.pop(2))
        from camctl.capture.input_copy import InputPhase, InputStep
        await _finish_read_attempt(runtime, assembly,
            {"id": 2, "delivery_status": 1, "source_device_file_id": 9}, ticket,
            InputStep(InputPhase.READ_NOT_STARTED, copy_id=2, dispatch_prevented=prevented))
        return prevented

    task = asyncio.create_task(run_owned_read(runtime, 2, body))
    entered = asyncio.create_task(source_waiting.wait())
    done, _ = await asyncio.wait((entered, task), return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        entered.cancel()
        await task
    task.cancel()
    # supervisor 已接手后明确观察相同文件任务的停止控制。
    await controls[0].requested()
    release.set()
    with pytest.raises(asyncio.CancelledError) as interrupted:
        await task
    driver.open_read.assert_not_called()
    reader.assert_not_called()
    assert interrupted.value.__cause__ is None
    assert runtime.pending_read_results == {}
    assert runtime.continuing_read_tickets == {}
    operations.finish_attempt.assert_called_once()
    from camctl.operations.models import SettlementBasis, AttemptStatus, EffectState
    finish = operations.finish_attempt.call_args.args[0]
    assert finish.outcome.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert finish.outcome.outcome.status is AttemptStatus.FAILED
    assert finish.outcome.outcome.effect is EffectState.NO_EFFECT
    assert finish.ticket == ticket and finish.run_finish is None and finish.retry_wait
    driver.open_read.assert_not_called()
    assert pending == {2: ticket}
    assert supervisor.outstanding() == ()


def runtime_for_result(continuation):
    from camctl.operations.attempts import RetryWaitGate
    return SimpleNamespace(continuing_read_tickets={2: continuation}, pending_read_ends={},
        pending_read_results={}, occurred_at=lambda: 91, retry_gate=RetryWaitGate())


@pytest.mark.parametrize("canceled", [False, True])
def test_fresh_prevented_read_holds_not_dispatched_without_refunding_budget(canceled):
    from camctl.outputs.read_attempts import ReadContinuation, ReadDispatchPrevented, prevented_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    from camctl.operations.models import AttemptStatus, EffectState, SettlementBasis
    from camctl.operations.attempts import RunOutcome
    ticket = AttemptTicket(2, "read", "2", "read/2", 5)
    runtime = runtime_for_result(ReadContinuation(ticket, True))
    pending = prevented_read_result(runtime, ticket,
        ReadDispatchPrevented("canceled_before_dispatch" if canceled else "read_stopped_before_dispatch", canceled),
        _LOCAL_READ_EVIDENCE)
    assert pending.finish.ticket == ticket
    outcome = pending.finish.outcome.outcome
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.effect is EffectState.NO_EFFECT
    assert outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert outcome.settlement.evidence.type == "dispatch_prevented"
    assert pending.finish.retry_wait is (not canceled)
    assert (pending.finish.run_finish.status is RunOutcome.CANCELED) if canceled else pending.finish.run_finish is None
    assert runtime.pending_read_results[(5, 2)] is pending


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("bytes_read", [0, 7])
def test_canceled_continuation_uses_original_actual_end_time_key_and_evidence(complete, bytes_read):
    from camctl.outputs.read_attempts import ReadContinuation, ReadDispatchPrevented, prevented_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.contracts.values import OperationKey
    from camctl.devices.read_session import ReadEnd
    from camctl.operations.models import AttemptStatus, SettlementBasis
    from camctl.operations.attempts import RunOutcome
    from camctl.devices.evidence import EvidenceRegistry
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    step = InputStep(InputPhase.RECOPY_PENDING if complete else InputPhase.SEGMENT_FAILED,
        read_end=ReadEnd(True, bytes_read, None if complete else "stopped"),
        content_complete=complete, stop_requested=True)
    original_key = OperationKey("b" * 32)
    continuation = ReadContinuation(ticket, False, step, 17, original_key, _LOCAL_READ_EVIDENCE)
    runtime = runtime_for_result(continuation)
    pending = prevented_read_result(runtime, ticket, ReadDispatchPrevented("canceled_before_dispatch", True),
        EvidenceRegistry(()))
    assert pending.key == original_key and pending.finish.occurred_at == 17
    assert pending.finish.run_finish.status is RunOutcome.CANCELED
    assert pending.finish.outcome.outcome.status is (AttemptStatus.SUCCEEDED if complete else AttemptStatus.UNKNOWN)
    assert pending.finish.outcome.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert pending.finish.outcome.settlement_contract is _LOCAL_READ_EVIDENCE.contract("read_returned", 1)


def test_continuing_file_stop_keeps_actual_observation_without_new_attempt_result():
    from camctl.outputs.read_attempts import ReadContinuation, ReadDispatchPrevented, prevented_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    previous = ReadContinuation(ticket, False)
    runtime = runtime_for_result(previous)
    assert prevented_read_result(runtime, ticket, ReadDispatchPrevented("read_stopped_before_dispatch"),
        _LOCAL_READ_EVIDENCE) is None
    assert runtime.continuing_read_tickets[2] is previous
    assert runtime.pending_read_results == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("canceled", [False, True])
async def test_source_callback_returns_latest_qualification_and_runs_start_only_when_allowed(monkeypatch, canceled):
    from camctl.outputs.read_attempts import ReadDispatchPrevented
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = (2, 0, 2, 1, 1, None, 9, None, "camera")
    owned.connection.execute.return_value = cursor
    database = inline_business_database(owned)
    original_read = database.read
    waiting, release = asyncio.Event(), asyncio.Event()
    reads = []
    async def read(callback, *, description="业务读取"):
        waiting.set()
        await release.wait()
        reads.append(description)
        return await original_read(callback, description=description)
    monkeypatch.setattr(database, "read", read)
    monkeypatch.setattr("camctl.capture.media_flow.load_confirmed_source",
        lambda owned, identity: SourceFile("source", {}, 12))
    driver = create_autospec(ReadDriver, instance=True, spec_set=True)
    source_session = object()
    driver.open_read.return_value = source_session
    calls = []
    driver.open_read.side_effect = lambda *args, **kwargs: calls.append("open") or source_session
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    task = asyncio.create_task(DriverReadSessions(database, driver, ticket).open_session(9, 0,
        on_start=lambda: calls.append("start")))
    await waiting.wait()
    cursor.fetchone.return_value = (2, int(canceled), 2, 1, 1, None, 9, None, "camera")
    release.set()
    result = await task
    assert reads == ["媒体读取源事实"]
    if canceled:
        assert isinstance(result, ReadDispatchPrevented) and result.canceled
        assert calls == []
    else:
        assert result is source_session
        assert calls == ["start", "open"]


@pytest.mark.asyncio
async def test_source_fact_error_preserves_original_markers_even_after_stop(monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.read_attempts import ReadContinuation
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    database = inline_business_database(owned)
    error = ConsistencyError("设备文件缺失")
    def missing(owned, identity):
        raise error
    monkeypatch.setattr("camctl.capture.media_flow.load_confirmed_source", missing)
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    original = ReadContinuation(ticket, True)
    pending = {2: original}
    driver = create_autospec(ReadDriver, instance=True, spec_set=True)
    sessions = DriverReadSessions(database, driver, ticket, control=SimpleNamespace(stop_requested=True))
    from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
    with pytest.raises(ReadPreparationError) as raised:
        await sessions.open_session(9, 0, on_start=lambda: pending.pop(2))
    assert raised.value.__cause__ is error
    assert raised.value.settlement is ReadPreparationSettlement.UNCONFIRMED
    assert pending == {2: original}
    driver.open_read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_end", [False, True])
async def test_old_ticket_without_fresh_evidence_never_becomes_not_dispatched(monkeypatch, has_end):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.devices.read_session import ReadEnd
    from camctl.outputs.read_attempts import HeldReadEnd, ReadDispatchPrevented, prepare_prevented_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    from camctl.operations.models import SettlementBasis
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchall.return_value = [(5, "read/2", 1, 1, 1, None, 17)]
    owned.connection.execute.return_value = cursor
    runtime = SimpleNamespace(database=inline_business_database(owned), continuing_read_tickets={},
        pending_read_results={}, pending_read_ends={}, occurred_at=lambda: 91,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=20,
        last_recovery_diagnostic=None, on_recovery_diagnostic=None)
    if has_end:
        runtime.pending_read_ends[2] = HeldReadEnd(ticket, ReadEnd(True, 12, None), 21, evidence=_LOCAL_READ_EVIDENCE)
    from camctl.devices.evidence import EvidenceContract
    evidence = _LOCAL_READ_EVIDENCE.with_contract(EvidenceContract("local_read_recovery", 1, "read", frozenset()))
    pending = await prepare_prevented_read_result(runtime, ticket,
        ReadDispatchPrevented("canceled_before_dispatch", True), evidence)
    assert pending.finish.outcome.outcome.settlement.basis is (
        SettlementBasis.OBSERVED if has_end else SettlementBasis.ASSUMED)
    assert pending.finish.occurred_at == (21 if has_end else 91)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completed", "unknown", "rolled_back"])
async def test_prevented_result_save_preserves_original_key_until_reliable_application(kind):
    from camctl.outputs.read_attempts import ReadContinuation, ReadDispatchPrevented, prevented_read_result, save_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE, _save_read_result_and_settle
    from camctl.operations.attempts import RetryWaitGate
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.contracts.values import ConsistencyError
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    runtime = runtime_for_result(ReadContinuation(ticket, True))
    runtime.database = inline_business_database(owned)
    runtime.operations = create_autospec(OperationRepository(), instance=True, spec_set=True)
    runtime.monotonic_ns = lambda: 10
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind(kind))
    pending = prevented_read_result(runtime, ticket, ReadDispatchPrevented("read_stopped_before_dispatch"), _LOCAL_READ_EVIDENCE)
    facts = {"id": 2, "delivery_status": 1, "source_device_file_id": 9}
    if kind != "completed":
        with pytest.raises(ConsistencyError):
            await _save_read_result_and_settle(runtime, pending, facts)
        assert runtime.pending_read_results[(5, 1)] is pending
        assert runtime.continuing_read_tickets[2].never_dispatched
        runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    await _save_read_result_and_settle(runtime, pending, facts, retry=True)
    assert runtime.pending_read_results == {} and runtime.continuing_read_tickets == {}
    assert all(call.args[:2] == (pending.finish, pending.key) for call in runtime.operations.finish_attempt.call_args_list)


@pytest.mark.asyncio
async def test_stopped_partial_observation_keeps_original_key_across_continuation_and_cancel():
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.devices.read_session import ReadEnd
    from camctl.outputs.read_attempts import stopped_read_result, save_read_result, prevented_read_result, ReadDispatchPrevented
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = (0, 12)
    owned.connection.execute.return_value = cursor
    runtime = SimpleNamespace(database=inline_business_database(owned), continuing_read_tickets={},
        pending_read_results={}, pending_read_ends={}, occurred_at=lambda: 91)
    original = stopped_read_result(runtime, ticket, InputStep(InputPhase.SEGMENT_FAILED,
        read_end=ReadEnd(True, 0, "stopped"), stop_requested=True), _LOCAL_READ_EVIDENCE, 17)
    assert await save_read_result(runtime, original) is None
    assert runtime.pending_read_results == {}
    held = runtime.continuing_read_tickets[2]
    assert held.result_key == original.key and held.evidence is original.evidence
    pending = prevented_read_result(runtime, ticket, ReadDispatchPrevented("canceled_before_dispatch", True), _LOCAL_READ_EVIDENCE)
    assert pending.key == original.key and pending.finish.occurred_at == original.occurred_at


@pytest.mark.asyncio
async def test_old_local_ticket_without_recovery_contract_retains_explicit_responsibility():
    from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason
    from camctl.outputs.read_attempts import ReadDispatchPrevented, prepare_prevented_read_result
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchall.return_value = [(5, "read/2", 1, 1, 1, None, 17)]
    owned.connection.execute.return_value = cursor
    runtime = SimpleNamespace(database=inline_business_database(owned), continuing_read_tickets={},
        pending_read_results={}, pending_read_ends={}, occurred_at=lambda: 91,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=20,
        last_recovery_diagnostic=None, on_recovery_diagnostic=None)
    assert await prepare_prevented_read_result(runtime, ticket,
        ReadDispatchPrevented("canceled_before_dispatch", True), _LOCAL_READ_EVIDENCE) is None
    assert runtime.pending_read_results == {} and runtime.continuing_read_tickets == {}
    assert runtime.last_recovery_diagnostic.reason is RecoveryBlockedReason.EVIDENCE_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("consumer", ["device", "host", "internal"])
async def test_terminal_attempt_stops_source_without_writing_new_result(monkeypatch, canceled, consumer):
    from camctl.outputs.read_attempts import ReadContinuation
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE, _finish_read_attempt, _finish_local_attempt
    from camctl.capture.media_flow import _finish_internal_read
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.operations.attempts import AttemptConfig
    from camctl.persistence.repositories.operations import OperationRepository
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = (2, int(canceled), 3, 1, 2, "saved", 9, None, "camera")
    owned.connection.execute.return_value = cursor
    database = inline_business_database(owned)
    monkeypatch.setattr("camctl.capture.media_flow.load_confirmed_source",
        lambda owned, identity: SourceFile("source", {}, 12))
    driver = create_autospec(ReadDriver, instance=True, spec_set=True)
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    prevented = await DriverReadSessions(database, driver, ticket).open_session(9, 0)
    assert prevented.terminal
    runtime = runtime_for_result(ReadContinuation(ticket, True))
    runtime.operations = create_autospec(OperationRepository(), instance=True, spec_set=True)
    runtime.database = database
    runtime.evidence = _LOCAL_READ_EVIDENCE
    step = InputStep(InputPhase.READ_NOT_STARTED, copy_id=2, dispatch_prevented=prevented)
    facts = {"id": 2, "delivery_status": 1, "source_device_file_id": 9}
    if consumer == "device":
        await _finish_read_attempt(runtime, SimpleNamespace(evidence=_LOCAL_READ_EVIDENCE), facts, ticket, step)
    elif consumer == "host":
        await _finish_local_attempt(runtime, facts, ticket, step, AttemptConfig(3))
    else:
        await _finish_internal_read(runtime, ticket, step)
    driver.open_read.assert_not_called()
    runtime.operations.finish_attempt.assert_not_called()
    assert runtime.pending_read_results == {} and runtime.continuing_read_tickets == {}
