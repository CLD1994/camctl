"""清理及发布在可靠结果保存后仍交付完整ERROR，纯协作者隔离。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs import handoff, work_files
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from unit.outputs.test_cleanup_query_save_responsibility import (
    _runtime, _saved, _actual_result, _TICKET, original_config_reader,
)
from unit.outputs.test_cleanup_delete_save_responsibility import _pending
from unit.outputs.test_work_files import _facts

pytestmark = pytest.mark.asyncio


async def test_failed_query_saved_with_reliable_absence_emits_original_error_once():
    from camctl.outputs.cleanup_flow import _run_query
    actual = _actual_result(False, status=AttemptStatus.FAILED)
    runtime = _runtime(actual, _saved(AttemptStatus.FAILED))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            assert await _run_query(runtime, _TICKET, 91) is False
            assert lines == []
            assert await _run_query(runtime, _TICKET, 91) is False
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'device_error'
    assert record['error']['details']['exit_code'] == 7
    assert record['ticket']['attempt_id'] == _TICKET.attempt_id
    assert record['context']['cleanup_item_id'] == 91
    assert record['outcome']['call_info']['remote_exit_code'] == 7


async def test_delete_legacy_failure_retains_error_and_observation():
    from camctl.outputs.cleanup_flow import _save_delete_return
    from camctl.persistence.repositories.outputs import OutputsRepository
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    runtime.outputs = create_autospec(OutputsRepository, instance=True)
    runtime.outputs.finish_cleanup_attempt.return_value = _saved(AttemptStatus.FAILED)
    pending = _pending(runtime, AttemptTicket(1, 'delete', '91', 'delete/91', 7))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await _save_delete_return(runtime, pending)
            await _save_delete_return(runtime, pending)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error'] == {'code': 'local_io_failed', 'stage': 'unlink',
                               'details': {'path': 'file', 'errno': 5}}
    assert record['details']['observations'][0]['type'] == 'file_absent'


async def test_unperformed_canceled_query_does_not_emit_error():
    from camctl.outputs.cleanup_flow import _not_dispatched, _run_query
    runtime = _runtime(_not_dispatched('query'), _saved(AttemptStatus.FAILED))
    from camctl.outputs.cleanup_flow import _not_dispatched_evidence
    runtime.evidence = _not_dispatched_evidence(runtime.evidence, _TICKET)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            assert await _run_query(runtime, _TICKET, 91) is None
    assert lines == []


@pytest.mark.parametrize('operation', ['query', 'delete'])
async def test_work_file_io_failure_saved_and_logged_without_repeating_io(monkeypatch, operation):
    failure = OSError(5, '文件IO失败')
    calls = []
    async def thread_call(callback, *args):
        calls.append(callback)
        if callback is work_files._observe_work_file:
            return failure if operation == 'query' else True
        raise failure
    monkeypatch.setattr(work_files.asyncio, 'to_thread', thread_call)
    from camctl.persistence.repositories.outputs import OutputsRepository
    repository = create_autospec(OutputsRepository, instance=True)
    repository.save_cleanup_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    database = SimpleNamespace(write=AsyncMock(return_value=DbOutcome(DbOutcomeKind.COMPLETED)))
    context = work_files.WorkFileContext(repository, database, Path('/staging'), 1000,
        work_files.WorkFileLimits(1, 1))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            result = await work_files._perform_deletion(_facts(), context, advance_cursor=False)
            assert result[0] is work_files.WorkFileSingleOutcome.FAILED
            assert lines == []
    assert len(calls) == (1 if operation == 'query' else 2)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['stage'] == f'work_file_{operation}'
    assert record['error']['type'] == 'builtins.OSError'
    assert record['context']['file_id'] == 28
    assert record['details']['failure']['code'] == 'work_file_delete_failed'


async def test_handoff_failure_is_logged_even_when_reobservation_confirms_published(monkeypatch):
    failure = PermissionError(13, '移动权限失败')
    first = handoff.DeliveryDecision(handoff.DeliveryHandoffOutcome.PUBLISH)
    retry = handoff.DeliveryDecision(handoff.DeliveryHandoffOutcome.DELIVERED_LOCALLY)
    async def thread_call(callback, *args):
        return next(decisions)
    decisions = iter([first, retry])
    monkeypatch.setattr(handoff.asyncio, 'to_thread', thread_call)
    monkeypatch.setattr(handoff, '_save_intent', AsyncMock())
    monkeypatch.setattr(handoff, '_save_publication', AsyncMock())
    monkeypatch.setattr(handoff, '_staging_ref', Mock(return_value=(None, None, None)))
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.host_files.io import DirectorySyncStage
    monkeypatch.setattr(handoff, 'publish_file', AsyncMock(return_value=PublishResult(
        PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED, False, 'move_failed', failure)))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            result = await handoff._publish_delivery(41, SimpleNamespace(directories=None), SimpleNamespace(file_name='41.bin'))
    assert result.phase is handoff.DeliveryPhase.PUBLISHED
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['type'] == 'builtins.PermissionError'
    assert record['context']['delivery_id'] == 41


async def test_member_rejection_logs_original_request_once_during_save_retry():
    from camctl.outputs.cleanup_flow import FailCleanupItem, CleanupItemSaved, CleanupItemDisposition
    from camctl.outputs.cleanup_ownership import save_cleanup_request
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    runtime.outputs.fail_cleanup_item.side_effect = [
        DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError('save failed')),
        DbOutcome(DbOutcomeKind.COMPLETED, CleanupItemSaved(CleanupItemDisposition.SAVED, 91))]
    request = FailCleanupItem(91, 'file_delete_failed', {'output_id': '31'}, 1000)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await save_cleanup_request(runtime, 'fail_cleanup_item', request)
            await save_cleanup_request(runtime, 'fail_cleanup_item', request)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'file_delete_failed'
    assert record['error']['details'] == {'output_id': '31'}
    assert record['context']['cleanup_item_id'] == 91


async def test_target_fixed_result_returns_and_logs_each_business_rejection():
    from camctl.operations.models import ErrorValue
    from camctl.outputs.cleanup_flow import CleanupTargetsSaved, CleanupTargetsDisposition, FixCleanupTargets
    from camctl.outputs.cleanup_ownership import save_cleanup_request
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.FAILED))
    failure = ErrorValue('output_not_found', 'output_selection', {'requested_output_id': '81'})
    runtime.outputs.fix_cleanup_targets.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CleanupTargetsSaved(CleanupTargetsDisposition.SAVED, (91,), ((91, failure),)))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await save_cleanup_request(runtime, 'fix_cleanup_targets', FixCleanupTargets(11, 1000), action=True)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'output_not_found'
    assert record['context']['cleanup_item_id'] == 91
    assert record['error']['details']['requested_output_id'] == '81'


async def test_query_target_exception_graph_is_preserved(monkeypatch):
    from camctl.outputs import cleanup_flow
    failure = cleanup_flow.LocalArtifactTargetError('saved path rejected')
    failure.__cause__ = ValueError('original path reason')
    monkeypatch.setattr(cleanup_flow, '_prepare_cleanup_call', AsyncMock(side_effect=failure))
    runtime = _runtime(_actual_result(None), _saved(AttemptStatus.FAILED))
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await cleanup_flow._run_query(runtime, _TICKET, 91)
    assert len(lines) == 1
    error = json.loads(lines[0])['run_error']
    assert error['type'].endswith('.LocalArtifactTargetError')
    assert error['cause']['message'] == 'original path reason'


async def test_restored_saved_query_does_not_generate_old_error_again():
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import (
        PendingCleanupQuery, CleanupQueryPhase, CleanupQueryConsumer, _run_query)
    actual = _actual_result(False, status=AttemptStatus.FAILED)
    runtime = _runtime(actual, _saved(AttemptStatus.FAILED))
    pending = PendingCleanupQuery(runtime.database.metadata.instance_id, 91,
        CleanupQueryConsumer.AFTER_DELETE, _TICKET, actual, None, 1000, 300,
        new_operation_key(), phase=CleanupQueryPhase.SAVED)
    runtime.pending_queries[(_TICKET.run_id, _TICKET.attempt_id)] = pending
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            assert await _run_query(runtime, _TICKET, 91) is False
    assert lines == []
    runtime.driver.query_file.assert_not_called()


async def test_query_validation_failure_retains_original_exception(monkeypatch):
    from camctl.logging_runtime.diagnostics import flush_diagnostics
    from camctl.outputs import cleanup_flow
    from camctl.operations.validation import OutcomeValidationError
    failure = TypeError('driver returned an invalid value')
    monkeypatch.setattr(cleanup_flow, '_prepare_query_finish', Mock(side_effect=failure))
    runtime = _runtime(_actual_result(None), _saved(AttemptStatus.FAILED))
    lines = []
    with error_diagnostics_context(None, lines.append):
        with pytest.raises(OutcomeValidationError):
            async with diagnostic_scope():
                await cleanup_flow._run_query(runtime, _TICKET, 91)
        assert lines == []
        await flush_diagnostics()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['type'] == 'builtins.TypeError'
    assert record['error']['message'] == str(failure)
    assert record['details']['ticket']['attempt_id'] == _TICKET.attempt_id
