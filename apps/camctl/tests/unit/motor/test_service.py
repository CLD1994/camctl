"""唯一发送许可、最终时间检查与结果保存的行为边界。"""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.contracts.values import OperationKey
from camctl.motor.models import (
    MotorActionFacts, SendFacts, SendOutcome, SendPermit,
    PrepareSendResult, PrepareOutcome, MotorFinalKind,
)
from camctl.motor.notification import NotificationWriter, NotificationWriteResult, WriteKind
from camctl.motor.rules import MotorDecision
from camctl.motor.service import MotorRuntime, advance_motor
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.persistence.repositories.motor import MotorRepository
from camctl.session.clock import ClockCheckInput, ClockBecameUntrusted
from camctl.session.service import StateDbFailure
from support.business_database import inline_business_database

pytestmark = pytest.mark.asyncio

KEY = OperationKey("a" * 32)
PERMIT = SendPermit(1, 2, KEY)
PENDING = SendFacts(2, 1, 1000, KEY, SendOutcome.PENDING, None, None, None)


def make_runtime(monkeypatch, times=(1000, 1000)):
    monkeypatch.setattr("camctl.motor.service.new_operation_key", lambda: KEY)
    row = dict(id=1, type=8, status=1, execution_started=0,
               cancel_requested=0, scheduled_at=1000, max_delay_ms=1,
               first_window_observed_at=None,
               input_fields_json={"params": {"position": -10},
                                  "policy": {"max_delay_ms": 1}})
    repo = Mock(spec=MotorRepository)
    repo.read_facts.side_effect = [MotorActionFacts(row, None),
        MotorActionFacts({**row, "status": 2, "execution_started": 1,
                          "first_window_observed_at": 1000}, PENDING)]
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        PrepareSendResult(PrepareOutcome.GRANTED, PERMIT))
    repo.observe_window.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repo.finish_send.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repo.verify_operation.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
        error=OSError("verification unavailable"))
    writer = Mock(spec=NotificationWriter)
    writer.available = True
    writer.send.return_value = NotificationWriteResult(WriteKind.WRITTEN, 76)
    clock = SimpleNamespace(utc_micros=Mock(side_effect=times))
    database = inline_business_database(OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing")))
    runtime = MotorRuntime(database, repo, writer, clock,
        lambda connection: ClockCheckInput(None, 0, 0, 0), {})
    return runtime, repo, writer, row


async def test_send_once_after_intent_and_save_local_result(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    assert await advance_motor(1, runtime) is MotorDecision.SEND
    writer.send.assert_called_once_with(
        b'{"type":"motor_control","action_instance_id":"1","params":{"position":-10}}\n')
    request = repo.finish_send.call_args.args[0]
    assert request.kind is MotorFinalKind.WRITTEN
    assert request.written_bytes == 76
    assert request.permit == PERMIT
    assert runtime.permits == {}


async def test_expiration_at_final_check_never_writes(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, 2001))
    assert await advance_motor(1, runtime) is MotorDecision.EXPIRE
    writer.send.assert_not_called()
    request = repo.finish_send.call_args.args[0]
    assert request.kind is MotorFinalKind.EXPIRED
    assert request.expiration_reason.value == "window_exhausted"
    assert request.permit == PERMIT


async def test_untrusted_final_clock_preserves_intent_and_never_writes(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, -1))
    with pytest.raises(ClockBecameUntrusted):
        await advance_motor(1, runtime)
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()


async def test_clock_returns_before_window_preserves_local_permission(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, 999))
    assert await advance_motor(1, runtime) is MotorDecision.WAIT
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()
    assert runtime.permits == {1: PERMIT}


async def test_unknown_prepare_never_grants_send(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
                                               error=OSError("commit outcome unknown"))
    with pytest.raises(StateDbFailure):
        await advance_motor(1, runtime)
    writer.send.assert_not_called()
    assert runtime.permits == {}


async def test_existing_intent_is_not_sent_even_after_window(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch, (3000,))
    repo.read_facts.side_effect = None
    repo.read_facts.return_value = MotorActionFacts(
        {**row, "status": 2, "execution_started": 1}, PENDING)
    assert await advance_motor(1, runtime) is MotorDecision.RECOVER_UNKNOWN
    writer.send.assert_not_called()
    repo.prepare_send.assert_not_called()
    assert repo.finish_send.call_args.args[0].kind is MotorFinalKind.UNCONFIRMED


async def test_cancellation_terminal_at_final_read_never_writes(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch)
    canceled = SendFacts(2, 1, 1000, KEY, SendOutcome.NOT_SENT, None, None, 1001)
    repo.read_facts.side_effect = [MotorActionFacts(row, None), MotorActionFacts(
        {**row, "status": 6, "execution_started": 1, "cancel_requested": 1}, canceled)]
    assert await advance_motor(1, runtime) is MotorDecision.KEEP_TERMINAL
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()
    assert runtime.permits == {}


