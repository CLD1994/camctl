"""读取原保存责任经共享端口交接，pending只由主协程应用。"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.read_attempts import save_read_business
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository, FinishObtain
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [DbOutcomeKind.COMPLETED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_original_read_business_keeps_key_until_actual_result_is_applied(kind):
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime = SimpleNamespace(database=inline_business_database(owned), pending_read_business={})
    request = FinishObtain(3, 9)
    def execute(actual_request, key, actual_owned):
        assert actual_owned is owned
        assert actual_request is request
        assert runtime.pending_read_business[(2, "finish")].key == key
        return DbOutcome(kind, value="saved")
    save = create_autospec(OutputsRepository(), spec_set=True).finish_obtain
    save.side_effect = execute
    if kind is DbOutcomeKind.COMPLETED:
        assert (await save_read_business(runtime, 2, "finish", request, save)).value == "saved"
        assert runtime.pending_read_business == {}
    else:
        with pytest.raises(ConsistencyError):
            await save_read_business(runtime, 2, "finish", request, save)
        original = runtime.pending_read_business[(2, "finish")]
        assert original.request is request
        save.side_effect = lambda actual_request, key, actual_owned: DbOutcome(DbOutcomeKind.COMPLETED, key)
        assert (await save_read_business(runtime, 2, "finish", request, save)).value == original.key
        assert runtime.pending_read_business == {}


@pytest.mark.asyncio
async def test_cancel_during_read_business_save_still_applies_actual_pending_result():
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime = SimpleNamespace(database=inline_business_database(owned), pending_read_business={})
    caller = asyncio.current_task()
    repository = create_autospec(OutputsRepository(), spec_set=True)
    def execute(request, key, actual_owned):
        caller.cancel()
        return DbOutcome(DbOutcomeKind.COMPLETED, "saved")
    repository.finish_obtain.side_effect = execute
    with pytest.raises(asyncio.CancelledError):
        await save_read_business(runtime, 2, "finish", FinishObtain(3, 9), repository.finish_obtain)
    assert runtime.pending_read_business == {}


@pytest.mark.asyncio
async def test_start_due_uses_worker_operation_and_async_discovery():
    from camctl.outputs.obtain_flow import _start_due
    from camctl.outputs.read_discovery import ReadDiscoveryOwner
    from camctl.contracts.pages import Page
    from unittest.mock import AsyncMock
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    discovery = create_autospec(ReadDiscoveryOwner, instance=True, spec_set=True)
    discovery.rows = AsyncMock(return_value=Page(((31,),), None))
    outputs = create_autospec(OutputsRepository(), spec_set=True)
    outputs.start_obtain_action.side_effect = lambda request, key, actual: (
        DbOutcome(DbOutcomeKind.COMPLETED, (request.action_id, key, actual)))
    runtime = SimpleNamespace(database=inline_business_database(owned), discovery=discovery, outputs=outputs)
    await _start_due(runtime, 9)
    discovery.rows.assert_awaited_once()
    request, key, actual = outputs.start_obtain_action.call_args.args
    assert request.action_id == 31 and request.occurred_at == 9
    assert actual is owned and key


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_read_intent_applies_granted_ticket_before_cancel_and_reuses_unknown_key(cancel):
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginAttemptResult, BeginDisposition, OperationKind
    from camctl.operations.models import AttemptTicket
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.outputs.read_attempts import acquire_read_attempt
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchall.return_value = []
    owned.connection.execute.return_value = cursor
    repository = create_autospec(OperationRepository(), spec_set=True)
    runtime = SimpleNamespace(database=inline_business_database(owned), operations=repository,
        pending_read_business={}, continuing_read_tickets={})
    intent = AttemptIntent("read", 3, OperationKind.READ_FILE, AttemptTarget(copy_id=2),
        None, AttemptConfig(3), 9, copy_round=1)
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    caller = asyncio.current_task()
    def begin(request, key, actual):
        assert request is intent and actual is owned
        if cancel:
            caller.cancel()
            return DbOutcome(DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, ticket))
        return DbOutcome(DbOutcomeKind.UNKNOWN)
    repository.begin_attempt.side_effect = begin
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await acquire_read_attempt(runtime, intent)
        assert runtime.continuing_read_tickets == {2: ticket}
        assert runtime.pending_read_business == {}
    else:
        with pytest.raises(ConsistencyError):
            await acquire_read_attempt(runtime, intent)
        pending = runtime.pending_read_business[(2, "read_intent")]
        repository.begin_attempt.side_effect = lambda request, key, actual: DbOutcome(
            DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, ticket))
        assert await acquire_read_attempt(runtime, intent) == (ticket, None)
        assert repository.begin_attempt.call_args.args[:2] == (intent, pending.key)
        assert runtime.pending_read_business == {} and runtime.continuing_read_tickets == {2: ticket}


@pytest.mark.asyncio
async def test_result_save_and_loop_settlement_survive_cancel_as_one_phase():
    from camctl.operations.attempts import AttemptFinish, RetryWaitGate, RunFinish, RunOutcome
    from camctl.operations.models import AttemptStatus, AttemptTicket, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE, _save_read_result_and_settle
    from camctl.outputs.read_attempts import hold_read_result
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    operations = create_autospec(OperationRepository(), spec_set=True)
    runtime = SimpleNamespace(database=inline_business_database(owned), operations=operations,
        pending_read_results={}, pending_read_ends={}, continuing_read_tickets={}, retry_gate=RetryWaitGate())
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    pending = hold_read_result(runtime, AttemptFinish(ticket,
        validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE), 9,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)))
    caller = asyncio.current_task()
    def save(request, key, actual):
        assert request is pending.finish and key == pending.key and actual is owned
        caller.cancel()
        return DbOutcome(DbOutcomeKind.COMPLETED)
    operations.finish_attempt.side_effect = save
    facts = {"id": 2, "delivery_status": 1, "source_device_file_id": None}
    with pytest.raises(asyncio.CancelledError):
        await _save_read_result_and_settle(runtime, pending, facts)
    assert runtime.pending_read_results == {}


@pytest.mark.asyncio
async def test_local_source_fact_wait_keeps_unsent_ticket_and_read_end(monkeypatch):
    from pathlib import Path
    from camctl.outputs.obtain_flow import LocalReadSessions
    from camctl.outputs.handoff import DeliveryDirectories
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    database = inline_business_database(owned)
    entered = asyncio.Event()
    async def read(callback, *, description="业务读取"):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(database, "read", read)
    held = {2: object()}
    reader = create_autospec(__import__("camctl.host_files.io", fromlist=["LocalSourceReader"]).LocalSourceReader)
    monkeypatch.setattr("camctl.outputs.obtain_flow.LocalSourceReader", reader)
    sessions = LocalReadSessions(database, DeliveryDirectories(Path("/staging"), Path("/ready"), Path("/processing")))
    task = asyncio.create_task(sessions.open_session(7, 0, on_start=lambda: held.pop(2)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 2 in held
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_local_result_uses_its_adopted_budget_before_holding_actual_result():
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.operations.attempts import AttemptConfig, RetryWaitGate
    from camctl.operations.models import AttemptTicket
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.outputs.obtain_flow import _finish_local_attempt
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    operations = create_autospec(OperationRepository(), spec_set=True)
    operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime = SimpleNamespace(database=inline_business_database(owned), operations=operations,
        pending_read_results={}, pending_read_ends={}, continuing_read_tickets={},
        retry_gate=RetryWaitGate(), occurred_at=lambda: 9)
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    await _finish_local_attempt(runtime, {"id": 2, "delivery_status": 1,
        "source_device_file_id": None}, ticket, InputStep(InputPhase.INPUT_READY), AttemptConfig(3))
    owned.connection.execute.assert_not_called()
    operations.finish_attempt.assert_called_once()
    assert runtime.pending_read_results == {}


@pytest.mark.asyncio
async def test_release_unknown_keeps_original_request_and_key_until_rechecked():
    from camctl.operations.attempts import AttemptFinish, RetryWaitGate, RunFinish, RunOutcome
    from camctl.operations.models import AttemptStatus, AttemptTicket, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE, _save_read_result_and_settle
    from camctl.outputs.read_attempts import hold_read_result
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    operations = create_autospec(OperationRepository(), spec_set=True)
    operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    outputs = create_autospec(OutputsRepository(), spec_set=True)
    outputs.release_read_slot.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN), DbOutcome(DbOutcomeKind.COMPLETED)]
    runtime = SimpleNamespace(database=inline_business_database(owned), operations=operations, outputs=outputs,
        pending_read_results={}, pending_read_ends={}, continuing_read_tickets={}, pending_read_business={},
        retry_gate=RetryWaitGate(), occurred_at=lambda: 9)
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    pending = hold_read_result(runtime, AttemptFinish(ticket,
        validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE), 9,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)))
    facts = {"id": 2, "delivery_status": 1, "source_device_file_id": 7}
    with pytest.raises(ConsistencyError):
        await _save_read_result_and_settle(runtime, pending, facts)
    original = runtime.pending_read_business[(2, "release_read_slot")]
    assert runtime.pending_read_results
    await _save_read_result_and_settle(runtime, pending, facts)
    assert runtime.pending_read_results == {} and runtime.pending_read_business == {}
    assert all(call.args[:2] == (original.request, original.key) for call in outputs.release_read_slot.call_args_list)
