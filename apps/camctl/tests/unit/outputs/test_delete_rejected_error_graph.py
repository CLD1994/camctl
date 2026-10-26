"""删除原结果保存未完成时，逐层消费保留同一数据库实际错误。"""
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs import cleanup_flow
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories.outputs import OutputsRepository
from .test_cleanup_query_save_responsibility import _runtime, _saved, _actual_result, original_config_reader
from .test_cleanup_delete_save_responsibility import _pending

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('kind', [DbOutcomeKind.NOT_EXECUTED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_original_delete_rejection_carries_complete_database_outcome(monkeypatch, kind):
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    runtime.outputs = create_autospec(OutputsRepository, instance=True)
    error = ValueError('original delete save failure')
    error.__cause__ = OSError(5, 'delete save IO')
    outcome = DbOutcome(kind, error=error, secondary_errors=(RuntimeError('save close failure'),))
    runtime.outputs.finish_cleanup_attempt.return_value = outcome
    pending = _pending(runtime, AttemptTicket(1, 'delete', '91', 'delete/91', 7))
    runtime.pending_deletes[91] = pending
    step = await cleanup_flow._save_delete_return(runtime, pending)
    assert db_outcome_for(step.exception) is outcome
    monkeypatch.setattr(cleanup_flow, 'delete_source_file', AsyncMock(return_value=step))
    with pytest.raises(ConsistencyError) as caught:
        await cleanup_flow._advance_cleanup_round(runtime)
    assert caught.value is step.exception
    assert db_outcome_for(caught.value).error is error
    assert db_outcome_for(caught.value).secondary_errors == outcome.secondary_errors


async def test_delete_save_control_keeps_original_identity_and_actual_result():
    import asyncio
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    runtime.outputs = create_autospec(OutputsRepository, instance=True)
    control = asyncio.CancelledError('original worker cancellation')
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError(5, 'save IO failure'), control=control)
    runtime.outputs.finish_cleanup_attempt.return_value = outcome
    pending = _pending(runtime, AttemptTicket(1, 'delete', '91', 'delete/91', 7))
    with pytest.raises(asyncio.CancelledError) as caught:
        await cleanup_flow._save_delete_return(runtime, pending)
    assert caught.value is control
    assert db_outcome_for(control) is outcome
    assert not pending.saved


async def test_original_supervision_result_replay_logs_one_complete_graph():
    import json
    from camctl.bootstrap.cleanup_assembly import _exception_facts
    from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
    from camctl.devices.ports import DeviceCallResult
    from camctl.outputs.cleanup_flow import CleanupSupervisionDiagnostic, LocalArtifactTaskFailure
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    runtime.outputs = create_autospec(OutputsRepository, instance=True)
    runtime.outputs.finish_cleanup_attempt.return_value = _saved(AttemptStatus.SUCCEEDED)
    pending = _pending(runtime, AttemptTicket(1, 'delete', '91', 'delete/91', 7))
    pending.actual = DeviceCallResult(pending.actual.observations, None)
    try:
        try:
            raise OSError(13, 'original runner OS failure', '/staging/source')
        except OSError as cause:
            raise ValueError('original runner supervision failure') from cause
    except ValueError as original:
        diagnostic = CleanupSupervisionDiagnostic(tuple(_exception_facts(original, 'runner')), original)
        carrier = LocalArtifactTaskFailure(pending.actual, diagnostic)
        carrier.__cause__ = original
    pending.supervision_diagnostic = diagnostic
    pending.diagnostic_exception = carrier
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await cleanup_flow._save_delete_return(runtime, pending)
            await cleanup_flow._save_delete_return(runtime, pending)
            with pytest.raises(ConsistencyError) as delivery:
                cleanup_flow._deliver_supervision(pending, '删除')
            assert cleanup_flow._deliver_supervision(pending, '删除') is None
    assert len(records) == 1
    assert 'original runner supervision failure' in records[0]
    assert '"errno":13' in records[0]
    assert json.loads(records[0])['context']['cleanup_item_id'] == 91
    assert delivery.value.__cause__ is diagnostic.exception
    runtime.outputs.finish_cleanup_attempt.assert_called_once()


async def test_database_control_and_supervision_error_keep_original_control():
    import asyncio
    import json
    from camctl.operations.error_diagnostics import exception_diagnostic
    from camctl.persistence.models import _attach_db_outcome
    from camctl.bootstrap.cleanup_assembly import _exception_facts
    from camctl.outputs.cleanup_flow import CleanupSupervisionDiagnostic
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    pending = _pending(runtime, AttemptTicket(1, 'delete', '91', 'delete/91', 7))
    runner = ValueError('runner supervision failure')
    runner.__cause__ = OSError(13, 'runner original IO failure')
    pending.supervision_diagnostic = CleanupSupervisionDiagnostic(tuple(_exception_facts(runner, 'runner')), runner)
    control = asyncio.CancelledError('original database control')
    original_cause = OSError(5, 'original save failure')
    control.__cause__ = original_cause
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=original_cause, control=control)
    _attach_db_outcome(control, outcome)
    with pytest.raises(asyncio.CancelledError) as caught:
        cleanup_flow._raise_supervision_with_error(pending, control, 'original save pending')
    assert caught.value is control
    assert control.__cause__ is original_cause
    assert db_outcome_for(control) is outcome
    assert not pending.supervision_diagnostic_delivered
    document = json.dumps(exception_diagnostic(control), ensure_ascii=False)
    assert 'runner supervision failure' in document and 'runner original IO failure' in document
    assert 'errno' in document
