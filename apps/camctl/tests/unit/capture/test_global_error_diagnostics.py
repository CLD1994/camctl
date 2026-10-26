"""拍摄结果消费者的实际错误交给公共诊断输出，协作者均为内存替身。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture.handlers import (
    PendingCallResult, PendingCaptureCompletion, StartCloseRequest,
    resume_capture_completions,
)
from camctl.capture.media import RecordingFailure
from camctl.capture.media_flow import CaptureProcessingSaves
from camctl.capture.preparation import PendingPreparation, _save_owner
from camctl.capture.processing import (
    CheckPhase, CheckResultSave, MediaObservation, ProcessingError, RepairOutcome, RepairResultSave,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceContract
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome, StaleRunFinish
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis, ValidatedOutcome,
)
from camctl.operations.tool_diagnostics import ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, CaptureResult, FinishBindingFailure, FinishCapture, FinishRecordingResults,
)
from camctl.persistence.repositories.operations import OperationRepository
from .test_stop_result_handoff import _runtime

pytestmark = pytest.mark.asyncio


def _outcome(operation='stop'):
    raw = b'camera error\xff\n'
    stream = ToolStream(ToolStreamState.COMPLETE, raw)
    return CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'device', {'operation': operation}),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('returned', 1)),
        diagnostics=ToolDiagnostics(1, (ToolCallOutput(1, operation, stream, stream),)))


def _documents(records):
    return [json.loads(record) for record in records]


@pytest.mark.parametrize('operation', ['control', 'stop', 'query', 'result'])
async def test_call_error_is_logged_before_failed_business_save_and_replay_logs_once(mocker, operation):
    runtime, _, _, _ = _runtime(mocker)
    ticket = AttemptTicket(1, operation, '71', operation + '/21', 9)
    outcome = _outcome(operation)
    validated = ValidatedOutcome(outcome, EvidenceContract('returned', 1, operation, frozenset()), (), ticket)
    pending = PendingCallResult(new_operation_key(), AttemptFinish(ticket, validated, 100), None)
    identity = (ticket.run_id, ticket.attempt_id)
    runtime.pending_start_results[identity] = pending
    writer = mocker.patch.object(runtime, '_write_call_result', autospec=True,
        return_value=DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError('save unknown')))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(ConsistencyError):
                await runtime._save_call_result(identity, pending)
        writer.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
        async with diagnostic_scope():
            await runtime._save_call_result(identity, runtime.pending_start_results[identity])
    document, = _documents(records)
    assert document['error']['details'] == {'operation': operation}
    assert document['ticket']['run_id'] == 9
    assert document['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()
    assert not runtime.pending_start_results


@pytest.mark.parametrize('wrapper', ['ordinary', 'results', 'start_close', 'binding'])
async def test_failed_capture_is_logged_when_combined_database_save_completes(wrapper, monkeypatch):
    failure = RecordingFailure('capture_failed', {'activity_id': '71', 'reason': 'no_outputs'})
    command = FinishCapture(21, (), OutputCatalogFacts(21, True), 100, failure)
    if wrapper == 'results':
        command = FinishRecordingResults(command, 9)
    elif wrapper == 'start_close':
        command = StartCloseRequest(StaleRunFinish(('start/21',), RunOutcome.FAILED, 100,
            ErrorValue('device_error', 'device')), command)
    elif wrapper == 'binding':
        monkeypatch.setattr('camctl.persistence.repositories.capture.validate_error_details', lambda code, details: None)
        command = FinishBindingFailure(21, 100,
            RecordingFailure('device_binding_unavailable', {
                'device_id': 'cam-1', 'expected_driver_id': 'driver-1', 'reason': 'missing'}))
    pending = PendingCaptureCompletion(new_operation_key(), command)
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(4, 2, ()))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await resume_capture_completions(database, pending_capture_completions={21: pending},
                capture=create_autospec(CaptureRepository, instance=True))
    document, = _documents(records)
    assert document['context']['action_id'] == 21
    assert document['error']['code'] == ('device_binding_unavailable' if wrapper == 'binding' else 'capture_failed')


@pytest.mark.parametrize('kind', ['check', 'repair', 'issues'])
async def test_media_error_is_logged_when_original_request_is_retried(kind):
    error = ProcessingError('probe_unconfirmed', 'probe', {'reason': 'duration unavailable'})
    command = (RepairResultSave(3, RepairOutcome.FAILED, 100, error=error) if kind == 'repair'
        else CheckResultSave(3, MediaObservation(CheckPhase.COMPLETED, duration_s=1, issues=(error,))
             if kind == 'issues' else MediaObservation(CheckPhase.UNCONFIRMED, error=error), 100))
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = (21,)
    database.write.side_effect = (DbOutcome(DbOutcomeKind.UNKNOWN), DbOutcome(DbOutcomeKind.COMPLETED))
    saves = CaptureProcessingSaves(database)
    save = saves.save_repair_result if kind == 'repair' else saves.save_check_result
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await save(command)
        async with diagnostic_scope():
            await save(command)
    document, = _documents(records)
    assert document['error']['code'] == 'probe_unconfirmed'
    assert document['context']['processing_id'] == 3
    assert document['context']['action_id'] == 21
    assert not saves.pending


async def test_preparation_original_failed_finish_logs_even_after_database_success():
    ticket = AttemptTicket(1, 'control', '71', 'prepare/71/' + 'a' * 32, 9)
    outcome = _outcome('prepare')
    validated = ValidatedOutcome(outcome, EvidenceContract('returned', 1, 'control', frozenset()), (), ticket)
    owner = PendingPreparation(21, 71, 'a' * 32, None, None, None, ticket=ticket,
        finish=AttemptFinish(ticket, validated, 100, run_finish=RunFinish(RunOutcome.FAILED, outcome.error)))
    database = create_autospec(BusinessDatabase, instance=True)
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await _save_owner(owner, database, create_autospec(OperationRepository, instance=True))
    document, = _documents(records)
    assert document['error']['details']['operation'] == 'prepare'
    assert document['context']['action_id'] == 21
    assert owner.saved


async def test_normal_not_dispatched_control_does_not_log_error(mocker):
    runtime, _, _, _ = _runtime(mocker)
    ticket = AttemptTicket(1, 'control', '71', 'start/21', 9)
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue('canceled', 'dispatch'), settlement=Settlement(
            SettlementBasis.NOT_DISPATCHED, EvidenceValue('dispatch_prevented', 1)))
    validated = ValidatedOutcome(outcome, EvidenceContract('dispatch_prevented', 1, 'control', frozenset()), (), ticket)
    pending = PendingCallResult(new_operation_key(), AttemptFinish(ticket, validated, 100), None,
        canceled_unstarted=True)
    identity = (9, 1)
    runtime.pending_start_results[identity] = pending
    mocker.patch.object(runtime, '_write_call_result', autospec=True, return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await runtime._save_call_result(identity, pending)
    assert not records


async def test_dispatch_keeps_all_handled_errors_and_continues_next_action(monkeypatch):
    from camctl.capture import dispatch
    from camctl.scheduling.service import ActionDescriptor
    errors = [ValueError('first'), RuntimeError('second')]
    handler = AsyncMock(side_effect=errors + [None])
    monkeypatch.setattr(dispatch, 'capture_handler', lambda _: handler)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            outcomes = await dispatch.dispatch_ready(object(), [ActionDescriptor(i, 'camera_record', True) for i in (1, 2, 3)])
    assert handler.await_count == 3 and len(outcomes) == 3
    documents = _documents(records)
    assert [document['error']['message'] for document in documents] == ['first', 'second']
    assert [document['context']['action_id'] for document in documents] == [1, 2]


async def test_baseline_freezes_full_actual_error_before_return_model_discards_tool_outcome():
    from decimal import Decimal
    from camctl.capture.baseline_models import BaselineReadIntent, BaselineRef, BaselineState
    from camctl.capture.baseline_reads import PendingBaselineRead, _read_and_freeze
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryRead, DirectoryReader
    from camctl.operations.process import RawToolOutcome
    intent = BaselineReadIntent(21, 71, DeviceBinding('cam-1', 'driver-1'), ('/DCIM',), None,
        Decimal(3), BaselineRef(71, BaselineState.COLLECTING, None, None, 0, 0), 1,
        new_operation_key(), new_operation_key(), 100)
    owner = PendingBaselineRead(intent, intent_view=SimpleNamespace(call_id=11))
    directory = create_autospec(DirectoryReader, instance=True)
    actual = RawToolOutcome(None, b'file metadata', 'timeout', None, stderr=b'directory\xff')
    directory.read_directory.return_value = DirectoryRead(actual,
        ErrorValue('device_error', 'directory', {'path': '/DCIM'}), None)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await _read_and_freeze(owner, directory, lambda: 101)
    document, = _documents(records)
    assert document['details']['tool_outcome']['stderr']['hex'] == b'directory\xff'.hex()
    assert document['details']['stdout_hex'] == b'file metadata'.hex()
    assert document['context']['read_no'] == 1
    assert owner.return_request.returned.outcome is None


async def test_failed_result_page_and_final_attempt_share_one_diagnostic(mocker):
    from decimal import Decimal
    from camctl.capture.result_scans import PendingResultScan, advance_result_scan
    from camctl.capture.result_pages import ResultPageRef
    from .test_result_scan import _runtime as scan_runtime, _page
    from .test_result_pages import _TICKET
    scan_world = scan_runtime()
    scan_world.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ResultPageRef(_TICKET, 1, 10, None))
    page = _page(error=ErrorValue('device_error', 'results', {'reason': 'directory unavailable'}))
    scan_world.results.list_page.return_value = page
    scan = PendingResultScan(_TICKET, None, Decimal(3))
    runtime, _, _, _ = _runtime(mocker)
    identity = (_TICKET.run_id, _TICKET.attempt_id)
    runtime.pending_result_scans[identity] = scan
    mocker.patch.object(runtime, '_write_call_result', autospec=True,
        return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            assert await advance_result_scan(scan, runtime=scan_world)
            pending = PendingCallResult(new_operation_key(), AttemptFinish(_TICKET, scan.last_outcome, 100), None)
            runtime.pending_start_results[identity] = pending
            await runtime._save_call_result(identity, pending)
    document, = _documents(records)
    assert document['context']['page_no'] == 1
    assert document['error']['details'] == {'reason': 'directory unavailable'}
    assert scan.reading_context.closed


@pytest.mark.parametrize('kind', ['exception', 'tool', 'invalid', 'probe'])
async def test_handled_segment_probe_errors_keep_actual_diagnostic_when_save_is_unknown(monkeypatch, kind):
    from decimal import Decimal
    from pathlib import Path
    from camctl.capture import segment_inspections as module
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.capture.media_flow import MediaFlow
    from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
    from camctl.host_files.media import MediaProbe
    from camctl.host_files.models import FilePurpose, FileRef
    from camctl.host_files.tasks import FileTaskId, FileTaskResult
    from camctl.outputs.qualification import OperationConfig
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRef
    target = CoverageTarget(CoverageMetric.DURATION_S, 60)
    config = OperationConfig(3, Decimal(3), Decimal(0))
    request = module.SegmentInspectionRequest(21, 4, target, config, 100)
    ref = SegmentInspectionRef(3, 21, 2, 4, 11, 1,
        {'metric': 'duration_s', 'value': 60},
        {'max_attempts': 3, 'timeout_s': Decimal(3), 'retry_interval_s': Decimal(0)})
    flow = create_autospec(MediaFlow, instance=True)
    flow.database = create_autospec(BusinessDatabase, instance=True)
    flow.database.write.side_effect = [DbOutcome(DbOutcomeKind.COMPLETED, ref), DbOutcome(DbOutcomeKind.UNKNOWN)]
    flow.database.read.side_effect = [SimpleNamespace(copy=None), ref, ref, (2, 0), (11, 'a.mp4'), ref]
    flow.pending_segment_inspection_results = {}
    flow.pending_read_results = {}
    flow.occurred_at = lambda: 101
    probe = AsyncMock()
    failure = ValueError('invalid probe response')
    failure.__cause__ = RuntimeError('original tool failure')
    if kind == 'exception':
        probe.side_effect = failure
    else:
        probe.return_value = (FileTaskResult(FileTaskId('probe'), True, error='tool failed') if kind == 'tool'
            else FileTaskResult(FileTaskId('probe'), True, value=None) if kind == 'invalid'
            else FileTaskResult(FileTaskId('probe'), True, value=MediaProbe(None, 'invalid_structure: wrong streams')))
    flow.tools = SimpleNamespace(probe_segment=probe)
    monkeypatch.setattr(module, 'retry_read_business', AsyncMock())
    monkeypatch.setattr(module, 'retry_read_ends', AsyncMock())
    monkeypatch.setattr(module, '_obtain_input', AsyncMock(return_value=InputStep(InputPhase.INPUT_READY,
        copy_id=5, input_file=FileRef(6, FilePurpose.RECORDING_INPUT, 'input.mp4', Path('/staging')))))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            result = await module.inspect_segment(flow, request)
    document, = _documents(records)
    assert result.reason == 'result_save_unknown'
    assert document['context']['action_id'] == 21
    assert document['context']['inspection_id'] == 3
    if kind == 'exception':
        assert document['error']['message'] == 'invalid probe response'
        assert document['error']['cause']['message'] == 'original tool failure'


@pytest.mark.parametrize('later_failed', [False, True])
async def test_tolerated_action6_setting_error_is_logged_with_the_outer_result(later_failed):
    from decimal import Decimal
    from camctl.logging_runtime.diagnostics import record_call_outcome
    from camctl.operations.process import LocalExit, RawToolOutcome
    from ..devices.test_adb_camera_driver import (
        Action6ContractTransport, _action6_contract_driver, _request,
    )
    bitrate = ('simulate_device', '-s', 'bitrate', '2')
    transport = Action6ContractTransport(aperture_payload='e3', overrides={
        bitrate: RawToolOutcome(LocalExit(exit_code=9), b'bitrate failed', None, None, stderr=b'bitrate stderr')
    } if later_failed else None)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            result = await _action6_contract_driver(transport).control(_request())
            record_call_outcome(result.outcome, ticket=_request().ticket)
    document, = _documents(records)
    outcome = document.get('outcome') or document['details']['outcome']
    calls = outcome['settlement']['evidence']['data']['calls']
    assert any(call['error'] is not None and call['error']['details'].get('response_payload') == 'e3'
        for call in calls)
    if later_failed:
        assert document['error']['details']['reason'] == 'exit_nonzero'


async def test_capture_completion_preserves_all_database_errors_for_fatal_diagnostic():
    from camctl.persistence.repositories.capture import FinishCanceledCapture
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 100))
    primary = OSError('database failed')
    secondary = RuntimeError('observer failed')
    database = create_autospec(BusinessDatabase, instance=True)
    database.write.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=primary,
        secondary_errors=(secondary,))
    with pytest.raises(ConsistencyError) as raised:
        await resume_capture_completions(database, pending_capture_completions={21: pending},
            capture=create_autospec(CaptureRepository, instance=True))
    assert isinstance(raised.value.__cause__, BaseExceptionGroup)
    assert raised.value.__cause__.exceptions == (primary, secondary)


@pytest.mark.parametrize('operation', ['probe', 'repair'])
@pytest.mark.parametrize('ran', [True, False])
async def test_uncompleted_media_tool_logs_actual_failure_and_keeps_cancel_control(operation, ran):
    from camctl.capture.media import execute_check, execute_repair
    from camctl.host_files.tasks import FileTaskId, FileTaskResult
    from .test_media_execution import (
        _Saves, _Tools, _status, _check_context, _repair_context, _saved, _REGISTERED_OUTPUT,
    )
    result = FileTaskResult(FileTaskId(operation), ran,
        error='file_conflict: resource occupied' if ran else None)
    tools = _Tools(result if operation == 'probe' else None, result if operation == 'repair' else None)
    saves = _Saves({'start_repair_output': _saved(_REGISTERED_OUTPUT)})
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            step = (await execute_check(_check_context(_status(), saves, tools)) if operation == 'probe'
                else await execute_repair(_repair_context(_status(repair_state=3), saves, tools)))
    assert step.error is not None
    if ran:
        document, = _documents(records)
        assert document['error']['message'] == 'file_conflict: resource occupied'
    else:
        assert not records


@pytest.mark.parametrize('kind', ['presence', 'completion'])
async def test_file_observation_error_survives_successful_business_fact_save(kind):
    from camctl.capture.files import FilePresenceSave, FileCompletionSave
    from camctl.capture.handlers import FileFactStage
    from .test_file_fact_identity import _runtime_with_saved_ownership
    runtime, _, _ = _runtime_with_saved_ownership()
    runtime.database = create_autospec(BusinessDatabase, instance=True)
    runtime.database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, object())
    error = {'code': 'device_error', 'stage': 'metadata', 'details': {'reason': 'unavailable'}}
    command = FilePresenceSave(3, 2, 100, error=error) if kind == 'presence' else FileCompletionSave(3, 4, 100, error=error)
    stage = FileFactStage.PRESENCE if kind == 'presence' else FileFactStage.COMPLETION
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await runtime._save_file_fact((1, 'original'), stage, command)
    document, = _documents(records)
    assert document['error'] == error
    assert document['context']['action_id'] == 1
    assert document['context']['file_id'] == 3


async def test_handled_segment_save_error_keeps_secondary_errors_in_public_diagnostic():
    from camctl.capture.segment_inspections import _save_result
    from camctl.persistence.repositories.segment_inspections import InspectionResultSave
    secondary = OSError('database notification failed')
    database = create_autospec(BusinessDatabase, instance=True)
    database.write.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, secondary_errors=(secondary,))
    flow = SimpleNamespace(database=database, pending_segment_inspection_results={})
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            assert not await _save_result(flow, InspectionResultSave(3, 2, {'probe': None}, 100))
    document, = _documents(records)
    assert document['context']['inspection_id'] == 3
    assert document['error']['cause']['message'] == 'database notification failed'


async def test_invalid_call_diagnostic_preserves_original_outcome_before_fatal_handling(mocker):
    from camctl.logging_runtime.diagnostics import record_error
    runtime, _, ticket, _ = _runtime(mocker)
    outcome = _outcome()
    def rejected(actual):
        raise ValueError('diagnostic rejected')
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(ConsistencyError) as raised:
                await runtime.finish(ticket, outcome, diagnostics_check=rejected)
            record_error(raised.value, stage='fatal')
    document, = _documents(records)
    assert document['details']['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()
    assert document['context']['ticket']['run_id'] == ticket.run_id


async def test_preparation_validation_error_keeps_original_tool_bytes_and_exception():
    from camctl.devices.ports import DeviceCallResult
    from camctl.logging_runtime.diagnostics import record_error
    ticket = AttemptTicket(1, 'control', '71', 'prepare/71/' + 'a' * 32, 9)
    def rejected(actual):
        raise ValueError('preparation diagnostic rejected')
    owner = PendingPreparation(21, 71, 'a' * 32, None, None, None,
        ticket=ticket, actual=DeviceCallResult.from_outcome(_outcome('prepare'), diagnostics_check=rejected),
        returned_at=100)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(ValueError) as raised:
                await _save_owner(owner, create_autospec(BusinessDatabase, instance=True),
                    create_autospec(OperationRepository, instance=True))
            record_error(raised.value, stage='fatal')
    document, = _documents(records)
    assert document['error']['message'] == 'preparation diagnostic rejected'
    assert document['details']['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()


@pytest.mark.parametrize('code,stage,details,expected', [
    ('canceled', 'dispatch', {}, False),
    ('window_ended', 'dispatch', {}, False),
    ('start_preparation_timeout', 'dispatch', {}, True),
    ('transport_cancelled', 'transport', {}, False),
    ('transport_cancelled', 'transport', {'signal_failures': [{'stage': 'terminate', 'message': 'denied'}]}, True),
    ('transport_cancelled', 'transport', {'output_failure': 'pipe close failed'}, True),
])
async def test_call_control_classification_keeps_actual_timeout_and_cleanup_errors(mocker, code, stage, details, expected):
    runtime, _, ticket, _ = _runtime(mocker)
    actual = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue(code, stage, details), settlement=Settlement(
            SettlementBasis.NOT_DISPATCHED, EvidenceValue('dispatch_prevented', 1)))
    validated = ValidatedOutcome(actual, EvidenceContract('dispatch_prevented', 1, 'stop', frozenset()), (), ticket)
    pending = PendingCallResult(new_operation_key(), AttemptFinish(ticket, validated, 100), None)
    identity = (ticket.run_id, ticket.attempt_id)
    runtime.pending_start_results[identity] = pending
    mocker.patch.object(runtime, '_write_call_result', autospec=True, return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await runtime._save_call_result(identity, pending)
    assert bool(records) is expected
