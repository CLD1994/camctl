"""电机错误即使可靠保存并继续会话，也必须交付ERROR。"""
import json

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.motor.models import MotorActionFacts
from camctl.motor.notification import NotificationWriteResult, WriteKind
from .diagnostic_fixtures import motor_flow_world, motor_database

pytestmark = pytest.mark.asyncio


async def test_failed_notification_preserves_result_details(monkeypatch, motor_database):
    runtime, repo, writer, _ = motor_flow_world(monkeypatch, motor_database)
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 5, 'short_write')
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await runtime.advance()
            assert repo.finish_send.call_count == 1
            assert lines == []
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'motor_notification_failed'
    assert record['error']['details'] == {'written_bytes': 5, 'reason': 'short_write'}
    assert record['context']['action_id'] == 1


async def test_recovered_unknown_intent_has_error_without_resending(monkeypatch, motor_database):
    runtime, repo, writer, row = motor_flow_world(monkeypatch, motor_database, (3000,))
    from camctl.motor.models import PrepareSendRequest
    from camctl.persistence.repositories.motor import MotorRepository
    from ..persistence.test_motor_transactions import NOW
    from camctl.contracts.values import new_operation_key
    key = new_operation_key()
    prepared = await runtime.database.write(
        lambda owned: MotorRepository().prepare_send(PrepareSendRequest(1, NOW, NOW), key, owned),
        key=key, description="保存待恢复的原电机意图")
    assert prepared.value.permit is not None
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await runtime.advance()
    writer.send.assert_not_called()
    assert len(lines) == 1
    assert json.loads(lines[0])['error']['code'] == 'motor_notification_unconfirmed'


async def test_normal_expiration_has_no_error(monkeypatch, motor_database):
    runtime, _, writer, _ = motor_flow_world(monkeypatch, motor_database, (1000, 2001))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await runtime.advance()
    writer.send.assert_not_called()
    assert lines == []


async def test_notification_error_graph_reaches_public_diagnostic(monkeypatch, motor_database):
    runtime, _, writer, _ = motor_flow_world(monkeypatch, motor_database)
    failure = BrokenPipeError(32, 'notification pipe closed')
    failure.__cause__ = OSError(5, 'pipe transport reason')
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 0, 'broken_pipe', 32, failure)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await runtime.advance()
    assert len(lines) == 1
    error = json.loads(lines[0])['details']['notification']['exception']
    assert error['type'] == 'builtins.BrokenPipeError'
    assert error['cause']['errno'] == 5
