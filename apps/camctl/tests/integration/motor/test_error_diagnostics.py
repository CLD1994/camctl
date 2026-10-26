"""电机错误即使可靠保存并继续会话，也必须交付ERROR。"""
import json

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.motor.models import MotorActionFacts
from camctl.motor.notification import NotificationWriteResult, WriteKind
from camctl.motor.service import advance_motor
from unit.motor.test_service import make_runtime, PENDING

pytestmark = pytest.mark.asyncio


async def test_failed_notification_preserves_result_details(monkeypatch):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 5, 'short_write', 5)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await advance_motor(1, runtime)
            assert repo.finish_send.call_count == 1
            assert lines == []
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'motor_notification_failed'
    assert record['error']['details'] == {'written_bytes': 5, 'reason': 'short_write', 'errno': 5}
    assert record['context']['action_id'] == 1


async def test_recovered_unknown_intent_has_error_without_resending(monkeypatch):
    runtime, repo, writer, row = make_runtime(monkeypatch, (3000,))
    repo.read_facts.side_effect = None
    repo.read_facts.return_value = MotorActionFacts({**row, 'status': 2}, PENDING)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await advance_motor(1, runtime)
    writer.send.assert_not_called()
    assert len(lines) == 1
    assert json.loads(lines[0])['error']['code'] == 'motor_notification_unconfirmed'


async def test_normal_expiration_has_no_error(monkeypatch):
    runtime, _, writer, _ = make_runtime(monkeypatch, (1000, 2001))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await advance_motor(1, runtime)
    writer.send.assert_not_called()
    assert lines == []


async def test_notification_error_graph_reaches_public_diagnostic(monkeypatch):
    runtime, _, writer, _ = make_runtime(monkeypatch)
    failure = BrokenPipeError(32, 'notification pipe closed')
    failure.__cause__ = OSError(5, 'pipe transport reason')
    writer.send.return_value = NotificationWriteResult(WriteKind.FAILED, 0, 'broken_pipe', 32, failure)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await advance_motor(1, runtime)
    assert len(lines) == 1
    error = json.loads(lines[0])['details']['notification']['exception']
    assert error['type'] == 'builtins.BrokenPipeError'
    assert error['cause']['errno'] == 5
