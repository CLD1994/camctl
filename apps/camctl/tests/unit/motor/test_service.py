"""唯一发送许可、最终时间检查与结果保存的行为边界。"""
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
from camctl.persistence.repositories.motor import MotorRepository
from camctl.session.clock import ClockCheckInput, ClockBecameUntrusted
from camctl.session.service import StateDbFailure

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
    writer = Mock(spec=NotificationWriter)
    writer.available = True
    writer.send.return_value = NotificationWriteResult(WriteKind.WRITTEN, 76)
    clock = SimpleNamespace(utc_micros=Mock(side_effect=times))
    runtime = MotorRuntime(SimpleNamespace(connection=object()), repo, writer, clock,
        lambda connection: ClockCheckInput(None, 0, 0, 0), {})
    return runtime, repo, writer, row


def test_send_once_after_intent_and_save_local_result(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    assert advance_motor(1, runtime) is MotorDecision.SEND
    writer.send.assert_called_once_with(
        b'{"type":"motor_control","action_instance_id":"1","params":{"position":-10}}\n')
    request = repo.finish_send.call_args.args[0]
    assert request.kind is MotorFinalKind.WRITTEN
    assert request.written_bytes == 76
    assert request.permit == PERMIT
    assert runtime.permits == {}


def test_expiration_at_final_check_never_writes(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, 2001))
    assert advance_motor(1, runtime) is MotorDecision.EXPIRE
    writer.send.assert_not_called()
    request = repo.finish_send.call_args.args[0]
    assert request.kind is MotorFinalKind.EXPIRED
    assert request.expiration_reason.value == "window_exhausted"
    assert request.permit == PERMIT


def test_untrusted_final_clock_preserves_intent_and_never_writes(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, -1))
    with pytest.raises(ClockBecameUntrusted):
        advance_motor(1, runtime)
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()


def test_clock_returns_before_window_preserves_local_permission(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch, (1000, 999))
    assert advance_motor(1, runtime) is MotorDecision.WAIT
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()
    assert runtime.permits == {1: PERMIT}


def test_unknown_prepare_never_grants_send(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
                                               error=OSError("commit outcome unknown"))
    with pytest.raises(StateDbFailure):
        advance_motor(1, runtime)
    writer.send.assert_not_called()
    assert runtime.permits == {}


def test_existing_intent_is_not_sent_even_after_window(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch, (3000,))
    repo.read_facts.side_effect = None
    repo.read_facts.return_value = MotorActionFacts(
        {**row, "status": 2, "execution_started": 1}, PENDING)
    assert advance_motor(1, runtime) is MotorDecision.RECOVER_UNKNOWN
    writer.send.assert_not_called()
    repo.prepare_send.assert_not_called()
    assert repo.finish_send.call_args.args[0].kind is MotorFinalKind.UNCONFIRMED


def test_cancellation_terminal_at_final_read_never_writes(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch)
    canceled = SendFacts(2, 1, 1000, KEY, SendOutcome.NOT_SENT, None, None, 1001)
    repo.read_facts.side_effect = [MotorActionFacts(row, None), MotorActionFacts(
        {**row, "status": 6, "execution_started": 1, "cancel_requested": 1}, canceled)]
    assert advance_motor(1, runtime) is MotorDecision.KEEP_TERMINAL
    writer.send.assert_not_called()
    repo.finish_send.assert_not_called()
    assert runtime.permits == {}


def test_failed_write_is_recorded_without_retry(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 0, "would_block", 11)
    advance_motor(1, runtime)
    writer.send.assert_called_once()
    request = repo.finish_send.call_args.args[0]
    assert (request.kind, request.written_bytes, request.errno, request.reason) == (
        MotorFinalKind.FAILED, 0, 11, "would_block")


def test_unknown_result_commit_cannot_enable_second_write(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    repo.finish_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
                                              error=OSError("result commit unknown"))
    with pytest.raises(StateDbFailure):
        advance_motor(1, runtime)
    writer.send.assert_called_once()
    assert runtime.permits == {}


def _reconnectable(runtime):
    old = SimpleNamespace(connection=Mock(), metadata=SimpleNamespace(instance_id='a'*32))
    fresh = SimpleNamespace(connection=Mock(), metadata=SimpleNamespace(instance_id='a'*32))
    runtime.owned = old
    runtime.open_connection = Mock(return_value=fresh)
    return old, fresh


def test_unknown_intent_is_verified_by_original_key_without_send_permission(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    old, fresh = _reconnectable(runtime)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.COMPLETED,
                                                       PrepareSendResult(PrepareOutcome.ALREADY)))
    assert advance_motor(1,runtime) is MotorDecision.WAIT
    original = repo.prepare_send.call_args.args[0]
    repo.verify_operation.assert_called_once_with(original,KEY,fresh)
    old.connection.close.assert_called_once()
    assert runtime.owned is fresh
    assert runtime.permits == {}
    writer.send.assert_not_called()


def test_unknown_result_reuses_verified_original_commit_without_second_write(monkeypatch):
    from camctl.motor.models import FinishSendResult
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    old, fresh = _reconnectable(runtime)
    repo.finish_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.COMPLETED,FinishSendResult(3,True)))
    assert advance_motor(1,runtime) is MotorDecision.SEND
    original = repo.finish_send.call_args.args[0]
    repo.verify_operation.assert_called_once_with(original,KEY,fresh)
    writer.send.assert_called_once()
    assert runtime.permits == {}


def test_unknown_commit_with_reliable_absence_does_not_repeat_transaction(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    _reconnectable(runtime)
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError('lost receipt'))
    repo.verify_operation = Mock(return_value=DbOutcome(DbOutcomeKind.ROLLED_BACK,error=OSError('absent')))
    with pytest.raises(StateDbFailure):
        advance_motor(1,runtime)
    repo.prepare_send.assert_called_once()
    repo.verify_operation.assert_called_once()
    writer.send.assert_not_called()