async def test_failed_write_is_recorded_without_retry(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 0, "would_block", 11)
    await advance_motor(1, runtime)
    writer.send.assert_called_once()
    request = repo.finish_send.call_args.args[0]
    assert (request.kind, request.written_bytes, request.errno, request.reason) == (
        MotorFinalKind.FAILED, 0, 11, "would_block")


async def test_unknown_result_commit_cannot_enable_second_write(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.finish_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
                                              error=OSError("result commit unknown"))
    with pytest.raises(StateDbFailure):
        await advance_motor(1, runtime)
    writer.send.assert_called_once()
    assert runtime.permits == {}



async def test_unknown_intent_is_verified_by_original_key_without_send_permission(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.COMPLETED,
                                                       PrepareSendResult(PrepareOutcome.ALREADY)))
    assert await advance_motor(1,runtime) is MotorDecision.WAIT
    original = repo.prepare_send.call_args.args[0]
    assert repo.verify_operation.call_args.args[:2] == (original, KEY)
    assert runtime.permits == {}
    writer.send.assert_not_called()


async def test_unknown_result_reuses_verified_original_commit_without_second_write(monkeypatch):
    from camctl.motor.models import FinishSendResult
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.finish_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.COMPLETED,FinishSendResult(3,True)))
    assert await advance_motor(1,runtime) is MotorDecision.SEND
    original = repo.finish_send.call_args.args[0]
    assert repo.verify_operation.call_args.args[:2] == (original, KEY)
    writer.send.assert_called_once()
    assert runtime.permits == {}


async def test_unknown_commit_with_reliable_absence_does_not_repeat_transaction(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.ROLLED_BACK,error=OSError('absent')))
    with pytest.raises(StateDbFailure):
        await advance_motor(1,runtime)
    repo.prepare_send.assert_called_once()
    repo.verify_operation.assert_called_once()
    writer.send.assert_not_called()


async def test_cancel_during_intent_save_keeps_committed_unsent_permit(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    original_write = runtime.database.write

    async def paused_write(callback, *, key, description):
        if description == "电机意图事务":
            started.set()
            await release.wait()
        return await original_write(callback, key=key, description=description)

    runtime.database.write = paused_write
    task = asyncio.create_task(advance_motor(1, runtime))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.permits == {1: PERMIT}
    writer.send.assert_not_called()


async def test_cancel_during_result_save_still_applies_terminal_receipt(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch, times=(3000,))
    runtime.permits[1] = PERMIT
    repo.read_facts.side_effect = None
    repo.read_facts.return_value = MotorActionFacts(
        {**row, "status": 2, "execution_started": 1,
         "first_window_observed_at": 1000}, PENDING)
    started, release = asyncio.Event(), asyncio.Event()
    original_write = runtime.database.write

    async def paused_write(callback, *, key, description):
        started.set()
        await release.wait()
        return await original_write(callback, key=key, description=description)

    runtime.database.write = paused_write
    task = asyncio.create_task(advance_motor(1, runtime))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.permits == {}
    request = repo.finish_send.call_args.args[0]
    assert request.kind is MotorFinalKind.EXPIRED
    writer.send.assert_not_called()


async def test_database_callbacks_do_not_update_permits_or_send(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    active = False

    class LoopPermits(dict):
        def __setitem__(self, identity, permit):
            assert not active
            return super().__setitem__(identity, permit)

        def pop(self, identity, *default):
            assert not active
            return super().pop(identity, *default)

    def guarded(callback):
        def execute(owned):
            nonlocal active
            active = True
            try:
                return callback(owned)
            finally:
                active = False
        return execute

    original_read, original_write = runtime.database.read, runtime.database.write

    async def read(callback, *, description):
        return await original_read(guarded(callback), description=description)

    async def write(callback, *, key, description):
        return await original_write(guarded(callback), key=key, description=description)

    def send(message):
        assert not active
        return NotificationWriteResult(WriteKind.WRITTEN, len(message))

    runtime.permits = LoopPermits()
    runtime.database.read = read
    runtime.database.write = write
    writer.send.side_effect = send
    assert await advance_motor(1, runtime) is MotorDecision.SEND
    assert runtime.permits == {}


async def test_existing_permit_rechecks_terminal_during_qualification_wait(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch)
    runtime.permits[1] = PERMIT
    current = MotorActionFacts(
        {**row, "status": 2, "execution_started": 1}, PENDING)
    repo.read_facts.side_effect = lambda *_: current
    started, release = asyncio.Event(), asyncio.Event()
    original_read = runtime.database.read

    async def paused_read(callback, *, description):
        if description in ("电机墙钟策略读取", "电机发送资格读取"):
            started.set()
            await release.wait()
        return await original_read(callback, description=description)

    runtime.database.read = paused_read
    task = asyncio.create_task(advance_motor(1, runtime))
    await asyncio.wait_for(started.wait(), 1)
    current = MotorActionFacts(
        {**row, "status": 6, "execution_started": 1, "cancel_requested": 1},
        SendFacts(2, 1, 1000, KEY, SendOutcome.NOT_SENT, None, None, 1001))
    release.set()

    assert await task is MotorDecision.KEEP_TERMINAL
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()
    assert runtime.permits == {}
