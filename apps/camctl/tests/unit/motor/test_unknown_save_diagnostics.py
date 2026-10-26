"""原电机提交未知及其独立核实错误都不能被后续回执覆盖。"""
import json
from unittest.mock import AsyncMock

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.motor.models import PrepareOutcome, PrepareSendResult
from camctl.motor.service import advance_motor
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.session.service import StateDbFailure
from .test_service import make_runtime, KEY

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('verification', ['success', 'rejected', 'raises'])
async def test_unknown_write_logs_original_error_and_independent_verification_error(monkeypatch, verification):
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    original = ValueError('original commit error')
    original.__cause__ = OSError(5, 'commit IO error')
    repo.prepare_send.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=original,
        secondary_errors=(RuntimeError('commit cleanup error'),))
    verification_error = OSError(13, 'verification error')
    if verification == 'success':
        repo.verify_operation.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
            PrepareSendResult(PrepareOutcome.ALREADY))
    elif verification == 'rejected':
        repo.verify_operation.return_value = DbOutcome(DbOutcomeKind.ROLLED_BACK, error=verification_error)
    else:
        repo.verify_operation.side_effect = verification_error
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            if verification == 'success':
                await advance_motor(1, runtime)
            else:
                with pytest.raises(StateDbFailure):
                    await advance_motor(1, runtime)
    assert len(records) == (1 if verification == 'success' else 2)
    assert 'original commit error' in records[0]
    assert 'commit IO error' in records[0] and 'commit cleanup error' in records[0]
    if verification != 'success':
        assert 'verification error' in records[1]
    assert json.loads(records[0])['context']['action_id'] == 1
    repo.prepare_send.assert_called_once()
    repo.verify_operation.assert_called_once()
    assert repo.verify_operation.call_args.args[1] == KEY
    writer.send.assert_not_called()


@pytest.mark.parametrize('with_failure', [False, True])
async def test_write_control_preserves_identity_and_only_logs_actual_failure(monkeypatch, with_failure):
    import asyncio
    from camctl.motor.models import PrepareSendRequest
    from camctl.motor.service import _save
    from camctl.persistence.models import _attach_db_outcome
    runtime, repo, writer, _ = make_runtime(monkeypatch)
    control = asyncio.CancelledError('worker cancellation')
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN,
        error=OSError(5, 'commit IO failure') if with_failure else None, control=control)
    _attach_db_outcome(control, outcome)
    runtime.database.write = AsyncMock(side_effect=control)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(asyncio.CancelledError) as caught:
                await _save(runtime, 'prepare_send', PrepareSendRequest(1, 1000, 1000), '意图事务')
    assert caught.value is control
    assert len(records) == int(with_failure)
    if records:
        assert 'commit IO failure' in records[0]
    runtime.database.write.assert_awaited_once()
    repo.verify_operation.assert_not_called()
    writer.send.assert_not_called()
